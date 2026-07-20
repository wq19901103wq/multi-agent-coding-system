import asyncio
import json
import re
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntimeResult, FakeRuntime
from mac_system.config import get_agent_config
from mac_system.core.lock_manager import ProjectLockManager
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Approval, FeedbackPacket, Task
from mac_system.state_machine import StateMachine


def test_conversation_coordinator_prompt_examples_match_contract():
    """Prompt examples must not teach values rejected by the runtime schema."""
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "conversation_coordinator.md").read_text(
        encoding="utf-8"
    )
    schema = json.loads(
        (root / "specs" / "schemas" / "conversation_coordinator_output.json").read_text(
            encoding="utf-8"
        )
    )
    examples = [
        json.loads(block)
        for block in re.findall(r"```json\s*(\{.*?\})\s*```", prompt, re.DOTALL)
    ]
    assert examples

    properties = schema["properties"]
    top_level_allowed = set(properties)
    action_allowed = set(properties["proposed_action"]["properties"])
    for example in examples:
        assert set(example) <= top_level_allowed
        for field in ("intent", "effect", "commitment"):
            assert example[field] in properties[field]["enum"]
        action = example["proposed_action"]
        assert set(action) <= action_allowed
        assert action["type"] in properties["proposed_action"]["properties"]["type"]["enum"]
        if "option_key" in action:
            assert isinstance(action["option_key"], str)
        if action["type"] == "inspect_workspace":
            inspection = example["inspection"]
            assert inspection["files_read"]
            assert inspection["summary"]
            assert inspection["findings"]


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


def test_architecture_confirmation_only_allows_confirm_or_reject(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project_path = tmp_path / "architecture-options"
    mac_path = project_path / ".mac"
    mac_path.mkdir(parents=True)
    (mac_path / "ARCHITECTURE.json").write_text(
        json.dumps({
            "decisions": [{
                "id": "DEC-DB-001",
                "recommended": "sqlite",
                "options": [
                    {"key": "sqlite", "label": "SQLite"},
                    {"key": "postgres", "label": "PostgreSQL"},
                ],
            }]
        }),
        encoding="utf-8",
    )
    project = type("ProjectStub", (), {
        "id": "proj_options", "task_type": "feature",
        "workspace_path": str(project_path),
    })()

    _, options = oc._build_confirmation_question(
        project, "ARCHITECTURE_CONFIRM_PENDING"
    )
    assert [(opt["action"], opt["label"]) for opt in options] == [
        ("confirm", "确认"),
        ("reject", "驳回"),
    ]


@pytest.mark.asyncio
async def test_acceptance_coverage_violation_removes_confirm(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "complete",
            "result": "fail",
            "summary": "coverage incomplete",
            "recommendation": {"action": "reject_to_specs", "reason": "missing AC"},
            "evidence": [],
            "acceptance_matrix": [
                {"acceptance_criterion_id": "AC-001", "status": "pass", "evidence": []}
            ],
            "blocking_conditions": [],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="coverage", task_type="feature", description="Test", approval_mode="normal"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-001"}, {"id": "AC-002"}
            ]
        }),
        encoding="utf-8",
    )

    await oc.tick(project.id)
    from mac_system.models import Approval
    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.status == "pending",
        )
    ).first()
    assert approval is not None
    assert "confirm" not in {opt["action"] for opt in approval.payload["options"]}
    assert service.get_open_feedback_packets(project.id)[0].kind == "acceptance_rejection"


@pytest.mark.asyncio
async def test_acceptance_failed_matrix_entry_is_not_counted_as_covered(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="coverage-fail", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-001"}]}),
        encoding="utf-8",
    )

    violation = oc._check_acceptance_coverage(
        project,
        {
            "result": "pending_human_review",
            "action": "confirm",
            "blocking_conditions": [],
            "acceptance_matrix": [
                {"acceptance_criterion_id": "AC-001", "result": "fail"}
            ],
        },
    )

    assert violation is not None
    assert violation["missing_ids"] == ["AC-001"]


