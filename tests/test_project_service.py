import pytest
from contextlib import contextmanager
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.core.project_service import (
    InvalidStateTransitionError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.models import (
    ConversationSession,
    FeedbackPacket,
    PendingConversationAction,
    Task,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def service(session, monkeypatch):
    from mac_system.config import get_settings

    monkeypatch.setattr(get_settings(), "mac_allowed_bind_roots", "/")
    return ProjectService(session)


async def test_create_project(service):
    project = await service.create_project(
        name="test-project",
        task_type="feature",
        description="Test project",
    )
    assert project.id.startswith("proj_")
    assert project.name == "test-project"
    assert project.task_type == "feature"
    assert project.current_state == "PLANNING_REQUIREMENTS"
    assert project.status == "active"
    assert project.workspace_path


async def test_create_project_with_creator(service):
    project = await service.create_project(
        name="test-project",
        task_type="feature",
        description="Test project",
        creator_open_id="ou_123",
    )
    from mac_system.models import ProjectUser

    users = service.session.exec(
        select(ProjectUser).where(ProjectUser.project_id == project.id)
    ).all()
    assert len(users) == 1
    assert users[0].open_id == "ou_123"


async def test_reconcile_project_invariants_repairs_terminal_and_missing_approval(service):
    completed = await service.create_project(
        name="completed", task_type="feature", description="done"
    )
    completed.current_state = "COMPLETED"
    completed.status = "active"
    awaiting = await service.create_project(
        name="awaiting", task_type="feature", description="confirm"
    )
    awaiting.current_state = "REQUIREMENT_CONFIRM_PENDING"
    service.session.commit()

    needing_tick = service.reconcile_project_invariants()

    assert service.require_project(completed.id).status == "completed"
    assert awaiting.id in needing_tick


async def test_chat_directory_binding_is_inherited_and_project_metadata_is_isolated(
    service, tmp_path
):
    repo = tmp_path / "existing-repo"
    binding = service.bind_directory_to_chat("oc_bound", str(repo), "ou_owner")
    assert binding.directory == str(repo.resolve())

    first = await service.create_project(
        name="first",
        task_type="feature",
        description="one",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_bound",
        feishu_chat_type="group",
    )
    second = await service.create_project(
        name="second",
        task_type="feature",
        description="two",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_bound",
        feishu_chat_type="group",
    )
    assert first.workspace_path == second.workspace_path == str(repo.resolve())
    assert first.bound_directory == str(repo.resolve())
    assert (repo / ".mac" / "projects" / first.id / "STATE.json").exists()
    assert (repo / ".mac" / "projects" / second.id / "STATE.json").exists()

    service.unbind_directory_from_chat("oc_bound")
    assert service.get_chat_directory_binding("oc_bound") is None
    assert first.bound_directory == str(repo.resolve())


def test_directory_binding_rejects_path_outside_allowlist(service, tmp_path, monkeypatch):
    from mac_system.config import get_settings

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(get_settings(), "mac_allowed_bind_roots", str(allowed))

    with pytest.raises(ValueError, match="MAC_ALLOWED_BIND_ROOTS"):
        service.bind_directory_to_chat("oc_outside", str(outside), "ou_owner")


async def test_get_project(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    fetched = service.get_project(project.id)
    assert fetched is not None
    assert fetched.id == project.id


def test_require_project_not_found(service):
    with pytest.raises(ProjectNotFoundError):
        service.require_project("proj_nonexistent")


async def test_transition_state(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    # From PLANNING_REQUIREMENTS we can go to REQUIREMENT_CONFIRM_PENDING via complete
    updated = await service.transition_state(project.id, "complete")
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert updated.previous_state == "PLANNING_REQUIREMENTS"


async def test_invalid_transition(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    with pytest.raises(InvalidStateTransitionError):
        await service.transition_state(project.id, "confirm")


async def test_artifact_versions_propagate_staleness_and_confirm(service):
    project = await service.create_project(
        name="versions", task_type="feature", description="Test"
    )
    service.update_artifact_version(project.id, "requirements", 1, 1, "confirmed")
    service.update_artifact_version(project.id, "architecture", 1, 1, "confirmed")
    service.update_artifact_version(project.id, "specs", 1, 1, "confirmed")

    service.mark_downstream_artifacts_stale(project.id, "requirements")
    assert service.get_artifact_version(project.id, "architecture")["status"] == "stale"
    assert service.get_artifact_version(project.id, "specs")["status"] == "stale"
    assert {item["artifact_id"] for item in service.get_stale_artifacts(project.id)} == {
        "architecture", "specs"
    }

    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    service.update_artifact_version(
        project.id, "requirements", 2, 2, "pending_confirmation"
    )
    await service.transition_state(project.id, "confirm")
    assert service.get_artifact_version(project.id, "requirements")["status"] == "confirmed"


async def test_create_and_complete_task(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={"description": "plan"},
    )
    assert task.id.startswith("task_")
    assert task.status == "pending"

    await service.start_task(task.id, "/tmp/wt")
    started = service.session.get(Task, task.id)
    assert started.status == "running"
    assert started.worktree_path == "/tmp/wt"

    completed = await service.complete_task(
        task.id,
        output_data={"success": True, "next_state_event": "complete"},
        cost_info={"estimated_cost_usd": 0.01},
    )
    assert completed.status == "completed"
    assert completed.cost_usd == 0.01


async def test_create_approval(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        options=[
            {"action": "confirm", "label": "Confirm", "next_state": "PLANNING_ARCHITECTURE"},
            {"action": "reject", "label": "Reject", "next_state": "PLANNING_REQUIREMENTS"},
        ],
    )
    assert approval.id.startswith("appr_")
    assert approval.status == "pending"

    responded = await service.respond_approval(approval.id, "confirm", "ou_123")
    assert responded.status == "responded"
    assert responded.response == "confirm"
    assert responded.responded_by == "ou_123"


async def test_complete_task_accumulates_cost(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={"description": "plan"},
    )
    await service.start_task(task.id, "/tmp/wt")
    await service.complete_task(
        task.id,
        output_data={"success": True},
        cost_info={"estimated_cost_usd": 0.42},
    )
    updated_project = service.get_project(project.id)
    assert updated_project.total_cost_usd == 0.42

    task2 = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await service.start_task(task2.id, "/tmp/wt")
    await service.complete_task(
        task2.id,
        output_data={"success": True},
        cost_info={"estimated_cost_usd": 0.18},
    )
    updated_project = service.get_project(project.id)
    assert updated_project.total_cost_usd == pytest.approx(0.60)


def test_recover_stale_tasks(service):
    import asyncio

    project = asyncio.run(
        service.create_project(name="test", task_type="feature", description="Test")
    )
    task = asyncio.run(
        service.create_task(
            project_id=project.id,
            agent_type="planner",
            state="PLANNING_REQUIREMENTS",
            input_data={},
        )
    )
    asyncio.run(service.start_task(task.id, "/tmp/wt"))

    recovered = service.recover_stale_tasks()
    assert len(recovered) == 1
    assert recovered[0].id == task.id
    assert recovered[0].status == "failed"
    assert "restarted" in recovered[0].error_message


def test_recover_stale_tasks_empty(service):
    assert service.recover_stale_tasks() == []


async def test_restart_recovery_reschedules_active_agent_project(
    service, session, monkeypatch
):
    from mac_system.api import main as api_main

    project = await service.create_project(
        name="restart-recovery", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state=project.current_state,
        input_data={},
    )
    await service.start_task(task.id, "/tmp/wt")
    service.recover_stale_tasks()

    @contextmanager
    def same_session_scope():
        yield session

    ticked: list[str] = []

    class StubOrchestrator:
        feishu_bridge = None

        async def tick(self, project_id: str) -> None:
            ticked.append(project_id)

    monkeypatch.setattr(api_main, "session_scope", same_session_scope)
    monkeypatch.setattr(api_main, "_build_orchestrator", lambda _session: StubOrchestrator())

    await api_main._resume_recovered_project(project.id)

    assert ticked == [project.id]


async def test_full_crash_restart_lifecycle_marks_task_failed_and_resumes(
    service, session, monkeypatch
):
    """B7: simulate the full crash → restart → resume path end-to-end.

    Sequence:
      1. Project is mid-execution (task running).
      2. Server process is killed (simulated by recover_stale_tasks).
      3. On startup, reconcile_project_invariants runs.
      4. _resume_recovered_project schedules a tick for each recovered project.

    Asserts:
      - Running task transitioned to "failed" with a restart error message.
      - Project remains in its pre-crash state (state machine intact).
      - Reconcile identifies the project as needing a tick.
      - Resume triggers orchestrator.tick for the recovered project.
    """
    from mac_system.api import main as api_main

    project = await service.create_project(
        name="crash-lifecycle", task_type="feature", description="Test"
    )
    # Force the project into an agent-execution state so the invariant checker
    # treats it as resumable.
    project.current_state = "CODING"
    service.session.add(project)
    service.session.commit()

    running_task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await service.start_task(running_task.id, "/tmp/wt")

    # Step 1: server killed → on next startup, recover_stale_tasks runs first.
    recovered = service.recover_stale_tasks()
    assert [t.id for t in recovered] == [running_task.id]

    service.session.refresh(running_task)
    assert running_task.status == "failed"
    assert "restarted" in (running_task.error_message or "")

    # Step 2: project state is unchanged (crash did not corrupt state machine).
    service.session.refresh(project)
    assert project.current_state == "CODING"
    assert project.status == "active"

    # Step 3: on startup, the set of "projects to resume" is built from
    # recover_stale_tasks (failed running tasks) + reconcile (human-state
    # repairs). For a crashed *running* agent with no completed output,
    # reconcile contributes nothing — the resume path goes through
    # recover_stale_tasks alone.
    needing_tick = service.reconcile_project_invariants()
    assert project.id not in needing_tick
    recovered_project_ids = {t.project_id for t in recovered}
    assert project.id in recovered_project_ids

    # Step 4: _resume_recovered_project triggers an orchestrator tick.
    @contextmanager
    def same_session_scope():
        yield session

    ticked: list[str] = []

    class StubOrchestrator:
        feishu_bridge = None

        async def tick(self, project_id: str) -> None:
            ticked.append(project_id)

    monkeypatch.setattr(api_main, "session_scope", same_session_scope)
    monkeypatch.setattr(api_main, "_build_orchestrator", lambda _session: StubOrchestrator())

    await api_main._resume_recovered_project(project.id)
    assert ticked == [project.id]


async def test_pending_intent_crud(service):
    intent = service.upsert_pending_intent(
        open_id="ou_123",
        description="实现一个 config-validator CLI",
        stage="awaiting_type_confirm",
    )
    assert intent.open_id == "ou_123"
    assert intent.stage == "awaiting_type_confirm"

    fetched = service.get_pending_intent("ou_123")
    assert fetched is not None
    assert fetched.description == "实现一个 config-validator CLI"

    service.delete_pending_intent("ou_123")
    assert service.get_pending_intent("ou_123") is None


async def test_pending_intent_survives_new_service(session):
    """Simulate a service restart by creating a fresh ProjectService on the same DB."""
    service1 = ProjectService(session)
    service1.upsert_pending_intent(
        open_id="ou_456",
        description="修复 port 校验范围 bug",
        stage="awaiting_type_confirm",
    )

    service2 = ProjectService(session)
    intent = service2.get_pending_intent("ou_456")
    assert intent is not None
    assert intent.description == "修复 port 校验范围 bug"


async def test_pending_intent_expires_after_one_hour(service):
    from datetime import timedelta

    intent = service.upsert_pending_intent(
        open_id="ou_789",
        description="写一份 README",
        stage="awaiting_description",
    )
    # Manually age the record beyond the TTL.
    intent.created_at = intent.created_at - timedelta(hours=2)
    service.session.commit()

    assert service.get_pending_intent("ou_789") is None


async def test_night_mode_inside_window_uses_night_config(service, monkeypatch):
    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: True)
    )
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )
    # Night config auto-resolves clarifications/conflicts and only requires
    # REQUIREMENT_CONFIRM_PENDING and ACCEPTANCE_PENDING.
    assert project.approval_config.get("auto_resolve_clarification") is True
    assert "REQUIREMENT_CONFIRM_PENDING" in project.approval_config.get(
        "require_confirmation_for", []
    )


async def test_night_mode_outside_window_falls_back_to_normal(service, monkeypatch):
    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: False)
    )
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )
    # Outside the night window, night mode behaves like normal.
    assert project.approval_config.get("auto_resolve_clarification") is False
    assert "human_confirmation" in project.approval_config.get(
        "require_confirmation_for_types", []
    )


