
import json
import re
from pathlib import Path

import pytest

from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import validate_agent_output
from mac_system.config import get_agent_config


def test_delivery_prompt_ask_user_examples_match_runtime_schemas():
    root = Path(__file__).resolve().parents[1]
    cases = {
        "coder.md": "coder_output.json",
        "tester.md": "tester_output.json",
        "reviewer.md": "reviewer_output.json",
        "planner_requirements.md": "agent_turn_output.json",
        "planner_architecture.md": "agent_turn_output.json",
        "planner_specs.md": "agent_turn_output.json",
        "acceptance_evaluator.md": "acceptance_evaluator_output.json",
    }
    for prompt_name, schema_name in cases.items():
        prompt = (root / "prompts" / prompt_name).read_text(encoding="utf-8")
        candidates = [
            block for block in re.findall(
                r"```json\s*(\{.*?\})\s*```", prompt, re.DOTALL
            )
            if '"next_state_event": "ask_user"' in block
        ]
        assert candidates, prompt_name
        output = json.loads(candidates[-1])
        error = validate_agent_output(
            output, root / "specs" / "schemas" / schema_name
        )
        assert error is None, f"{prompt_name}: {error}"


def test_acceptance_evaluator_allows_direct_acceptance_without_tester_id():
    root = Path(__file__).resolve().parents[1]
    output = {
        "success": True,
        "next_state_event": "complete",
        "result": "pass",
        "summary": "direct black-box evidence passed",
        "evidence": {
            "tests_passed": True,
            "review_result": "pass",
            "meets_requirements": True,
        },
        "acceptance_matrix": [{
            "acceptance_criterion_id": "AC-BROWSER-001",
            "requirement_ids": ["REQ-001"],
            "spec_ids": ["SPEC-001"],
            "implementation_evidence": ["Acceptance Agent browser trace"],
            "test_evidence_ids": [],
            "review_result": "pass",
            "result": "pass",
        }],
        "blocking_conditions": [],
    }

    error = validate_agent_output(
        output,
        root / "specs" / "schemas" / "acceptance_evaluator_output.json",
    )

    assert error is None


def test_agent_resolution_protocol_matches_shared_schema_requirements():
    root = Path(__file__).resolve().parents[1]
    prompt_names = [
        "coder.md",
        "tester.md",
        "reviewer.md",
        "planner_requirements.md",
        "planner_architecture.md",
        "planner_specs.md",
        "planner_bug_analysis.md",
        "planner_doc_outline.md",
        "acceptance_evaluator.md",
    ]
    allowed_categories = json.loads(
        (root / "specs" / "schemas" / "resolution_request.json").read_text(
            encoding="utf-8"
        )
    )["properties"]["category"]["enum"]

    for prompt_name in prompt_names:
        prompt = (root / "prompts" / prompt_name).read_text(encoding="utf-8")
        assert "开放问题用 `allow_free_text: true`，不需 `options`" not in prompt
        assert "开放问题使用 `allow_free_text: true` 和 `options: []`" in prompt or (
            "开放问题用 `allow_free_text: true`，但仍必须按 schema 输出 `options: []`"
            in prompt
        )
        for category in allowed_categories:
            assert f"`{category}`" in prompt, f"{prompt_name}: missing {category}"


def test_delivery_prompts_forbid_schema_external_metadata_wrappers():
    root = Path(__file__).resolve().parents[1]
    prompt_names = [
        "coder.md",
        "tester.md",
        "reviewer.md",
        "planner_requirements.md",
        "planner_architecture.md",
        "planner_specs.md",
        "planner_bug_analysis.md",
        "planner_doc_outline.md",
        "acceptance_agent.md",
        "acceptance_evaluator.md",
    ]

    for prompt_name in prompt_names:
        prompt = (root / "prompts" / prompt_name).read_text(encoding="utf-8")
        assert "与 `{{output_schema_path}}` 定义的对象完全同构" in prompt
        assert "只输出 schema 允许的字段" in prompt


