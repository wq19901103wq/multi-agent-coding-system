import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.coordinator_runtime import (
    CoordinatorEvent,
    CoordinatorToolContext,
)
from mac_system.core.coordinator_sessions import CoordinatorSessionStore
from mac_system.models import (
    CoordinatorAgentSession,
    CoordinatorSession,
    CoordinatorToolCall,
    EventLog,
    Project,
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
                context={"orchestration_mode": "agent_loop"},
            )
        )
        value.commit()
        yield value


@pytest.mark.asyncio
async def test_tool_call_is_persisted_before_effect_and_completed_result_is_replayed(
    session,
):
    store = CoordinatorSessionStore(session)
    context = CoordinatorToolContext("thread-1", "turn-1", "call-1", "report")
    observed = []

    async def execute(tool_call_id):
        row = session.get(CoordinatorToolCall, tool_call_id)
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
    calls = list(session.exec(select(CoordinatorToolCall)).all())
    assert len(calls) == 1
    assert calls[0].status == "completed"


def test_native_session_event_cursor_and_agent_registry_are_durable(session):
    store = CoordinatorSessionStore(session)
    store.set_native_thread("proj-1", "thread-1")
    first = store.append_event(
        "proj-1", CoordinatorEvent("item/started", "thread-1", "turn-1", {"x": 1})
    )
    second = store.append_event(
        "proj-1", CoordinatorEvent("item/completed", "thread-1", "turn-1", {"x": 2})
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
    coordinator = session.exec(select(CoordinatorSession)).one()
    assert coordinator.native_thread_id == "thread-1"
    assert coordinator.event_cursor == 2
    assert len(session.exec(select(EventLog)).all()) == 2
    assert session.exec(select(CoordinatorAgentSession)).one().id == agent.id
    assert store.create_agent(
        "proj-1",
        task_name="inspect",
        provider="claude_code",
        access="write",
        objective="must not replace the first registration",
        tool_call_id="ctool-2",
    ).id == agent.id