def test_night_window_wrap_around_midnight():
    """A 23:00-08:00 window should be active at midnight and inactive at noon."""
    window = {"start_time": "23:00", "end_time": "08:00", "timezone": "Asia/Shanghai"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(window.get("timezone", "Asia/Shanghai"))

    midnight = datetime(2026, 1, 1, 1, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=midnight) is True

    noon = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=noon) is False

    edge_start = datetime(2026, 1, 1, 23, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=edge_start) is True

    edge_end = datetime(2026, 1, 1, 8, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=edge_end) is False


def test_night_window_non_wrapping():
    """A 09:00-18:00 window should be active during the day."""
    window = {"start_time": "09:00", "end_time": "18:00", "timezone": "Asia/Shanghai"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(window.get("timezone", "Asia/Shanghai"))
    during = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=during) is True

    before = datetime(2026, 1, 1, 8, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=before) is False


def test_event_dedup(service):
    assert service.is_event_processed("evt_1") is False
    service.mark_event_processed("evt_1", "message")
    service.session.commit()
    assert service.is_event_processed("evt_1") is True


async def test_create_project_binds_chat_and_context(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
        creator_open_id="ou_123",
        feishu_chat_id="chat_abc",
        feishu_chat_type="group",
    )
    from mac_system.models import ProjectChat, ChatUserContext

    chats = service.session.exec(
        select(ProjectChat).where(ProjectChat.project_id == project.id)
    ).all()
    assert len(chats) == 1
    assert chats[0].chat_id == "chat_abc"

    ctx = service.session.exec(
        select(ChatUserContext).where(
            ChatUserContext.chat_id == "chat_abc",
            ChatUserContext.open_id == "ou_123",
        )
    ).first()
    assert ctx is not None
    assert ctx.active_project_id == project.id