def test_prompts_separate_external_blackbox_from_internal_whitebox():
    root = Path(__file__).resolve().parents[1]
    coordinator = (root / "prompts" / "conversation_coordinator.md").read_text(
        encoding="utf-8"
    )
    coder = (root / "prompts" / "coder.md").read_text(encoding="utf-8")
    tester = (root / "prompts" / "tester.md").read_text(encoding="utf-8")

    for prompt in (coordinator, coder, tester):
        assert "外部" in prompt
        assert "黑盒" in prompt
        assert "真实用户入口" in prompt
        assert "生成仓库" in prompt
        assert "源码" in prompt
    assert "内部白盒角色" in tester
    assert "复测合同" in coder


def test_coder_prompt_assigns_canonical_deployment_to_acceptance():
    coder = (
        Path(__file__).resolve().parents[1] / "prompts" / "coder.md"
    ).read_text(encoding="utf-8")

    assert "不得把 task worktree 安装成 canonical 服务" in coder
    assert "正式目录合并由 Orchestrator 负责" in coder
    assert "真实入口黑盒由 Acceptance Agent 负责" in coder
    assert "`complete` 表示“当前实现版本已部署到该入口并从入口验证”" not in coder


def test_tester_open_feedback_does_not_block_acceptance_handoff():
    tester = (
        Path(__file__).resolve().parents[1] / "prompts" / "tester.md"
    ).read_text(encoding="utf-8")

    assert "`open` 只表示反馈仍需沿交付链追踪" in tester
    assert "不得仅因该反馈尚未由下游阶段关闭而制造返工" in tester
    assert "所有仍然 open 且 blocking 的反馈必须被本轮通过证据验证" not in tester


def test_coordinator_separates_product_capability_from_reference_carrier():
    prompt = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "conversation_coordinator.md"
    ).read_text(encoding="utf-8")

    assert "产品能力" in prompt
    assert "目标合同" in prompt
    assert "技术承载方式" in prompt
    assert "不得把参考项目使用的框架" in prompt
    assert "项目内持续运行的 Coordinator" in prompt
    assert "本轮不得读取参考仓库" in prompt


def test_coordinator_read_capability_matches_runtime_context():
    prompt = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "conversation_coordinator.md"
    ).read_text(encoding="utf-8")

    assert "context_access.read_roots" in prompt
    assert "只决定项目归属和默认工作目录，不是你的读取边界" in prompt
    assert "你没有文件工具" not in prompt
    assert "只读文件检查能力" in prompt
    assert "latest_tasks[].profiling" in prompt


@pytest.fixture
def agent_config(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coder.md").write_text(
        "# Coder\nWorkdir: {{worktree_path}}\nProject: {{project_id}}\nDesc: {{description}}\nFiles:\n{{context_files}}"
    )
    (prompts_dir / "planner_requirements.md").write_text(
        "# Planner\nDesc: {{description}}\nBrief: {{confirmed_brief}}\n"
        "Guard: {{question_guard_feedback}}\nOutput: {{output_schema_path}}\n"
        "Artifact: {{artifact_schema_path}}"
    )

    schemas_dir = tmp_path / "specs" / "schemas"
    schemas_dir.mkdir(parents=True)
    (schemas_dir / "coder_output.json").write_text("{}")
    (schemas_dir / "requirements.json").write_text("{}")

    return {
        "agents": {
            "coder": {
                "prompt_file": str(prompts_dir / "coder.md"),
                "output_schema": str(schemas_dir / "coder_output.json"),
                "default_allowed_tools": ["Read", "Edit"],
            },
            "planner": {
                "prompt_file": str(prompts_dir / "planner_requirements.md"),
                "output_schema": str(schemas_dir / "requirements.json"),
                "default_allowed_tools": ["Read", "Edit", "Write"],
                "stages": {
                    "requirements": {
                        "prompt_file": str(prompts_dir / "planner_requirements.md"),
                        "output_schema": str(schemas_dir / "requirements.json"),
                        "skills": [],
                    }
                },
            },
        }
    }


