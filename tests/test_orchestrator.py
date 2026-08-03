import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.orchestrator import AgentOrchestrator, _concise_agent_failure
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntimeResult, FakeRuntime, validate_agent_output
from mac_system.config import get_agent_config
from mac_system.core.failure_router import FailureRouter
from mac_system.core.lock_manager import ProjectLockManager
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Approval, FeedbackPacket, NotificationOutbox, Task
from mac_system.state_machine import StateMachine


def test_capability_fact_filter_uses_trace_ids_not_fact_text():
    facts = [
        {
            "key": "runtime.same_words_as_current_request",
            "type": "capability",
            "value": {"description": "current memory and persona"},
            "affected_trace_ids": ["AC-OLD"],
        },
        {
            "key": "runtime.opaque_capability",
            "type": "capability",
            "value": {"description": "unrelated-looking text"},
            "affected_trace_ids": ["AC-CURRENT"],
        },
        {
            "key": "project.unscoped_fact",
            "type": "requirement",
            "value": {"value": "always kept by this trace filter"},
        },
    ]

    selected = AgentOrchestrator._filter_facts_for_current_trace(
        facts, {"AC-CURRENT", "fb_current"}
    )

    assert [fact["key"] for fact in selected] == [
        "runtime.opaque_capability",
        "project.unscoped_fact",
    ]


def test_rework_feedback_frontier_is_newest_blocking_packet():
    from datetime import datetime, timedelta

    older = FeedbackPacket(
        id="fb_old",
        project_id="proj",
        source_stage="REVIEW",
        source_round=1,
        kind="quality_failure",
        summary="old failure whose wording overlaps the new work",
        blocking=True,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    newest = FeedbackPacket(
        id="fb_new",
        project_id="proj",
        source_stage="TESTING",
        source_round=2,
        kind="quality_failure",
        summary="current failure",
        blocking=True,
        created_at=datetime(2026, 1, 1) + timedelta(seconds=1),
        updated_at=datetime(2026, 1, 1) + timedelta(seconds=1),
    )
    later_nonblocking = FeedbackPacket(
        id="fb_note",
        project_id="proj",
        source_stage="REVIEW",
        source_round=3,
        kind="suggestion",
        summary="optional note",
        blocking=False,
        created_at=datetime(2026, 1, 1) + timedelta(seconds=2),
        updated_at=datetime(2026, 1, 1) + timedelta(seconds=2),
    )

    assert AgentOrchestrator._current_feedback_frontier(
        [older, newest, later_nonblocking]
    ) == [newest]


def test_requirements_rework_uses_current_artifact_not_initial_scope_facts():
    facts = [
        {
            "key": "project.scope",
            "scope_type": "project",
            "value": {"value": ["obsolete broad scope"]},
        },
        {
            "key": "current.stage.decision",
            "scope_type": "stage",
            "scope_ref": "requirements",
            "value": {"value": "new scoped decision"},
        },
    ]

    assert AgentOrchestrator._requirements_round_facts(
        facts, has_current_artifact=False
    ) == facts
    assert AgentOrchestrator._requirements_round_facts(
        facts, has_current_artifact=True
    ) == [facts[1]]


def test_clarification_card_uses_current_question_not_stale_context_answer():
    service = Mock()
    interaction = SimpleNamespace(id="qi_current")
    service.session.exec.return_value.first.return_value = interaction
    service.get_questions_for_interaction.return_value = [
        SimpleNamespace(
            status="pending",
            question="当前微信窗口是否已经登录？",
        )
    ]
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.project_service = service
    project = SimpleNamespace(
        id="proj_1",
        context={"clarification_answer": "数小时前已经消费的无关问题"},
    )

    question = orchestrator._build_input_question(project, "CLARIFICATION_PENDING")

    assert "当前微信窗口是否已经登录" in question
    assert "数小时前" not in question


@pytest.mark.asyncio
async def test_input_resolution_receives_actual_question_batch_and_capabilities():
    service = Mock()
    service.get_question_interaction.return_value = SimpleNamespace(
        id="qi_1",
        status="pending",
    )
    service.get_questions_for_interaction.return_value = [
        SimpleNamespace(
            status="pending",
            question_id="q_llm",
            kind="blocking",
            question="是否配置真实 LLM 能力？",
            reason="真实入口验证需要该能力",
            options=[{"key": "configure_secret", "recommended": True}],
            default_assumption=None,
            recommended_option="configure_secret",
            sensitivity="secret",
            secret_env_var="LLM_API_KEY",
        )
    ]
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.project_service = service
    orchestrator._project_runtime_capability_contracts = Mock(
        return_value=[
            {
                "capability_id": "ctx_cap",
                "available": True,
                "env_vars": ["LLM_API_KEY"],
            }
        ]
    )
    orchestrator._build_summary_for_state = Mock(return_value="summary")
    orchestrator._execute_auxiliary_agent = AsyncMock(
        return_value={
            "disposition": "answered",
            "answer": "使用已配置能力",
        }
    )
    project = SimpleNamespace(
        id="proj_1",
        context={"_pending_question_interaction_id": "qi_1"},
    )

    answer = await orchestrator._answer_input_with_coordinator(
        project,
        "TESTING_INPUT_PENDING",
        "Tester 请求澄清，请回复你的答案",
    )

    assert answer == "使用已配置能力"
    call = orchestrator._execute_auxiliary_agent.await_args
    assert call.kwargs["input_data"]["input_questions"][0]["question_id"] == "q_llm"
    assert call.kwargs["input_data"]["runtime_capabilities"][0]["env_vars"] == [
        "LLM_API_KEY"
    ]
    assert "q_llm" in call.kwargs["extra_variables"]["input_questions"]


def test_project_runtime_env_resolves_plist_secret_reference(
    tmp_path, monkeypatch
):
    import plistlib

    source = tmp_path / "service.plist"
    with source.open("wb") as stream:
        plistlib.dump(
            {"EnvironmentVariables": {"SOURCE_LLM_KEY": "secret-value"}},
            stream,
        )
    service = Mock()
    service.session.exec.return_value.all.return_value = []
    service.get_active_context_records.return_value = [
        SimpleNamespace(
            id="ctx_cap",
            value={
                "source_file": str(source),
                "env_bindings": {"LLM_API_KEY": "SOURCE_LLM_KEY"},
            },
        )
    ]
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.project_service = service
    orchestrator.prompt_renderer = SimpleNamespace(
        agent_config={"runtime_capabilities": []}
    )
    monkeypatch.setattr(
        "mac_system.agents.orchestrator.get_allowed_bind_roots",
        lambda: [tmp_path],
    )

    assert orchestrator._runtime_env_for_project("proj_1") == {
        "LLM_API_KEY": "secret-value"
    }


def _resolution_blocker(blocker_id: str = "rr_test") -> dict:
    return {
        "id": blocker_id,
        "category": "missing_information",
        "summary": "missing required information",
        "evidence": ["work item does not contain the answer"],
        "required_resolution": "provide the missing decision",
        "preferred_resolver": "coordinator",
        "blocking": True,
    }


def test_all_delivery_schemas_require_resolution_request_for_ask_user():
    root = Path(__file__).resolve().parents[1]
    question_batch = {
        "batch_key": "batch_contract",
        "questions": [{
            "question_id": "q_contract",
            "decision_key": "contract.required_value",
            "kind": "blocking",
            "question": "Which value is required?",
            "reason": "The WorkItem is missing it",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": [],
        }],
    }
    for filename in (
        "agent_turn_output.json",
        "coder_output.json",
        "tester_output.json",
        "reviewer_output.json",
        "acceptance_evaluator_output.json",
    ):
        schema = root / "specs" / "schemas" / filename
        incomplete = {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": question_batch,
        }
        assert validate_agent_output(incomplete, schema)
        complete = {
            **incomplete,
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker()],
        }
        assert validate_agent_output(complete, schema) is None
        wrong_ask_reason = {**complete, "completion_reason": "missing_api_key"}
        assert validate_agent_output(wrong_ask_reason, schema)
        stage_extras = {
            "tester_output.json": {
                "passed": True,
                "test_evidence": [],
                "verified_feedback_ids": ["fb_test"],
            },
            "reviewer_output.json": {"result": "pass"},
            "acceptance_evaluator_output.json": {
                "result": "pass",
                "summary": "accepted",
                "evidence": {
                    "tests_passed": True,
                    "review_result": "pass",
                    "meets_requirements": True,
                },
                "acceptance_matrix": [{
                    "acceptance_criterion_id": "AC-001",
                    "requirement_ids": ["REQ-001"],
                    "spec_ids": ["SPEC-001"],
                    "implementation_evidence": ["implemented"],
                    "test_evidence_ids": ["TE-001"],
                    "review_result": "pass",
                    "result": "pass",
                }],
                "blocking_conditions": [],
            },
        }
        stage_complete = {
            "success": True,
            "next_state_event": "complete",
            "completion_reason": "specs_generated",
            **stage_extras.get(filename, {}),
        }
        assert validate_agent_output(stage_complete, schema) is None


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
    assert "保留原 pending action" in prompt
    assert "禁止把每条后续消息默认当成旧问题答案" in prompt
    assert "当前阶段产物无法审批" in prompt
    assert "`commitment` 不存在 `none` 取值" in prompt
    assert "禁止读取 `.env`" in prompt
    assert "不得再问“是否立即配置”" in prompt
    assert "适配 DeepSeek" in prompt
    assert "新的执行范围卡就是唯一确认入口" in prompt
    assert "不要把上面的范围补充改写成 `request_clarification`" in prompt

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


def test_reviewer_prompt_uses_cumulative_iteration_acceptance_contract():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "reviewer.md").read_text(encoding="utf-8")

    assert "BUG_ANALYSIS.json" in prompt
    assert "DOC_OUTLINE.json" in prompt
    assert "唯一当前验收合同" in prompt
    assert "不得扩大" in prompt
    assert '`verification_method="manual"` 或 `verification_method="e2e"`' in prompt


def test_schema_validation_reports_aliases_and_missing_canonical_fields_together():
    root = Path(__file__).resolve().parents[1]
    output = {
        "success": True,
        "reply": "确认后开始开发",
        "intent": "authorize_execution",
        "effect": "workspace_write",
        "commitment": "authorized",
        "needs_clarification": False,
        "updated_summary": "个人网站需要个人 Agent",
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "none",
            "task_type": "feature",
            "description": "开发带个人 Agent 的综合性个人网站",
            "scope": ["个人 Agent"],
            "permissions": ["workspace_write"],
            "acceptance_criteria": ["个人 Agent 可正常使用"],
            "decisions": [{"title": "个人 Agent", "choice": "需要"}],
        },
    }
    error = validate_agent_output(
        output,
        root / "specs" / "schemas" / "conversation_coordinator_output.json",
    )

    assert error is not None
    assert "'choice', 'title' were unexpected" in error
    for field in ("key", "question", "answer", "status"):
        assert f"'{field}' is a required property" in error


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
    assert "_context_freshness" not in data

    task = service.session.exec(
        select(Task).where(Task.project_id == project.id)
    ).first()
    assert task.output_data["_context_freshness"]["fresh"] is True
    assert task.output_data["_context_freshness"]["task_context_version"] == 1


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
        name="coverage", task_type="feature", description="Test", approval_mode="strict"
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
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].kind == "acceptance_rejection"
    assert feedback[0].failure_class == "observed_behavior_failure"


@pytest.mark.asyncio
async def test_acceptance_evaluator_failure_becomes_coder_feedback(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="acceptance-feedback", task_type="feature", description="Test"
    )
    evaluator = await service.create_task(
        project_id=project.id,
        agent_type="acceptance_evaluator",
        state="ACCEPTANCE_PENDING",
        round_number=3,
        input_data={},
    )
    await service.complete_task(evaluator.id, {
        "result": "fail",
        "next_state_event": "fail",
    })
    recommendation = {
        "result": "fail",
        "next_state_event": "fail",
        "action": "reject_to_coding",
        "summary": "真实入口仍会卡在加载态",
        "reason": "修复流式请求结束语义",
        "acceptance_matrix": [
            {
                "acceptance_criterion_id": "AC-STREAM-001",
                "result": "fail",
                "implementation_evidence": ["回复已显示但加载态未结束"],
                "test_evidence_ids": [],
                "notes": "真实浏览器等待超时",
            },
            {
                "acceptance_criterion_id": "AC-HEALTH-001",
                "result": "pass",
            },
        ],
    }

    first = await oc._record_acceptance_evaluator_failure(project, recommendation)
    second = await oc._record_acceptance_evaluator_failure(project, recommendation)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].source_task_id == evaluator.id
    assert feedback[0].source_round == 3
    assert feedback[0].recommended_rework_target == "coding"
    assert feedback[0].user_comment == "修复流式请求结束语义"
    assert feedback[0].evidence == [{
        "type": "acceptance_failure",
        "acceptance_criterion_id": "AC-STREAM-001",
        "result": "fail",
        "implementation_evidence": ["回复已显示但加载态未结束"],
        "test_evidence_ids": [],
        "manual_verification": None,
        "notes": "真实浏览器等待超时",
    }]
    assert oc._feedback_relevant_to_state("CODING", feedback[0]) is True
    assert oc._feedback_relevant_to_state("PLANNING_ARCHITECTURE", feedback[0]) is False

    next_evaluator = await service.create_task(
        project_id=project.id,
        agent_type="acceptance_evaluator",
        state="ACCEPTANCE_PENDING",
        round_number=4,
        input_data={},
    )
    await service.complete_task(next_evaluator.id, {
        "result": "fail",
        "next_state_event": "fail",
    })
    next_recommendation = {
        **recommendation,
        "summary": "当前只剩移动 WebKit 超时",
        "reason": "只修复当前移动端缺陷",
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-MOBILE-001",
            "result": "fail",
            "implementation_evidence": ["WebKit request timed out"],
            "test_evidence_ids": [],
        }],
    }

    latest = await oc._record_acceptance_evaluator_failure(
        project, next_recommendation
    )

    open_feedback = service.get_open_feedback_packets(project.id)
    assert [item.id for item in open_feedback] == [latest.id]
    superseded = service.session.get(FeedbackPacket, first.id)
    assert superseded.status == "resolved"
    assert superseded.resolved_by_task_id == next_evaluator.id


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


def test_external_acceptance_classification_separates_method_from_fact_source(
    orchestrator, tmp_path,
):
    service, wm, renderer = orchestrator
    project_path = tmp_path / "classification"
    (project_path / ".mac").mkdir(parents=True)
    project = SimpleNamespace(
        task_type="feature",
        workspace_path=str(project_path),
        bound_directory=None,
        id="proj_classification",
    )
    (project_path / ".mac" / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-SYSTEM",
                    "verification_method": "manual",
                    "evidence_source": "system_observable",
                },
                {
                    "id": "AC-EXTERNAL",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                },
                {
                    "id": "AC-LEGACY",
                    "verification_method": "manual",
                },
            ]
        }),
        encoding="utf-8",
    )

    assert AgentOrchestrator._external_manual_acceptance_criterion_ids(project) == {
        "AC-EXTERNAL",
    }
    assert AgentOrchestrator._acceptance_stage_manual_criterion_ids(project) == {
        "AC-SYSTEM",
        "AC-EXTERNAL",
        "AC-LEGACY",
    }


def test_cumulative_acceptance_contract_walks_three_generation_lineage(tmp_path):
    workspace = tmp_path / "workspace"
    mac_dir = workspace / ".mac"
    current_dir = mac_dir / "projects" / "proj_current"
    bug_dir = mac_dir / "projects" / "proj_bug"
    root_namespace = mac_dir / "projects" / "proj_root"
    for path in (current_dir, bug_dir, root_namespace):
        path.mkdir(parents=True, exist_ok=True)

    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-ROOT"}]}),
        encoding="utf-8",
    )
    (bug_dir / "BUG_ANALYSIS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-BUG"}]}),
        encoding="utf-8",
    )
    (bug_dir / "STATE.json").write_text(
        json.dumps({"context": {"base_project_id": "proj_root"}}),
        encoding="utf-8",
    )
    (current_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-CURRENT"}]}),
        encoding="utf-8",
    )
    project = SimpleNamespace(
        id="proj_current",
        task_type="feature",
        workspace_path=str(workspace),
        bound_directory=str(workspace),
        context={"base_project_id": "proj_bug"},
    )

    assert AgentOrchestrator._required_acceptance_criterion_ids(project) == {
        "AC-ROOT",
        "AC-BUG",
        "AC-CURRENT",
    }
    assert AgentOrchestrator._current_acceptance_criteria_for_project(project) == [
        {"id": "AC-CURRENT"}
    ]


def test_quality_context_uses_one_authoritative_artifact_per_kind(tmp_path):
    workspace = tmp_path / "workspace"
    mac_dir = workspace / ".mac"
    current_dir = mac_dir / "projects" / "proj_current"
    previous_dir = mac_dir / "projects" / "proj_previous"
    for directory in (current_dir, previous_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (current_dir / "ITERATION_BRIEF.json").write_text("{}", encoding="utf-8")
    (previous_dir / "ITERATION_BRIEF.json").write_text("{}", encoding="utf-8")
    (previous_dir / "STATE.json").write_text(
        json.dumps({"context": {}}), encoding="utf-8"
    )
    (previous_dir / "ARCHITECTURE.json").write_text("{}", encoding="utf-8")
    (mac_dir / "ARCHITECTURE.json").write_text("{}", encoding="utf-8")
    (mac_dir / "REQUIREMENTS.json").write_text("{}", encoding="utf-8")
    project = SimpleNamespace(
        id="proj_current",
        task_type="iteration",
        workspace_path=str(workspace),
        bound_directory=str(workspace),
        context={"base_project_id": "proj_previous"},
    )
    oc = object.__new__(AgentOrchestrator)

    context_files = oc._context_files_for_state(project, "REVIEW")

    by_name = {}
    for path in context_files:
        by_name.setdefault(path.name, []).append(path)
    assert by_name["ITERATION_BRIEF.json"] == [current_dir / "ITERATION_BRIEF.json"]
    assert by_name["ARCHITECTURE.json"] == [previous_dir / "ARCHITECTURE.json"]
    assert by_name["REQUIREMENTS.json"] == [mac_dir / "REQUIREMENTS.json"]


def test_iteration_brief_contributes_current_and_regression_acceptance(tmp_path):
    workspace = tmp_path / "workspace"
    current_dir = workspace / ".mac" / "projects" / "proj_iteration"
    current_dir.mkdir(parents=True)
    (workspace / ".mac" / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-REGRESSION"}]}),
        encoding="utf-8",
    )
    iteration_ac = {
        "id": "AC-ITER-001",
        "statement": "new behavior",
        "verification_method": "automated_test",
        "evidence_source": "system_observable",
    }
    (current_dir / "ITERATION_BRIEF.json").write_text(
        json.dumps({"acceptance_criteria": [iteration_ac]}),
        encoding="utf-8",
    )
    project = SimpleNamespace(
        id="proj_iteration",
        task_type="iteration",
        workspace_path=str(workspace),
        bound_directory=str(workspace),
        context={},
    )

    assert AgentOrchestrator._required_acceptance_criterion_ids(project) == {
        "AC-ITER-001",
    }
    assert AgentOrchestrator._current_acceptance_criteria_for_project(project) == [
        iteration_ac
    ]
    current, regression = AgentOrchestrator._work_item_acceptance_contracts(
        project
    )
    assert current == [iteration_ac]
    assert regression == [{"id": "AC-REGRESSION"}]
    assert AgentOrchestrator._verification_acceptance_criteria_for_project(
        project
    ) == [iteration_ac]


def test_current_requirements_can_retire_historical_acceptance_criteria(tmp_path):
    workspace = tmp_path / "workspace"
    current_dir = workspace / ".mac" / "projects" / "proj_current"
    current_dir.mkdir(parents=True)
    (workspace / ".mac" / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-KEEP", "statement": "still required"},
                {"id": "AC-REMOVE", "statement": "obsolete capability"},
                {"id": "AC-OVERRIDE", "statement": "old wording"},
            ]
        }),
        encoding="utf-8",
    )
    (current_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "retired_acceptance_criterion_ids": ["AC-REMOVE"],
            "acceptance_criteria": [
                {"id": "AC-CURRENT", "statement": "new capability"},
                {"id": "AC-OVERRIDE", "statement": "new wording"},
            ],
        }),
        encoding="utf-8",
    )
    project = SimpleNamespace(
        id="proj_current",
        task_type="feature",
        workspace_path=str(workspace),
        bound_directory=str(workspace),
        context={},
    )

    contract = AgentOrchestrator._acceptance_criteria_for_project(project)
    assert {item["id"] for item in contract} == {
        "AC-KEEP",
        "AC-CURRENT",
        "AC-OVERRIDE",
    }
    assert next(item for item in contract if item["id"] == "AC-OVERRIDE")[
        "statement"
    ] == "new wording"


@pytest.mark.asyncio
async def test_tester_owned_harness_failure_retries_before_human_rework(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="tester-self-retry",
        task_type="feature",
        description="verify real integration",
        approval_mode="normal",
    )
    project.current_state = "TESTING"
    project.context = {
        **project.context,
        "loop_supervisor": {
            "signature": "stale-contract-failure",
            "prohibited_default_target": "testing",
        },
    }
    service.session.commit()
    task = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(task.id, {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
    })
    task = service.session.get(Task, task.id)
    assert task is not None
    oc._execute_agent = AsyncMock()

    await oc._handle_agent_output(
        project,
        StateMachine("feature"),
        task,
        tmp_path,
        {
            "success": True,
            "next_state_event": "fail",
            "passed": False,
            "failure_class": "test_bug",
            "recommended_rework_target": "testing",
            "failures": [{"test": "probe", "error": "invalid harness"}],
            "report": "The tester-authored probe did not execute",
        },
    )

    refreshed = service.require_project(project.id)
    assert refreshed.current_state == "TESTING"
    assert refreshed.context["quality_self_retry_counts"]["TESTING"] == 1
    assert refreshed.updated_at >= task.updated_at
    assert len(service.get_open_feedback_packets(project.id)) == 1
    oc._execute_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_tester_round_closes_independently_verified_feedback(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="partial-feedback-closure",
        task_type="feature",
        description="verify previous fixes before reporting a new blocker",
        approval_mode="normal",
    )
    project.current_state = "TESTING"
    service.session.commit()
    previous = await service.create_feedback_packet(
        project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="human_correction",
        summary="old defect",
        blocking=True,
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "implementation_bug",
        "recommended_rework_target": "coding",
        "test_evidence": [{
            "id": "TE-OLD-FIX",
            "type": "integration",
            "result": "pass",
            "feedback_ids": [previous.id],
            "acceptance_criterion_ids": ["AC-OLD"],
        }],
        "verified_feedback_ids": [previous.id],
        "failures": [{
            "id": "TF-NEW",
            "test": "new behavior",
            "error": "a separate implementation defect remains",
            "blocking": True,
        }],
        "report": "old defect fixed; separate defect remains",
    }
    await service.complete_task(task.id, output)
    task = service.session.get(Task, task.id)

    await oc._handle_agent_output(
        project,
        StateMachine("feature"),
        task,
        tmp_path,
        output,
        runtime_tool_calls=[{"name": "Bash", "input": {"command": "pytest"}}],
    )

    assert service.session.get(FeedbackPacket, previous.id).status == "resolved"
    open_packets = service.get_open_feedback_packets(project.id)
    assert previous.id not in {packet.id for packet in open_packets}
    assert any(packet.summary != "old defect" for packet in open_packets)


@pytest.mark.asyncio
async def test_tester_self_retry_reuses_worktree_changes(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="tester-retry-worktree",
        task_type="feature",
        description="repair a weak formal test",
        approval_mode="normal",
    )
    project.current_state = "TESTING"
    service.session.commit()

    class TwoPassTesterRuntime(FakeRuntime):
        materializes_worktree_changes = True

        def __init__(self):
            super().__init__({})
            self.calls = 0
            self.worktrees = []

        async def execute(self, **kwargs):
            self.calls += 1
            worktree = Path(kwargs["worktree"])
            self.worktrees.append(worktree)
            probe = worktree / ".mac" / "retry_probe.py"
            if self.calls == 1:
                probe.parent.mkdir(exist_ok=True)
                probe.write_text("assert 1 == 1\n", encoding="utf-8")
                return AgentRuntimeResult(
                    True,
                    {
                        "success": True,
                        "next_state_event": "fail",
                        "passed": False,
                        "failure_class": "test_bug",
                        "recommended_rework_target": "testing",
                        "failures": [{
                            "test": "retry_probe",
                            "error": "harness incomplete",
                        }],
                        "test_evidence": [],
                        "verified_feedback_ids": [],
                    },
                    [], "ok", None, [],
                )
            assert probe.read_text(encoding="utf-8") == "assert 1 == 1\n"
            return AgentRuntimeResult(
                True,
                {
                    "success": True,
                    "next_state_event": "complete",
                    "passed": True,
                    "failure_class": "none",
                    "recommended_rework_target": "none",
                    "failures": [],
                    "test_evidence": [{
                        "id": "TE-RETRY",
                        "result": "pass",
                        "acceptance_criterion_ids": [],
                        "command_or_method": "python .mac/retry_probe.py",
                    }],
                    "uncovered_acceptance_criterion_ids": [],
                    "verified_feedback_ids": [],
                },
                [], "ok", None, [],
            )

    runtime = TwoPassTesterRuntime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    await oc.tick(project.id)

    assert runtime.calls == 2
    assert runtime.worktrees[0] == runtime.worktrees[1]
    assert (runtime.worktrees[1] / ".mac" / "retry_probe.py").is_file()


def test_tester_repository_change_is_not_self_retriable():
    assert AgentOrchestrator._tester_failure_is_self_retriable({
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "failures": [{
            "file": "tests/test_browser_probes.py",
            "error": "formal probe must be added to the repository",
        }],
    }) is False

    assert AgentOrchestrator._tester_failure_is_self_retriable({
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "failures": [{"error": "existing command was interrupted"}],
    }) is True


@pytest.mark.asyncio
async def test_rework_reentry_resets_tester_self_retry_window(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="tester-new-rework-window",
        task_type="feature",
        description="verify a later independent correction",
    )
    project.current_state = "REWORK"
    project.context = {
        **project.context,
        "quality_self_retry_counts": {"TESTING": 1},
    }
    service.session.commit()

    updated = await service.transition_state(
        project.id, "to_testing", triggered_by="user"
    )

    assert updated.current_state == "TESTING"
    assert "quality_self_retry_counts" not in updated.context


