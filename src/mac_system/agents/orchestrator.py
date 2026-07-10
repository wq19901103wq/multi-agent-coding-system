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


def _flatten_item(item: Any) -> str:
    """Convert a dict/list item to a readable string, never raw JSON repr."""
    if isinstance(item, dict):
        # Decision objects: use topic + selected option.
        topic = item.get("topic") or item.get("decision") or ""
        if topic:
            options = item.get("options", [])
            if isinstance(options, list) and options:
                chosen = options[0]
                if isinstance(chosen, dict):
                    label = chosen.get("label") or chosen.get("key") or str(chosen)
                    return f"{topic}：{label}"
            return topic

        # Module objects: name + responsibility.
        name = item.get("name") or ""
        resp = item.get("responsibility") or ""
        if name and resp:
            return f"{name} — {resp}"
        if name:
            return str(name)

        # Generic: show key-value pairs compactly, skip large nested values.
        parts = []
        for k, v in item.items():
            if isinstance(v, (dict, list)):
                continue
            parts.append(f"{k}: {v}")
        return "; ".join(parts) if parts else str(item)
    if isinstance(item, list):
        return ", ".join(str(i) for i in item)
    return str(item)


def _split_markdown_sections(text: str, max_chars: int = 1600) -> list[str]:
    """Split Markdown text into readable chunks at section boundaries.

    Tries to keep sections together; only splits a section if it alone exceeds
    ``max_chars``.  Returns at least one chunk.
    """
    if len(text) <= max_chars:
        return [text]

    # Split on lines that look like section headers (**title** or ## title).
    import re
    header_pattern = re.compile(r"^(\*\*[^*]+\*\*|#{1,6}\s+.+)$", re.MULTILINE)
    sections: list[str] = []
    last = 0
    for m in header_pattern.finditer(text):
        if m.start() > last:
            sections.append(text[last:m.start()].strip())
        last = m.start()
    if last < len(text):
        sections.append(text[last:].strip())

    # Drop empty leading chunk if the text started with a header.
    sections = [s for s in sections if s]

    chunks: list[str] = []
    current = ""
    for section in sections:
        if len(section) > max_chars:
            # Flush current first.
            if current:
                chunks.append(current)
                current = ""
            # Hard-split oversized section by paragraphs.
            paragraphs = [p for p in section.split("\n\n") if p.strip()]
            for para in paragraphs:
                if len(current) + len(para) + 2 > max_chars:
                    if current:
                        chunks.append(current)
                    current = para
                else:
                    current = f"{current}\n\n{para}".strip() if current else para
        else:
            if current and len(current) + len(section) + 2 > max_chars:
                chunks.append(current)
                current = section
            else:
                current = f"{current}\n\n{section}".strip() if current else section
    if current:
        chunks.append(current)
    return chunks if chunks else [text]