class FakeProject:
    def __init__(self):
        self.id = "proj_001"
        self.task_type = "feature"
        self.description = "test project"
        self.workspace_path = "/tmp/proj_001"
        self.context = {}


def test_render_system_prompt(agent_config):
    renderer = PromptRenderer(agent_config)
    rendered = "# Coder\nWorkdir: /tmp/worktree"
    prompt = renderer.render_system_prompt(
        "coder", rendered_base_prompt=rendered
    )
    assert "# Coder" in prompt
    assert "Workdir: /tmp/worktree" in prompt
    assert "{{worktree_path}}" not in prompt
    assert "本轮最终输出 JSON Schema（强制）" in prompt
    assert '```json\n{}\n```' in prompt


def test_runtime_user_prompt_does_not_duplicate_rendered_contract(agent_config):
    renderer = PromptRenderer(agent_config)
    prompt = renderer.render_runtime_user_prompt("coder")

    assert "执行 system prompt 中已完整定义的本轮任务" in prompt
    assert "WorkItem" in prompt
    assert len(prompt) < 160


def test_task_prompt_declares_exact_project_evidence_boundary(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    project.workspace_path = str(tmp_path / "workspace")
    worktree = tmp_path / "workspace" / ".mac" / "worktrees" / "coder"
    worktree.mkdir(parents=True)

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="CODING",
        worktree=worktree,
        context_files=[],
    )

    assert "项目证据边界（必须遵守）" in prompt
    assert str(worktree.resolve()) in prompt
    assert "不得扫描父目录、MAC 系统源码/规格" in prompt
    # Coder only receives its isolated checkout, not the mutable main repo.
    boundary = prompt.split("项目证据边界（必须遵守）", 1)[1]
    assert f"- {Path(project.workspace_path).resolve()}\n" not in boundary


def test_task_prompt_never_exposes_mac_schema_paths(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    project.workspace_path = str(tmp_path / "workspace")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="planner",
        stage="requirements",
        project=project,
        state="PLANNING_REQUIREMENTS",
        worktree=worktree,
        context_files=[],
        output_schema_path="/mac-system/specs/schemas/agent_turn_output.json",
        artifact_schema_path="/mac-system/specs/schemas/requirements.json",
    )

    assert "/mac-system/specs/schemas" not in prompt
    assert "已完整嵌入 system prompt" in prompt
    assert "禁止用 Read/Bash 再读取 schema 文件" in prompt


def test_all_configured_agents_receive_their_schema_in_system_prompt():
    renderer = PromptRenderer(get_agent_config())
    configured = get_agent_config()["agents"]

    for agent_type, agent_cfg in configured.items():
        stages = [*agent_cfg.get("stages", {}).keys()]
        if agent_cfg.get("prompt_file"):
            stages.insert(0, None)
        for stage in stages:
            prompt = renderer.render_system_prompt(agent_type, stage)
            schema_path = renderer.get_output_schema_path(agent_type, stage)
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            assert "本轮最终输出 JSON Schema（强制）" in prompt
            assert compact in prompt
            if renderer.get_artifact_schema_path(agent_type, stage):
                assert "本轮产物文件 JSON Schema（强制）" in prompt


def test_system_prompt_embeds_external_question_and_resolution_schema_refs():
    renderer = PromptRenderer(get_agent_config())
    prompt = renderer.render_system_prompt("coder")

    assert "引用 Schema：agent_question_batch.json" in prompt
    assert "引用 Schema：resolution_request.json" in prompt
    assert '"decision_key"' in prompt
    assert '"preferred_resolver"' in prompt


