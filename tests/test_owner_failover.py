import asyncio
import json
from pathlib import Path

import pytest
from sqlmodel import select

from mac_system.agents.root_runtime import AgentEvent, AppServerProtocolError, TurnResult
from mac_system.config import Settings
from mac_system.core.project_owner import (
    ProjectOwner, ProjectOwnerRuntimeContext, ProjectOwnerRuntimeRegistry, _KimiOwnerThread,
)
from mac_system.models import EventLog, NotificationOutbox
from tests.test_project_owner import owner_context, _question


@pytest.fixture
def owner_takeover(owner_context):
    service, project, _ = owner_context
    project.feishu_chat_id = 'oc-owner-test'
    service.session.add(project)
    service.session.commit()
    calls = []
    control = {'error': AppServerProtocolError('connection failed'), 'stage': 'run',
               'close_error': False, 'backup_error': False, 'invalid': False}
    decision = dict(outcome='defer', decision='', rationale='缺少用户依据',
                    impact='保留原问题', reversible=False, confidence='low', answers=[])

    class Runtime:
        def __init__(self, provider, context):
            self.provider = self.active_model = provider
            self.thread_id = provider + '-owner'
            calls.append((provider, 'build'))

        async def create_thread(self):
            calls.append((self.provider, 'create'))
            if self.provider == 'codex' and control['stage'] == 'create':
                raise control['error']

        async def run_turn(self, text, *, output_schema=None):
            calls.append((self.provider, 'run'))
            if self.provider == 'codex' and control['stage'] == 'run':
                if isinstance(control['error'], Exception):
                    raise control['error']
                return TurnResult(self.thread_id, 'turn', 'failed', None, (
                    AgentEvent('turn/completed', self.thread_id, 'turn', {'turn': {
                        'error': {'codexErrorInfo': control['error'], 'message': 'network unavailable'},
                    }}),
                ))
            if control['backup_error']:
                raise AppServerProtocolError('backup unavailable')
            value = {**decision, 'unexpected': True} if control['invalid'] else decision
            return TurnResult(self.thread_id, 'turn', 'completed', json.dumps(value), ())

        async def close(self):
            calls.append((self.provider, 'close'))
            if control['close_error']:
                raise RuntimeError('writers still alive')

    registry = ProjectOwnerRuntimeRegistry({p: (lambda ctx, p=p: Runtime(p, ctx))
                                          for p in ('codex', 'kimi_code')})
    settings = Settings(PROJECT_LEAD_FAILOVER_PROVIDER='kimi_code', PROJECT_OWNER_PROVIDER='codex')
    return ProjectOwner(service, settings, runtime_registry=registry), project, calls, control


@pytest.mark.asyncio
async def test_project_provider_pin_keeps_owner_on_codex_after_outage(owner_takeover):
    owner, project, calls, _ = owner_takeover
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    with pytest.raises(RuntimeError):
        await owner.decide(project, [_question()])
    assert ('codex', 'build') in calls
    assert not any(provider == 'kimi_code' for provider, _ in calls)
    assert owner.service.session.exec(select(NotificationOutbox)).all() == []


@pytest.mark.asyncio
async def test_project_provider_pin_overrides_owner_default(owner_takeover):
    owner, project, calls, control = owner_takeover
    owner.settings.project_owner_provider = 'kimi_code'
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    control['stage'] = 'none'
    result = await owner.decide(project, [_question()])
    assert result.outcome == 'defer'
    assert ('codex', 'build') in calls
    assert not any(provider == 'kimi_code' for provider, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,error', [
    ('create', OSError('cannot connect')), ('run', asyncio.TimeoutError()),
    ('run', AppServerProtocolError('stdout closed')),
    ('run', {'httpConnectionFailed': {'httpStatusCode': None}}),
    ('run', {'responseTooManyFailedAttempts': {'httpStatusCode': None}}),
])
async def test_owner_outage_switches_once_after_close_with_durable_notice(owner_takeover, stage, error):
    owner, project, calls, control = owner_takeover
    control.update(stage=stage, error=error)
    result = await owner.decide(project, [_question()])
    assert result.outcome == 'defer' and result.answers == {}
    assert calls.index(('codex', 'close')) < calls.index(('kimi_code', 'build'))
    assert calls.count(('kimi_code', 'run')) == 1
    notices = owner.service.session.exec(select(NotificationOutbox)).all()
    assert len(notices) == 1 and notices[0].status == 'pending'
    events = owner.service.session.exec(select(EventLog).where(
        EventLog.message == 'Project Owner provider handoff')).all()
    assert len(events) == 1 and events[0].meta['to_provider'] == 'kimi_code'
    assert 'human_mainline' in events[0].meta['context']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['disabled', 'close_failure', 'backup_failure', 'cancelled'])
async def test_owner_failover_never_loops_or_ignores_cancellation(owner_takeover, mode):
    owner, project, calls, control = owner_takeover
    if mode == 'disabled':
        owner.settings.project_lead_failover_provider = ''
    elif mode == 'close_failure':
        control['close_error'] = True
    elif mode == 'backup_failure':
        control['backup_error'] = True
    else:
        control.update(stage='create', error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError if mode == 'cancelled' else RuntimeError):
        await owner.decide(project, [_question()])
    assert calls.count(('kimi_code', 'build')) == (1 if mode == 'backup_failure' else 0)


@pytest.mark.asyncio
async def test_kimi_owner_invalid_schema_gets_bounded_correction(owner_takeover):
    owner, project, calls, control = owner_takeover
    control['invalid'] = True
    with pytest.raises(RuntimeError, match='结构化结果无效'):
        await owner.decide(project, [_question()])
    assert calls.count(('codex', 'run')) == 1
    assert calls.count(('kimi_code', 'run')) == 2


@pytest.mark.asyncio
async def test_owner_task_error_does_not_change_provider(owner_takeover):
    owner, project, calls, control = owner_takeover
    control['error'] = 'contextWindowExceeded'
    with pytest.raises(RuntimeError, match='未返回可用'):
        await owner.decide(project, [_question()])
    assert not any(p == 'kimi_code' for p, _ in calls)


@pytest.mark.asyncio
async def test_kimi_owner_uses_empty_native_tools_and_explicit_empty_mcp(tmp_path, monkeypatch):
    from tests.test_kimi_runtime import WireProcess
    process = WireProcess()
    observed = {}

    async def spawn(*args, **kwargs):
        observed['command'] = args
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    runtime = _KimiOwnerThread(ProjectOwnerRuntimeContext(
        settings=Settings(), owner_dir=tmp_path, tools=[], event_handler=lambda event: None,
    ))
    spec_dir = Path(runtime.agent_file).parent
    try:
        await runtime.create_thread()
        args = observed['command']
        spec = json.loads(Path(args[args.index('--agent-file') + 1]).read_text())
        assert spec['agent']['tools'] == [] and spec['agent']['subagents'] == {}
        assert 'extend' not in spec['agent']
        assert json.loads(Path(args[args.index('--mcp-config-file') + 1]).read_text()) == {'mcpServers': {}}
        task = asyncio.create_task(runtime.run_turn('owner request', output_schema={'type': 'object'}))
        while not hasattr(process, 'prompt_id'):
            await asyncio.sleep(0)
        process.text('{}')
        process.finish()
        assert (await task).final_message == '{}'
        prompt = next(m for m in process.messages if m.get('method') == 'prompt')
        assert 'JSON Schema' in prompt['params']['user_input']
    finally:
        await runtime.close()
    assert not spec_dir.exists()
