import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (
    AssistantMessage, PermissionResultAllow, PermissionResultDeny,
    ResultMessage, SystemMessage, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from mac_system.agents import claude_runtime
from mac_system.agents.claude_runtime import ClaudeCodeThread
from mac_system.agents.root_runtime import DynamicTool, ProjectLeadRuntimeError


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.messages = asyncio.Queue()
        self.inputs = []
        self.disconnected = False

    @property
    def session_id(self):
        return self.options.session_id or self.options.resume

    async def connect(self):
        self.feed(SystemMessage('init', {'session_id': self.session_id, 'model': 'native-model'}))

    async def receive_messages(self):
        while True:
            yield await self.messages.get()

    async def query(self, prompt, session_id):
        assert session_id == self.session_id
        async for message in prompt:
            self.inputs.append(message)

    def feed(self, *messages):
        for message in messages:
            self.messages.put_nowait(message)

    def acknowledge(self, index=0):
        message = self.inputs[index]
        self.feed(UserMessage(message['message']['content'], uuid=message['uuid'],
                              origin={'kind': 'human'}))

    def result(self, text='done', **kwargs):
        self.feed(ResultMessage(
            subtype=kwargs.pop('subtype', 'success'), duration_ms=1, duration_api_ms=1,
            is_error=kwargs.pop('is_error', False), num_turns=1,
            session_id=self.session_id, result=text, **kwargs))

    def idle(self):
        self.feed(SystemMessage('session_state_changed', {'state': 'idle'}))

    async def interrupt(self):
        self.result('', terminal_reason='aborted_tools')
        self.idle()

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
async def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_runtime, 'ClaudeSDKClient', FakeClient)
    adapter = ClaudeCodeThread(claude_bin='claude', cwd=tmp_path, tools=[],
                               silence_timeout_seconds=0.1)
    await adapter.create_thread()
    yield adapter
    await adapter.close()


async def start(runtime):
    client = runtime._client
    before = len(client.inputs)
    task = asyncio.create_task(runtime.run_turn('hello'))
    async def wait_for_input():
        while len(client.inputs) == before:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait_for_input(), 1)
    client.acknowledge(before)
    return task, client


async def test_native_session_options_resume_and_binding(runtime, tmp_path, monkeypatch):
    options = runtime._client.options
    assert options.session_id == runtime.thread_id and options.resume is None
    assert options.permission_mode == 'default'
    assert options.setting_sources == ['user', 'project', 'local']
    assert options.system_prompt == {'type': 'preset', 'preset': 'claude_code'}
    assert not options.fallback_model and not options.continue_conversation
    monkeypatch.setenv('VIRTUAL_ENV', '/host/venv')
    monkeypatch.setenv('PATH', '/host/venv/bin:/usr/bin')
    restored = ClaudeCodeThread(claude_bin='claude', cwd=tmp_path, tools=[])
    await restored.resume_thread(runtime.thread_id)
    try:
        assert restored._client.options.resume == runtime.thread_id
        assert restored._client.options.session_id is None
        assert restored._client.options.env['VIRTUAL_ENV'] == ''
        assert restored._client.options.env['PATH'] == '/usr/bin'
    finally:
        await restored.close()