def test_structured_output_few_shots_match_each_agent_schema():
    root = Path(__file__).resolve().parents[1]
    cases = {
        "conversation_coordinator.md": "conversation_coordinator_output.json",
        "planner_requirements.md": "agent_turn_output.json",
        "planner_architecture.md": "agent_turn_output.json",
        "planner_specs.md": "agent_turn_output.json",
        "planner_bug_analysis.md": "agent_turn_output.json",
        "planner_doc_outline.md": "agent_turn_output.json",
        "coder.md": "coder_output.json",
        "tester.md": "tester_output.json",
        "reviewer.md": "reviewer_output.json",
        "acceptance_agent.md": "acceptance_agent_output.json",
        "acceptance_evaluator.md": "acceptance_evaluator_output.json",
        "project_manager.md": "project_manager_output.json",
        "project_manager_bind_dir.md": "project_manager_bind_dir_output.json",
        "project_manager_create_dir.md": "project_manager_bind_dir_output.json",
        "coordinator_agent_clarification.md": "coordinator_agent_clarification_output.json",
        "coordinator_failure_recovery.md": "coordinator_failure_recovery_output.json",
    }
    checked = 0
    for prompt_name, schema_name in cases.items():
        prompt = (root / "prompts" / prompt_name).read_text(encoding="utf-8")
        for index, block in enumerate(
            re.findall(r"```json\s*(.*?)\s*```", prompt, re.DOTALL)
        ):
            # Context placeholders are input templates, not output few-shots.
            if "{{" in block:
                continue
            try:
                candidate = json.loads(block)
            except json.JSONDecodeError as exc:
                pytest.fail(f"{prompt_name} JSON block {index} is invalid: {exc}")
            if not isinstance(candidate, dict) or "success" not in candidate:
                continue
            checked += 1
            error = validate_agent_output(
                candidate, root / "specs" / "schemas" / schema_name
            )
            assert error is None, f"{prompt_name} block {index}: {error}"
    assert checked >= 40


def test_tester_prompt_maps_mixed_verification_methods_to_separate_evidence():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "tester.md").read_text(encoding="utf-8")

    assert "`manual` | 仅 `manual`" in prompt
    assert "`metric` | `integration` / `e2e`" in prompt
    assert "schema 中没有 `type=\"metric\"`" in prompt
    assert '"id": "TE-MANUAL-001"' in prompt
    assert '"id": "TE-METRIC-001"' in prompt
    assert "不要把验证方法不同的 AC 合并进同一条证据" in prompt


def test_reference_inventory_is_delegated_by_chat_router_and_preserved_in_legacy_prompts():
    root = Path(__file__).resolve().parents[1]
    coordinator = (root / "prompts" / "conversation_coordinator.md").read_text(
        encoding="utf-8"
    )
    planner = (root / "prompts" / "planner_requirements.md").read_text(
        encoding="utf-8"
    )
    tester = (root / "prompts" / "tester.md").read_text(encoding="utf-8")
    acceptance = (root / "prompts" / "acceptance_evaluator.md").read_text(
        encoding="utf-8"
    )

    assert "完整参考项目由交付 Coordinator 动态拆解" in coordinator
    assert "不得先生成能力清单或行为探针" in coordinator
    assert "根 Coordinator 必须先让只读 Agent" in coordinator
    assert "reference.capability_inventory" not in coordinator
    assert "新事实覆盖冲突旧事实" in coordinator
    assert "参考能力盘点是需求边界，不是可选示例" in planner
    assert "不得使用某种产品预设的固定能力分类" in planner
    assert "REFERENCE_CONTRACT.json" in planner
    assert "capability_inventory.capabilities" in planner
    assert "禁止另包一层 `details`" in planner
    assert "禁止在 Tester 中硬编码某个样本项目" in tester
    assert "禁止硬编码某个样本项目" in acceptance


def test_reference_delivery_prompts_do_not_reintroduce_fixed_product_taxonomy():
    prompt_dir = Path(__file__).resolve().parents[1] / "prompts"
    fixed_taxonomy = "身份、人设风格、记忆知识、工具动作和交互策略"

    for name in ("coder.md", "tester.md", "acceptance_agent.md"):
        text = (prompt_dir / name).read_text(encoding="utf-8")
        assert fixed_taxonomy not in text
        assert "capability_inventory" in text
        assert "behavioral_probes" in text
        assert 'scope="in_scope"' in text
        assert "future_iteration" in text