@pytest.mark.asyncio
async def test_tool_progress_uses_project_labeled_content_card(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator

    class ProgressBridge:
        def __init__(self):
            self.cards = []

        async def send_content_card(self, **kwargs):
            self.cards.append(kwargs)

    bridge = ProgressBridge()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
    monkeypatch.setattr(oc.settings, "debug_agent_progress", True)
    project = await service.create_project(
        name="目录绑定项目",
        task_type="feature",
        description="Test",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_group",
        feishu_chat_type="group",
    )
    task = type("TaskStub", (), {"agent_type": "coder"})()
    callback = oc._make_progress_callback(project, task)
    assert callback is not None
    await callback("tool", json.dumps({"name": "Read", "input": {"file": "a.py"}}))

    assert bridge.cards[0]["title"] == "[目录绑定项目] 编码 Agent · 正在执行"
    assert bridge.cards[0]["color"] == "purple"
    assert "读取文件" in bridge.cards[0]["content"]
    assert "a.py" in bridge.cards[0]["content"]


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
async def test_agent_failure_retries_automatically_and_persists_count(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": False,
            "next_state_event": "fail",
            "logs": "fake failure",
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    oc.settings.max_agent_retries = 2
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="auto",
    )

    await oc.tick(project.id)

    # One tick performs both allowed attempts, then waits for a user recovery
    # decision instead of requiring an external/manual tick.
    updated = service.get_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS_RECOVERY_PENDING"
    assert updated.context.get("retry_counts", {}).get("PLANNING_REQUIREMENTS") == 2
    tasks = service.session.exec(select(Task).where(Task.project_id == project.id)).all()
    assert len(tasks) == 2


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
        },
        "coder": {"success": True, "next_state_event": "complete"},
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
        },
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "issues": [],
        },
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
async def test_agent_transitions_to_recovery_on_last_allowed_attempt(orchestrator):
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

    updated = service.get_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS_RECOVERY_PENDING"
    assert updated.status == "active"
    assert updated.context["last_agent_failure"]["agent"] == "planner"
    assert not service._running_tasks(project.id)

    await service.transition_state(project.id, "retry", triggered_by="user")
    retried = service.get_project(project.id)
    assert retried.current_state == "PLANNING_REQUIREMENTS"
    assert retried.context["retry_counts"]["PLANNING_REQUIREMENTS"] == 0
    assert "last_agent_failure" not in retried.context