async def test_chat_user_context_routing(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
        creator_open_id="ou_123",
        feishu_chat_id="chat_abc",
        feishu_chat_type="group",
    )
    active = service.get_chat_user_active_project("chat_abc", "ou_123")
    assert active is not None
    assert active.id == project.id

    service.set_chat_user_context("chat_abc", "ou_123", "proj_nonexistent")
    assert service.get_chat_user_active_project("chat_abc", "ou_123") is None


async def test_list_projects_for_chat(service):
    p1 = await service.create_project(
        name="p1", task_type="feature", description="p1", feishu_chat_id="chat_x"
    )
    p2 = await service.create_project(
        name="p2", task_type="feature", description="p2", feishu_chat_id="chat_x"
    )
    await service.cancel_project(p2.id)
    projects = service.list_projects_for_chat("chat_x")
    assert [p.id for p in projects] == [p1.id]


async def test_cancelled_project_cannot_transition_from_late_agent_result(service):
    project = await service.create_project(
        name="cancel-race", task_type="feature", description="Test"
    )
    await service.cancel_project(project.id)

    with pytest.raises(InvalidStateTransitionError):
        await service.transition_state(project.id, "complete")

    refreshed = service.get_project(project.id)
    assert refreshed.status == "cancelled"
    assert refreshed.current_state == "PLANNING_REQUIREMENTS"


