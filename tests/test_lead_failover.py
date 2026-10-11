import asyncio
import json

import httpx
import pytest
from sqlmodel import select

from tests.test_root_agent import root_context, _FakeLeadBindings
from mac_system.agents.root_runtime import AgentEvent, AppServerProtocolError, ProjectLeadRuntimeError, ProjectLeadRuntimeRegistry, TurnResult
from mac_system.core import root_agent as module
from mac_system.core.notification_service import retry_notification
from mac_system.models import NotificationOutbox, Project, ProjectHistoryRound
from mac_system.models import EventLog


class Bridge:
    def __init__(self):
        self.cards = []
        self.fail_result = False

    async def _send_interactive(self, recipient, card, receive_id_type='chat_id'):
        if self.fail_result and 'TAKEOVER_ANSWER' in str(card):
            raise httpx.ConnectError('delivery unavailable')
        self.cards.append(card)
        return f'om-{len(self.cards)}'

    async def update_card(self, message_id, card):
        pass


@pytest.fixture
def takeover(root_context, monkeypatch):
    session, engine = root_context
    engine.settings.project_lead_failover_provider = 'kimi_code'
    engine.feishu_bridge = Bridge()
    project = session.get(Project, 'proj-root')
    project.feishu_chat_id = 'oc-test'
    session.add(project)
    session.commit()
    monkeypatch.setattr(module, 'schedule_project_owner_review', lambda *a, **kw: None)
    calls = []
    control = {'codex': AppServerProtocolError('transport unavailable'),
               'kimi_code': 'completed', 'close_fails': False}

    class Lead(_FakeLeadBindings):
        def __init__(self, provider, context):
            self.provider = provider
            self.thread_id = None
            self.active_model = None
            self.update_bindings(tools=context.tools, event_handler=context.event_handler,
                                 server_request_handler=context.server_request_handler)
            calls.append((provider, 'build'))

        async def create_thread(self):
            self.thread_id = 'original-codex-session'
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            calls.append((self.provider, 'resume', thread_id))
            return thread_id

        async def run_turn(self, text):
            calls.append((self.provider, 'run', text))
            outcome = control[self.provider]
            if isinstance(outcome, Exception):
                raise outcome
            turn = {'id': self.provider + '-turn', 'status': outcome}
            if control.get('code'):
                turn['error'] = {'codexErrorInfo': control['code'], 'message': 'upstream unavailable'}
            return TurnResult(self.thread_id, turn['id'], outcome,
                              'TAKEOVER_ANSWER' if outcome == 'completed' else None,
                              (AgentEvent('turn/completed', self.thread_id, turn['id'], {'turn': turn}),))

        async def close(self):
            calls.append((self.provider, 'close'))
            if control['close_fails']:
                raise RuntimeError('writers still alive')

    engine.lead_runtime_registry = ProjectLeadRuntimeRegistry({
        provider: (lambda context, p=provider: Lead(p, context))
        for provider in ('codex', 'kimi_code')
    })
    return session, engine, calls, control


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [AppServerProtocolError('transport unavailable'), 'failed'])
async def test_project_provider_pin_blocks_automatic_takeover(takeover, outcome):
    session, engine, calls, control = takeover
    project = session.get(Project, 'proj-root')
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    session.add(project)
    session.commit()
    control.update(codex=outcome, code='httpConnectionFailed')
    if isinstance(outcome, Exception):
        with pytest.raises(AppServerProtocolError):
            await engine.run(project.id, '继续')
    else:
        result = await engine.run(project.id, '继续')
        assert result.status == 'failed'
    assert not any(call[0] == 'kimi_code' for call in calls)
    assert engine.store.ensure(project.id).provider == 'codex'
    assert not engine.store.ensure(project.id).provider_handoff