@pytest.mark.asyncio
async def test_quality_gate_retry_exhaustion_offers_recovery(orchestrator):
    """Runtime exhaustion is recoverable and is never confused with quality fail."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "tester": {
            "success": False,
            "next_state_event": "fail",
            "logs": "fake tester crash",
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    oc.settings.max_test_retries = 2
    project = await service.create_project(
        name="retry-exhaustion-tester",
        task_type="feature",
        description="always fails",
        approval_mode="normal",
    )
    project.current_state = "TESTING"
    project.context = {
        **project.context,
        "retry_counts": {"TESTING": oc.settings.max_test_retries},
    }
    service.session.commit()

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "TESTING_RECOVERY_PENDING"
    assert updated.status == "active"
    assert set(StateMachine("feature").available_events(updated.current_state)) == {
        "retry",
        "choose_rework",
        "cancel",
    }


@pytest.mark.asyncio
async def test_quality_outputs_are_persisted_for_bounded_review(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-evidence",
        task_type="feature",
        description="persist compact quality evidence",
        approval_mode="normal",
    )
    worktree = tmp_path / "review-evidence-worktree"
    (worktree / ".mac").mkdir(parents=True)
    tester_output = {
        "success": True,
        "next_state_event": "complete",
        "passed": True,
        "test_evidence": [],
    }

    oc._persist_stage_output(project, worktree, tester_output, "TESTING")

    persisted = Path(project.workspace_path) / ".mac" / "TESTER_OUTPUT.json"
    assert json.loads(persisted.read_text(encoding="utf-8")) == tester_output
    context_names = {
        path.name for path in oc._context_files_for_state(project, "REVIEW")
    }
    assert "TESTER_OUTPUT.json" in context_names


@pytest.mark.asyncio
async def test_acceptance_evaluator_can_ask_user_and_resume(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "acceptance-business-check",
                "questions": [{
                    "question_id": "q_business_result",
                    "kind": "blocking",
                    "question": "实际业务结果是否符合预期？",
                    "reason": "自动测试无法验证人工体验",
                    "allow_free_text": True,
                    "options": [],
                    "affected_trace_ids": ["AC-001"],
                }],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="acceptance-question",
        task_type="feature",
        description="ask user at acceptance",
        approval_mode="normal",
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()

    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state == "ACCEPTANCE_INPUT_PENDING"
    interaction = service.get_open_question_interaction(project.id)
    assert interaction is not None
    assert interaction.agent == "acceptance_evaluator"
    assert interaction.resume_state == "ACCEPTANCE_PENDING"


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
        },
        "coder": {"success": True, "next_state_event": "complete"},
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
        },
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "issues": [],
        },
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
async def test_tester_ask_user_is_not_converted_to_failure(orchestrator):
    """A tester asking for missing credentials must reach TESTING_INPUT_PENDING.

    Regression: the TESTING quality gate used to stomp any output with
    ``passed != True`` into a bogus quality_failure FeedbackPacket, so a
    legitimate ask_user (e.g. "请提供 LLM API Key") never reached the user.
    """
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "tester": {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "clarify_4",
                "questions": [{
                    "question_id": "q_llm_api_key", "kind": "blocking",
                    "question": "请提供 LLM API Key", "reason": "集成测试需要真实凭证",
                    "allow_free_text": True,
                    "options": [],
                    "affected_trace_ids": [],
                }],
            },
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
    assert updated.current_state == "TESTING_INPUT_PENDING"
    assert service.get_open_feedback_packets(project.id) == []
    interaction_id = updated.context.get("_pending_question_interaction_id")
    assert interaction_id is not None
    questions = service.get_questions_for_interaction(interaction_id)
    assert [q.question_id for q in questions] == ["q_llm_api_key"]


@pytest.mark.asyncio
async def test_rework_approval_marks_recommended_option_primary(orchestrator):
    """REWORK approval options must expose option_key + primary recommendation.

    The Feishu card and the E2E auto-takeover helper both read
    ``pending_approval.options[*]`` to find the recommended rollback target.
    Without this enrichment the helper falls back to a static mapping and can
    pick a target that sends the project back to the same failing stage
    (observed in agent_fail_auto_rework E2E: tester kept re-running unchanged
    code because ``to_testing`` was picked instead of ``to_coding``).
    """
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
    # Drive one more tick so the REWORK entry runs and creates the approval.
    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REWORK"

    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.state == "REWORK",
            Approval.status == "pending",
        )
    ).first()
    assert approval is not None, "REWORK approval was not created"
    options = approval.payload["options"]
    assert options, "REWORK approval has no options"

    # Every option must expose option_key so helpers can match it.
    for opt in options:
        assert opt.get("option_key") == opt.get("action"), (
            f"option missing option_key: {opt}"
        )

    primary = [opt for opt in options if opt.get("type") == "primary"]
    assert len(primary) == 1, (
        f"expected exactly one primary option, got {len(primary)}: {options}"
    )
    assert primary[0]["action"] == "to_coding"
    assert primary[0]["option_key"] == "to_coding"


@pytest.mark.asyncio
async def test_rework_approval_without_recommendation_marks_nothing_primary(orchestrator):
    """If the failing agent gave no recommendation, no option is primary."""
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": False,
            "report": "Tests failed but cause unclear",
            "failure_class": "environment_failure",
            "recommended_rework_target": "none",
            "failures": [{"test": "test_env", "error": "db unavailable"}],
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
    await oc.tick(project.id)

    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.state == "REWORK",
            Approval.status == "pending",
        )
    ).first()
    assert approval is not None
    options = approval.payload["options"]
    primary = [opt for opt in options if opt.get("type") == "primary"]
    assert primary == [], (
        f"no option should be primary when recommendation is 'none': {options}"
    )
    # option_key is still present for helper compatibility.
    for opt in options:
        assert opt.get("option_key") == opt.get("action")


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
async def test_reviewer_explicit_fail_without_issues_forces_rework(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "fail",
            "issues": [],
            "summary": "Reviewer explicitly failed the change",
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="review-fail", task_type="feature", description="Test", approval_mode="auto"
    )
    project.current_state = "REVIEW"
    service.session.commit()

    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "REWORK"
    feedback = service.get_open_feedback_packets(project.id)
    assert any(item.source_stage == "REVIEW" for item in feedback)


@pytest.mark.asyncio
async def test_feedback_cannot_be_resolved_by_another_project(orchestrator):
    service, _, _ = orchestrator
    project_a = await service.create_project(
        name="a", task_type="feature", description="A"
    )
    project_b = await service.create_project(
        name="b", task_type="feature", description="B"
    )
    task_b = await service.create_task(
        project_id=project_b.id,
        agent_type="reviewer",
        state="REVIEW",
        input_data={},
    )
    packet = await service.create_feedback_packet(
        project_id=project_a.id,
        source_stage="REVIEW",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="A feedback",
    )

    with pytest.raises(ValueError, match="does not belong"):
        await service.resolve_feedback_packet(
            packet.id,
            resolved_by_task_id=task_b.id,
            project_id=project_b.id,
        )

    assert service.session.get(FeedbackPacket, packet.id).status == "open"


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


def test_planner_artifact_validation_rejects_invalid_json(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "REQUIREMENTS.json").write_text(
        '{"artifact_id":"requirements", invalid}', encoding="utf-8"
    )

    error = oc._planner_artifact_error(tmp_path, "PLANNING_REQUIREMENTS")

    assert error is not None
    assert "not valid JSON" in error


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
    await oc.tick(project.id)  # requirements confirm -> architecture
    await oc.tick(project.id)  # architecture
    await oc.tick(project.id)  # architecture confirm -> specs
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


# ---------------------------------------------------------------------------
# Unified ask_user / QuestionInteraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_batch_creates_interaction(orchestrator, tmp_path):
    """Agent outputs question_batch -> QuestionInteraction created, state -> INPUT_PENDING."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "req_clarify_1",
                "questions": [{
                    "question_id": "q_scope", "kind": "blocking",
                    "question": "范围？", "reason": "笼统",
                    "allow_free_text": False,
                    "options": [
                        {"key": "full", "label": "全部", "impact": "大", "recommended": True},
                        {"key": "core", "label": "核心", "impact": "小", "recommended": False},
                    ],
                    "affected_trace_ids": [],
                }],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="qa-test", task_type="feature", description="test", approval_mode="normal",
    )
    await oc.tick(project.id)
    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_INPUT_PENDING"
    interaction_id = project.context.get("_pending_question_interaction_id")
    assert interaction_id is not None
    interaction = service.get_question_interaction(interaction_id)
    assert interaction is not None
    assert interaction.status == "pending"
    assert interaction.resume_state == "PLANNING_REQUIREMENTS"
    questions = service.get_questions_for_interaction(interaction_id)
    assert len(questions) == 1
    assert questions[0].question_id == "q_scope"
    assert questions[0].recommended_option == "full"


