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
    assert process.command[process.command.index('--session') + 1] == 'kimi-session-uuid'
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error', [('finished', None), ('max_steps_reached', None),
                                       (None, {'code': -32003, 'message': 'quota exhausted'})])
async def test_wire_empty_or_error_never_success(wire, status, error):
    runtime, process = wire
    task = await started(runtime, process)
    process.finish(status, error)
    result = await task
    assert result.status == 'failed' and result.final_message is None
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