@pytest.mark.asyncio
async def test_project_provider_pin_overrides_default_for_new_thread(takeover):
    session, engine, calls, control = takeover
    engine.settings.project_lead_provider = 'kimi_code'
    project = session.get(Project, 'proj-root')
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    session.add(project)
    session.commit()
    control['codex'] = 'completed'
    result = await engine.run(project.id, '继续')
    assert result.status == 'completed'
    assert engine.store.ensure(project.id).provider == 'codex'
    assert not any(call[0] == 'kimi_code' for call in calls)


@pytest.mark.asyncio
async def test_project_provider_pin_rejects_cross_provider_resume(takeover):
    session, engine, calls, _ = takeover
    project = session.get(Project, 'proj-root')
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    session.add(project)
    row = engine.store.ensure(project.id, provider='kimi_code')
    row.native_thread_id = 'kimi-native-id'
    session.add(row)
    session.commit()
    with pytest.raises(ProjectLeadRuntimeError, match='durable session'):
        await engine._root_thread(project)
    assert calls == []


@pytest.mark.asyncio
async def test_takeover_stops_old_writer_and_preserves_answer_once(takeover):
    session, engine, calls, control = takeover
    # Another project's public instructions must never enter the handoff.
    other = Project(id='other', name='other', task_type='feature', description='OTHER_SECRET', workspace_path='/tmp/other')
    session.add(other)
    session.commit()
    engine.store.begin_turn('other', user_request='OTHER_SECRET')
    result = await engine.run('proj-root', '继续当前任务')
    assert result.status == 'completed'
    row = engine.store.ensure('proj-root')
    handoff = row.provider_handoff
    assert row.provider == 'kimi_code' and handoff['from_thread_id'] == 'original-codex-session'
    assert row.native_thread_id != handoff['from_thread_id']
    assert calls.index(('codex', 'close')) < calls.index(('kimi_code', 'build'))
    assert '继续当前任务' in handoff['public_context'] and 'OTHER_SECRET' not in handoff['public_context']
    assert handoff['status'] == 'completed'
    history = session.exec(select(ProjectHistoryRound).where(ProjectHistoryRound.project_id == 'proj-root')).one()
    assert history.report_content == 'TAKEOVER_ANSWER' and history.report_ref
    project = session.get(Project, 'proj-root')
    await engine._publish_handoff(project, handoff)
    await engine._publish_final(project, result)
    assert sum('助手自动接管' in str(c) for c in engine.feishu_bridge.cards) == 1
    assert sum('TAKEOVER_ANSWER' in str(c) for c in engine.feishu_bridge.cards) == 1
    assert len([c for c in calls if c[:2] == ('kimi_code', 'run')]) == 1


@pytest.mark.asyncio
async def test_close_failure_blocks_takeover_and_future_writer(takeover):
    session, engine, calls, control = takeover
    control['close_fails'] = True
    for _ in range(2):
        with pytest.raises(RuntimeError, match='writers still alive'):
            await engine.run('proj-root', '继续')
    assert not any(c[0] == 'kimi_code' for c in calls)
    assert len([c for c in calls if c[:2] == ('codex', 'run')]) == 1
    assert not engine.store.ensure('proj-root').provider_handoff


@pytest.mark.asyncio
@pytest.mark.parametrize('old_status', ['pending', 'completed', 'failed'])
async def test_return_to_codex_allows_new_cycle_without_replaying_old_handoff(takeover, old_status):
    session, engine, calls, control = takeover
    row = engine.store.ensure('proj-root')
    old = {'id': 'old-handoff', 'to_provider': 'kimi_code', 'status': old_status,
           'public_context': 'STALE_HANDOFF_CONTEXT', 'reason': 'old outage'}
    row.provider_handoff = old
    session.add(row)
    session.commit()
    result = await engine.run('proj-root', 'current request')
    assert result.status == 'completed'
    assert row.provider == 'kimi_code'
    assert row.provider_handoff['id'] != old['id']
    assert 'STALE_HANDOFF_CONTEXT' not in str(calls)
    archives = session.exec(select(EventLog).where(EventLog.message ==
        'Previous provider handoff archived after return to Codex')).all()
    assert len(archives) == 1 and archives[0].meta['provider_handoff'] == old
    assert not engine._can_failover('proj-root')