@pytest.mark.asyncio
async def test_question_batch_suppresses_confirmed_decision_and_keeps_delta(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "req_delta_1",
                "questions": [
                    {
                        "question_id": "q_agent_scope",
                        "decision_key": "agent.capability_scope",
                        "kind": "blocking",
                        "question": "Agent 是仅简历问答还是开放智能体？",
                        "reason": "确定产品定位",
                        "allow_free_text": False,
                        "options": [
                            {"key": "open", "label": "开放", "impact": "能力完整", "recommended": True}
                        ],
                        "affected_trace_ids": [],
                    },
                    {
                        "question_id": "q_sandbox_language",
                        "decision_key": "sandbox.languages",
                        "kind": "decision",
                        "question": "代码沙箱支持哪些语言？",
                        "reason": "确定运行时范围",
                        "allow_free_text": False,
                        "options": [
                            {"key": "python", "label": "Python", "impact": "实现简单", "recommended": True}
                        ],
                        "affected_trace_ids": [],
                    },
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="confirmed-brief",
        task_type="feature",
        description="个人数字分身，具备开放智能体能力",
        approval_mode="normal",
        initial_context={
            "confirmed_brief": {
                "decisions": [{
                    "key": "agent.capability_scope",
                    "question": "Agent 的能力范围是什么？",
                    "answer": "个人数字分身，同时具备开放智能体能力",
                    "status": "confirmed",
                }]
            }
        },
    )

    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state == "REQUIREMENT_INPUT_PENDING"
    interaction = service.get_open_question_interaction(project.id)
    questions = service.get_questions_for_interaction(interaction.id)
    assert [question.question_id for question in questions] == ["q_sandbox_language"]
    task = service.session.exec(
        select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())
    ).first()
    assert len(task.output_data["question_batch"]["questions"]) == 1



@pytest.mark.asyncio
async def test_tick_recovers_completed_but_unapplied_question_batch(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="recover-unapplied", task_type="feature", description="test",
        approval_mode="normal",
    )
    task = await service.create_task(
        project.id, "planner", "PLANNING_REQUIREMENTS", {"description": "test"}, 1
    )
    worktree = tmp_path / "recover-worktree"
    worktree.mkdir()
    await service.start_task(task.id, str(worktree))
    await service.complete_task(task.id, {
        "success": True,
        "next_state_event": "ask_user",
        "question_batch": {
            "batch_key": "recover_batch",
            "questions": [{
                "question_id": "q_recover", "kind": "blocking",
                "question": "恢复后选择？", "reason": "验证崩溃恢复",
                "allow_free_text": False,
                "options": [
                    {"key": "yes", "label": "继续", "impact": "继续", "recommended": True}
                ],
                "affected_trace_ids": [],
            }],
        },
    }, None)

    assert project.id in service.reconcile_project_invariants()
    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state == "REQUIREMENT_INPUT_PENDING"
    interaction = service.get_open_question_interaction(project.id)
    assert interaction is not None
    tasks = service.session.exec(select(Task).where(Task.project_id == project.id)).all()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_question_batch_sends_card_immediately(orchestrator, tmp_path):
    """Agent outputs question_batch -> choice card + separate free-text cards are sent."""
    service, wm, renderer = orchestrator

    class CardBridge:
        def __init__(self):
            self.cards = []
            self.updated = []

        async def _send_interactive(self, receive_id, card, receive_id_type=None):
            self.cards.append((receive_id, card, receive_id_type))
            return f"msg_id_{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updated.append((message_id, card))

    bridge = CardBridge()
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "req_clarify_card",
                "questions": [
                    {
                        "question_id": "q_card", "kind": "blocking",
                        "question": "需要卡片吗？", "reason": "验证",
                        "allow_free_text": False,
                        "options": [
                            {"key": "yes", "label": "是", "impact": "无", "recommended": True},
                        ],
                        "affected_trace_ids": [],
                    },
                    {
                        "question_id": "q_why", "kind": "blocking",
                        "question": "为什么需要？", "reason": "了解动机",
                        "allow_free_text": True,
                        "options": [],
                        "affected_trace_ids": [],
                    },
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer, feishu_bridge=bridge)
    project = await service.create_project(
        name="card-test", task_type="feature", description="test", approval_mode="normal",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_card_group",
        feishu_chat_type="group",
    )
    await oc.tick(project.id)

    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_INPUT_PENDING"
    # state-transition card + choice batch card. Free-text card is NOT sent
    # until the choice questions are submitted.
    assert len(bridge.cards) == 2

    # First interactive card is the state-transition notification.
    receive_id, transition_card, receive_id_type = bridge.cards[0]
    assert receive_id == "oc_card_group"
    assert receive_id_type == "chat_id"
    assert "状态流转" in transition_card["header"]["title"]["content"]

    # Second card is the choice batch card.
    receive_id, batch_card, receive_id_type = bridge.cards[1]
    assert receive_id == "oc_card_group"
    assert receive_id_type == "chat_id"
    header = batch_card["header"]["title"]["content"]
    assert "需要确认 1 个问题" in header
    assert any(
        "**需要卡片吗？** （阻塞）" in el.get("text", {}).get("content", "")
        for el in batch_card["elements"]
    )
    assert any(
        "推荐：是" in el.get("text", {}).get("content", "")
        for el in batch_card["elements"]
    )

    def _find_option_button(card):
        for el in card["elements"]:
            if el.get("tag") == "action":
                for action in el.get("actions", []):
                    if action.get("tag") == "button" and action.get("value", {}).get("action") == "select_question_option":
                        return True
        return False

    assert _find_option_button(batch_card)
    assert any(
        action.get("value", {}).get("action") == "submit_selected_options"
        for el in batch_card["elements"]
        if el.get("tag") == "action"
        for action in el.get("actions", [])
    )

    # Approval payload records the choice batch card but not free-text yet.
    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.status == "pending",
            Approval.approval_type == "input",
        )
    ).first()
    assert approval.payload.get("question_batch_message_id") == "msg_id_2"
    assert approval.payload.get("selected_options") == {}
    assert approval.payload.get("reply_message_to_question", {}) == {}


