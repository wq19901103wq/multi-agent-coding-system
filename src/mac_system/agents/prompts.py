import json
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
        rendered_base_prompt: str | None = None,
    ) -> str:
        agent_cfg = self.get_agent_config(agent_type)
        stage_cfg = self.get_stage_config(agent_type, stage)

        prompt_file = stage_cfg.get("prompt_file") or agent_cfg.get("prompt_file")
        if not prompt_file:
            raise ValueError(f"No prompt_file configured for {agent_type}/{stage}")

        base_path = Path(prompt_file)
        if not base_path.is_absolute():
            base_path = Path.cwd() / base_path

        # Templates contain task-scoped variables such as worktree and schema
        # paths.  When the orchestrator has already rendered the task contract,
        # reuse it here so the higher-priority system prompt never exposes
        # literal ``{{...}}`` placeholders.
        base_prompt = (
            rendered_base_prompt
            if rendered_base_prompt is not None
            else base_path.read_text(encoding="utf-8")
        )

        # The runtime's active stream-json path cannot rely on the model being
        # able to read a schema file from disk (chat-scoped agents deliberately
        # have no filesystem tools).  Inject the authoritative contracts into
        # the system prompt so every structured-output agent sees them before
        # generating a response.  Keep the JSON compact to limit repeated
        # prompt cost; the source schema files remain the single source of
        # truth rather than duplicating their fields in Markdown prompts.
        schema_sections = [
            self._schema_prompt_section(
                "本轮最终输出 JSON Schema（强制）",
                self.get_output_schema_path(agent_type, stage),
            )
        ]
        artifact_schema = self.get_artifact_schema_path(agent_type, stage)
        if artifact_schema is not None:
            schema_sections.append(
                self._schema_prompt_section(
                    "本轮产物文件 JSON Schema（强制）",
                    artifact_schema,
                )
            )
        base_prompt += "\n\n---\n\n" + "\n\n".join(schema_sections)

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

    @staticmethod
    def render_runtime_user_prompt(agent_type: str, stage: str | None = None) -> str:
        """Return a small kickoff turn for a fully rendered system contract.

        ``render_system_prompt`` already contains the complete task-scoped
        contract, schemas and examples. Sending the same rendered template as
        the user turn doubled large WorkItems and their historical facts.
        """
        scope = f"{agent_type}/{stage}" if stage else agent_type
        return (
            f"执行 system prompt 中已完整定义的本轮任务（{scope}）。"
            "直接使用其中的 WorkItem、上下文和输出合同，不要重复复述任务。"
        )

    @staticmethod
    def _schema_prompt_section(title: str, schema_path: Path) -> str:
        loaded: dict[Path, dict[str, Any]] = {}

        def _load(path: Path) -> dict[str, Any]:
            resolved = path.resolve()
            if resolved in loaded:
                return loaded[resolved]
            try:
                value = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Unable to load JSON schema {resolved}: {exc}"
                ) from exc
            loaded[resolved] = value
            for ref in _external_refs(value):
                _load((resolved.parent / ref).resolve())
            return value

        def _external_refs(value: Any) -> set[str]:
            refs: set[str] = set()
            if isinstance(value, dict):
                ref = value.get("$ref")
                if isinstance(ref, str) and ref and not ref.startswith("#"):
                    refs.add(ref.split("#", 1)[0])
                for child in value.values():
                    refs.update(_external_refs(child))
            elif isinstance(value, list):
                for child in value:
                    refs.update(_external_refs(child))
            return refs

        schema = _load(schema_path)
        primary = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        related_sections = []
        for path, related in loaded.items():
            if path == schema_path.resolve():
                continue
            related_sections.append(
                f"### 引用 Schema：{path.name}\n\n```json\n"
                + json.dumps(
                    related, ensure_ascii=False, separators=(",", ":")
                )
                + "\n```"
            )
        section = (
            f"## {title}\n\n"
            "以下 schema 是字段名、必填项、类型、枚举和条件约束的唯一权威来源。"
            "最终输出必须是一个符合它的 JSON 对象；few-shot 只用于解释业务语义，"
            "如示例与 schema 冲突，以 schema 为准。不要输出 Markdown 代码围栏。\n\n"
            f"```json\n{primary}\n```"
        )
        if related_sections:
            section += (
                "\n\n主 Schema 引用的外部 Schema 同样是强制合同：\n\n"
                + "\n\n".join(related_sections)
            )
        return section

    def render_task_prompt(
        self,
        agent_type: str,
        stage: str | None,
        project: Project | None,
        state: str,
        worktree: Path,
        context_files: list[Path],
        task_id: str | None = None,
        output_schema_path: str | None = None,
        artifact_schema_path: str | None = None,
        rejection_note: str = "",
        open_feedback: list[dict] | None = None,
        round_number: int = 1,
        qa_history: list[dict] | None = None,
        work_item_contract: dict[str, Any] | None = None,
        extra_variables: dict[str, str] | None = None,
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

        confirmed_brief = (
            getattr(project, "context", {}).get("confirmed_brief", {})
            if project and isinstance(getattr(project, "context", {}), dict)
            else {}
        )
        has_current_requirements = state == "PLANNING_REQUIREMENTS" and any(
            path.name == "REQUIREMENTS.json" for path in context_files
        )
        if has_current_requirements:
            # The brief is the source for the first Requirements compilation.
            # On re-entry, REQUIREMENTS.json is the versioned scope authority;
            # replaying the initial brief resurrects scope that a later round
            # deliberately narrowed or retired.  New corrections/decisions are
            # delivered independently through WorkItem facts and retry/feedback.
            confirmed_brief = {}
        question_guard_feedback = (
            getattr(project, "context", {}).get("question_guard_feedback", [])
            if project and isinstance(getattr(project, "context", {}), dict)
            else []
        )

        variables = {
            "project_id": project.id if project else "",
            "task_id": task_id or "",
            "agent_type": agent_type,
            "state": state,
            "task_type": project.task_type if project else "",
            "description": project.description or "" if project else "",
            "state_json_path": str(worktree / ".mac" / "STATE.json"),
            "task_json_path": str(worktree / ".mac" / "TASK.json"),
            # Both contracts are embedded verbatim in the higher-priority
            # system prompt by ``render_system_prompt``.  Do not expose their
            # MAC-repository absolute paths in the task prompt: models tend to
            # spend a Read call on those paths and then present MAC internals
            # as if they were project context in the user-visible progress
            # card.  The runtime still receives the real output_schema Path
            # out-of-band for deterministic validation.
            "output_schema_path": (
                "已完整嵌入 system prompt；禁止用 Read/Bash 再读取 schema 文件"
                if output_schema_path
                else ""
            ),
            "artifact_schema_path": (
                "已完整嵌入 system prompt；禁止用 Read/Bash 再读取 schema 文件"
                if artifact_schema_path or output_schema_path
                else ""
            ),
            "worktree_path": str(worktree),
            "workspace_path": (
                str(worktree)
                if agent_type in {"coder", "tester"}
                else (str(project.workspace_path) if project else str(worktree))
            ),
            "context_files": context_files_md,
            "rejection_note": rejection_note or "无",
            "round_number": round_number,
            "open_feedback_count": len(open_feedback) if open_feedback else 0,
            "confirmed_brief": json.dumps(confirmed_brief, ensure_ascii=False, indent=2),
            "requirements_authority": (
                "当前 REQUIREMENTS.json 是本轮范围权威；原始需求仅作历史背景。"
                "只按当前产物以及本轮 WorkItem、反馈和 Coordinator 指令修改，"
                "不得用早期较宽范围重新扩张当前合同。"
                if has_current_requirements
                else "这是首次生成 REQUIREMENTS.json；以已确认方案编译当前范围。"
            ),
            "question_guard_feedback": json.dumps(
                question_guard_feedback, ensure_ascii=False, indent=2
            ),
        }
        if extra_variables:
            variables.update(extra_variables)

        prompt = self._replace_variables(template, variables)

        # If the user rejected a previous result and provided concrete feedback,
        # append an explicit instruction so the agent cannot ignore it.
        feedback_texts = {
            str(fb.get("summary") or fb.get("user_comment") or "").strip()
            for fb in (open_feedback or [])
            if isinstance(fb, dict)
        }
        if rejection_note and rejection_note.strip() not in feedback_texts:
            prompt += (
                "\n\n## 用户驳回反馈（必须纳入本次输出）\n\n"
                f"用户针对上一版结果提出了以下修改意见，请在本次输出中充分体现：\n\n{rejection_note}\n"
            )

        # Surface open feedback packets so agents can address them and report
        # which ones they believe have been resolved.
        if open_feedback:
            prompt += "\n\n## 反馈上下文（必须处理或独立复核）\n\n"
            for idx, fb in enumerate(open_feedback, start=1):
                target = fb.get("recommended_rework_target") or "未指定"
                status = fb.get("status") or "open"
                prompt += (
                    f"{idx}. [{fb.get('source_stage')}] {fb.get('summary')}"
                    f"（状态：{status}；建议回退目标：{target}）\n"
                )
            prompt += (
                "\nopen/disputed 反馈需要处理；resolved 反馈只要求质量 Agent"
                " 独立复核先前的关闭结论。确认已解决时，在"
                " `verified_feedback_ids` 中引用对应 feedback_id。\n"
            )
            if agent_type in {
                "planner",
                "requirements_analyst",
                "architect",
                "spec_writer",
                "bug_analyst",
                "documentation_planner",
            }:
                prompt += (
                    "规划类 Agent 必须把 open/disputed 反馈实际落到本阶段"
                    " canonical `.mac/*.json` 产物，并更新 round/version；不得只在"
                    " stdout 中声称已修改。\n"
                )

        if work_item_contract:
            prompt += (
                "\n\n## WorkItem 合同（本轮任务的权威输入）\n\n"
                + json.dumps(work_item_contract, ensure_ascii=False, indent=2)
                + "\n\n执行边界：逐项满足 `completion_conditions`；只处理"
                " `in_scope_acceptance_criterion_ids` 与 `feedback_refs`；只把"
                " `relevant_facts` 当作确认事实。Runtime capability 表示本轮 Agent"
                " 可亲自执行的受保护动作，不等于目标产品不能实现同类集成；已列出的"
                "能力可直接使用，不得再次索要密钥。若合同包含 `reference_sources`，"
                "其中路径是系统放入当前 task worktree 的只读语义参考副本：可按需"
                "检查并把适用代码、数据、知识资产或测试复制/改造到正式交付路径，"
                "但不得把参考副本本身当成交付产物，也不得修改或访问原始源仓库。"
                "敏感值输入必须遵守两步协议：处理方式由普通选择题决定；只有确实"
                "需要用户提供值时，才另发 `sensitivity=secret`、"
                "`allow_free_text=true`、`options=[]` 且包含 `secret_env_var` 的"
                "独立问题。不得把选项 key、label 或推荐项当作密钥值。"
                "确有阻塞缺口时返回结构化"
                " question_batch 给 Coordinator；达到无进展上限后不得重复原方法。\n"
            )

        # Runtime cwd/add-dir settings constrain the common path, but models
        # can still mention absolute host paths in shell commands. Define the
        # matching evidence boundary for every project Agent: host/MAC files
        # are not project facts unless deliberately supplied as context.
        worktree_root = worktree.resolve()
        workspace_root = (
            Path(project.workspace_path).resolve() if project else worktree_root
        )
        authorized_roots = [str(worktree_root)]
        if workspace_root != worktree_root and agent_type not in {"coder", "tester"}:
            authorized_roots.append(str(workspace_root))
        authorized_roots.extend(
            str(path.resolve())
            for path in context_files
            if path.exists() and path.resolve() not in {worktree_root, workspace_root}
        )
        prompt += (
            "\n\n## 项目证据边界（必须遵守）\n\n"
            "本轮只允许把以下项目根目录或显式上下文文件中的内容作为事实依据：\n"
            + "\n".join(f"- {root}" for root in dict.fromkeys(authorized_roots))
            + "\n不得扫描父目录、MAC 系统源码/规格、用户主目录或其他项目来补全需求。"
            "即使工具技术上能读到，边界外内容也不是当前项目事实。"
            "若 WorkItem 缺少阻塞信息，返回 question_batch 给 Coordinator，"
            "不要自行越界寻找。\n"
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
            prompt += (
                "\n凭证类答案只显示注入状态和环境变量名；真实值由 Runtime 注入进程环境。"
                "禁止尝试从 Prompt、项目事实或日志中获取密钥原文。\n"
            )

        retry_feedback = str((extra_variables or {}).get("retry_feedback") or "").strip()
        if retry_feedback:
            prompt += (
                "\n\n## 上一次技术校验反馈（本次必须修正）\n\n"
                "上一次输出未被系统接受，原因如下：\n\n"
                f"{retry_feedback}\n\n"
                "保持用户意图和业务结论不变，只修正该格式或完成合同问题；"
                "不要重复输出同一个无效值。即使只需修正一个字段，也必须重新输出"
                "当前 Agent 的完整 JSON Schema 对象；禁止只输出证据片段、单个字段、"
                "解释文字或下一步计划。\n"
            )

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

    def get_artifact_schema_path(
        self,
        agent_type: str,
        stage: str | None = None,
    ) -> Path | None:
        """Return a stage artifact schema, distinct from the turn envelope."""
        stage_cfg = self.get_stage_config(agent_type, stage)
        schema_file = stage_cfg.get("artifact_schema")
        if not schema_file:
            return None
        path = Path(schema_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    def get_allowed_tools(
        self,
        agent_type: str,
        stage: str | None = None,
    ) -> list[str]:
        agent_cfg = self.get_agent_config(agent_type)
        stage_cfg = self.get_stage_config(agent_type, stage)
        if "allowed_tools" in stage_cfg:
            return list(stage_cfg["allowed_tools"])
        return agent_cfg.get("default_allowed_tools", ["Read", "Edit", "Write", "Bash"])