@pytest.mark.asyncio
async def test_completed_reverse_handoff_allows_takeover_and_preserves_progress(takeover):
    session, engine, calls, control = takeover
    row = engine.store.set_native_thread('proj-root', 'returned-codex-session')
    old = {'id': 'return-handoff', 'from_provider': 'kimi_code',
           'from_thread_id': 'previous-kimi-session', 'to_provider': 'codex',
           'status': 'completed', 'public_context': 'STALE_HANDOFF_CONTEXT'}
    row.provider_handoff = old
    session.add(row)
    session.commit()
    engine.store.begin_turn('proj-root', user_request='之前的已授权任务')
    history = session.get(ProjectHistoryRound, row.active_history_round_id)
    history.report_content = '成功结果全部保留；抽样方案待确认'
    history.report_ref = 'delivered-progress'
    history.report_status = 'completed'
    session.add(history)
    session.commit()
    engine.store.finish_active_history_round('proj-root', status='completed')

    assert engine._can_failover('proj-root')
    result = await engine.run('proj-root', '保留进度继续')

    assert result.status == 'completed'
    handoff = row.provider_handoff
    assert handoff['from_thread_id'] == 'returned-codex-session'
    assert handoff['status'] == 'completed'
    assert '成功结果全部保留；抽样方案待确认' in handoff['public_context']
    assert '保留进度继续' in handoff['public_context']
    assert 'STALE_HANDOFF_CONTEXT' not in str(calls)
    assert calls.index(('codex', 'close')) < calls.index(('kimi_code', 'build'))
    archives = session.exec(select(EventLog).where(EventLog.message ==
        'Previous provider handoff archived after return to Codex')).all()
    assert len(archives) == 1 and archives[0].meta['provider_handoff'] == old
    assert not engine._can_failover('proj-root')


@pytest.mark.asyncio
@pytest.mark.parametrize('old_status', ['pending', 'failed', 'interrupted'])
@pytest.mark.parametrize('structured_failure', [False, True])
async def test_incomplete_reverse_handoff_keeps_original_failure_without_takeover(
    takeover, old_status, structured_failure,
):
    session, engine, calls, control = takeover
    row = engine.store.ensure('proj-root')
    old = {'id': 'return-handoff', 'from_provider': 'kimi_code',
           'to_provider': 'codex', 'status': old_status,
           'reason': '用户要求切回 Codex',
           'public_context': '返回时保存的进度'}
    row.provider_handoff = old
    session.add(row)
    session.commit()

    assert not engine._can_failover('proj-root')
    if structured_failure:
        control.update(codex='failed', code='responseStreamDisconnected')
        result = await engine.run('proj-root', '继续')
        assert result.status == 'failed'
    else:
        with pytest.raises(AppServerProtocolError, match='transport unavailable'):
            await engine.run('proj-root', '继续')

    assert calls.count(('codex', 'close')) == (0 if structured_failure else 1)
    assert not any(call[0] == 'kimi_code' for call in calls)
    assert row.provider == 'codex'
    assert row.provider_handoff['id'] == old['id']


@pytest.mark.asyncio
async def test_native_retry_exhaustion_enters_takeover(takeover):
    session, engine, calls, control = takeover
    control.update(codex='failed', code={'responseTooManyFailedAttempts': {'httpStatusCode': None}})
    result = await engine.run('proj-root', 'continue after outage')
    assert result.status == 'completed'
    assert engine.store.ensure('proj-root').provider == 'kimi_code'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [('completed', None), ('interrupted', 'serverOverloaded'),
                                       ('failed', 'contextWindowExceeded'), ('failed', None)])
async def test_normal_terminal_outcomes_never_switch(takeover, status, code):
    session, engine, calls, control = takeover
    control.update(codex=status, code=code)
    result = await engine.run('proj-root', '继续')
    assert result.status == status
    assert not any(c[0] == 'kimi_code' for c in calls)


