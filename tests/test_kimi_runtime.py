import asyncio
import json

import pytest

from mac_system.agents.kimi_runtime import KimiWireThread, KimiWireError
from mac_system.agents.root_runtime import DynamicTool
from tests.test_root_runtime import _FakeProcess


class WireProcess(_FakeProcess):
    def event(self, kind, payload=None):
        self._feed({'method': 'event', 'params': {'type': kind, 'payload': payload or {}}})

    def request(self, kind, payload, request_id='rpc-1'):
        self._feed({'method': 'request', 'id': request_id,
                    'params': {'type': kind, 'payload': payload}})

    def finish(self, status='finished', error=None):
        self._feed({'id': self.prompt_id, **({'error': error} if error else
                                           {'result': {'status': status}})})

    def text(self, value):
        self.event('ContentPart', {'type': 'text', 'text': value})

    async def receive(self, message):
        self.messages.append(message)
        method = message.get('method')
        if method == 'initialize':
            self._feed({'id': message['id'], 'result': {'protocol_version': '1.3'}})
        elif method == 'prompt':
            self.prompt_id = message['id']
        elif method == 'steer':
            self._feed({'id': message['id'], 'result': {'status': 'steered'}})
        elif method == 'cancel':
            self._feed({'id': message['id'], 'result': {}})
            self.finish('cancelled')


@pytest.fixture
async def wire(monkeypatch, tmp_path):
    process = WireProcess()
    async def spawn(*args, **kwargs):
        assert '--wire' in args and '--yolo' not in args
        assert kwargs['start_new_session'] is True
        process.command = args
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    runtime = KimiWireThread(kimi_bin='kimi', cwd=tmp_path, tools=[],
                            silence_timeout_seconds=0.02)
    yield runtime, process
    await runtime.close()


async def started(runtime, process):
    await runtime.resume_thread('kimi-session-uuid')
    task = asyncio.create_task(runtime.run_turn('continue'))
    while not hasattr(process, 'prompt_id'):
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_wire_native_content_final_and_hidden_thinking(wire):
    runtime, process = wire
    task = await started(runtime, process)
    process.event('StepBegin', {'n': 1})
    process.text('progress only')
    process.event('ToolCall', {'id': 'read', 'function': {'name': 'ReadFile'}})
    process.event('ToolResult', {'tool_call_id': 'read'})
    process.event('StepBegin', {'n': 2})
    process.event('ContentPart', {'type': 'think', 'think': 'PRIVATE'})
    process.event('SubagentEvent', {'private': 'PRIVATE'})
    process.text('retained answer')
    process.finish()
    result = await task
    assert result.status == 'completed' and result.final_message == 'retained answer'
    assert 'PRIVATE' not in repr(result)
    assert any(e.kind == 'item/completed' and e.payload['item'].get('phase') == 'final_answer'
               for e in result.events)
    assert any(e.kind == 'item/completed' and e.payload['item'].get('phase') == 'commentary'
               and e.payload['item']['text'] == 'progress only' for e in result.events)
    assert process.command[process.command.index('--session') + 1] == 'kimi-session-uuid'
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['ToolCall', 'StepBegin'])
async def test_wire_public_progress_is_completed_before_turn_ends(wire, boundary):
    runtime, process = wire
    observed = []
    ready = asyncio.Event()

    def on_event(event):
        observed.append(event)
        if event.kind == 'item/completed' and event.payload.get('item', {}).get('phase') == 'commentary':
            ready.set()

    runtime.event_handler = on_event
    task = await started(runtime, process)
    try:
        process.event('StepBegin', {'n': 1})
        process.text('Checking ')
        process.text('the project')
        process.event('ContentPart', {'type': 'think', 'think': 'PRIVATE'})
        process.event('SubagentEvent', {'private': 'PRIVATE'})
        if boundary == 'ToolCall':
            for tool_id in ['read', 'check']:
                process.event('ToolCall', {'id': tool_id, 'function': {'name': 'ReadFile'}})
                process.event('ToolResult', {'tool_call_id': tool_id, 'output': 'PRIVATE OUTPUT'})
            process.event('StepBegin', {'n': 2})
        else:
            process.event('StepBegin', {'n': 2})
        await asyncio.wait_for(ready.wait(), 0.5)
        assert not task.done()
        process.text('Final answer')
        process.finish()
        result = await task
        assert result.final_message == 'Final answer'
        messages = [event.payload['item'] for event in observed
                    if event.kind == 'item/completed' and event.payload['item']['type'] == 'agentMessage']
        assert [(item['phase'], item['text']) for item in messages] == [
            ('commentary', 'Checking the project'), ('final_answer', 'Final answer')]
        for item in messages:
            starts = [event for event in observed if event.kind == 'item/started'
                      and event.payload['item']['id'] == item['id']]
            deltas = [event.payload['delta'] for event in observed
                      if event.kind == 'item/agentMessage/delta' and event.payload['itemId'] == item['id']]
            assert len(starts) == 1
            assert ''.join(deltas) == item['text']
        assert 'PRIVATE' not in repr(result)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wire_final_answer_is_not_progress(wire):
    runtime, process = wire
    task = await started(runtime, process)
    process.text('Only the answer')
    process.finish()
    result = await task
    messages = [event.payload['item'] for event in result.events
                if event.kind == 'item/completed']
    assert [(item['phase'], item['text']) for item in messages] == [
        ('final_answer', 'Only the answer')]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error', [
    ('cancelled', None), ('max_steps_reached', None),
    (None, {'code': -32003, 'message': 'quota exhausted'})])
