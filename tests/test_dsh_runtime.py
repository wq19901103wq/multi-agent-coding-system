import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mac_system.agents import dsh_runtime
from mac_system.agents.dsh_runtime import DshACPThread, _MacMCP
from mac_system.agents.root_runtime import DynamicTool, ProjectLeadRuntimeError


class FakeProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.writes = []
        self.resume_error = None

    def feed(self, message):
        self.stdout.feed_data((json.dumps(message) + '\n').encode())

    def write(self, data):
        message = json.loads(data)
        self.writes.append(message)
        method = message.get('method')
        response = {}
        if method == 'initialize':
            response = {'protocolVersion': 1, 'agentCapabilities': {
                'mcpCapabilities': {'http': True}, 'sessionCapabilities': {'resume': {}, 'close': {}}}}
        elif method == 'session/new':
            response = {'sessionId': 'native-session', 'configOptions': [
                {'id': 'model', 'currentValue': 'native-model'}]}
        elif method == 'session/resume' and self.resume_error:
            self.feed({'id': message['id'], 'error': {'code': -32602, 'message': self.resume_error}})
            return
        elif method == 'session/cancel':
            prompt = next(m for m in reversed(self.writes) if m.get('method') == 'session/prompt')
            self.feed({'id': prompt['id'], 'result': {'stopReason': 'cancelled'}})
            return
        if method in {'initialize', 'session/new', 'session/resume', 'session/close'}:
            self.feed({'id': message['id'], 'result': response})

    async def drain(self):
        pass

    def finish(self, reason='end_turn', error=None):
        prompt = next(m for m in reversed(self.writes) if m.get('method') == 'session/prompt')
        self.feed({'id': prompt['id'], **({'error': error} if error else {'result': {'stopReason': reason}})})

    def update(self, kind, session='native-session', **values):
        self.feed({'method': 'session/update', 'params': {
            'sessionId': session, 'update': {'sessionUpdate': kind, **values}}})

    def text(self, text, message_id='answer', **kwargs):
        self.update('agent_message_chunk', messageId=message_id, content={'type': 'text', 'text': text}, **kwargs)