@pytest.mark.asyncio
async def test_structured_infrastructure_failure_and_backup_failure(takeover):
    session, engine, calls, control = takeover
    control.update(codex='failed', code='usageLimitExceeded', kimi_code='failed')
    result = await engine.run('proj-root', '继续')
    assert result.status == 'failed' and result.final_message is None
    assert engine.store.ensure('proj-root').provider_handoff['status'] == 'failed'
    assert len([c for c in calls if c[1] == 'build']) == 2


@pytest.mark.asyncio
async def test_pending_handoff_resumes_durable_target_and_deduplicates_notice(takeover):
    session, engine, calls, control = takeover
    engine.store.set_native_thread('proj-root', 'old-codex')
    row = engine.store.prepare_provider_handoff('proj-root', provider='kimi_code', thread_id='durable-kimi',
                                               reason='offline', public_context='保留的公开进度')
    await engine._publish_handoff(session.get(Project, 'proj-root'), row.provider_handoff)
    session.expire_all()  # reconstruct the durable state after a transport loss
    result = await engine.run('proj-root', '重启后继续')
    assert result.status == 'completed'
    assert ('kimi_code', 'resume', 'durable-kimi') in calls
    assert not any(c[0] == 'codex' for c in calls)
    assert '保留的公开进度' in next(c[2] for c in calls if c[1] == 'run')
    assert sum('助手自动接管' in str(c) for c in engine.feishu_bridge.cards) == 1
    with pytest.raises(ValueError, match='already'):
        engine.store.prepare_provider_handoff('proj-root', provider='kimi_code', thread_id='new', reason='', public_context='')


@pytest.mark.asyncio
async def test_delivery_failure_retries_answer_without_rerunning_takeover(takeover):
    session, engine, calls, control = takeover
    engine.feishu_bridge.fail_result = True
    result = await engine.run('proj-root', '继续')
    assert result.status == 'completed'
    outbox = session.exec(select(NotificationOutbox).where(
        NotificationOutbox.dedupe_key == f'root-result:{result.turn_id}:1')).one()
    assert outbox.status != 'sent' and 'TAKEOVER_ANSWER' in json.dumps(outbox.payload)
    assert engine.store.ensure('proj-root').status == 'idle'
    engine.feishu_bridge.fail_result = False
    await retry_notification(engine.service, engine.feishu_bridge, outbox)
    assert outbox.status == 'sent'
    assert len([c for c in calls if c[1] == 'run']) == 2


@pytest.mark.asyncio
async def test_stopped_owner_loop_blocks_new_writer_and_thread_reset(takeover):
    session, engine, calls, control = takeover
    control['codex'] = 'completed'
    await engine.run('proj-root', '继续')
    handle = module._ROOT_THREADS['proj-root']
    original_loop = handle.loop
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    handle.loop = dead_loop
    try:
        with pytest.raises(RuntimeError, match='event loop is unavailable'):
            await engine.start_new_thread('proj-root')
        assert module._ROOT_THREADS['proj-root'] is handle
        assert engine.store.ensure('proj-root').native_thread_id == 'original-codex-session'
        with pytest.raises(RuntimeError, match='event loop is unavailable'):
            await engine.run('proj-root', '继续')
        assert len([c for c in calls if c[1] == 'build']) == 1
    finally:
        handle.loop = original_loop
        await engine._release_root('proj-root', handle.runtime)


def test_takeover_progress_names_current_provider_without_rewriting_text():
    label, _, _ = module._native_activity_card('item/completed', {
        'type': 'nativeToolCall', 'tool': 'ReadFile', 'status': 'completed',
    }, provider='Kimi')
    assert 'Kimi' in label and 'Codex' not in label and '外部编码' not in label
    user_text = 'Codex 故障已记录，正在由 Kimi 接续。'
    assert module._native_activity_card('item/completed', {
        'type': 'agentMessage', 'text': user_text,
    }, provider='Kimi')[0] == user_text
