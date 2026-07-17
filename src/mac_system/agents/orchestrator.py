import json
import logging
from pathlib import Path
from collections.abc import Awaitable
from typing import Any, Callable


from sqlmodel import select

from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import get_settings
from mac_system.core.lock_manager import ProjectLockManager, get_default_lock_manager
from mac_system.core.project_service import ProjectService
from mac_system.core.project_paths import project_mac_dir
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.models import Approval, FeedbackPacket, Project, Task
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


def _split_markdown_sections(text: str | None, max_chars: int = 1600) -> list[str]:
    """Split Markdown text into readable chunks at section boundaries.

    Tries to keep sections together; only splits a section if it alone exceeds
    ``max_chars``.  Returns at least one chunk.
    """
    if not text:
        return ["（暂无详细汇报内容）"]
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


# Quality-gate confirmation states.  In manual/strict modes these wait for
# human approval; in night/auto modes they are confirmed immediately so the
# flow is not blocked, while still emitting a report card.
_AUTO_CONFIRM_STATES: frozenset[str] = frozenset({
    "CODING_CONFIRM_PENDING",
    "TESTING_CONFIRM_PENDING",
    "REVIEW_CONFIRM_PENDING",
})

_PLANNER_ARTIFACTS: dict[str, tuple[str, str]] = {
    "PLANNING_REQUIREMENTS": ("REQUIREMENTS.json", "requirements"),
    "PLANNING_ARCHITECTURE": ("ARCHITECTURE.json", "architecture"),
    "PLANNING_IMPLEMENTATION_SPECS": ("SPECS.json", "specs"),
}


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

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        """Cancel a project, fail running tasks, and notify the chat/user."""
        project = self.project_service.require_project(project_id)
        if project.status == "cancelled":
            return

        # Best-effort kill any running agent subprocess.
        for task in self.project_service._running_tasks(project_id):
            try:
                self.runtime.cancel(task.id)
            except Exception:
                pass

        await self.project_service.cancel_project(project_id, reason=reason, triggered_by="user")

        if self.feishu_bridge is None:
            return
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            await self.feishu_bridge.send_text(
                receive_id,
                f"项目 {project.name or project.id} 已终止。{reason}".strip(),
                receive_id_type=receive_id_type,
            )
        except Exception as exc:
            logger.warning("Failed to send cancellation notification for %s: %s", project_id, exc)

    async def _tick_unsafe(self, project_id: str) -> None:
        project = self.project_service.require_project(project_id)

        if project.status in ("paused", "cancelled"):
            return

        state_machine = StateMachine(project.task_type)
        state = project.current_state

        # Apply any agent result that was produced while the project was paused.
        paused_result = project.context.get("paused_agent_result")
        if paused_result:
            if paused_result.get("state") == state and not state_machine.is_terminal(state):
                task = self.project_service.session.get(Task, paused_result["task_id"])
                if task is not None:
                    await self._handle_agent_output(
                        project,
                        state_machine,
                        task,
                        Path(paused_result["worktree_path"]),
                        paused_result["output"],
                    )
                    project = self.project_service.require_project(project.id)
                ctx = dict(project.context)
                ctx.pop("paused_agent_result", None)
                project.context = ctx
                self.project_service.session.commit()
                return
            # Stale paused result; clear it.
            ctx = dict(project.context)
            ctx.pop("paused_agent_result", None)
            project.context = ctx
            self.project_service.session.commit()

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
                project.id, "runtime_exhausted", triggered_by="orchestrator"
            )
            return

        # Idempotency guard: do NOT start a new task if there is already a
        # running agent for this project at this same state.  This prevents
        # duplicate claude processes when tick() is called concurrently.
        from sqlmodel import select
        from mac_system.models import Task as TaskModel
        running_task = self.project_service.session.exec(
            select(TaskModel)
            .where(
                TaskModel.project_id == project.id,
                TaskModel.state == state,
                TaskModel.status == "running",
            )
            .limit(1)
        ).first()
        if running_task is not None:
            logger.info(
                "Skipping _execute_agent for %s at %s — task %s is already running",
                project.id, state, running_task.id,
            )
            return

        if await self._recover_unapplied_completed_task(project, state_machine):
            return

        # Build round-aware task input.  Each re-entry into the same agent stage
        # increments the stage round number so prompts and artifacts can refer to
        # the correct iteration.
        stage_rounds = dict(project.context.get("stage_rounds", {}))
        round_number = stage_rounds.get(state, 0) + 1
        stage_rounds[state] = round_number
        project.context = {**project.context, "stage_rounds": stage_rounds}

        # Include open feedback packets and any legacy rejection note.
        new_context = dict(project.context)
        rejection_note = new_context.pop("latest_rejection_feedback", "")
        project.context = new_context
        open_feedback = [
            {
                "feedback_id": fb.id,
                "source_stage": fb.source_stage,
                "source_task_id": fb.source_task_id,
                "source_round": fb.source_round,
                "kind": fb.kind,
                "failure_class": fb.failure_class,
                "summary": fb.summary,
                "blocking": fb.blocking,
                "recommended_rework_target": fb.recommended_rework_target,
                "evidence": fb.evidence,
                "user_comment": fb.user_comment,
            }
            for fb in self.project_service.get_open_feedback_packets(project.id)
        ]
        # Collect answered QuestionInteraction history so agents don't re-ask.
        qa_history = self._collect_qa_history(project.id)

        # Create task
        task = await self.project_service.create_task(
            project_id=project.id,
            agent_type=agent_type,
            state=state,
            round_number=round_number,
            input_data={
                "description": project.description,
                "state": state,
                "round_number": round_number,
                "rejection_note": rejection_note,
                "open_feedback": open_feedback,
            },
        )

        # Create worktree
        try:
            worktree = self.worktree_manager.create_worktree(
                project_id=project.id,
                task_id=task.id,
                agent_type=agent_type,
                stage=stage,
                workspace_path=project.workspace_path,
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
                workspace_path=project.workspace_path,
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
                rejection_note=rejection_note,
                open_feedback=open_feedback,
                round_number=round_number,
                qa_history=qa_history,
            )
            allowed_tools = self.prompt_renderer.get_allowed_tools(agent_type)
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Prompt rendering failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Prompt rendering failed: {e}")
            return

        # Execute agent
        logger.info(
            "Agent %s(%s) started for %s (task=%s)", agent_type, stage, project.id, task.id,
        )
        import time as _time
        _agent_start = _time.monotonic()
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
                task_id=task.id,
                progress_callback=self._make_progress_callback(project, task),
            )
        except Exception as e:
            _elapsed = _time.monotonic() - _agent_start
            logger.error(
                "Agent %s failed for %s after %.1fs: %s",
                agent_type, project.id, _elapsed, e,
            )
            await self.project_service.fail_task(task.id, f"Runtime execution failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Runtime execution failed: {e}")
            return
        _elapsed = _time.monotonic() - _agent_start
        logger.info(
            "Agent %s completed for %s in %.1fs (success=%s)",
            agent_type, project.id, _elapsed, result.success,
        )

        # Fallback: some runtimes write the agent output into TASK.json instead of
        # returning it directly in the result event.
        if (not result.output_json) and result.success:
            task_json_path = worktree / ".mac" / "TASK.json"
            if task_json_path.exists():
                try:
                    task_doc = json.loads(task_json_path.read_text(encoding="utf-8"))
                    output = task_doc.get("output")
                    if isinstance(output, dict) and output.get("next_state_event"):
                        result.output_json = output
                except Exception as exc:
                    logger.warning("Failed to read TASK.json output: %s", exc)

        # If the project was cancelled while the agent was running, discard the result.
        project = self.project_service.require_project(project.id)
        self.project_service.session.refresh(project)
        if project.status == "cancelled":
            await self.project_service.fail_task(task.id, "Project cancelled while agent was running")
            return

        # If the project was paused, complete the task and stash the result so
        # we can apply it on resume instead of re-running the agent.
        if project.status == "paused":
            await self.project_service.complete_task(
                task.id, result.output_json, result.cost_info
            )
            project = self.project_service.require_project(project.id)
            project.context = {
                **project.context,
                "paused_agent_result": {
                    "task_id": task.id,
                    "state": state,
                    "output": result.output_json,
                    "worktree_path": str(worktree),
                },
            }
            self.project_service.session.commit()
            return

        # If the project state changed (e.g. user rollback/cancel), discard stale result.
        if project.current_state != state:
            await self.project_service.fail_task(
                task.id,
                f"Project state changed from {state} to {project.current_state} while agent was running",
            )
            return

        if not result.success or not result.output_json:
            error = result.logs or "Agent returned failure"
            await self.project_service.fail_task(task.id, error)
            await self._handle_agent_failure(project, state, task, error)
            return

        self._persist_stage_output(project, worktree, result.output_json, state)

        # Complete task
        await self.project_service.complete_task(
            task.id, result.output_json, result.cost_info
        )

        # Consume any pending user inputs received while the agent was running.
        await self._consume_pending_user_inputs(project, task, state, result.output_json)

        # Handle agent output
        await self._handle_agent_output(project, state_machine, task, worktree, result.output_json)

        # In night/auto modes, quality-gate confirmations do not block the
        # next tick.  Resolve them immediately while still emitting report cards.
        await self._auto_resolve_quality_gates(project, state_machine)

    async def _recover_unapplied_completed_task(
        self, project: Project, state_machine: StateMachine
    ) -> bool:
        """Apply a task result committed just before an orchestrator crash.

        A result is recoverable only while the project is still in the task's
        original state and the task is newer than the project. Normal completed
        transitions update the project timestamp, so they are never replayed.
        """
        task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.state == project.current_state,
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        if task is None or not task.output_data:
            return False
        if task.updated_at <= project.updated_at:
            return False
        output = dict(task.output_data)
        worktree = Path(task.worktree_path) if task.worktree_path else Path(project.workspace_path)
        if not worktree.exists() and not output.get("question_batch"):
            logger.warning(
                "Cannot recover completed task %s for %s: worktree is missing",
                task.id,
                project.id,
            )
            return False
        logger.warning(
            "Recovering completed but unapplied task %s for %s at %s",
            task.id,
            project.id,
            project.current_state,
        )
        await self.project_service.log_event(
            project_id=project.id,
            level="WARNING",
            message=f"Recovering unapplied completed task: {task.id}",
            meta={"task_id": task.id, "state": project.current_state},
        )
        await self._handle_agent_output(project, state_machine, task, worktree, output)
        refreshed = self.project_service.require_project(project.id)
        await self._auto_resolve_quality_gates(refreshed, state_machine)
        return True

    async def _auto_resolve_quality_gates(
        self,
        project: Project,
        state_machine: StateMachine,
    ) -> None:
        """Auto-resolve quality-gate confirmations when the approval mode allows.

        These states still produce an approval record and report card, but if
        the current mode says they do not require human confirmation we confirm
        them right away and continue to the next agent stage in the same tick.
        """
        for _ in range(5):
            refreshed = self.project_service.get_project(project.id)
            if refreshed is None:
                return
            state = refreshed.current_state
            if state not in _AUTO_CONFIRM_STATES:
                break
            state_type = state_machine.state_type(state)
            if self._state_requires_confirmation(
                state, state_type, refreshed.approval_config
            ):
                break
            await self._handle_human_confirmation(refreshed, state_machine)
            new_state = self.project_service.get_project(project.id).current_state
            if new_state == state:
                break

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
                    workspace_path=project.workspace_path,
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
                    workspace_path=project.workspace_path,
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"MAC sync failed: {e}")
                return None

        try:
            return await self._run_auxiliary_agent(project, task, worktree, input_data, context_files)
        except Exception as e:
            # Safety net: any unexpected error in _run_auxiliary_agent must
            # still fail the task so it is not left in "running" forever.
            logger.error("Auxiliary agent %s crashed for %s: %s", agent_type, project.id, e)
            await self.project_service.fail_task(task.id, f"Auxiliary agent crashed: {e}")
            return None

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
                qa_history=self._collect_qa_history(project.id),
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

        if task.agent_type == "acceptance_evaluator":
            evidence_path = project_mac_dir(project) / "ACCEPTANCE_EVALUATOR_OUTPUT.json"
            evidence_path.write_text(
                json.dumps(result.output_json, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        await self.project_service.complete_task(task.id, result.output_json, result.cost_info)
        return result.output_json

    async def run_chat_scoped_agent(
        self,
        agent_type: str,
        stage: str | None,
        input_data: dict[str, Any],
        extra_variables: dict[str, str] | None = None,
        *,
        worktree: Path | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Run an auxiliary agent for a chat-scoped (non-project) operation.

        Used by operations like `/bind` (no-args) which need an LLM agent but
        don't have a Project context. Uses a temp directory as the worktree
        since there's no project workspace. Does NOT create a Task row in the
        database - this is fire-and-forget LLM invocation with no audit trail.

        Returns the agent output JSON, or None if execution failed.
        """
        import tempfile

        owns_worktree = worktree is None
        if worktree is None:
            worktree = Path(tempfile.mkdtemp(prefix=f"mac_chat_scoped_{agent_type}_"))
        try:
            try:
                system_prompt = self.prompt_renderer.render_system_prompt(agent_type, stage)
                output_schema = self.prompt_renderer.get_output_schema_path(agent_type, stage)
                task_prompt = self.prompt_renderer.render_task_prompt(
                    agent_type=agent_type,
                    stage=stage,
                    project=None,
                    state="chat_scoped",
                    worktree=worktree,
                    context_files=[],
                    output_schema_path=str(output_schema),
                    extra_variables=extra_variables,
                )
                allowed_tools = self.prompt_renderer.get_allowed_tools(agent_type)
                if agent_type == "conversation_coordinator" and owns_worktree:
                    # Ordinary chat turns do not need filesystem tools. Keeping
                    # the tool surface empty reduces CLI startup/planning work;
                    # workspace inspections still receive Read explicitly.
                    allowed_tools = []
            except Exception as e:
                logger.error(
                    "Chat-scoped agent %s/%s prompt rendering failed: %s",
                    agent_type, stage, e,
                )
                return None

            try:
                result = await self.runtime.execute(
                    agent_type=agent_type,
                    task_prompt=task_prompt,
                    system_prompt=system_prompt,
                    workspace=worktree,
                    worktree=worktree,
                    context_files=[],
                    output_schema=output_schema,
                    allowed_tools=allowed_tools,
                    readonly_dirs=[worktree],
                    timeout_seconds=self.settings.agent_timeout_seconds,
                    progress_callback=(
                        progress_callback if self.settings.debug_agent_progress else None
                    ),
                )
            except Exception as e:
                logger.error(
                    "Chat-scoped agent %s/%s runtime failed: %s",
                    agent_type, stage, e,
                )
                return None

            if not result.success or not result.output_json:
                logger.warning(
                    "Chat-scoped agent %s/%s returned failure: %s",
                    agent_type, stage, result.logs or "no output",
                )
                return None
            return result.output_json
        finally:
            # Best-effort cleanup of the temp dir. Don't raise on failure.
            try:
                if owns_worktree and worktree.exists():
                    import shutil
                    shutil.rmtree(worktree, ignore_errors=True)
            except Exception:
                pass

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
                "next_state_event": output.get("next_state_event"),
                "question_batch": output.get("question_batch"),
                "action": recommendation.get("action"),
                "reason": recommendation.get("reason"),
                "result": output.get("result"),
                "summary": output.get("summary"),
                "acceptance_matrix": output.get("acceptance_matrix", []),
                "blocking_conditions": output.get("blocking_conditions", []),
            }
        return {
            "next_state_event": output.get("next_state_event"),
            "question_batch": output.get("question_batch"),
            "result": output.get("result"),
            "summary": output.get("summary"),
            "acceptance_matrix": output.get("acceptance_matrix", []),
            "blocking_conditions": output.get("blocking_conditions", []),
        }

    def _check_acceptance_coverage(
        self, project: Project, evaluation: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return missing requirement AC IDs from the evaluator matrix."""
        if evaluation is None:
            return None
        if (
            evaluation.get("result") == "fail"
            or evaluation.get("blocking_conditions")
            or str(evaluation.get("action", "")).startswith("reject_")
        ):
            return {
                "missing_ids": [],
                "message": evaluation.get("reason")
                or evaluation.get("summary")
                or "验收评估未通过",
            }

        requirements_path = project_mac_dir(project) / "REQUIREMENTS.json"
        if not requirements_path.exists():
            return None
        try:
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Unable to read requirements for %s", project.id, exc_info=True)
            return None

        required_ids = {
            item.get("id")
            for item in requirements.get("acceptance_criteria", [])
            if isinstance(item, dict) and item.get("id")
        }
        if not required_ids:
            return None
        covered_ids = {
            item.get("acceptance_criterion_id")
            for item in evaluation.get("acceptance_matrix", [])
            if isinstance(item, dict)
            and item.get("acceptance_criterion_id")
            and item.get("result") == "pass"
        }
        missing_ids = sorted(required_ids - covered_ids)
        if not missing_ids:
            return None
        return {
            "missing_ids": missing_ids,
            "message": f"验收矩阵未覆盖以下验收标准: {', '.join(missing_ids)}",
        }

    async def _record_acceptance_coverage_failure(
        self, project: Project, violation: dict[str, Any]
    ) -> None:
        existing = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.kind == "acceptance_rejection",
                FeedbackPacket.status.in_(["open", "disputed"]),
                FeedbackPacket.summary == violation["message"],
            )
        ).first()
        if existing is not None:
            return
        await self.project_service.create_feedback_packet(
            project_id=project.id,
            source_stage="ACCEPTANCE",
            source_task_id=None,
            source_round=1,
            kind="acceptance_rejection",
            summary=violation["message"],
            blocking=True,
            failure_class="requirement_mismatch",
            recommended_rework_target="specs",
            evidence=[
                {
                    "type": "missing_ac_coverage",
                    "missing_ids": violation["missing_ids"],
                }
            ],
        )

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
        feedback_packet_id: str | None = None

        # Quality gates: only an explicit pass is allowed to move forward.
        # A failing assessment is recorded as a structured FeedbackPacket and
        # routed to REWORK via the state machine's "fail" event.
        if state == "TESTING":
            if output.get("passed") is not True:
                event = "fail"
                packet = await self._create_feedback_from_tester_output(project, task, output)
                feedback_packet_id = packet.id if packet else None
            else:
                await self._resolve_verified_feedback(
                    output.get("verified_feedback_ids", []), task.id, project.id
                )
        elif state == "REVIEW":
            issues = output.get("issues", [])
            blocking_issue = any(issue.get("blocking") for issue in issues)
            if output.get("result") != "pass" or blocking_issue:
                event = "fail"
                packet = await self._create_feedback_from_reviewer_output(project, task, output)
                feedback_packet_id = packet.id if packet else None
            else:
                await self._resolve_verified_feedback(
                    output.get("verified_feedback_ids", []), task.id, project.id
                )

        # Unified ask_user: when an agent emits a question_batch, create a
        # QuestionInteraction and route to the state's ask_user event.
        question_batch = output.get("question_batch")
        if question_batch and isinstance(question_batch, dict) and event not in ("fail",):
            question_batch, suppressed_questions = self._filter_confirmed_questions(
                project, question_batch
            )
            if suppressed_questions:
                await self.project_service.log_event(
                    project_id=project.id,
                    level="INFO",
                    message="Suppressed questions already answered by confirmed brief",
                    meta={"questions": suppressed_questions, "task_id": task.id},
                )
                output["question_batch"] = question_batch
                task.output_data = dict(output)
                self.project_service.session.commit()

            batch_questions = question_batch.get("questions") or []
            if batch_questions:
                interaction = await self._create_question_interaction_from_output(
                    project, task, state, state_machine, question_batch
                )
                ctx = dict(project.context)
                ctx["_pending_question_interaction_id"] = interaction.id
                project.context = ctx
                self.project_service.session.commit()
                event = "ask_user"
            elif suppressed_questions:
                ctx = dict(project.context)
                ctx["question_guard_feedback"] = suppressed_questions
                project.context = ctx
                self.project_service.session.commit()
                error = "Agent only asked questions already resolved by confirmed decisions"
                await self.project_service.fail_task(task.id, error)
                await self._handle_agent_failure(project, state, task, error)
                return

        if not question_batch and project.context.get("question_guard_feedback"):
            ctx = dict(project.context)
            ctx.pop("question_guard_feedback", None)
            project.context = ctx
            self.project_service.session.commit()

        # Apply any free-text user inputs that arrived while the agent was
        # running.  Blocking inputs discard the agent result instead of merging
        # stale output.
        discard_event = await self._consume_pending_user_inputs(project, task, state, output)
        if discard_event:
            event = discard_event

        # For planner stages, copy the .mac/ output files written by the agent
        # from the worktree back to the main workspace.  Only do this when the
        # result is actually going to be kept (event == "complete").
        if event == "complete" and state in (
            "PLANNING_REQUIREMENTS",
            "PLANNING_ARCHITECTURE",
            "PLANNING_IMPLEMENTATION_SPECS",
            "BUG_ANALYSIS",
            "DOC_OUTLINE",
        ):
            artifact_error = self._planner_artifact_error(worktree, state)
            if artifact_error:
                await self.project_service.fail_task(task.id, artifact_error)
                await self._handle_agent_failure(project, state, task, artifact_error)
                return
            self._copy_mac_outputs(project, worktree, output, state)
            artifact_entry = _PLANNER_ARTIFACTS.get(state)
            if artifact_entry:
                filename, artifact_id = artifact_entry
                artifact_path = project_mac_dir(project) / filename
                try:
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    logger.warning(
                        "Unable to read artifact version from %s", artifact_path,
                        exc_info=True,
                    )
                else:
                    new_version = artifact.get("version")
                    if isinstance(new_version, int):
                        previous = self.project_service.get_artifact_version(
                            project.id, artifact_id
                        )
                        self.project_service.update_artifact_version(
                            project.id,
                            artifact_id,
                            new_version,
                            artifact.get("round"),
                            artifact.get("status", "pending_confirmation"),
                        )
                        if previous and previous.get("version") != new_version:
                            self.project_service.mark_downstream_artifacts_stale(
                                project.id, artifact_id
                            )

        # For coder/tester/reviewer, merge worktree changes only on success.
        if state in ("CODING", "TESTING", "REVIEW") and event == "complete":
            try:
                self.worktree_manager.merge_worktree(
                    project.id, task.id, task.agent_type, worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception as e:
                await self.project_service.fail_task(task.id, f"Merge failed: {e}")
                await self._handle_agent_failure(project, state, task, f"Merge failed: {e}")
                return
            try:
                self.worktree_manager.remove_worktree(
                    project.id, task.id, task.agent_type, worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove worktree %s after merge: %s", worktree, e
                )

        # Transition state
        payload = self._build_transition_payload(output)
        if feedback_packet_id:
            payload["feedback_packet_id"] = feedback_packet_id
        from_state = project.current_state
        await self.project_service.transition_state(
            project.id, event, payload=payload, triggered_by="agent"
        )

        # Send a brief status notification so the user can follow progress.
        await self._notify_state_transition(project, task, from_state)

        # If the agent asked for user input, immediately render the input card
        # so the user does not have to wait for an external tick.
        project = self.project_service.require_project(project.id)
        new_state = project.current_state
        if state_machine.state_type(new_state) == "human_input":
            await self._handle_human_input(project, state_machine, auto_resolve=False)

        # Clean up the now-unneeded worktree for non-merge states.
        if state not in ("CODING", "TESTING", "REVIEW"):
            try:
                self.worktree_manager.remove_worktree(
                    project.id, task.id, task.agent_type, worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove worktree %s after transition: %s", worktree, e
                )

    async def _create_feedback_from_tester_output(
        self,
        project: Project,
        task: Task,
        output: dict[str, Any],
    ) -> Any:
        """Create a FeedbackPacket from a failing tester output."""
        failures = output.get("failures", [])
        recommended = output.get("recommended_rework_target")
        if recommended in (None, "none"):
            recommended = None
        return await self.project_service.create_feedback_packet(
            project_id=project.id,
            source_stage="TESTING",
            source_task_id=task.id,
            source_round=task.round_number,
            kind="quality_failure",
            summary=output.get("report") or "测试未通过",
            blocking=True,
            failure_class=output.get("failure_class") or "implementation_bug",
            recommended_rework_target=recommended,
            evidence=[{"type": "test_failure", **f} for f in failures] if failures else [],
        )

    async def _create_feedback_from_reviewer_output(
        self,
        project: Project,
        task: Task,
        output: dict[str, Any],
    ) -> Any:
        """Create a FeedbackPacket from a failing reviewer output."""
        issues = output.get("issues", [])
        blocking_issue = next((i for i in issues if i.get("blocking")), None)
        failure_class = blocking_issue.get("category") if blocking_issue else "implementation"
        recommended = output.get("recommended_rework_target")
        if recommended in (None, "none"):
            recommended = None
        return await self.project_service.create_feedback_packet(
            project_id=project.id,
            source_stage="REVIEW",
            source_task_id=task.id,
            source_round=task.round_number,
            kind="quality_failure",
            summary=output.get("summary") or "Review 未通过",
            blocking=True,
            failure_class=failure_class,
            recommended_rework_target=recommended,
            evidence=[{"type": "review_issue", **i} for i in issues] if issues else [],
        )

    async def _create_question_interaction_from_output(
        self, project, task, state, state_machine, question_batch,
    ):
        """Create a QuestionInteraction from an agent's question_batch output."""
        batch_key = question_batch.get("batch_key") or f"{state}_{task.id}"
        questions = question_batch.get("questions") or []
        agent = state_machine.agent_for(state) or task.agent_type
        stage = state_machine.stage_for(state)
        self.project_service.expire_open_question_interactions(project.id)
        return await self.project_service.create_question_interaction(
            project_id=project.id, origin_task_id=task.id, origin_round=task.round_number,
            resume_state=state, agent=agent, stage=stage, batch_key=batch_key,
            questions=questions,
        )

    def _filter_confirmed_questions(
        self, project: Project, question_batch: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Remove questions whose decision key is already confirmed.

        The guard is intentionally deterministic. Natural-language similarity
        remains the Planner's responsibility; the orchestrator only suppresses
        an exact stable decision key so it cannot silently discard a genuinely
        different question.
        """
        brief = project.context.get("confirmed_brief", {})
        decisions = brief.get("decisions", []) if isinstance(brief, dict) else []
        confirmed: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("status") != "confirmed":
                continue
            key = str(decision.get("key") or "").strip().lower()
            if key:
                confirmed[key] = decision

        retained: list[dict[str, Any]] = []
        suppressed: list[dict[str, str]] = []
        for question in question_batch.get("questions") or []:
            if not isinstance(question, dict):
                retained.append(question)
                continue
            key = str(question.get("decision_key") or "").strip().lower()
            decision = confirmed.get(key)
            if not decision:
                retained.append(question)
                continue
            suppressed.append({
                "decision_key": key,
                "question": str(question.get("question") or ""),
                "confirmed_answer": str(decision.get("answer") or ""),
            })

        filtered = dict(question_batch)
        filtered["questions"] = retained
        return filtered, suppressed

    async def _resolve_verified_feedback(
        self,
        verified_feedback_ids: list[str] | None,
        resolved_by_task_id: str,
        project_id: str,
    ) -> None:
        """Mark feedback packets resolved when a quality gate verifies the fix."""
        if not verified_feedback_ids:
            return
        for feedback_id in verified_feedback_ids:
            try:
                await self.project_service.resolve_feedback_packet(
                    feedback_id,
                    resolved_by_task_id=resolved_by_task_id,
                    project_id=project_id,
                )
            except Exception as exc:
                logger.warning("Failed to resolve feedback packet %s: %s", feedback_id, exc)

    def _discard_event_for_state(self, state: str) -> str:
        """Return the event that discards a stale agent result for ``state``."""
        if state in ("PLANNING_REQUIREMENTS", "PLANNING_ARCHITECTURE",
                     "PLANNING_IMPLEMENTATION_SPECS", "BUG_ANALYSIS", "DOC_OUTLINE"):
            return "ask_user"
        if state == "CODING":
            return "clarify"
        return "fail"

    async def _consume_pending_user_inputs(
        self,
        project: Project,
        task: Task,
        state: str,
        output: dict[str, Any],
    ) -> str | None:
        """Consume free-text user inputs received while an agent was running.

        scope_change / correction inputs are recorded as blocking feedback.
        When such an input arrives in an agent-execution stage, the agent's
        result is treated as stale: it is not merged/copied and the project is
        routed to the appropriate human_input state (or REWORK for quality
        gates) so the user's input can be incorporated before proceeding.
        """
        pending = self.project_service.get_pending_user_inputs(project.id)
        if not pending:
            return None

        blocking_inputs: list[Any] = []
        for inp in pending:
            await self.project_service.consume_pending_user_input(
                inp.id, task.id, commit=False
            )
            await self.project_service.create_feedback_packet(
                project_id=project.id,
                source_stage=state,
                source_task_id=task.id,
                source_round=task.round_number,
                kind=inp.kind,
                summary=inp.content,
                blocking=inp.kind in ("scope_change", "correction"),
                user_comment=inp.content,
                commit=False,
            )
            if inp.kind in ("scope_change", "correction"):
                blocking_inputs.append(inp)

        if not blocking_inputs:
            self.project_service.session.commit()
            return None

        user_note = "\n".join(f"- {inp.content}" for inp in blocking_inputs)
        stale_note = f"Agent 运行期间收到 blocking 输入，当前结果已丢弃：\n{user_note}"

        event = self._discard_event_for_state(state)
        output["next_state_event"] = event
        output["clarification_question"] = user_note
        output.setdefault("notes", []).append(stale_note)

        if state == "TESTING":
            output["passed"] = False
            output["report"] = (
                (output.get("report") or "测试未通过") + "\n\n用户中途输入（强制驳回）：\n" + user_note
            )
        elif state == "REVIEW":
            output["result"] = "fail"
            output["summary"] = (
                (output.get("summary") or "Review 未通过") + "\n\n用户中途输入（强制驳回）：\n" + user_note
            )

        self.project_service.session.commit()
        return event

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

        next_retry_count = retry_count + 1
        if next_retry_count < max_retries:
            # Reassign the context dict so SQLAlchemy detects the JSON mutation.
            new_counts = dict(project.context.get("retry_counts", {}))
            new_counts[retry_key] = next_retry_count
            project.context = {**project.context, "retry_counts": new_counts}
            # Persist the retry count immediately so the next tick sees it even
            # if the outer session_scope rolls back for unrelated reasons.
            self.project_service.session.commit()
            await self.project_service.log_event(
                project_id=project.id,
                level="INFO",
                message=f"Automatically retrying {task.agent_type} at {state}",
                meta={"next_attempt": next_retry_count + 1, "max_attempts": max_retries},
            )
            refreshed = self.project_service.require_project(project.id)
            await self._execute_agent(refreshed, StateMachine(project.task_type))
            return

        # This failure consumed the last allowed attempt. Transition now;
        # relying on a future tick leaves an active project with no task.
        new_counts = dict(project.context.get("retry_counts", {}))
        new_counts[retry_key] = next_retry_count
        project.context = {**project.context, "retry_counts": new_counts}
        self.project_service.session.commit()

        failure_context = dict(project.context)
        failure_context["last_agent_failure"] = {
            "state": state,
            "agent": task.agent_type,
            "task_id": task.id,
            "attempts": next_retry_count,
            "error": error[-1200:],
        }
        project.context = failure_context
        self.project_service.session.commit()
        await self.project_service.transition_state(
            project.id, "runtime_exhausted", triggered_by="orchestrator"
        )

    def _state_requires_confirmation(
        self,
        state: str,
        state_type: str,
        approval_config: dict,
    ) -> bool:
        """Return True if the state should be confirmed by a human.

        A state requires confirmation when either:
        - its state name is listed in ``require_confirmation_for``;
        - its state type is listed in ``require_confirmation_for_types``; or
        - it is the REWORK state, which must always be driven by a human decision.
        """
        # REWORK is a human decision on where to roll back; never auto-resolve it.
        if state == "REWORK" or state.endswith("_RECOVERY_PENDING"):
            return True
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

        acceptance_recommendation = None
        acceptance_coverage_violation = None
        if state == "ACCEPTANCE_PENDING":
            acceptance_recommendation = await self._evaluate_acceptance(project)
            question_batch = (
                acceptance_recommendation.get("question_batch")
                if acceptance_recommendation
                else None
            )
            if (
                acceptance_recommendation
                and acceptance_recommendation.get("next_state_event") == "ask_user"
                and isinstance(question_batch, dict)
                and question_batch.get("questions")
            ):
                acceptance_task = self.project_service.session.exec(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.agent_type == "acceptance_evaluator",
                        Task.status == "completed",
                    )
                    .order_by(Task.created_at.desc())
                ).first()
                if acceptance_task is not None:
                    self.project_service.expire_open_question_interactions(project.id)
                    interaction = await self.project_service.create_question_interaction(
                        project_id=project.id,
                        origin_task_id=acceptance_task.id,
                        origin_round=acceptance_task.round_number,
                        resume_state="ACCEPTANCE_PENDING",
                        agent="acceptance_evaluator",
                        stage="acceptance",
                        batch_key=question_batch.get("batch_key") or f"acceptance_{acceptance_task.id}",
                        questions=question_batch["questions"],
                    )
                    project.context = {
                        **project.context,
                        "_pending_question_interaction_id": interaction.id,
                    }
                    self.project_service.session.commit()
                    await self.project_service.transition_state(
                        project.id, "ask_user", triggered_by="acceptance_evaluator"
                    )
                    refreshed = self.project_service.require_project(project.id)
                    await self._handle_human_input(
                        refreshed, state_machine, auto_resolve=False
                    )
                    return
            acceptance_coverage_violation = self._check_acceptance_coverage(
                project, acceptance_recommendation
            )
            if acceptance_coverage_violation:
                await self._record_acceptance_coverage_failure(
                    project, acceptance_coverage_violation
                )
                acceptance_recommendation = {
                    **(acceptance_recommendation or {}),
                    "action": "reject_to_specs",
                    "reason": acceptance_coverage_violation["message"],
                    "summary": acceptance_coverage_violation["message"],
                }
                # Coverage is a hard gate: do not offer a misleading pass button.
                options = [opt for opt in options if opt.get("action") != "confirm"]

        task = (
            self.project_service.session.exec(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
            .first()
        )

        # Human-state ticks are idempotent. Reuse an existing pending approval
        # instead of expiring the card that another tick just created.
        approval = self.project_service.session.exec(
            select(Approval)
            .where(
                Approval.project_id == project.id,
                Approval.state == state,
                Approval.status == "pending",
            )
            .order_by(Approval.created_at.desc())
        ).first()
        if approval is None:
            rounds = dict(project.context.get("confirmation_rounds", {}))
            round_number = rounds.get(state, 0) + 1
            rounds[state] = round_number
            project.context = {**project.context, "confirmation_rounds": rounds}
            approval = await self.project_service.create_approval(
                project_id=project.id,
                task_id=task.id if task else None,
                state=state,
                approval_type="confirmation",
                question=question,
                options=options,
                payload={"round_number": round_number},
            )
        elif acceptance_coverage_violation:
            approval.payload = {**approval.payload, "options": options}
            approval.question = question
            self.project_service.session.commit()

        # Auto-resolve if the current mode does not require confirmation.
        if not requires_confirmation:
            chosen: str | None = None
            if acceptance_coverage_violation:
                chosen = "reject_to_specs"
            elif state in _AUTO_CONFIRM_STATES:
                # Quality gates are shown to the user (report card) but
                # default to confirm so the flow is not blocked in auto/night.
                chosen = "confirm"
            else:
                strategy = approval_config.get("auto_resolve_strategy", "none")
                if strategy == "project_manager_default":
                    chosen = await self._choose_action_with_project_manager(
                        project, state, question, options
                    )
                if chosen is None:
                    chosen = options[0]["action"]
                    if strategy == "project_manager_default":
                        logger.warning(
                            "project_manager agent failed to choose an action for %s, falling back to %s",
                            project.id, chosen,
                        )

            # For quality gates, still emit the report card before auto-resolving.
            if state in _AUTO_CONFIRM_STATES and self.feishu_bridge:
                await self._send_confirmation_cards(
                    project, approval, question, options, show_actions=False
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

        # Otherwise, send notification and wait
        if self.feishu_bridge:
            await self._send_confirmation_cards(
                project, approval, question, options, acceptance_recommendation
            )

    async def _send_confirmation_cards(
        self,
        project: Project,
        approval: Any,
        question: str,
        options: list[dict],
        acceptance_recommendation: dict[str, Any] | None = None,
        show_actions: bool = True,
    ) -> None:
        """Send a report card followed by the action confirmation card."""
        if self.feishu_bridge is None:
            return

        sm = StateMachine(project.task_type)
        state_display = sm.display_name(project.current_state)
        project_name = project.name or project.id
        title = f"[{project_name}] {question}"

        state_details = self._build_details_for_state(project, project.current_state)
        details: dict[str, Any] = {"当前阶段": state_display}
        if state_details:
            details.update(state_details)
        if acceptance_recommendation:
            rec_summary = acceptance_recommendation.get("reason") or acceptance_recommendation.get("summary", "")
            if rec_summary:
                details["验收评估"] = rec_summary[:200]
            if acceptance_recommendation.get("action") == "confirm":
                details["建议操作"] = "验收通过"
        summary_text = self._build_summary_for_state(project, project.current_state)
        if not summary_text:
            logger.warning(
                "Summary for %s at %s is empty; using fallback", project.id, project.current_state
            )
            summary_text = self._fallback_summary(project, project.current_state)
        receive_id, receive_id_type = self._resolve_project_recipient(project)

        # Always send a separate report card first, then the action card.
        # This separates "本轮汇报" from "状态流转/确认".
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

        # The confirmation card carries only metadata and action buttons.
        try:
            await self.feishu_bridge.send_confirmation_card(
                open_id=receive_id,
                approval_id=approval.id,
                title=title,
                summary="请查看上方报告后选择操作",
                details=details,
                options=options,
                receive_id_type=receive_id_type,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                show_actions=show_actions,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send confirmation card for %s: %s", project.id, exc
            )

    async def _handle_human_input(
        self,
        project: Project,
        state_machine: StateMachine,
        auto_resolve: bool = True,
    ) -> None:
        state = project.current_state
        approval_config = project.approval_config
        state_type = state_machine.state_type(state)
        requires_confirmation = self._state_requires_confirmation(
            state, state_type, approval_config
        )

        question = self._build_input_question(project, state)

        # REWORK is option-based: the user chooses which upstream stage to revisit.
        if state == "REWORK":
            question, options = self._build_confirmation_question(project, state)
        else:
            options = []

        # Idempotency: reuse an existing pending input approval for this state.
        approval = self.project_service.session.exec(
            select(Approval).where(
                Approval.project_id == project.id,
                Approval.state == state,
                Approval.status == "pending",
                Approval.approval_type == "input",
            ).order_by(Approval.created_at.desc())
        ).first()
        if approval is None:
            approval = await self.project_service.create_approval(
                project_id=project.id,
                task_id=None,
                state=state,
                approval_type="input",
                question=question,
                options=options,
            )

        if auto_resolve and not requires_confirmation:
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
            project_tag = project.name or project.id
            context_text = self._build_summary_for_state(project, state)
            if not context_text:
                context_text = self._fallback_summary(project, state)

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
                            title=f"[{project_tag}] {question}",
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

            if state == "REWORK":
                # Present the rollback options as actionable buttons.
                details = {"当前阶段": state_machine.display_name(state)}
                state_details = self._build_details_for_state(project, state)
                if state_details:
                    details.update(state_details)
                await self.feishu_bridge.send_confirmation_card(
                    open_id=receive_id,
                    approval_id=approval.id,
                    title=f"[{project_tag}] {question}",
                    summary="请选择要回退到哪一层重新处理",
                    details=details,
                    options=options,
                    receive_id_type=receive_id_type,
                    chat_id=project.feishu_chat_id,
                    chat_type=project.feishu_chat_type,
                )
            else:
                # Unified ask_user: if there is a pending QuestionInteraction,
                # render a batch question card instead of a free-text input card.
                interaction_id = project.context.get("_pending_question_interaction_id")
                sent_batch_card = False
                if interaction_id:
                    interaction = self.project_service.get_question_interaction(interaction_id)
                    if interaction is not None and interaction.status == "pending":
                        if interaction.approval_id is None:
                            interaction.approval_id = approval.id
                            self.project_service.session.commit()
                        existing_payload = dict(approval.payload)
                        existing_payload["question_interaction_id"] = interaction.id
                        approval.payload = existing_payload
                        self.project_service.session.commit()
                        data = self.project_service.serialize_question_interaction(interaction.id)
                        if data and data["questions"]:
                            try:
                                await self._send_question_batch_card(
                                    project, approval, data, receive_id, receive_id_type
                                )
                                sent_batch_card = True
                            except Exception as exc:
                                logger.warning(
                                    "Failed to send question batch card for %s: %s",
                                    project.id, exc,
                                )
                if not sent_batch_card:
                    await self.feishu_bridge.send_input_card(
                        open_id=receive_id,
                        approval_id=approval.id,
                        title=f"[{project_tag}] {question}",
                        question=question,
                        context=card_context,
                        receive_id_type=receive_id_type,
                        chat_id=project.feishu_chat_id,
                        chat_type=project.feishu_chat_type,
                    )

    async def _send_question_batch_card(
        self, project, approval, interaction_data, receive_id, receive_id_type,
    ):
        """Send Feishu cards for a batch of agent questions.

        Choice questions are grouped into a single interactive card.
        Free-text questions are sent as separate input cards so the user
        can reply to each one directly.
        """
        from mac_system.feishu.card import build_question_batch_card, build_input_card
        questions = interaction_data.get("questions", [])
        pending_questions = [q for q in questions if q.get("status") == "pending"]
        if not pending_questions:
            if interaction_data.get("is_resolvable"):
                await self.project_service.respond_approval(approval.id, "answer", "system_auto")
                await self.project_service.transition_state(
                    project.id, "answer",
                    payload={"resume_state": interaction_data.get("resume_state")},
                    triggered_by="system_auto",
                )
            return

        choice_questions = [q for q in pending_questions if q.get("options")]
        free_text_questions = [q for q in pending_questions if not q.get("options")]

        payload = dict(approval.payload)
        reply_message_ids = list(payload.get("reply_message_ids", []))
        reply_message_to_question = dict(payload.get("reply_message_to_question", {}))

        if choice_questions:
            selected_options = dict(payload.get("selected_options", {}))
            card = build_question_batch_card(
                approval_id=approval.id,
                interaction_id=interaction_data["interaction_id"],
                questions=choice_questions,
                agent=interaction_data.get("agent", ""),
                stage=interaction_data.get("stage"),
                impact=None,
                state=project.current_state,
                project_name=project.name or project.id,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                selected_options=selected_options,
            )
            message_id = await self.feishu_bridge._send_interactive(
                receive_id, card, receive_id_type=receive_id_type,
            )
            if message_id:
                reply_message_ids.append(message_id)
                payload["question_batch_message_id"] = message_id
                payload["selected_options"] = selected_options

        # Free-text questions are sent only after all choice questions are
        # answered. If there are choice questions, the first free-text card is
        # sent from the submit handler. If there are no choice questions, send
        # the first free-text card now.
        if free_text_questions and not choice_questions:
            q = free_text_questions[0]
            title = f"[{project.name or project.id}] [{interaction_data.get('stage') or interaction_data.get('agent', '')}] {q.get('question', '')}"
            card = build_input_card(
                title=title,
                question=q.get("question", ""),
                context=q.get("reason", ""),
                approval_id=approval.id,
                state=project.current_state,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                kind=q.get("kind", "blocking"),
                question_id=q.get("question_id"),
                interaction_id=interaction_data["interaction_id"],
            )
            message_id = await self.feishu_bridge._send_interactive(
                receive_id, card, receive_id_type=receive_id_type,
            )
            if message_id:
                reply_message_ids.append(message_id)
                reply_message_to_question[message_id] = q.get("question_id")

        if reply_message_ids:
            approval.payload = {
                **payload,
                "reply_message_ids": reply_message_ids[-20:],
                "reply_message_to_question": reply_message_to_question,
            }
            self.project_service.session.commit()

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
        state_path = project_mac_dir(project) / "STATE.json"
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

    def _collect_qa_history(self, project_id: str) -> list[dict[str, Any]]:
        """Return answered questions across all interactions for this project."""
        from mac_system.models import QuestionInteraction as QI
        interactions = self.project_service.session.exec(
            select(QI).where(
                QI.project_id == project_id,
                QI.status == "answered",
            ).order_by(QI.created_at)
        ).all()
        history: list[dict[str, Any]] = []
        for it in interactions:
            questions = self.project_service.get_questions_for_interaction(it.id)
            for q in questions:
                qa = self.project_service.get_question_answer(q.id)
                if qa is None:
                    continue
                history.append({
                    "question_id": q.question_id,
                    "question": q.question,
                    "answer": qa.answer,
                    "answer_label": qa.answer_label,
                    "skipped": qa.skipped,
                    "state": it.resume_state,
                    "round": it.origin_round,
                })
        return history

    def _context_files_for_state(self, project: Project, state: str) -> list[Path]:
        mac = project_mac_dir(project)
        files = [mac / "STATE.json"]

        if state in ("PLANNING_REQUIREMENTS",):
            files.append(mac / "TASK.json")
        elif state in ("PLANNING_ARCHITECTURE",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json"])
        elif state in ("PLANNING_IMPLEMENTATION_SPECS",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json", mac / "ARCHITECTURE.json"])
        elif state in ("CODING", "TESTING", "REVIEW"):
            files.extend([mac / "STATE.json", mac / "SPECS.json"])
            if (mac / "REQUIREMENTS.json").exists():
                files.append(mac / "REQUIREMENTS.json")
            if (mac / "ARCHITECTURE.json").exists():
                files.append(mac / "ARCHITECTURE.json")
            if state in ("TESTING", "REVIEW") and (mac / "CODER_OUTPUT.json").exists():
                files.append(mac / "CODER_OUTPUT.json")
            if state == "REVIEW" and (mac / "TESTER_OUTPUT.json").exists():
                files.append(mac / "TESTER_OUTPUT.json")

        elif state == "ACCEPTANCE_PENDING":
            files.extend(
                mac / filename
                for filename in (
                    "REQUIREMENTS.json",
                    "ARCHITECTURE.json",
                    "SPECS.json",
                    "CODER_OUTPUT.json",
                    "TESTER_OUTPUT.json",
                    "REVIEWER_OUTPUT.json",
                )
            )

        return [f for f in files if f.exists()]

    def _persist_stage_output(
        self,
        project: Project,
        worktree: Path,
        output: dict[str, Any],
        state: str,
    ) -> None:
        """Persist compact execution evidence for downstream quality agents."""
        mapping = {
            "CODING": "CODER_OUTPUT.json",
            "TESTING": "TESTER_OUTPUT.json",
            "REVIEW": "REVIEWER_OUTPUT.json",
        }
        filename = mapping.get(state)
        if filename is None:
            return
        destination = project_mac_dir(project) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = worktree / ".mac" / filename
        if source.is_file():
            import shutil

            shutil.copy2(source, destination)
            return
        destination.write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
            "PLANNING_IMPLEMENTATION_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = mapping.get(state)
        if filename is None:
            return

        dst_mac = project_mac_dir(project)
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

    @staticmethod
    def _planner_artifact_error(worktree: Path, state: str) -> str | None:
        """Reject a malformed planner file before it reaches the main workspace.

        Fake runtimes used by unit tests may not write a file; their structured
        output continues through the existing fallback path. A real agent file,
        once present, must at least be valid JSON so recovery/retry can repair it.
        """
        mapping = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_IMPLEMENTATION_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = mapping.get(state)
        if filename is None:
            return None
        artifact_path = worktree / ".mac" / filename
        if not artifact_path.is_file():
            return None
        try:
            parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Planner artifact {filename} is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return f"Planner artifact {filename} must contain a JSON object"
        return None

    def _write_planner_output(self, project: Project, state: str, output: dict[str, Any]) -> None:
        mapping = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_IMPLEMENTATION_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = mapping.get(state)
        if not filename:
            return
        with open(project_mac_dir(project) / filename, "w", encoding="utf-8") as f:
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
        state_machine = StateMachine(project.task_type)

        # Planning gates are generated from the profile instead of duplicated
        # here for every task type.
        for stage in state_machine.profile["stages"]:
            if stage["kind"] != "planning":
                continue
            confirmation = stage["confirmation"]
            if confirmation["state"] != state:
                continue
            label_map = {
                "confirm": "确认",
                "reject": "驳回",
                "select_option": "选择其他选项",
            }
            options = [
                {
                    "action": event,
                    "label": label_map.get(event, event),
                    "next_state": state_machine.next_state(state, event),
                }
                for event in state_machine.available_events(state)
            ]
            subject = confirmation.get("subject") or confirmation["display"].removeprefix("等待确认")
            return f"请确认{subject}", options

        # Quality-gate confirmations: report card + confirmation card.
        # They default to auto-confirm after a short delay.
        quality_gate_options = [
            {"action": "confirm", "label": "通过", "next_state": None},
            {"action": "reject", "label": "不通过，打回重改", "next_state": "REWORK"},
        ]
        if state == "CODING_CONFIRM_PENDING":
            return "请确认编码结果", quality_gate_options
        if state == "TESTING_CONFIRM_PENDING":
            return "请确认测试结果", quality_gate_options
        if state == "REVIEW_CONFIRM_PENDING":
            return "请确认 Review 结果", quality_gate_options

        if state == "REWORK":
            return "本轮未通过，请选择如何处理", state_machine.rework_options()

        if state.endswith("_RECOVERY_PENDING"):
            failure = project.context.get("last_agent_failure", {})
            agent = failure.get("agent", "Agent")
            attempts = failure.get("attempts", "多次")
            label_map = {
                "retry": "再次重试",
                "choose_rework": "选择回退阶段",
                "cancel": "终止项目",
            }
            options = [
                {
                    "action": event,
                    "label": label_map.get(event, event),
                    "next_state": state_machine.next_state(state, event),
                }
                for event in state_machine.available_events(state)
            ]
            return f"{agent} 已连续失败 {attempts} 次，请选择恢复方式", options

        if state == "ACCEPTANCE_PENDING":
            restart_option = {
                "action": "restart_with_type",
                "label": "重新归类创建",
                "next_state": "CANCELLED",
            }
            return (
                "请验收最终产物",
                [
                    {"action": "confirm", "label": "验收通过", "next_state": "MERGING"},
                    {"action": "reject", "label": "驳回并选择重做层级", "next_state": "REWORK"},
                    *state_machine.acceptance_rework_options(),
                    restart_option,
                ],
            )

        return (f"请确认 {state}", [{"action": "confirm", "label": "确认", "next_state": "COMPLETED"}])

    def _build_input_question(self, project: Project, state: str) -> str:
        pending_input = project.context.get("clarification_answer")
        if pending_input and state in {
            "CLARIFICATION_PENDING",
            "REQUIREMENT_INPUT_PENDING",
            "ARCHITECTURE_INPUT_PENDING",
            "IMPLEMENTATION_SPEC_INPUT_PENDING",
            "BUG_FIX_INPUT_PENDING",
            "OUTLINE_INPUT_PENDING",
            "TESTING_INPUT_PENDING",
            "REVIEW_INPUT_PENDING",
        }:
            return (
                f"Agent 运行期间收到你的输入：\n{pending_input}\n\n"
                "请确认或补充后回复"
            )
        if state == "CLARIFICATION_PENDING":
            return "Agent 请求澄清，请回复你的答案"
        elif state == "CONFLICT_PENDING":
            return "发现设计冲突，请裁决"
        elif state == "REQUIREMENT_INPUT_PENDING":
            return "Agent 对需求有疑问，请补充说明"
        elif state == "ARCHITECTURE_INPUT_PENDING":
            return "Agent 对技术方案有疑问，请补充说明"
        elif state == "IMPLEMENTATION_SPEC_INPUT_PENDING":
            return "Agent 对 Spec 有疑问，请补充说明"
        elif state == "BUG_FIX_INPUT_PENDING":
            return "Agent 对 Bug 分析有疑问，请补充说明"
        elif state == "OUTLINE_INPUT_PENDING":
            return "Agent 对文档大纲有疑问，请补充说明"
        elif state == "TESTING_INPUT_PENDING":
            return "Tester 请求澄清，请回复你的答案"
        elif state == "REVIEW_INPUT_PENDING":
            return "Reviewer 请求澄清，请回复你的答案"
        elif state == "ACCEPTANCE_INPUT_PENDING":
            return "验收评估 Agent 请求澄清，请回复你的答案"
        return f"请输入 {state}"

    def _build_summary_for_state(self, project: Project, state: str) -> str:
        """Read the relevant .mac/ output file and return a human-readable summary.

        The output is pure plain text / lark-md — no JSON key-value pairs.
        """
        if state == "ACCEPTANCE_PENDING":
            return self._build_acceptance_summary(project)

        if state.endswith("_RECOVERY_PENDING"):
            failure = project.context.get("last_agent_failure", {})
            error = failure.get("error") or "Agent 未返回有效结果"
            return (
                f"**执行失败**\n"
                f"- Agent：{failure.get('agent', 'unknown')}\n"
                f"- 阶段：{failure.get('state', state)}\n"
                f"- 已尝试：{failure.get('attempts', '多次')} 次\n"
                f"- 原因：{error}\n\n"
                "请选择再次重试、回退处理或终止项目。"
            )

        # Quality gates: pull the latest completed task output for the report card.
        quality_gate_agent = {
            "CODING_CONFIRM_PENDING": "coder",
            "TESTING_CONFIRM_PENDING": "tester",
            "REVIEW_CONFIRM_PENDING": "reviewer",
        }.get(state)
        if quality_gate_agent:
            return self._build_agent_task_summary(project, quality_gate_agent)

        if state == "REWORK":
            open_feedback = self.project_service.get_open_feedback_packets(project.id)
            if open_feedback:
                parts = ["**未解决的反馈**"]
                for fb in open_feedback:
                    target = fb.recommended_rework_target or "未指定"
                    parts.append(f"- [{fb.source_stage}] {fb.summary}（建议回退：{target}）")
                parts.append("\n请选择要回退到哪一层重新处理。")
                return "\n".join(parts)
            legacy_feedback = project.context.get("latest_rejection_feedback", "")
            if legacy_feedback:
                return f"**驳回/反馈**\n{legacy_feedback}\n\n请选择要回退到哪一层重新处理。"
            return "请选择要回退到哪一层重新处理。"

        mac = project_mac_dir(project)

        file_map = {
            "REQUIREMENT_CONFIRM_PENDING": "REQUIREMENTS.json",
            "ARCHITECTURE_CONFIRM_PENDING": "ARCHITECTURE.json",
            "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "SPECS.json",
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
            rows = []
            for ac in data["acceptance_criteria"]:
                if isinstance(ac, dict):
                    sid = ac.get("id", "")
                    stmt = ac.get("statement") or ac.get("description", "")
                    rows.append(f"• {sid} {stmt}" if sid else f"• {stmt}")
                else:
                    rows.append(f"• {ac}")
            if rows:
                parts.append("**验收标准**\n" + "\n".join(rows))
        if data.get("constraints"):
            parts.append("**约束**\n" + "\n".join(f"• {c}" for c in data["constraints"]))
        if data.get("requirements"):
            count = len(data["requirements"])
            parts.append(f"**需求条目**：共 {count} 条（详细内容可通过 `/status` 查看）")
        if data.get("ambiguities"):
            rows = []
            for amb in data["ambiguities"]:
                if isinstance(amb, dict):
                    q = amb.get("question", "")
                    rows.append(f"• {q}" if q else "")
                else:
                    rows.append(f"• {amb}")
            rows = [r for r in rows if r]
            if rows:
                parts.append("**待澄清**\n" + "\n".join(rows))
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

    def _build_agent_task_summary(self, project: Project, agent_type: str) -> str:
        """Return a human-readable summary from the latest completed task of an agent."""
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
            return self._fallback_summary(project, project.current_state)

        out = task.output_data
        parts: list[str] = []

        if agent_type == "coder":
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

        elif agent_type == "tester":
            report = out.get("report", "")
            if report:
                parts.append(f"**测试报告**\n{report}")
            passed = out.get("passed")
            if passed is not None:
                parts.append(f"**结果**：{'通过' if passed else '未通过'}")
            failures = out.get("failures") or []
            if failures and isinstance(failures, list):
                parts.append("**失败项**\n" + "\n".join(
                    f"• {f.get('test', '')}：{f.get('error', '')}" for f in failures if isinstance(f, dict)
                ))
            coverage = out.get("coverage")
            if coverage is not None:
                parts.append(f"**覆盖率**：{coverage * 100:.1f}%")

        elif agent_type == "reviewer":
            result = out.get("result")
            if result:
                parts.append(f"**Review 结论**：{'通过' if result == 'pass' else '不通过'}")
            summary = out.get("summary", "")
            if summary:
                parts.append(f"**Review 总结**\n{summary}")
            issues = out.get("issues") or []
            if issues and isinstance(issues, list):
                issue_lines = []
                for issue in issues:
                    if isinstance(issue, dict):
                        sev = issue.get("severity", "")
                        desc = issue.get("description", "")
                        file = issue.get("file", "")
                        line = issue.get("line")
                        loc = f"{file}:{line}" if file and line else file
                        issue_lines.append(f"• [{sev}] {desc}" + (f"（{loc}）" if loc else ""))
                if issue_lines:
                    parts.append("**问题清单**\n" + "\n".join(issue_lines))
            notes = out.get("notes")
            if notes and isinstance(notes, list):
                parts.append("**备注**\n" + "\n".join(f"• {n}" for n in notes if isinstance(n, str)))

        if not parts:
            return self._fallback_summary(project, project.current_state)
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

    def _make_progress_callback(self, project: Project, task: Any) -> Callable[[str, str], Awaitable[None]] | None:
        """Return a callback that pushes agent progress to Feishu.

        Labels every progress card with project and agent.  Only used when
        DEBUG_AGENT_PROGRESS=1; failures never affect the task itself.
        """
        logger.info(
            "progress_callback requested for %s (bridge=%s, debug=%s)",
            project.id, self.feishu_bridge is not None, self.settings.debug_agent_progress,
        )
        if self.feishu_bridge is None:
            logger.info("progress_callback skipped — no FeishuBridge")
            return None
        if not self.settings.debug_agent_progress:
            logger.info("progress_callback skipped — DEBUG_AGENT_PROGRESS not set")
            return None
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        if not receive_id:
            logger.info("progress_callback skipped — no recipient for %s", project.id)
            return None

        agent_label_map = {
            "planner": "需求分析师",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理",
            "acceptance_evaluator": "验收评估",
        }
        agent_display = agent_label_map.get(task.agent_type, task.agent_type or "Agent")
        project_tag = project.name or project.id

        async def _cb(event_type: str, content: str) -> None:
            try:
                if event_type == "thinking":
                    await self.feishu_bridge.send_content_card(
                        open_id=receive_id,
                        title=f"[{project_tag}] {agent_display} · 思考中",
                        content=content,
                        color="blue",
                        receive_id_type=receive_id_type,
                    )
                elif event_type == "tool":
                    import json
                    data = json.loads(content)
                    name = data.get("name", "?")
                    inp = data.get("input", {})
                    preview = json.dumps(inp, ensure_ascii=False)[:200]
                    await self.feishu_bridge.send_content_card(
                        open_id=receive_id,
                        title=f"[{project_tag}] {agent_display} · 正在执行",
                        content=f"⚙️ **{name}**: {preview}",
                        color="purple",
                        receive_id_type=receive_id_type,
                    )
            except Exception as exc:
                logger.warning("progress_callback failed: %s", exc)
        return _cb

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
        mac = project_mac_dir(project)
        file_map = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_IMPLEMENTATION_SPECS": "SPECS.json",
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
