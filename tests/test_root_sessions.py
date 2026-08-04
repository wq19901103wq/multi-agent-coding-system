import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.root_runtime import (
    AgentEvent,
    ToolContext,
)
from mac_system.core.root_sessions import (
    SHUTDOWN_INTERRUPTED_ERROR,
    RootSessionStore,
    recover_interrupted_native_turns,
)
from mac_system.models import (
    AgentSession,
    Approval,
    RootAgentSession,
    DynamicToolCall,
    EventLog,
    Project,
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


def test_service_restart_recovers_interrupted_native_turn(session):
    store = RootSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    store.set_turn("proj-1", turn_id="turn-1", status="waiting_user")
    project = session.get(Project, "proj-1")
    project.execution_status = "waiting_user"
    project.waiting_reason = "请选择"
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
    assert session.get(Project, "proj-1").execution_status == "idle"
    assert session.get(Approval, "approval-1").status == "expired"
    assert session.get(QuestionInteraction, "interaction-1").status == "expired"


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
