import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import select

from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import get_settings
from mac_system.core.lock_manager import ProjectLockManager, get_default_lock_manager
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Project, Task
from mac_system.state_machine import StateMachine


class OrchestratorError(Exception):
    pass


logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(
        self,
        project_service: ProjectService,
        worktree_manager: WorktreeManager,
        runtime: AgentRuntime,
        prompt_renderer: PromptRenderer,
        feishu_bridge=None,
        lock_manager: ProjectLockManager | None = None,
    ):
        self.project_service = project_service
        self.worktree_manager = worktree_manager
        self.runtime = runtime
        self.prompt_renderer = prompt_renderer
        self.feishu_bridge = feishu_bridge
        self.settings = get_settings()
        self.lock_manager = lock_manager or get_default_lock_manager()

    async def tick(self, project_id: str) -> None:
        lock = await self.lock_manager.acquire(project_id)
        async with lock:
            await self._tick_unsafe(project_id)

    async def _tick_unsafe(self, project_id: str) -> None:
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
            output_schema = self.prompt_renderer.get_output_schema_path(agent_type, stage)
            task_prompt = self.prompt_renderer.render_task_prompt(
                agent_type=agent_type,
                stage=stage,
                project=project,
                state=state,
                worktree=worktree,
                context_files=self._context_files_for_state(project, state),
                task_id=task.id,
                output_schema_path=str(output_schema),
            )
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

    async def _execute_auxiliary_agent(
        self,
        project: Project,
        agent_type: str,
        input_data: dict[str, Any],
        context_files: list[Path] | None = None,
    ) -> dict[str, Any] | None:
        """Execute an auxiliary agent (e.g. project_manager, acceptance_evaluator).

        Returns the agent output JSON, or None if execution failed.
        Read-only auxiliary agents run directly in the main workspace so they do
        not create empty worktree branches or pollute the git history.
        """
        task = await self.project_service.create_task(
            project_id=project.id,
            agent_type=agent_type,
            state=project.current_state,
            input_data=input_data,
        )

        is_readonly_aux = agent_type in ("project_manager", "acceptance_evaluator")
        worktree = Path(project.workspace_path)
        await self.project_service.start_task(task.id, str(worktree))

        if not is_readonly_aux:
            try:
                worktree = self.worktree_manager.create_worktree(
                    project_id=project.id,
                    task_id=task.id,
                    agent_type=agent_type,
                    stage=None,
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"Worktree creation failed: {e}")
                return None

            await self.project_service.start_task(task.id, str(worktree))

            state_doc = self._build_state_doc(project)
            try:
                self.worktree_manager.sync_mac_files(
                    project_id=project.id,
                    worktree=worktree,
                    state_doc=state_doc,
                    task_input={
                        "task_id": task.id,
                        "agent_type": agent_type,
                        "state": project.current_state,
                        "input": input_data,
                    },
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"MAC sync failed: {e}")
                return None

        return await self._run_auxiliary_agent(project, task, worktree, input_data, context_files)

    async def _run_auxiliary_agent(
        self,
        project: Project,
        task: Task,
        worktree: Path,
        input_data: dict[str, Any],
        context_files: list[Path] | None,
    ) -> dict[str, Any] | None:
        """Render prompts, run the runtime, and complete or fail the task."""
        try:
            system_prompt = self.prompt_renderer.render_system_prompt(task.agent_type, None)
            output_schema = self.prompt_renderer.get_output_schema_path(task.agent_type, None)
            task_prompt = self.prompt_renderer.render_task_prompt(
                agent_type=task.agent_type,
                stage=None,
                project=project,
                state=project.current_state,
                worktree=worktree,
                context_files=context_files or self._context_files_for_state(project, project.current_state),
                task_id=task.id,
                output_schema_path=str(output_schema),
            )
            allowed_tools = self.prompt_renderer.get_allowed_tools(task.agent_type)
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Prompt rendering failed: {e}")
            return None

        try:
            result = await self.runtime.execute(
                agent_type=task.agent_type,
                task_prompt=task_prompt,
                system_prompt=system_prompt,
                workspace=Path(project.workspace_path),
                worktree=worktree,
                context_files=context_files or self._context_files_for_state(project, project.current_state),
                output_schema=output_schema,
                allowed_tools=allowed_tools,
                readonly_dirs=[Path(project.workspace_path)],
                timeout_seconds=self.settings.agent_timeout_seconds,
            )
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Runtime execution failed: {e}")
            return None

        if not result.success or not result.output_json:
            await self.project_service.fail_task(task.id, result.logs or "Agent returned failure")
            return None

        await self.project_service.complete_task(task.id, result.output_json, result.cost_info)
        return result.output_json

    async def _choose_action_with_project_manager(
        self,
        project: Project,
        state: str,
        question: str,
        options: list[dict],
    ) -> str | None:
        """Ask project_manager agent to choose an action for a confirmation."""
        valid_actions = {opt["action"] for opt in options}
        input_data = {
            "mode": "auto_confirmation",
            "state": state,
            "question": question,
            "options": options,
        }
        output = await self._execute_auxiliary_agent(
            project,
            agent_type="project_manager",
            input_data=input_data,
        )
        if output is None:
            return None
        suggested = output.get("suggested_action")
        if suggested in valid_actions:
            return suggested
        # Some schema values map to generic confirm/reject.
        if suggested == "continue" and "confirm" in valid_actions:
            return "confirm"
        return None

    async def _answer_input_with_project_manager(
        self,
        project: Project,
        state: str,
        question: str,
    ) -> str | None:
        """Ask project_manager agent to answer an input request."""
        input_data = {
            "mode": "auto_answer",
            "state": state,
            "question": question,
        }
        output = await self._execute_auxiliary_agent(
            project,
            agent_type="project_manager",
            input_data=input_data,
        )
        if output is None:
            return None
        answer = output.get("clarification_answer")
        if answer:
            return answer
        return output.get("reply")

    async def _evaluate_acceptance(
        self,
        project: Project,
    ) -> dict[str, Any] | None:
        """Run acceptance_evaluator and return a recommendation dict."""
        output = await self._execute_auxiliary_agent(
            project,
            agent_type="acceptance_evaluator",
            input_data={"mode": "acceptance_evaluation"},
        )
        if output is None:
            return None
        recommendation = output.get("recommendation")
        if recommendation:
            return {
                "action": recommendation.get("action"),
                "reason": recommendation.get("reason"),
                "summary": output.get("summary"),
            }
        return {
            "result": output.get("result"),
            "summary": output.get("summary"),
        }

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
                    project.id, task.id, task.agent_type, worktree
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"Merge failed: {e}")
                await self._handle_agent_failure(project, state, task, f"Merge failed: {e}")
                return
            try:
                self.worktree_manager.remove_worktree(
                    project.id, task.id, task.agent_type, worktree
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove worktree %s after merge: %s", worktree, e
                )

        # Transition state
        payload = self._build_transition_payload(output)
        await self.project_service.transition_state(
            project.id, event, payload=payload, triggered_by="agent"
        )

        # Clean up the now-unneeded worktree for non-merge states.
        if state not in ("CODING", "TESTING", "REVIEW"):
            try:
                self.worktree_manager.remove_worktree(
                    project.id, task.id, task.agent_type, worktree
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove worktree %s after transition: %s", worktree, e
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
            # Reassign the context dict so SQLAlchemy detects the JSON mutation.
            new_counts = dict(project.context.get("retry_counts", {}))
            new_counts[retry_key] = retry_count + 1
            project.context = {**project.context, "retry_counts": new_counts}
            # Persist the retry count immediately so the next tick sees it even
            # if the outer session_scope rolls back for unrelated reasons.
            self.project_service.session.commit()
            # Stay in same state for retry
            return

        await self.project_service.transition_state(
            project.id, "fail", triggered_by="orchestrator"
        )

    def _state_requires_confirmation(
        self,
        state: str,
        state_type: str,
        approval_config: dict,
    ) -> bool:
        """Return True if the state should be confirmed by a human.

        A state requires confirmation when either:
        - its state name is listed in ``require_confirmation_for``; or
        - its state type is listed in ``require_confirmation_for_types``.
        """
        require_names = approval_config.get("require_confirmation_for", [])
        require_types = approval_config.get("require_confirmation_for_types", [])
        return state in require_names or state_type in require_types

    async def _handle_human_confirmation(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        state = project.current_state
        approval_config = project.approval_config
        state_type = state_machine.state_type(state)
        requires_confirmation = self._state_requires_confirmation(
            state, state_type, approval_config
        )

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

        # Auto-resolve if state does not require confirmation
        if not requires_confirmation:
            strategy = approval_config.get("auto_resolve_strategy", "none")
            if strategy == "project_manager_default":
                chosen = await self._choose_action_with_project_manager(
                    project, state, question, options
                )
                if chosen is None:
                    chosen = options[0]["action"]
                    logger.warning(
                        "project_manager agent failed to choose an action for %s, falling back to %s",
                        project.id, chosen,
                    )
                await self.project_service.respond_approval(
                    approval.id, chosen, "system_auto"
                )
                await self.project_service.transition_state(
                    project.id, chosen, triggered_by="system_auto"
                )
                return

        # Enrich acceptance cards with an evaluator recommendation.
        acceptance_recommendation = None
        if state == "ACCEPTANCE_PENDING":
            acceptance_recommendation = await self._evaluate_acceptance(project)

        # Otherwise, send notification and wait
        if self.feishu_bridge:
            details = {"state": state}
            if acceptance_recommendation:
                details["acceptance_recommendation"] = acceptance_recommendation
            receive_id, receive_id_type = self._resolve_project_recipient(project)
            await self.feishu_bridge.send_confirmation_card(
                open_id=receive_id,
                approval_id=approval.id,
                title=question,
                summary=self._build_summary_for_state(project, state),
                details=details,
                options=options,
                receive_id_type=receive_id_type,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
            )

    async def _handle_human_input(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        state = project.current_state
        approval_config = project.approval_config
        state_type = state_machine.state_type(state)
        requires_confirmation = self._state_requires_confirmation(
            state, state_type, approval_config
        )

        question = self._build_input_question(project, state)

        approval = await self.project_service.create_approval(
            project_id=project.id,
            task_id=None,
            state=state,
            approval_type="input",
            question=question,
            options=[],
        )

        if not requires_confirmation:
            strategy = approval_config.get("auto_resolve_strategy", "none")
            if strategy == "project_manager_default":
                answer = await self._answer_input_with_project_manager(
                    project, state, question
                )
                if answer is None:
                    answer = "Auto-resolved by system"
                    logger.warning(
                        "project_manager agent failed to answer input for %s, falling back to placeholder",
                        project.id,
                    )
                await self.project_service.respond_approval(
                    approval.id, "answer", "system_auto"
                )
                await self.project_service.transition_state(
                    project.id, "answer",
                    payload={"clarification_answer": answer},
                    triggered_by="system_auto",
                )
                return

        if self.feishu_bridge:
            receive_id, receive_id_type = self._resolve_project_recipient(project)
            await self.feishu_bridge.send_input_card(
                open_id=receive_id,
                approval_id=approval.id,
                title=question,
                question=question,
                context=self._build_summary_for_state(project, state),
                receive_id_type=receive_id_type,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
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

        # If there is nothing to commit (e.g. all changes were already merged
        # into previous commits), skip the commit instead of failing.
        diff_check = subprocess.run(
            ["git", "diff-index", "--quiet", "--cached", "HEAD"],
            cwd=workspace,
            capture_output=True,
        )
        if diff_check.returncode == 0:
            return

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
        key = (project.task_type, state)

        confirm_options = {
            ("feature", "REQUIREMENT_CONFIRM_PENDING"): (
                "请确认需求理解",
                [
                    {"action": "confirm", "label": "确认", "next_state": "PLANNING_ARCHITECTURE"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_REQUIREMENTS"},
                ],
            ),
            ("feature", "ARCHITECTURE_CONFIRM_PENDING"): (
                "请确认技术方案",
                [
                    {"action": "confirm", "label": "确认", "next_state": "PLANNING_SPECS"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_ARCHITECTURE"},
                    {"action": "select_option", "label": "选择其他选项", "next_state": "PLANNING_ARCHITECTURE"},
                ],
            ),
            ("feature", "SPEC_CONFIRM_PENDING"): (
                "请确认 Spec",
                [
                    {"action": "confirm", "label": "确认", "next_state": "CODING"},
                    {"action": "reject", "label": "驳回", "next_state": "PLANNING_SPECS"},
                ],
            ),
            ("bug_fix", "BUG_FIX_CONFIRM_PENDING"): (
                "请确认修复方案",
                [
                    {"action": "confirm", "label": "确认", "next_state": "CODING"},
                    {"action": "reject", "label": "驳回", "next_state": "BUG_ANALYSIS"},
                ],
            ),
            ("documentation", "OUTLINE_CONFIRM_PENDING"): (
                "请确认文档大纲",
                [
                    {"action": "confirm", "label": "确认", "next_state": "CODING"},
                    {"action": "reject", "label": "驳回", "next_state": "DOC_OUTLINE"},
                ],
            ),
        }

        if key in confirm_options:
            return confirm_options[key]

        if state == "ACCEPTANCE_PENDING":
            restart_option = {
                "action": "restart_with_type",
                "label": "重新归类创建",
                "next_state": "CANCELLED",
            }
            if project.task_type == "feature":
                return (
                    "请验收最终产物",
                    [
                        {"action": "confirm", "label": "验收通过", "next_state": "MERGING"},
                        {"action": "reject", "label": "驳回到实现", "next_state": "CODING"},
                        {"action": "reject_to_specs", "label": "驳回到 Spec", "next_state": "PLANNING_SPECS"},
                        {"action": "reject_to_architecture", "label": "驳回到架构", "next_state": "PLANNING_ARCHITECTURE"},
                        {"action": "reject_to_requirements", "label": "驳回到需求", "next_state": "PLANNING_REQUIREMENTS"},
                        restart_option,
                    ],
                )
            if project.task_type == "bug_fix":
                return (
                    "请验收最终产物",
                    [
                        {"action": "confirm", "label": "验收通过", "next_state": "MERGING"},
                        {"action": "reject", "label": "驳回到实现", "next_state": "CODING"},
                        {"action": "reject_to_analysis", "label": "驳回到分析", "next_state": "BUG_ANALYSIS"},
                        restart_option,
                    ],
                )
            if project.task_type == "documentation":
                return (
                    "请验收最终产物",
                    [
                        {"action": "confirm", "label": "验收通过", "next_state": "MERGING"},
                        {"action": "reject", "label": "驳回到实现", "next_state": "CODING"},
                        {"action": "reject_to_outline", "label": "驳回到大纲", "next_state": "DOC_OUTLINE"},
                        restart_option,
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

    def _get_creator_open_id(self, project_id: str) -> str | None:
        from sqlmodel import select
        from mac_system.models import ProjectUser

        result = self.project_service.session.exec(
            select(ProjectUser).where(ProjectUser.project_id == project_id)
        ).first()
        return result.open_id if result else None

    def _resolve_project_recipient(self, project: Project) -> tuple[str | None, str]:
        """Return the Feishu recipient for a project.

        If the project was created from a group chat, reply to the group so
        everyone in the chat sees the card; otherwise reply to the creator
        via open_id.
        """
        if project.feishu_chat_type == "group" and project.feishu_chat_id:
            return project.feishu_chat_id, "chat_id"
        return self._get_creator_open_id(project.id), "open_id"