def test_render_task_prompt(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    worktree = tmp_path / "wt"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="CODING",
        worktree=worktree,
        context_files=[worktree / ".mac" / "SPECS.json"],
    )
    assert str(worktree) in prompt
    assert "proj_001" in prompt
    assert "test project" in prompt
    assert "SPECS.json" in prompt


def test_render_task_prompt_hides_output_and_artifact_schema_paths(
    agent_config, tmp_path
):
    renderer = PromptRenderer(agent_config)
    worktree = tmp_path / "wt-planner-schemas"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="planner",
        stage="requirements",
        project=FakeProject(),
        state="PLANNING_REQUIREMENTS",
        worktree=worktree,
        context_files=[],
        output_schema_path="/schemas/agent_turn_output.json",
        artifact_schema_path="/schemas/requirements.json",
    )

    assert "/schemas/agent_turn_output.json" not in prompt
    assert "/schemas/requirements.json" not in prompt
    assert prompt.count("已完整嵌入 system prompt") == 2


def test_render_task_prompt_includes_retry_validation_feedback(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    worktree = tmp_path / "wt-retry"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="CODING",
        worktree=worktree,
        context_files=[],
        extra_variables={
            "retry_feedback": (
                "Invalid agent output: schema violation at intent: "
                "'clarify_scope' is not allowed"
            )
        },
    )

    assert "上一次技术校验反馈" in prompt
    assert "clarify_scope" in prompt
    assert "不要重复输出同一个无效值" in prompt


def test_render_task_prompt_includes_work_item_contract(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    worktree = tmp_path / "wt-contract"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="CODING",
        worktree=worktree,
        context_files=[],
        work_item_contract={
            "objective": "实现导出",
            "relevant_facts": [
                {"key": "export.format", "version": 1, "value": {"answer": "CSV"}}
            ],
            "context_version": 3,
        },
    )

    assert "WorkItem 合同" in prompt
    assert '"export.format"' in prompt
    assert "只把 `relevant_facts` 当作确认事实" in prompt
    assert "不等于目标产品不能实现同类集成" in prompt


def test_render_task_prompt_does_not_duplicate_rejection_packet(
    agent_config, tmp_path
):
    renderer = PromptRenderer(agent_config)
    worktree = tmp_path / "wt-feedback-dedupe"
    worktree.mkdir()
    feedback = "错误处理需要补回归测试"

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=FakeProject(),
        state="CODING",
        worktree=worktree,
        context_files=[],
        rejection_note=feedback,
        open_feedback=[{"source_stage": "REVIEW", "summary": feedback}],
    )

    assert prompt.count(feedback) == 1
    assert "反馈上下文" in prompt
    assert "用户驳回反馈" not in prompt


def test_planner_open_feedback_requires_canonical_artifact_revision(
    agent_config, tmp_path
):
    renderer = PromptRenderer(agent_config)
    worktree = tmp_path / "wt-planner-feedback"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="planner",
        stage=None,
        project=FakeProject(),
        state="PLANNING_IMPLEMENTATION_SPECS",
        worktree=worktree,
        context_files=[],
        open_feedback=[{
            "source_stage": "PLANNING_IMPLEMENTATION_SPECS",
            "summary": "补充边界条件",
            "status": "open",
        }],
    )

    assert "canonical `.mac/*.json` 产物" in prompt
    assert "不得只在 stdout 中声称已修改" in prompt