@pytest.fixture
async def runtime(monkeypatch, tmp_path):
    processes, launches = [], []
    async def spawn(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        launches.append((args, kwargs))
        return process
    async def stop(process, **kwargs):
        assert kwargs == {'isolated_group': True}
        process.returncode = 0
        process.stdout.feed_eof()
        process.stderr.feed_eof()
    async def start_mcp(self):
        return {'type': 'http', 'name': 'mac', 'url': 'http://127.0.0.1:1/mcp/', 'headers': []}
    monkeypatch.setattr(dsh_runtime.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(dsh_runtime, 'stop_process_tree', stop)
    monkeypatch.setattr(_MacMCP, 'start', start_mcp)
    adapter = DshACPThread(dsh_bin='/native/dsh', dsh_home=tmp_path / 'home', cwd=tmp_path,
                           tools=[], request_timeout_seconds=0.1, silence_timeout_seconds=0.1)
    adapter.processes, adapter.launches = processes, launches
    await adapter.create_thread()
    yield adapter
    await adapter.close()


async def start(runtime):
    process = runtime._process
    count = len(process.writes)
    task = asyncio.create_task(runtime.run_turn('hello'))
    async def ready():
        while len(process.writes) == count:
            await asyncio.sleep(0)
    await asyncio.wait_for(ready(), 1)
    return task, process


async def test_native_persistence_and_failed_resume_never_creates_replacement(runtime, monkeypatch):
    assert runtime.thread_id == 'native-session' and runtime.active_model == 'native-model'
    args, kwargs = runtime.launches[0]
    assert args == ('/native/dsh', '--profile', 'acp')
    assert kwargs['start_new_session']
    assert kwargs['env']['DSH_HOME'] == str(runtime.dsh_home)
    initial = runtime._process
    await runtime.resume_thread('native-session')
    assert initial.returncode == 0
    assert [m['method'] for m in runtime._process.writes] == ['initialize', 'session/resume']
    params = runtime._process.writes[-1]['params']
    assert params['cwd'] == str(runtime.cwd) and params['sessionId'] == 'native-session'
    assert params['mcpServers'][0]['name'] == 'mac'
    spawn = dsh_runtime.asyncio.create_subprocess_exec
    async def reject(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        process.resume_error = 'missing session'
        return process
    monkeypatch.setattr(dsh_runtime.asyncio, 'create_subprocess_exec', reject)
    with pytest.raises(ProjectLeadRuntimeError, match='missing session'):
        await runtime.resume_thread('missing')
    assert not any(m.get('method') == 'session/new' for m in runtime.processes[-1].writes)
    assert runtime._process is None
    assert runtime._launch_dir is None


async def test_launch_isolates_workspace_dotenv_and_preserves_session_cwd(runtime):
    dotenv = runtime.cwd / '.env'
    dotenv.write_text('HTTPS_PROXY=http://proxy.invalid:8080\n')
    initial_dir = Path(runtime.launches[0][1]['cwd'])
    initial_session = runtime._process.writes[-1]
    assert initial_session['params']['cwd'] == str(runtime.cwd)
    await runtime.resume_thread(runtime.thread_id)
    launch_dir = Path(runtime.launches[-1][1]['cwd'])
    assert launch_dir != runtime.cwd and launch_dir != initial_dir
    assert not initial_dir.exists()
    assert launch_dir.is_dir() and not list(launch_dir.iterdir())
    assert runtime._process.writes[-1]['params']['cwd'] == str(runtime.cwd)
    assert dotenv.read_text() == 'HTTPS_PROXY=http://proxy.invalid:8080\n'
    await runtime.close()
    assert not launch_dir.exists() and runtime._launch_dir is None


@pytest.mark.parametrize('binary', ['tools/dsh', './dsh', 'dsh'])
async def test_launch_preserves_relative_executable_resolution(runtime, binary):
    runtime.dsh_bin = binary
    await runtime.resume_thread(runtime.thread_id)
    args, kwargs = runtime.launches[-1]
    expected = str(runtime.cwd / binary) if '/' in binary else binary
    assert args == (expected, '--profile', 'acp')
    if '/' in binary:
        assert kwargs['env']['PATH'].split(dsh_runtime.os.pathsep)[0] == str(Path(expected).parent)


async def test_spawn_failure_cleans_launch_directory_and_bridge(runtime, monkeypatch):
    launches = []
    async def fail_spawn(*args, **kwargs):
        launches.append(Path(kwargs['cwd']))
        assert launches[-1].is_dir()
        raise FileNotFoundError('missing DSH binary')
    monkeypatch.setattr(dsh_runtime.asyncio, 'create_subprocess_exec', fail_spawn)
    with pytest.raises(FileNotFoundError, match='missing DSH binary'):
        await runtime.resume_thread(runtime.thread_id)
    assert not launches[-1].exists()
    assert runtime._launch_dir is None and runtime._mcp is None and runtime._process is None


async def test_ordered_public_output_terminal_and_privacy(runtime):
    task, process = await start(runtime)
    process.text('Checking files', 'progress')
    process.update('agent_thought_chunk', content={'type': 'text', 'text': 'PRIVATE thought'})
    process.text('PRIVATE foreign session', session='other')
    process.update('tool_call', toolCallId='tool', title='bash', rawInput={'command': 'PRIVATE input'})
    process.update('tool_call_update', toolCallId='tool', status='completed', content=['PRIVATE output'])
    process.text('All ')
    process.text('done')
    process.finish()
    result = await task
    assert result.status == 'completed' and result.final_message == 'All done'
    assert 'PRIVATE' not in repr(result)
    texts = [e.payload['item'] for e in result.events if e.kind == 'item/completed'
             and e.payload['item']['type'] == 'agentMessage']
    assert [(m['phase'], m['text']) for m in texts] == [
        ('commentary', 'Checking files'), ('final_answer', 'All done')]
    second, process = await start(runtime)
    process.text('Second answer')
    process.finish()
    assert (await second).final_message == 'Second answer'
    assert sum(m.get('method') == 'session/prompt' for m in process.writes) == 2


@pytest.mark.parametrize('decision,expected', [('accept', 'allow-once'), ('decline', 'reject-once'),
                                             ('acceptForSession', None), ('cancel', None)])
async def test_permission_exact_one_shot_choices(runtime, decision, expected):
    seen = []
    async def handler(method, params):
        seen.append((method, params))
        return {'decision': decision}
    runtime.server_request_handler = handler
    task, process = await start(runtime)
    process.update('tool_call', toolCallId='tool', title='bash', rawInput={'command': 'touch result'})
    process.feed({'id': 'approval', 'method': 'session/request_permission', 'params': {
        'sessionId': runtime.thread_id, 'toolCall': {'toolCallId': 'tool'}, 'options': [
            {'optionId': 'allow-once', 'kind': 'allow_once'}, {'optionId': 'reject-once', 'kind': 'reject_once'}]}})
    async def replied():
        while not any(m.get('id') == 'approval' for m in process.writes):
            await asyncio.sleep(0)
    await asyncio.wait_for(replied(), 1)
    reply = next(m for m in process.writes if m.get('id') == 'approval')['result']['outcome']
    assert reply == ({'outcome': 'selected', 'optionId': expected} if expected else {'outcome': 'cancelled'})
    assert seen[0][0] == 'item/commandExecution/requestApproval'
    assert 'touch result' in seen[0][1]['command']
    assert seen[0][1]['availableDecisions'] == ['accept', 'decline', 'cancel']
    process.update('tool_call_update', toolCallId='tool', status='completed')
    process.text('done')
    process.finish()
    assert (await task).status == 'completed'


async def test_missing_or_foreign_permission_never_prompts_user(runtime):
    def unexpected(*args):
        pytest.fail('foreign approval reached host')
    runtime.server_request_handler = unexpected
    task, process = await start(runtime)
    process.feed({'id': 'foreign', 'method': 'session/request_permission', 'params': {
        'sessionId': 'other', 'toolCall': {'toolCallId': 'unknown'}, 'options': []}})
    async def replied():
        while not any(m.get('id') == 'foreign' for m in process.writes):
            await asyncio.sleep(0)
    await asyncio.wait_for(replied(), 1)
    assert process.writes[-1]['result']['outcome'] == {'outcome': 'cancelled'}
    process.text('done')
    process.finish()
    await task


@pytest.mark.parametrize('reason', ['end_turn', 'max_tokens', 'refusal', 'unknown'])
async def test_empty_or_incomplete_result_is_not_success(runtime, reason):
    task, process = await start(runtime)
    process.finish(reason)
    result = await task
    assert result.status == 'failed' and result.final_message is None


async def test_steer_submits_nothing_and_interrupt_uses_native_cancel(runtime):
    task, process = await start(runtime)
    before = len(process.writes)
    with pytest.raises(ProjectLeadRuntimeError, match='input was not submitted'):
        await runtime.steer(runtime.active_turn_id, 'keep this for next turn')
    assert len(process.writes) == before
    assert await runtime.interrupt(runtime.active_turn_id)
    assert (await task).status == 'interrupted'
    assert any(m.get('method') == 'session/cancel' and 'id' not in m for m in process.writes)


@pytest.mark.parametrize('failure', ['error', 'eof', 'timeout'])
async def test_failed_prompt_stops_native_writer(runtime, failure):
    task, process = await start(runtime)
    if failure == 'error':
        process.finish(error={'code': -32603, 'message': 'native provider failed'})
    elif failure == 'eof':
        process.stdout.feed_eof()
    with pytest.raises(ProjectLeadRuntimeError):
        await task
    assert process.returncode == 0 and runtime._process is None


async def test_stop_failure_still_closes_bridge_without_forgetting_writer(runtime, monkeypatch):
    process, bridge = runtime._process, runtime._mcp
    launch_dir = runtime._launch_dir
    stop = dsh_runtime.stop_process_tree
    closed = []
    async def fail_stop(*args, **kwargs):
        raise PermissionError('process inspection denied')
    async def close_bridge():
        closed.append(True)
    monkeypatch.setattr(dsh_runtime, 'stop_process_tree', fail_stop)
    monkeypatch.setattr(bridge, 'close', close_bridge)
    try:
        with pytest.raises(PermissionError, match='inspection denied'):
            await runtime.close()
        assert closed == [True] and runtime._mcp is None
        assert runtime._reader is None and runtime._stderr is None
        assert runtime._process is process and process.returncode is None
        assert runtime._launch_dir is launch_dir and Path(launch_dir.name).is_dir()
    finally:
        monkeypatch.setattr(dsh_runtime, 'stop_process_tree', stop)


async def test_mcp_extensions_use_live_bindings_and_question_handler(runtime):
    bridge = _MacMCP(runtime)
    seen = []
    async def host(method, params):
        seen.append((method, params))
        return {'answers': {'choice': {'answers': ['Yes']}}}
    def handler(arguments, context):
        seen.append(context)
        return {'answer': arguments['value']}
    runtime.active_turn_id = 'active'
    runtime.update_bindings(tools=[DynamicTool('echo', 'Echo', {'type': 'object'}, handler)],
                            event_handler=None, server_request_handler=host)
    result = await bridge.call_tool(None, SimpleNamespace(name='echo', arguments={'value': 7}))
    assert json.loads(result.content[0].text) == {'answer': 7}
    assert seen[-1].thread_id == runtime.thread_id and seen[-1].turn_id == 'active'
    result = await bridge.call_tool(None, SimpleNamespace(name='request_user_input', arguments={
        'questions': [{'id': 'choice', 'header': 'Choose', 'question': 'Continue?'}]}))
    assert seen[-1][0] == 'item/tool/requestUserInput'
    assert json.loads(result.content[0].text)['answers']['choice']['answers'] == ['Yes']
    runtime.update_bindings(tools=[], event_handler=None, server_request_handler=host)
    assert [t.name for t in (await bridge.list_tools(None, None)).tools] == ['request_user_input']
    runtime.active_turn_id = None
    with pytest.raises(ValueError, match='No active'):
        await bridge.call_tool(None, SimpleNamespace(name='echo', arguments={}))


async def test_private_mcp_http_transport_auth_schema_and_call(tmp_path):
    adapter = DshACPThread(dsh_bin='dsh', cwd=tmp_path, tools=[
        DynamicTool('echo', 'Echo', {'type': 'object', 'required': ['text'],
                                   'properties': {'text': {'type': 'string'}}}, lambda a: a)])
    adapter.thread_id, adapter.active_turn_id = 'session', 'turn'
    bridge = _MacMCP(adapter)
    declaration = await bridge.start()
    headers = {'Authorization': f'Bearer {bridge.token}', 'Accept': 'application/json, text/event-stream'}
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.post(declaration['url'], json={})).status_code == 401
            async def request(method, params):
                response = await client.post(declaration['url'], headers=headers, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                assert response.status_code == 200, response.text
                return response.json()
            init = await request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                                'clientInfo': {'name': 'test', 'version': '1'}})
            assert 'serverInfo' in init['result']
            listed = await request('tools/list', {})
            assert {t['name'] for t in listed['result']['tools']} == {'echo', 'request_user_input'}
            called = await request('tools/call', {'name': 'echo', 'arguments': {'text': 'value'}})
            assert json.loads(called['result']['content'][0]['text']) == {'text': 'value'}
            invalid = await request('tools/call', {'name': 'echo', 'arguments': {}})
            assert invalid.get('error') or invalid['result']['isError']
    finally:
        await bridge.close()
    assert bridge.task.done()