@pytest.mark.asyncio
async def test_question_batch_answer_resolves_and_resumes(orchestrator, tmp_path):
    """Answering all blocking questions resolves the interaction and resumes the agent."""
    service, wm, renderer = orchestrator
    call_count = {"planner": 0}

    class TwoPhaseRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            call_count["planner"] += 1
            if call_count["planner"] == 1:
                return AgentRuntimeResult(
                    success=True,
                    output_json={
                        "success": True, "next_state_event": "ask_user",
                        "question_batch": {
                            "batch_key": "b1",
                            "questions": [{
                                "question_id": "q1", "kind": "blocking",
                                "question": "Q1", "reason": "r",
                                "allow_free_text": False,
                                "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                                "affected_trace_ids": [],
                            }],
                        },
                    },
                    output_files=[], logs="", cost_info=None,
                )
            return AgentRuntimeResult(
                success=True,
                output_json={"success": True, "next_state_event": "complete",
                             "summary": "done", "goals": [], "acceptance_criteria": []},
                output_files=[], logs="", cost_info=None,
            )

    runtime = TwoPhaseRuntime({})
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="qa-resume", task_type="feature", description="test", approval_mode="normal",
    )
    await oc.tick(project.id)
    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_INPUT_PENDING"
    interaction_id = project.context.get("_pending_question_interaction_id")
    questions = service.get_questions_for_interaction(interaction_id)
    await service.submit_question_answer(questions[0].id, "a", "A", False, "ou_user")
    assert service.interaction_is_resolvable(interaction_id) is True
    await service.resolve_question_interaction(interaction_id)
    await service.transition_state(
        project.id, "answer",
        payload={"resume_state": "PLANNING_REQUIREMENTS"},
        triggered_by="user",
    )
    project = service.require_project(project.id)
    assert project.current_state == "PLANNING_REQUIREMENTS"
    await oc.tick(project.id)
    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_question_batch_partial_answer_not_resolvable(orchestrator, tmp_path):
    """A partially-answered interaction with pending blocking questions is not resolvable."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True, "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "b1",
                "questions": [
                    {"question_id": "q1", "kind": "blocking", "question": "Q1",
                     "reason": "", "allow_free_text": False,
                     "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                     "affected_trace_ids": []},
                    {"question_id": "q2", "kind": "blocking", "question": "Q2",
                     "reason": "", "allow_free_text": False,
                     "options": [{"key": "x", "label": "X", "impact": "", "recommended": True}],
                     "affected_trace_ids": []},
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="qa-partial", task_type="feature", description="test", approval_mode="normal",
    )
    await oc.tick(project.id)
    project = service.require_project(project.id)
    interaction_id = project.context.get("_pending_question_interaction_id")
    questions = service.get_questions_for_interaction(interaction_id)
    await service.submit_question_answer(questions[0].id, "a", "A", False, "ou_user")
    assert service.interaction_is_resolvable(interaction_id) is False


@pytest.mark.asyncio
async def test_question_batch_skip_blocking_is_resolvable(orchestrator, tmp_path):
    """Skipping a blocking question counts as resolved."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True, "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "b1",
                "questions": [{
                    "question_id": "q1", "kind": "blocking", "question": "Q1",
                    "reason": "", "allow_free_text": False,
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "affected_trace_ids": [],
                }],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="qa-skip", task_type="feature", description="test", approval_mode="normal",
    )
    await oc.tick(project.id)
    project = service.require_project(project.id)
    interaction_id = project.context.get("_pending_question_interaction_id")
    questions = service.get_questions_for_interaction(interaction_id)
    await service.submit_question_answer(questions[0].id, None, None, True, "ou_user")
    assert service.interaction_is_resolvable(interaction_id) is True