def test_get_output_schema_path(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    path = renderer.get_output_schema_path("coder")
    assert path.exists()


def test_planning_artifact_schemas_allow_absent_hash_as_null():
    schema_dir = Path(__file__).resolve().parents[1] / "specs" / "schemas"
    for filename in ("requirements.json", "architecture.json", "specs.json"):
        schema = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
        assert schema["properties"]["content_hash"]["type"] == ["string", "null"]


def test_get_allowed_tools(agent_config):
    renderer = PromptRenderer(agent_config)
    assert renderer.get_allowed_tools("coder") == ["Read", "Edit"]
    assert renderer.get_allowed_tools("planner") == ["Read", "Edit", "Write"]


def test_planner_prompt_includes_confirmed_brief(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    project.context = {
        "confirmed_brief": {
            "decisions": [{
                "key": "agent.capability_scope",
                "answer": "开放智能体",
                "status": "confirmed",
            }]
        }
    }
    worktree = tmp_path / "wt-brief"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="planner",
        stage="requirements",
        project=project,
        state="PLANNING_REQUIREMENTS",
        worktree=worktree,
        context_files=[],
    )

    assert '"key": "agent.capability_scope"' in prompt
    assert '"answer": "开放智能体"' in prompt


def test_requirements_rework_uses_versioned_artifact_not_initial_brief(
    agent_config, tmp_path
):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    project.context = {
        "confirmed_brief": {
            "scope": ["obsolete broad initial scope"],
            "acceptance_criteria": ["obsolete initial acceptance"],
        }
    }
    worktree = tmp_path / "wt-requirements-rework"
    worktree.mkdir()
    requirements = worktree / ".mac" / "REQUIREMENTS.json"
    requirements.parent.mkdir()
    requirements.write_text("{}", encoding="utf-8")

    prompt = renderer.render_task_prompt(
        agent_type="planner",
        stage="requirements",
        project=project,
        state="PLANNING_REQUIREMENTS",
        worktree=worktree,
        context_files=[requirements],
    )

    assert "obsolete broad initial scope" not in prompt
    assert "obsolete initial acceptance" not in prompt
    requirements_template = (
        Path(__file__).resolve().parents[1] / "prompts" / "planner_requirements.md"
    ).read_text(encoding="utf-8")
    assert "{{requirements_authority}}" in requirements_template
    assert "不得据此复活已缩小或废止的范围" in requirements_template


def test_qa_history_describes_runtime_secret_injection(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    worktree = tmp_path / "wt-qa"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="TESTING",
        worktree=worktree,
        context_files=[],
        qa_history=[{
            "question": "请提供用于集成测试的 LLM API key",
            "answer": "[secret injected as LLM_API_KEY]",
            "state": "TESTING",
            "round": 2,
            "sensitivity": "secret",
            "secret_env_var": "LLM_API_KEY",
        }],
    )

    assert "历史问答记录" in prompt
    assert "[secret injected as LLM_API_KEY]" in prompt
    assert "Runtime 注入进程环境" in prompt
    assert "写入 worktree 根目录" not in prompt
    assert "禁止尝试从 Prompt、项目事实或日志中获取密钥原文" in prompt


def test_no_qa_history_means_no_credential_note(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    project = FakeProject()
    worktree = tmp_path / "wt-no-qa"
    worktree.mkdir()

    prompt = renderer.render_task_prompt(
        agent_type="coder",
        stage=None,
        project=project,
        state="TESTING",
        worktree=worktree,
        context_files=[],
    )

    assert "历史问答记录" not in prompt
    assert "set -a; source .env; set +a" not in prompt


def test_production_config_splits_planning_roles_and_runtime_ownership():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "agent_config.json").read_text(encoding="utf-8"))
    agents = config["agents"]
    assert agents["requirements_analyst"]["runtime"] == "claude_code"
    assert agents["architect"]["runtime"] == "codex"
    assert agents["spec_writer"]["runtime"] == "claude_code"
    assert agents["bug_analyst"]["runtime"] == "codex"
    assert agents["documentation_planner"]["runtime"] == "claude_code"
    assert agents["coder"]["runtime"] == "codex"
    # Tester uses the available Codex runtime so a depleted Claude/DeepSeek
    # account does not stop the quality stage before tests can run. Role
    # independence comes from separate tasks, prompts, and worktrees.
    assert agents["tester"]["runtime"] == "codex"
    assert agents["reviewer"]["runtime"] == "codex"
    deepseek_capability = next(
        capability
        for capability in config["runtime_capabilities"]
        if capability["id"] == "deepseek_openai_compatible"
    )
    assert deepseek_capability["env_bindings"] == {
        "LLM_API_KEY": "ANTHROPIC_AUTH_TOKEN"
    }
    assert set(deepseek_capability["applies_to"]) == {
        "coder",
        "tester",
        "acceptance_agent",
    }
    assert agents["coder"]["max_tool_calls"] == 60
    assert agents["tester"]["max_tool_calls"] == 60
    assert agents["reviewer"]["max_tool_calls"] == 50
    assert "HostAutomation" in agents["tester"]["default_allowed_tools"]
    assert "HostAutomation" in agents["reviewer"]["default_allowed_tools"]
    assert agents["acceptance_agent"]["runtime"] == "codex"
    assert "HostAutomation" in agents["acceptance_agent"]["default_allowed_tools"]
    acceptance_prompt = (root / "prompts" / "acceptance_agent.md").read_text(
        encoding="utf-8"
    )
    assert "唯一当前 AC ID 集合" in acceptance_prompt
    assert "不得重新加入 `criteria`" in acceptance_prompt
    assert "{{acceptance_contract_feedback}}" in acceptance_prompt
    evaluator_prompt = (root / "prompts" / "acceptance_evaluator.md").read_text(
        encoding="utf-8"
    )
    assert "字段内只放 ID" in evaluator_prompt
    # Keep black-box execution and its independent evidence adjudication on
    # Codex.  The Claude/DeepSeek route produced a 60k-token zero-tool verdict
    # for a fixed 27-row matrix and materially dominated acceptance latency.
    assert agents["acceptance_evaluator"]["runtime"] == "codex"
    assert agents["acceptance_evaluator"]["default_allowed_tools"] == []
    assert agents["acceptance_evaluator"]["max_tool_calls"] == 0
    assert agents["project_manager"]["runtime"] == "claude_code"
    assert config["runtimes"]["zcode"] == {
        "enabled": False,
        "mode": "desktop_only",
        "description": config["runtimes"]["zcode"]["description"],
    }