async def test_cancel_project_clears_conversation_session_and_pending_actions(service):
    project = await service.create_project(
        name="cancel-cleanup", task_type="feature", description="Test"
    )
    chat_id = "chat_cleanup"
    conversation = service.get_or_create_conversation_session(chat_id)
    service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    action = service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="confirm_plan",
        payload={"key": "value"},
        source_message_ids=["m1"],
        idempotency_key="key1",
        created_by="ou_1",
        target_project_id=project.id,
    )

    await service.cancel_project(project.id)

    refreshed = service.session.get(ConversationSession, conversation.id)
    assert refreshed.active_project_id is None
    assert refreshed.pending_action_id is None

    refreshed_action = service.session.get(PendingConversationAction, action.id)
    assert refreshed_action.status == "expired"


async def test_create_approval_expires_existing_pending(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    old = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Old?",
        options=[],
    )
    assert old.status == "pending"

    new = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="New?",
        options=[],
    )
    service.session.refresh(old)
    assert old.status == "expired"
    assert new.status == "pending"


async def test_resolve_approval_is_atomic(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    await service.transition_state(project.id, "complete")
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        options=[{"action": "confirm", "label": "Confirm"}],
    )
    responded, updated = await service.resolve_approval(
        approval.id, "confirm", responded_by="ou_123"
    )
    assert responded.status == "responded"
    assert updated.current_state == "PLANNING_ARCHITECTURE"


