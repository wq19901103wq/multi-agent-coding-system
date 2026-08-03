import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.runtime import FakeRuntime
from mac_system.config import Settings
from mac_system.agents.coordinator_runtime import CoordinatorEvent, CoordinatorTurnResult
from mac_system.core.coordinator_engine import (
    CoordinatorEngine,
    _AGENT_RUNS,
    _AgentRunHandle,
    _COORDINATOR_WAKEUPS,
    _native_agent_instructions,
    _submit_background,
    close_coordinator_runtimes,
    register_coordinator_service_loop,
)
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.feishu.bridge import FeishuBridge
from mac_system.models import (
    CardProjection,
    CoordinatorAgentSession,
    EventLog,
    NotificationOutbox,
    Project,
    Task,
)


@pytest.fixture
def engine_context(tmp_path):
    db = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(db)
    workspace = tmp_path / "project"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    (workspace / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
    with Session(db) as session:
        session.add(
            Project(
                id="proj-1",
                name="dynamic",
                task_type="feature",
                description="inspect",
                current_state="COORDINATING",
                execution_status="coordinating",
                workspace_path=str(workspace),
                context={"orchestration_mode": "agent_loop"},
            )
        )
        session.commit()
        output = {
            "status": "completed",
            "summary": "repository inspected",
            "changes": [],
            "verification": [{"command": "read README", "result": "ok", "passed": True}],
            "blockers": [],
        }
        runtime = FakeRuntime({"dynamic_agent": output})
        engine = CoordinatorEngine(
            session,
            runtime,
            WorktreeManager(tmp_path),
            Settings(PROJECTS_DIR=tmp_path),
        )
        engine.project = session.get(Project, "proj-1")
        yield session, engine


def test_engine_exposes_only_the_seven_dynamic_host_tools(engine_context):
    _, engine = engine_context
    names = [tool.name for tool in engine._tools(engine.project)]
    assert names == [
        "mac_spawn_agent",
        "mac_send_message",
        "mac_wait_agent",
        "mac_list_agents",
        "mac_request_user_input",
        "mac_report",
        "mac_complete_project",
    ]


def test_native_agent_discovery_is_repository_scoped_and_bounded():
    instructions = _native_agent_instructions("write")

    assert "Do not recursively scan the user's home" in instructions
    assert "repository configuration" in instructions
    assert "narrowly targeted probes" in instructions
    assert "report the missing environment fact to the Coordinator" in instructions
    assert "only one resource-intensive verification command at a time" in instructions
    assert "never launch an equivalent verification concurrently" in instructions


@pytest.mark.asyncio
async def test_completion_wakeup_waits_for_parent_idle_and_coalesces(
    engine_context, monkeypatch
):
    session, engine = engine_context
    coordinator = engine.store.ensure(engine.project.id)
    coordinator.native_thread_id = "parent-thread"
    coordinator.status = "running"
    coordinator.active_turn_id = "parent-turn"
    session.add(coordinator)
    session.commit()
    calls: list[tuple[str, str]] = []

    async def fake_run(self, project_id, trigger):
        calls.append((project_id, trigger))
        return CoordinatorTurnResult(
            thread_id="root-thread",
            turn_id="wakeup-turn",
            status="completed",
            final_message="continued",
            events=(),
        )

    monkeypatch.setattr(CoordinatorEngine, "run", fake_run)
    register_coordinator_service_loop(None)
    engine._schedule_coordinator_wakeup(
        session.get_bind(),
        project_id=engine.project.id,
        task_name="verifier-a",
        task_id="task-a",
    )
    engine._schedule_coordinator_wakeup(
        session.get_bind(),
        project_id=engine.project.id,
        task_name="verifier-b",
        task_id="task-b",
    )

    await asyncio.sleep(0.4)
    assert calls == []
    assert len(_COORDINATOR_WAKEUPS) == 1

    coordinator.status = "idle"
    coordinator.active_turn_id = None
    session.add(coordinator)
    session.commit()
    for _ in range(30):
        if calls and not _COORDINATOR_WAKEUPS:
            break
        await asyncio.sleep(0.05)

    assert len(calls) == 1
    assert calls[0][0] == engine.project.id
    assert "mac_list_agents" in calls[0][1]
    assert _COORDINATOR_WAKEUPS == {}


@pytest.mark.asyncio
async def test_background_agents_use_registered_service_loop_not_caller_loop():
    service_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_service_loop():
        asyncio.set_event_loop(service_loop)
        ready.set()
        service_loop.run_forever()

    thread = threading.Thread(target=run_service_loop, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    register_coordinator_service_loop(service_loop)
    caller_loop = asyncio.get_running_loop()

    async def identify_loop():
        return asyncio.get_running_loop()

    try:
        future, owner = _submit_background(
            identify_loop(), name="test-service-owned-agent"
        )
        assert owner is service_loop
        assert owner is not caller_loop
        assert await asyncio.wrap_future(future) is service_loop
    finally:
        register_coordinator_service_loop(None)
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=2)
        service_loop.close()


def test_agent_finishes_after_short_lived_caller_loop_closes(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'service-loop.db'}")
    SQLModel.metadata.create_all(db)
    workspace = tmp_path / "service-loop-project"
    workspace.mkdir()
    started = threading.Event()
    release = threading.Event()

    class DelayedFakeRuntime(FakeRuntime):
        async def execute(self, *args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait)
            return await super().execute(*args, **kwargs)

    output = {
        "status": "completed",
        "summary": "survived caller loop closure",
        "changes": [],
        "verification": [],
        "blockers": [],
    }
    runtime = DelayedFakeRuntime({"dynamic_agent": output})
    runtime.available_providers = lambda: {"claude_code"}
    runtime.provider_for = lambda _agent: "claude_code"
    with Session(db) as session:
        session.add(
            Project(
                id="proj-service-loop",
                name="service-loop",
                task_type="feature",
                description="verify durable scheduling",
                current_state="COORDINATING",
                execution_status="coordinating",
                workspace_path=str(workspace),
                context={"orchestration_mode": "agent_loop"},
            )
        )
        session.commit()

    service_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()

    def run_service_loop():
        asyncio.set_event_loop(service_loop)
        loop_ready.set()
        service_loop.run_forever()

    thread = threading.Thread(target=run_service_loop, daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=2)
    register_coordinator_service_loop(service_loop)

    async def spawn_from_temporary_loop():
        with Session(db) as session:
            engine = CoordinatorEngine(
                session,
                runtime,
                WorktreeManager(tmp_path),
                Settings(PROJECTS_DIR=tmp_path),
            )
            engine.project = session.get(Project, "proj-service-loop")
            result = await engine._spawn_agent(
                {
                    "task_name": "durable-reader",
                    "message": "finish after this caller returns",
                    "provider": "claude_code",
                    "access": "read_only",
                },
                "ctool-service-loop",
            )
            assert result["status"] == "running"

    try:
        asyncio.run(spawn_from_temporary_loop())
        assert started.wait(timeout=2)
        release.set()
        deadline = time.monotonic() + 5
        status = "running"
        while time.monotonic() < deadline:
            with Session(db) as session:
                agent = session.exec(select(CoordinatorAgentSession)).one()
                status = agent.status
            if status == "completed":
                break
            time.sleep(0.05)
        assert status == "completed"
    finally:
        release.set()
        register_coordinator_service_loop(None)
        asyncio.run_coroutine_threadsafe(
            close_coordinator_runtimes(), service_loop
        ).result(timeout=2)
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=2)
        service_loop.close()


