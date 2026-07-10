import asyncio
import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import FakeRuntime
from mac_system.config import get_agent_config
from mac_system.core.lock_manager import ProjectLockManager
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Task


@pytest.fixture
def session():
    get_agent_config.cache_clear()
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def orchestrator(session, tmp_path):
    service = ProjectService(session)
    worktree_manager = WorktreeManager(Path(service.settings.projects_dir))
    renderer = PromptRenderer(get_agent_config())
    return service, worktree_manager, renderer


@pytest.mark.asyncio
async def test_orchestrator_auto_runs_planner_requirements(orchestrator, tmp_path):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # REQUIREMENTS.json should be written
    req_file = Path(updated.workspace_path) / ".mac" / "REQUIREMENTS.json"
    assert req_file.exists()
    data = json.loads(req_file.read_text())
    assert data["summary"] == "config-validator CLI"


@pytest.mark.asyncio
async def test_orchestrator_auto_confirms_and_runs_architecture(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "手写校验器",
            "tech_stack": ["Python"],
            "modules": [],
            "decisions": [],
            "risks": [],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    # requirements -> confirm -> architecture
    await oc.tick(project.id)  # PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING
    await oc.tick(project.id)  # REQUIREMENT_CONFIRM_PENDING -> PLANNING_ARCHITECTURE
    await oc.tick(project.id)  # PLANNING_ARCHITECTURE -> ARCHITECTURE_CONFIRM_PENDING

    updated = service.get_project(project.id)
    assert updated.current_state == "ARCHITECTURE_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_orchestrator_normal_mode_waits_for_confirmation(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="normal",
    )

    await oc.tick(project.id)  # PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING
    await oc.tick(project.id)  # create pending approval

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # Should have created a pending approval
    status = service.get_project_status(project.id)
    assert status["pending_approval"] is not None
    assert status["pending_approval"]["type"] == "confirmation"


def test_build_confirmation_question_per_task_type(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, None, renderer)

    feature = type("Project", (), {
        "task_type": "feature",
        "id": "proj_feature",
    })()
    bug_fix = type("Project", (), {
        "task_type": "bug_fix",
        "id": "proj_bug",
    })()
    documentation = type("Project", (), {
        "task_type": "documentation",
        "id": "proj_doc",
    })()

    question, options = oc._build_confirmation_question(feature, "REQUIREMENT_CONFIRM_PENDING")
    assert question == "请确认需求理解"
    assert any(o["action"] == "confirm" for o in options)
    assert any(o["action"] == "reject" for o in options)

    question, options = oc._build_confirmation_question(bug_fix, "BUG_FIX_CONFIRM_PENDING")
    assert question == "请确认修复方案"
    actions = [o["action"] for o in options]
    assert "confirm" in actions
    assert "reject" in actions

    question, options = oc._build_confirmation_question(documentation, "OUTLINE_CONFIRM_PENDING")
    assert question == "请确认文档大纲"
    actions = [o["action"] for o in options]
    assert "confirm" in actions
    assert "reject" in actions


def test_build_acceptance_options_per_task_type(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, None, renderer)

    feature = type("Project", (), {"task_type": "feature", "id": "proj_feature"})()
    bug_fix = type("Project", (), {"task_type": "bug_fix", "id": "proj_bug"})()
    documentation = type("Project", (), {"task_type": "documentation", "id": "proj_doc"})()

    _, options = oc._build_confirmation_question(feature, "ACCEPTANCE_PENDING")
    actions = [o["action"] for o in options]
    assert "reject_to_requirements" in actions
    assert "reject_to_architecture" in actions
    assert "reject_to_specs" in actions

    _, options = oc._build_confirmation_question(bug_fix, "ACCEPTANCE_PENDING")
    actions = [o["action"] for o in options]
    assert "reject_to_analysis" in actions
    assert "reject_to_requirements" not in actions

    _, options = oc._build_confirmation_question(documentation, "ACCEPTANCE_PENDING")
    actions = [o["action"] for o in options]
    assert "reject_to_outline" in actions
    assert "reject_to_requirements" not in actions