@pytest.mark.asyncio
async def test_quality_feedback_creation_is_idempotent_per_task(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="quality-feedback-idempotency",
        task_type="feature",
        description="avoid recovery duplicates",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    output = {
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "report": "structured conclusion needs correction",
        "failures": [],
    }

    first = await oc._create_feedback_from_tester_output(project, tester, output)
    second = await oc._create_feedback_from_tester_output(project, tester, output)

    assert first.id == second.id
    assert len(service.get_open_feedback_packets(project.id)) == 1


@pytest.mark.asyncio
async def test_system_observable_manual_ac_uses_acceptance_agent_evidence(
    orchestrator,
):
    """Final black-box evidence must not require duplicate Tester/Reviewer coverage."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="system-manual-acceptance", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-SYSTEM-MANUAL",
                "verification_method": "manual",
                "evidence_source": "system_observable",
            }]
        }),
        encoding="utf-8",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(tester.id, {
        "passed": True,
        "test_evidence": [{
            "id": "TE-SKIPPED",
            "result": "skipped",
            "acceptance_criterion_ids": ["AC-SYSTEM-MANUAL"],
        }],
    })
    reviewer = await service.create_task(project.id, "reviewer", "REVIEW", {})
    await service.complete_task(reviewer.id, {
        "result": "pass",
        "requirement_coverage": [{
            "acceptance_criterion_id": "AC-SYSTEM-MANUAL",
            "status": "partially_covered",
            "evidence": ["deferred to final black-box acceptance"],
        }],
    })
    evaluation = {
        "result": "pass",
        "action": "confirm",
        "blocking_conditions": [],
        "_acceptance_revision_key": "rev-system-manual",
        "_acceptance_agent_output": {
            "criteria": [{
                "acceptance_criterion_id": "AC-SYSTEM-MANUAL",
                "evidence_source": "system_observable",
                "result": "pass",
                "method": "real browser interaction",
                "actual": "todo persisted after refresh",
                "evidence": ["browser trace: add, refresh, item retained"],
                "limitations": [],
            }],
        },
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-SYSTEM-MANUAL",
            "result": "pass",
            "test_evidence_ids": ["acceptance-agent-browser-trace"],
        }],
    }

    assert oc._check_acceptance_coverage(project, evaluation) is None
    oc._record_acceptance_matrix_evidence(project, evaluation)
    evidence = service.get_active_acceptance_evidence(project.id)[
        "AC-SYSTEM-MANUAL"
    ]
    assert evidence.source_type == "acceptance_agent"
    assert evidence.verdict == "pass"

    # The evaluator audits evidence; it cannot replace a blocked executor with
    # an unaudited self-authored success claim.
    evaluation["_acceptance_agent_output"]["criteria"][0].update({
        "result": "blocked",
        "evidence": [],
        "limitations": ["browser was initially unavailable"],
    })
    evaluation["acceptance_matrix"][0]["manual_verification"] = {
        "status": "verified",
        "source_question_id": "acceptance-evaluator-playwright",
        "evidence_scope": "Playwright real browser: add, refresh, item retained",
        "limitations": ["无"],
    }
    violation = oc._check_acceptance_coverage(project, evaluation)
    assert violation is not None
    assert "missing direct Acceptance Agent black-box evidence" in violation["message"]


@pytest.mark.asyncio
async def test_acceptance_handoff_completes_hybrid_criterion_without_duplicate_review(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="hybrid-acceptance",
        task_type="feature",
        description="Test",
    )
    project.task_type = "iteration"
    service.session.add(project)
    service.session.commit()
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "ITERATION_BRIEF.json").write_text(
        json.dumps(
            {
                "acceptance_criteria": [
                    {
                        "id": "AC-HYBRID",
                        "verification_method": "automated_test",
                        "evidence_source": "system_observable",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(
        tester.id,
        {
            "passed": True,
            "test_evidence": [
                {
                    "id": "TE-PASS",
                    "result": "pass",
                    "acceptance_criterion_ids": ["AC-HYBRID"],
                },
                {
                    "id": "TE-ACCEPTANCE",
                    "type": "e2e",
                    "result": "skipped",
                    "acceptance_criterion_ids": ["AC-HYBRID"],
                    "command_or_method": "Acceptance opens the public URL",
                    "expected": "The end-to-end behavior passes",
                },
            ],
        },
    )
    reviewer = await service.create_task(project.id, "reviewer", "REVIEW", {})
    await service.complete_task(
        reviewer.id,
        {
            "result": "pass",
            "requirement_coverage": [
                {
                    "acceptance_criterion_id": "AC-HYBRID",
                    "status": "partially_covered",
                    "evidence": ["Acceptance owns the final independent run"],
                }
            ],
        },
    )
    evaluation = {
        "result": "pass",
        "action": "confirm",
        "blocking_conditions": [],
        "_acceptance_revision_key": "rev-hybrid",
        "_acceptance_agent_output": {
            "criteria": [
                {
                    "acceptance_criterion_id": "AC-HYBRID",
                    "evidence_source": "system_observable",
                    "result": "pass",
                    "method": "real browser interaction",
                    "actual": "public flow passed",
                    "evidence": ["browser trace"],
                    "limitations": [],
                }
            ]
        },
        "acceptance_matrix": [
            {
                "acceptance_criterion_id": "AC-HYBRID",
                "result": "pass",
                "test_evidence_ids": ["TE-PASS"],
            }
        ],
    }

    assert oc._check_acceptance_coverage(project, evaluation) is None
    oc._record_acceptance_matrix_evidence(project, evaluation)
    evidence = service.get_active_acceptance_evidence(project.id)["AC-HYBRID"]
    assert evidence.source_type == "acceptance_agent"
    assert evidence.verdict == "pass"


def test_acceptance_coverage_rework_route_uses_current_profile():
    iteration = SimpleNamespace(task_type="iteration")
    feature = SimpleNamespace(task_type="feature")

    assert AgentOrchestrator._acceptance_coverage_rework_route(iteration) == (
        "reject_to_coding",
        "coding",
    )
    assert AgentOrchestrator._acceptance_coverage_rework_route(feature) == (
        "reject_to_specs",
        "specs",
    )


def test_user_facing_summary_hides_protocol_ids_and_state_enums():
    from mac_system.agents.orchestrator import _sanitize_user_facing_summary

    rendered = _sanitize_user_facing_summary(
        "反馈 fb_20260728_deadbeef 已验证；TESTING_CONFIRM_PENDING 完成；"
        "verification_method=manual",
        StateMachine("feature"),
    )

    assert "fb_20260728_deadbeef" not in rendered
    assert "TESTING_CONFIRM_PENDING" not in rendered
    assert "相关内部记录" in rendered
    assert "等待确认测试结果" in rendered
    assert "验证方式=manual" in rendered


@pytest.mark.asyncio
async def test_acceptance_agent_is_cached_by_artifact_revision(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "acceptance_agent": {
            "success": True,
            "result": "pass",
            "summary": "black-box pass",
            "criteria": [{
                "acceptance_criterion_id": "AC-001",
                "evidence_source": "system_observable",
                "result": "pass",
                "method": "HTTP black-box",
                "expected": "works",
                "actual": "worked",
                "evidence": ["status=200 and semantic response matched"],
                "limitations": [],
            }],
            "user_input_requests": [],
            "environment": {
                "entry_points": ["http://127.0.0.1:3000"],
                "started_services": [],
                "artifacts": [],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="active-acceptance",
        task_type="feature",
        description="test",
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-001"}]}),
        encoding="utf-8",
    )
    revision = oc._acceptance_revision_key(project)

    first, first_task_id = await oc._run_acceptance_agent(project, revision)
    second, second_task_id = await oc._run_acceptance_agent(project, revision)

    assert first == second
    assert first_task_id == second_task_id
    tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_agent",
        )
    ).all()
    assert len(tasks) == 1
    assert tasks[0].input_data["work_item_contract"]["acceptance_criteria"] == [
        {"id": "AC-001"}
    ]
    assert (mac_dir / "ACCEPTANCE_AGENT_OUTPUT.json").exists()

    project.context = {**project.context, "acceptance_retry_generation": 1}
    service.session.commit()
    third, third_task_id = await oc._run_acceptance_agent(project, revision)

    assert third["result"] == first["result"]
    assert third["criteria"] == first["criteria"]
    assert third_task_id != first_task_id
    tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_agent",
        )
    ).all()
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_acceptance_agent_does_not_receive_previous_acceptance_output(orchestrator):
    service, wm, renderer = orchestrator

    class CapturingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({
                "acceptance_agent": {
                    "success": True,
                    "result": "pass",
                    "summary": "current revision executed",
                    "criteria": [{
                        "acceptance_criterion_id": "AC-001",
                        "evidence_source": "system_observable",
                        "result": "pass",
                        "method": "current public entry",
                        "expected": "works",
                        "actual": "worked now",
                        "evidence": ["current trace"],
                        "limitations": [],
                    }],
                    "user_input_requests": [],
                    "environment": {
                        "entry_points": [],
                        "started_services": [],
                        "artifacts": [],
                    },
                }
            })
            self.kwargs = None

        async def execute(self, **kwargs):
            self.kwargs = kwargs
            return await super().execute(**kwargs)

    runtime = CapturingRuntime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="acceptance-no-stale-output", task_type="feature", description="test"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-001"}]}),
        encoding="utf-8",
    )
    stale_output = mac_dir / "ACCEPTANCE_AGENT_OUTPUT.json"
    stale_output.write_text('{"summary":"stale browser timeout"}', encoding="utf-8")

    await oc._run_acceptance_agent(project, oc._acceptance_revision_key(project))

    assert runtime.kwargs is not None
    assert stale_output not in runtime.kwargs["context_files"]
    assert str(stale_output) not in runtime.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_auxiliary_agent_respects_explicit_empty_context(orchestrator):
    service, wm, renderer = orchestrator

    class CapturingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({"project_manager": {"success": True}})
            self.context_files = None

        async def execute(self, **kwargs):
            self.context_files = kwargs["context_files"]
            return await super().execute(**kwargs)

    runtime = CapturingRuntime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="empty-aux-context", task_type="feature", description="test"
    )

    await oc._execute_auxiliary_agent(
        project,
        agent_type="project_manager",
        input_data={},
        context_files=[],
    )

    assert runtime.context_files == []


def test_acceptance_ledger_reuses_active_unaffected_evidence_across_revision():
    rows = [
        {
            "acceptance_criterion_id": "AC-OLD",
            "source_type": "acceptance_agent",
            "artifact_revision": "old-revision",
        },
        {
            "acceptance_criterion_id": "AC-CURRENT",
            "source_type": "acceptance_agent",
            "artifact_revision": "current-revision",
            "verdict": "pass",
        },
        {
            "acceptance_criterion_id": "AC-CURRENT-FAIL",
            "source_type": "acceptance_agent",
            "artifact_revision": "current-revision",
            "verdict": "fail",
        },
        {
            "acceptance_criterion_id": "AC-PHONE",
            "source_type": "external",
            "artifact_revision": "old-revision",
        },
        {
            "acceptance_criterion_id": "AC-PREF",
            "source_type": "user",
            "artifact_revision": "old-revision",
        },
    ]

    filtered = AgentOrchestrator._acceptance_ledger_for_revision(
        rows, "current-revision"
    )

    assert {row["acceptance_criterion_id"] for row in filtered} == {
        "AC-OLD",
        "AC-CURRENT",
        "AC-CURRENT-FAIL",
        "AC-PHONE",
        "AC-PREF",
    }
    assert all(row["reusable_for_current_revision"] for row in filtered)
    assert next(
        row for row in filtered if row["acceptance_criterion_id"] == "AC-OLD"
    )["evidence_revision_matches"] is False


@pytest.mark.asyncio
async def test_acceptance_agent_retries_incomplete_cumulative_coverage(orchestrator):
    service, wm, renderer = orchestrator

    complete = {
        "success": True,
        "result": "pass",
        "summary": "complete black-box coverage",
        "criteria": [
            {
                "acceptance_criterion_id": ac_id,
                "evidence_source": "system_observable",
                "result": "pass",
                "method": "real entry",
                "expected": "works",
                "actual": "worked",
                "evidence": ["direct trace"],
                "limitations": [],
            }
            for ac_id in ("AC-BASE", "AC-FIX")
        ],
        "user_input_requests": [],
        "environment": {"entry_points": [], "started_services": [], "artifacts": []},
    }

    class CoverageRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({})
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            output = complete if self.calls == 2 else {
                **complete,
                "criteria": complete["criteria"][1:],
            }
            return AgentRuntimeResult(
                success=True,
                output_json=output,
                output_files=[],
                logs="",
                cost_info=None,
            )

    runtime = CoverageRuntime()
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="cumulative-acceptance",
        task_type="bug_fix",
        description="test",
    )
    project.current_state = "ACCEPTANCE_PENDING"
    project.context = {**project.context, "base_project_id": "proj_base"}
    project.bound_directory = project.workspace_path
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-BASE"}]}),
        encoding="utf-8",
    )
    iteration_dir = mac_dir / "projects" / project.id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "BUG_ANALYSIS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-FIX"}]}),
        encoding="utf-8",
    )

    output, _ = await oc._run_acceptance_agent(
        project, oc._acceptance_revision_key(project)
    )

    assert runtime.calls == 2
    assert {row["acceptance_criterion_id"] for row in output["criteria"]} == {
        "AC-BASE",
        "AC-FIX",
    }


@pytest.mark.asyncio
async def test_acceptance_retry_receives_latest_evaluator_failures(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "acceptance_agent": {
            "success": True,
            "result": "pass",
            "summary": "targeted retry passed",
            "criteria": [{
                "acceptance_criterion_id": "AC-KEYBOARD",
                "evidence_source": "system_observable",
                "result": "pass",
                "method": "real browser keyboard path",
                "expected": "works",
                "actual": "worked",
                "evidence": ["trace"],
                "limitations": [],
            }],
            "user_input_requests": [],
            "environment": {"entry_points": [], "started_services": [], "artifacts": []},
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="acceptance-retry-feedback", task_type="feature", description="test"
    )
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-KEYBOARD"}]}),
        encoding="utf-8",
    )
    evaluator = await service.create_task(
        project.id, "acceptance_evaluator", "ACCEPTANCE_PENDING", {}
    )
    await service.complete_task(evaluator.id, {
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-KEYBOARD",
            "result": "fail",
            "notes": "Enter/Space path was not executed",
        }]
    })

    await oc._run_acceptance_agent(project, oc._acceptance_revision_key(project))

    task = service.session.exec(
        select(Task)
        .where(Task.project_id == project.id, Task.agent_type == "acceptance_agent")
        .order_by(Task.created_at.desc())
    ).first()
    feedback = task.input_data["acceptance_contract_feedback"]
    assert "AC-KEYBOARD" in feedback
    assert "Enter/Space path was not executed" in feedback


@pytest.mark.asyncio
async def test_evaluator_only_config_change_does_not_invalidate_acceptance(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="acceptance-revision-scope", task_type="feature", description="test"
    )

    before = oc._acceptance_revision_key(project)
    renderer.agent_config["agents"]["acceptance_evaluator"]["runtime"] = "codex"
    after = oc._acceptance_revision_key(project)

    assert before == after

    project.context = {**project.context, "acceptance_retry_generation": 99}
    assert oc._acceptance_revision_key(project) == after


@pytest.mark.asyncio
async def test_equivalent_quality_task_does_not_force_browser_acceptance_rerun(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="acceptance-quality-scope", task_type="feature", description="test"
    )
    artifact_before = oc._acceptance_revision_key(project)
    quality_before = oc._acceptance_quality_revision(project)

    tester = await service.create_task(
        project.id, "tester", "TESTING", {"description": "verify"}
    )
    await service.complete_task(tester.id, {
        "passed": True,
        "test_evidence": [{"id": "TE-1", "result": "pass"}],
    })

    assert oc._acceptance_revision_key(project) == artifact_before
    assert oc._acceptance_quality_revision(project) != quality_before
    assert AgentOrchestrator._acceptance_evaluator_revision_key(
        artifact_before, "acceptance-task", quality_before
    ) != AgentOrchestrator._acceptance_evaluator_revision_key(
        artifact_before, "acceptance-task", oc._acceptance_quality_revision(project)
    )

    semantic_revision = oc._acceptance_quality_revision(project)
    equivalent = await service.create_task(
        project.id, "tester", "TESTING", {"description": "verify again"}
    )
    await service.complete_task(equivalent.id, {
        "passed": True,
        "test_evidence": [{"id": "TE-1", "result": "pass"}],
        "_profiling": {"elapsed_seconds": 999, "attempt_id": "different"},
    })
    assert oc._acceptance_quality_revision(project) == semantic_revision


@pytest.mark.asyncio
async def test_acceptance_evaluator_ask_user_does_not_require_final_matrix(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-external-fact", task_type="feature", description="test"
    )
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-PHONE"}]}),
        encoding="utf-8",
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    error = oc._acceptance_evaluator_id_coverage_error(project, {
        "success": True,
        "next_state_event": "ask_user",
        "completion_reason": "resolution_requested",
        "blockers": [{
            "id": "rr_phone",
            "category": "missing_information",
            "summary": "phone observation missing",
            "evidence": ["no device available"],
            "required_resolution": "report observed result",
            "preferred_resolver": "coordinator",
            "blocking": True,
            "affected_acceptance_criterion_ids": ["AC-PHONE"],
        }],
        "question_batch": {
            "batch_key": "phone",
            "questions": [{"affected_trace_ids": ["AC-PHONE"]}],
        },
    })

    assert error is None


@pytest.mark.asyncio
async def test_acceptance_evaluator_cannot_pause_user_before_internal_work(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-ordering", task_type="feature", description="test"
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    error = oc._acceptance_evaluator_id_coverage_error(project, {
        "next_state_event": "ask_user",
        "blockers": [
            {
                "blocking": True,
                "affected_acceptance_criterion_ids": ["AC-PHONE"],
            },
            {
                "blocking": True,
                "affected_acceptance_criterion_ids": ["AC-MEMORY"],
            },
        ],
        "question_batch": {
            "questions": [{"affected_trace_ids": ["AC-PHONE"]}],
        },
    })

    assert "AC-MEMORY" in error
    assert "先返回 fail" in error


@pytest.mark.asyncio
async def test_acceptance_evidence_refs_canonicalize_only_registered_id_prefixes(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="evidence-ref-canonicalization",
        task_type="feature",
        description="test",
    )
    task = await service.create_task(
        project.id, "tester", "TESTING", {"description": "test"}, 1
    )
    await service.start_task(task.id, project.workspace_path)
    await service.complete_task(
        task.id,
        {
            "success": True,
            "result": "pass",
            "test_evidence": [{"id": "TE-001", "result": "pass"}],
        },
        None,
    )

    normalized = oc._canonicalize_acceptance_test_evidence_refs(
        project,
        {
            "acceptance_matrix": [{
                "acceptance_criterion_id": "AC-001",
                "test_evidence_ids": [
                    "TE-001: test name passed",
                    "TE-UNKNOWN: invented evidence",
                ],
            }]
        },
    )

    assert normalized["acceptance_matrix"][0]["test_evidence_ids"] == [
        "TE-001",
        "TE-UNKNOWN: invented evidence",
    ]


@pytest.mark.asyncio
async def test_acceptance_manual_ac_uses_linked_user_answer_as_final_evidence(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="manual-acceptance", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-WEB-001",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }
            ]
        }),
        encoding="utf-8",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(
        tester.id,
        {
            "passed": True,
            "test_evidence": [{
                "id": "TE-WEB-MANUAL",
                "type": "manual",
                "result": "skipped",
                "acceptance_criterion_ids": ["AC-WEB-001"],
                "command_or_method": "Open the site in a phone browser",
                "expected": "Conversation survives refresh",
            }],
        },
    )
    reviewer = await service.create_task(project.id, "reviewer", "REVIEW", {})
    await service.complete_task(
        reviewer.id,
        {
            "result": "pass",
            "requirement_coverage": [{
                "acceptance_criterion_id": "AC-WEB-001",
                "status": "partially_covered",
                "evidence": ["awaiting external browser verification"],
            }],
        },
    )
    evaluation = {
        "result": "pass",
        "action": "confirm",
        "blocking_conditions": [],
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-WEB-001",
            "result": "pass",
            "test_evidence_ids": ["TE-WEB-MANUAL-USER"],
        }],
    }

    before_answer = oc._check_acceptance_coverage(project, evaluation)
    assert before_answer is not None
    assert "missing dedicated verified manual verdict" in before_answer["message"]

    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="manual-web",
        questions=[{
            "question_id": "q_manual_web",
            "kind": "blocking",
            "question": "Did the browser verification pass?",
            "allow_free_text": False,
            "options": [
                {"key": "verified_pass", "label": "Passed"},
                {"key": "verified_fail", "label": "Failed"},
                {"key": "not_verified", "label": "Not verified"},
            ],
            "affected_trace_ids": ["AC-WEB-001"],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "verified_pass",
        "Passed",
        False,
        "ou_user",
    )
    await service.resolve_question_interaction(interaction.id)

    evaluation["acceptance_matrix"][0]["manual_verification"] = {
        "status": "verified",
        "source_question_id": "q_manual_web",
        "evidence_scope": "Real mobile viewport; refresh restored all messages",
        "limitations": [],
    }
    assert oc._check_acceptance_coverage(project, evaluation) is None


@pytest.mark.asyncio
async def test_acceptance_manual_ac_rejects_prose_or_partial_verification(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="manual-partial", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-MOBILE",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }
            ]
        }),
        encoding="utf-8",
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="manual-mobile",
        questions=[{
            "question_id": "q_manual_mobile",
            "kind": "blocking",
            "question": "Did the real-device verification pass?",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": ["AC-MOBILE"],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "The server returned HTTP 200, but no physical phone was available.",
        "Partial",
        False,
        "ou_user",
    )
    await service.resolve_question_interaction(interaction.id)

    violation = oc._check_acceptance_coverage(project, {
        "result": "pass",
        "action": "confirm",
        "blocking_conditions": [],
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-MOBILE",
            "result": "pass",
            "test_evidence_ids": ["TE-MANUAL"],
            "manual_verification": {
                "status": "verified",
                "source_question_id": "q_manual_mobile",
                "evidence_scope": "Server-side HTTP check only",
                "limitations": [],
            },
        }],
    })

    assert violation is not None
    assert "missing dedicated verified manual verdict" in violation["message"]

    recommendation = {
        "next_state_event": "complete",
        "result": "pass",
        "question_batch": None,
        "blockers": [],
    }
    oc._ensure_structured_manual_acceptance_questions(project, recommendation)

    assert recommendation["next_state_event"] == "ask_user"
    questions = recommendation["question_batch"]["questions"]
    assert [item["affected_trace_ids"] for item in questions] == [["AC-MOBILE"]]
    assert {option["key"] for option in questions[0]["options"]} == {
        "verified_pass",
        "verified_fail",
        "not_verified",
        "provide_details",
    }
    assert recommendation["blockers"][0][
        "affected_acceptance_criterion_ids"
    ] == ["AC-MOBILE"]


@pytest.mark.asyncio
async def test_acceptance_evaluator_reuses_unchanged_artifact_revision(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-cache", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-001",
                "verification_method": "automated",
            }]
        }),
        encoding="utf-8",
    )
    runtime = FakeRuntime({
        "acceptance_agent": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "active acceptance passed",
            "entry_point": "http://127.0.0.1:8000",
            "criteria": [{
                "acceptance_criterion_id": "AC-001",
                "result": "pass",
                "evidence": ["browser exercised the criterion"],
            }],
            "blocking_conditions": [],
        },
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "accepted",
            "evidence": {
                "tests_passed": True,
                "coverage": 1.0,
                "review_result": "pass",
                "meets_requirements": True,
            },
            "acceptance_matrix": [{
                "acceptance_criterion_id": "AC-001",
                "requirement_ids": ["REQ-001"],
                "spec_ids": ["SPEC-001"],
                "implementation_evidence": ["implemented"],
                "test_evidence_ids": ["TE-001"],
                "review_result": "pass",
                "result": "pass",
            }],
            "blocking_conditions": [],
            "artifact_versions": {"requirements": 1},
            "recommendation": {
                "action": "confirm",
                "reason": "all covered",
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    first = await oc._evaluate_acceptance(project)
    second = await oc._evaluate_acceptance(project)

    assert first["_cached"] is False
    assert second["_cached"] is True
    tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_evaluator",
        )
    ).all()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_acceptance_evaluator_receives_cumulative_lineage_contract(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-lineage", task_type="feature", description="Test"
    )
    workspace = Path(project.workspace_path)
    mac_dir = workspace / ".mac"
    current_dir = mac_dir / "projects" / project.id
    bug_dir = mac_dir / "projects" / "proj_bug"
    current_dir.mkdir(parents=True, exist_ok=True)
    bug_dir.mkdir(parents=True, exist_ok=True)
    project.bound_directory = str(workspace)
    project.context = {**project.context, "base_project_id": "proj_bug"}
    service.session.add(project)
    service.session.commit()
    (bug_dir / "STATE.json").write_text(
        json.dumps({"context": {"base_project_id": "proj_root"}}),
        encoding="utf-8",
    )
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-ROOT"}]}),
        encoding="utf-8",
    )
    (bug_dir / "BUG_ANALYSIS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-BUG"}]}),
        encoding="utf-8",
    )
    (current_dir / "REQUIREMENTS.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-CURRENT"}]}),
        encoding="utf-8",
    )
    criteria = [
        {
            "acceptance_criterion_id": ac_id,
            "result": "pass",
            "evidence": ["direct evidence"],
        }
        for ac_id in ("AC-ROOT", "AC-BUG", "AC-CURRENT")
    ]
    matrix = [
        {
            "acceptance_criterion_id": ac_id,
            "requirement_ids": ["REQ-001"],
            "spec_ids": ["SPEC-001"],
            "implementation_evidence": ["implemented"],
            "test_evidence_ids": ["TE-001"],
            "review_result": "pass",
            "result": "pass",
        }
        for ac_id in ("AC-ROOT", "AC-BUG", "AC-CURRENT")
    ]
    runtime = FakeRuntime({
        "acceptance_agent": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "passed",
            "criteria": criteria,
            "blocking_conditions": [],
        },
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "accepted",
            "evidence": {
                "tests_passed": True,
                "coverage": 1.0,
                "review_result": "pass",
                "meets_requirements": True,
            },
            "acceptance_matrix": matrix,
            "blocking_conditions": [],
            "artifact_versions": {"requirements": 1},
            "recommendation": {"action": "confirm", "reason": "all covered"},
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    await oc._evaluate_acceptance(project)

    evaluator_task = service.session.exec(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_evaluator",
        )
        .order_by(Task.created_at.desc())
    ).first()
    assert {
        item["id"]
        for item in evaluator_task.input_data["work_item_contract"][
            "acceptance_criteria"
        ]
    } == {"AC-ROOT", "AC-BUG", "AC-CURRENT"}
    assert "acceptance_evaluator_evidence_bundle" in evaluator_task.input_data


@pytest.mark.asyncio
async def test_acceptance_evaluator_bundle_embeds_zero_tool_evidence(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="acceptance-bundle", task_type="feature", description="d"
    )
    mac = Path(project.workspace_path) / ".mac"
    (mac / "SPECS.json").write_text(json.dumps({
        "version": 3,
        "specs": [{
            "id": "SPEC-001",
            "requirement_ids": ["REQ-001"],
            "acceptance_criterion_ids": ["AC-001"],
            "affected_files": ["src/app.py"],
            "test_cases": [{"id": "TC-001"}],
        }],
    }), encoding="utf-8")
    (mac / "CODER_OUTPUT.json").write_text(json.dumps({
        "modified_files": [{"path": "src/app.py"}],
        "completion_evidence": ["implemented"],
    }), encoding="utf-8")
    (mac / "TESTER_OUTPUT.json").write_text(json.dumps({
        "passed": True,
        "test_evidence": [{"id": "TE-001", "result": "pass"}],
        "failures": [],
    }), encoding="utf-8")
    (mac / "REVIEWER_OUTPUT.json").write_text(json.dumps({
        "result": "pass",
        "issues": [],
        "requirement_coverage": [{
            "acceptance_criterion_id": "AC-001", "status": "covered"
        }],
    }), encoding="utf-8")

    bundle = oc._acceptance_evaluator_evidence_bundle(project)

    assert bundle["spec_mappings"] == [{
        "id": "SPEC-001",
        "requirement_ids": ["REQ-001"],
        "acceptance_criterion_ids": ["AC-001"],
        "affected_files": ["src/app.py"],
        "test_case_ids": ["TC-001"],
    }]
    assert bundle["tester_output"]["test_evidence"][0]["id"] == "TE-001"
    assert bundle["reviewer_output"]["result"] == "pass"


def test_acceptance_evaluator_revision_includes_prompt_contract(monkeypatch):
    digests = iter([b"first evaluator prompt", b"updated evaluator prompt"])

    monkeypatch.setattr(Path, "read_bytes", lambda _path: next(digests))

    first = AgentOrchestrator._acceptance_evaluator_revision_key("art", "task")
    second = AgentOrchestrator._acceptance_evaluator_revision_key("art", "task")

    assert first != second


@pytest.mark.asyncio
async def test_external_acceptance_evidence_invalidates_cached_evaluation(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-external-revision", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
                "acceptance_criteria": [{
                    "id": "AC-MANUAL",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }]
        }),
        encoding="utf-8",
    )
    runtime = FakeRuntime({
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "ask_user",
            "result": "needs_input",
            "summary": "manual verification required",
            "question_batch": {
                "batch_key": "manual_acceptance",
                "questions": [{
                    "question_id": "q_manual",
                    "kind": "blocking",
                    "question": "Did it pass?",
                    "reason": "Needs external observation",
                    "allow_free_text": False,
                    "options": [
                        {"key": "verified_pass", "label": "Passed"},
                        {"key": "verified_fail", "label": "Failed"},
                        {"key": "not_verified", "label": "Not verified"},
                        {"key": "provide_details", "label": "Details"},
                    ],
                    "affected_trace_ids": ["AC-MANUAL"],
                }],
            },
            "blockers": [{
                "id": "manual",
                "affected_acceptance_criterion_ids": ["AC-MANUAL"],
            }],
            "acceptance_matrix": [{
                "acceptance_criterion_id": "AC-MANUAL",
                "result": "not_verified",
            }],
            "blocking_conditions": [],
            "recommendation": {
                "action": "ask_user",
                "reason": "manual verification required",
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    first = await oc._evaluate_acceptance(project)
    assert first["_cached"] is False
    service.record_acceptance_evidence(
        project.id,
        "AC-MANUAL",
        verdict="pass",
        verification_method="external",
        evidence_scope="real browser failure and recovery flow",
        source_type="user",
        source_ref="q_manual",
        evidence=[{"answer": "verified_pass"}],
        limitations=[],
    )

    second = await oc._evaluate_acceptance(project)

    assert second["_cached"] is False
    tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "acceptance_evaluator",
        )
    ).all()
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_fresh_manual_pass_suppresses_stale_repeat_question(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-stale-question", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
                "acceptance_criteria": [{
                    "id": "AC-MANUAL",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }]
        }),
        encoding="utf-8",
    )
    service.record_acceptance_evidence(
        project.id,
        "AC-MANUAL",
        verdict="pass",
        verification_method="external",
        evidence_scope="real browser failure and recovery flow",
        source_type="user",
        source_ref="q_manual",
        evidence=[{"answer": "verified_pass"}],
        limitations=[],
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    evaluation = {
        "next_state_event": "ask_user",
        "result": "needs_input",
        "action": "ask_user",
        "question_batch": {
            "batch_key": "manual_acceptance",
            "questions": [{
                "question_id": "q_manual",
                "affected_trace_ids": ["AC-MANUAL"],
            }],
        },
        "blockers": [{
            "id": "manual",
            "affected_acceptance_criterion_ids": ["AC-MANUAL"],
        }],
        "blocking_conditions": [],
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-MANUAL",
            "result": "not_verified",
        }],
    }

    oc._overlay_manual_acceptance_evidence(project, evaluation)
    oc._reconcile_resolved_manual_acceptance(project, evaluation)

    assert evaluation["question_batch"] is None
    assert evaluation["blockers"] == []
    assert evaluation["next_state_event"] == "complete"
    assert evaluation["result"] == "pass"
    assert evaluation["action"] == "confirm"


@pytest.mark.asyncio
async def test_stale_ledger_evidence_is_not_resurrected_from_question_history(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="acceptance-stale-ledger", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
                "acceptance_criteria": [{
                    "id": "AC-MANUAL",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }]
        }),
        encoding="utf-8",
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="manual_acceptance",
        questions=[{
            "question_id": "q_manual",
            "kind": "blocking",
            "question": "Did it pass?",
            "allow_free_text": False,
            "options": [{"key": "verified_pass", "label": "Passed"}],
            "affected_trace_ids": ["AC-MANUAL"],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id, "verified_pass", "Passed", False, "ou_user"
    )
    await service.resolve_question_interaction(interaction.id)
    service.record_acceptance_evidence(
        project.id,
        "AC-MANUAL",
        verdict="pass",
        verification_method="external",
        evidence_scope="real browser",
        source_type="user",
        source_ref="q_manual",
    )
    service.invalidate_acceptance_evidence(
        project.id,
        reason="delivery behavior changed",
        criterion_ids={"AC-MANUAL"},
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    assert oc._verified_manual_acceptance_answers(project) == {}
    recommendation = {
        "next_state_event": "complete",
        "question_batch": None,
        "blockers": [],
    }
    oc._ensure_structured_manual_acceptance_questions(project, recommendation)
    assert recommendation["next_state_event"] == "ask_user"
    assert recommendation["question_batch"]["questions"][0][
        "affected_trace_ids"
    ] == ["AC-MANUAL"]


@pytest.mark.asyncio
async def test_auxiliary_work_exposes_running_then_boundary_waiting_status(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="auxiliary-posture", task_type="feature", description="Test"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.set_execution_status(project.id, "waiting_user")
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    observed: list[str] = []

    async def fake_run(project_arg, task, *args, **kwargs):
        observed.append(service.require_project(project_arg.id).execution_status)
        await service.complete_task(task.id, {"result": "pass"})
        return {"result": "pass"}

    monkeypatch.setattr(oc, "_run_auxiliary_agent", fake_run)
    await oc._execute_auxiliary_agent(
        project,
        agent_type="acceptance_evaluator",
        input_data={"mode": "acceptance_evaluation"},
    )

    assert observed == ["running"]

    project = service.require_project(project.id)
    project.current_state = "REWORK"
    service.session.commit()
    await oc._handle_human_confirmation(project, StateMachine("feature"))

    assert service.require_project(project.id).execution_status == "waiting_user"


@pytest.mark.asyncio
async def test_repair_feedback_scopes_acceptance_evidence_invalidation(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="scoped-evidence-invalidation", task_type="feature", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-UI-001", "verification_method": "manual"},
                {"id": "AC-ERROR-002", "verification_method": "manual"},
            ]
        }),
        encoding="utf-8",
    )
    keep = service.record_acceptance_evidence(
        project.id,
        "AC-UI-001",
        verdict="pass",
        verification_method="external",
        evidence_scope="browser UI",
        source_type="user",
        source_ref="q_ui",
    )
    invalidate = service.record_acceptance_evidence(
        project.id,
        "AC-ERROR-002",
        verdict="fail",
        verification_method="external",
        evidence_scope="backend stopped",
        source_type="user",
        source_ref="q_error",
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="Network error copy is not user-readable",
        blocking=True,
        recommended_rework_target="coding",
        evidence=[{
            "type": "test_failure",
            "id": "TF-AC-ERROR-002",
            "error": "Only this failure is in repair scope",
        }],
    )
    task = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {
            "work_item_contract": {
                "feedback_refs": [packet.id],
            }
        },
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    affected = oc._acceptance_criteria_affected_by_task(project, task)
    service.invalidate_acceptance_evidence(
        project.id,
        reason="repair changed affected behavior",
        criterion_ids=affected or None,
    )

    assert affected == {"AC-ERROR-002"}
    service.session.refresh(keep)
    service.session.refresh(invalidate)
    assert keep.status == "active"
    assert invalidate.status == "stale"


def test_no_change_coder_round_does_not_claim_delivery_change():
    assert not AgentOrchestrator._coder_changed_delivery_artifacts({
        "modified_files": [],
        "notes": ["No code changes were needed"],
    })
    assert not AgentOrchestrator._coder_changed_delivery_artifacts({
        "modified_files": [{"path": ".mac/CODER_OUTPUT.json"}],
    })
    assert AgentOrchestrator._coder_changed_delivery_artifacts({
        "modified_files": [{"path": "public/app.js"}],
    })


@pytest.mark.asyncio
async def test_explicit_manual_acceptance_failure_routes_to_internal_testing(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="manual-fail-route", task_type="feature", description="Test"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
                "acceptance_criteria": [{
                    "id": "AC-MOBILE",
                    "description": "Complete a conversation on a phone",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }]
        }),
        encoding="utf-8",
    )
    service.record_acceptance_evidence(
        project.id,
        "AC-MOBILE",
        verdict="fail",
        verification_method="external",
        evidence_scope="physical phone",
        source_type="user",
        source_ref="q_mobile",
        evidence=[{"answer": "verified_fail"}],
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    await oc._handle_human_confirmation(project, StateMachine("feature"))

    updated = service.require_project(project.id)
    assert updated.current_state == "TESTING"
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].recommended_rework_target == "testing"
    assert "AC-MOBILE" in feedback[0].summary


@pytest.mark.asyncio
async def test_manual_acceptance_failure_honors_coordinator_rework_target(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="manual-fail-coding", task_type="feature", description="Test"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.commit()
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "REQUIREMENTS.json").write_text(
        json.dumps({
                "acceptance_criteria": [{
                    "id": "AC-ERROR",
                    "description": "Service failures have a user-readable error",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }]
        }),
        encoding="utf-8",
    )
    service.record_acceptance_evidence(
        project.id,
        "AC-ERROR",
        verdict="fail",
        verification_method="external",
        evidence_scope="browser after backend stop",
        source_type="user",
        source_ref="q_error",
        evidence=[{"answer": "verified_fail"}],
    )
    retry_packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="ACCEPTANCE",
        source_task_id=None,
        source_round=1,
        kind="acceptance_rejection",
        failure_class="observed_behavior_failure",
        summary="用户已明确报告人工验收失败：AC-ERROR",
        blocking=True,
        recommended_rework_target="coding",
        user_comment="页面显示 Failed to fetch",
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    await oc._handle_human_confirmation(project, StateMachine("feature"))

    assert service.require_project(project.id).current_state == "CODING"
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].recommended_rework_target == "coding"


@pytest.mark.asyncio
async def test_documentation_acceptance_does_not_require_absent_testing_stage(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="doc-evidence", task_type="documentation", description="Test"
    )
    mac_path = Path(project.workspace_path) / ".mac"
    (mac_path / "DOC_OUTLINE.json").write_text(
        json.dumps({"acceptance_criteria": [{"id": "AC-DOC-001"}]}),
        encoding="utf-8",
    )
    reviewer = await service.create_task(
        project.id, "reviewer", "REVIEW", {}
    )
    await service.complete_task(
        reviewer.id,
        {
            "result": "pass",
            "requirement_coverage": [{
                "acceptance_criterion_id": "AC-DOC-001",
                "status": "covered",
                "evidence": ["AGENTS.md"],
            }],
        },
    )

    violation = oc._check_acceptance_coverage(
        project,
        {
            "result": "pending_human_review",
            "action": "confirm",
            "blocking_conditions": [],
            "acceptance_matrix": [{
                "acceptance_criterion_id": "AC-DOC-001",
                "result": "pass",
                "test_evidence_ids": [],
            }],
        },
    )

    assert violation is None


@pytest.mark.asyncio
async def test_tool_progress_uses_project_labeled_content_card(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator

    class ProgressBridge:
        def __init__(self):
            self.cards = []
            self.updated = []

        async def _send_interactive(self, receive_id, card, receive_id_type=None):
            self.cards.append({
                "receive_id": receive_id,
                "card": card,
                "receive_id_type": receive_id_type,
            })
            return "om_progress"

        async def update_card(self, message_id, card):
            self.updated.append((message_id, card))

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
    task = type("TaskStub", (), {"id": "task_progress", "agent_type": "coder"})()
    callback = oc._make_progress_callback(project, task)
    assert callback is not None
    await callback("tool", json.dumps({"name": "Read", "input": {"file": "a.py"}}))

    rendered_create = json.dumps(bridge.cards[0]["card"], ensure_ascii=False)
    assert "[目录绑定项目] 编码 Agent · 正在执行" in rendered_create
    assert "读取项目资料" in rendered_create
    assert "a.py" not in rendered_create

    await callback("tool", json.dumps({"name": "Bash", "input": {"command": "pytest"}}))

    assert len(bridge.cards) == 1
    assert len(bridge.updated) == 1
    assert bridge.updated[0][0] == "om_progress"
    rendered_update = json.dumps(bridge.updated[0][1], ensure_ascii=False)
    assert "执行项目内检查或构建" in rendered_update
    assert "pytest" not in rendered_update

    await callback(
        "thinking",
        "private chain of thought /Users/example and final JSON {secret: true}",
    )
    rendered_thinking = json.dumps(bridge.updated[-1][1], ensure_ascii=False)
    assert "正在分析当前阶段并整理结果" in rendered_thinking
    assert "private chain of thought" not in rendered_thinking
    assert "/Users/example" not in rendered_thinking
    assert "secret" not in rendered_thinking

    await oc._finalize_progress_card(project, task, succeeded=True)
    projection = service.get_card_projection("task:task_progress:progress")
    assert projection is not None
    assert projection.lifecycle_status == "completed"
    rendered_final = json.dumps(bridge.updated[-1][1], ensure_ascii=False)
    assert "编码 Agent · 已完成" in rendered_final


@pytest.mark.asyncio
async def test_policy_skipped_acceptance_schedules_followup_without_recursive_tick(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="internal-doc-change",
        task_type="feature",
        description="documentation-only",
    )
    project.current_state = "ACCEPTANCE_PENDING"
    project.context = {
        **project.context,
        "delivery_policy": {
            "required_stages": ["coding", "testing"],
            "reason": "internal documentation change",
        },
    }
    service.session.add(project)
    service.session.commit()
    recursive_tick_called = False
    scheduled: list[str] = []

    async def forbidden_tick(project_id):
        nonlocal recursive_tick_called
        recursive_tick_called = True

    monkeypatch.setattr(oc, "tick", forbidden_tick)
    monkeypatch.setattr(oc, "_schedule_followup_tick", scheduled.append)

    await oc._handle_human_confirmation(project, StateMachine(project.task_type))

    assert recursive_tick_called is False
    assert scheduled == [project.id]
    assert service.require_project(project.id).current_state == "MERGING"


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
        },
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "产物覆盖合同",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
        },
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
async def test_orchestrator_strict_mode_waits_for_confirmation(orchestrator):
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
        approval_mode="strict",
    )

    await oc.tick(project.id)  # PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING
    await oc.tick(project.id)  # create pending approval

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # Should have created a pending approval
    status = service.get_project_status(project.id)
    assert status["pending_approval"] is not None
    assert status["pending_approval"]["type"] == "confirmation"


@pytest.mark.asyncio
async def test_normal_mode_uses_codex_coordinator_instead_of_waiting(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        },
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "需求产物覆盖当前合同",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="ai-first",
        task_type="feature",
        description="实现 config-validator CLI",
        approval_mode="normal",
    )

    await oc.tick(project.id)
    assert service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "PLANNING_ARCHITECTURE"
    approvals = service.session.exec(
        select(Approval).where(Approval.project_id == project.id)
    ).all()
    assert approvals[-1].status == "responded"
    assert approvals[-1].responded_by == "system_auto"
    coordinator_tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "conversation_coordinator",
        )
    ).all()
    assert coordinator_tasks


@pytest.mark.asyncio
async def test_stale_coordinator_confirmation_is_discarded_after_state_change(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="stale-coordinator-confirmation",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    project.current_state = "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    service.session.commit()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    async def move_project_while_coordinator_thinks(*args, **kwargs):
        current = service.require_project(project.id)
        current.current_state = "PLANNING_IMPLEMENTATION_SPECS"
        service.session.commit()
        return "confirm"

    monkeypatch.setattr(
        oc,
        "_choose_action_with_coordinator",
        move_project_while_coordinator_thinks,
    )

    await oc._handle_human_confirmation(
        service.require_project(project.id), StateMachine("feature")
    )

    assert service.require_project(project.id).current_state == (
        "PLANNING_IMPLEMENTATION_SPECS"
    )
    approval = service.session.exec(
        select(Approval).where(Approval.project_id == project.id)
    ).one()
    assert approval.status == "pending"
    assert approval.response is None


@pytest.mark.asyncio
async def test_coordinator_rejection_becomes_next_agent_feedback(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "action": "reject",
            "reason": "规格要求全量回归，与已确认的增量验证策略冲突",
            "evidence_refs": ["ctx_incremental_verification"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="coordinator-reject-feedback",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )

    project.current_state = "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    service.session.add(project)
    service.session.commit()
    action = await oc._choose_action_with_coordinator(
        project,
        project.current_state,
        "请确认实现计划",
        [
            {"action": "confirm", "label": "确认", "next_state": "CODING"},
            {
                "action": "reject",
                "label": "驳回",
                "next_state": "PLANNING_IMPLEMENTATION_SPECS",
            },
        ],
    )

    assert action == "reject"
    packets = service.get_open_feedback_packets(project.id)
    assert len(packets) == 1
    assert packets[0].kind == "coordinator_rejection"
    assert packets[0].recommended_rework_target == "specs"
    assert "增量验证" in packets[0].summary

    project.current_state = "PLANNING_IMPLEMENTATION_SPECS"
    service.session.add(project)
    service.session.commit()
    await oc._execute_agent(project, StateMachine(project.task_type))
    rerun = service.session.exec(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.agent_type == "spec_writer",
        )
        .order_by(Task.created_at.desc())
    ).first()
    assert rerun is not None
    assert rerun.input_data["open_feedback"][0]["feedback_id"] == packets[0].id
    assert rerun.input_data["work_item_contract"]["feedback_routes"][0][
        "target_stage"
    ] == "specs"


@pytest.mark.asyncio
async def test_rework_coordinator_route_carries_loop_cause_to_new_owner(
    orchestrator,
):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "action": "to_requirements",
            "reason": "The repeated verification loop exposes a contract defect",
            "evidence_refs": ["TESTER_OUTPUT.json"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="rework-causal-route",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    project.current_state = "REWORK"
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=2,
        kind="quality_failure",
        summary="The test verdict repeatedly contradicts its evidence",
        blocking=True,
        failure_class="test_bug",
        recommended_rework_target="testing",
    )
    project.context = {
        **project.context,
        "loop_supervisor": {
            "signature": FailureRouter.signature(packet),
            "prohibited_default_target": "testing",
        },
    }
    service.session.add(project)
    service.session.commit()

    action = await oc._choose_action_with_coordinator(
        project,
        "REWORK",
        "choose a rework owner",
        StateMachine(project.task_type).rework_options(),
    )

    assert action == "to_requirements"
    refreshed = service.require_project(project.id)
    assert refreshed.context["routed_feedback_ids_by_state"][
        "PLANNING_REQUIREMENTS"
    ] == [packet.id]
    retry = json.loads(
        refreshed.context["agent_retry_feedback"]["PLANNING_REQUIREMENTS"]
    )
    assert retry["causal_feedback_ids"] == [packet.id]


@pytest.mark.asyncio
async def test_upstream_artifact_revision_invalidates_old_loop_supervisor(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="artifact-loop-reset",
        task_type="feature",
        description="test",
    )
    project.context = {
        **project.context,
        "loop_supervisor": {
            "signature": "old-contract-loop",
            "prohibited_default_target": "coding",
        },
    }
    service.session.add(project)
    service.session.commit()

    changed = await oc._invalidate_loop_supervisor_after_artifact_revision(
        project,
        artifact_id="implementation_specs",
        previous_version=27,
        new_version=28,
    )

    assert changed is True
    assert "loop_supervisor" not in service.require_project(project.id).context
    events = service.get_project_events(project.id, limit=10)
    assert any(
        event.message == "Cleared LoopSupervisor after upstream artifact revision"
        and event.meta["previous_version"] == 27
        and event.meta["new_version"] == 28
        for event in events
    )


@pytest.mark.asyncio
async def test_confirmation_coordinator_receives_owner_feedback_causality(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="approval-causality",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    service.session.commit()

    prior = await service.create_task(
        project.id,
        "conversation_coordinator",
        "REQUIREMENT_CONFIRM_PENDING",
        {"description": "prior rejection"},
    )
    await service.complete_task(prior.id, {
        "success": True,
        "action": "reject",
        "reason": "退役旧的流程状态验收项，并留下墓碑",
    })
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="REQUIREMENT_CONFIRM_PENDING",
        source_task_id=prior.id,
        source_round=1,
        kind="coordinator_rejection",
        summary="退役旧的流程状态验收项，并留下墓碑",
        blocking=True,
        recommended_rework_target="requirements",
    )
    owner = await service.create_task(
        project.id,
        "requirements_analyst",
        "PLANNING_REQUIREMENTS",
        {"description": "apply rejection"},
    )
    await service.complete_task(owner.id, {
        "success": True,
        "next_state_event": "complete",
        "addressed_feedback": [{
            "feedback_id": packet.id,
            "status": "fixed",
            "explanation": "已写入退役墓碑",
        }],
        "completion_evidence": ["旧验收项已从 active 合同移除"],
    })

    captured: dict = {}

    async def capture_auxiliary(*args, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "action": "confirm",
            "reason": "修订有完整因果依据",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
        }

    oc._execute_auxiliary_agent = capture_auxiliary
    action = await oc._choose_action_with_coordinator(
        project,
        project.current_state,
        "请确认需求理解",
        [
            {"action": "confirm", "label": "确认", "next_state": "PLANNING_ARCHITECTURE"},
            {"action": "reject", "label": "驳回", "next_state": "PLANNING_REQUIREMENTS"},
        ],
    )

    assert action == "confirm"
    causal = captured["input_data"]["approval_decision_context"]
    assert causal["owner_task_id"] == owner.id
    assert causal["owner_result"]["addressed_feedback"][0][
        "feedback_id"
    ] == packet.id
    assert causal["active_feedback"][0]["summary"].startswith("退役旧的流程")
    assert packet.id in captured["extra_variables"]["approval_decision_context"]


@pytest.mark.asyncio
async def test_specs_confirmation_uses_owner_contract_and_excludes_unassigned_feedback(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="approval-owner-contract",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    project.current_state = "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    service.session.commit()
    assigned = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_IMPLEMENTATION_SPECS",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="compile the current architecture",
        blocking=True,
        recommended_rework_target="specs",
    )
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="older testing-only correction",
        blocking=True,
        recommended_rework_target="testing",
    )
    owner = await service.create_task(
        project.id,
        "spec_writer",
        "PLANNING_IMPLEMENTATION_SPECS",
        {
            "description": "compile specs",
            "open_feedback": [{"feedback_id": assigned.id}],
            "work_item_contract": {
                "relevant_facts": [],
                "acceptance_criteria": [{"id": "AC-CURRENT"}],
                "regression_acceptance_criteria": [{"id": "AC-REGRESSION"}],
                "feedback_refs": [assigned.id],
            },
        },
    )
    await service.complete_task(owner.id, {
        "success": True,
        "next_state_event": "complete",
        "addressed_feedback": [{
            "feedback_id": assigned.id,
            "status": "fixed",
            "explanation": "compiled",
        }],
    })
    captured: dict[str, Any] = {}

    async def capture_auxiliary(*args, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "action": "confirm",
            "reason": "contract is consistent",
            "evidence_refs": [".mac/SPECS.json"],
        }

    oc._execute_auxiliary_agent = capture_auxiliary
    action = await oc._choose_action_with_coordinator(
        project,
        project.current_state,
        "confirm specs",
        [
            {"action": "confirm", "label": "确认", "next_state": "CODING"},
            {"action": "reject", "label": "驳回", "next_state": "PLANNING_IMPLEMENTATION_SPECS"},
        ],
    )

    assert action == "confirm"
    context = captured["input_data"]["approval_decision_context"]
    assert context["owner_contract"]["acceptance_criterion_ids"] == ["AC-CURRENT"]
    assert context["owner_contract"]["regression_acceptance_criterion_ids"] == [
        "AC-REGRESSION"
    ]
    assert [item["feedback_id"] for item in context["active_feedback"]] == [
        assigned.id
    ]
    work_item = captured["input_data"]["work_item_contract"]
    assert work_item["relevant_facts"] == []
    assert [item["id"] for item in work_item["regression_acceptance_criteria"]] == [
        "AC-REGRESSION"
    ]


@pytest.mark.asyncio
async def test_coordinator_cannot_confirm_specs_with_unmapped_active_ac(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "已把流程条件写入 definition_of_done",
            "evidence_refs": [".mac/SPECS.json"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="unmapped-ac",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    mac = Path(project.workspace_path) / ".mac"
    (mac / "REQUIREMENTS.json").write_text(json.dumps({
        "acceptance_criteria": [
            {"id": "AC-PRODUCT-001"},
            {"id": "AC-PROCESS-001"},
        ],
    }), encoding="utf-8")
    (mac / "SPECS.json").write_text(json.dumps({
        "specs": [{
            "id": "SPEC-001",
            "acceptance_criterion_ids": ["AC-PRODUCT-001"],
            "definition_of_done": [
                "AC-PROCESS-001 已移入 Orchestrator 完成条件"
            ],
        }],
    }), encoding="utf-8")
    project.current_state = "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    service.session.add(project)
    service.session.commit()

    action = await oc._choose_action_with_coordinator(
        project,
        project.current_state,
        "请确认实现计划",
        [
            {"action": "confirm", "label": "确认", "next_state": "CODING"},
            {
                "action": "reject",
                "label": "驳回",
                "next_state": "PLANNING_IMPLEMENTATION_SPECS",
            },
        ],
    )

    assert action == "reject"
    packet = service.get_open_feedback_packets(project.id)[0]
    assert packet.recommended_rework_target == "specs"
    assert "AC-PROCESS-001" in packet.summary
    assert "Requirements 必须同步移除或退役" in packet.summary


def test_coordinator_upstream_route_transfers_resolution_to_target_agent(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = asyncio.run(service.create_project(
        name="route-resolution-transfer",
        task_type="feature",
        description="test",
    ))
    project.context = {
        **project.context,
        "_coordinator_route": {
            "event": "route_to_coding",
            "target_stage": "coding",
            "origin_state": "TESTING",
            "target_state": "CODING",
            "causal_feedback_ids": ["fb_route_cause"],
            "resolution_instruction": (
                "发布当前交付到受管入口并回填 running revision"
            ),
        },
    }
    service.session.add(project)
    service.session.commit()

    event = oc._consume_coordinator_route(project)

    assert event == "route_to_coding"
    payload = json.loads(project.context["agent_retry_feedback"]["CODING"])
    assert payload["route_target_stage"] == "coding"
    assert "发布当前交付" in payload["coordinator_resolution"]
    assert "必须实际执行" in payload["instruction"]
    assert project.context["routed_feedback_ids_by_state"]["CODING"] == [
        "fb_route_cause"
    ]


@pytest.mark.asyncio
async def test_coordinator_escalation_carries_causal_feedback_across_stage_owner(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="route-causal-feedback",
        task_type="feature",
        description="test",
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=3,
        kind="test_failure",
        summary="The implementation contract is ambiguous after repeated fixes",
        blocking=True,
        failure_class="observed_behavior_failure",
        recommended_rework_target="coding",
    )
    project.context = {
        **project.context,
        "_coordinator_route": {
            "event": "to_specs",
            "target_stage": "specs",
            "origin_state": "REWORK",
            "target_state": "PLANNING_IMPLEMENTATION_SPECS",
            "causal_feedback_ids": [packet.id],
            "resolution_instruction": "Clarify the contract before coding again",
        },
    }
    service.session.add(project)
    service.session.commit()

    assert oc._consume_coordinator_route(project) == "to_specs"
    project.current_state = "PLANNING_IMPLEMENTATION_SPECS"
    project.previous_state = "REWORK"
    service.session.add(project)
    service.session.commit()

    await oc._execute_agent(project, StateMachine(project.task_type))

    task = service.session.exec(
        select(Task)
        .where(
            Task.project_id == project.id,
            Task.agent_type == "spec_writer",
        )
        .order_by(Task.created_at.desc())
    ).first()
    assert task is not None
    assert task.input_data["open_feedback"][0]["feedback_id"] == packet.id
    assert packet.id in task.input_data["work_item_contract"]["feedback_refs"]
    refreshed = service.require_project(project.id)
    assert "routed_feedback_ids_by_state" not in refreshed.context


@pytest.mark.asyncio
async def test_legacy_coordinator_rejection_is_backfilled_before_rerun(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="legacy-coordinator-reject",
        task_type="feature",
        description="test",
    )
    coordinator = await service.create_task(
        project.id,
        "conversation_coordinator",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
        {"description": "approve"},
    )
    await service.complete_task(coordinator.id, {
        "success": True,
        "action": "reject",
        "reason": "必须把增量验证策略写进 Spec",
        "evidence_refs": ["ctx_incremental"],
    })

    await oc._recover_missing_coordinator_rejection_feedback(
        project, "PLANNING_IMPLEMENTATION_SPECS"
    )
    packets = service.get_open_feedback_packets(project.id)
    assert len(packets) == 1
    assert packets[0].source_task_id == coordinator.id
    assert packets[0].recommended_rework_target == "specs"

    await oc._recover_missing_coordinator_rejection_feedback(
        project, "PLANNING_IMPLEMENTATION_SPECS"
    )
    assert len(service.get_open_feedback_packets(project.id)) == 1


@pytest.mark.asyncio
async def test_normal_mode_coordinator_answers_internal_input(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "disposition": "answered",
            "answer": "沿用已确认架构中的接口契约",
            "reason": "架构产物已有唯一答案",
            "evidence_refs": [".mac/ARCHITECTURE.json"],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="input-ai-first",
        task_type="feature",
        description="实现 CLI",
        approval_mode="normal",
    )
    project.current_state = "CLARIFICATION_PENDING"
    service.session.commit()

    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "CODING"
    status = service.get_project_status(project.id)
    assert status["pending_approval"] is None


@pytest.mark.asyncio
async def test_normal_mode_auto_routes_evidence_backed_rework(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="rework-ai-first",
        task_type="feature",
        description="实现 Todo CLI",
        approval_mode="normal",
    )
    project.current_state = "REWORK"
    service.session.commit()
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        failure_class="implementation_bug",
        summary="实现不符合测试合同",
        blocking=True,
        recommended_rework_target="coding",
        evidence=[{"test": "test_add", "result": "fail"}],
    )

    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "CODING"
    assert service.get_project_status(project.id)["pending_approval"] is None


@pytest.mark.asyncio
async def test_loop_supervisor_stops_third_blind_rework_route(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "action": "to_testing",
            "reason": "相同 coding 路径已无进展，先由独立验证重新界定剩余失败",
            "evidence_refs": ["feedback:AC-LOOP"],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="loop-supervisor",
        task_type="feature",
        description="修复循环问题",
        approval_mode="normal",
    )
    project.current_state = "REWORK"
    service.session.commit()
    for status in ("resolved", "resolved", "open"):
        await service.create_feedback_packet(
            project_id=project.id,
            source_stage="REVIEW",
            source_task_id=None,
            source_round=1,
            kind="quality_failure",
            failure_class="implementation",
            summary="same structured failure",
            blocking=True,
            recommended_rework_target="coding",
            evidence=[{"affected_acceptance_criterion_ids": ["AC-LOOP"]}],
            status=status,
        )

    await oc.tick(project.id)

    refreshed = service.require_project(project.id)
    assert refreshed.current_state == "TESTING"
    assert refreshed.context["loop_supervisor"]["occurrences"] == 3
    coordinator_tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.agent_type == "conversation_coordinator",
        )
    ).all()
    assert coordinator_tasks


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
    assert "retry" in actions
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

    class RecordingFailureRuntime(FakeRuntime):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.prompts = []
            self.system_prompts = []

        async def execute(self, **kwargs):
            self.prompts.append(kwargs["task_prompt"])
            self.system_prompts.append(kwargs["system_prompt"])
            return await super().execute(**kwargs)

    runtime = RecordingFailureRuntime({
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
    assert len([task for task in tasks if task.agent_type == "requirements_analyst"]) == 2
    assert len([
        task for task in tasks if task.agent_type == "conversation_coordinator"
    ]) == 1
    assert "上一次技术校验反馈" not in runtime.system_prompts[0]
    assert "fake runtime" in runtime.system_prompts[1]
    assert all(len(prompt) < 160 for prompt in runtime.prompts)


@pytest.mark.asyncio
async def test_non_retryable_upstream_failure_skips_automatic_retry(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": False,
            "next_state_event": "fail",
            "_runtime_logs": "API error: upstream rejected request",
            "_runtime_failure": {
                "domain": "upstream_api",
                "status_code": 402,
                "retryable": False,
                "suggested_resolver": "coordinator",
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    oc.settings.max_agent_retries = 5
    project = await service.create_project(
        name="non-retryable-upstream",
        task_type="feature",
        description="d",
        approval_mode="normal",
    )

    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS_RECOVERY_PENDING"
    tasks = service.session.exec(
        select(Task).where(Task.project_id == project.id)
    ).all()
    assert len([
        task for task in tasks
        if task.agent_type == "requirements_analyst"
    ]) == 1
    reports = service.get_task_failure_history(
        project.id,
        agent_type="requirements_analyst",
        state="PLANNING_REQUIREMENTS",
    )
    assert reports[-1].suggested_resolver == "coordinator"
    assert updated.context["last_agent_failure"]["failure"]["retryable"] is False
    pending = service.get_project_status(project.id)["pending_approval"]
    assert pending["recovery"] == {
        "domain": "upstream_api",
        "status_code": 402,
        "retryable": False,
        "suggested_resolver": "coordinator",
    }


@pytest.mark.asyncio
async def test_tick_until_stable_drives_next_agent_and_stops_at_user_boundary():
    project = SimpleNamespace(
        id="proj_1",
        task_type="feature",
        status="active",
        current_state="CODING",
        execution_status="running",
    )
    service = Mock()
    service.require_project.return_value = project
    service.session.expire_all = Mock()
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.project_service = service

    async def advance(_project_id: str) -> None:
        if project.current_state == "CODING":
            project.current_state = "TESTING"
        elif project.current_state == "TESTING":
            project.current_state = "TESTING_CONFIRM_PENDING"
            project.execution_status = "waiting_user"

    orchestrator.tick = AsyncMock(side_effect=advance)

    await orchestrator.tick_until_stable(project.id)

    # The new human state must receive one idempotent tick to create/resolve
    # its approval. Only then does an unchanged state prove a stable boundary.
    assert orchestrator.tick.await_count == 3
    assert project.current_state == "TESTING_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_tick_until_stable_reschedules_when_bounded_drive_is_exhausted():
    project = SimpleNamespace(
        id="proj_1",
        task_type="feature",
        status="active",
        current_state="CODING",
        execution_status="running",
    )
    scheduled: list[str] = []
    service = Mock()
    service.require_project.return_value = project
    service.session.expire_all = Mock()
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.project_service = service
    orchestrator.feishu_bridge = object()
    orchestrator.background_scheduler = scheduled.append
    orchestrator._background_ticks = set()
    orchestrator._has_immediate_work = Mock(return_value=True)

    async def advance(_project_id: str) -> None:
        project.current_state = (
            "TESTING" if project.current_state == "CODING" else "REVIEW"
        )

    orchestrator.tick = AsyncMock(side_effect=advance)

    await orchestrator.tick_until_stable(project.id, max_ticks=2)

    assert orchestrator.tick.await_count == 2
    assert project.current_state == "REVIEW"
    assert scheduled == [project.id]


@pytest.mark.asyncio
async def test_agent_completion_schedules_followup_for_persistent_service():
    oc = object.__new__(AgentOrchestrator)
    oc.feishu_bridge = object()
    oc.project_service = Mock()
    oc.project_service.session.expire_all = Mock()
    oc._has_immediate_work = Mock(return_value=True)
    oc._background_ticks = set()
    oc.tick = AsyncMock()

    oc._schedule_followup_tick("proj_1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    oc.tick.assert_awaited_once_with("proj_1")
    assert not oc._background_ticks


def test_followup_uses_process_scheduler_when_callback_loop_is_ephemeral():
    scheduled: list[str] = []
    oc = object.__new__(AgentOrchestrator)
    oc.feishu_bridge = object()
    oc.project_service = Mock()
    oc.project_service.session.expire_all = Mock()
    oc._has_immediate_work = Mock(return_value=True)
    oc.background_scheduler = scheduled.append
    oc._background_ticks = set()

    oc._schedule_followup_tick("proj_durable")

    assert scheduled == ["proj_durable"]
    assert not oc._background_ticks


def test_followup_is_coalesced_inside_tick_until_stable_driver():
    scheduled: list[str] = []
    oc = object.__new__(AgentOrchestrator)
    oc.feishu_bridge = object()
    oc.project_service = Mock()
    oc.project_service.session.expire_all = Mock()
    oc._has_immediate_work = Mock(return_value=True)
    oc.background_scheduler = scheduled.append
    oc._background_ticks = set()
    oc._drive_depth = 1

    oc._schedule_followup_tick("proj_driven")

    assert scheduled == []
    assert not oc._background_ticks


@pytest.mark.asyncio
async def test_concurrent_ticks_serialized(orchestrator):
    service, wm, renderer = orchestrator

    planner_call_count = 0

    class CountingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            nonlocal planner_call_count
            if kwargs.get("agent_type") in {"planner", "requirements_analyst"}:
                planner_call_count += 1
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
    assert planner_call_count == 1
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"


@pytest.mark.asyncio
async def test_tick_refreshes_project_snapshot_after_waiting_for_lock(tmp_path):
    """A queued tick must not dispatch from a pre-lock identity-map snapshot."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-tick.db'}")
    SQLModel.metadata.create_all(engine)
    lock_manager = ProjectLockManager()

    with Session(engine) as writer_session, Session(engine) as queued_session:
        writer = ProjectService(writer_session)
        queued = ProjectService(queued_session)
        project = await writer.create_project(
            name="stale-tick",
            task_type="feature",
            description="test",
            approval_mode="strict",
        )

        # Simulate a request that loaded the execution state before another
        # event-loop tick advanced the durable project state.
        assert queued.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"
        await writer.transition_state(project.id, "complete", triggered_by="agent")

        call_count = 0

        class CountingRuntime(FakeRuntime):
            async def execute(self, **kwargs):
                nonlocal call_count
                call_count += 1
                return await super().execute(**kwargs)

        oc = AgentOrchestrator(
            queued,
            WorktreeManager(Path(queued.settings.projects_dir)),
            CountingRuntime({}),
            PromptRenderer(get_agent_config()),
            lock_manager=lock_manager,
        )

        await oc.tick(project.id)

        queued.session.expire_all()
        refreshed = queued.require_project(project.id)
        assert refreshed.current_state == "REQUIREMENT_CONFIRM_PENDING"
        assert call_count == 0
        approval = queued.session.exec(
            select(Approval).where(
                Approval.project_id == project.id,
                Approval.status == "pending",
            )
        ).first()
        assert approval is not None


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
        agent_type="requirements_analyst",
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
            "acceptance_criteria": [{
                "id": "AC-1",
                "verification_method": "automated_test",
                "evidence_source": "system_observable",
            }],
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
            "acceptance_criteria": [{
                "id": "AC-1",
                "verification_method": "automated_test",
                "evidence_source": "system_observable",
            }],
        },
        "coder": {"success": True, "next_state_event": "complete"},
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "产物覆盖合同",
            "evidence_refs": [".mac/BUG_ANALYSIS.json"],
        },
            "tester": {
                "success": True,
                "next_state_event": "complete",
                "passed": True,
                "test_evidence": [{
                    "id": "TE-1",
                    "type": "unit",
                    "result": "pass",
                    "acceptance_criterion_ids": ["AC-1"],
                }],
                "uncovered_acceptance_criterion_ids": [],
                "failures": [],
            },
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
                "result": "pass",
                "issues": [],
                "requirement_coverage": [{
                    "acceptance_criterion_id": "AC-1",
                    "status": "covered",
                    "evidence": ["TE-1"],
                }],
                "stale_artifact_ids": [],
            },
        "acceptance_agent": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "acceptance passed",
            "entry_point": "local test runtime",
            "criteria": [{
                "acceptance_criterion_id": "AC-1",
                "result": "pass",
                "evidence": ["TE-1"],
            }],
            "blocking_conditions": [],
        },
        "acceptance_evaluator": {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "summary": "accepted",
            "evidence": {
                "tests_passed": True,
                "coverage": 1.0,
                "review_result": "pass",
                "meets_requirements": True,
            },
            "acceptance_matrix": [{
                "acceptance_criterion_id": "AC-1",
                "requirement_ids": [],
                "spec_ids": [],
                "implementation_evidence": ["coder completed"],
                "test_evidence_ids": ["TE-1"],
                "review_result": "pass",
                "result": "pass",
            }],
            "blocking_conditions": [],
            "artifact_versions": {},
            "recommendation": {"action": "confirm", "reason": "all covered"},
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
    assert updated.context["last_agent_failure"]["agent"] == "requirements_analyst"
    resumable = updated.context.get("_resumable_agent_worktree")
    assert resumable is not None
    assert resumable["agent_type"] == "requirements_analyst"
    assert resumable["state"] == "PLANNING_REQUIREMENTS"
    assert Path(resumable["worktree_path"]).exists()
    assert not service._running_tasks(project.id)
    pending = service.get_project_status(project.id)["pending_approval"]
    assert pending is not None
    recovery_approval = service.get_approval(pending["approval_id"])
    assert recovery_approval.state == "PLANNING_REQUIREMENTS_RECOVERY_PENDING"
    assert {option["action"] for option in pending["options"]} == {"retry", "cancel"}

    await service.transition_state(project.id, "retry", triggered_by="user")
    retried = service.get_project(project.id)
    assert retried.current_state == "PLANNING_REQUIREMENTS"
    assert retried.context["retry_counts"]["PLANNING_REQUIREMENTS"] == 0
    assert "last_agent_failure" not in retried.context


@pytest.mark.asyncio
async def test_recovery_card_details_show_failed_agent_not_last_completed(orchestrator):
    """When coder fails into CODING_RECOVERY_PENDING, the card details must
    label the failed coder, not the most recently *completed* task (which
    would be the planner, misleading the user into thinking the planner
    failed)."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="coder-failure",
        task_type="feature",
        description="coder crashes",
        approval_mode="normal",
    )
    # Planner completed successfully; coder task failed and drove the project
    # into RECOVERY_PENDING. This mirrors what _handle_agent_failure persists.
    planner_task = await service.create_task(
        project.id, "planner", "PLANNING_REQUIREMENTS", {"summary": "ok"},
    )
    planner_task.status = "completed"
    coder_task = await service.create_task(
        project.id, "coder", "CODING", {"error": "boom"},
    )
    coder_task.status = "failed"
    service.session.commit()
    project.current_state = "CODING_RECOVERY_PENDING"
    project.context = {
        **project.context,
        "last_agent_failure": {
            "state": "CODING",
            "agent": "coder",
            "task_id": "task_failed_coder",
            "attempts": 3,
            "error": "API Error: 402 Insufficient Balance",
        },
    }
    service.session.commit()

    details = oc._build_details_for_state(project, "CODING_RECOVERY_PENDING")

    # "生成者" must NOT surface here - no artifact was produced. Surfacing the
    # planner (the last *completed* task) would mislabel the failure.
    assert "生成者" not in details
    failed_label = details.get("失败 Agent")
    assert failed_label == "编码 Agent"


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
async def test_inconsistent_tester_pass_becomes_quality_rework_not_task_failure(
    orchestrator,
):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "tester": {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
            "failure_class": "none",
            "recommended_rework_target": "none",
            "test_files": [],
            "test_evidence": [{
                "id": "TE-visible",
                "spec_id": "SPEC-1",
                "acceptance_criterion_ids": ["AC-1"],
                "feedback_ids": [],
                "type": "e2e",
                "command_or_method": "real UI",
                "result": "fail",
                "expected": "one visible card",
                "actual": "three visible cards",
            }],
            "uncovered_acceptance_criterion_ids": [],
            "environment": {},
            "coverage": 1.0,
            "failures": [{
                "id": "FAIL-1",
                "test": "visible tool activity",
                "error": "duplicate cards",
                "blocking": True,
            }],
            "report": "真实入口仍有重复卡片",
            "notes": [],
            "verified_feedback_ids": [],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="quality-normalization",
        task_type="feature",
        description="d",
        approval_mode="normal",
    )
    project.current_state = "TESTING"
    service.session.commit()

    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state != "TESTING_INPUT_PENDING"
    assert updated.current_state == "REWORK"
    tasks = service.get_project_tasks(project.id)
    assert len(tasks) == 1
    assert tasks[0].status == "completed"
    assert tasks[0].output_data["passed"] is False
    assert tasks[0].output_data["next_state_event"] == "fail"
    assert not [task for task in tasks if task.status == "failed"]
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].summary == "真实入口仍有重复卡片"


@pytest.mark.asyncio
async def test_tester_empty_fail_verdict_is_reconciled_from_passing_evidence(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="contradictory-tester-verdict", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-AUTO",
                "verification_method": "automated_test",
            }]
        }),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "test_evidence": [{
            "id": "TE-AUTO",
            "spec_id": "SPEC-AUTO",
            "acceptance_criterion_ids": ["AC-AUTO"],
            "feedback_ids": [],
            "type": "unit",
            "command_or_method": "pytest -q",
            "expected": "pass",
            "actual": "1 passed",
            "result": "pass",
        }],
        "uncovered_acceptance_criterion_ids": [],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest -q"}}]
    )

    assert "complete passing evidence" in reason
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True
    assert output["failure_class"] == "none"
    assert oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        runtime_tool_calls=[{"name": "Bash", "input": {"command": "pytest"}}],
    ) is None
    assert output["recommended_rework_target"] == "none"


@pytest.mark.asyncio
async def test_iteration_regression_only_skip_does_not_fail_current_delivery(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="bounded-regression-verdict", task_type="feature", description="d"
    )
    project.task_type = "iteration"
    project.bound_directory = project.workspace_path
    project.current_state = "TESTING"
    service.session.commit()
    workspace_mac = Path(project.workspace_path) / ".mac"
    current_mac = workspace_mac / "projects" / project.id
    current_mac.mkdir(parents=True)
    (current_mac / "ITERATION_BRIEF.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-CURRENT",
                "verification_method": "automated_test",
            }],
        }),
        encoding="utf-8",
    )
    (workspace_mac / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-OLD-MANUAL",
                "verification_method": "manual",
            }],
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "test_evidence": [
            {
                "id": "TE-CURRENT",
                "spec_id": "SPEC-CURRENT",
                "acceptance_criterion_ids": ["AC-CURRENT"],
                "feedback_ids": [],
                "type": "unit",
                "command_or_method": "pytest -q",
                "expected": "pass",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-OLD",
                "spec_id": "SPEC-OLD",
                "acceptance_criterion_ids": ["AC-OLD-MANUAL"],
                "feedback_ids": [],
                "type": "manual",
                "command_or_method": "historical physical-device check",
                "expected": "pass",
                "actual": "not rerun for this bounded iteration",
                "result": "skipped",
            },
        ],
        "uncovered_acceptance_criterion_ids": ["AC-OLD-MANUAL"],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest -q"}}]
    )

    assert "complete passing evidence" in reason
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True


@pytest.mark.asyncio
async def test_tester_fail_with_acceptance_handoff_is_reconciled(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="tester-acceptance-handoff", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"},
                {"id": "AC-BROWSER", "verification_method": "e2e"},
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "test_evidence": [
            {
                "id": "TE-AUTO",
                "acceptance_criterion_ids": ["AC-AUTO"],
                "type": "unit",
                "command_or_method": "pytest -q",
                "expected": "pass",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-BROWSER",
                "acceptance_criterion_ids": ["AC-BROWSER"],
                "type": "e2e",
                "command_or_method": "Acceptance Agent opens the real browser",
                "expected": "page works from the user entry point",
                "result": "skipped",
            },
        ],
        "uncovered_acceptance_criterion_ids": [],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest -q"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True

    task = await service.create_task(project.id, "tester", "TESTING", {})
    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        [{"name": "Bash", "input": {"command": "pytest -q"}}],
    )
    assert error is None


@pytest.mark.asyncio
async def test_tester_pass_with_acceptance_handoff_ignores_downstream_open_feedback(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="tester-pass-acceptance-handoff", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"},
                {"id": "AC-BROWSER", "verification_method": "e2e"},
            ]
        }),
        encoding="utf-8",
    )
    retry_packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="Deploy and verify the canonical entry during Acceptance",
        blocking=True,
        recommended_rework_target="coding",
    )
    output = {
        "success": True,
        "next_state_event": "complete",
        "passed": True,
        "failure_class": "none",
        "recommended_rework_target": "none",
        "test_evidence": [
            {
                "id": "TE-AUTO",
                "acceptance_criterion_ids": ["AC-AUTO"],
                "type": "unit",
                "command_or_method": "pytest -q",
                "expected": "pass",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-RETRY",
                "acceptance_criterion_ids": ["AC-OLD-REGRESSION"],
                "feedback_ids": [retry_packet.id],
                "type": "unit",
                "command_or_method": "pytest -q",
                "expected": "the test harness is consistent",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-BROWSER",
                "acceptance_criterion_ids": ["AC-BROWSER"],
                "type": "e2e",
                "command_or_method": "Acceptance opens the canonical browser entry",
                "expected": "the delivered revision is usable",
                "actual": "pending post-merge Acceptance",
                "result": "skipped",
            },
        ],
        "uncovered_acceptance_criterion_ids": ["AC-BROWSER"],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [retry_packet.id],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest -q"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True

    task = await service.create_task(project.id, "tester", "TESTING", {})
    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        [{"name": "Bash", "input": {"command": "pytest -q"}}],
    )
    assert error is None


@pytest.mark.asyncio
async def test_undeployed_canonical_e2e_failure_is_handed_to_acceptance(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="undeployed-acceptance-handoff", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    project.context = {
        **project.context,
        "runtime_snapshot": {
            "artifact_revision": "revision-new",
            "running_revision": "revision-old",
            "deployment_status": "unknown",
        },
    }
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"},
                {"id": "AC-BROWSER", "verification_method": "e2e"},
                {"id": "AC-HYBRID", "verification_method": "automated_test"},
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "environment_failure",
        "recommended_rework_target": "testing",
        "test_evidence": [
            {
                "id": "TE-AUTO",
                "acceptance_criterion_ids": ["AC-AUTO"],
                "type": "unit",
                "command_or_method": "pytest -q",
                "expected": "pass",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-BROWSER",
                "acceptance_criterion_ids": ["AC-BROWSER"],
                "type": "e2e",
                "command_or_method": "open canonical browser entry",
                "expected": "current revision is usable",
                "actual": "connection refused before deployment",
                "result": "fail",
            },
            {
                "id": "TE-HYBRID-BROWSER",
                "acceptance_criterion_ids": ["AC-HYBRID"],
                "type": "e2e",
                "command_or_method": "open the canonical browser entry after merge",
                "expected": "the inherited behavior remains usable",
                "actual": "connection refused before deployment",
                "result": "fail",
            },
        ],
        "uncovered_acceptance_criterion_ids": ["AC-BROWSER", "AC-HYBRID"],
        "failures": [{
            "id": "TF-ENTRY",
            "test": "canonical entry",
            "error": "not deployed",
            "blocking": True,
        }],
        "notes": [],
        "verified_feedback_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest -q"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True
    assert output["test_evidence"][1]["result"] == "skipped"
    assert output["test_evidence"][2]["result"] == "skipped"
    assert output["failures"] == []


@pytest.mark.asyncio
async def test_feedback_references_expand_only_unambiguous_prefixes(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="feedback-id-prefix", task_type="feature", description="d"
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="fix the failed assertion",
        blocking=True,
    )
    short_id = packet.id.rsplit("_", 1)[0]
    output = {
        "verified_feedback_ids": [short_id],
        "test_evidence": [{"feedback_ids": [short_id]}],
    }

    oc._canonicalize_feedback_references(project.id, output)

    assert output["verified_feedback_ids"] == [packet.id]
    assert output["test_evidence"][0]["feedback_ids"] == [packet.id]


@pytest.mark.asyncio
async def test_tester_system_observable_manual_pending_advances_to_acceptance(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="external-manual", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"},
                {
                    "id": "AC-MOBILE",
                    "verification_method": "manual",
                    "evidence_source": "system_observable",
                },
            ]
        }),
        encoding="utf-8",
    )
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="ACCEPTANCE",
        source_task_id=None,
        source_round=1,
        kind="acceptance_rejection",
        failure_class="missing_test_evidence",
        summary="最终真实入口验收尚未关闭",
        blocking=True,
        recommended_rework_target="specs",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        # The model may misclassify an unexecuted final-manual check as a
        # testing defect. Structured evidence, not that label, is authoritative.
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "test_evidence": [
            {
                "id": "TE-AUTO",
                "spec_id": "SPEC-1",
                "acceptance_criterion_ids": ["AC-AUTO"],
                "feedback_ids": [],
                "type": "unit",
                "command_or_method": "pytest",
                "expected": "pass",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-MOBILE",
                "spec_id": "SPEC-2",
                "acceptance_criterion_ids": ["AC-MOBILE"],
                "feedback_ids": [],
                "type": "manual",
                "command_or_method": "手机浏览器打开网站并完成对话",
                "expected": "页面可见交互符合合同",
                "actual": "等待外部验收者执行",
                "result": "skipped",
            },
        ],
        "uncovered_acceptance_criterion_ids": ["AC-MOBILE"],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True
    assert output["failure_class"] == "none"
    assert "最终验收阶段" in output["notes"][-1]


@pytest.mark.asyncio
async def test_tester_e2e_handoff_allows_lower_level_evidence_for_same_ac(
    orchestrator,
):
    """Internal evidence must not invalidate an explicit final-E2E hand-off."""
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="mixed-e2e-handoff", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-CLI",
                "verification_method": "automated_test",
                "evidence_source": "system_observable",
            }]
        }),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "test_bug",
        "recommended_rework_target": "testing",
        "test_evidence": [
            {
                "id": "TE-INTERNAL",
                "spec_id": "SPEC-CLI",
                "acceptance_criterion_ids": ["AC-CLI"],
                "feedback_ids": [],
                "type": "integration",
                "command_or_method": "pytest tests/test_cli.py",
                "expected": "local dispatch works",
                "actual": "1 passed",
                "result": "pass",
            },
            {
                "id": "TE-E2E",
                "spec_id": "SPEC-CLI",
                "acceptance_criterion_ids": ["AC-CLI"],
                "feedback_ids": [],
                "type": "e2e",
                "command_or_method": "launch the real CLI and enter /cost",
                "expected": "visible billing output and exit 0",
                "actual": "reserved for Acceptance Agent",
                "result": "skipped",
            },
        ],
        "uncovered_acceptance_criterion_ids": ["AC-CLI"],
        "failures": [],
        "notes": [],
        "verified_feedback_ids": [],
        "deferred_obligations": [{
            "obligation": "Independent final browser verification",
            "target_stage": "Acceptance",
            "acceptance_criterion_ids": ["AC-CLI"],
            "contract": "Open the public entry and record the visible result",
        }],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Bash", "input": {"command": "pytest"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["passed"] is True
    assert output["failure_class"] == "none"
    assert oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        runtime_tool_calls=[{"name": "Bash", "input": {"command": "pytest"}}],
    ) is None


def test_quality_runtime_capabilities_support_codex_event_names():
    codex_calls = [
        {
            "name": "command_execution",
            "input": {"command": "python -m pytest -q"},
        }
    ]

    assert AgentOrchestrator._runtime_observed_command(codex_calls) is True
    assert AgentOrchestrator._runtime_observed_read(codex_calls) is True
    assert AgentOrchestrator._runtime_observed_command(
        [{"name": "file_change", "input": {"path": "app.py"}}]
    ) is False
    assert AgentOrchestrator._runtime_observed_read(
        [{"name": "file_change", "input": {"path": "app.py"}}]
    ) is False


@pytest.mark.asyncio
async def test_failed_external_manual_test_remains_quality_failure(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="failed-manual", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-MOBILE",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "failure_class": "implementation_bug",
        "recommended_rework_target": "coding",
        "test_evidence": [{
            "id": "TE-MOBILE",
            "spec_id": "SPEC-1",
            "acceptance_criterion_ids": ["AC-MOBILE"],
            "type": "manual",
            "command_or_method": "手机浏览器操作",
            "expected": "可发送消息",
            "actual": "发送按钮无响应",
            "result": "fail",
        }],
        "uncovered_acceptance_criterion_ids": [],
        "failures": [{"id": "F-1", "blocking": True}],
    }

    reason = oc._normalize_quality_gate_output(project, output, [])

    assert reason is None
    assert output["next_state_event"] == "fail"
    assert output["passed"] is False


@pytest.mark.asyncio
async def test_reviewer_system_observable_manual_pending_advances_to_acceptance(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-external-manual", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"},
                {
                    "id": "AC-MOBILE",
                    "verification_method": "manual",
                    "evidence_source": "system_observable",
                },
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "result": "fail",
        "recommended_rework_target": "testing",
        "issues": [{
            "id": "RVW-MANUAL",
            "severity": "minor",
            "category": "tests",
            "description": "等待手机浏览器验收",
            "blocking": False,
        }],
        "requirement_coverage": [
            {
                "acceptance_criterion_id": "AC-AUTO",
                "status": "covered",
                "evidence": ["automated evidence"],
            },
            {
                "acceptance_criterion_id": "AC-MOBILE",
                "status": "partially_covered",
                "evidence": ["页面实现与复测合同已就绪，待真实手机操作"],
            },
        ],
        "stale_artifact_ids": [],
        "notes": [],
    }

    tool_calls = [{"name": "Read", "input": {"file_path": "app.py"}}]
    assert oc._reviewer_only_external_manual_pending(project, output, tool_calls)
    reason = oc._normalize_quality_gate_output(project, output, tool_calls)

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["result"] == "pass"
    assert output["recommended_rework_target"] == "none"
    assert "最终验收阶段" in output["notes"][-1]


@pytest.mark.asyncio
async def test_reviewer_real_partial_coverage_remains_failure(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-real-gap", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-AUTO", "verification_method": "automated_test"}
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "result": "fail",
        "recommended_rework_target": "coding",
        "issues": [{"id": "RVW-1", "blocking": True}],
        "requirement_coverage": [{
            "acceptance_criterion_id": "AC-AUTO",
            "status": "partially_covered",
            "evidence": ["missing implementation"],
        }],
        "stale_artifact_ids": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Read", "input": {}}]
    )

    assert reason is None
    assert output["next_state_event"] == "fail"
    assert output["result"] == "fail"


@pytest.mark.asyncio
async def test_reviewer_future_acceptance_evidence_does_not_block_review(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-future-acceptance", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-E2E",
                    "verification_method": "automated_test",
                    "evidence_source": "system_observable",
                }
            ]
        }),
        encoding="utf-8",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    tester.status = "completed"
    tester.output_data = {
        "test_evidence": [{
            "id": "TE-ACCEPTANCE",
            "type": "e2e",
            "result": "skipped",
            "acceptance_criterion_ids": ["AC-E2E"],
            "command_or_method": "Acceptance Agent opens the public entry",
            "expected": "Independent browser evidence is recorded",
        }]
    }
    service.session.add(tester)
    service.session.commit()
    output = {
        "success": True,
        "next_state_event": "fail",
        "result": "fail",
        "recommended_rework_target": "testing",
        "issues": [{
            "id": "RVW-FUTURE",
            "severity": "major",
            "category": "tests",
            "description": "Acceptance Agent independent evidence is pending",
            "blocking": True,
        }],
        "blockers": [{
            "id": "future-acceptance",
            "category": "quality_failure",
            "blocking": True,
            "affected_acceptance_criterion_ids": ["AC-E2E"],
        }],
        "requirement_coverage": [{
            "acceptance_criterion_id": "AC-E2E",
            "status": "partially_covered",
            "evidence": ["Tester and Reviewer completed independent browser checks"],
        }],
        "stale_artifact_ids": [],
        "notes": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Read", "input": {"file_path": "app.py"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["result"] == "pass"
    assert output["recommended_rework_target"] == "none"


@pytest.mark.asyncio
async def test_reviewer_empty_fail_with_clean_partial_handoff_is_normalized(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-clean-handoff", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-A", "verification_method": "automated_test"},
                {"id": "AC-B", "verification_method": "automated_test"},
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "fail",
        "result": "fail",
        "recommended_rework_target": "testing",
        "issues": [],
        "requirement_coverage": [
            {
                "acceptance_criterion_id": "AC-A",
                "status": "covered",
                "evidence": ["Reviewer verified it"],
            },
            {
                "acceptance_criterion_id": "AC-B",
                "status": "partially_covered",
                "evidence": ["Current stages passed; next stage owns final evidence"],
            },
        ],
        "stale_artifact_ids": [],
        "notes": [],
    }

    tool_calls = [{"name": "Read", "input": {"file_path": "app.py"}}]
    assert oc._reviewer_only_external_manual_pending(project, output, tool_calls)
    reason = oc._normalize_quality_gate_output(project, output, tool_calls)

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["result"] == "pass"
    assert output["recommended_rework_target"] == "none"


@pytest.mark.asyncio
async def test_reviewer_pass_with_clean_acceptance_handoff_stays_pass(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-pass-handoff", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-A", "verification_method": "automated_test"},
                {"id": "AC-B", "verification_method": "automated_test"},
            ]
        }),
        encoding="utf-8",
    )
    output = {
        "success": True,
        "next_state_event": "complete",
        "result": "pass",
        "recommended_rework_target": "none",
        "issues": [],
        "requirement_coverage": [
            {
                "acceptance_criterion_id": "AC-A",
                "status": "covered",
                "evidence": ["Reviewer evidence"],
            },
            {
                "acceptance_criterion_id": "AC-B",
                "status": "partially_covered",
                "evidence": ["Acceptance stage owns the remaining evidence"],
            },
        ],
        "stale_artifact_ids": [],
        "notes": [],
    }

    reason = oc._normalize_quality_gate_output(
        project, output, [{"name": "Read", "input": {"file_path": "app.py"}}]
    )

    assert reason is None
    assert output["next_state_event"] == "complete"
    assert output["result"] == "pass"


@pytest.mark.asyncio
async def test_reviewer_coverage_ids_must_exactly_match_requirements(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-id-contract", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {"id": "AC-IDEN-001", "verification_method": "e2e"},
                {"id": "AC-WEB-001", "verification_method": "manual"},
            ]
        }),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "reviewer", "REVIEW", {})
    output = {
        "success": True,
        "next_state_event": "fail",
        "result": "fail",
        "issues": [],
        "requirement_coverage": [
            {
                "acceptance_criterion_id": "AC-001",
                "status": "covered",
                "evidence": ["renamed ID"],
            }
        ],
        "stale_artifact_ids": [],
    }

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        runtime_tool_calls=[{"name": "Read", "input": {}}],
    )

    assert "must exactly match the cumulative active acceptance contract" in str(error)
    assert "AC-IDEN-001" in str(error)
    assert "AC-001" in str(error)


def test_recovery_and_rework_summaries_distinguish_system_from_quality(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = type(
        "Project",
        (),
        {
            "id": "proj_summary",
            "current_state": "CODING_RECOVERY_PENDING",
            "context": {
                "last_agent_failure": {
                    "agent": "coder",
                    "state": "CODING",
                    "attempts": 2,
                    "error": "invalid output",
                }
            },
        },
    )()

    summary = oc._build_summary_for_state(project, "CODING_RECOVERY_PENDING")

    assert "运行或输出协议问题" in summary
    assert "不是质量验收结论" in summary


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
    # A real runtime can return valid structured stdout after accidentally
    # writing a malformed local artifact.  Downstream agents must receive the
    # schema-validated object, never the untrusted transport file.
    (worktree / ".mac" / "TESTER_OUTPUT.json").write_text(
        '{"success": true, "report": "unescaped "quote""}',
        encoding="utf-8",
    )

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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_acceptance_business_result")],
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
async def test_auto_mode_uses_coordinator_for_confirmation(orchestrator):
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "config-validator CLI",
            "goals": ["实现 CLI"],
            "acceptance_criteria": ["测试通过"],
        },
        "conversation_coordinator": {
            "success": True,
            "action": "reject",
            "reason": "需求理解有误，建议重新分析。",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
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
    # Coordinator suggested "reject", so we should be back at PLANNING_REQUIREMENTS.
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
            "acceptance_criteria": [{
                "id": "AC-1",
                "verification_method": "automated_test",
                "evidence_source": "system_observable",
            }],
        },
        "coder": {"success": True, "next_state_event": "complete"},
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "产物覆盖合同",
            "evidence_refs": [".mac/BUG_ANALYSIS.json"],
        },
            "tester": {
                "success": True,
                "next_state_event": "complete",
                "passed": True,
                "test_evidence": [{
                    "id": "TE-1",
                    "type": "unit",
                    "result": "pass",
                    "acceptance_criterion_ids": ["AC-1"],
                }],
                "uncovered_acceptance_criterion_ids": [],
                "failures": [],
            },
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
                "result": "pass",
                "issues": [],
                "requirement_coverage": [{
                    "acceptance_criterion_id": "AC-1",
                    "status": "covered",
                    "evidence": ["TE-1"],
                }],
                "stale_artifact_ids": [],
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

    class CapturingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            self.last_kwargs = kwargs
            return await super().execute(**kwargs)

    runtime = CapturingRuntime({
        "project_manager": {
            "success": True,
            "next_state_event": "complete",
            "reply": "继续",
            "suggested_action": "confirm",
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    oc._runtime_env_for_project = Mock(return_value={"RUNTIME_KEY": "secret"})
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
    assert runtime.last_kwargs["runtime_env"] == {"RUNTIME_KEY": "secret"}
    assert runtime.last_kwargs["task_id"] == tasks[0].id


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
        approval_mode="strict",
    )
    project.current_state = "TESTING"
    service.session.commit()

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REWORK"
    assert "loop_supervisor" not in updated.context
    feedback = service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].source_stage == "TESTING"
    tester_task = service.session.exec(
        select(Task)
        .where(Task.project_id == project.id, Task.agent_type == "tester")
        .order_by(Task.created_at.desc())
    ).first()
    work_item = tester_task.input_data["work_item_contract"]
    assert work_item["runtime_contract"]["deployment_status"] == "unknown"
    assert "HostAutomation" in work_item["effective_runtime_capabilities"]
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_llm_api_key")],
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
        approval_mode="strict",
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
        approval_mode="strict",
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
async def test_quality_failure_without_rework_target_escalates_via_coordinator(orchestrator):
    """An unowned failure asks Coordinator instead of making users guess a stage."""
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
            Approval.state == "TESTING_INPUT_PENDING",
            Approval.status == "pending",
        )
    ).first()
    assert approval is not None
    assert approval.approval_type == "input"
    interaction = service.get_open_question_interaction(project.id)
    assert interaction is not None
    assert interaction.agent == "tester"


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
async def test_reviewer_receives_feedback_that_tester_just_verified(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "reviewer": {
            "success": True,
            "next_state_event": "complete",
            "result": "fail",
            "summary": "independent review",
            "recommended_rework_target": "coding",
            "issues": [{
                "id": "r1",
                "severity": "major",
                "category": "implementation",
                "description": "still needs review",
                "blocking": True,
            }],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="review-verified-feedback",
        task_type="feature",
        description="Test",
        approval_mode="auto",
    )
    packet = await service.create_feedback_packet(
        project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="human_rejection",
        summary="centralize duplicated errors",
        status="resolved",
    )
    tester_task = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(
        tester_task.id,
        {"verified_feedback_ids": [packet.id]},
    )
    project.current_state = "REVIEW"
    service.session.commit()

    await oc.tick(project.id)

    reviewer_task = service.session.exec(
        select(Task)
        .where(Task.project_id == project.id, Task.agent_type == "reviewer")
        .order_by(Task.created_at.desc())
    ).first()
    feedback_context = reviewer_task.input_data["open_feedback"]
    assert feedback_context[0]["feedback_id"] == packet.id
    assert feedback_context[0]["status"] == "resolved"
    assert feedback_context[0]["summary"] == "centralize duplicated errors"


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

    # Re-enter the same state and tick again. FakeRuntime does not write a
    # planner artifact; remove the legacy fallback so this test stays focused
    # on round numbering rather than artifact-schema validation.
    (Path(project.workspace_path) / ".mac" / "REQUIREMENTS.json").unlink()
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
async def test_completion_gate_rejects_unknown_test_evidence(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="negative-evidence", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    task = await service.create_task(project.id, "tester", "TESTING", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
            "test_evidence": [{"id": "TE-1", "result": "unknown"}],
            "uncovered_acceptance_criterion_ids": [],
            "failures": [],
        },
    )
    assert error and "tester complete requires" in error


@pytest.mark.asyncio
async def test_reviewer_legacy_acceptance_handoff_does_not_force_retry(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="reviewer-acceptance-handoff", task_type="feature", description="d"
    )
    mac = Path(project.workspace_path) / ".mac"
    (mac / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{
                "id": "AC-MANUAL-001",
                "verification_method": "manual",
                "evidence_source": "system_observable",
            }]
        }),
        encoding="utf-8",
    )
    tester = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(tester.id, {
        "success": True,
        "next_state_event": "complete",
        "passed": True,
        "test_evidence": [{
            "id": "TE-MANUAL-001",
            "type": "manual",
            "result": "skipped",
            "acceptance_criterion_ids": ["AC-MANUAL-001"],
            "command_or_method": "从真实入口执行刷新恢复流程",
            "expected": "刷新后历史完整恢复",
        }],
        "uncovered_acceptance_criterion_ids": ["AC-MANUAL-001"],
    })
    project.current_state = "REVIEW"
    service.session.commit()
    reviewer = await service.create_task(project.id, "reviewer", "REVIEW", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        reviewer,
        {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "issues": [],
            "requirement_coverage": [{
                "acceptance_criterion_id": "AC-MANUAL-001",
                "status": "partially_covered",
                "evidence": ["最终验收按真实入口复测"],
            }],
            "blockers": [],
            "deferred_obligations": [{
                "acceptance_criterion_id": "AC-MANUAL-001",
                "owner": "acceptance",
                "obligation": "从真实入口完成人工观察",
            }],
        },
        runtime_tool_calls=[{"name": "Read", "input": {"file_path": "app.py"}}],
    )

    assert error is None


@pytest.mark.asyncio
async def test_completion_gate_allows_only_explicitly_deferred_uncovered_ac(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="deferred-ac", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "STATE.json").write_text(
        json.dumps(
            {
                "context": {
                    "confirmed_brief": {
                        "acceptance_criteria": [
                            "AC-CORE-001: core behavior",
                            "AC-CLOUD-PENDING: waits for external credentials",
                            {
                                "id": "AC-DEVICE-DEFERRED",
                                "status": "blocked_external",
                            },
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps(
            {
                "acceptance_criteria": [
                    {"id": "AC-CORE-001", "verification_method": "automated_test"},
                    {"id": "AC-CLOUD-PENDING", "verification_method": "automated_test"},
                    {"id": "AC-DEVICE-DEFERRED", "verification_method": "automated_test"},
                ]
            }
        ),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    output = {
        "success": True,
        "next_state_event": "complete",
        "passed": True,
        "test_evidence": [
            {
                "id": "TE-1",
                "type": "unit",
                "result": "pass",
                "acceptance_criterion_ids": ["AC-CORE-001"],
            }
        ],
        "uncovered_acceptance_criterion_ids": [
            "AC-CLOUD-PENDING",
            "AC-DEVICE-DEFERRED",
        ],
        "failures": [],
        "verified_feedback_ids": [],
    }

    assert (
        oc._agent_completion_error(
            project,
            StateMachine("feature"),
            task,
            output,
            runtime_tool_calls=[{"name": "Bash", "input": {"command": "pytest"}}],
        )
        is None
    )

    output["uncovered_acceptance_criterion_ids"] = ["AC-CORE-001"]
    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        output,
        runtime_tool_calls=[{"name": "Bash", "input": {"command": "pytest"}}],
    )
    assert error and "full AC coverage" in error


@pytest.mark.asyncio
async def test_completion_gate_enforces_declared_ac_verification_method(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="evidence-method", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    service.session.commit()
    requirements_path = Path(project.workspace_path) / ".mac" / "REQUIREMENTS.json"
    requirements_path.write_text(
        json.dumps(
            {
                "acceptance_criteria": [
                    {
                        "id": "AC-E2E-001",
                        "verification_method": "e2e",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "passed": True,
            "test_evidence": [
                {
                    "id": "TE-1",
                    "type": "unit",
                    "result": "pass",
                    "acceptance_criterion_ids": ["AC-E2E-001"],
                }
            ],
            "uncovered_acceptance_criterion_ids": [],
            "failures": [],
        },
        runtime_tool_calls=[
            {"name": "Bash", "input": {"command": "pytest -q"}}
        ],
    )

    assert error and "requires e2e" in error


@pytest.mark.asyncio
async def test_reviewer_pass_requires_runtime_observed_read(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="review-evidence", task_type="feature", description="d"
    )
    project.current_state = "REVIEW"
    service.session.commit()
    task = await service.create_task(project.id, "reviewer", "REVIEW", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "result": "pass",
            "issues": [],
            "requirement_coverage": [
                {
                    "acceptance_criterion_id": "AC-1",
                    "status": "covered",
                    "evidence": ["src/app.py"],
                }
            ],
            "stale_artifact_ids": [],
        },
        runtime_tool_calls=[],
    )

    assert error and "runtime-observed Read" in error


@pytest.mark.asyncio
async def test_agent_contract_rejects_ask_user_without_resolution_request(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="missing-resolution-request", task_type="feature", description="d"
    )
    task = await service.create_task(
        project.id, "planner", "PLANNING_REQUIREMENTS", {}
    )
    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "missing-blocker",
                "questions": [{
                    "question_id": "q1", "kind": "blocking",
                    "question": "Need a decision", "reason": "missing",
                    "allow_free_text": True, "options": [],
                    "affected_trace_ids": [],
                }],
            },
        },
    )
    assert error and "ResolutionRequest" in error


@pytest.mark.asyncio
async def test_iteration_cannot_interrupt_user_for_regression_only_blocker(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="bounded-iteration",
        task_type="feature",
        description="improve file delivery",
    )
    project.task_type = "iteration"
    project.bound_directory = project.workspace_path
    project.current_state = "TESTING"
    service.session.commit()
    workspace_mac = Path(project.workspace_path) / ".mac"
    current_mac = workspace_mac / "projects" / project.id
    current_mac.mkdir(parents=True)
    (current_mac / "ITERATION_BRIEF.json").write_text(
        json.dumps({
            "acceptance_criteria": [{"id": "AC-ITER-FILE"}],
        }),
        encoding="utf-8",
    )
    (workspace_mac / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [{"id": "AC-OLD-VISION"}],
        }),
        encoding="utf-8",
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("iteration"),
        task,
        {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {
                "batch_key": "old-credential",
                "questions": [{
                    "question_id": "q1",
                    "kind": "blocking",
                    "question": "Provide an old integration credential",
                    "reason": "historical test cannot run",
                    "allow_free_text": True,
                    "options": [],
                    "affected_trace_ids": [],
                }],
            },
            "blockers": [{
                "id": "rr1",
                "blocking": True,
                "preferred_resolver": "coordinator",
                "affected_acceptance_criterion_ids": ["AC-OLD-VISION"],
            }],
        },
    )

    assert error and "regression-only" in error


@pytest.mark.asyncio
async def test_quality_agent_cannot_rework_to_stage_missing_from_profile(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="profile-owned-rework", task_type="feature", description="d"
    )
    project.task_type = "iteration"
    project.current_state = "REVIEW"
    service.session.commit()
    task = await service.create_task(project.id, "reviewer", "REVIEW", {})

    error = oc._agent_completion_error(
        project,
        StateMachine("iteration"),
        task,
        {
            "success": True,
            "next_state_event": "fail",
            "result": "fail",
            "recommended_rework_target": "specs",
            "issues": [{
                "id": "RVW-1",
                "blocking": True,
                "category": "spec",
                "description": "historical spec is pending",
            }],
            "requirement_coverage": [],
        },
    )

    assert error and "not a stage" in error


@pytest.mark.asyncio
async def test_completion_gate_rejects_partially_fixed_blocking_feedback(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="negative-feedback", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="REVIEW",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="must fix",
        blocking=True,
    )
    task = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"open_feedback": [{"feedback_id": packet.id}]},
    )

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "addressed_feedback": [{
                "feedback_id": packet.id,
                "status": "partially_fixed",
                "explanation": "some work remains",
            }],
        },
    )
    assert error and packet.id in error


@pytest.mark.asyncio
async def test_coder_can_complete_when_assigned_feedback_is_already_satisfied(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="already-satisfied-feedback", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="ACCEPTANCE",
        source_task_id=None,
        source_round=1,
        kind="human_rejection",
        summary="upstream contract now makes the expected exit code explicit",
        blocking=True,
    )
    task = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"open_feedback": [{"feedback_id": packet.id}]},
    )

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "addressed_feedback": [{
                "feedback_id": packet.id,
                "status": "not_applicable",
                "explanation": "existing behavior already satisfies the clarified contract",
            }],
        },
    )

    assert error is None


@pytest.mark.asyncio
async def test_coder_completion_checks_only_work_item_feedback(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="scoped-feedback", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    coding_packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="implementation must change",
        blocking=True,
        recommended_rework_target="coding",
    )
    testing_packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="manual browser verification remains",
        blocking=True,
        recommended_rework_target="testing",
    )
    environment_packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="Coordinator must provide browser verification",
        blocking=True,
        failure_class="environment_failure",
    )
    task = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"open_feedback": [{"feedback_id": coding_packet.id}]},
    )

    error = oc._agent_completion_error(
        project,
        StateMachine("feature"),
        task,
        {
            "success": True,
            "next_state_event": "complete",
            "addressed_feedback": [{
                "feedback_id": coding_packet.id,
                "status": "fixed",
                "explanation": "implemented",
            }],
        },
    )

    assert error is None
    assert oc._feedback_relevant_to_state("CODING", coding_packet) is True
    assert oc._feedback_relevant_to_state("CODING", testing_packet) is False
    assert oc._feedback_relevant_to_state("CODING", environment_packet) is False
    assert oc._feedback_relevant_to_state("TESTING", testing_packet) is True
    assert oc._feedback_relevant_to_state("TESTING", environment_packet) is True


@pytest.mark.asyncio
async def test_runtime_capability_resolves_env_reference_without_prompt_secret(
    orchestrator, tmp_path, monkeypatch
):
    service, wm, renderer = orchestrator
    source = tmp_path / ".env"
    source.write_text(
        "SOURCE_KEY=sk-test-secret-value-123456789\nSOURCE_URL=https://api.test\n",
        encoding="utf-8",
    )
    project = await service.create_project(
        name="capability", task_type="feature", description="d"
    )
    fact = service.create_context_record(
        project.id,
        record_type="capability",
        record_key="runtime.env.test",
        value={
            "source_file": str(source),
            "env_bindings": {
                "ANTHROPIC_AUTH_TOKEN": "SOURCE_KEY",
                "ANTHROPIC_BASE_URL": "SOURCE_URL",
            },
            "applies_to": ["coder", "tester"],
        },
        source_type="user",
        sensitivity="secret_ref",
    )
    monkeypatch.setattr(
        "mac_system.agents.orchestrator.get_allowed_bind_roots", lambda: [tmp_path]
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    env = oc._runtime_env_for_project(project.id)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test-secret-value-123456789"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.test"
    assert oc._runtime_env_for_project(
        project.id,
        agent_type="coder",
        allowed_capability_fact_ids=set(),
    ) == {}
    selected_env = oc._runtime_env_for_project(
        project.id,
        agent_type="coder",
        allowed_capability_fact_ids={fact.id},
    )
    assert selected_env["ANTHROPIC_AUTH_TOKEN"] == "sk-test-secret-value-123456789"
    relevant = service.build_relevant_context(
        project.id, agent_type="coder", state="CODING"
    )
    rendered = json.dumps(relevant, ensure_ascii=False)
    assert fact.id in rendered
    assert "sk-test-secret-value-123456789" not in rendered


def test_kimi_coding_credential_is_not_exposed_as_product_runtime_capability(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    secret = "kimi-secret-must-not-enter-contract"
    monkeypatch.setenv("KIMICODE_API_KEY", secret)
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    runtime_env = oc._configured_runtime_env()
    contracts = oc._configured_runtime_capability_contracts("coder")

    assert runtime_env == {}
    assert contracts == []
    assert secret not in json.dumps(contracts, ensure_ascii=False)


def test_configured_runtime_capability_respects_agent_scope(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    secret = "system-managed-secret"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    assert oc._configured_runtime_env("conversation_coordinator") == {}
    assert oc._configured_runtime_env("coder") == {"LLM_API_KEY": secret}


def test_execution_agent_tool_budgets_are_runtime_enforced(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    assert oc._max_tool_calls_for("coder") == 60
    assert oc._max_tool_calls_for("tester") == 60
    assert oc._max_tool_calls_for("reviewer") == 50
    assert oc._max_tool_calls_for("acceptance_evaluator") == 0

    assert oc._max_tool_calls_for_attempt(
        "tester", retry_count=1, retry_feedback="Tool-call budget exceeded: allowed 60"
    ) == 90
    assert oc._max_tool_calls_for_attempt(
        "tester", retry_count=1, retry_feedback="provider timeout"
    ) == 60


def test_retry_failure_evidence_is_bounded(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    calls = [
        {"name": "Bash", "input": {"command": f"command-{index}"}}
        for index in range(20)
    ]

    compact = oc._compact_failure_evidence({
        "runtime_success": False,
        "runtime_logs": "failure details",
        "tool_calls": calls,
    })

    assert compact["tool_call_count"] == 20
    assert len(compact["last_tool_calls"]) == 3
    assert compact["last_tool_calls"][0]["command"] == "command-17"
    assert "input" not in compact["last_tool_calls"][0]


def test_retry_failure_summary_does_not_replay_unbounded_file_lists():
    files = [f"generated/path/{index}.json" for index in range(5000)]
    summary = (
        "Agent contract violation: modified_files do not match the real task "
        f"worktree diff (unreported={files})"
    )

    compact = _concise_agent_failure(summary)

    assert len(compact) <= 1200
    assert "Agent contract violation:" in compact
    assert "...[truncated]..." in compact
    assert "generated/path/4999.json" in compact
    assert "generated/path/2500.json" not in compact


@pytest.mark.asyncio
async def test_explicit_runtime_capability_grant_is_scoped_to_agent_and_state(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="runtime-grant", task_type="feature", description="d"
    )
    project.context = {
        **project.context,
        "runtime_capability_grants": [{
            "agent": "coder",
            "state": "CODING",
            "question_id": "q_runtime",
            "capabilities": ["HostAutomation", "UnknownCapability"],
        }],
    }
    service.session.commit()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    assert oc._runtime_capability_grants_for(
        service.require_project(project.id), "coder", "CODING"
    ) == ["HostAutomation"]
    assert oc._runtime_capability_grants_for(
        service.require_project(project.id), "tester", "TESTING"
    ) == []
    contracts = oc._available_runtime_capability_contracts(
        service.require_project(project.id), "coder", "CODING"
    )
    host_contract = next(
        item for item in contracts if item["capability_id"] == "HostAutomation"
    )
    assert host_contract["status"] == "available"
    assert "without asking for this permission again" in host_contract["usage"]
    assert all(
        item["capability_id"] != "HostAutomation"
        for item in oc._available_runtime_capability_contracts(
            service.require_project(project.id), "tester", "TESTING"
        )
    )


@pytest.mark.asyncio
async def test_runtime_capability_grant_recovers_from_answered_origin_option(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="audited-runtime-grant", task_type="feature", description="d"
    )
    task = await service.create_task(project.id, "coder", "CODING", {})
    task.output_data = {
        "question_batch": {
            "batch_key": "runtime-grant",
            "questions": [{
                "question_id": "q_runtime",
                "question": "Allow the requested runtime capability?",
                "options": [{
                    "key": "allow",
                    "label": "Allow",
                    "grants_runtime_capabilities": ["HostAutomation"],
                }],
            }],
        }
    }
    service.session.add(task)
    service.session.commit()
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state="CODING",
        agent="coder",
        stage="coding",
        batch_key="runtime-grant",
        questions=[{
            "question_id": "q_runtime",
            "question": "Coordinator-rewritten question",
            "options": [{"key": "allow", "label": "Allow"}],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id, "allow", "Allow", False, "user"
    )

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    assert oc._runtime_capability_grants_for(
        service.require_project(project.id), "coder", "CODING"
    ) == ["HostAutomation"]
    assert oc._runtime_capability_grants_for(
        service.require_project(project.id), "tester", "TESTING"
    ) == []


def test_coordinator_rewrite_preserves_grant_only_for_exact_option_key(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    merged = oc._preserve_option_protocol_metadata(
        [{
            "key": "allow",
            "label": "original",
            "grants_runtime_capabilities": ["HostAutomation"],
        }],
        [
            {"key": "allow", "label": "rewritten"},
            {"key": "deny", "label": "HostAutomation-looking wording"},
        ],
    )

    assert merged[0]["grants_runtime_capabilities"] == ["HostAutomation"]
    assert "grants_runtime_capabilities" not in merged[1]


@pytest.mark.asyncio
async def test_all_confirmed_duplicate_questions_resume_complete(
    orchestrator, monkeypatch, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="resolved-questions", task_type="feature", description="d"
    )
    task = await service.create_task(
        project.id, "planner", "PLANNING_REQUIREMENTS", {}
    )
    question = {
        "question_id": "q_known",
        "decision_key": "decision.known",
        "question": "已回答的问题",
    }
    monkeypatch.setattr(
        oc,
        "_filter_confirmed_questions",
        lambda _project, batch: ({**batch, "questions": []}, [question]),
    )

    worktree = tmp_path / "resolved-question-worktree"
    (worktree / ".mac").mkdir(parents=True)
    await oc._handle_agent_output(
        project,
        StateMachine("feature"),
        task,
        worktree,
        {
            "success": True,
            "next_state_event": "ask_user",
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_known_decision")],
            "question_batch": {"batch_key": "known", "questions": [question]},
            "summary": "resolved",
        },
    )

    assert service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert task.status != "failed"


@pytest.mark.asyncio
async def test_answered_agent_question_exposes_resumable_worktree(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="resume-question-worktree", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    await service.start_task(task.id, str(worktree))
    (worktree / "src").mkdir(exist_ok=True)
    (worktree / "src" / "before-question.ts").write_text("export const kept = true;\n")
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=task.round_number,
        resume_state="CODING",
        agent="coder",
        stage=None,
        batch_key="keep-worktree",
        questions=[
            {
                "question_id": "q_keep",
                "kind": "blocking",
                "question": "continue?",
            }
        ],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id, "yes", "yes", False, "user"
    )
    await service.resolve_question_interaction(interaction.id)
    await service.complete_task(
        task.id,
        {
            "success": True,
            "next_state_event": "ask_user",
            "question_batch": {"batch_key": "keep-worktree", "questions": []},
        },
    )

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    candidate = oc._find_resumable_agent_worktree(
        service.require_project(project.id), "CODING", "coder"
    )

    assert candidate == {
        "task_id": task.id,
        "worktree_path": str(worktree),
        "mode": "answered_agent_clarification",
    }
    assert (worktree / "src" / "before-question.ts").exists()


@pytest.mark.asyncio
async def test_service_restart_exposes_resumable_coder_worktree(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="resume-service-restart", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"retry_feedback": "Coordinator-scoped recovery instruction"},
    )
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    await service.start_task(task.id, str(worktree))
    (worktree / "partial.py").write_text("partial = True\n")

    service.recover_stale_tasks()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    candidate = oc._find_resumable_agent_worktree(
        service.require_project(project.id), "CODING", "coder"
    )

    assert candidate == {
        "task_id": task.id,
        "worktree_path": str(worktree),
        "mode": "service_restart",
    }
    assert service.require_project(project.id).context["agent_retry_feedback"] == {
        "CODING": "Coordinator-scoped recovery instruction"
    }
    assert (worktree / "partial.py").read_text() == "partial = True\n"


@pytest.mark.asyncio
async def test_restart_recovery_walks_only_contiguous_interrupted_retry_lineage(
    orchestrator,
):
    service, _, _ = orchestrator
    project = await service.create_project(
        name="resume-planner-instruction", task_type="feature", description="d"
    )
    project.current_state = "PLANNING_REQUIREMENTS"
    service.session.commit()

    first = await service.create_task(
        project.id,
        "requirements_analyst",
        "PLANNING_REQUIREMENTS",
        {"retry_feedback": "preserve the current scoped contract"},
    )
    await service.start_task(first.id)
    first.status = "failed"
    first.error_message = "service restarted while task was running"
    service.session.add(first)
    service.session.commit()

    second = await service.create_task(
        project.id,
        "requirements_analyst",
        "PLANNING_REQUIREMENTS",
        {"retry_feedback": ""},
    )
    await service.start_task(second.id)

    service.recover_stale_tasks()

    assert service.require_project(project.id).context["agent_retry_feedback"] == {
        "PLANNING_REQUIREMENTS": "preserve the current scoped contract"
    }


def test_continuation_output_preserves_incremental_evidence_and_redacts_secrets():
    compact = AgentOrchestrator._compact_continuation_output({
        "success": True,
        "next_state_event": "ask_user",
        "passed": False,
        "failure_class": "environment_failure",
        "recommended_rework_target": "none",
        "test_evidence": [{
            "id": "TE-001",
            "spec_id": "SPEC-001",
            "acceptance_criterion_ids": ["AC-001"],
            "type": "integration",
            "command_or_method": "pytest tests/test_integration.py -q",
            "result": "pass",
            "actual": "12 passed",
            "irrelevant_large_payload": "x" * 5000,
        }],
        "failures": [{
            "id": "TF-001",
            "test": "live provider",
            "error": "missing runtime key",
            "blocking": True,
        }],
        "report": "Authorization: Bearer sk-secretvalue123456789",
        "notes": ["reuse passing evidence"],
        "environment": {"SECRET": "must-not-be-copied"},
    })

    assert compact["test_evidence"][0]["result"] == "pass"
    assert "command_or_method" not in compact["test_evidence"][0]
    assert "actual" not in compact["test_evidence"][0]
    assert "irrelevant_large_payload" not in compact["test_evidence"][0]
    assert compact["failures"][0]["blocking"] is True
    assert "secretvalue" not in json.dumps(compact)
    assert "environment" not in compact


def test_equivalent_feedback_rounds_share_one_prompt_body_but_keep_all_ids():
    payloads = [
        {
            "feedback_id": "fb_old",
            "source_round": 1,
            "kind": "quality_failure",
            "summary": "older report",
            "evidence": [{"acceptance_criterion_ids": ["AC-001"]}],
            "failure_route": {"signature": "same-structured-failure"},
        },
        {
            "feedback_id": "fb_new",
            "source_round": 2,
            "kind": "quality_failure",
            "summary": "latest report",
            "evidence": [{"acceptance_criterion_ids": ["AC-001"]}],
            "failure_route": {"signature": "same-structured-failure"},
        },
    ]

    compact = AgentOrchestrator._compact_feedback_prompt_payloads(payloads)

    assert compact == [{
        **payloads[1],
        "related_feedback_ids": ["fb_old"],
        "equivalent_round_count": 2,
    }]

    from mac_system.core.work_item import WorkItemCompiler

    contract = WorkItemCompiler().compile(
        state="TESTING",
        agent_type="tester",
        objective="verify",
        supplied_contract={},
        relevant_facts=[],
        acceptance_criteria=[],
        regression_acceptance_criteria=[],
        open_feedback=compact,
        artifact_refs={},
        context_version=1,
    )
    assert contract["feedback_refs"] == ["fb_new", "fb_old"]


def test_unstructured_user_corrections_are_never_compacted_together():
    payloads = [
        {
            "feedback_id": "fb_memory",
            "source_round": 1,
            "kind": "correction",
            "summary": "natural facts are not persisted",
            "evidence": [],
            "failure_route": {"signature": "same-empty-route"},
        },
        {
            "feedback_id": "fb_deploy",
            "source_round": 1,
            "kind": "correction",
            "summary": "canonical entry is not running",
            "evidence": [],
            "failure_route": {"signature": "same-empty-route"},
        },
    ]

    assert AgentOrchestrator._compact_feedback_prompt_payloads(payloads) == payloads


@pytest.mark.asyncio
async def test_requirements_scope_change_supersedes_older_quality_feedback_only(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="scope-boundary-feedback", task_type="feature", description="d"
    )
    old_quality = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="failure against old scope",
        recommended_rework_target="coding",
    )
    old_correction = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="durable user correction",
        recommended_rework_target="coding",
    )
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="scope_change",
        summary="narrow the active delivery contract",
        recommended_rework_target="requirements",
    )
    current_quality = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=2,
        kind="quality_failure",
        summary="failure against current scope",
        recommended_rework_target="coding",
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    superseded = oc._quality_feedback_superseded_by_requirements_scope_change(
        project.id
    )

    assert old_quality.id in superseded
    assert old_correction.id not in superseded
    assert current_quality.id not in superseded


@pytest.mark.asyncio
async def test_agent_contract_retry_preserves_partial_worktree(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="resume-contract-retry", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    await service.start_task(task.id, str(worktree))
    (worktree / "src").mkdir(exist_ok=True)
    partial = worktree / "src" / "partial.ts"
    partial.write_text("export const partial = true;\n")

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    async def no_retry_execution(*_args, **_kwargs):
        return None

    monkeypatch.setattr(oc, "_execute_agent", no_retry_execution)
    await oc._handle_agent_failure(
        service.require_project(project.id),
        "CODING",
        service.session.get(Task, task.id),
        "Agent contract violation: invalid completion",
    )

    candidate = oc._find_resumable_agent_worktree(
        service.require_project(project.id), "CODING", "coder"
    )
    assert candidate == {
        "task_id": task.id,
        "worktree_path": str(worktree),
        "mode": "runtime_retry",
    }
    assert partial.read_text() == "export const partial = true;\n"


@pytest.mark.asyncio
async def test_exhausted_failure_gets_one_coordinator_guided_internal_retry(
    orchestrator, monkeypatch
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="guided-retry", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    project.context = {**project.context, "retry_counts": {"CODING": 1}}
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    await service.fail_task(
        task.id,
        "Agent contract violation: reported files do not match actual diff",
        evidence={"reported": ["a.py"], "actual": []},
    )
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    monkeypatch.setattr(oc, "_max_retries_for_state", lambda _state: 2)

    async def coordinator_resolution(*_args, **_kwargs):
        return {
            "success": True,
            "disposition": "retry_agent",
            "summary": "internal",
            "reason": "Coder can inspect the diff",
            "resolution_instruction": "Read the real diff and use a different strategy",
        }

    executions = []

    async def record_execution(refreshed, _state_machine):
        executions.append(refreshed.id)

    monkeypatch.setattr(oc, "_coordinate_exhausted_failure", coordinator_resolution)
    monkeypatch.setattr(oc, "_execute_agent", record_execution)

    await oc._handle_agent_failure(
        service.require_project(project.id),
        "CODING",
        service.session.get(Task, task.id),
        task.error_message,
    )

    updated = service.require_project(project.id)
    assert updated.current_state == "CODING"
    assert updated.context["retry_counts"]["CODING"] == 0
    assert updated.context["coordinator_recovery_counts"]["CODING"] == 1
    assert "different strategy" in updated.context["agent_retry_feedback"]["CODING"]
    assert executions == [project.id]


@pytest.mark.asyncio
async def test_coordinator_recovery_retry_is_bounded_and_hands_instruction_to_agent(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="coordinator-recovery-handoff",
        task_type="feature",
        description="d",
        approval_mode="normal",
    )
    project.current_state = "CODING_RECOVERY_PENDING"
    project.context = {
        **project.context,
        "retry_counts": {"CODING": 3},
        "coordinator_recovery_counts": {"CODING": 1},
        "coordinator_recovery_resolutions": {
            "CODING": {
                "success": True,
                "disposition": "retry_agent",
                "summary": "retry without user input",
                "resolution_instruction": "Fix the durable memory path first",
            }
        },
        "last_agent_failure": {
            "state": "CODING",
            "agent": "coder",
            "error": "timeout",
        },
    }
    service.session.commit()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    await oc._handle_human_confirmation(
        service.require_project(project.id), StateMachine("feature")
    )

    updated = service.require_project(project.id)
    assert updated.current_state == "CODING"
    assert updated.context["coordinator_recovery_counts"]["CODING"] == 2
    retry_feedback = json.loads(
        updated.context["agent_retry_feedback"]["CODING"]
    )
    assert retry_feedback["coordinator_resolution"] == (
        "Fix the durable memory path first"
    )
    assert "coordinator_recovery_resolutions" not in updated.context
    approval = service.session.exec(
        select(Approval).where(Approval.project_id == project.id)
    ).one()
    assert approval.status == "responded"
    assert approval.response == "retry"
    assert approval.responded_by == "system_auto"


@pytest.mark.asyncio
async def test_repeated_coordinator_recovery_still_escalates_to_user(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="bounded-coordinator-recovery",
        task_type="feature",
        description="d",
        approval_mode="normal",
    )
    project.current_state = "CODING_RECOVERY_PENDING"
    project.context = {
        **project.context,
        "coordinator_recovery_counts": {"CODING": 2},
        "coordinator_recovery_resolutions": {
            "CODING": {
                "success": True,
                "disposition": "retry_agent",
                "resolution_instruction": "Try once more",
            }
        },
    }
    service.session.commit()
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    await oc._handle_human_confirmation(
        service.require_project(project.id), StateMachine("feature")
    )

    updated = service.require_project(project.id)
    assert updated.current_state == "CODING_RECOVERY_PENDING"
    approval = service.session.exec(
        select(Approval).where(Approval.project_id == project.id)
    ).one()
    assert approval.status == "pending"


@pytest.mark.asyncio
async def test_coder_completion_requires_reported_files_in_real_worktree(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="worktree-diff-contract", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    changed = worktree / "src" / "inside-worktree.ts"
    changed.parent.mkdir(exist_ok=True)
    changed.write_text("export const changed = true;\n")

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    valid = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": "src/inside-worktree.ts"}],
        },
    )
    escaped = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": "src/only-in-main.ts"}],
        },
    )

    unchanged = worktree / "src" / "inside-worktree.ts"
    unchanged.unlink()
    false_fixed_output = {
        "next_state_event": "complete",
        "modified_files": [],
        "addressed_feedback": [
            {
                "feedback_id": "fb_blocking",
                "status": "fixed",
                "explanation": "claimed without changing code",
            }
        ],
    }
    false_fixed = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        false_fixed_output,
    )
    already_satisfied = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [],
            "addressed_feedback": [
                {
                    "feedback_id": "fb_upstream_contract",
                    "status": "not_applicable",
                    "explanation": "existing implementation already satisfies it",
                }
            ],
        },
    )

    wheel = worktree / "dist" / "demo-0.1.0-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    wheel.write_bytes(b"generated wheel")
    generated_artifact = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": "dist/demo-0.1.0-py3-none-any.whl"}],
        },
    )

    assert valid is None
    assert escaped is not None
    assert "not_in_worktree=['src/only-in-main.ts']" in escaped
    assert "unreported=['src/inside-worktree.ts']" in escaped
    assert false_fixed is None
    assert false_fixed_output["addressed_feedback"][0]["status"] == "not_applicable"
    assert already_satisfied is None
    assert generated_artifact is not None
    assert "generated build/test artifacts must be cleaned" in generated_artifact


@pytest.mark.asyncio
async def test_coder_completion_preserves_dotfile_paths(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="worktree-dotfile-contract", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    (worktree / ".gitignore").write_text(".env\n")

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    error = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": ".gitignore"}],
        },
    )

    assert error is None


@pytest.mark.asyncio
async def test_coder_completion_accepts_spec_declared_manifested_asset_tree(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="bulk-assets", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    (worktree / ".mac" / "SPECS.json").write_text(
        json.dumps({
            "specs": [{
                "id": "SPEC-ASSETS",
                "affected_files": ["data/knowledge/baseline"],
            }]
        }),
        encoding="utf-8",
    )
    baseline = worktree / "data" / "knowledge" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "a.json").write_text("{}\n", encoding="utf-8")
    (baseline / "b.json").write_text("{}\n", encoding="utf-8")
    manifest = worktree / "data" / "knowledge" / "import_manifest.json"
    manifest.write_text('{"files": ["a.json", "b.json"]}\n', encoding="utf-8")

    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    accepted = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{
                "path": "data/knowledge/baseline",
                "kind": "directory",
                "manifest_path": "data/knowledge/import_manifest.json",
            }],
        },
    )
    undeclared = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{
                "path": "data/knowledge",
                "kind": "directory",
                "manifest_path": "data/knowledge/import_manifest.json",
            }],
        },
    )

    assert accepted is None
    assert undeclared is not None
    assert "SPECS.json affected_files directory" in undeclared


@pytest.mark.asyncio
async def test_coder_diff_contract_can_include_inherited_resumed_changes(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="resumed-worktree-diff", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(project.id, "coder", "CODING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "coder",
        workspace_path=project.workspace_path,
    )
    prior = worktree / "src" / "prior.ts"
    prior.parent.mkdir(exist_ok=True)
    prior.write_text("export const value = 1;\n")
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    baseline = oc._coder_worktree_snapshot(worktree)

    unchanged_error = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {"next_state_event": "complete", "modified_files": []},
        baseline_snapshot=baseline,
    )
    inherited_error = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": "src/prior.ts"}],
        },
        baseline_snapshot=baseline,
        allow_inherited_changes=True,
    )
    prior.write_text("export const value = 2;\n")
    changed_error = oc._coder_worktree_completion_error(
        "CODING",
        worktree,
        {
            "next_state_event": "complete",
            "modified_files": [{"path": "src/prior.ts"}],
        },
        baseline_snapshot=baseline,
    )

    assert unchanged_error is None
    assert inherited_error is None
    assert changed_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "agent_type"),
    [("CODING", "coder"), ("TESTING", "tester")],
)
async def test_coder_main_workspace_mutation_fails_before_merge(
    orchestrator, monkeypatch, state, agent_type
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="workspace-isolation", task_type="feature", description="d"
    )
    project.current_state = state
    service.session.commit()

    class EscapingRuntime(FakeRuntime):
        async def execute(self, *args, **kwargs):
            workspace = kwargs["workspace"]
            (workspace / "README.md").write_text("escaped edit\n", encoding="utf-8")
            return AgentRuntimeResult(
                success=True,
                output_json={
                    "success": True,
                    "next_state_event": "complete",
                    "modified_files": [],
                    "notes": [],
                },
                output_files=[],
                logs="escaped",
                cost_info=None,
                tool_calls=[{"name": "Write", "input": {"file_path": "README.md"}}],
            )

    oc = AgentOrchestrator(service, wm, EscapingRuntime({}), renderer)

    async def preserve_failure(*_args, **_kwargs):
        return None

    monkeypatch.setattr(oc, "_handle_agent_failure", preserve_failure)
    await oc.tick(project.id)

    task = service.session.exec(
        select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())
    ).first()
    assert task is not None
    assert task.status == "failed"
    assert "Workspace isolation violation" in (task.error_message or "")
    assert agent_type in (task.error_message or "")
    assert task.worktree_path and Path(task.worktree_path).exists()
    assert service.require_project(project.id).current_state == state


@pytest.mark.asyncio
async def test_coder_runtime_does_not_expose_main_workspace_as_additional_dir(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="coder-worktree-only", task_type="feature", description="d"
    )
    project.current_state = "CODING"
    service.session.commit()

    class CapturingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            self.last_kwargs = kwargs
            return AgentRuntimeResult(
                success=True,
                output_json={
                    "success": True,
                    "next_state_event": "complete",
                    "modified_files": [],
                    "notes": [],
                },
                output_files=[],
                logs="ok",
                cost_info=None,
                tool_calls=[],
            )

    runtime = CapturingRuntime({})
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    await oc.tick(project.id)

    assert runtime.last_kwargs["readonly_dirs"] == []
    worktree = runtime.last_kwargs["worktree"]
    assert worktree != Path(project.workspace_path)
    assert runtime.last_kwargs["context_files"]
    assert all(
        path.is_relative_to(worktree / ".mac")
        for path in runtime.last_kwargs["context_files"]
    )
    boundary = runtime.last_kwargs["system_prompt"].split(
        "项目证据边界（必须遵守）", 1
    )[1]
    assert f"- {Path(project.workspace_path).resolve()}\n" not in boundary


@pytest.mark.asyncio
async def test_tester_runtime_does_not_expose_main_workspace_as_additional_dir(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="tester-worktree-only", task_type="feature", description="d"
    )
    project.current_state = "TESTING"
    project.context = {
        **project.context,
        "quality_self_retry_counts": {"TESTING": 1},
    }
    service.session.commit()

    class CapturingRuntime(FakeRuntime):
        async def execute(self, **kwargs):
            self.last_kwargs = kwargs
            return AgentRuntimeResult(
                success=True,
                output_json={
                    "success": True,
                    "next_state_event": "fail",
                    "passed": False,
                    "failure_class": "test_bug",
                    "recommended_rework_target": "testing",
                    "failures": [{"test": "probe", "error": "weak assertion"}],
                    "test_evidence": [],
                    "verified_feedback_ids": [],
                },
                output_files=[],
                logs="ok",
                cost_info=None,
                tool_calls=[],
            )

    runtime = CapturingRuntime({})
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    await oc.tick(project.id)

    assert runtime.last_kwargs["readonly_dirs"] == []
    worktree = runtime.last_kwargs["worktree"]
    assert worktree != Path(project.workspace_path)
    assert runtime.last_kwargs["context_files"]
    assert all(
        path.is_relative_to(worktree / ".mac")
        for path in runtime.last_kwargs["context_files"]
    )
    boundary = runtime.last_kwargs["system_prompt"].split(
        "项目证据边界（必须遵守）", 1
    )[1]
    assert f"- {Path(project.workspace_path).resolve()}\n" not in boundary


@pytest.mark.asyncio
async def test_pending_user_input_before_planner_is_injected_without_wasted_run(orchestrator):
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

    # Simulate a correction received in the provider/recovery gap, before the
    # next concrete planner task exists.
    await service.create_pending_user_input(
        project_id=project.id,
        sender_id="ou_123",
        observed_state="PLANNING_REQUIREMENTS",
        content="需求不对，要改",
        kind="correction",
    )

    await oc.tick(project.id)

    updated = service.get_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"

    # The correction is injected into the first attempt, so no throwaway run is
    # needed and the artifact is produced once.
    req_file = Path(updated.workspace_path) / ".mac" / "REQUIREMENTS.json"
    assert req_file.exists()
    planner_tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.state == "PLANNING_REQUIREMENTS",
        )
    ).all()
    assert len(planner_tasks) == 1
    assert planner_tasks[0].status == "completed"
    assert "需求不对，要改" in planner_tasks[0].input_data["retry_feedback"]
    assert service.get_pending_user_inputs(project.id) == []

    # A blocking feedback packet should have been created.
    open_feedback = service.get_open_feedback_packets(project.id)
    assert any(fb.kind == "correction" and fb.blocking for fb in open_feedback)
    correction = next(fb for fb in open_feedback if fb.kind == "correction")
    assert correction.id in planner_tasks[0].input_data[
        "work_item_contract"
    ]["feedback_refs"]


@pytest.mark.asyncio
async def test_pending_input_routed_to_other_owner_skips_current_agent(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "requirements updated by owner",
            "goals": ["recompile the contract"],
            "acceptance_criteria": ["AC-001: source contract is complete"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="owner-route",
        task_type="feature",
        description="deliver from a reference",
        approval_mode="normal",
    )
    project.current_state = "CODING"
    service.session.add(project)
    service.session.commit()
    await service.create_pending_user_input(
        project_id=project.id,
        sender_id="ou_1",
        observed_state="CODING",
        content="the source-derived contract is incomplete",
        kind="correction",
        recommended_rework_target="requirements",
    )

    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    coder_tasks = service.session.exec(
        select(Task).where(Task.project_id == project.id, Task.state == "CODING")
    ).all()
    assert len(coder_tasks) == 1
    assert coder_tasks[0].status == "stale"
    assert coder_tasks[0].output_data["completion_reason"] == (
        "routed_to_feedback_owner"
    )


@pytest.mark.asyncio
async def test_persisted_correction_resumes_at_assigned_owner(orchestrator):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "requirements owner handled correction",
            "goals": ["repair contract"],
            "acceptance_criteria": ["AC-001: repaired"],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="persisted-owner-route",
        task_type="feature",
        description="deliver",
        approval_mode="normal",
    )
    project.current_state = "PLANNING_IMPLEMENTATION_SPECS"
    service.session.add(project)
    service.session.commit()
    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_IMPLEMENTATION_SPECS",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="source contract is incomplete",
        blocking=True,
        recommended_rework_target="requirements",
    )

    await oc.tick(project.id)

    assert service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    spec_tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.state == "PLANNING_IMPLEMENTATION_SPECS",
        )
    ).all()
    assert spec_tasks == []


@pytest.mark.asyncio
async def test_addressed_routed_correction_does_not_route_to_owner_again(orchestrator):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="addressed-owner-route",
        task_type="feature",
        description="deliver",
        approval_mode="auto",
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="the delivery contract needs an upstream correction",
        blocking=True,
        recommended_rework_target="specs",
    )
    state_machine = StateMachine(project.task_type)

    assert not oc._feedback_owner_already_addressed(
        project.id, packet, state_machine
    )

    owner = await service.create_task(
        project_id=project.id,
        agent_type="spec_writer",
        state="PLANNING_IMPLEMENTATION_SPECS",
        input_data={"open_feedback": [{"feedback_id": packet.id}]},
    )
    await service.complete_task(
        owner.id,
        {
            "success": True,
            "next_state_event": "complete",
            "addressed_feedback": [
                {
                    "feedback_id": packet.id,
                    "status": "fixed",
                    "explanation": "the assigned owner updated the contract",
                }
            ],
        },
    )

    # The packet remains open for independent Tester verification, but the
    # owner-routing guard must no longer interpret it as unhandled work.
    assert service.get_open_feedback_packets(project.id)[0].id == packet.id
    assert oc._feedback_owner_already_addressed(project.id, packet, state_machine)
    assert not oc._feedback_needed_for_work_item(
        project.id,
        "PLANNING_IMPLEMENTATION_SPECS",
        packet,
        state_machine,
        set(),
    )
    assert oc._feedback_needed_for_work_item(
        project.id,
        "TESTING",
        packet,
        state_machine,
        set(),
    )
    assert oc._feedback_needed_for_work_item(
        project.id,
        "PLANNING_IMPLEMENTATION_SPECS",
        packet,
        state_machine,
        {packet.id},
    )


@pytest.mark.asyncio
async def test_delivered_correction_cannot_retake_routing_after_owner_turn(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="spent-owner-route",
        task_type="feature",
        description="deliver",
        approval_mode="auto",
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_IMPLEMENTATION_SPECS",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="re-run testing with the corrected stage policy",
        blocking=True,
        recommended_rework_target="testing",
    )
    owner = await service.create_task(
        project_id=project.id,
        agent_type="tester",
        state="TESTING",
        input_data={
            "work_item_contract": {"feedback_refs": [packet.id]},
        },
    )
    await service.complete_task(
        owner.id,
        {
            "success": True,
            "next_state_event": "fail",
            "passed": False,
            "failure_class": "implementation_bug",
            "recommended_rework_target": "coding",
            "failures": [{"id": "TF-NEW", "blocking": True}],
            "test_evidence": [],
        },
    )

    assert service.get_open_feedback_packets(project.id)[0].id == packet.id
    assert oc._feedback_owner_already_addressed(
        project.id,
        packet,
        StateMachine(project.task_type),
    )


@pytest.mark.asyncio
async def test_newest_unhandled_correction_prevents_opposing_owner_route_loop(
    orchestrator,
):
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "spec_writer": {
            "success": True,
            "next_state_event": "complete",
            "summary": "compiled the newest architecture correction",
            "specifications": [],
        },
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="opposing-owner-routes",
        task_type="feature",
        description="deliver",
        approval_mode="normal",
    )
    project.current_state = "PLANNING_IMPLEMENTATION_SPECS"
    service.session.add(project)
    service.session.commit()
    service.create_context_record(
        project.id,
        record_type="decision",
        record_key="delivery.old_resolution",
        value={"answer": "obsolete project-wide instruction"},
        source_type="coordinator",
    )

    await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_IMPLEMENTATION_SPECS",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="older verification correction",
        blocking=True,
        recommended_rework_target="testing",
    )
    newest = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="new architecture must be compiled into specs first",
        blocking=True,
        recommended_rework_target="specs",
    )

    await oc.tick(project.id)

    refreshed = service.require_project(project.id)
    assert refreshed.current_state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    tasks = service.get_project_tasks(project.id)
    assert [task.agent_type for task in tasks] == ["spec_writer"]
    assert tasks[0].status == "completed"
    assert newest.id in tasks[0].input_data["work_item_contract"]["feedback_refs"]
    assert tasks[0].input_data["work_item_contract"]["relevant_facts"] == []
    assert tasks[0].output_data["_round_audit"]["verdict"] == "accepted"
    events = service.get_project_events(project.id)
    audit_events = [
        event for event in events if event.message == "Agent round audit accepted"
    ]
    assert len(audit_events) == 1
    assert audit_events[0].meta["task_id"] == tasks[0].id
    assert audit_events[0].meta["input_contract"]["feedback_refs"]


@pytest.mark.asyncio
async def test_context_version_guard_marks_late_result_stale(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="late-result",
        task_type="feature",
        description="Test",
        approval_mode="normal",
    )
    task = await service.create_task(
        project.id,
        "planner",
        project.current_state,
        {"description": "old requirement"},
    )
    worktree = tmp_path / "late-result-worktree"
    worktree.mkdir()
    await service.start_task(task.id, str(worktree))
    await service.create_pending_user_input(
        project.id,
        sender_id="ou_1",
        observed_state=project.current_state,
        content="需求改成新版本",
        kind="correction",
        recommended_rework_target="requirements",
    )
    freshness = service.assess_task_freshness(task.id)
    assert freshness["fresh"] is False

    await oc._discard_stale_task_result(
        project,
        StateMachine(project.task_type),
        task,
        worktree,
        {"success": True, "next_state_event": "complete", "summary": "old"},
        freshness,
    )

    stale_task = service.session.get(Task, task.id)
    assert stale_task.status == "stale"
    assert stale_task.output_data["_context_freshness"]["fresh"] is False
    assert service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert (Path(project.workspace_path) / ".mac" / "REQUIREMENTS.json").exists()
    feedback = service.get_open_feedback_packets(project.id)[0]
    assert feedback.recommended_rework_target == "requirements"


@pytest.mark.parametrize(
    "agent_type",
    [
        "requirements_analyst",
        "architect",
        "spec_writer",
        "coder",
        "tester",
        "reviewer",
        "acceptance_agent",
    ],
)
def test_full_reference_snapshot_allows_code_and_data_reuse_without_secrets(
    tmp_path, monkeypatch, agent_type
):
    source = tmp_path / "reference-project"
    source.mkdir()
    (source / "agent.py").write_text(
        "REFERENCE_BEHAVIOR = 'preserve'\n", encoding="utf-8"
    )
    (source / "domain_data.sqlite").write_bytes(b"domain-data")
    (source / "local_knowledge").mkdir()
    (source / "local_knowledge" / "examples.jsonl").write_text(
        '{"input":"hello","output":"world"}\n', encoding="utf-8"
    )
    (source / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (source / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private", encoding="utf-8")
    worktree = tmp_path / "task-worktree"
    worktree.mkdir()
    project = SimpleNamespace(context={
        "confirmed_brief": {
            "reference_mode": "full",
            "decisions": [{
                "key": "reference.source_repo",
                "status": "confirmed",
                "details": {"source_path": str(source)},
            }],
        }
    })
    monkeypatch.setattr(
        "mac_system.agents.orchestrator.get_allowed_bind_roots",
        lambda: [tmp_path],
    )

    snapshot = AgentOrchestrator._sync_reference_snapshot(
        project, worktree, agent_type
    )

    assert snapshot == (worktree / ".mac" / "reference" / "source").resolve()
    assert (snapshot / "agent.py").read_text(
        encoding="utf-8"
    ) == "REFERENCE_BEHAVIOR = 'preserve'\n"
    assert (snapshot / "domain_data.sqlite").read_bytes() == b"domain-data"
    assert (snapshot / "local_knowledge" / "examples.jsonl").exists()
    assert (snapshot / ".env.example").exists()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / ".git").exists()
    reference_contract = json.loads(
        (snapshot.parent / "REFERENCE_CONTRACT.json").read_text(encoding="utf-8")
    )
    assert reference_contract == {
        "mode": "full",
        "source_repo": {"source_path": str(source)},
    }


def test_full_reference_contract_is_compiled_from_confirmed_decisions():
    project = SimpleNamespace(context={
        "confirmed_brief": {
            "reference_mode": "full",
            "decisions": [
                {
                    "key": "reference.capability_inventory",
                    "status": "confirmed",
                    "details": {"capabilities": [{"id": "cap-1"}]},
                },
                {
                    "key": "reference.behavioral_probes",
                    "status": "confirmed",
                    "details": {"probes": [{"capability_id": "cap-1"}]},
                },
                {
                    "key": "unrelated.preference",
                    "status": "confirmed",
                    "details": {"value": "ignored"},
                },
            ],
        }
    })

    reference = AgentOrchestrator._reference_contract_for_project(project)

    assert reference == {
        "mode": "full",
        "capability_inventory": {"capabilities": [{"id": "cap-1"}]},
        "behavioral_probes": {"probes": [{"capability_id": "cap-1"}]},
    }


def test_requirements_secondary_reference_contract_is_validated_and_persisted(
    tmp_path,
):
    workspace = tmp_path / "delivery"
    worktree = tmp_path / "planner-worktree"
    reference_dir = worktree / ".mac" / "reference"
    reference_dir.mkdir(parents=True)
    (worktree / ".mac" / "REQUIREMENTS.json").write_text(
        json.dumps({
            "artifact_id": "requirements",
            "acceptance_criteria": [{"id": "AC-OPAQUE-001"}],
        }),
        encoding="utf-8",
    )
    source = tmp_path / "arbitrary-reference-product"
    source.mkdir()
    (source / "engine.xyz").write_text("opaque behavior", encoding="utf-8")
    (reference_dir / "REFERENCE_MANIFEST.json").write_text(
        json.dumps({"files": [
            {"path": "engine.xyz"},
            {"path": "assets/examples/one.dat"},
        ]}),
        encoding="utf-8",
    )
    contract = {
        "mode": "full",
        "source_repo": {"source_path": str(source)},
        "capability_inventory": {"capabilities": [{
            "id": "cap-opaque",
            "name": "任意领域能力",
            "category": "domain-defined-category",
            "observable_behavior": "用户可以观察到转换结果",
            "user_visible": True,
            "source_files": ["engine.xyz", "assets/examples"],
            "reuse_policy": "adapt",
            "scope": "in_scope",
            "acceptance_criteria": ["AC-OPAQUE-001"],
        }]},
        "behavioral_probes": {"probes": [{
            "capability_id": "cap-opaque",
            "input": "run arbitrary input",
            "expected_output": "observable result",
            "acceptance_criterion": "AC-OPAQUE-001",
            "source_file": "engine.xyz",
            "source_quote": "opaque behavior",
        }]},
    }
    (reference_dir / "REFERENCE_CONTRACT.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    project = SimpleNamespace(
        id="proj_reference",
        workspace_path=str(workspace),
        bound_directory=str(workspace),
        context={
            "confirmed_brief": {
                "reference_mode": "full",
                "decisions": [{
                    "key": "reference.source_repo",
                    "status": "confirmed",
                    "details": {"source_path": str(source)},
                }],
            }
        },
    )

    assert AgentOrchestrator._reference_contract_artifact_error(
        project, worktree
    ) is None
    oc = object.__new__(AgentOrchestrator)
    oc._copy_mac_outputs(
        project,
        worktree,
        {"success": True, "next_state_event": "complete"},
        "PLANNING_REQUIREMENTS",
    )

    persisted = workspace / ".mac" / "projects" / project.id
    assert json.loads(
        (persisted / "REFERENCE_CONTRACT.json").read_text(encoding="utf-8")
    ) == contract
    assert AgentOrchestrator._reference_contract_for_project(project) == contract
    assert AgentOrchestrator._planning_confirmation_integrity_error(
        project, "REQUIREMENT_CONFIRM_PENDING"
    ) is None


def test_reference_contract_validation_uses_structure_not_capability_words(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    project = SimpleNamespace(
        id="proj_bad_reference",
        workspace_path=str(tmp_path / "workspace"),
        bound_directory=str(tmp_path / "workspace"),
        context={
            "confirmed_brief": {
                "reference_mode": "full",
                "decisions": [{
                    "key": "reference.source_repo",
                    "status": "confirmed",
                    "details": {"source_path": str(source)},
                }],
            }
        },
    )
    reference_dir = tmp_path / "wt" / ".mac" / "reference"
    reference_dir.mkdir(parents=True)
    (reference_dir / "REFERENCE_MANIFEST.json").write_text(
        json.dumps({"files": [{"path": "opaque.bin"}]}), encoding="utf-8"
    )
    (reference_dir / "REFERENCE_CONTRACT.json").write_text(
        json.dumps({
            "mode": "full",
            "source_repo": {"source_path": str(source)},
            "capability_inventory": {"capabilities": [{
                "id": "cap-any", "name": "任意", "category": "unknown-domain",
                "observable_behavior": "visible", "user_visible": True,
                "source_files": ["opaque.bin"], "reuse_policy": "adapt",
                "scope": "in_scope",
                "acceptance_criteria": ["AC-ANY-001"],
            }]},
            "behavioral_probes": {"probes": []},
        }),
        encoding="utf-8",
    )

    error = AgentOrchestrator._reference_contract_artifact_error(
        project, tmp_path / "wt"
    )

    assert error == (
        "Reference contract behavioral_probes.probes must be a non-empty array"
    )


def test_requirements_confirmation_rejects_missing_secondary_reference_artifact(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    project = SimpleNamespace(
        id="proj_missing_companion",
        workspace_path=str(tmp_path / "workspace"),
        bound_directory=str(tmp_path / "workspace"),
        context={
            "confirmed_brief": {
                "reference_mode": "full",
                "decisions": [{
                    "key": "reference.source_repo",
                    "status": "confirmed",
                    "details": {"source_path": str(source)},
                }],
            }
        },
    )

    error = AgentOrchestrator._planning_confirmation_integrity_error(
        project, "REQUIREMENT_CONFIRM_PENDING"
    )

    assert "missing its durable REFERENCE_CONTRACT.json" in str(error)
    assert AgentOrchestrator._planning_confirmation_integrity_error(
        project, "ARCHITECTURE_CONFIRM_PENDING"
    ) is None


@pytest.mark.asyncio
async def test_running_correction_routes_to_coordinator_selected_owner(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="route-correction",
        task_type="feature",
        description="generic delivery",
        approval_mode="normal",
    )
    project.current_state = "CODING"
    service.session.commit()
    task = await service.create_task(
        project.id, "coder", "CODING", {"description": "old contract"}
    )
    worktree = tmp_path / "stale-coder-worktree"
    worktree.mkdir()
    await service.start_task(task.id, str(worktree))
    oc._execute_agent = AsyncMock()

    await oc._rerun_stale_agent_result(
        project,
        StateMachine(project.task_type),
        task,
        worktree,
        {"success": True, "next_state_event": "complete"},
        "coordinator correction",
        route_target_stage="requirements",
    )

    refreshed = service.require_project(project.id)
    assert refreshed.current_state == "PLANNING_REQUIREMENTS"
    oc._execute_agent.assert_awaited_once()
    dispatched_project = oc._execute_agent.await_args.args[0]
    assert dispatched_project.current_state == "PLANNING_REQUIREMENTS"


@pytest.mark.asyncio
async def test_successful_rerun_expires_older_agent_question(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="superseded-question",
        task_type="bug_fix",
        description="修复问题",
        approval_mode="normal",
    )
    old_task = await service.create_task(
        project.id, "bug_analyst", "BUG_ANALYSIS", {"description": "old"}
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=old_task.id,
        origin_round=1,
        resume_state="BUG_ANALYSIS",
        agent="bug_analyst",
        stage="bug_analysis",
        batch_key="old-question",
        questions=[{
            "question_id": "q_old",
            "kind": "blocking",
            "question": "旧问题？",
            "allow_free_text": True,
            "options": [],
        }],
    )
    new_task = await service.create_task(
        project.id, "bug_analyst", "BUG_ANALYSIS", {"description": "new"}
    )

    expired = service.expire_superseded_question_interactions(
        project.id,
        agent="bug_analyst",
        resume_state="BUG_ANALYSIS",
        completed_task_id=new_task.id,
    )

    assert expired == 1
    assert service.get_question_interaction(interaction.id).status == "expired"
    assert service.get_open_question_interaction(project.id) is None


def test_planner_artifact_validation_rejects_invalid_json(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "REQUIREMENTS.json").write_text(
        '{"artifact_id":"requirements", invalid}', encoding="utf-8"
    )

    project = type("ProjectStub", (), {"workspace_path": str(tmp_path / "main")})()
    error = oc._planner_artifact_error(project, tmp_path, "PLANNING_REQUIREMENTS")

    assert error is not None
    assert "not valid JSON" in error


def test_planner_artifact_validation_rejects_stale_round_after_feedback(
    orchestrator, tmp_path, monkeypatch
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "SPECS.json").write_text(
        json.dumps({"artifact_id": "specs", "round": 1}), encoding="utf-8"
    )

    monkeypatch.setattr(renderer, "get_artifact_schema_path", lambda *_: None)
    monkeypatch.setattr(
        service,
        "get_open_feedback_packets",
        lambda _project_id: [type("FeedbackStub", (), {"blocking": True})()],
    )
    project = type(
        "ProjectStub",
        (),
        {"id": "proj_test", "workspace_path": str(tmp_path / "main")},
    )()

    error = oc._planner_artifact_error(
        project,
        tmp_path,
        "PLANNING_IMPLEMENTATION_SPECS",
        expected_round=2,
    )

    assert error is not None
    assert "expected round 2" in error


def test_planner_turn_recovers_from_valid_artifact_after_stdout_protocol_failure(
    orchestrator, tmp_path, monkeypatch
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "SPECS.json").write_text(
        json.dumps({"status": "pending_confirmation"}), encoding="utf-8"
    )
    monkeypatch.setattr(oc, "_planner_artifact_error", lambda *args, **kwargs: None)
    project = SimpleNamespace(id="proj_test", workspace_path=str(tmp_path / "main"))
    task = SimpleNamespace(id="task_test", round_number=6)

    output = oc._recover_planner_turn_from_artifact(
        project=project,
        worktree=tmp_path,
        state="PLANNING_IMPLEMENTATION_SPECS",
        task=task,
        runtime_logs="Invalid agent output: result is not a JSON object",
    )

    assert output is not None
    assert output["next_state_event"] == "complete"
    assert output["modified_files"][0]["path"] == ".mac/SPECS.json"


def test_planner_turn_does_not_recover_unrelated_runtime_failure(
    orchestrator, tmp_path, monkeypatch
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "SPECS.json").write_text(
        json.dumps({"status": "pending_confirmation"}), encoding="utf-8"
    )
    monkeypatch.setattr(oc, "_planner_artifact_error", lambda *args, **kwargs: None)

    output = oc._recover_planner_turn_from_artifact(
        project=SimpleNamespace(id="proj_test", workspace_path=str(tmp_path / "main")),
        worktree=tmp_path,
        state="PLANNING_IMPLEMENTATION_SPECS",
        task=SimpleNamespace(id="task_test", round_number=6),
        runtime_logs="Tool-call budget exceeded: allowed 40",
    )

    assert output is None


@pytest.mark.asyncio
async def test_pending_user_input_before_coder_is_injected_without_wasted_run(orchestrator):
    service, wm, renderer = orchestrator

    class FeedbackAwareFakeRuntime(FakeRuntime):
        async def execute(self, *args, **kwargs):
            result = await super().execute(*args, **kwargs)
            agent_type = kwargs.get("agent_type") or (args[0] if args else "")
            if agent_type == "coder":
                refs = re.findall(
                    r'"feedback_refs":\s*\[\s*"([^"]+)"',
                    kwargs.get("system_prompt", ""),
                )
                result.output_json = {
                    **result.output_json,
                    "addressed_feedback": [
                        {
                            "feedback_id": ref,
                            "status": "fixed",
                            "explanation": "fake runtime applied the injected correction",
                        }
                        for ref in refs
                    ],
                }
            return result

    runtime = FeedbackAwareFakeRuntime({
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
            "modified_files": [],
        },
        "conversation_coordinator": {
            "success": True,
            "action": "confirm",
            "reason": "产物覆盖合同",
            "evidence_refs": [".mac/REQUIREMENTS.json"],
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
    await oc.tick(project.id)  # requirements AI approval -> architecture
    await oc.tick(project.id)  # architecture
    await oc.tick(project.id)  # architecture AI approval -> specs
    await oc.tick(project.id)  # specs
    await oc.tick(project.id)  # specs AI approval -> CODING

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
    assert updated.current_state != "CLARIFICATION_PENDING"
    coder_tasks = service.session.exec(
        select(Task).where(Task.project_id == project.id, Task.state == "CODING")
    ).all()
    assert len(coder_tasks) == 1
    assert coder_tasks[0].status == "completed"
    assert "这里写错了" in coder_tasks[0].input_data["retry_feedback"]
    assert service.get_pending_user_inputs(project.id) == []


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
        agent_type="requirements_analyst",
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
            "agent_type": "requirements_analyst",
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
    planner_tasks = service.session.exec(
        select(Task).where(
            Task.project_id == project.id,
            Task.state == "PLANNING_REQUIREMENTS",
        )
    ).all()
    assert [item.id for item in planner_tasks] == [task.id]


# ---------------------------------------------------------------------------
# Unified ask_user / QuestionInteraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_answers_agent_question_only_from_active_fact(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="coordinator-mediation",
        task_type="feature",
        description="Test",
    )
    fact = service.create_context_record(
        project.id,
        record_type="decision",
        record_key="database.engine",
        value={"answer": "SQLite"},
        source_type="user",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state=project.current_state,
        input_data={"description": "implement"},
    )
    batch = {
        "batch_key": "db-question",
        "questions": [
            {
                "question_id": "q_db",
                "decision_key": "database.engine",
                "kind": "blocking",
                "question": "使用哪个数据库？",
                "reason": "实现需要",
                "allow_free_text": False,
                "options": [],
                "affected_trace_ids": [],
            }
        ],
    }
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state=project.current_state,
        agent="coder",
        stage=None,
        batch_key=batch["batch_key"],
        questions=batch["questions"],
    )
    runtime = FakeRuntime(
        {
            "conversation_coordinator": {
                "success": True,
                "summary": "已有确认决策",
                "resolutions": [
                    {
                        "question_id": "q_db",
                        "status": "answered",
                        "answer": "SQLite",
                        "reason": "项目决策已确认",
                        "source_fact_ids": [fact.id],
                    }
                ],
            }
        }
    )
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    unresolved = await oc._mediate_agent_questions(
        project, task, project.current_state, batch, interaction
    )

    assert unresolved == []
    question = service.get_questions_for_interaction(interaction.id)[0]
    answer = service.get_question_answer(question.id)
    assert answer.answer == "SQLite"
    assert answer.answer_source == "coordinator"
    assert answer.source_refs == [fact.id]


@pytest.mark.asyncio
async def test_coordinator_cannot_answer_external_manual_acceptance_for_user(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="manual-mediation", task_type="feature", description="Test"
    )
    mac_dir = Path(project.workspace_path) / ".mac"
    (mac_dir / "REQUIREMENTS.json").write_text(
        json.dumps({
            "acceptance_criteria": [
                {
                    "id": "AC-MOBILE",
                    "verification_method": "manual",
                    "evidence_source": "external_fact",
                }
            ]
        }),
        encoding="utf-8",
    )
    fact = service.create_context_record(
        project.id,
        record_type="decision",
        record_key="old.mobile.result",
        value={"answer": "passed"},
        source_type="user",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="acceptance_evaluator",
        state="ACCEPTANCE_PENDING",
        input_data={"description": "accept"},
    )
    batch = {
        "batch_key": "manual-check",
        "questions": [{
            "question_id": "q_manual_mobile",
            "kind": "blocking",
            "question": "Did the physical-device test pass?",
            "reason": "External observation required",
            "allow_free_text": False,
            "options": [{"key": "verified_pass", "label": "Passed"}],
            "affected_trace_ids": ["AC-MOBILE"],
        }],
    }
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key=batch["batch_key"],
        questions=batch["questions"],
    )
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "summary": "reuse old fact",
            "resolutions": [{
                "question_id": "q_manual_mobile",
                "status": "answered",
                "answer": "verified_pass",
                "reason": "old fact says passed",
                "source_fact_ids": [fact.id],
            }],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    unresolved = await oc._mediate_agent_questions(
        project, task, "ACCEPTANCE_PENDING", batch, interaction
    )

    assert unresolved == batch["questions"]
    question = service.get_questions_for_interaction(interaction.id)[0]
    assert service.get_question_answer(question.id) is None


@pytest.mark.asyncio
async def test_coordinator_cannot_answer_agent_question_with_unknown_fact(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="coordinator-escalation", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="tester",
        state=project.current_state,
        input_data={"description": "test"},
    )
    batch = {
        "batch_key": "credential-question",
        "questions": [
            {
                "question_id": "q_key",
                "kind": "blocking",
                "question": "测试凭证是否可用？",
                "reason": "集成测试需要",
                "allow_free_text": True,
                "options": [],
                "affected_trace_ids": [],
            }
        ],
    }
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state=project.current_state,
        agent="tester",
        stage=None,
        batch_key=batch["batch_key"],
        questions=batch["questions"],
    )
    runtime = FakeRuntime(
        {
            "conversation_coordinator": {
                "success": True,
                "summary": "无可靠依据",
                "resolutions": [
                    {
                        "question_id": "q_key",
                        "status": "answered",
                        "answer": "可用",
                        "reason": "猜测",
                        "source_fact_ids": ["fact_not_active"],
                    }
                ],
            }
        }
    )
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    unresolved = await oc._mediate_agent_questions(
        project, task, project.current_state, batch, interaction
    )

    assert [item["question_id"] for item in unresolved] == ["q_key"]
    question = service.get_questions_for_interaction(interaction.id)[0]
    assert service.get_question_answer(question.id) is None


@pytest.mark.asyncio
async def test_quality_failure_without_rework_target_routes_through_coordinator(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="external-prerequisite", task_type="feature", description="Test"
    )
    project.current_state = "TESTING"
    service.session.commit()
    task = await service.create_task(project.id, "tester", "TESTING", {})
    worktree = wm.create_worktree(
        project.id,
        task.id,
        "tester",
        workspace_path=project.workspace_path,
    )
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "summary": "Only the user can provide the external prerequisite",
            "resolutions": [{
                "question_id": "resolve_TF-EXT",
                "status": "ask_user",
                "reason": "No authorized source contains this external fact",
                "question": "Has the required external prerequisite been configured?",
                "options": [{
                    "key": "resolved",
                    "label": "Configured",
                    "impact": "Resume verification",
                    "recommended": True,
                }],
                "allow_free_text": True,
                "source_fact_ids": [],
            }],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    await oc._handle_agent_output(
        project,
        StateMachine(project.task_type),
        task,
        worktree,
        {
            "success": True,
            "next_state_event": "fail",
            "passed": False,
            "failure_class": "external_prerequisite",
            "recommended_rework_target": "none",
            "failures": [{
                "id": "TF-EXT",
                "error": "Required external prerequisite is unavailable",
                "expected": "Configure the external prerequisite",
                "actual": "Not available to the runtime",
                "blocking": True,
            }],
        },
    )

    updated = service.require_project(project.id)
    assert updated.current_state == "TESTING_INPUT_PENDING"
    interaction = service.get_open_question_interaction(project.id)
    assert interaction is not None
    questions = service.get_questions_for_interaction(interaction.id)
    assert questions[0].question == "Has the required external prerequisite been configured?"
    assert service.get_open_feedback_packets(project.id) == []


@pytest.mark.asyncio
async def test_coordinator_returns_internal_investigation_to_requesting_agent(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="internal-resolution", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="tester",
        state=project.current_state,
        input_data={"description": "test"},
    )
    batch = {
        "batch_key": "runtime-status",
        "questions": [{
            "question_id": "q_runtime",
            "kind": "blocking",
            "question": "Is the local service running?",
            "reason": "Need runtime evidence",
            "allow_free_text": False,
            "options": [],
            "affected_trace_ids": [],
        }],
    }
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state=project.current_state,
        agent="tester",
        stage="testing",
        batch_key=batch["batch_key"],
        questions=batch["questions"],
    )
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "summary": "Tester can inspect it",
            "resolutions": [{
                "question_id": "q_runtime",
                "status": "retry_agent",
                "reason": "The service and logs are inside the authorized runtime",
                "resolution_instruction": "Check the health endpoint and process logs directly",
                "source_fact_ids": [],
            }],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    unresolved = await oc._mediate_agent_questions(
        project, task, project.current_state, batch, interaction
    )

    assert unresolved == []
    question = service.get_questions_for_interaction(interaction.id)[0]
    answer = service.get_question_answer(question.id)
    assert answer.answer_source == "coordinator"
    assert "health endpoint" in answer.answer
    assert answer.source_refs == ["policy:internal_resolution"]
    retry_payload = json.loads(
        service.require_project(project.id).context["agent_retry_feedback"][
            project.current_state
        ]
    )
    assert "health endpoint" in retry_payload["coordinator_resolution"]
    assert retry_payload["resolved_questions"][0]["question_id"] == "q_runtime"
    await service.resolve_question_interaction(interaction.id)
    resolutions = oc._resolved_questions_for_task(project.id, task.id)
    assert resolutions == [{
        "question_id": "q_runtime",
        "decision_key": None,
        "resolution_instruction": "Check the health endpoint and process logs directly",
        "answer_source": "coordinator",
        "source_refs": ["policy:internal_resolution"],
    }]
    brief = service.require_project(project.id).context.get("confirmed_brief", {})
    assert not brief.get("decisions")
    # Old releases did persist this policy answer. Provenance must still keep
    # it from suppressing the recurring unresolved blocker.
    refreshed = service.require_project(project.id)
    refreshed.context = {
        **refreshed.context,
        "confirmed_brief": {
            "decisions": [{
                "key": "q_runtime",
                "question_id": "q_runtime",
                "answer": "retry internally",
                "status": "confirmed",
                "source": "coordinator",
            }]
        },
    }
    service.session.commit()
    filtered, suppressed = oc._filter_confirmed_questions(
        service.require_project(project.id), batch
    )
    assert filtered["questions"] == batch["questions"]
    assert suppressed == []

    second_interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=2,
        resume_state=project.current_state,
        agent="tester",
        stage="testing",
        batch_key="runtime-status-repeat",
        questions=batch["questions"],
    )
    repeated = await oc._mediate_agent_questions(
        service.require_project(project.id),
        task,
        project.current_state,
        batch,
        second_interaction,
    )
    assert repeated == batch["questions"]
    repeated_question = service.get_questions_for_interaction(
        second_interaction.id
    )[0]
    assert service.get_question_answer(repeated_question.id) is None


@pytest.mark.asyncio
async def test_fact_grounded_coordinator_answer_reaches_exact_resumed_agent(
    orchestrator,
):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="fact-grounded-resolution", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={"description": "test"},
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state="CODING",
        agent="coder",
        stage="coding",
        batch_key="probe-contract",
        questions=[{
            "question_id": "q_probe_contract",
            "kind": "blocking",
            "question": "Continue coding or revise the spec?",
            "allow_free_text": False,
            "options": [],
            "affected_trace_ids": ["AC-PRB-001"],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "Continue coding the complete probe contract",
        "Continue coding",
        False,
        "coordinator",
        answer_source="coordinator",
        source_refs=["ctx_confirmed_specs"],
    )
    await service.resolve_question_interaction(interaction.id)
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)

    assert oc._resolved_questions_for_task(project.id, task.id) == [{
        "question_id": "q_probe_contract",
        "decision_key": None,
        "resolution_instruction": "Continue coding the complete probe contract",
        "answer_source": "coordinator",
        "source_refs": ["ctx_confirmed_specs"],
    }]
    continuation = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={"resumed_from_task_id": task.id},
    )
    assert oc._resolved_questions_for_task(project.id, continuation.id) == [{
        "question_id": "q_probe_contract",
        "decision_key": None,
        "resolution_instruction": "Continue coding the complete probe contract",
        "answer_source": "coordinator",
        "source_refs": ["ctx_confirmed_specs"],
    }]
    assert oc._resolved_questions_for_continuation(
        project.id,
        continuation.id,
        artifact_revision_changed=True,
        continuation_mode="runtime_retry",
    ) == []
    assert oc._resolved_questions_for_continuation(
        project.id,
        continuation.id,
        artifact_revision_changed=True,
        continuation_mode="quality_self_retry",
    )[0]["resolution_instruction"] == "Continue coding the complete probe contract"

@pytest.mark.asyncio
async def test_coordinator_rewrites_required_user_input_as_short_choices(orchestrator):
    service, wm, renderer = orchestrator
    project = await service.create_project(
        name="choice-rewrite", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project.id, "planner", project.current_state, {}
    )
    batch = {
        "batch_key": "preference",
        "questions": [{
            "question_id": "q_style",
            "kind": "decision",
            "question": "Describe every visual preference in detail",
            "reason": "User preference required",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": [],
        }],
    }
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state=project.current_state,
        agent="planner",
        stage="requirements",
        batch_key=batch["batch_key"],
        questions=batch["questions"],
    )
    options = [
        {"key": "minimal", "label": "Minimal", "impact": "Simple UI", "recommended": True},
        {"key": "details", "label": "Add details", "impact": "Optional text", "recommended": False, "triggers_free_text": True},
    ]
    runtime = FakeRuntime({
        "conversation_coordinator": {
            "success": True,
            "summary": "Preference requires user",
            "resolutions": [{
                "question_id": "q_style",
                "status": "ask_user",
                "reason": "Only the user can choose the style",
                "question": "Choose a visual style",
                "options": options,
                "allow_free_text": True,
                "source_fact_ids": [],
            }],
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer)

    unresolved = await oc._mediate_agent_questions(
        project, task, project.current_state, batch, interaction
    )

    assert unresolved[0]["question"] == "Choose a visual style"
    assert unresolved[0]["options"] == options
    stored = service.get_questions_for_interaction(interaction.id)[0]
    assert stored.question == "Choose a visual style"
    assert stored.recommended_option == "minimal"


@pytest.mark.parametrize(
    ("output", "expected_status"),
    [
        (
            {
                "success": True,
                "summary": "canonical",
                "resolutions": [{
                    "question_id": "q1", "status": "ask_user",
                    "reason": "missing fact", "source_fact_ids": [],
                }],
            },
            "ask_user",
        ),
        (
            {
                "batch_key": "b1",
                "responses": [{
                    "question_id": "q1", "status": "ask_user",
                    "question": "choose", "options": [],
                    "reason": "missing fact",
                }],
            },
            "ask_user",
        ),
        (
            {
                "clarifications": [{
                    "question_id": "q1", "disposition": "ask_user",
                    "question": "choose", "options": [],
                    "allow_free_text": True, "reason": "missing fact",
                }],
            },
            "ask_user",
        ),
    ],
)
def test_coordinator_clarification_schema_and_normalization(output, expected_status):
    root = Path(__file__).resolve().parents[1]
    schema = root / "specs" / "schemas" / "coordinator_agent_clarification_output.json"
    assert validate_agent_output(output, schema) is None
    normalized = AgentOrchestrator._normalize_coordinator_resolutions(output)
    assert normalized == [normalized[0]]
    assert normalized[0]["question_id"] == "q1"
    assert normalized[0]["status"] == expected_status


def test_coordinator_clarification_schema_rejects_unconstrained_shape():
    root = Path(__file__).resolve().parents[1]
    schema = root / "specs" / "schemas" / "coordinator_agent_clarification_output.json"
    assert validate_agent_output({"responses": [{"status": "ask_user"}]}, schema)


@pytest.mark.asyncio
async def test_secret_answer_is_redacted_from_prompt_context_and_injected_at_runtime(
    orchestrator,
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="secret-context", task_type="feature", description="Test"
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="TESTING",
        agent="tester",
        stage=None,
        batch_key="secret-key",
        questions=[
            {
                "question_id": "q_llm_key",
                "decision_key": "capability.llm_api",
                "kind": "blocking",
                "question": "请提供 LLM API Key",
                "reason": "集成测试需要",
                "allow_free_text": True,
                "options": [],
                "affected_trace_ids": [],
                "sensitivity": "secret",
                "secret_env_var": "LLM_API_KEY",
            }
        ],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    secret_value = "sk-private-test-value"
    await service.submit_question_answer(
        question.id,
        secret_value,
        None,
        False,
        "ou_1",
        answer_source="user",
    )
    await service.resolve_question_interaction(interaction.id)

    stored_answer = service.get_question_answer(question.id)
    assert stored_answer.answer.startswith("secret_ref:")
    assert secret_value not in stored_answer.answer

    history = oc._collect_qa_history(project.id)
    serialized_history = json.dumps(history, ensure_ascii=False)
    assert secret_value not in serialized_history
    assert "[secret injected as LLM_API_KEY]" in serialized_history
    runtime_env = oc._runtime_env_for_project(project.id)
    assert runtime_env["LLM_API_KEY"] == secret_value
    assert secret_value not in json.dumps(
        oc._configured_runtime_capability_contracts("tester"),
        ensure_ascii=False,
    )

    capability = next(
        record
        for record in service.get_active_context_records(project.id)
        if record.record_key == "capability.llm_api"
    )
    assert capability.record_type == "capability"
    assert capability.value == {
        "available": True,
        "secret_ref": "env:LLM_API_KEY",
        "question_id": "q_llm_key",
        "applies_to": ["tester", "TESTING"],
    }
    assert secret_value not in json.dumps(capability.value)


@pytest.mark.asyncio
async def test_question_batch_creates_interaction(orchestrator, tmp_path):
    """Agent outputs question_batch -> QuestionInteraction created, state -> INPUT_PENDING."""
    service, wm, renderer = orchestrator
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "ask_user",
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_scope")],
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_scope_delta")],
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
        select(Task)
            .where(Task.project_id == project.id, Task.agent_type == "requirements_analyst")
        .order_by(Task.created_at.desc())
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
        project.id, "requirements_analyst", "PLANNING_REQUIREMENTS", {"description": "test"}, 1
    )
    worktree = tmp_path / "recover-worktree"
    worktree.mkdir()
    await service.start_task(task.id, str(worktree))
    await service.complete_task(task.id, {
        "success": True,
        "next_state_event": "ask_user",
        "completion_reason": "resolution_requested",
        "blockers": [_resolution_blocker("rr_recovery")],
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
    assert len(tasks) == 2
    assert {task.agent_type for task in tasks} == {
        "requirements_analyst",
        "conversation_coordinator",
    }


@pytest.mark.asyncio
async def test_recovery_never_applies_helper_agent_output_as_stage_result(
    orchestrator, tmp_path
):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="recover-only-stage-agent",
        task_type="feature",
        description="test",
        approval_mode="normal",
    )
    helper = await service.create_task(
        project.id,
        "conversation_coordinator",
        "PLANNING_REQUIREMENTS",
        {"description": "mediate retry"},
        1,
    )
    worktree = tmp_path / "helper-worktree"
    worktree.mkdir()
    await service.start_task(helper.id, str(worktree))
    await service.complete_task(
        helper.id,
        {
            "success": True,
            "disposition": "retry_agent",
            "summary": "retry the stage agent",
            "resolution_instruction": "read the complete contract",
        },
        None,
    )

    recovered = await oc._recover_unapplied_completed_task(
        service.require_project(project.id), StateMachine("feature")
    )

    assert recovered is False
    assert service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_card")],
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
async def test_question_batch_uses_legacy_card_for_single_free_text_choice(orchestrator, tmp_path):
    """Choice cards stay on Card 1.0; custom answers use the follow-up input flow."""
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_inline")],
            "question_batch": {
                "batch_key": "req_inline",
                "questions": [
                    {
                        "question_id": "q_db",
                        "kind": "blocking",
                        "question": "数据库选型？",
                        "reason": "影响后续 ORM 决策",
                        "allow_free_text": True,
                        "options": [
                            {"key": "accept_default", "label": "用 SQLite", "impact": "零依赖", "recommended": True},
                            {"key": "provide_custom", "label": "我要修改"},
                        ],
                        "affected_trace_ids": [],
                    },
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer, feishu_bridge=bridge)
    project = await service.create_project(
        name="inline-test", task_type="feature", description="test", approval_mode="normal",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_inline_group",
        feishu_chat_type="group",
    )
    await oc.tick(project.id)

    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_INPUT_PENDING"
    # state-transition card + choice batch card.
    assert len(bridge.cards) == 2
    _, batch_card, _ = bridge.cards[1]

    assert "schema" not in batch_card
    assert not [el for el in batch_card["elements"] if el.get("tag") == "form"]

    # No inline marker is persisted; selecting the explicit custom option
    # triggers the established follow-up input-card flow.
    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.status == "pending",
            Approval.approval_type == "input",
        )
    ).first()
    assert approval.payload.get("inline_free_text_question_id") is None


@pytest.mark.asyncio
async def test_question_batch_no_inline_form_when_multiple_free_text(orchestrator, tmp_path):
    """When multiple choice questions have allow_free_text=True, the orchestrator
    does NOT set inline_free_text_question_id (falls back to 2-step flow per
    the 'one input per card' UX constraint)."""
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_multi_ft")],
            "question_batch": {
                "batch_key": "req_multi_ft",
                "questions": [
                    {
                        "question_id": "q_db",
                        "kind": "blocking",
                        "question": "数据库选型？",
                        "allow_free_text": True,
                        "options": [
                            {"key": "accept_default", "label": "SQLite", "recommended": True},
                            {"key": "provide_custom", "label": "我要修改"},
                        ],
                        "affected_trace_ids": [],
                    },
                    {
                        "question_id": "q_cache",
                        "kind": "blocking",
                        "question": "缓存方案？",
                        "allow_free_text": True,
                        "options": [
                            {"key": "accept_default", "label": "无缓存", "recommended": True},
                            {"key": "provide_custom", "label": "我要修改"},
                        ],
                        "affected_trace_ids": [],
                    },
                ],
            },
        }
    })
    oc = AgentOrchestrator(service, wm, runtime, renderer, feishu_bridge=bridge)
    project = await service.create_project(
        name="multi-ft-test", task_type="feature", description="test", approval_mode="normal",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_multi_ft_group",
        feishu_chat_type="group",
    )
    await oc.tick(project.id)

    project = service.require_project(project.id)
    assert project.current_state == "REQUIREMENT_INPUT_PENDING"
    _, batch_card, _ = bridge.cards[1]

    # Legacy 1.0 envelope because no inline form is rendered.
    assert "schema" not in batch_card
    assert "elements" in batch_card
    assert not any(el.get("tag") == "form" for el in batch_card["elements"])

    approval = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.status == "pending",
            Approval.approval_type == "input",
        )
    ).first()
    assert approval.payload.get("inline_free_text_question_id") is None


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
                        "completion_reason": "resolution_requested",
                        "blockers": [_resolution_blocker("rr_two_phase")],
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_partial")],
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_skip")],
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
            "completion_reason": "resolution_requested",
            "blockers": [_resolution_blocker("rr_restart")],
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
async def test_cancel_project_cancels_auxiliary_running_tasks(orchestrator, tmp_path):
    """cancel_project must mark ALL running tasks (incl. auxiliary) as cancelled."""
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

    # The auxiliary task must be cancelled, not left running.
    updated_task = service.session.get(Task, aux_task.id)
    assert updated_task.status == "cancelled"
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

    assert service.session.get(Task, late_task.id).status == "cancelled"


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
                {
                    "question": "支持哪些客户端？",
                    "impact": "决定技术栈",
                    "blocking": True,
                },
                {
                    "question": "是否需要单点登录？",
                    "default_assumption": "不需要",
                    "blocking": True,
                },
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
async def test_informational_architecture_open_questions_allow_complete(orchestrator, tmp_path):
    """Architecture open_questions are informational, not implicit blockers."""
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
    assert result is None
    assert output["next_state_event"] == "complete"
    assert "question_batch" not in output


@pytest.mark.asyncio
async def test_informational_requirement_ambiguity_allows_complete(orchestrator, tmp_path):
    service, wm, renderer = orchestrator
    oc = AgentOrchestrator(service, wm, FakeRuntime({}), renderer)
    project = await service.create_project(
        name="info-ambiguity", task_type="feature", description="test"
    )
    worktree = tmp_path / "worktree"
    mac = worktree / ".mac"
    mac.mkdir(parents=True)
    (mac / "REQUIREMENTS.json").write_text(
        json.dumps({
            "summary": "test",
            "ambiguities": [{
                "question": "stderr or stdout",
                "default_assumption": "stderr",
                "blocking": False,
            }],
        }),
        encoding="utf-8",
    )
    output = {"next_state_event": "complete"}
    result = oc._block_on_unresolved_ambiguities(
        project, "PLANNING_REQUIREMENTS", worktree, output
    )
    assert result is None
    assert output["next_state_event"] == "complete"


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

    assert len(bridge.interactive) == 1
    refreshed = service.get_approval(approval.id)
    assert refreshed.payload.get("notification")


@pytest.mark.asyncio
async def test_human_input_outbox_prevents_duplicate_when_marker_missing(orchestrator):
    """The durable outbox remains authoritative if an approval marker is lost."""
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
    assert len(bridge.interactive) == 1


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
    assert len(bridge.interactive) == 1

    await service.respond_approval(approval.id, "to_testing", "ou_1")
    approval2 = await service.create_approval(
        project_id=project.id, task_id=None, state="REWORK",
        approval_type="input", question="本轮未通过，请选择如何处理",
        options=sm.rework_options(),
    )
    await oc._handle_human_input(service.require_project(project.id), sm, auto_resolve=False)

    assert len(bridge.interactive) == 2
    assert approval2.id in json.dumps(bridge.interactive[-1], ensure_ascii=False)


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
    first_delivery_count = len(bridge.interactive)
    await oc._handle_human_confirmation(service.require_project(project.id), sm)

    assert first_delivery_count == 2  # report + decision
    assert len(bridge.interactive) == first_delivery_count
    outbox = service.session.exec(
        select(NotificationOutbox).where(NotificationOutbox.project_id == project.id)
    ).all()
    assert len(outbox) == 2
    assert all(item.status == "sent" for item in outbox)
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
    assert len(bridge.interactive) == 4


async def test_agent_completion_to_human_confirmation_creates_approval(orchestrator):
    """Bug #2: _handle_agent_output must invoke _handle_human_confirmation
    when the agent transitions to a human_confirmation state.

    Without the elif branch, the approval card is never sent after agent
    completion and the project appears stuck until an external caller ticks
    again.  This was the root cause of the 31-minute silence in
    proj_20260723_004806_1358.
    """
    service, wm, renderer = orchestrator

    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "test",
            "goals": ["实现"],
            "acceptance_criteria": ["测试通过"],
        }
    })

    oc = AgentOrchestrator(service, wm, runtime, renderer)
    project = await service.create_project(
        name="test-bug2",
        task_type="feature",
        description="test",
        approval_mode="strict",
    )

    # Single tick: PLANNING_REQUIREMENTS -> planner runs -> REQUIREMENT_CONFIRM_PENDING.
    # The elif branch in _handle_agent_output must call
    # _handle_human_confirmation, which creates the pending Approval.
    await oc.tick(project.id)

    updated = service.require_project(project.id)
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"

    approvals = service.session.exec(
        select(Approval).where(
            Approval.project_id == project.id,
            Approval.state == "REQUIREMENT_CONFIRM_PENDING",
            Approval.status == "pending",
        )
    ).all()
    assert len(approvals) >= 1, (
        "Approval should be created by _handle_human_confirmation after "
        "agent completion transitions to REQUIREMENT_CONFIRM_PENDING"
    )
