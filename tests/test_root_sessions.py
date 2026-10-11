import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.root_runtime import (
    AgentEvent,
    ToolContext,
)
from mac_system.core.project_service import ProjectService
from mac_system.core.root_sessions import (
    SHUTDOWN_INTERRUPTED_ERROR,
    RootSessionStore,
    prune_redundant_native_event_snapshots,
    recover_interrupted_external_agents,
    recover_interrupted_native_turns,
)
from mac_system.models import (
    AgentSession,
    Approval,
    RootAgentSession,
    DynamicToolCall,
    EventLog,
    Project,
    ProjectDecision,
    ProjectHistoryRound,
    QuestionInteraction,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as value:
        value.add(
            Project(
                id="proj-1",
                name="test",
                task_type="feature",
                description="test",
                current_state="COORDINATING",
                workspace_path="/tmp/project",
                context={"orchestration_mode": "root_agent"},
            )
        )
        value.commit()
        yield value


@pytest.mark.asyncio
async def test_tool_call_is_persisted_before_effect_and_completed_result_is_replayed(
    session,
):
    store = RootSessionStore(session)
    context = ToolContext("thread-1", "turn-1", "call-1", "report")
    observed = []

    async def execute(tool_call_id):
        row = session.get(DynamicToolCall, tool_call_id)
        observed.append((row.status, row.arguments))
        return {"sent": True}

    first = await store.execute_tool_once(
        "proj-1", context, {"message": "hello"}, execute
    )
    second = await store.execute_tool_once(
        "proj-1", context, {"message": "hello"}, execute
    )

    assert first == second == {"sent": True}
    assert observed == [("running", {"message": "hello"})]
    calls = list(session.exec(select(DynamicToolCall)).all())
    assert len(calls) == 1
    assert calls[0].status == "completed"


def test_native_session_event_cursor_and_agent_registry_are_durable(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    first = store.append_event(
        "proj-1", AgentEvent("item/started", "thread-1", "turn-1", {"x": 1})
    )
    second = store.append_event(
        "proj-1", AgentEvent("item/completed", "thread-1", "turn-1", {"x": 2})
    )
    agent = store.create_agent(
        "proj-1",
        task_name="inspect",
        provider="codex",
        access="read_only",
        objective="inspect the repository",
        tool_call_id="ctool-1",
    )

    assert (first, second) == (1, 2)
    root = session.exec(select(RootAgentSession)).one()
    assert root.native_thread_id == "thread-1"
    assert root.event_cursor == 2
    assert len(session.exec(select(EventLog)).all()) == 2
    assert session.exec(select(AgentSession)).one().id == agent.id
    with pytest.raises(ValueError, match="task name already exists"):
        store.create_agent(
            "proj-1",
            task_name="inspect",
            provider="claude_code",
            access="write",
            objective="must not replace the first registration",
            tool_call_id="ctool-2",
        )


def test_root_session_uses_the_configured_project_lead_provider(session):
    store = RootSessionStore(session, default_provider="pi")

    root = store.ensure("proj-1")
    assert root.provider == "pi"


def test_explicit_provider_switch_detaches_old_thread_and_waits_for_next_request(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "old-codex-thread")

    assert store.switch_provider("proj-1", "kimi_code") == ("codex", "kimi_code")
    root = store.ensure("proj-1")
    project = session.get(Project, "proj-1")
    assert root.provider == "kimi_code"
    assert root.native_thread_id is None
    assert root.status == "idle"
    assert project.context["execution_provider"] == "kimi_code"
    assert "manual_provider_switch_pending" not in project.context
    assert store.switch_provider("proj-1", "kimi_code") == ("kimi_code", "kimi_code")
    assert len(list(session.exec(select(EventLog).where(
        EventLog.message == "Project Lead provider changed by explicit user command"
    )))) == 1


def test_explicit_provider_switch_rejects_active_turn(session):
    store = RootSessionStore(session)
    store.set_turn("proj-1", turn_id="turn-active", status="running")

    with pytest.raises(RuntimeError, match="仍在处理消息"):
        store.switch_provider("proj-1", "kimi_code")
    assert store.ensure("proj-1").provider == "codex"


def test_terminal_turn_projection_overwrites_concurrent_event_session(session):
    store = RootSessionStore(session)
    project = session.get(Project, "proj-1")
    assert project.execution_status == "idle"

    with Session(session.get_bind()) as event_session:
        event_project = event_session.get(Project, "proj-1")
        event_project.execution_status = "running"
        event_session.add(event_project)
        event_session.commit()

    # The owning Session still has the old idle value cached. The terminal
    # lifecycle boundary must nevertheless write idle over the event Session's
    # running projection.
    store.set_turn("proj-1", turn_id=None, status="idle")
    session.expire_all()

    assert session.get(Project, "proj-1").execution_status == "idle"


def test_service_restart_recovers_interrupted_native_turn(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.begin_turn(
        "proj-1",
        user_request="继续完成用户请求",
        request_origin="project_owner",
        parent_decision_id="decision-parent",
    )
    store.set_turn(
        "proj-1", turn_id="turn-1", status="waiting_user", reason="请选择"
    )
    project = session.get(Project, "proj-1")
    assert project.execution_status == "waiting_user"
    assert project.waiting_reason == "请选择"
    approval = Approval(
        id="approval-1",
        project_id="proj-1",
        state="ROOT_AGENT_WAITING_USER",
        approval_type="input",
        status="pending",
    )
    interaction = QuestionInteraction(
        id="interaction-1",
        project_id="proj-1",
        batch_key="root:request-1",
        status="pending",
        approval_id=approval.id,
    )
    session.add(project)
    session.add(approval)
    session.add(interaction)
    session.commit()

    assert recover_interrupted_native_turns(session) == ["proj-1"]

    session.expire_all()
    root = session.exec(select(RootAgentSession)).one()
    assert root.native_thread_id == "thread-1"
    assert root.active_turn_id is None
    assert root.status == "idle"
    assert root.restart_recovery_pending is True
    assert store.get_restart_recovery("proj-1") == (
        "thread-1",
        "继续完成用户请求",
        "project_owner",
        "decision-parent",
    )
    assert store.get_restart_recovery("proj-1") is not None
    assert session.get(Project, "proj-1").execution_status == "idle"
    assert session.get(Approval, "approval-1").status == "expired"
    assert session.get(QuestionInteraction, "interaction-1").status == "expired"
    store.begin_turn("proj-1", user_request="继续完成用户请求")
    assert store.ensure("proj-1").restart_recovery_pending is False


def test_system_recovery_reuses_the_same_project_history_round(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    root = store.begin_turn(
        "proj-1",
        user_request="继续完成同一轮",
        request_origin="human",
    )
    first_round_id = root.active_history_round_id
    store.set_turn("proj-1", turn_id="turn-1", status="running")

    assert recover_interrupted_native_turns(session) == ["proj-1"]
    recovered = store.begin_turn(
        "proj-1",
        user_request="继续完成同一轮",
        request_origin="system_recovery",
    )

    assert recovered.active_history_round_id == first_round_id
    assert len(session.exec(select(ProjectHistoryRound)).all()) == 1
    assert session.get(ProjectHistoryRound, first_round_id).report_status == "pending"


def test_graceful_shutdown_interruption_remains_recoverable_after_idle_projection(
    session,
):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    root = store.begin_turn("proj-1", user_request="继续完成用户请求")
    history_round_id = root.active_history_round_id
    store.set_turn("proj-1", turn_id="turn-1", status="running")
    store.finish_active_history_round("proj-1", status="interrupted")
    store.set_turn("proj-1", turn_id=None, status="idle")

    assert store.mark_shutdown_interrupted_for_recovery(
        "proj-1", history_round_id
    )
    assert recover_interrupted_native_turns(session) == ["proj-1"]
    assert store.get_restart_recovery("proj-1") == (
        "thread-1", "继续完成用户请求", "human", None
    )
    store.begin_turn(
        "proj-1", user_request="继续完成用户请求", request_origin="system_recovery"
    )
    assert store.ensure("proj-1").restart_recovery_pending is False


def test_graceful_shutdown_does_not_replay_a_completed_round(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    root = store.begin_turn("proj-1", user_request="完成用户请求")
    history_round_id = root.active_history_round_id
    store.finish_active_history_round("proj-1", status="completed")
    store.set_turn("proj-1", turn_id=None, status="idle")

    assert not store.mark_shutdown_interrupted_for_recovery(
        "proj-1", history_round_id
    )
    assert recover_interrupted_native_turns(session) == []
    assert store.get_restart_recovery("proj-1") is None


def test_restart_recovers_idle_failed_transport_with_pending_round(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    root = store.begin_turn("proj-1", user_request="继续完成当前任务")
    history_round_id = root.active_history_round_id
    store.set_turn(
        "proj-1", turn_id=None, status="idle", error="provider transport failed"
    )

    assert recover_interrupted_native_turns(session) == ["proj-1"]
    assert store.get_restart_recovery("proj-1") == (
        "thread-1", "继续完成当前任务", "human", None
    )
    assert session.get(ProjectHistoryRound, history_round_id).report_status == "pending"


def test_restart_does_not_replay_idle_pending_round_without_failure(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.begin_turn("proj-1", user_request="等待结果送达")
    store.set_turn("proj-1", turn_id=None, status="idle")

    assert recover_interrupted_native_turns(session) == []
    assert store.get_restart_recovery("proj-1") is None


def test_new_human_turn_preserves_stale_round_and_starts_next_sequence(session):
    store = RootSessionStore(session)
    first = store.begin_turn("proj-1", user_request="第一轮")
    first_round_id = first.active_history_round_id

    second = store.begin_turn("proj-1", user_request="第二轮")
    rows = list(
        session.exec(
            select(ProjectHistoryRound).order_by(ProjectHistoryRound.sequence)
        ).all()
    )

    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].id == first_round_id
    assert rows[0].report_status == "missing_report"
    assert rows[1].id == second.active_history_round_id
    assert rows[1].user_instructions[0]["text"] == "第二轮"


def test_project_history_keeps_reply_context_for_short_human_messages(session):
    store = RootSessionStore(session)
    root = store.begin_turn(
        "proj-1",
        user_request="同意",
        parent_decision_id="decision-1",
        reply_context={
            "kind": "decision",
            "subject_type": "project_decision",
            "subject_ref": "decision-1",
            "summary": "是否先完成移动端验收？建议先完成。",
        },
    )

    history = session.get(ProjectHistoryRound, root.active_history_round_id)

    assert history.parent_decision_id == "decision-1"
    assert history.user_instructions == [{
        "text": "同意",
        "origin": "human",
        "time": history.user_instructions[0]["time"],
        "reply_to": {
            "kind": "decision",
            "subject_type": "project_decision",
            "subject_ref": "decision-1",
            "summary": "是否先完成移动端验收？建议先完成。",
        },
    }]

    store.append_history_instruction(
        "proj-1",
        "继续",
        reply_context={
            "kind": "report",
            "subject_type": "root_turn",
            "subject_ref": "turn-1",
            "summary": "移动端验收已经通过。",
        },
    )
    session.refresh(history)
    assert history.user_instructions[-1]["text"] == "继续"
    assert history.user_instructions[-1]["reply_to"]["summary"] == (
        "移动端验收已经通过。"
    )


def test_confirmed_interrupt_closes_active_history_round(session):
    store = RootSessionStore(session)
    root = store.begin_turn("proj-1", user_request="执行到一半后停止")
    round_id = root.active_history_round_id

    history = store.finish_active_history_round("proj-1", status="interrupted")

    assert history is not None and history.id == round_id
    assert history.report_status == "interrupted"
    assert history.completed_at is not None
    assert store.ensure("proj-1").active_history_round_id is None


def test_restart_reconciles_turn_cancelled_during_graceful_shutdown(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.set_turn(
        "proj-1",
        turn_id=None,
        status="idle",
        error=SHUTDOWN_INTERRUPTED_ERROR,
    )

    assert recover_interrupted_native_turns(session) == ["proj-1"]
    root = session.exec(select(RootAgentSession)).one()
    assert root.status == "idle"
    assert root.last_error == (
        "Previous native Codex turn was interrupted by service restart"
    )
    assert recover_interrupted_native_turns(session) == []


def test_restart_does_not_replay_completed_round_from_stale_running_projection(
    session,
):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.begin_turn("proj-1", user_request="为什么已经进入 G6？")
    ProjectService(session).record_project_history_report(
        "proj-1",
        native_turn_id="turn-completed",
        title="解释结果",
        content="已经解释 G6 的来源。",
        report_ref="report:turn-completed",
    )
    store.set_turn(
        "proj-1",
        turn_id=None,
        status="running",
        error=SHUTDOWN_INTERRUPTED_ERROR,
    )

    assert recover_interrupted_native_turns(session) == ["proj-1"]
    root = store.ensure("proj-1")
    assert root.status == "idle"
    assert root.restart_recovery_pending is False
    assert store.get_restart_recovery("proj-1") is None


def test_service_restart_does_not_resume_a_paused_project(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.begin_turn("proj-1", user_request="持续迭代")
    store.set_turn("proj-1", turn_id="turn-1", status="running")
    project = session.get(Project, "proj-1")
    project.status = "paused"
    session.add(project)
    session.commit()

    assert recover_interrupted_native_turns(session) == ["proj-1"]

    session.expire_all()
    root = store.ensure("proj-1")
    assert root.status == "idle"
    assert root.active_turn_id is None
    assert root.restart_recovery_pending is False
    assert store.get_restart_recovery("proj-1") is None


def test_startup_repairs_stale_project_and_unowned_responder_state(session):
    store = RootSessionStore(session)
    root = store.ensure("proj-1")
    root.status = "idle"
    project = session.get(Project, "proj-1")
    project.execution_status = "running"
    project.waiting_reason = "旧进程留下的状态"
    interaction = QuestionInteraction(
        id="interaction-stale",
        project_id="proj-1",
        batch_key="root:stale",
        status="resolving",
        resolution_source="project_owner",
    )
    approval = Approval(
        id="approval-stale",
        project_id="proj-1",
        state="ROOT_AGENT_WAITING_USER",
        approval_type="input",
        status="pending",
    )
    decision = ProjectDecision(
        id="decision-stale",
        project_id="proj-1",
        question="继续吗？",
        decision="继续",
        rationale="测试",
        status="applying",
    )
    session.add_all([root, project, interaction, approval, decision])
    session.commit()

    assert recover_interrupted_native_turns(session) == ["proj-1"]

    session.expire_all()
    repaired = session.get(Project, "proj-1")
    assert repaired.execution_status == "idle"
    assert repaired.waiting_reason is None
    stale = session.get(QuestionInteraction, "interaction-stale")
    assert stale.status == "expired"
    assert stale.resolution_source == "native_turn_interrupted"
    assert session.get(Approval, "approval-stale").status == "expired"
    assert session.get(ProjectDecision, "decision-stale").status == "application_failed"
    assert recover_interrupted_native_turns(session) == []


def test_service_restart_closes_unowned_external_agent_rows(session):
    store = RootSessionStore(session)
    running = store.create_agent(
        "proj-1",
        task_name="browser-check",
        provider="pi",
        access="host_automation",
        objective="run browser verification",
        tool_call_id="ctool-running",
    )
    store.update_agent(running, status="running", latest_message="Pi 正在执行测试")

    assert recover_interrupted_external_agents(session) == [running.id]

    session.expire_all()
    recovered = session.get(AgentSession, running.id)
    assert recovered.status == "failed"
    assert recovered.result["interrupted"] is True
    assert "service restart" in recovered.latest_message
    assert recover_interrupted_external_agents(session) == []


def test_native_event_retention_drops_streams_and_keeps_latest_token_baseline(session):
    def event(kind, *, thread="thread-1", created_suffix=0):
        return EventLog(
            project_id="proj-1",
            level="INFO",
            message=kind,
            meta={
                "profile_type": "root_agent_event",
                "kind": kind,
                "thread_id": thread,
                "turn_id": f"turn-{created_suffix}",
            },
        )

    session.add_all(
        [
            event("item/commandExecution/outputDelta"),
            event("turn/diff/updated"),
            event("thread/tokenUsage/updated", created_suffix=1),
            event("thread/tokenUsage/updated", created_suffix=2),
            event("item/completed"),
            EventLog(
                project_id="proj-1",
                level="INFO",
                message="Root turn performance",
                meta={"profile_type": "root_turn_timing"},
            ),
        ]
    )
    session.commit()

    assert prune_redundant_native_event_snapshots(session) == {
        "verbose_events": 2,
        "token_snapshots": 1,
    }
    rows = list(session.exec(select(EventLog)).all())
    kinds = [row.meta.get("kind") for row in rows]
    assert kinds.count("thread/tokenUsage/updated") == 1
    assert "item/completed" in kinds
    assert any(row.meta.get("profile_type") == "root_turn_timing" for row in rows)


def test_native_event_retention_scopes_cleanup_to_one_project(session):
    session.add(
        Project(
            id="proj-2",
            name="second",
            task_type="feature",
            description="second",
            current_state="COORDINATING",
            workspace_path="/tmp/second",
            context={"orchestration_mode": "root_agent"},
        )
    )
    for project_id in ("proj-1", "proj-2"):
        for kind in (
            "item/commandExecution/outputDelta",
            "thread/tokenUsage/updated",
            "thread/tokenUsage/updated",
        ):
            session.add(
                EventLog(
                    project_id=project_id,
                    level="INFO",
                    message=kind,
                    meta={
                        "profile_type": "root_agent_event",
                        "kind": kind,
                        "thread_id": "same-thread",
                    },
                )
            )
    session.commit()

    assert prune_redundant_native_event_snapshots(
        session, project_id="proj-1"
    ) == {"verbose_events": 1, "token_snapshots": 1}
    assert len(session.exec(select(EventLog).where(EventLog.project_id == "proj-2")).all()) == 3
    assert prune_redundant_native_event_snapshots(session) == {
        "verbose_events": 1,
        "token_snapshots": 1,
    }
