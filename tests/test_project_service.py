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


async def test_log_event(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    await service.log_event(project.id, "INFO", "test event", {"key": "value"})
    service.session.commit()
    status = service.get_project_status(project.id)
    assert status["recent_events"][0]["message"] == "test event"