async def test_wire_non_success_text_does_not_become_final_answer(wire, status, error):
    runtime, process = wire
    task = await started(runtime, process)
    process.text('Public progress before stopping')
    process.finish(status, error)
    result = await task
    assert result.status != 'completed' and result.final_message is None
    messages = [event.payload['item'] for event in result.events
                if event.kind == 'item/completed']
    assert [(item['phase'], item['text']) for item in messages] == [
        ('commentary', 'Public progress before stopping')]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error', [('finished', None), ('max_steps_reached', None),
                                       (None, {'code': -32003, 'message': 'quota exhausted'})])
async def test_wire_empty_or_error_never_success(wire, status, error):
    runtime, process = wire
    task = await started(runtime, process)
    process.event('ContentPart', {'type': 'think', 'think': 'PRIVATE'})
    process.finish(status, error)
    result = await task
    assert result.status == 'failed' and result.final_message is None
    assert 'PRIVATE' not in repr(result)
    if status == 'finished':
        terminal = next(e for e in result.events if e.kind == 'turn/completed')
        assert 'empty response' in terminal.payload['turn']['error']['message']
    await runtime.close()


@pytest.mark.asyncio
async def test_wire_premature_finish_does_not_confirm_unfinished_tool(wire):
    runtime, process = wire
    task = await started(runtime, process)
    process.event('ToolCall', {'id': 'write', 'function': {'name': 'Shell'}})
    process.text('Done')
    process.finish()
    result = await task
    assert result.status == 'failed' and result.final_message is None
    terminal = next(e for e in result.events if e.kind == 'turn/completed')
    assert 'unfinished tool calls' in terminal.payload['turn']['error']['message']
    assert not any(e.kind == 'item/completed' and
                   e.payload['item'].get('phase') == 'final_answer' for e in result.events)
    assert len([m for m in process.messages if m.get('method') == 'prompt']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('answered', [False, True])
async def test_wire_terminal_checks_pending_host_requests(wire, answered):
    runtime, process = wire
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_method, _params):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {'decision': 'accept'}

    runtime.server_request_handler = handler
    task = await started(runtime, process)
    process.request('ApprovalRequest', {'id': 'approval', 'description': 'write file'})
    await asyncio.wait_for(entered.wait(), 0.5)
    if answered:
        release.set()
        while not any(m.get('id') == 'rpc-1' and 'result' in m for m in process.messages):
            await asyncio.sleep(0)
    process.text('Done')
    process.finish()
    result = await task
    assert result.status == ('completed' if answered else 'failed')
    assert result.final_message == ('Done' if answered else None)
    if not answered:
        assert cancelled.is_set()
        assert not any(m.get('id') == 'rpc-1' and 'result' in m for m in process.messages)


@pytest.mark.asyncio
async def test_wire_terminal_does_not_lose_host_reply_failure(wire, monkeypatch):
    runtime, process = wire
    task = await started(runtime, process)
    original_write = runtime._write

    async def broken_reply(message):
        if message.get('id') == 'rpc-1':
            raise KimiWireError('host reply transport failed')
        await original_write(message)

    monkeypatch.setattr(runtime, '_write', broken_reply)
    runtime.tools = {'status': DynamicTool('status', 'status', {'type': 'object'},
                                         lambda _args: {'ok': True})}
    process.request('ToolCallRequest', {'id': 'status', 'name': 'status', 'arguments': '{}'})
    process.finish()
    with pytest.raises(KimiWireError, match='host reply transport failed'):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['exit', 'closed_transport'])
