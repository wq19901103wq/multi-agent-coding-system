import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import select

from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Project, Task
from mac_system.state_machine import StateMachine


class OrchestratorError(Exception):
    pass


class AgentOrchestrator:
    def __init__(
        self,
        project_service: ProjectService,
        worktree_manager: WorktreeManager,
        runtime: AgentRuntime,
        prompt_renderer: PromptRenderer,
        feishu_bridge=None,
    ):
        self.project_service = project_service
        self.worktree_manager = worktree_manager
        self.runtime = runtime
        self.prompt_renderer = prompt_renderer
        self.feishu_bridge = feishu_bridge
        self.settings = get_settings()

    async def tick(self, project_id: str) -> None:
        project = self.project_service.require_project(project_id)

        if project.status in ("paused", "cancelled"):
            return

        state_machine = StateMachine(project.task_type)
        state = project.current_state
        state_type = state_machine.state_type(state)

        if state_machine.is_terminal(state):
            return

        if state_type == "agent_execution":
            await self._execute_agent(project, state_machine)
        elif state_type == "human_confirmation":
            await self._handle_human_confirmation(project, state_machine)
        elif state_type == "human_input":
            await self._handle_human_input(project, state_machine)
        elif state_type == "system_action":
            await self._execute_system_action(project, state_machine)
        elif state_type == "idle":
            return

    async def _execute_agent(self, project: Project, state_machine: StateMachine) -> None:
        state = project.current_state
        agent_type = state_machine.agent_for(state)
        stage = state_machine.stage_for(state)

        if not agent_type:
            raise OrchestratorError(f"No agent configured for state {state}")

        # Check retry limits for non-deterministic states
        retry_key = state
        retry_count = project.context.get("retry_counts", {}).get(retry_key, 0)
        max_retries = self._max_retries_for_state(state)
        if retry_count >= max_retries:
            await self.project_service.transition_state(
                project.id, "fail_max_retries", triggered_by="orchestrator"
            )
            return

        # Create task
        task = await self.project_service.create_task(
            project_id=project.id,
            agent_type=agent_type,
            state=state,
            input_data={"description": project.description, "state": state},
        )

        # Create worktree
        try:
            worktree = self.worktree_manager.create_worktree(
                project_id=project.id,
                task_id=task.id,
                agent_type=agent_type,
                stage=stage,
            )
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Worktree creation failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Worktree creation failed: {e}")
            return

        await self.project_service.start_task(task.id, str(worktree))

        # Sync .mac/ files
        state_doc = self._build_state_doc(project)
        try:
            self.worktree_manager.sync_mac_files(
                project_id=project.id,
                worktree=worktree,
                state_doc=state_doc,
                task_input={
                    "task_id": task.id,
                    "agent_type": agent_type,
                    "state": state,
                    "input": {"description": project.description},
                },
            )
        except Exception as e:
            await self.project_service.fail_task(task.id, f"MAC sync failed: {e}")
            await self._handle_agent_failure(project, state, task, f"MAC sync failed: {e}")
            return

        # Render prompts
        try:
            system_prompt = self.prompt_renderer.render_system_prompt(agent_type, stage)
            task_prompt = self.prompt_renderer.render_task_prompt(
                agent_type=agent_type,
                stage=stage,
                project=project,
                state=state,
                worktree=worktree,
                context_files=self._context_files_for_state(project, state),
            )
            output_schema = self.prompt_renderer.get_output_schema_path(agent_type, stage)
            allowed_tools = self.prompt_renderer.get_allowed_tools(agent_type)
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Prompt rendering failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Prompt rendering failed: {e}")
            return

        # Execute agent
        try:
            result = await self.runtime.execute(
                agent_type=agent_type,
                task_prompt=task_prompt,
                system_prompt=system_prompt,
                workspace=Path(project.workspace_path),
                worktree=worktree,
                context_files=self._context_files_for_state(project, state),
                output_schema=output_schema,
                allowed_tools=allowed_tools,
                readonly_dirs=[Path(project.workspace_path)],
                timeout_seconds=self.settings.agent_timeout_seconds,
            )
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Runtime execution failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Runtime execution failed: {e}")
            return

        if not result.success or not result.output_json:
            error = result.logs or "Agent returned failure"
            await self.project_service.fail_task(task.id, error)
            await self._handle_agent_failure(project, state, task, error)
            return

        # Complete task
        await self.project_service.complete_task(
            task.id, result.output_json, result.cost_info
        )

        # Handle agent output
        await self._handle_agent_output(project, state_machine, task, worktree, result.output_json)

    async def _handle_agent_output(
        self,
        project: Project,
        state_machine: StateMachine,
        task: Task,
        worktree: Path,
        output: dict[str, Any],
    ) -> None:
        state = project.current_state
        event = output.get("next_state_event", "complete")

        # For planner stages, write the output JSON to .mac/ file
        if state in (
            "PLANNING_REQUIREMENTS",
            "PLANNING_ARCHITECTURE",
            "PLANNING_SPECS",
            "BUG_ANALYSIS",
            "DOC_OUTLINE",
        ):
            self._write_planner_output(project, state, output)

        # For coder/tester/reviewer, merge worktree changes
        if state in ("CODING", "TESTING", "REVIEW") and event == "complete":
            try:
                self.worktree_manager.merge_worktree(
                    project.id, task.id, task.agent_type
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"Merge failed: {e}")
                await self._handle_agent_failure(project, state, task, f"Merge failed: {e}")
                return

        # Transition state
        payload = self._build_transition_payload(output)
        await self.project_service.transition_state(
            project.id, event, payload=payload, triggered_by="agent"
        )

    async def _handle_agent_failure(
        self,
        project: Project,
        state: str,
        task: Task,
        error: str,
    ) -> None:
        retry_key = state
        retry_count = project.context.setdefault("retry_counts", {}).get(retry_key, 0)
        max_retries = self._max_retries_for_state(state)

        await self.project_service.log_event(
            project_id=project.id,
            level="ERROR",
            message=f"Agent {task.agent_type} failed at {state}: {error}",
            meta={"task_id": task.id, "state": state, "retry_count": retry_count},
        )

        if retry_count < max_retries:
            project.context["retry_counts"][retry_key] = retry_count + 1
            # Stay in same state for retry
            return

        await self.project_service.transition_state(
            project.id, "fail", triggered_by="orchestrator"
        )

    async def _handle_human_confirmation(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        state = project.current_state
        approval_config = project.approval_config
        require_confirmation = approval_config.get("require_confirmation_for", [])

        # Build question/options based on state
        question, options = self._build_confirmation_question(project, state)

        task = (
            self.project_service.session.exec(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
            .first()
        )

        approval = await self.project_service.create_approval(
            project_id=project.id,
            task_id=task.id if task else None,
            state=state,
            approval_type="confirmation",
            question=question,
            options=options,
        )

        # Auto-resolve if state not in require_confirmation_for
        if state not in require_confirmation:
            strategy = approval_config.get("auto_resolve_strategy", "none")
            if strategy == "project_manager_default":
                # For MVP, auto-confirm
                chosen = options[0]["action"]
                await self.project_service.respond_approval(
                    approval.id, chosen, "system_auto"
                )
                await self.project_service.transition_state(
                    project.id, chosen, triggered_by="system_auto"
                )
                return

        # Otherwise, send notification and wait
        if self.feishu_bridge:
            await self.feishu_bridge.send_confirmation_card(
                open_id=await self._get_creator_open_id(project.id),
                approval_id=approval.id,
                title=question,
                summary=self._build_summary_for_state(project, state),
                details={},
                options=options,
            )

    async def _handle_human_input(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        state = project.current_state
        approval_config = project.approval_config
        require_confirmation = approval_config.get("require_confirmation_for", [])

        question = self._build_input_question(project, state)

        approval = await self.project_service.create_approval(
            project_id=project.id,
            task_id=None,
            state=state,
            approval_type="input",
            question=question,
            options=[],
        )

        if state not in require_confirmation:
            strategy = approval_config.get("auto_resolve_strategy", "none")
            if strategy == "project_manager_default":
                # Auto-answer with a placeholder
                await self.project_service.respond_approval(
                    approval.id, "answer", "system_auto"
                )
                await self.project_service.transition_state(
                    project.id, "answer",
                    payload={"clarification_answer": "Auto-resolved by system"},
                    triggered_by="system_auto",
                )
                return

        if self.feishu_bridge:
            await self.feishu_bridge.send_input_card(
                open_id=await self._get_creator_open_id(project.id),
                approval_id=approval.id,
                title=question,
                question=question,
                context=self._build_summary_for_state(project, state),
            )

    async def _execute_system_action(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        state = project.current_state
        action = state_machine.get_state(state).get("action")

        if action == "git_commit_push":
            await self._git_commit_push(project)
            await self.project_service.transition_state(
                project.id, "complete", triggered_by="system"
            )
        else:
            await self.project_service.transition_state(
                project.id, "complete", triggered_by="system"
            )

    async def _git_commit_push(self, project: Project) -> None:
        import subprocess

        workspace = Path(project.workspace_path)
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)

        commit_message = f"[{project.task_type}] {project.name or project.id}\n\nAuto-generated by MAC System"
        subprocess.run(
            ["git", "commit", "-q", "-m", commit_message],
            cwd=workspace,
            check=True,
        )

        if self.settings.git_remote_url:
            branch_name = f"feature/{project.id}"
            subprocess.run(
                ["git", "push", self.settings.git_remote_url, f"HEAD:{branch_name}"],
                cwd=workspace,
                check=True,
            )

    def _max_retries_for_state(self, state: str) -> int:
        if state == "TESTING":
            return self.settings.max_test_retries
        if state == "REVIEW":
            return self.settings.max_review_retries
        return self.settings.max_agent_retries

    def _build_state_doc(self, project: Project) -> dict[str, Any]:
        state_path = Path(project.workspace_path) / ".mac" / "STATE.json"
        if state_path.exists():
            return json.loads(state_path.read_text(encoding="utf-8"))
        return {
            "version": "1.0",
            "project_id": project.id,
            "current_state": project.current_state,
            "previous_state": project.previous_state,
            "context": project.context,
            "history": [],
        }

    def _context_files_for_state(self, project: Project, state: str) -> list[Path]:
        workspace = Path(project.workspace_path)
        mac = workspace / ".mac"
        files = [mac / "STATE.json"]

        if state in ("PLANNING_REQUIREMENTS",):
            files.append(mac / "TASK.json")
        elif state in ("PLANNING_ARCHITECTURE",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json"])
        elif state in ("PLANNING_SPECS",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json", mac / "ARCHITECTURE.json"])
        elif state in ("CODING", "TESTING", "REVIEW"):
            files.extend([mac / "STATE.json", mac / "SPECS.json"])
            if (mac / "REQUIREMENTS.json").exists():
                files.append(mac / "REQUIREMENTS.json")
            if (mac / "ARCHITECTURE.json").exists():
                files.append(mac / "ARCHITECTURE.json")

        return [f for f in files if f.exists()]

    def _write_planner_output(self, project: Project, state: str, output: dict[str, Any]) -> None:
        workspace = Path(project.workspace_path)
        mapping = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = mapping.get(state)
        if not filename:
            return
        with open(workspace / ".mac" / filename, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    def _build_transition_payload(self, output: dict[str, Any]) -> dict[str, Any]:
        payload = {}
        if "resolved_decisions" in output:
            payload["resolved_decisions"] = output["resolved_decisions"]
        if "clarification_question" in output:
            payload["clarification_answer"] = output["clarification_question"]
        if "conflict" in output:
            payload["conflict_resolution"] = output["conflict"]
        if output.get("request_rollback_to"):
            # Map rollback request to appropriate payload
            payload["rollback_target"] = output["request_rollback_to"]
        return payload

    def _build_confirmation_question(self, project: Project, state: str) -> tuple[str, list[dict]]:
        if state == "REQUIREMENT_CONFIRM_PENDING":
            return (
                "请确认需求理解",
                [
                    {"action": "confirm", "label": "确认", "next_state": "PLANNING_ARCHITECTURE"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_REQUIREMENTS"},
                ],
            )
        elif state == "ARCHITECTURE_CONFIRM_PENDING":
            return (
                "请确认技术方案",
                [
                    {"action": "confirm", "label": "确认", "next_state": "PLANNING_SPECS"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_ARCHITECTURE"},
                    {"action": "select_option", "label": "选择其他选项", "next_state": "PLANNING_ARCHITECTURE"},
                ],
            )
        elif state == "SPEC_CONFIRM_PENDING":
            return (
                "请确认 Spec",
                [
                    {"action": "confirm", "label": "确认", "next_state": "CODING"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_SPECS"},
                ],
            )
        elif state == "ACCEPTANCE_PENDING":
            return (
                "请验收最终产物",
                [
                    {"action": "confirm", "label": "验收通过", "next_state": "MERGING"},
                    {"action": "reject", "label": "驳回到实现", "next_state": "CODING"},
                    {"action": "reject_to_specs", "label": "驳回到 Spec", "next_state": "PLANNING_SPECS"},
                    {"action": "reject_to_architecture", "label": "驳回到架构", "next_state": "PLANNING_ARCHITECTURE"},
                    {"action": "reject_to_requirements", "label": "驳回到需求", "next_state": "PLANNING_REQUIREMENTS"},
                ],
            )
        return (f"请确认 {state}", [{"action": "confirm", "label": "确认", "next_state": "COMPLETED"}])

    def _build_input_question(self, project: Project, state: str) -> str:
        if state == "CLARIFICATION_PENDING":
            return "Agent 请求澄清，请回复你的答案"
        elif state == "CONFLICT_PENDING":
            return "发现设计冲突，请裁决"
        return f"请输入 {state}"

    def _build_summary_for_state(self, project: Project, state: str) -> str:
        # Simplified summary
        return f"Project {project.id} at {state}"

    async def _get_creator_open_id(self, project_id: str) -> str | None:
        from sqlmodel import select
        from mac_system.models import ProjectUser

        result = self.project_service.session.exec(
            select(ProjectUser).where(ProjectUser.project_id == project_id)
        ).first()
        return result.open_id if result else None