def test_short_lived_caller_can_wait_for_service_loop_owned_agent(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'foreign-wait.db'}")
    SQLModel.metadata.create_all(db)
    workspace = tmp_path / "foreign-wait-project"
    workspace.mkdir()
    started = threading.Event()
    release = threading.Event()

    class DelayedFakeRuntime(FakeRuntime):
        async def execute(self, *args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait)
            return await super().execute(*args, **kwargs)

    runtime = DelayedFakeRuntime({
        "dynamic_agent": {
            "status": "completed",
            "summary": "joined across loops",
            "changes": [],
            "verification": [],
            "blockers": [],
        }
    })
    runtime.available_providers = lambda: {"claude_code"}
    runtime.provider_for = lambda _agent: "claude_code"
    with Session(db) as session:
        session.add(Project(
            id="proj-foreign-wait",
            name="foreign-wait",
            task_type="feature",
            description="verify cross-loop join",
            current_state="COORDINATING",
            execution_status="coordinating",
            workspace_path=str(workspace),
            context={"orchestration_mode": "agent_loop"},
        ))
        session.commit()

    service_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()

    def run_service_loop():
        asyncio.set_event_loop(service_loop)
        loop_ready.set()
        service_loop.run_forever()

    thread = threading.Thread(target=run_service_loop, daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=2)
    register_coordinator_service_loop(service_loop)

    async def spawn_on_service_loop():
        with Session(db) as session:
            engine = CoordinatorEngine(
                session,
                runtime,
                WorktreeManager(tmp_path),
                Settings(PROJECTS_DIR=tmp_path),
            )
            engine.project = session.get(Project, "proj-foreign-wait")
            return await engine._spawn_agent(
                {
                    "task_name": "service-owned-reader",
                    "message": "wait from a Feishu-style caller loop",
                    "provider": "claude_code",
                    "access": "read_only",
                },
                "ctool-foreign-spawn",
            )

    async def wait_from_temporary_loop():
        with Session(db) as session:
            engine = CoordinatorEngine(
                session,
                runtime,
                WorktreeManager(tmp_path),
                Settings(PROJECTS_DIR=tmp_path),
            )
            engine.project = session.get(Project, "proj-foreign-wait")
            return await engine._wait_agent(
                {"timeout_seconds": 2}, "ctool-foreign-wait"
            )

    try:
        spawned = asyncio.run_coroutine_threadsafe(
            spawn_on_service_loop(), service_loop
        ).result(timeout=3)
        assert spawned["status"] == "running"
        assert started.wait(timeout=2)
        timer = threading.Timer(0.1, release.set)
        timer.start()
        result = asyncio.run(wait_from_temporary_loop())
        timer.join(timeout=1)
        assert result["agents"][0]["status"] == "completed"
    finally:
        release.set()
        register_coordinator_service_loop(None)
        asyncio.run_coroutine_threadsafe(
            close_coordinator_runtimes(), service_loop
        ).result(timeout=2)
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=2)
        service_loop.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_joins_agent_owned_by_foreign_loop():
    service_loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    agent_started = threading.Event()
    agent_cancelled = threading.Event()

    def run_service_loop():
        asyncio.set_event_loop(service_loop)
        loop_ready.set()
        service_loop.run_forever()

    async def durable_agent():
        agent_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            agent_cancelled.set()

    async def create_agent_task():
        return asyncio.create_task(durable_agent())

    thread = threading.Thread(target=run_service_loop, daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=2)
    foreign_task = asyncio.run_coroutine_threadsafe(
        create_agent_task(), service_loop
    ).result(timeout=2)
    assert agent_started.wait(timeout=2)
    _AGENT_RUNS["foreign-shutdown"] = _AgentRunHandle(
        "proj-foreign-shutdown", foreign_task, service_loop
    )

    try:
        await asyncio.wait_for(close_coordinator_runtimes(), timeout=2)
        assert agent_cancelled.wait(timeout=2)
        assert "foreign-shutdown" not in _AGENT_RUNS
    finally:
        service_loop.call_soon_threadsafe(service_loop.stop)
        thread.join(timeout=2)
        service_loop.close()


