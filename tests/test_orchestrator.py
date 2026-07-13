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
async def test_tick_skips_when_task_already_running(orchestrator):
    """_execute_agent must not start a new agent when a running task exists."""
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
            "summary": "test",
            "goals": [],
            "acceptance_criteria": [],
        }
    })

    lock_manager = ProjectLockManager()
    oc = AgentOrchestrator(service, wm, runtime, renderer, lock_manager=lock_manager)
    project = await service.create_project(
        name="dup-test",
        task_type="feature",
        description="test",
        approval_mode="auto",
    )

    # Before ticking, manually insert a running task so _execute_agent
    # sees it and skips starting a duplicate agent.
    existing = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={},
    )
    await service.start_task(existing.id)

    # Now tick — it should detect the running task and skip agent execution.
    await oc.tick(project.id)

    # The runtime must NOT have been called.
    assert call_count == 0

    # The existing task must still be running (tick did not touch it).
    tasks = service.session.exec(select(Task).where(Task.project_id == project.id)).all()
    assert len(tasks) == 1
    assert tasks[0].id == existing.id
    assert tasks[0].status == "running"


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
async def test_agent_transitions_failed_on_last_allowed_attempt(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": False,
            "next_state_event": "fail",
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    oc.settings.max_agent_retries = 3
    project = await service.create_project(
        name="retry-exhaustion",
        task_type="feature",
        description="always fails",
        approval_mode="normal",
    )

    await oc.tick(project.id)
    assert service.get_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    await oc.tick(project.id)
    assert service.get_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "FAILED"
    assert updated.status == "failed"
    assert not service._running_tasks(project.id)


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


@pytest.mark.asyncio
async def test_tester_fail_creates_feedback_packet_and_goes_to_rework(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": False,
            "report": "Add command is broken",
            "failure_class": "implementation_bug",
            "recommended_rework_target": "coding",
            "failures": [{"test": "test_add", "error": "assertion failed"}],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 Todo CLI",
        approval_mode="auto",
    )
    project.current_state = "TESTING"
    service.session.commit()

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REWORK"
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].source_stage == "TESTING"
    assert feedback[0].kind == "quality_failure"
    assert feedback[0].recommended_rework_target == "coding"


@pytest.mark.asyncio
async def test_reviewer_fail_creates_feedback_packet_and_goes_to_rework(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "fail",
            "summary": "Missing error handling",
            "recommended_rework_target": "coding",
            "issues": [
                {
                    "id": "r1",
                    "severity": "major",
                    "category": "implementation",
                    "description": "No validation",
                    "blocking": True,
                }
            ],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 Todo CLI",
        approval_mode="auto",
    )
    project.current_state = "REVIEW"
    service.session.commit()

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REWORK"
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].source_stage == "REVIEW"


@pytest.mark.asyncio
async def test_pending_user_input_forces_quality_gate_to_rework(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
            "report": "All tests pass",
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 Todo CLI",
        approval_mode="auto",
    )
    project.current_state = "TESTING"
    service.session.commit()

    await service.create_pending_user_input(
        project_id=project.id,
        sender_id="user_1",
        observed_state="TESTING",
        content="这里需要改成支持多用户",
        kind="scope_change",
    )

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REWORK"
    feedback = service.get_open_feedback_packets(project.id)
    assert any(fb.kind == "scope_change" for fb in feedback)


@pytest.mark.asyncio
async def test_stage_round_number_increments_on_re_entry(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "requirements v2",
            "goals": ["g1"],
            "acceptance_criteria": ["a1"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 CLI",
        approval_mode="auto",
    )

    await oc.tick(project.id)
    first_task = service.session.exec(
        select(Task).where(Task.project_id == project.id)
    ).first()
    assert first_task.round_number == 1

    # Re-enter the same state and tick again.
    project.current_state = "PLANNING_REQUIREMENTS"
    service.session.commit()
    await oc.tick(project.id)

    tasks = service.session.exec(
        select(Task)
        .where(Task.project_id == project.id)
        .order_by(Task.created_at.asc())
    ).all()
    assert len(tasks) == 2
    assert tasks[1].round_number == 2


@pytest.mark.asyncio
async def test_pending_user_input_discards_planner_result(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "discarded plan",
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

    # Simulate a correction received while the planner is running.
    await service.create_pending_user_input(
        project_id=project.id,
        sender_id="ou_123",
        observed_state="PLANNING_REQUIREMENTS",
        content="需求不对，要改",
        kind="correction",
    )

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_INPUT_PENDING"

    # The planner artifact should NOT have been copied to the workspace.
    req_file = Path(updated.workspace_path) / ".mac" / "REQUIREMENTS.json"
    assert not req_file.exists()

    # A blocking feedback packet should have been created.
    open_feedback = service.get_open_feedback_packets(project.id)
    assert any(fb.kind == "correction" and fb.blocking for fb in open_feedback)


@pytest.mark.asyncio
async def test_pending_user_input_discards_coder_result(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "plan",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        },
        "coder": {
            "success": True,
            "next_state_event": "complete",
            "modified_files": [{"path": "src/main.py", "description": "add feature"}],
        },
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    # Advance to CODING. Auto mode confirms planning stages.
    await oc.tick(project.id)  # requirements
    await oc.tick(project.id)  # requirements confirm
    await oc.tick(project.id)  # architecture
    await oc.tick(project.id)  # architecture confirm
    await oc.tick(project.id)  # specs
    await oc.tick(project.id)  # specs confirm -> CODING

    project = service.get_project(project.id)
    assert project.current_state == "CODING"

    await service.create_pending_user_input(
        project_id=project.id,
        sender_id="ou_123",
        observed_state="CODING",
        content="这里写错了",
        kind="correction",
    )

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "CLARIFICATION_PENDING"


@pytest.mark.asyncio
async def test_resume_applies_paused_agent_result(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "paused plan",
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

    # Manually stash a paused agent result and set project paused.
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={},
    )
    await service.start_task(task.id, str(Path(project.workspace_path) / "worktrees" / "wt"))
    await service.complete_task(task.id, {"success": True, "next_state_event": "complete"})

    project = service.require_project(project.id)
    project.status = "paused"
    project.context = {
        **project.context,
        "paused_agent_result": {
            "task_id": task.id,
            "state": "PLANNING_REQUIREMENTS",
            "output": {"success": True, "next_state_event": "complete"},
            "worktree_path": str(Path(project.workspace_path) / "worktrees" / "wt"),
        },
    }
    service.session.commit()

    await service.resume_project(project.id)
    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert "paused_agent_result" not in updated.context