def _mention_prefix(open_id: str | None) -> str:
    """Return a Feishu @ mention tag for use in group text messages."""
    if not open_id:
        return ""
    return f'<at user_id="{open_id}">@用户</at> '


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

        # Include any rejection feedback the user provided.
        rejection_note = project.context.pop("latest_rejection_feedback", "")

        # Create task
        task = await self.project_service.create_task(
            project_id=project.id,
            agent_type=agent_type,
            state=state,
            input_data={
                "description": project.description,
                "state": state,
                "rejection_note": rejection_note,
            },
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

        # For planner stages, copy the .mac/ output files written by the agent
        # from the worktree back to the main workspace.  (Coder/tester/reviewer
        # stages go through a full git merge later in this method.)
        if state in (
            "PLANNING_REQUIREMENTS",
            "PLANNING_ARCHITECTURE",
            "PLANNING_SPECS",
            "BUG_ANALYSIS",
            "DOC_OUTLINE",
        ):
            self._copy_mac_outputs(project, worktree, output, state)

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
        from_state = project.current_state
        await self.project_service.transition_state(
            project.id, event, payload=payload, triggered_by="agent"
        )

        # Send a brief status notification so the user can follow progress.
        await self._notify_state_transition(project, task, from_state)

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
        retry_count = project.context.get("retry_counts", {}).get(retry_key, 0)
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

        # Track how many times this confirmation state has been entered.
        rounds = dict(project.context.get("confirmation_rounds", {}))
        round_number = rounds.get(state, 0) + 1
        rounds[state] = round_number
        project.context = {**project.context, "confirmation_rounds": rounds}

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
            payload={"round_number": round_number},
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
                # Notify the user that the system auto-resolved this step.
                await self._notify_auto_resolve(project, state, chosen)
                return

        # Enrich acceptance cards with an evaluator recommendation.
        acceptance_recommendation = None
        if state == "ACCEPTANCE_PENDING":
            acceptance_recommendation = await self._evaluate_acceptance(project)

        # Otherwise, send notification and wait
        if self.feishu_bridge:
            sm = StateMachine(project.task_type)
            state_display = sm.display_name(state)
            project_name = project.name or project.id
            title = f"{question} — {project_name}"

            state_details = self._build_details_for_state(project, state)
            details: dict[str, Any] = {"当前阶段": state_display}
            if state_details:
                details.update(state_details)
            if acceptance_recommendation:
                rec_summary = acceptance_recommendation.get("reason") or acceptance_recommendation.get("summary", "")
                if rec_summary:
                    details["验收评估"] = rec_summary[:200]
                if acceptance_recommendation.get("action") == "confirm":
                    details["建议操作"] = "验收通过"
            summary_text = self._build_summary_for_state(project, state)
            receive_id, receive_id_type = self._resolve_project_recipient(project)
            creator_open_id = self._get_creator_open_id(project.id)

            # Feishu interactive card JSON is best kept under ~30 KB and the
            # mobile client renders long lark_md blocks poorly.  When the summary
            # is long we split it into multiple display cards (a la feishu-connect)
            # and keep the action buttons on the final confirmation card so the
            # user reads the full content before deciding.
            CARD_SUMMARY_LIMIT = 900
            is_long = len(summary_text) > CARD_SUMMARY_LIMIT

            if is_long:
                chunks = _split_markdown_sections(summary_text, max_chars=1600)
                total = len(chunks)
                for idx, chunk in enumerate(chunks, start=1):
                    try:
                        await self.feishu_bridge.send_content_card(
                            open_id=receive_id,
                            title=title,
                            content=chunk,
                            page=idx,
                            total=total,
                            color="blue",
                            receive_id_type=receive_id_type,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to send summary content card for %s: %s", project.id, exc
                        )
                        break
                # Final card only carries metadata and action buttons.
                card_summary = "请查看上方完整内容后选择操作"
            else:
                card_summary = summary_text

            try:
                await self.feishu_bridge.send_confirmation_card(
                    open_id=receive_id,
                    approval_id=approval.id,
                    title=title,
                    summary=card_summary,
                    details=details,
                    options=options,
                    receive_id_type=receive_id_type,
                    chat_id=project.feishu_chat_id,
                    chat_type=project.feishu_chat_type,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to send confirmation card for %s: %s", project.id, exc
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
            creator_open_id = self._get_creator_open_id(project.id)
            context_text = self._build_summary_for_state(project, state)

            # Keep the input card compact; stream long context as display cards.
            INPUT_CONTEXT_LIMIT = 800
            is_long_context = len(context_text) > INPUT_CONTEXT_LIMIT

            if is_long_context:
                chunks = _split_markdown_sections(context_text, max_chars=1600)
                total = len(chunks)
                for idx, chunk in enumerate(chunks, start=1):
                    try:
                        await self.feishu_bridge.send_content_card(
                            open_id=receive_id,
                            title=question,
                            content=chunk,
                            page=idx,
                            total=total,
                            color="orange",
                            receive_id_type=receive_id_type,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to send input context card for %s: %s", project.id, exc
                        )
                        break
                card_context = "请查看上方完整上下文后回复"
            else:
                card_context = context_text

            await self.feishu_bridge.send_input_card(
                open_id=receive_id,
                approval_id=approval.id,
                title=question,
                question=question,
                context=card_context,
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

    def _copy_mac_outputs(
        self, project: Project, worktree: Path, output: dict[str, Any], state: str
    ) -> None:
        """Copy .mac/ output files from the worktree to the main workspace.

        The real agent writes REQUIREMENTS.json / ARCHITECTURE.json / etc.
        inside the worktree; for FakeRuntime (tests) no files are on disk so
        we fall back to writing the output dict directly.
        """
        import shutil

        mapping = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = mapping.get(state)
        if filename is None:
            return

        dst_mac = Path(project.workspace_path) / ".mac"
        dst_mac.mkdir(parents=True, exist_ok=True)
        src_file = worktree / ".mac" / filename

        if src_file.is_file():
            shutil.copy2(src_file, dst_mac / filename)
            logger.debug("Copied %s from worktree to workspace", filename)
        else:
            # Fallback: write the output dict (used by FakeRuntime in tests).
            with open(dst_mac / filename, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            logger.debug("Wrote %s from agent output (fallback)", filename)

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
        """Read the relevant .mac/ output file and return a human-readable summary.

        The output is pure plain text / lark-md — no JSON key-value pairs.
        """
        if state == "ACCEPTANCE_PENDING":
            return self._build_acceptance_summary(project)

        workspace = Path(project.workspace_path)
        mac = workspace / ".mac"

        file_map = {
            "REQUIREMENT_CONFIRM_PENDING": "REQUIREMENTS.json",
            "ARCHITECTURE_CONFIRM_PENDING": "ARCHITECTURE.json",
            "SPEC_CONFIRM_PENDING": "SPECS.json",
            "BUG_FIX_CONFIRM_PENDING": "BUG_ANALYSIS.json",
            "OUTLINE_CONFIRM_PENDING": "DOC_OUTLINE.json",
        }
        filename = file_map.get(state)
        if filename is None:
            return self._fallback_summary(project, state)

        path = mac / filename
        if not path.exists():
            return self._fallback_summary(project, state)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._fallback_summary(project, state)

        parts: list[str] = []

        # SPECS.json has a "specs" list instead of a single summary.
        if data.get("specs") and isinstance(data["specs"], list):
            spec_parts = []
            for spec in data["specs"]:
                if isinstance(spec, dict):
                    name = spec.get("name") or spec.get("title") or ""
                    desc = spec.get("description") or spec.get("summary") or ""
                    if name and desc:
                        spec_parts.append(f"• {name}：{desc}")
                    elif name:
                        spec_parts.append(f"• {name}")
                elif isinstance(spec, str):
                    spec_parts.append(f"• {spec}")
            if spec_parts:
                parts.append("**详细规格**\n" + "\n".join(spec_parts))

        if data.get("summary"):
            parts.append(data["summary"])
        if data.get("goals"):
            parts.append("**目标**\n" + "\n".join(f"• {g}" for g in data["goals"]))
        if data.get("acceptance_criteria"):
            parts.append("**验收标准**\n" + "\n".join(f"• {a}" for a in data["acceptance_criteria"]))
        if data.get("root_cause"):
            parts.append(f"**根因分析**\n{data['root_cause']}")
        if data.get("fix_strategy"):
            parts.append(f"**修复方案**\n{data['fix_strategy']}")
        if data.get("affected_files"):
            parts.append("**涉及文件**\n" + "\n".join(f"• {f}" for f in data["affected_files"]))
        if data.get("tech_stack"):
            parts.append(f"**技术栈**\n{', '.join(data['tech_stack'])}")
        if data.get("modules"):
            parts.append("**模块划分**\n" + "\n".join(
                f"• {_flatten_item(m)}" for m in data["modules"]
            ))
        if data.get("decisions"):
            parts.append("**关键决策**\n" + "\n".join(
                f"• {_flatten_item(d)}" for d in data["decisions"]
            ))
        if data.get("risks"):
            parts.append("**风险**\n" + "\n".join(
                f"• {_flatten_item(r)}" for r in data["risks"]
            ))

        if not parts:
            return self._fallback_summary(project, state)
        return "\n\n".join(parts)

    def _build_acceptance_summary(self, project: Project) -> str:
        """Build a human-readable summary for the acceptance gate.

        Combines the latest completed coder/tester/reviewer/acceptance_evaluator
        task outputs so the user sees what was built, tested, reviewed and
        recommended without reading raw JSON.
        """
        from sqlmodel import select

        parts: list[str] = []

        # Load task outputs from DB, newest first.
        tasks = self.project_service.session.exec(
            select(Task)
            .where(Task.project_id == project.id, Task.status == "completed")
            .order_by(Task.updated_at.desc())
        ).all()

        acceptance_task = next((t for t in tasks if t.agent_type == "acceptance_evaluator"), None)
        review_task = next((t for t in tasks if t.agent_type == "reviewer"), None)
        test_task = next((t for t in tasks if t.agent_type == "tester"), None)
        code_task = next((t for t in tasks if t.agent_type == "coder"), None)

        if code_task and code_task.output_data:
            out = code_task.output_data
            files = out.get("modified_files") or []
            if files and isinstance(files, list):
                file_lines = []
                for f in files:
                    if isinstance(f, dict):
                        path = f.get("path") or f.get("file") or ""
                        desc = f.get("description") or ""
                        if path and desc:
                            file_lines.append(f"• {path}：{desc}")
                        elif path:
                            file_lines.append(f"• {path}")
                    elif isinstance(f, str):
                        file_lines.append(f"• {f}")
                if file_lines:
                    parts.append("**实现文件**\n" + "\n".join(file_lines))
            notes = out.get("notes")
            if notes and isinstance(notes, list):
                parts.append("**实现备注**\n" + "\n".join(f"• {n}" for n in notes if isinstance(n, str)))

        if test_task and test_task.output_data:
            out = test_task.output_data
            report = out.get("report", "")
            if report:
                parts.append(f"**测试报告**\n{report}")
            else:
                passed = out.get("passed")
                coverage = out.get("coverage")
                status = "通过" if passed else "未通过"
                line = f"测试状态：{status}"
                if coverage is not None:
                    line += f"，覆盖率 {coverage:.0%}"
                parts.append(f"**测试结果**\n{line}")
            failures = out.get("failures") or []
            if failures and isinstance(failures, list):
                parts.append("**失败项**\n" + "\n".join(f"• {_flatten_item(f)}" for f in failures))

        if review_task and review_task.output_data:
            out = review_task.output_data
            result = out.get("result", "")
            issues = out.get("issues") or []
            summary = out.get("summary", "")
            if summary:
                parts.append(f"**Review 结论**\n{summary}")
            elif result:
                parts.append(f"**Review 结论**\n{result}")
            if issues and isinstance(issues, list):
                issue_lines = []
                for issue in issues[:5]:
                    if isinstance(issue, dict):
                        sev = issue.get("severity", "")
                        desc = issue.get("description", "")
                        file = issue.get("file", "")
                        if desc:
                            label = f"[{sev}]" if sev else ""
                            loc = f" {file}" if file else ""
                            issue_lines.append(f"• {label}{loc} {desc}")
                    elif isinstance(issue, str):
                        issue_lines.append(f"• {issue}")
                if issue_lines:
                    parts.append("**Review 问题**\n" + "\n".join(issue_lines))

        if acceptance_task and acceptance_task.output_data:
            out = acceptance_task.output_data
            summary = out.get("summary", "")
            reason = out.get("reason") or out.get("recommendation", {}).get("reason", "")
            action = out.get("action") or out.get("recommendation", {}).get("action", "")
            rec_parts: list[str] = []
            if summary:
                rec_parts.append(summary)
            if reason:
                rec_parts.append(f"理由：{reason}")
            if action:
                rec_parts.append(f"建议：{'验收通过' if action == 'confirm' else '需要修改'}")
            if rec_parts:
                parts.append("**验收评估**\n" + "\n".join(rec_parts))

        if not parts:
            return self._fallback_summary(project, "ACCEPTANCE_PENDING")
        return "\n\n".join(parts)

    def _fallback_summary(self, project: Project, state: str) -> str:
        name = project.name or project.id
        sm = StateMachine(project.task_type)
        display = sm.display_name(state)
        return f"项目 {name}\n当前阶段：{display}（尚未生成输出文件）"

    def _build_details_for_state(self, project: Project, state: str) -> dict[str, Any]:
        """Return compact key-value fields for a confirmation card (Chinese labels only).

        These fields are intentionally small metadata — the main content lives in
        the card summary and, when long, in follow-up text messages.  Keeping
        details short avoids duplicating long text inside the card.
        """
        # Agent label for the most recently completed task.
        agent_label = self._last_agent_label(project)
        details: dict[str, str] = {}
        if agent_label:
            details["生成者"] = agent_label

        # For acceptance, add a compact recommendation line if available.
        if state == "ACCEPTANCE_PENDING":
            from sqlmodel import select

            acceptance_task = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == "acceptance_evaluator",
                    Task.status == "completed",
                )
                .order_by(Task.updated_at.desc())
                .limit(1)
            ).first()
            if acceptance_task and acceptance_task.output_data:
                out = acceptance_task.output_data
                action = out.get("action") or out.get("recommendation", {}).get("action", "")
                if action == "confirm":
                    details["评估建议"] = "通过"
                elif action:
                    details["评估建议"] = "需修改"

        return details

    def _last_agent_label(self, project: Project) -> str:
        """Human-readable label for the last completed agent (e.g. 需求分析师)."""
        agent_label_map = {
            "planner": "需求分析师",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理 Agent",
            "acceptance_evaluator": "验收评估 Agent",
        }
        tasks = self.project_service.session.exec(
            select(Task)
            .where(Task.project_id == project.id, Task.status == "completed")
            .order_by(Task.updated_at.desc())
            .limit(1)
        ).all()
        if tasks:
            return agent_label_map.get(tasks[0].agent_type, tasks[0].agent_type)
        return ""

    async def _notify_auto_resolve(
        self, project: Project, from_state: str, chosen: str
    ) -> None:
        """Notify the user that a confirmation was auto-resolved."""
        if self.feishu_bridge is None:
            return
        sm = StateMachine(project.task_type)
        display = sm.display_name(project.current_state)
        name = project.name or project.id
        label = {"confirm": "已自动确认", "reject": "已自动驳回"}.get(chosen, f"已自动处理({chosen})")
        line = f"**{name}**\n{label}\n当前阶段：{display}"
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            await self.feishu_bridge.send_text(
                receive_id, line, receive_id_type=receive_id_type,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send auto-resolve notification for %s: %s", project.id, exc
            )

    async def _notify_state_transition(
        self, project: Project, task: Task | None, from_state: str
    ) -> None:
        """Send a rich card notification when the project transitions between stages.

        The card reflects the correct semantics: a stage is only "completed" when
        no human confirmation is required, or when the user has already confirmed.
        """
        if self.feishu_bridge is None:
            return

        # Only notify for meaningful transitions.
        skip_states = {"INIT", "PAUSED", "CANCELLED"}
        if from_state in skip_states:
            return

        sm = StateMachine(project.task_type)
        to_state = project.current_state
        from_state_type = sm.state_type(from_state)
        to_state_type = sm.state_type(to_state)
        from_agent = self._agent_label_for_state(from_state)
        to_agent = self._agent_label_for_state(to_state)
        name = project.name or project.id

        # Round number for confirmation states.
        round_number = None
        if from_state_type == "human_confirmation":
            rounds = project.context.get("confirmation_rounds", {})
            round_number = rounds.get(from_state, 1)

        # Read the previous agent's output for a brief summary.
        summary_note = ""
        if from_state_type == "agent_execution":
            summary_note = self._agent_output_brief(project, from_state)

        from mac_system.feishu.card import build_transition_card

        card = build_transition_card(
            project_name=name,
            from_state=from_state,
            to_state=to_state,
            from_state_type=from_state_type,
            to_state_type=to_state_type,
            from_agent=from_agent,
            to_agent=to_agent,
            status_note=summary_note,
            round_number=round_number,
        )
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            await self.feishu_bridge._send_interactive(
                receive_id, card, receive_id_type=receive_id_type,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send transition card for %s: %s", project.id, exc
            )

    def _agent_output_brief(self, project: Project, state: str) -> str:
        """Return a one-line summary of what the agent at *state* produced."""
        workspace = Path(project.workspace_path)
        mac = workspace / ".mac"
        file_map = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = file_map.get(state)
        if filename:
            path = mac / filename
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return ""
                summary = data.get("summary", "")
                if summary and len(summary) > 120:
                    summary = summary[:120] + "…"
                return summary
            return ""

        # For CODING/TESTING/REVIEW, read the most recent completed task output.
        agent_type_map = {
            "CODING": "coder",
            "TESTING": "tester",
            "REVIEW": "reviewer",
        }
        agent_type = agent_type_map.get(state)
        if not agent_type:
            return ""

        from sqlmodel import select
        task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == agent_type,
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
            .limit(1)
        ).first()
        if not task or not task.output_data:
            return ""

        out = task.output_data
        candidates = [
            out.get("summary", ""),
            out.get("result", ""),
            out.get("report", ""),
        ]
        if out.get("notes") and isinstance(out["notes"], list):
            candidates.append(" ".join(str(n) for n in out["notes"] if n))
        summary = next((s for s in candidates if s), "")
        if summary and len(summary) > 120:
            summary = summary[:120] + "…"
        return summary

    def _agent_label_for_state(self, state: str) -> str:
        """Map a state name to the Chinese label of the agent that handles it."""
        # Try each task type; human_confirmation / terminal states don't have agents.
        agent_type = None
        for tt in ("feature", "bug_fix", "documentation"):
            try:
                sm = StateMachine(tt)
                agent_type = sm.agent_for(state)
                if agent_type:
                    break
            except (KeyError, FileNotFoundError, ValueError):
                continue
        label_map = {
            "planner": "需求分析师",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理 Agent",
            "acceptance_evaluator": "验收评估 Agent",
        }
        return label_map.get(agent_type or "", agent_type or "")

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