def test_execution_agents_bound_long_running_commands_and_forbid_duplicate_runs():
    root = Path(__file__).resolve().parents[1]
    for filename in ("coder.md", "tester.md", "reviewer.md"):
        prompt = (root / "prompts" / filename).read_text(encoding="utf-8")
        assert "180 秒" in prompt
        assert "旧进程" in prompt
        assert "重复" in prompt


def test_execution_prompt_tool_targets_do_not_conflict_with_runtime_hard_limits():
    root = Path(__file__).resolve().parents[1]
    for filename in ("coder.md", "tester.md", "reviewer.md"):
        prompt = (root / "prompts" / filename).read_text(encoding="utf-8")
        assert "硬上限由 `agent_config.json` 统一管理" in prompt
        assert "软目标" in prompt


def test_delivery_agents_enforce_general_anti_overfitting_invariant():
    root = Path(__file__).resolve().parents[1]
    for filename in (
        "coder.md",
        "tester.md",
        "reviewer.md",
        "acceptance_agent.md",
        "acceptance_evaluator.md",
    ):
        prompt = (root / "prompts" / filename).read_text(encoding="utf-8")
        assert "过拟合" in prompt
        assert "协议 token" in prompt
    coder = (root / "prompts" / "coder.md").read_text(encoding="utf-8")
    reviewer = (root / "prompts" / "reviewer.md").read_text(encoding="utf-8")
    assert "扩大样例短语白名单" in coder
    assert "扩大样例短语白名单" in reviewer


def test_quality_agents_route_tracked_file_fixes_to_coder():
    root = Path(__file__).resolve().parents[1]
    for filename in ("tester.md", "reviewer.md"):
        prompt = (root / "prompts" / filename).read_text(encoding="utf-8")
        assert "受版本控制" in prompt
        assert "才路由 `testing`" in prompt or "`testing` 仅用于" in prompt
        assert "修改任何受版本控制" in prompt