async def test_feedback_packet_commit_param(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="x",
        commit=False,
    )
    # Not committed yet
    assert service.session.get(FeedbackPacket, packet.id) is packet
    service.session.commit()
    assert service.session.get(FeedbackPacket, packet.id) is not None


# ---------------------------------------------------------------------------
# QuestionInteraction / Question / QuestionAnswer
# ---------------------------------------------------------------------------


async def test_create_question_interaction_with_questions(service):
    project = await service.create_project(name="qi-test", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="req_batch_1",
        questions=[
            {"question_id": "q_scope", "kind": "blocking", "question": "范围？",
             "reason": "不明确", "allow_free_text": False,
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True},
                         {"key": "b", "label": "B", "impact": "", "recommended": False}],
             "affected_trace_ids": ["REQ-001"]},
            {"question_id": "q_extra", "kind": "non_blocking", "question": "补充？",
             "reason": "", "allow_free_text": True, "options": [], "affected_trace_ids": []},
        ],
    )
    assert interaction.id.startswith("qi_")
    assert interaction.status == "pending"
    questions = service.get_questions_for_interaction(interaction.id)
    assert len(questions) == 2
    assert questions[0].question_id == "q_scope"
    assert questions[0].recommended_option == "a"
    assert questions[1].allow_free_text is True


async def test_submit_answer_and_resolvable(service):
    project = await service.create_project(name="qi-r", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "kind": "blocking", "question": "Q1",
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": []},
            {"question_id": "q2", "kind": "non_blocking", "question": "Q2",
             "options": [], "allow_free_text": True, "affected_trace_ids": []},
        ],
    )
    assert service.interaction_is_resolvable(interaction.id) is False
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(questions[0].id, "a", "A", False, "ou_user")
    assert service.interaction_is_resolvable(interaction.id) is True
    resolved = await service.resolve_question_interaction(interaction.id)
    assert resolved.status == "answered"


