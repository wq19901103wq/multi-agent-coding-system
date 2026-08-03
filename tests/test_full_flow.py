"""Simulated full feature/bug-fix/documentation flows using FakeRuntime.

These tests exercise the complete state machine and all major branches
(confirm, reject/rework, scope-change discard, pause/resume) without calling
real LLM APIs.
"""

from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import FakeRuntime
from mac_system.config import get_agent_config
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Approval, Project, Task
from mac_system.state_machine import StateMachine


@pytest.fixture
def session():
    get_agent_config.cache_clear()
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def base_orchestrator(session):
    service = ProjectService(session)
    worktree_manager = WorktreeManager(Path(service.settings.projects_dir))
    renderer = PromptRenderer(get_agent_config())
    return service, worktree_manager, renderer


def _make_runtime(extra: dict | None = None) -> FakeRuntime:
    outputs = {
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "plan summary",
            "goals": ["实现功能"],
            "requirements": [
                {
                    "id": "REQ-001",
                    "title": "核心功能",
                    "description": "实现核心功能",
                    "priority": "must",
                    "kind": "functional",
                    "source": "用户",
                    "status": "confirmed",
                }
            ],
            "acceptance_criteria": [
                {
                    "id": "AC-001",
                    "requirement_ids": ["REQ-001"],
                    "statement": "功能可用",
                    "verification_method": "automated_test",
                    "evidence_source": "system_observable",
                }
            ],
            "tech_stack": ["Python"],
            "modules": [
                {"name": "main", "responsibility": "入口", "files": ["src/main.py"]}
            ],
            "decisions": [
                {
                    "id": "DEC-001",
                    "topic": "实现方式",
                    "options": [
                        {"key": "a", "label": "方案 A"},
                        {"key": "b", "label": "方案 B"},
                    ],
                    "recommended": "a",
                    "reason": "简单",
                    "affected_requirement_ids": ["REQ-001"],
                    "affected_acceptance_criterion_ids": ["AC-001"],
                }
            ],
            "risks": [],
            "dependencies": [],
            "invariants": ["不要丢失数据"],
            "failure_modes": [
                {"scenario": "异常", "detection": "日志", "recovery": "重试"}
            ],
            "specs": [
                {
                    "id": "SPEC-001",
                    "title": "实现核心功能",
                    "description": "按需求实现",
                    "requirement_ids": ["REQ-001"],
                    "acceptance_criterion_ids": ["AC-001"],
                    "decision_ids": ["DEC-001"],
                    "acceptance_criteria": ["功能可用"],
                    "dependencies": [],
                    "owners": {
                        "implementation": "coder",
                        "verification": "tester",
                        "review": "reviewer",
                    },
                    "test_cases": [
                        {
                            "id": "TC-001",
                            "type": "unit",
                            "scenario": "正常输入",
                            "expected": "通过",
                            "acceptance_criterion_ids": ["AC-001"],
                        }
                    ],
                    "definition_of_done": ["测试通过"],
                }
            ],
        },
        "coder": {
            "success": True,
            "next_state_event": "complete",
            "modified_files": [
                {"path": "src/main.py", "description": "实现功能"},
                {"path": "tests/test_main.py", "description": "测试"},
            ],
        },
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
            "failure_class": "none",
            "recommended_rework_target": "none",
            "test_files": ["tests/test_main.py"],
            "test_evidence": [
                {
                    "id": "TE-001",
                    "spec_id": "SPEC-001",
                    "acceptance_criterion_ids": ["AC-001"],
                    "type": "unit",
                    "command_or_method": "pytest",
                    "result": "pass",
                }
            ],
            "uncovered_acceptance_criterion_ids": [],
            "environment": {},
            "coverage": 1.0,
            "failures": [],
            "report": "全部通过",
            "notes": [],
        },
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "issues": [],
            "recommended_rework_target": "none",
            "verified_feedback_ids": [],
            "requirement_coverage": [
                {
                    "acceptance_criterion_id": "AC-001",
                    "status": "covered",
                    "evidence": ["tests/test_main.py"],
                }
            ],
            "stale_artifact_ids": [],
            "summary": "Review 通过",
            "notes": [],
        },
        "acceptance_agent": {
            "success": True,
            "result": "pass",
            "summary": "真实入口黑盒验收通过",
            "criteria": [{
                "acceptance_criterion_id": "AC-001",
                "evidence_source": "system_observable",
                "result": "pass",
                "method": "从公开入口执行核心功能",
                "expected": "功能可用",
                "actual": "功能按预期返回",
                "evidence": ["blackbox:core-flow:pass"],
                "limitations": [],
            }],
            "user_input_requests": [],
            "environment": {
                "entry_points": ["local://fake-entry"],
                "started_services": [],
                "artifacts": [],
            },
        },
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "验收通过",
            "evidence": {
                "tests_passed": True,
                "coverage": 1.0,
                "review_result": "pass",
                "meets_requirements": True,
            },
            "acceptance_matrix": [
                {
                    "acceptance_criterion_id": "AC-001",
                    "requirement_ids": ["REQ-001"],
                    "spec_ids": ["SPEC-001"],
                    "implementation_evidence": ["src/main.py"],
                    "test_evidence_ids": ["TE-001"],
                    "review_result": "pass",
                    "result": "pass",
                }
            ],
            "blocking_conditions": [],
            "artifact_versions": {"requirements": 1, "specs": 1, "coder_output": 1},
            "recommendation": {"action": "confirm", "reason": "通过"},
        },
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "阶段产物覆盖当前合同且没有未解决 blocker",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
        },
    }
    if extra:
        outputs.update(extra)
    return FakeRuntime(outputs)