@pytest.mark.asyncio
async def test_root_coordinator_has_no_codex_execution_environment(
    engine_context, monkeypatch
):
    _, engine = engine_context
    captured = {}

    class FakeRoot:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.thread_id = None

        async def create_thread(self):
            self.thread_id = "root-thread"
            return self.thread_id

        async def close(self):
            return None

    monkeypatch.setattr(
        "mac_system.core.coordinator_engine.CodexAppServerCoordinator",
        FakeRoot,
    )
    root = await engine._root_runtime(engine.project)
    try:
        assert captured["environments"] == []
        assert captured["sandbox"] == "read-only"
        assert captured["thread_config"] == {
            "features.multi_agent": False,
            "features.multi_agent_v2": False,
            "features.apps": False,
            "features.plugins": False,
            "features.js_repl": False,
            "features.in_app_browser": False,
        }
        assert captured["codex_home"] == engine.settings.coordinator_codex_home
    finally:
        await engine._release_root(engine.project.id, root)


@pytest.mark.asyncio
async def test_failed_root_thread_is_replaced_and_rehydrated_from_durable_state(
    engine_context, monkeypatch
):
    _, engine = engine_context
    old = engine.store.ensure(engine.project.id)
    old.native_thread_id = "unsafe-old-thread"
    old.status = "failed"
    old.last_error = "provider protocol failed"
    engine.session.add(old)
    engine.session.commit()
    calls = []

    class FakeRoot:
        def __init__(self, **_kwargs):
            self.thread_id = None

        async def create_thread(self):
            calls.append(("create", None))
            self.thread_id = "safe-new-thread"
            return self.thread_id

        async def resume_thread(self, thread_id):
            calls.append(("resume", thread_id))
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, text):
            calls.append(("turn", text))
            return CoordinatorTurnResult(
                thread_id=self.thread_id,
                turn_id="recovery-turn",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        "mac_system.core.coordinator_engine.CodexAppServerCoordinator",
        FakeRoot,
    )

    await engine.run(engine.project.id, "继续")

    assert calls[0] == ("create", None)
    assert not any(kind == "resume" for kind, _ in calls)
    recovery_prompt = next(value for kind, value in calls if kind == "turn")
    assert "Project objective: inspect" in recovery_prompt
    assert "Current user trigger: 继续" in recovery_prompt
    assert engine.store.ensure(engine.project.id).native_thread_id == "safe-new-thread"


