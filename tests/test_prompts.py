
import pytest

from mac_system.agents.prompts import PromptRenderer


@pytest.fixture
def agent_config(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coder.md").write_text(
        "# Coder\nWorkdir: {{worktree_path}}\nProject: {{project_id}}\nDesc: {{description}}\nFiles:\n{{context_files}}"
    )
    (prompts_dir / "planner_requirements.md").write_text(
        "# Planner\nDesc: {{description}}\nBrief: {{confirmed_brief}}\nGuard: {{question_guard_feedback}}"
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
    prompt = renderer.render_system_prompt("coder")
    assert "# Coder" in prompt
    assert "{{worktree_path}}" in prompt  # variables not replaced in system prompt


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


def test_get_output_schema_path(agent_config, tmp_path):
    renderer = PromptRenderer(agent_config)
    path = renderer.get_output_schema_path("coder")
    assert path.exists()


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


def test_qa_history_includes_credential_handling_note(agent_config, tmp_path):
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
            "answer": "sk-test-redacted",
            "state": "TESTING",
            "round": 2,
        }],
    )

    assert "历史问答记录" in prompt
    assert "sk-test-redacted" in prompt
    assert "`.env`" in prompt
    assert "set -a; source .env; set +a" in prompt
    assert "严禁把凭证写入会被 git 提交的文件" in prompt


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