@pytest.mark.asyncio
async def test_question_batch_restart_recovery(orchestrator, tmp_path):
    """After restart, serialize should only show pending questions."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True, "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "b1",
                "questions": [
                    {"question_id": "q1", "kind": "blocking", "question": "Q1",
                     "reason": "", "allow_free_text": False,
                     "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                     "affected_trace_ids": []},
                    {"question_id": "q2", "kind": "blocking", "question": "Q2",
                     "reason": "", "allow_free_text": False,
                     "options": [{"key": "b", "label": "B", "impact": "", "recommended": True}],
                     "affected_trace_ids": []},
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="qa-restart", task_type="feature", description="test", approval_mode="normal",
    )
    await oc.tick(project.id)
    project = service.require_project(project.id)
    interaction_id = project.context.get("_pending_question_interaction_id")
    questions = service.get_questions_for_interaction(interaction_id)
    await service.submit_question_answer(questions[0].id, "a", "A", False, "ou_user")
    data = service.serialize_question_interaction(interaction_id)
    pending = [q for q in data["questions"] if q["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["question_id"] == "q2"


# ---------------------------------------------------------------------------
# Auxiliary task cancellation / failure consistency (Phase 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_project_fails_auxiliary_running_tasks(orchestrator, tmp_path):
    """cancel_project must mark ALL running tasks (incl. auxiliary) as failed."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({"planner": {"success": True, "next_state_event": "complete",
                                        "summary": "x", "goals": [], "acceptance_criteria": []}})
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="cancel-aux", task_type="feature", description="test", approval_mode="normal",
    )
    # Create a fake running auxiliary task (acceptance_evaluator).
    aux_task = await service.create_task(
        project_id=project.id, agent_type="acceptance_evaluator",
        state="ACCEPTANCE_PENDING", input_data={},
    )
    await service.start_task(aux_task.id, "/tmp/fake")

    # Cancel the project.
    await oc.cancel_project(project.id, reason="test cancel")

    # The auxiliary task must be failed, not left running.
    updated_task = service.session.get(Task, aux_task.id)
    assert updated_task.status == "failed"
    assert "test cancel" in (updated_task.error_message or "")


@pytest.mark.asyncio
async def test_cancel_persists_project_state_before_killing_runtime(orchestrator):
    """Interrupted handlers must see cancelled state before their process exits."""
    service, wm, renderer = orchestrator

    class ObservingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({})
            self.cancelled_statuses: list[str] = []

        def cancel(self, task_id: str) -> None:
            task = service.session.get(Task, task_id)
            project = service.require_project(task.project_id)
            self.cancelled_statuses.append(project.status)

    runtime = ObservingRuntime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="cancel-order", task_type="feature", description="test",
        approval_mode="normal",
    )
    task = await service.create_task(
        project_id=project.id, agent_type="planner",
        state="PLANNING_REQUIREMENTS", input_data={},
    )
    await service.start_task(task.id, "/tmp/fake")

    await oc.cancel_project(project.id, reason="cancel race")

    assert runtime.cancelled_statuses == ["cancelled"]


@pytest.mark.asyncio
async def test_repeated_cancel_cleans_late_running_task(orchestrator):
    service, _, _ = orchestrator
    project = await service.create_project(
        name="late-retry", task_type="feature", description="test",
        approval_mode="normal",
    )
    await service.cancel_project(project.id, reason="first cancel")
    late_task = await service.create_task(
        project_id=project.id, agent_type="planner",
        state="PLANNING_REQUIREMENTS", input_data={},
    )
    await service.start_task(late_task.id, "/tmp/fake")

    await service.cancel_project(project.id, reason="repeat cancel")

    assert service.session.get(Task, late_task.id).status == "failed"


@pytest.mark.asyncio
async def test_auxiliary_agent_crash_fails_task(orchestrator, tmp_path):
    """If _run_auxiliary_agent raises unexpectedly, the task must be failed."""
    service, wm, renderer = orchestrator
    # A runtime that raises during execute.
    class CrashingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            raise RuntimeError("unexpected crash")
    runtime = CrashingRuntime({})
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="crash-aux", task_type="feature", description="test", approval_mode="normal",
    )
    # Call _execute_auxiliary_agent directly.
    result = await oc._execute_auxiliary_agent(
        project, agent_type="acceptance_evaluator",
        input_data={"mode": "acceptance_evaluation"},
    )
    assert result is None  # crash -> None
    # The task must not be left running.
    from sqlmodel import select as _select
    tasks = service.session.exec(
        _select(Task).where(Task.project_id == project.id, Task.agent_type == "acceptance_evaluator")
    ).all()
    assert len(tasks) == 1
    assert tasks[0].status == "failed"