@pytest.mark.asyncio
async def test_agent_failure_persists_retry_count(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": False,
            "next_state_event": "fail",
            "logs": "fake failure",
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    await oc.tick(project.id)

    # Fake runtime returns failure; orchestrator should increment retry count
    # and stay in PLANNING_REQUIREMENTS.
    updated = service.get_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS"
    assert updated.context.get("retry_counts", {}).get("PLANNING_REQUIREMENTS") == 1


@pytest.mark.asyncio
async def test_concurrent_ticks_serialized(orchestrator):
    service, wm, renderer = orchestrator

    call_count = 0

    class CountingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            nonlocal call_count
            call_count += 1
            return await super().execute(**kwargs)

    runtime = CountingRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        }
    })

    lock_manager = ProjectLockManager()
    oc = AgentOrchestrator(service, wm, runtime, renderer, lock_manager=lock_manager)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="normal",
    )

    await asyncio.gather(oc.tick(project.id), oc.tick(project.id))

    updated = service.get_project(project.id)
    # Lock serialized the two ticks so the planner only ran once.
    assert call_count == 1
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_strict_mode_requires_bug_fix_confirmation(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "port validation bug",
            "root_cause": "missing range check",
            "fix_strategy": "add validation",
            "affected_files": ["src/validator.py"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="strict",
    )

    await oc.tick(project.id)
    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "BUG_FIX_CONFIRM_PENDING"
    status = service.get_project_status(project.id)
    assert status["pending_approval"] is not None


@pytest.mark.asyncio
async def test_night_mode_auto_confirms_bug_fix_confirmation(orchestrator, monkeypatch):
    service, wm, renderer = orchestrator

    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: True)
    )

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "port validation bug",
            "root_cause": "missing range check",
            "fix_strategy": "add validation",
            "affected_files": ["src/validator.py"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )

    # BUG_ANALYSIS -> BUG_FIX_CONFIRM_PENDING -> CODING -> TESTING -> REVIEW -> ACCEPTANCE_PENDING
    for _ in range(5):
        await oc.tick(project.id)

    updated = service.get_project(project.id)
    # Night mode only requires REQUIREMENT_CONFIRM_PENDING and ACCEPTANCE_PENDING,
    # so BUG_FIX_CONFIRM_PENDING is auto-confirmed and the flow reaches ACCEPTANCE_PENDING.
    assert updated.current_state == "ACCEPTANCE_PENDING"


@pytest.mark.asyncio
async def test_auto_mode_uses_project_manager_for_confirmation(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        },
        "project_manager": {
            "success": True,
            "next_state_event": "complete",
            "reply": "需求理解有误，建议重新分析。",
            "suggested_action": "reject",
        },
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    await oc.tick(project.id)
    await oc.tick(project.id)

    updated = service.get_project(project.id)
    # project_manager suggested "reject", so we should be back at PLANNING_REQUIREMENTS.
    assert updated.current_state == "PLANNING_REQUIREMENTS"


@pytest.mark.asyncio
async def test_restart_with_type_at_acceptance(orchestrator, monkeypatch):
    service, wm, renderer = orchestrator

    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: True)
    )

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "port validation bug",
            "root_cause": "missing range check",
            "fix_strategy": "add validation",
            "affected_files": ["src/validator.py"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )

    # Advance to ACCEPTANCE_PENDING.
    for _ in range(5):
        await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "ACCEPTANCE_PENDING"

    new_project = await service.restart_project_with_type(
        project.id, "feature", triggered_by="user"
    )
    await oc.tick(new_project.id)

    assert service.get_project(project.id).status == "cancelled"
    assert new_project.task_type == "feature"
    assert new_project.description == project.description
    assert service.get_project(new_project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_auxiliary_agent_task_is_completed(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "project_manager": {
            "success": True,
            "next_state_event": "complete",
            "suggested_action": "confirm",
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="normal",
    )

    output = await oc._execute_auxiliary_agent(
        project,
        agent_type="project_manager",
        input_data={"mode": "auto_confirmation", "state": project.current_state},
    )

    assert output is not None
    assert output["suggested_action"] == "confirm"

    # The auxiliary task should be persisted as completed, not left running.
    tasks = service.session.exec(
        select(Task).where(Task.project_id == project.id)
    ).all()
    assert len(tasks) == 1
    assert tasks[0].agent_type == "project_manager"
    assert tasks[0].status == "completed"
    assert tasks[0].output_data["suggested_action"] == "confirm"