@pytest.mark.asyncio
async def test_normal_ai_first_flow_completes_without_user_approval(base_orchestrator):
    service, wm, renderer = base_orchestrator
    oc = AgentOrchestrator(service, wm, _make_runtime(), renderer)
    project = await service.create_project(
        name="ai-first-flow",
        task_type="feature",
        description="实现一个简单功能",
        approval_mode="normal",
    )

    for _ in range(20):
        await oc.tick(project.id)
        project = service.require_project(project.id)
        if project.current_state in {"COMPLETED", "FAILED", "CANCELLED"}:
            break

    assert project.current_state == "COMPLETED"
    approvals = service.session.exec(
        select(Approval).where(Approval.project_id == project.id)
    ).all()
    assert approvals
    assert all(item.responded_by != "test_user" for item in approvals)
    assert service.get_project_status(project.id)["pending_approval"] is None


async def _resolve_pending(service: ProjectService, project: Project, action: str, payload: dict | None = None):
    """Resolve the unique pending approval for ``project`` with ``action``."""
    approval = service.session.exec(
        select(Approval)
        .where(Approval.project_id == project.id, Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    ).first()
    assert approval is not None, f"No pending approval in {project.current_state}"
    await service.resolve_approval(approval.id, action, responded_by="test_user", payload=payload)


async def _tick_until_pending(oc: AgentOrchestrator, service: ProjectService, project_id: str, max_ticks: int = 10):
    for _ in range(max_ticks):
        await oc.tick(project_id)
        project = service.require_project(project_id)
        pending = service.session.exec(
            select(Approval)
            .where(Approval.project_id == project_id, Approval.status == "pending")
        ).first()
        if pending is not None:
            return project, pending
        if project.current_state in ("COMPLETED", "FAILED", "CANCELLED"):
            return project, None
    raise RuntimeError("No pending approval reached")


@pytest.mark.asyncio
async def test_full_feature_flow_confirm_all(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="feature-flow",
        task_type="feature",
        description="实现一个简单功能",
        approval_mode="strict",
    )

    # PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # Confirm requirements
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # ARCHITECTURE_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ARCHITECTURE_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # SPEC_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # CODING runs automatically, then CODING_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # TESTING runs automatically, then TESTING_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "TESTING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # REVIEW runs automatically, then REVIEW_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REVIEW_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # Strict mode retains the explicit final acceptance boundary.
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ACCEPTANCE_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "COMPLETED"
    assert service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_agent",
            Task.status == "completed",
        )
    ).first() is not None


@pytest.mark.asyncio
async def test_full_feature_flow_reject_requirements(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="feature-reject",
        task_type="feature",
        description="实现一个简单功能",
        approval_mode="strict",
    )

    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # Reject and provide feedback
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="REQUIREMENT_CONFIRM_PENDING",
        source_task_id=None,
        source_round=1,
        kind="human_rejection",
        summary="需求理解有误",
        blocking=True,
        failure_class="human_rejection",
        user_comment="需求理解有误",
        commit=False,
    )
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)

    # Reject loops back to PLANNING_REQUIREMENTS and the planner re-runs,
    # ending up in REQUIREMENT_CONFIRM_PENDING again with a higher round.
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # New planner task should have round_number > 1
    tasks = service.session.exec(
        select(Task)
        .where(Task.project_id == project.id, Task.state == "PLANNING_REQUIREMENTS")
        .order_by(Task.created_at.desc())
    ).all()
    assert len(tasks) >= 2
    assert tasks[0].round_number == 2