async def test_wire_recovers_idle_transport_in_same_session_once(monkeypatch, tmp_path, failure):
    processes = []

    async def spawn(*args, **_kwargs):
        process = WireProcess()
        process.command = args
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    runtime = KimiWireThread(kimi_bin='kimi', cwd=tmp_path, tools=[],
                             silence_timeout_seconds=0.5)
    try:
        await runtime.resume_thread('durable-kimi-session')
        first = processes[0]
        task = asyncio.create_task(runtime.run_turn('first request'))
        while not hasattr(first, 'prompt_id'):
            await asyncio.sleep(0)
        first.text('First answer')
        first.finish()
        assert (await task).status == 'completed'
        if failure == 'exit':
            first.terminate()
        else:
            first.stdout.feed_data(b'not json\n')
        await runtime._reader

        task = asyncio.create_task(runtime.run_turn('second request'))

        async def new_prompt():
            while len(processes) < 2 or not hasattr(processes[1], 'prompt_id'):
                await asyncio.sleep(0)

        await asyncio.wait_for(new_prompt(), 0.5)
        second = processes[1]
        second.text('Second answer')
        second.finish()
        result = await task
        assert result.status == 'completed' and result.final_message == 'Second answer'
        assert first.returncode is not None
        assert [p.command[p.command.index('--session') + 1] for p in processes] == [
            'durable-kimi-session', 'durable-kimi-session']
        prompts = [[m['params']['user_input'] for m in p.messages
                    if m.get('method') == 'prompt'] for p in processes]
        assert prompts == [['first request'], ['second request']]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_wire_native_steer_and_cancel(wire):
    runtime, process = wire
    task = await started(runtime, process)
    with pytest.raises(KimiWireError):
        await runtime.steer('stale-turn', 'new message')
    await runtime.steer(runtime.active_turn_id, 'new message')
    assert await runtime.interrupt(runtime.active_turn_id)
    assert (await task).status == 'interrupted'
    assert [m for m in process.messages if m.get('method') == 'steer'][0]['params'] == {
        'user_input': 'new message'}
    await runtime.close()


@pytest.mark.asyncio
async def test_wire_parallel_requests_approval_question_tool(wire):
    runtime, process = wire
    release = asyncio.Event()
    observed = []
    async def handler(method, params):
        observed.append((method, params))
        await release.wait()
        if method.endswith('requestApproval'):
            return {'decision': 'acceptForSession'}
        return {'answers': {'0': {'answers': ['A']}}}
    runtime.server_request_handler = handler
    runtime.tools = {'status': DynamicTool('status', 'status', {'type': 'object'},
                                           lambda args: {'ok': True})}
    task = await started(runtime, process)
    process.request('ApprovalRequest', {'id': 'approval-native', 'description': 'write file'}, 'approval')
    process.request('QuestionRequest', {'id': 'question-native', 'questions': [
        {'question': 'Pick?', 'options': [{'label': 'A', 'description': 'first'}]}]}, 'question')
    process.request('ToolCallRequest', {'id': 'tool-native', 'name': 'status', 'arguments': '{}'}, 'tool')
    await asyncio.sleep(0.07)  # user requests outlast model-silence watchdog
    assert not task.done() and len(observed) == 2
    assert any(m.get('id') == 'tool' and 'result' in m for m in process.messages)
    release.set()
    while not any(m.get('id') == 'question' and 'result' in m for m in process.messages):
        await asyncio.sleep(0)
    answers = {m['id']: m['result'] for m in process.messages if 'result' in m}
    assert answers['approval'] == {'request_id': 'approval-native', 'response': 'approve_for_session'}
    assert answers['question'] == {'request_id': 'question-native', 'answers': {'Pick?': 'A'}}
    assert answers['tool']['tool_call_id'] == 'tool-native'
    # Completed host requests must not disable detection of a stalled model.
    with pytest.raises(KimiWireError, match='stopped responding'):
        await asyncio.wait_for(task, 0.5)
    await runtime.close()


@pytest.mark.asyncio
async def test_wire_long_tool_and_broken_transport(wire):
    runtime, process = wire
    task = await started(runtime, process)
    process.event('ToolCall', {'id': 'long-tool', 'function': {'name': 'Shell'}})
    await asyncio.sleep(0.07)
    assert not task.done()
    process.stdout.feed_data(b'not json\n')
    with pytest.raises(KimiWireError, match='wire error'):
        await task
    await runtime.close()


@pytest.mark.asyncio
async def test_wire_missing_approval_handler_never_authorizes(wire):
    runtime, process = wire
    task = await started(runtime, process)
    process.request('ApprovalRequest', {'id': 'approval-native', 'description': 'write file'})
    while not any(m.get('id') == 'rpc-1' and 'error' in m for m in process.messages):
        await asyncio.sleep(0)
    assert not any(m.get('result', {}).get('response', '').startswith('approve')
                   for m in process.messages)
    process.finish('cancelled')
    assert (await task).status == 'interrupted'