@pytest.mark.asyncio
async def test_readonly_agent_and_report_are_projected_to_durable_entities(engine_context):
    session, engine = engine_context
    result = await engine._spawn_agent(
        {
            "task_name": "verify-readme",
            "message": "Inspect README",
            "access": "read_only",
        },
        "ctool-1",
    )
    assert result["status"] == "running"
    waited = await engine._wait_agent({"timeout_seconds": 5}, "ctool-wait-1")
    report = await engine._report(
        {"title": "Progress", "message": "Inspection complete", "kind": "progress"},
        "ctool-2",
    )

    assert waited["agents"][0]["status"] == "completed"
    agent = session.exec(select(CoordinatorAgentSession)).one()
    assert agent.latest_message == "repository inspected"
    assert agent.task_id is not None
    notification = session.exec(select(NotificationOutbox)).one()
    assert notification.payload["content"] == "Inspection complete"
    assert report["status"] == "recorded"


@pytest.mark.asyncio
async def test_dynamic_agent_progress_card_tracks_durable_task_lifecycle(engine_context):
    session, engine = engine_context

    class RecordingBridge:
        def __init__(self):
            self.created = []
            self.updated = []

        async def _send_interactive(self, receive_id, card, receive_id_type="open_id"):
            self.created.append((receive_id, receive_id_type, card))
            return "om-agent-progress"

        async def update_card(self, message_id, card):
            self.updated.append((message_id, card))

    project = session.get(Project, "proj-1")
    project.feishu_chat_id = "oc-test"
    project.feishu_chat_type = "group"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge

    result = await engine._spawn_agent(
        {
            "task_name": "verify-visible-state",
            "message": "Inspect README",
            "access": "read_only",
        },
        "ctool-progress",
    )
    assert result["status"] == "running"
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-progress-wait")
    session.expire_all()

    projection = session.exec(select(CardProjection)).one()
    assert projection.subject_type == "coordinator_agent"
    assert projection.provider_message_id == "om-agent-progress"
    assert projection.lifecycle_status == "completed"
    assert len(bridge.created) == 1
    assert len(bridge.updated) == 1


@pytest.mark.asyncio
async def test_agent_listing_is_compact_by_default_and_full_only_when_named(
    engine_context,
):
    session, engine = engine_context
    coordinator = engine.store.ensure(engine.project.id)
    long_summary = "review evidence " * 500
    session.add(
        CoordinatorAgentSession(
            id="coord-agent-long",
            project_id=engine.project.id,
            coordinator_session_id=coordinator.id,
            task_name="security-review",
            provider="codex",
            access="read_only",
            status="completed",
            objective="review",
            latest_message=long_summary,
            result={"summary": long_summary, "large": "x" * 100_000},
        )
    )
    session.add(
        CoordinatorAgentSession(
            id="coord-agent-short",
            project_id=engine.project.id,
            coordinator_session_id=coordinator.id,
            task_name="writer",
            provider="codex",
            access="write",
            status="running",
            objective="implement",
        )
    )
    session.commit()

    compact = await engine._list_agents({}, "ctool-list-compact")
    assert compact["compact"] is True
    assert len(compact["agents"]) == 2
    review = next(
        item for item in compact["agents"] if item["agent"] == "security-review"
    )
    assert review["summary_truncated"] is True
    assert len(review["summary"]) < len(long_summary)
    assert "result" not in review

    detailed = await engine._list_agents(
        {"agents": ["security-review"], "include_full": True},
        "ctool-list-full",
    )
    assert detailed["compact"] is False
    assert [item["agent"] for item in detailed["agents"]] == ["security-review"]
    assert detailed["agents"][0]["summary"] == long_summary
    assert detailed["agents"][0]["result"]["large"] == "x" * 100_000