@pytest.mark.asyncio
async def test_full_feature_flow_rework_from_acceptance(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="feature-rework",
        task_type="feature",
        description="实现一个简单功能",
        approval_mode="strict",
    )

    # Advance to ACCEPTANCE_PENDING
    for expected in (
        "REQUIREMENT_CONFIRM_PENDING",
        "ARCHITECTURE_CONFIRM_PENDING",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
        "CODING_CONFIRM_PENDING",
        "TESTING_CONFIRM_PENDING",
        "REVIEW_CONFIRM_PENDING",
    ):
        project, _ = await _tick_until_pending(oc, service, project.id)
        assert project.current_state == expected
        await _resolve_pending(service, project, "confirm")
        await oc.tick(project.id)

    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ACCEPTANCE_PENDING"

    # Reject acceptance and choose to go back to coding
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)

    project = service.require_project(project.id)
    assert project.current_state == "REWORK"

    await _resolve_pending(service, project, "to_coding")
    await oc.tick(project.id)

    # REWORK -> CODING runs the coder immediately and lands in CODING_CONFIRM_PENDING.
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_pause_and_resume(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="feature-pause",
        task_type="feature",
        description="实现一个简单功能",
        approval_mode="strict",
    )

    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # Pause and resume should not lose the pending approval
    await service.pause_project(project.id)
    project = service.require_project(project.id)
    assert project.status == "paused"

    await service.resume_project(project.id)
    project = service.require_project(project.id)
    assert project.status == "active"
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_bug_fix_flow_confirm(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="bug-fix-flow",
        task_type="bug_fix",
        description="修复端口校验 bug",
        approval_mode="strict",
    )

    # BUG_ANALYSIS -> BUG_FIX_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "BUG_FIX_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # CODING_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # TESTING_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "TESTING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # REVIEW_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REVIEW_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # Strict mode retains the explicit final acceptance boundary.
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ACCEPTANCE_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_full_documentation_flow_confirm(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    project = await service.create_project(
        name="doc-flow",
        task_type="documentation",
        description="写一份 AGENTS.md",
        approval_mode="strict",
    )

    # DOC_OUTLINE -> OUTLINE_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "OUTLINE_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # CODING_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # REVIEW_CONFIRM_PENDING
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REVIEW_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)

    # Strict mode retains the explicit final acceptance boundary.
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ACCEPTANCE_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "COMPLETED"


@pytest.mark.asyncio
async def test_full_feature_flow_reject_architecture(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="reject-arch", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ARCHITECTURE_CONFIRM_PENDING"
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ARCHITECTURE_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_reject_specs(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="reject-specs", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    for exp in ("REQUIREMENT_CONFIRM_PENDING", "ARCHITECTURE_CONFIRM_PENDING"):
        project, _ = await _tick_until_pending(oc, service, project.id)
        assert project.current_state == exp
        await _resolve_pending(service, project, "confirm")
        await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_reject_coding_quality_gate(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="reject-coding", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    for exp in ("REQUIREMENT_CONFIRM_PENDING", "ARCHITECTURE_CONFIRM_PENDING",
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING"):
        project, _ = await _tick_until_pending(oc, service, project.id)
        assert project.current_state == exp
        await _resolve_pending(service, project, "confirm")
        await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "REWORK"
    await _resolve_pending(service, project, "to_coding")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_reject_testing_quality_gate(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="reject-test", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    for exp in ("REQUIREMENT_CONFIRM_PENDING", "ARCHITECTURE_CONFIRM_PENDING",
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING", "CODING_CONFIRM_PENDING"):
        project, _ = await _tick_until_pending(oc, service, project.id)
        assert project.current_state == exp
        await _resolve_pending(service, project, "confirm")
        await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "TESTING_CONFIRM_PENDING"
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "REWORK"
    await _resolve_pending(service, project, "to_testing")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "TESTING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_reject_review_quality_gate(base_orchestrator):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="reject-review", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    for exp in ("REQUIREMENT_CONFIRM_PENDING", "ARCHITECTURE_CONFIRM_PENDING",
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING", "CODING_CONFIRM_PENDING",
                "TESTING_CONFIRM_PENDING"):
        project, _ = await _tick_until_pending(oc, service, project.id)
        assert project.current_state == exp
        await _resolve_pending(service, project, "confirm")
        await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REVIEW_CONFIRM_PENDING"
    await _resolve_pending(service, project, "reject")
    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "REWORK"
    await _resolve_pending(service, project, "to_coding")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "CODING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_full_feature_flow_architecture_confirmation_has_no_option_selection(
    base_orchestrator,
):
    service, wm, renderer = base_orchestrator
    runtime = _make_runtime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="select-opt", task_type="feature",
        description="实现一个简单功能", approval_mode="strict",
    )
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "ARCHITECTURE_CONFIRM_PENDING"
    state_machine = StateMachine("feature")
    assert set(state_machine.available_events(project.current_state)) == {"confirm", "reject"}
    await _resolve_pending(service, project, "confirm")
    await oc.tick(project.id)
    project, _ = await _tick_until_pending(oc, service, project.id)
    assert project.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
