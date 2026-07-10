import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.core.project_service import (
    InvalidStateTransitionError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.models import Project, Task


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def service(session):
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