async def test_resolve_question_interaction_syncs_answers_to_confirmed_brief(service):
    project = await service.create_project(
        name="qi-brief", task_type="feature", description="Test",
        initial_context={"confirmed_brief": {"decisions": []}},
    )
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "decision_key": "requirements.scope", "kind": "blocking",
             "question": " scope?", "options": [], "allow_free_text": True,
             "affected_trace_ids": []},
        ],
    )
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(
        questions[0].id, "web only", "仅 Web 端", False, "ou_user"
    )
    await service.resolve_question_interaction(interaction.id)

    updated = service.get_project(project.id)
    brief = updated.context["confirmed_brief"]
    assert len(brief["decisions"]) == 1
    assert brief["decisions"][0]["key"] == "requirements.scope"
    assert brief["decisions"][0]["answer"] == "仅 Web 端"
    assert brief["decisions"][0]["status"] == "confirmed"


async def test_skip_blocking_is_resolvable(service):
    project = await service.create_project(name="qi-s", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="CODING", agent="coder", stage=None, batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(questions[0].id, None, None, True, "ou_user")
    assert service.interaction_is_resolvable(interaction.id) is True


async def test_get_open_question_interaction(service):
    project = await service.create_project(name="qi-o", task_type="feature", description="Test")
    assert service.get_open_question_interaction(project.id) is None
    await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    open_it = service.get_open_question_interaction(project.id)
    assert open_it is not None


async def test_expire_open_question_interactions(service):
    project = await service.create_project(name="qi-e", task_type="feature", description="Test")
    await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    assert service.expire_open_question_interactions(project.id) == 1
    assert service.get_open_question_interaction(project.id) is None


async def test_serialize_question_interaction(service):
    project = await service.create_project(name="qi-ser", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "kind": "blocking", "question": "Q1",
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": ["REQ-001"]},
            {"question_id": "q2", "kind": "decision", "question": "Q2",
             "options": [{"key": "x", "label": "X", "impact": "", "recommended": False},
                         {"key": "y", "label": "Y", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": []},
        ],
    )
    data = service.serialize_question_interaction(interaction.id)
    assert data is not None
    assert len(data["questions"]) == 2
    assert data["questions"][0]["recommended_option"] == "a"
    assert data["questions"][1]["recommended_option"] == "y"
    assert data["is_resolvable"] is False


async def test_default_authorization_prefix_match(service):
    project = await service.create_project(name="dauth", task_type="feature", description="Test")
    assert service.get_default_authorization(project.id, "q_agent_scope") is None
    await service.create_default_authorization(project.id, "q_agent", "chat_kb")
    auth = service.get_default_authorization(project.id, "q_agent_scope")
    assert auth is not None
    assert auth.authorized_option == "chat_kb"
    assert service.get_default_authorization(project.id, "q_blog") is None


# ---------------------------------------------------------------------------
# ConversationInteraction
# ---------------------------------------------------------------------------


async def test_conversation_interaction_create_and_resolve(service):
    ci = await service.create_conversation_interaction(
        chat_id="oc_c", open_id="ou_u", source_message_id="om_1",
        source_text="帮我加博客",
        candidates=[{"id": "proj_a", "name": "A"}, {"id": "proj_b", "name": "B"}],
    )
    assert ci.id.startswith("ci_")
    assert ci.status == "pending"
    assert len(ci.candidates) == 2
    pending = service.get_pending_conversation_interaction("oc_c", "ou_u")
    assert pending is not None
    resolved = await service.resolve_conversation_interaction(ci.id, "proj_a")
    assert resolved.status == "resolved"
    assert resolved.selected_project_id == "proj_a"
    assert service.get_pending_conversation_interaction("oc_c", "ou_u") is None


async def test_conversation_interaction_expires_previous(service):
    await service.create_conversation_interaction(
        chat_id="oc_c2", open_id="ou_u2", source_message_id="om_1",
        source_text="x", candidates=[],
    )
    await service.create_conversation_interaction(
        chat_id="oc_c2", open_id="ou_u2", source_message_id="om_2",
        source_text="y", candidates=[],
    )
    from mac_system.models import ConversationInteraction
    pending = service.session.exec(
        select(ConversationInteraction).where(ConversationInteraction.chat_id == "oc_c2")
        .where(ConversationInteraction.status == "pending")
    ).all()
    assert len(pending) == 1