async def test_owner_isolated_options_disable_builtins_and_append_instructions(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(claude_runtime, 'ClaudeSDKClient', FakeClient)
    adapter = ClaudeCodeThread(
        claude_bin='claude', cwd=tmp_path, tools=[],
        append_system_prompt='OWNER RULES', builtin_tools=[],
        strict_mcp_config=True, setting_sources=[],
    )
    await adapter.create_thread()
    try:
        options = adapter._client.options
        assert options.tools == []
        assert options.strict_mcp_config is True
        assert options.setting_sources == []
        assert options.system_prompt == {
            'type': 'preset', 'preset': 'claude_code', 'append': 'OWNER RULES'}
    finally:
        await adapter.close()


async def test_public_progress_tools_final_and_hidden_content(runtime):
    task, client = await start(runtime)
    client.feed(
        AssistantMessage([TextBlock('Checking files'),
                          ThinkingBlock('PRIVATE', 'signature'),
                          ToolUseBlock('read', 'Read', {'file_path': 'PRIVATE'})], 'model'),
        AssistantMessage([TextBlock('PRIVATE child')], 'model', parent_tool_use_id='child'),
        UserMessage([ToolResultBlock('read', 'PRIVATE file content')]),
        AssistantMessage([TextBlock('done')], 'model'),
    )
    client.result()
    client.idle()
    result = await task
    assert result.status == 'completed' and result.final_message == 'done'
    assert 'PRIVATE' not in repr(result)
    messages = [e.payload['item'] for e in result.events if e.kind == 'item/completed'
                and e.payload['item']['type'] == 'agentMessage']
    assert [(m['phase'], m['text']) for m in messages] == [
        ('commentary', 'Checking files'), ('final_answer', 'done')]
    # Next turn remains in the native session; it does not replay old input.
    task, client = await start(runtime)
    client.result('second')
    client.idle()
    assert (await task).final_message == 'second'
    assert len(client.inputs) == 2


async def test_steer_waits_for_latest_input_and_native_idle(runtime):
    task, client = await start(runtime)
    with pytest.raises(ProjectLeadRuntimeError, match='changed'):
        await runtime.steer('old-turn', 'must not send')
    await runtime.steer(runtime.active_turn_id, 'also do this')
    client.result('first response')
    client.idle()
    # Idle may already be in flight when the user steers. An old result and
    # the subsequent acknowledgement must not complete the host turn.
    client.acknowledge(1)
    await asyncio.sleep(0.01)
    assert not task.done()
    client.feed(AssistantMessage([TextBlock('combined response')], 'model'))
    client.result('combined response')
    await asyncio.sleep(0.01)
    assert not task.done()
    client.idle()
    assert (await task).final_message == 'combined response'
    assert client.inputs[1]['message']['content'] == 'also do this'


async def test_background_result_does_not_complete_human_turn(runtime):
    task, client = await start(runtime)
    client.result('PRIVATE background', origin={'kind': 'task-notification'})
    client.idle()
    await asyncio.sleep(0.01)
    assert not task.done()
    client.result('human answer')
    assert (await task).final_message == 'human answer'


@pytest.mark.parametrize('kwargs', [
    {'is_error': True, 'errors': ['provider unavailable']},
    {'subtype': 'error_max_turns'},
    {},
])
async def test_empty_or_error_is_not_success(runtime, kwargs):
    task, client = await start(runtime)
    client.result('', **kwargs)
    client.idle()
    result = await task
    assert result.status == 'failed' and result.final_message is None


async def test_interrupt_and_closed_transport(runtime):
    task, client = await start(runtime)
    await runtime.steer(runtime.active_turn_id, 'unacknowledged')
    assert await runtime.interrupt(runtime.active_turn_id)
    assert (await task).status == 'interrupted'
    task, client = await start(runtime)
    client.feed(ProjectLeadRuntimeError('transport dropped'))
    with pytest.raises(ProjectLeadRuntimeError, match='transport dropped'):
        await task
    assert client.disconnected


async def test_native_questions_permissions_and_live_tool_bindings(runtime):
    observed = []
    decision = 'accept'
    async def handler(method, params):
        observed.append((method, params))
        if method.endswith('requestUserInput'):
            return {'answers': {'0': {'answers': ['A', 'B']}}}
        return {'decision': decision}
    runtime.server_request_handler = handler
    task, client = await start(runtime)
    context = SimpleNamespace(tool_use_id='native-tool-id', description='Native request')
    question = {'questions': [{'question': 'Features?', 'header': 'Features',
                              'options': [{'label': 'A', 'description': 'one'},
                                          {'label': 'B', 'description': 'two'}],
                              'multiSelect': True}]}
    answer = await runtime._permission('AskUserQuestion', question, context)
    assert isinstance(answer, PermissionResultAllow)
    assert answer.updated_input['answers'] == {'Features?': 'A, B'}
    assert observed[-1][1]['questions'][0]['options'] is None
    assert 'A：one' in observed[-1][1]['questions'][0]['question']
    assert isinstance(await runtime._permission('Bash', {'command': 'pwd'}, context),
                      PermissionResultAllow)
    assert observed[-1][1]['command'] == 'pwd'
    assert observed[-1][1]['itemId'] == 'native-tool-id'
    decision = 'decline'
    assert isinstance(await runtime._permission('Write', {'content': 'x'}, context),
                      PermissionResultDeny)
    assert observed[-1][1]['availableDecisions'] == ['accept', 'decline', 'cancel']

    calls = []
    def tool_handler(args, tool_context):
        calls.append(tool_context)
        return {'value': args['value']}
    definition = DynamicTool('status', 'status', {'type': 'object'}, tool_handler)
    tool = runtime._mcp_tool(definition)
    runtime.update_bindings(tools=[definition], event_handler=None, server_request_handler=handler)
    response = await tool.handler({'value': 1})
    assert response['content'][0]['text'] == '{"value": 1}'
    assert calls[0].turn_id == runtime.active_turn_id
    assert calls[0].thread_id == runtime.thread_id
    client.result()
    client.idle()
    await task


async def test_foreign_session_result_fails_closed(runtime):
    task, client = await start(runtime)
    client.feed(ResultMessage('success', 1, 1, False, 1, 'other-session', result='foreign'))
    with pytest.raises(ProjectLeadRuntimeError, match='another session'):
        await task
    assert client.disconnected