@pytest.mark.asyncio
async def test_unresolved_requirement_ambiguities_force_ask_user(orchestrator, tmp_path):
    """A REQUIREMENTS.json with ambiguities must not reach confirmation."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="ambi-req", task_type="feature", description="test"
    )
    worktree = tmp_path / "worktree"
    mac = worktree / ".mac"
    mac.mkdir(parents=True)
    (mac / "REQUIREMENTS.json").write_text(
        json.dumps({
            "summary": "test",
            "ambiguities": [
                {"question": "支持哪些客户端？", "impact": "决定技术栈"},
                {"question": "是否需要单点登录？", "default_assumption": "不需要"},
            ],
        }),
        encoding="utf-8",
    )
    output = {"next_state_event": "complete"}
    result = oc._block_on_unresolved_ambiguities(
        project, "PLANNING_REQUIREMENTS", worktree, output
    )
    assert result == "ask_user"
    assert output["next_state_event"] == "ask_user"
    batch = output["question_batch"]
    assert len(batch["questions"]) == 2
    assert batch["questions"][0]["decision_key"] == "artifact.ambiguity_0"
    assert batch["questions"][0]["allow_free_text"] is True
    assert batch["questions"][0]["options"] == []
    assert [
        option["key"] for option in batch["questions"][1]["options"]
    ] == ["accept_default", "provide_custom"]
    assert batch["questions"][1]["options"][0]["recommended"] is True
    assert batch["questions"][1]["allow_free_text"] is True


@pytest.mark.asyncio
async def test_unresolved_architecture_open_questions_force_ask_user(orchestrator, tmp_path):
    """An ARCHITECTURE.json with open_questions must not reach confirmation."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="ambi-arch", task_type="feature", description="test"
    )
    worktree = tmp_path / "worktree"
    mac = worktree / ".mac"
    mac.mkdir(parents=True)
    (mac / "ARCHITECTURE.json").write_text(
        json.dumps({
            "summary": "test",
            "open_questions": ["数据库选 SQLite 还是 PostgreSQL？"],
        }),
        encoding="utf-8",
    )
    output = {"next_state_event": "complete"}
    result = oc._block_on_unresolved_ambiguities(
        project, "PLANNING_ARCHITECTURE", worktree, output
    )
    assert result == "ask_user"
    assert output["question_batch"]["questions"][0]["question"].startswith("数据库选")


@pytest.mark.asyncio
async def test_no_ambiguities_allows_complete(orchestrator, tmp_path):
    """A clean artifact should not trigger ask_user."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="clean", task_type="feature", description="test"
    )
    worktree = tmp_path / "worktree"
    mac = worktree / ".mac"
    mac.mkdir(parents=True)
    (mac / "REQUIREMENTS.json").write_text(
        json.dumps({"summary": "test", "ambiguities": []}),
        encoding="utf-8",
    )
    output = {"next_state_event": "complete"}
    result = oc._block_on_unresolved_ambiguities(
        project, "PLANNING_REQUIREMENTS", worktree, output
    )
    assert result is None
    assert "question_batch" not in output


@pytest.mark.asyncio
async def test_filter_confirmed_questions_falls_back_to_question_id(orchestrator):
    """Questions should be suppressed by either decision_key or question_id."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="filter-qid", task_type="feature", description="test",
        initial_context={
            "confirmed_brief": {
                "decisions": [
                    {"key": "q1", "answer": "A", "status": "confirmed"},
                ]
            }
        },
    )
    batch = {
        "batch_key": "b1",
        "questions": [
            {"question_id": "q1", "decision_key": "", "question": "Q1"},
            {"question_id": "q2", "decision_key": "", "question": "Q2"},
        ],
    }
    filtered, suppressed = oc._filter_confirmed_questions(project, batch)
    assert len(filtered["questions"]) == 1
    assert filtered["questions"][0]["question_id"] == "q2"
    assert len(suppressed) == 1


class _RecordingBridge:
    """Records every outbound card so idempotency can be asserted."""

    def __init__(self):
        self.confirmation_cards: list[dict] = []
        self.input_cards: list[dict] = []
        self.content_cards: list[dict] = []
        self.interactive: list[dict] = []
        self.updated: list[str] = []

    async def send_confirmation_card(self, open_id, approval_id, title, summary,
                                     details, options, receive_id_type="open_id",
                                     chat_id=None, chat_type="p2p", show_actions=True):
        self.confirmation_cards.append({"approval_id": approval_id, "title": title})
        return f"msg_conf_{len(self.confirmation_cards)}"

    async def send_input_card(self, open_id, approval_id, title, question, context,
                              receive_id_type="open_id", chat_id=None, chat_type="p2p"):
        self.input_cards.append({"approval_id": approval_id, "title": title})
        return f"msg_input_{len(self.input_cards)}"

    async def send_content_card(self, open_id, title, content, color="blue",
                                receive_id_type="open_id", page=None, total=None):
        self.content_cards.append({"title": title})
        return f"msg_content_{len(self.content_cards)}"

    async def _send_interactive(self, open_id, card, receive_id_type="open_id"):
        self.interactive.append(card)
        return f"msg_inter_{len(self.interactive)}"

    async def update_card(self, message_id, card):
        self.updated.append(message_id)

    async def send_text(self, open_id, text, receive_id_type="open_id"):
        return "msg_text"