@pytest.mark.asyncio
async def test_background_agent_uses_a_fresh_feishu_client(engine_context):
    _, engine = engine_context
    original = FeishuBridge("app-id", "app-secret")
    engine.feishu_bridge = original
    engine.settings.feishu_app_id = "app-id"
    engine.settings.feishu_app_secret = "app-secret"

    background, owned = engine._fresh_background_bridge()
    try:
        assert owned is True
        assert isinstance(background, FeishuBridge)
        assert background is not original
        assert background._client is not original._client
    finally:
        await background.close()
        await original.close()


@pytest.mark.asyncio
async def test_spawn_returns_running_identity_and_readonly_agents_execute_concurrently(
    engine_context,
):
    _, engine = engine_context
    gate = asyncio.Event()
    both_started = asyncio.Event()
    original_execute = engine.runtime.execute
    started = 0

    async def blocked_execute(*args, **kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await gate.wait()
        return await original_execute(*args, **kwargs)

    engine.runtime.execute = blocked_execute
    first = await engine._spawn_agent(
        {"task_name": "parallel-a", "message": "Inspect A", "access": "read_only"},
        "ctool-parallel-a",
    )
    second = await engine._spawn_agent(
        {"task_name": "parallel-b", "message": "Inspect B", "access": "read_only"},
        "ctool-parallel-b",
    )

    assert first["status"] == second["status"] == "running"
    await asyncio.wait_for(both_started.wait(), timeout=2)
    gate.set()
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-parallel-wait")
    final = await engine._list_agents({}, "ctool-parallel-list")
    assert {item["status"] for item in final["agents"]} == {"completed"}


@pytest.mark.asyncio
async def test_root_turn_end_preserves_running_agent_project_status(
    engine_context, monkeypatch
):
    session, engine = engine_context

    class FakeRoot:
        def __init__(self, **_kwargs):
            self.thread_id = None

        async def create_thread(self):
            self.thread_id = "root-status-thread"
            return self.thread_id

        async def run_turn(self, _text):
            task = await engine.service.create_task(
                "proj-1",
                agent_type="dynamic_agent",
                state="COORDINATOR_AGENT",
                input_data={"objective": "still running"},
            )
            await engine.service.start_task(
                task.id, engine.project.workspace_path
            )
            return CoordinatorTurnResult(
                thread_id=self.thread_id,
                turn_id="root-status-turn",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        "mac_system.core.coordinator_engine.CodexAppServerCoordinator",
        FakeRoot,
    )

    await engine.run("proj-1", "continue")
    session.expire_all()

    assert session.get(Project, "proj-1").execution_status == "running_agent"


@pytest.mark.asyncio
async def test_non_codex_followup_keeps_failed_write_worktree_and_agent_identity(
    engine_context,
):
    session, engine = engine_context
    engine.runtime.outputs["dynamic_agent"] = {
        "success": False,
        "summary": "first attempt failed after editing",
    }
    started = await engine._spawn_agent(
        {
            "task_name": "durable-writer",
            "message": "Implement the change",
            "provider": "FakeRuntime",
            "access": "write",
        },
        "ctool-write-first",
    )
    assert started["status"] == "running", started
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-write-first-wait")
    session.expire_all()
    agent = session.exec(select(CoordinatorAgentSession)).one()
    assert agent.status == "failed"
    failed_worktree = Path(agent.worktree_path)
    (failed_worktree / "partial.txt").write_text("preserve me\n", encoding="utf-8")

    engine.runtime.outputs["dynamic_agent"] = {
        "status": "completed",
        "summary": "continued successfully",
        "changes": ["partial.txt"],
        "verification": [],
        "blockers": [],
    }
    continued = await engine._send_message(
        {"agent": "durable-writer", "message": "Continue from current files"},
        "ctool-write-followup",
    )
    assert continued["status"] == "running"
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-write-followup-wait")
    session.expire_all()

    agents = list(session.exec(select(CoordinatorAgentSession)).all())
    assert len(agents) == 1
    assert agents[0].status == "completed"
    assert agents[0].result["host_delivery"]["merged"] is True
    assert agents[0].result["host_delivery"]["canonical_head"]
    assert (Path(engine.project.workspace_path) / "partial.txt").read_text() == "preserve me\n"


@pytest.mark.asyncio
async def test_send_message_cannot_start_a_concurrent_agent_continuation(
    engine_context,
):
    session, engine = engine_context
    gate = asyncio.Event()
    started = asyncio.Event()
    original_execute = engine.runtime.execute

    async def blocked_execute(*args, **kwargs):
        started.set()
        await gate.wait()
        return await original_execute(*args, **kwargs)

    engine.runtime.execute = blocked_execute
    first = await engine._spawn_agent(
        {
            "task_name": "single-writer",
            "message": "Implement once",
            "provider": "FakeRuntime",
            "access": "write",
        },
        "ctool-single-writer",
    )
    assert first["status"] == "running"
    await asyncio.wait_for(started.wait(), timeout=2)

    followup = await engine._send_message(
        {"agent": "single-writer", "message": "Do not fork the running task"},
        "ctool-concurrent-followup",
    )

    assert followup["status"] == "blocked"
    assert followup["agent"]["status"] == "running"
    assert len(list(session.exec(select(Task)).all())) == 1
    assert len(list(session.exec(select(CoordinatorAgentSession)).all())) == 1

    gate.set()
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-single-writer-wait")
    session.expire_all()
    agent = session.exec(select(CoordinatorAgentSession)).one()
    assert agent.status == "completed"


@pytest.mark.asyncio
async def test_transient_provider_deltas_do_not_pollute_durable_project_events(
    engine_context,
):
    session, engine = engine_context
    event = CoordinatorEvent(
        kind="item/agentMessage/delta",
        thread_id="thread-1",
        turn_id="turn-1",
        payload={"delta": "draft"},
    )

    await engine._on_event("proj-1", event)

    assert engine.store.ensure("proj-1").event_cursor == 0


def test_completed_agent_loop_status_is_user_facing_and_hides_protocol_events(
    engine_context,
):
    session, engine = engine_context
    project = session.get(Project, "proj-1")
    project.status = "completed"
    project.current_state = "COMPLETED"
    project.execution_status = "idle"
    session.add(project)
    session.add(
        EventLog(
            project_id=project.id,
            level="INFO",
            message="Coordinator event: thread/tokenUsage/updated",
            meta={"profile_type": "coordinator_event"},
        )
    )
    session.commit()

    status = engine.service.get_project_status(project.id)

    assert status["display"] == "交付完成"
    assert status["recent_events"] == []


@pytest.mark.asyncio
async def test_codex_send_message_resumes_the_same_native_agent_session(
    engine_context, monkeypatch
):
    session, engine = engine_context
    engine.runtime.available_providers = lambda: {"codex"}
    engine.runtime.provider_for = lambda _agent: "codex"
    calls: list[tuple[str, str]] = []

    class FakeNativeChild:
        def __init__(self, **kwargs):
            self.thread_id = None
            self.cwd = Path(kwargs["cwd"])
            calls.append(("home", str(kwargs.get("codex_home"))))
            calls.append(("sandbox", str(kwargs.get("sandbox"))))
            calls.append(("cwd", str(self.cwd)))

        async def create_thread(self):
            self.thread_id = "child-thread"
            calls.append(("create", self.thread_id))
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            calls.append(("resume", thread_id))
            return thread_id

        async def run_turn(self, text):
            calls.append(("turn", text))
            (self.cwd / "verifier-scratch.txt").write_text(
                "disposable verification state\n", encoding="utf-8"
            )
            return CoordinatorTurnResult(
                thread_id=self.thread_id,
                turn_id=f"turn-{len(calls)}",
                status="completed",
                final_message=f"done: {text}",
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        "mac_system.core.coordinator_engine.CodexAppServerCoordinator",
        FakeNativeChild,
    )
    first = await engine._spawn_agent(
        {
            "task_name": "native-verifier",
            "message": "inspect first",
            "provider": "codex",
            "access": "read_only",
        },
        "ctool-native-1",
    )
    assert first["status"] == "running"
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-native-wait")
    second = await engine._send_message(
        {"agent": "native-verifier", "message": "check the correction"},
        "ctool-native-2",
    )

    assert second["status"] == "running"
    await engine._wait_agent({"timeout_seconds": 5}, "ctool-native-wait-2")
    session.expire_all()
    agents = list(session.exec(select(CoordinatorAgentSession)).all())
    assert len(agents) == 1
    assert agents[0].native_session_id == "child-thread"
    assert ("resume", "child-thread") in calls
    assert ("home", str(engine.settings.agent_codex_home)) in calls
    assert ("sandbox", "workspace-write") in calls
    assert all(
        cwd != engine.project.workspace_path
        for kind, cwd in calls
        if kind == "cwd"
    )
    assert not (Path(engine.project.workspace_path) / "verifier-scratch.txt").exists()
    assert agents[0].worktree_path is None
