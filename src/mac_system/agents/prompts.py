import re
from pathlib import Path
from typing import Any

from mac_system.models import Project


class PromptRenderer:
    def __init__(
        self,
        agent_config: dict[str, Any],
        skills_dirs: list[Path] | None = None,
    ):
        self.agent_config = agent_config
        self.skills_dirs = skills_dirs or [
            Path.home() / ".kimi" / "skills",
        ]

    def get_agent_config(self, agent_type: str) -> dict[str, Any]:
        return self.agent_config.get("agents", {}).get(agent_type, {})

    def get_stage_config(self, agent_type: str, stage: str | None) -> dict[str, Any]:
        agent_cfg = self.get_agent_config(agent_type)
        if stage and "stages" in agent_cfg:
            return agent_cfg["stages"].get(stage, {})
        return agent_cfg

    def render_system_prompt(
        self,
        agent_type: str,
        stage: str | None = None,
    ) -> str:
        agent_cfg = self.get_agent_config(agent_type)
        stage_cfg = self.get_stage_config(agent_type, stage)

        prompt_file = stage_cfg.get("prompt_file") or agent_cfg.get("prompt_file")
        if not prompt_file:
            raise ValueError(f"No prompt_file configured for {agent_type}/{stage}")

        base_path = Path(prompt_file)
        if not base_path.is_absolute():
            base_path = Path.cwd() / base_path

        base_prompt = base_path.read_text(encoding="utf-8")

        # Inject skills
        skills = stage_cfg.get("skills") or agent_cfg.get("skills", [])
        skill_sections = []
        for skill_name in skills:
            skill_text = self._load_skill(skill_name)
            if skill_text:
                skill_sections.append(f"## Skill: {skill_name}\n\n{skill_text}")

        if skill_sections:
            return base_prompt + "\n\n---\n\n" + "\n\n---\n\n".join(skill_sections)
        return base_prompt

    def render_task_prompt(
        self,
        agent_type: str,
        stage: str | None,
        project: Project,
        state: str,
        worktree: Path,
        context_files: list[Path],
        task_id: str | None = None,
        output_schema_path: str | None = None,
        rejection_note: str = "",
        open_feedback: list[dict] | None = None,
        round_number: int = 1,
        qa_history: list[dict] | None = None,
    ) -> str:
        agent_cfg = self.get_agent_config(agent_type)
        stage_cfg = self.get_stage_config(agent_type, stage)
        prompt_file = stage_cfg.get("prompt_file") or agent_cfg.get("prompt_file")

        base_path = Path(prompt_file)
        if not base_path.is_absolute():
            base_path = Path.cwd() / base_path

        template = base_path.read_text(encoding="utf-8")

        # Build context files markdown
        context_files_md = "\n".join(
            f"- {f.name}: {f}" for f in context_files
        )

        variables = {
            "project_id": project.id,
            "task_id": task_id or "",
            "agent_type": agent_type,
            "state": state,
            "task_type": project.task_type,
            "description": project.description or "",
            "state_json_path": str(worktree / ".mac" / "STATE.json"),
            "task_json_path": str(worktree / ".mac" / "TASK.json"),
            "output_schema_path": output_schema_path or "",
            "worktree_path": str(worktree),
            "workspace_path": str(project.workspace_path),
            "context_files": context_files_md,
            "rejection_note": rejection_note or "无",
            "round_number": round_number,
            "open_feedback_count": len(open_feedback) if open_feedback else 0,
        }

        prompt = self._replace_variables(template, variables)

        # If the user rejected a previous result and provided concrete feedback,
        # append an explicit instruction so the agent cannot ignore it.
        if rejection_note:
            prompt += (
                "\n\n## 用户驳回反馈（必须纳入本次输出）\n\n"
                f"用户针对上一版结果提出了以下修改意见，请在本次输出中充分体现：\n\n{rejection_note}\n"
            )

        # Surface open feedback packets so agents can address them and report
        # which ones they believe have been resolved.
        if open_feedback:
            prompt += "\n\n## 待解决的反馈（必须在本次输出中处理或回应）\n\n"
            for idx, fb in enumerate(open_feedback, start=1):
                target = fb.get("recommended_rework_target") or "未指定"
                prompt += (
                    f"{idx}. [{fb.get('source_stage')}] {fb.get('summary')}"
                    f"（建议回退目标：{target}）\n"
                )
            prompt += (
                "\n请在输出中说明每条反馈是否已解决；"
                "如已解决，请在 `verified_feedback_ids` 中填入对应 feedback_id。\n"
            )

        # Inject Q&A history so the agent sees what was previously asked and
        # answered, and does not re-ask the same questions.
        if qa_history:
            prompt += "\n\n## 历史问答记录\n\n"
            for idx, entry in enumerate(qa_history, start=1):
                q = entry.get("question", "?")
                a = entry.get("answer_label") or entry.get("answer") or "(跳过)"
                s = entry.get("state", "")
                r = entry.get("round", 1)
                prompt += f"{idx}. Q：{q}\n   A：{a}（阶段：{s}，第{r}轮）\n"

        return prompt

    def _replace_variables(self, template: str, variables: dict[str, str]) -> str:
        result = template
        for key, value in variables.items():
            result = result.replace(f"{{{{{key}}}}}", str(value))
        return result

    def _load_skill(self, skill_name: str) -> str | None:
        for skills_dir in self.skills_dirs:
            skill_file = skills_dir / skill_name / "SKILL.md"
            if skill_file.exists():
                return skill_file.read_text(encoding="utf-8")
        return None

    def get_output_schema_path(
        self,
        agent_type: str,
        stage: str | None = None,
    ) -> Path:
        stage_cfg = self.get_stage_config(agent_type, stage)
        schema_file = stage_cfg.get("output_schema") or self.get_agent_config(agent_type).get("output_schema")
        if not schema_file:
            raise ValueError(f"No output_schema configured for {agent_type}/{stage}")
        path = Path(schema_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    def get_allowed_tools(
        self,
        agent_type: str,
    ) -> list[str]:
        agent_cfg = self.get_agent_config(agent_type)
        return agent_cfg.get("default_allowed_tools", ["Read", "Edit", "Write", "Bash"])