async def _rework_project(service):
    project = await service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1",
        feishu_chat_id="oc_group", feishu_chat_type="group",
    )
    project.current_state = "REWORK"
    service.session.add(project)
    service.session.commit()
    return project


@pytest.mark.asyncio
async def test_human_input_tick_twice_sends_cards_once(orchestrator):
    """Level-triggered ticks must not duplicate user-facing cards: the pending
    approval row is reused, so the card set is sent exactly once per approval
    instance."""
    service, wm, renderer = orchestrator
    bridge = _RecordingBridge()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
    project = await _rework_project(service)
    sm = StateMachine(project.task_type)
    approval = await service.create_approval(
        project_id=project.id, task_id=None, state="REWORK",
        approval_type="input", question="本轮未通过，请选择如何处理",
        options=sm.rework_options(),
    )

    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)
    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)

    assert len(bridge.confirmation_cards) == 1
    refreshed = service.get_approval(approval.id)
    assert refreshed.payload.get("notification")


@pytest.mark.asyncio
async def test_human_input_resends_when_notification_marker_missing(orchestrator):
    """A missing marker means the previous send crashed mid-way — the next
    tick must retry the notification."""
    service, wm, renderer = orchestrator
    bridge = _RecordingBridge()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
    project = await _rework_project(service)
    sm = StateMachine(project.task_type)
    approval = await service.create_approval(
        project_id=project.id, task_id=None, state="REWORK",
        approval_type="input", question="本轮未通过，请选择如何处理",
        options=sm.rework_options(),
    )

    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)
    approval = service.get_approval(approval.id)
    payload = dict(approval.payload)
    payload.pop("notification", None)
    approval.payload = payload
    service.session.commit()

    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)
    assert len(bridge.confirmation_cards) == 2


@pytest.mark.asyncio
async def test_human_input_new_approval_round_sends_new_card(orchestrator):
    """A new approval instance (next round) must notify again."""
    service, wm, renderer = orchestrator
    bridge = _RecordingBridge()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
    project = await _rework_project(service)
    sm = StateMachine(project.task_type)
    approval = await service.create_approval(
        project_id=project.id, task_id=None, state="REWORK",
        approval_type="input", question="本轮未通过，请选择如何处理",
        options=sm.rework_options(),
    )

    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)
    assert len(bridge.confirmation_cards) == 1

    await service.respond_approval(approval.id, "to_testing", "ou_1")
    approval2 = await service.create_approval(
        project_id=project.id, task_id=None, state="REWORK",
        approval_type="input", question="本轮未通过，请选择如何处理",
        options=sm.rework_options(),
    )
    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)

    assert len(bridge.confirmation_cards) == 2
    assert bridge.confirmation_cards[-1]["approval_id"] == approval2.id


@pytest.mark.asyncio
async def test_human_confirmation_tick_twice_sends_cards_once(orchestrator):
    """The confirmation path gets the same idempotency: reused pending
    approval ⇒ no duplicate report/action card set."""
    service, wm, renderer = orchestrator
    bridge = _RecordingBridge()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
    project = await service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1",
        feishu_chat_id="oc_group", feishu_chat_type="group",
    )
    project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    service.session.add(project)
    service.session.commit()
    sm = StateMachine(project.task_type)
    approval = await service.create_approval(
        project_id=project.id, task_id=None, state="ARCHITECTURE_CONFIRM_PENDING",
        approval_type="confirmation", question="确认方案",
        options=[{"action": "confirm", "label": "确认"},
                 {"action": "reject", "label": "驳回"}],
    )

    await oc._handle_human_confirmation(service.require_project(project.id), sm)
    first_content_count = len(bridge.content_cards)
    await oc._handle_human_confirmation(service.require_project(project.id), sm)

    assert len(bridge.confirmation_cards) == 1
    assert len(bridge.content_cards) == first_content_count
    refreshed = service.get_approval(approval.id)
    assert refreshed.payload["notification"].get("confirmation_card_message_id")

    # Next round (new approval instance) notifies again.
    await service.respond_approval(approval.id, "confirm", "ou_1")
    await service.create_approval(
        project_id=project.id, task_id=None, state="ARCHITECTURE_CONFIRM_PENDING",
        approval_type="confirmation", question="确认方案",
        options=[{"action": "confirm", "label": "确认"},
                 {"action": "reject", "label": "驳回"}],
    )
    await oc._handle_human_confirmation(service.require_project(project.id), sm)
    assert len(bridge.confirmation_cards) == 2