def test_quality_prompts_require_direct_reproducer_evidence_and_positive_ui_completion():
    root = Path(__file__).resolve().parents[1]
    tester = (root / "prompts" / "tester.md").read_text(encoding="utf-8")
    reviewer = (root / "prompts" / "reviewer.md").read_text(encoding="utf-8")
    coordinator = (
        root / "prompts" / "coordinator_approval_decision.md"
    ).read_text(encoding="utf-8")

    assert "专项反馈必须专项取证" in tester
    assert "不得因为某用例“未出现在失败/跳过摘要中”就反推它已通过" in tester
    assert "目标元素或响应已存在" in tester
    assert "可复现环境是证据的一部分" in tester
    assert "继承环境不同不是同一复现" in tester
    assert "矛盾证据先做因果分诊" in reviewer
    assert "测试失败本身只证明当前验证未通过" in reviewer
    assert "仅以“loading/pending 元素不存在”" in reviewer
    assert "相同命令在不同继承环境下不构成同一复现" in reviewer
    assert "只说明默认路径没有产生进展" in coordinator
    assert "不得仅凭相同错误现象、AC 集合或重复次数升级架构" in coordinator


def test_tester_prompt_keeps_runtime_and_future_stage_failures_in_their_owner():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "tester.md").read_text(encoding="utf-8")
    assert "effective_runtime_capabilities" in prompt
    assert "未来角色的份额留给 Orchestrator 后续阶段" in prompt
    assert "隔离可变用户状态不等于隐藏宿主已经安装的只读运行依赖" in prompt
    assert "test_bug" in prompt
    assert "Tester worktree 中对受版本控制文件的修改不会自动成为交付代码" in prompt
    assert "recommended_rework_target=\"coding\"" in prompt
    assert 'failure_class="requirement_mismatch"' in prompt
    assert 'recommended_rework_target="requirements"' in prompt


def test_planning_prompts_reject_future_workflow_states_as_product_ac():
    root = Path(__file__).resolve().parents[1]
    requirements_prompt = (root / "prompts/planner_requirements.md").read_text(
        encoding="utf-8"
    )
    specs_prompt = (root / "prompts/planner_specs.md").read_text(encoding="utf-8")
    assert "未来流程节点写成 AC" in requirements_prompt
    assert "属于 Orchestrator 的流程不变量或完成条件" in requirements_prompt
    assert "`retired_acceptance_criterion_ids` 清理历史合同" in requirements_prompt
    assert "指明该条件应回到 Requirements" in specs_prompt


def test_coordinator_rejects_unmapped_active_acceptance_criteria():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts/coordinator_approval_decision.md").read_text(
        encoding="utf-8"
    )
    assert "每个未退役 AC" in prompt
    assert "仍留在 Requirements 的 active `acceptance_criteria`" in prompt
    assert "不得用 `definition_of_done`" in prompt
    assert "未来 MAC 角色是否执行" in prompt
    assert "不得按具体 AC ID" in prompt
    assert "owner_contract.regression_acceptance_criterion_ids" in prompt
    assert "不需要重新出现在当前 Requirements" in prompt


def test_reviewer_uses_compiled_work_item_as_exact_acceptance_id_set():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "reviewer.md").read_text(encoding="utf-8")
    assert "WorkItem.in_scope_acceptance_criterion_ids" in prompt
    assert "唯一 coverage ID 合同" in prompt
    assert "WorkItem.regression_acceptance_criteria" in prompt
    assert "不得扩大 WorkItem 的当前 ID 集合" in prompt
    assert "`iteration` 快速流程的权威合同" in prompt
    assert "历史参考产物不适用这一阻塞条件" in prompt
    assert "不得仅因该标签未出现在继承的 `SPECS.json`" in prompt
    assert "所有验收合同中的 AC ID 并取并集" not in prompt
