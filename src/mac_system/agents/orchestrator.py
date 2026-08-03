import asyncio
import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from urllib.parse import unquote, urlparse
from pathlib import Path
from collections.abc import Awaitable
from typing import Any, Callable


from sqlmodel import select

from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import AgentRuntime, validate_agent_output
from mac_system.config import get_allowed_bind_roots, get_settings
from mac_system.core.lock_manager import ProjectLockManager, get_default_lock_manager
from mac_system.core.project_service import (
    InvalidStateTransitionError,
    ProjectService,
    StaleApprovalStateError,
)
from mac_system.core.notification_service import deliver_card
from mac_system.core.failure_router import FailureRouter
from mac_system.core.loop_supervisor import LoopSupervisor
from mac_system.core.project_paths import project_mac_dir
from mac_system.core.runtime_manager import RuntimeManager
from mac_system.core.delivery_policy import stage_required
from mac_system.core.work_item import extract_acceptance_criterion_ids
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.utils.formatting import (
    format_tool_preview,
    split_markdown_sections,
)
from mac_system.utils.redaction import redact_secrets, redact_text
from mac_system.models import (
    AcceptanceEvidence,
    Approval,
    FeedbackPacket,
    Project,
    QuestionInteraction,
    Task,
)
from mac_system.state_machine import StateMachine


class OrchestratorError(Exception):
    pass


logger = logging.getLogger(__name__)


_USER_FACING_INTERNAL_ID_RE = re.compile(
    r"\b(?:appr|proj|task|fb|exec|pending)_[A-Za-z0-9_-]+\b"
)


def _concise_agent_failure(error: str | None) -> str:
    """Return a bounded, redacted reason suitable for the next retry prompt."""
    text = str(error or "Agent returned failure")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    preferred = next(
        (
            line
            for line in reversed(lines)
            if "Invalid agent output:" in line
            or "Agent contract violation:" in line
            or line.startswith("API error:")
            or "API Error" in line
            or "Insufficient Balance" in line
        ),
        lines[-1] if lines else "Agent returned failure",
    )
    safe = str(redact_secrets(preferred))
    if len(safe) <= 1200:
        return safe
    return safe[:500] + " ...[truncated]... " + safe[-680:]


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


def _sanitize_user_facing_summary(text: str, state_machine: StateMachine) -> str:
    """Remove protocol noise while preserving user-relevant report content."""
    value = redact_text(str(text or ""))
    value = _USER_FACING_INTERNAL_ID_RE.sub("相关内部记录", value)
    for state in sorted(state_machine.states, key=len, reverse=True):
        value = value.replace(state, state_machine.display_name(state))
    field_labels = {
        "verification_method": "验证方式",
        "evidence_source": "证据来源",
        "missing reviewer coverage": "缺少代码评审覆盖",
        "invalid test evidence refs": "测试证据引用无效",
    }
    for raw, label in field_labels.items():
        value = value.replace(raw, label)
    return value


def _mention_prefix(open_id: str | None) -> str:
    """Return a Feishu @ mention tag for use in group text messages."""
    if not open_id:
        return ""
    return f'<at user_id="{open_id}">@用户</at> '


# Quality-gate confirmation states. Their preceding specialist has already
# produced a pass verdict, so AI-first modes advance immediately while still
# emitting a report card. Strict mode remains explicitly human-controlled.
_AUTO_CONFIRM_STATES: frozenset[str] = frozenset({
    "CODING_CONFIRM_PENDING",
    "TESTING_CONFIRM_PENDING",
    "REVIEW_CONFIRM_PENDING",
})

_PLANNER_ARTIFACTS: dict[str, tuple[str, str]] = {
    "PLANNING_REQUIREMENTS": ("REQUIREMENTS.json", "requirements"),
    "PLANNING_ARCHITECTURE": ("ARCHITECTURE.json", "architecture"),
    "PLANNING_IMPLEMENTATION_SPECS": ("SPECS.json", "specs"),
    "BUG_ANALYSIS": ("BUG_ANALYSIS.json", "bug_analysis"),
    "DOC_OUTLINE": ("DOC_OUTLINE.json", "doc_outline"),
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
        background_scheduler: Callable[[str], None] | None = None,
    ):
        self.project_service = project_service
        self.worktree_manager = worktree_manager
        self.runtime = runtime
        self.prompt_renderer = prompt_renderer
        self.feishu_bridge = feishu_bridge
        self.settings = get_settings()
        self.lock_manager = lock_manager or get_default_lock_manager()
        self.background_scheduler = background_scheduler
        self._drive_depth = 0
        self.loop_supervisor = LoopSupervisor()
        self._background_ticks: set[asyncio.Task] = set()

    @staticmethod
    def _round_audit_meta(
        task: Task,
        *,
        verdict: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Build a bounded input/output audit record for one Agent round.

        Full prompts and artifacts remain in the task/worktree.  This record
        deliberately keeps only the routing and contract facts needed to spot
        a wrong handoff immediately, without copying large or secret context
        into every log line.
        """
        task_input = task.input_data if isinstance(task.input_data, dict) else {}
        contract = task_input.get("work_item_contract")
        contract = contract if isinstance(contract, dict) else {}
        result = output if isinstance(output, dict) else {}

        def _criterion_ids(key: str) -> list[str]:
            return sorted(
                {
                    str(item.get("id"))
                    for item in (contract.get(key) or [])
                    if isinstance(item, dict) and item.get("id")
                }
            )

        def _feedback_ids(key: str) -> list[str]:
            values = result.get(key) or []
            ids: set[str] = set()
            for value in values:
                if isinstance(value, dict):
                    candidate = value.get("feedback_id")
                else:
                    candidate = value
                if candidate:
                    ids.add(str(candidate))
            return sorted(ids)

        artifact_refs = {
            str(name): {
                field: value.get(field)
                for field in ("version", "round", "status")
                if value.get(field) is not None
            }
            for name, value in (contract.get("artifact_refs") or {}).items()
            if isinstance(value, dict)
        }
        audit = {
            "schema_version": 1,
            "verdict": verdict,
            "task_id": task.id,
            "attempt_id": task.attempt_id,
            "attempt_number": task.attempt_number,
            "round_number": task.round_number,
            "agent_type": task.agent_type,
            "state": task.state,
            "context_version": task.context_version,
            "input_contract": {
                "artifact_contract_key": contract.get("artifact_contract_key"),
                "artifact_refs": artifact_refs,
                "acceptance_criterion_ids": _criterion_ids(
                    "acceptance_criteria"
                ),
                "regression_criterion_ids": _criterion_ids(
                    "regression_acceptance_criteria"
                ),
                "feedback_refs": sorted(
                    str(value)
                    for value in (contract.get("feedback_refs") or [])
                    if value
                ),
                "runtime_provider": task_input.get("runtime_provider"),
            },
            "output_decision": {
                key: result.get(key)
                for key in (
                    "success",
                    "next_state_event",
                    "passed",
                    "result",
                    "blocking",
                    "recommended_rework_target",
                )
                if key in result
            },
            "feedback_effects": {
                "addressed": _feedback_ids("addressed_feedback"),
                "verified": _feedback_ids("verified_feedback_ids"),
            },
            "evidence_counts": {
                "test_evidence": len(result.get("test_evidence") or []),
                "review_issues": len(result.get("issues") or []),
                "modified_files": len(result.get("modified_files") or []),
                "questions": len(
                    (result.get("question_batch") or {}).get("questions") or []
                )
                if isinstance(result.get("question_batch"), dict)
                else 0,
            },
        }
        if error:
            audit["error"] = _concise_agent_failure(error)
        return redact_secrets(audit)

    async def tick(self, project_id: str) -> None:
        project = self.project_service.require_project(project_id)
        if project.context.get("orchestration_mode") == "agent_loop":
            await self.run_coordinator(project_id)
            return
        lock = await self.lock_manager.acquire(project_id)
        async with lock:
            try:
                # A request can create its SQLAlchemy session before it waits
                # for this cross-loop lock. Another tick may advance the
                # project while the request is queued, leaving an identity-map
                # snapshot older than the lock boundary. Dispatch only from
                # durable state observed after acquiring the lock.
                self.project_service.session.expire_all()
                await self._tick_unsafe(project_id)
            except StaleApprovalStateError:
                # Feishu WS and HTTP control-plane ticks can run on different
                # event loops. If the other loop already advanced the state,
                # the database guard rejects this stale boundary cleanly.
                logger.info(
                    "Skipped stale approval creation after concurrent state "
                    "transition for %s",
                    project_id,
                )

    async def run_coordinator(
        self, project_id: str, trigger: str | None = None
    ):
        """Run or resume the native Coordinator session for one project."""
        from mac_system.core.coordinator_engine import CoordinatorEngine

        project = self.project_service.require_project(project_id)
        if trigger is None:
            brief = (project.context or {}).get("confirmed_brief") or {}
            trigger = (
                "Start or continue this project. User objective:\n"
                + str(brief.get("objective") or project.description or "")
                + "\nConfirmed scope and acceptance context:\n"
                + json.dumps(brief, ensure_ascii=False)
            )
        engine = CoordinatorEngine(
            self.project_service.session,
            self.runtime,
            self.worktree_manager,
            self.settings,
            feishu_bridge=self.feishu_bridge,
        )
        return await engine.run(project_id, trigger)

    async def steer_coordinator(self, project_id: str, text: str) -> bool:
        """Append user input to an active native turn when one exists."""
        from mac_system.core.coordinator_engine import CoordinatorEngine

        engine = CoordinatorEngine(
            self.project_service.session,
            self.runtime,
            self.worktree_manager,
            self.settings,
            feishu_bridge=self.feishu_bridge,
        )
        return await engine.steer(project_id, text)

    def _has_immediate_work(self, project_id: str) -> bool:
        """Whether the current state can advance without a new external event."""
        project = self.project_service.require_project(project_id)
        if project.context.get("orchestration_mode") == "agent_loop":
            return (
                project.status == "active"
                and project.execution_status
                not in {"waiting_user", "waiting_external", "running_agent"}
                and not self.project_service._running_tasks(project_id)
            )
        machine = StateMachine(project.task_type)
        if (
            project.status != "active"
            or machine.is_terminal(project.current_state)
            or machine.is_idle(project.current_state)
            or project.execution_status == "waiting_external"
        ):
            return False

        state_type = machine.state_type(project.current_state)
        if state_type == "agent_execution":
            return not self.project_service._running_tasks(project_id)
        if state_type == "human_confirmation":
            return self.project_service.get_project_status(project_id).get(
                "pending_approval"
            ) is None
        if state_type == "human_input":
            return self.project_service.get_open_question_interaction(project_id) is None
        return state_type == "system_action"

    async def tick_until_stable(
        self, project_id: str, *, max_ticks: int = 10
    ) -> None:
        """Advance an externally resumed flow until it reaches a real boundary.

        A single tick intentionally executes one state.  Some states can cross
        an automatically resolved confirmation boundary (for example CODING ->
        CODING_CONFIRM_PENDING -> TESTING) without creating a pending approval.
        External resume paths must keep driving in that case; otherwise the
        project is left marked ``running`` in the next agent state with no task.
        """
        self._drive_depth = getattr(self, "_drive_depth", 0) + 1
        exhausted = False
        try:
            project = self.project_service.require_project(project_id)
            if getattr(project, "context", {}).get("orchestration_mode") == "agent_loop":
                await self.tick(project_id)
                return
            for _ in range(max_ticks):
                project = self.project_service.require_project(project_id)
                machine = StateMachine(project.task_type)
                if (
                    project.status != "active"
                    or machine.is_terminal(project.current_state)
                    or machine.is_idle(project.current_state)
                    or project.execution_status == "waiting_external"
                ):
                    return

                before_state = project.current_state
                await self.tick(project_id)

                self.project_service.session.expire_all()
                project = self.project_service.require_project(project_id)
                if project.current_state == before_state:
                    return

            exhausted = True
            logger.warning(
                "Stopped automatic advancement for %s after %s ticks",
                project_id,
                max_ticks,
            )
        finally:
            self._drive_depth -= 1
            # The bound prevents one recovery callback from monopolizing the
            # event loop; it is not a terminal boundary.  Follow-up scheduling
            # is suppressed while this driver owns the flow, so reaching the
            # bound must enqueue another process-owned drive after releasing
            # that ownership or the project remains in an executable state
            # with no task.
            if exhausted and self._drive_depth == 0:
                self._schedule_followup_tick(project_id)

    def _schedule_followup_tick(self, project_id: str) -> None:
        """Queue the next internal step after the current project lock releases."""
        # The persistent Feishu service owns autonomous background execution.
        # CLI/unit orchestrators without a bridge retain deterministic one-step
        # tick semantics; their caller can use tick_until_stable explicitly.
        if self.feishu_bridge is None:
            return
        self.project_service.session.expire_all()
        if not self._has_immediate_work(project_id):
            return
        # ``tick_until_stable`` already owns the level-triggered drive loop.
        # Scheduling another worker from inside that loop creates redundant
        # lock waiters and can make a simple reject appear stalled for minutes.
        if getattr(self, "_drive_depth", 0) > 0:
            return
        # Real Feishu messages are handled on short-lived SDK event loops.  A
        # task created on that loop is cancelled as soon as the callback
        # returns.  Delegate to the process-owned scheduler when available;
        # API/tests that own a durable loop retain the lightweight local task.
        background_scheduler = getattr(self, "background_scheduler", None)
        if background_scheduler is not None:
            background_scheduler(project_id)
            return
        task = asyncio.create_task(self.tick(project_id))
        self._background_ticks.add(task)

        def _done(completed: asyncio.Task) -> None:
            self._background_ticks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error(
                    "Background follow-up tick failed for %s: %s",
                    project_id,
                    error,
                )

        task.add_done_callback(_done)

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        """Cancel a project, fail running tasks, and notify the chat/user."""
        project = self.project_service.require_project(project_id)
        running_task_ids = [
            task.id for task in self.project_service._running_tasks(project_id)
        ]

        # Persist cancellation before killing the process.  The interrupted
        # handler may wake up immediately and attempt a technical retry; it
        # must observe cancelled state before that can happen.
        await self.project_service.cancel_project(project_id, reason=reason, triggered_by="user")

        # Best-effort kill any running agent subprocess after the durable
        # cancellation marker is visible to other DB sessions.
        for task_id in running_task_ids:
            try:
                self.runtime.cancel(task_id)
            except Exception:
                pass

        if self.feishu_bridge is None:
            return
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            from mac_system.feishu.card import build_content_card

            await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"project:{project.id}:cancelled:{project.updated_at.isoformat()}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="result",
                card=build_content_card(
                    f"[{project.name or project.id}] 项目已终止",
                    reason or "项目已按用户要求终止。",
                    color="red",
                ),
                project_id=project.id,
            )
        except Exception as exc:
            logger.warning("Failed to send cancellation notification for %s: %s", project_id, exc)

    def interrupt_running_agents(self, project_id: str) -> list[str]:
        """Stop active runtime processes without cancelling the project.

        Used when a correction or scope change invalidates the context of an
        in-flight Agent. The durable user input is written before this method
        is called, so the normal retry sees the newer context instead of
        spending minutes finishing work that must be discarded.
        """
        interrupted: list[str] = []
        for task in self.project_service._running_tasks(project_id):
            try:
                self.runtime.cancel(task.id)
                interrupted.append(task.id)
            except Exception as exc:
                logger.warning("Failed to interrupt task %s: %s", task.id, exc)
        return interrupted

    async def _tick_unsafe(self, project_id: str) -> None:
        project = self.project_service.require_project(project_id)

        if project.status in ("paused", "cancelled"):
            return
        if project.execution_status == "waiting_external":
            logger.info(
                "Skipping tick for %s: waiting for external user evidence",
                project.id,
            )
            return

        state_machine = StateMachine(project.task_type)
        state = project.current_state

        # Apply any agent result that was produced while the project was paused.
        paused_result = project.context.get("paused_agent_result")
        if paused_result:
            expected_agent = state_machine.agent_for(state)
            if (
                paused_result.get("state") == state
                and paused_result.get("agent_type") == expected_agent
                and isinstance(paused_result.get("output"), dict)
                and "next_state_event" in paused_result["output"]
                and not state_machine.is_terminal(state)
            ):
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
            if state == "COMPLETED":
                await self._notify_delivery_completed(project)
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

    @staticmethod
    def _filter_facts_for_current_trace(
        facts: list[dict[str, Any]], current_trace_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Keep unscoped facts and facts linked to this WorkItem's contract.

        The relation is carried by durable AC/feedback IDs.  Human text,
        capability descriptions, environment variable names, and other
        keyword-like signals are deliberately not part of this decision.
        """
        return [
            fact
            for fact in facts
            if not (
                fact.get("type") == "capability"
                and fact.get("affected_trace_ids")
                and current_trace_ids.isdisjoint(
                    str(value)
                    for value in fact.get("affected_trace_ids") or []
                )
            )
        ]

    @staticmethod
    def _current_feedback_frontier(
        packets: list[FeedbackPacket],
    ) -> list[FeedbackPacket]:
        """Return the one packet that currently determines the next action.

        Feedback storage is an append-only verification ledger: an old packet
        may remain open until an independent quality Agent verifies closure.
        REWORK routing is different.  It must act on the newest blocking
        verdict, whose evidence already contains the concrete failures from
        that round, instead of replaying every still-open historical packet.
        """
        if not packets:
            return []
        blocking = [packet for packet in packets if packet.blocking]
        candidates = blocking or packets
        return [
            max(
                candidates,
                key=lambda packet: (packet.created_at, packet.id),
            )
        ]

    @staticmethod
    def _requirements_round_facts(
        facts: list[dict[str, Any]], *, has_current_artifact: bool
    ) -> list[dict[str, Any]]:
        """Use initial brief facts only before the first Requirements artifact.

        Once Requirements exists, it is the compiled scope authority.  A
        rework round receives that file plus current feedback; replaying the
        original project objective/scope resurrects requirements deliberately
        retired by later confirmed revisions.
        """
        explicitly_scoped = [
            fact
            for fact in facts
            if fact.get("scope_type") not in {None, "", "project"}
        ]
        if has_current_artifact:
            return explicitly_scoped
        brief_fact_keys = {
            "project.objective",
            "project.scope",
            "project.out_of_scope",
            "project.acceptance_criteria",
            "execution.permissions",
        }
        return [
            fact
            for fact in facts
            if fact in explicitly_scoped or fact.get("key") in brief_fact_keys
        ]

    async def _execute_agent(self, project: Project, state_machine: StateMachine) -> None:
        state = project.current_state
        agent_type = state_machine.agent_for(state)
        stage = state_machine.stage_for(state)

        # Backfill projects created before deterministic iteration
        # traceability was introduced. This is idempotent and must happen
        # before context files and the WorkItem are compiled for Tester.
        if project.task_type == "iteration":
            self.project_service.ensure_iteration_specs(project)

        if not agent_type:
            raise OrchestratorError(f"No agent configured for state {state}")

        profile_stage = next(
            (
                item
                for item in state_machine.profile.get("stages", [])
                if item.get("execution_state") == state
            ),
            None,
        )
        stage_id = str((profile_stage or {}).get("id") or "")
        delivery_policy = (project.context or {}).get("delivery_policy")
        if stage_id and not stage_required(delivery_policy, stage_id):
            await self.project_service.log_event(
                project_id=project.id,
                level="INFO",
                message=f"Stage {stage_id} skipped by delivery evidence policy",
                meta={
                    "event_type": "stage_skipped",
                    "execution_state": state,
                    "stage_id": stage_id,
                    "policy": delivery_policy,
                },
            )
            await self.project_service.transition_state(
                project.id,
                "complete",
                triggered_by="delivery_policy",
            )
            refreshed = self.project_service.require_project(project.id)
            await self._handle_human_confirmation(refreshed, state_machine)
            return

        # Check retry limits for non-deterministic states
        retry_key = state
        retry_count = project.context.get("retry_counts", {}).get(retry_key, 0)
        max_retries = self._max_retries_for_state(state)
        if retry_count >= max_retries:
            await self.project_service.transition_state(
                project.id, "runtime_exhausted", triggered_by="orchestrator"
            )
            refreshed = self.project_service.require_project(project.id)
            await self._handle_human_confirmation(refreshed, state_machine)
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
                TaskModel.agent_type == agent_type,
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

        # A correction may already have been persisted by an interrupted or
        # cancelled attempt. If Coordinator assigned it to another owner,
        # resume directly at that owner instead of rerunning the stale stage.
        # Resume exactly one correction at a time, newest first.  Selecting
        # independently from packets whose ``source_stage`` happens to match
        # the current state lets two still-open packets point at each other's
        # owners (for example specs -> testing and testing -> specs).  The
        # recursive owner handoff then oscillates until Python's recursion
        # limit, without ever executing either owner.  A correction's routing
        # lifecycle is separate from its later verification lifecycle, so the
        # globally newest unhandled routing directive is the only authoritative
        # one for this dispatch boundary.
        pending_owner_feedback = sorted(
            (
                feedback
                for feedback in self.project_service.get_open_feedback_packets(
                    project.id
                )
                if feedback.blocking
                and feedback.kind in {"scope_change", "correction"}
                and str(feedback.recommended_rework_target or "").strip()
                and not self._feedback_owner_already_addressed(
                    project.id, feedback, state_machine
                )
            ),
            key=lambda feedback: (feedback.created_at, feedback.id),
            reverse=True,
        )
        persisted_owner = (
            str(pending_owner_feedback[0].recommended_rework_target).strip()
            if pending_owner_feedback
            else None
        )
        persisted_route_event = (
            state_machine.route_event_to_stage(state, persisted_owner)
            if persisted_owner
            else None
        )
        if persisted_route_event:
            await self.project_service.transition_state(
                project.id,
                persisted_route_event,
                triggered_by="conversation_coordinator",
            )
            await self.project_service.log_event(
                project_id=project.id,
                level="INFO",
                message="Resumed persisted correction at assigned owner",
                meta={
                    "from_state": state,
                    "target_stage": persisted_owner,
                    "feedback_id": pending_owner_feedback[0].id,
                },
            )
            await self._execute_agent(
                self.project_service.require_project(project.id), state_machine
            )
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
        retry_feedback_by_state = dict(new_context.get("agent_retry_feedback", {}))
        retry_feedback = str(retry_feedback_by_state.get(state, "") or "")
        # Coordinator instructions are state-scoped durable input, not a
        # queue item that is consumed when a provider process starts.  Keep
        # the instruction until transition_state successfully leaves this
        # execution state; otherwise a service restart or provider crash can
        # silently relaunch the same stage without the decision that routed it.
        routed_feedback_by_state = dict(
            new_context.get("routed_feedback_ids_by_state", {})
        )
        routed_feedback_ids = {
            str(value)
            for value in (routed_feedback_by_state.pop(state, []) or [])
            if value
        }
        if routed_feedback_by_state:
            new_context["routed_feedback_ids_by_state"] = routed_feedback_by_state
        else:
            new_context.pop("routed_feedback_ids_by_state", None)
        project.context = new_context
        # A blocking input can arrive after the previous runtime process has
        # exited but before its provider fallback (or a recovered retry) is
        # created.  There is no live process to cancel in that gap.  Carry the
        # input into the next task explicitly instead of waiting until that
        # task finishes and invalidating yet another result.
        pending_before_start = [
            inp
            for inp in self.project_service.get_pending_user_inputs(project.id)
            if inp.kind in ("scope_change", "correction")
        ]
        if pending_before_start:
            pending_note = "\n".join(
                f"- [{inp.kind}] {inp.content}" for inp in pending_before_start
            )
            retry_feedback = "\n\n".join(
                part
                for part in (
                    retry_feedback.strip(),
                    "User input received before this attempt; incorporate it "
                    "into the current work and do not ask the user to repeat it:\n"
                    + pending_note,
                )
                if part
            )
        self._resolve_incoherent_tester_feedback(project.id)
        await self._recover_missing_coordinator_rejection_feedback(project, state)
        superseded_quality_ids = (
            self._quality_feedback_superseded_by_requirements_scope_change(
                project.id
            )
        )
        feedback_packets = [
            fb
            for fb in self.project_service.get_open_feedback_packets(project.id)
            if fb.id not in superseded_quality_ids
            and self._feedback_needed_for_work_item(
                project.id,
                state,
                fb,
                state_machine,
                routed_feedback_ids,
            )
        ]
        if state == "REWORK":
            feedback_packets = self._current_feedback_frontier(feedback_packets)
        if (
            state in {"CODING", "TESTING"}
            and project.previous_state == "REWORK"
            and not feedback_packets
            and not pending_before_start
        ):
            latest_reviewer = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == "reviewer",
                    Task.status == "completed",
                )
                .order_by(Task.updated_at.desc())
            ).first()
            reviewer_output = (
                latest_reviewer.output_data
                if latest_reviewer is not None
                and isinstance(latest_reviewer.output_data, dict)
                else {}
            )
            if self._clean_reviewer_handoff_output(reviewer_output):
                await self.project_service.log_event(
                    project_id=project.id,
                    level="WARNING",
                    message=(
                        "Skipped orphaned rework after resolving contradictory "
                        "Reviewer feedback"
                    ),
                    meta={"state": state, "reviewer_task_id": latest_reviewer.id},
                )
                await self.project_service.transition_state(
                    project.id, "complete", triggered_by="orchestrator"
                )
                refreshed = self.project_service.require_project(project.id)
                await self._auto_resolve_quality_gates(refreshed, state_machine)
                return
        if state == "REVIEW":
            # Tester may close a packet after verifying the fix. Reviewer still
            # needs the original statement to independently check that closure;
            # an ID in TESTER_OUTPUT alone is not meaningful evidence.
            latest_tester = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == "tester",
                    Task.status == "completed",
                )
                .order_by(Task.updated_at.desc())
            ).first()
            verified_ids = {
                str(value)
                for value in (
                    (
                        (latest_tester.output_data or {}).get(
                            "verified_feedback_ids"
                        )
                        or []
                    )
                    if latest_tester is not None
                    else []
                )
                if value
            }
            if verified_ids:
                known_ids = {packet.id for packet in feedback_packets}
                feedback_packets.extend(
                    packet
                    for packet in self.project_service.session.exec(
                        select(FeedbackPacket).where(
                            FeedbackPacket.project_id == project.id,
                            FeedbackPacket.id.in_(verified_ids),
                        )
                    ).all()
                    if packet.id not in known_ids
                )
        open_feedback = self._compact_feedback_prompt_payloads([
            self._feedback_packet_prompt_payload(packet)
            for packet in feedback_packets
        ])
        # Collect answered QuestionInteraction history so agents don't re-ask.
        qa_history = self._collect_qa_history(project.id)

        # Create task
        (
            task_acceptance_contract,
            regression_acceptance_contract,
        ) = self._work_item_acceptance_contracts(
            project
        )
        planning_state = state in {
            "PLANNING_REQUIREMENTS",
            "PLANNING_ARCHITECTURE",
            "PLANNING_IMPLEMENTATION_SPECS",
            "PLANNING_BUG_ANALYSIS",
            "PLANNING_DOC_OUTLINE",
        }
        # Reused workspaces have two contracts at every stage: the current
        # change scope and inherited regression obligations. Flattening them
        # made Coder implement unrelated historical requirements and made
        # quality Agents report old product scope as unfinished new work.
        relevant_facts = self.project_service.build_relevant_context(
            project.id,
            agent_type=agent_type,
            state=state,
        )
        current_trace_ids = {
            str(item.get("id"))
            for item in task_acceptance_contract
            if isinstance(item, dict) and item.get("id")
        } | {packet.id for packet in feedback_packets}
        relevant_facts = self._filter_facts_for_current_trace(
            relevant_facts, current_trace_ids
        )
        if state in {
            "PLANNING_ARCHITECTURE",
            "PLANNING_IMPLEMENTATION_SPECS",
        }:
            # These stages receive the confirmed upstream artifact files, the
            # current open feedback, and the reference contract separately.
            # Replaying every project-scoped requirement/decision here mixes
            # retired acceptance criteria and old corrections back into a new
            # artifact version.  Only explicitly scoped facts may supplement
            # those authoritative inputs.
            relevant_facts = [
                fact
                for fact in relevant_facts
                if fact.get("scope_type") not in {None, "", "project"}
            ]
        elif state == "PLANNING_REQUIREMENTS":
            relevant_facts = self._requirements_round_facts(
                relevant_facts,
                has_current_artifact=(
                    self.project_service.get_artifact_version(
                        project.id, "requirements"
                    )
                    is not None
                ),
            )
        elif not planning_state:
            # Requirements/Architecture/Specs are the compiled, current
            # authority for delivery agents.  Replaying every project-scoped
            # planning decision alongside them resurrects superseded choices
            # (for example, an earlier provider decision) and makes quality
            # agents test the old design.  Downstream agents receive only
            # explicitly scoped facts and runtime capabilities; the current
            # artifacts and acceptance contracts carry product requirements.
            relevant_facts = [
                fact
                for fact in relevant_facts
                if fact.get("type") == "capability"
                or fact.get("scope_type") not in {None, "", "project"}
            ]
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
                "retry_feedback": retry_feedback,
                "work_item_contract": {
                    "relevant_facts": relevant_facts,
                    "acceptance_criteria": task_acceptance_contract,
                    "regression_acceptance_criteria": regression_acceptance_contract,
                    "reference": self._reference_contract_for_project(project),
                    "include_regression_in_scope": (
                        state in {"TESTING", "REVIEW", "ACCEPTANCE_PENDING"}
                        and project.task_type != "iteration"
                    ),
                    **(
                        {
                            "runtime_contract": RuntimeManager.verification_contract(
                                project.context
                            )
                        }
                        if state in {"TESTING", "REVIEW"}
                        else {}
                    ),
                },
            },
        )
        if pending_before_start:
            # Bind the input to the concrete task only after its ID exists.
            # Feedback packets preserve the audit trail and make the same
            # correction visible to downstream quality gates.
            created_feedback: list[FeedbackPacket] = []
            for inp in pending_before_start:
                await self.project_service.consume_pending_user_input(
                    inp.id, task.id, commit=False
                )
                created_feedback.append(await self.project_service.create_feedback_packet(
                    project_id=project.id,
                    source_stage=state,
                    source_task_id=task.id,
                    source_round=round_number,
                    kind=inp.kind,
                    summary=inp.content,
                    blocking=True,
                    recommended_rework_target=inp.recommended_rework_target,
                    user_comment=inp.content,
                    commit=False,
                ))
            # The initial ``open_feedback`` snapshot predates the concrete task
            # ID. Add packets created from pre-start input to this same task's
            # contract so the Agent can cite their stable IDs when verifying
            # the correction instead of leaving an invisible blocker behind.
            task_open_feedback = list((task.input_data or {}).get("open_feedback") or [])
            task_open_feedback.extend(
                self._feedback_packet_prompt_payload(fb) for fb in created_feedback
            )
            task = self.project_service.refresh_task_work_item_feedback(
                task.id, task_open_feedback
            )
            # Coordinator ownership is authoritative.  Do not spend another
            # model turn asking the current stage to interpret feedback that
            # has already been routed to an earlier/different owner.
            route_target = self._select_rework_target(
                state_machine,
                {
                    str(item.recommended_rework_target).strip()
                    for item in pending_before_start
                    if str(item.recommended_rework_target or "").strip()
                },
            )
            route_event = (
                state_machine.route_event_to_stage(state, route_target)
                if route_target
                else None
            )
            if route_event:
                await self.project_service.mark_task_stale(
                    task.id,
                    {
                        "success": True,
                        "next_state_event": "fail",
                        "completion_reason": "routed_to_feedback_owner",
                    },
                    f"Coordinator routed new feedback to {route_target}",
                )
                await self.project_service.transition_state(
                    project.id,
                    route_event,
                    triggered_by="conversation_coordinator",
                )
                await self.project_service.log_event(
                    project_id=project.id,
                    level="INFO",
                    message="Routed new feedback before Agent execution",
                    meta={
                        "skipped_task_id": task.id,
                        "from_state": state,
                        "target_stage": route_target,
                    },
                )
                await self._execute_agent(
                    self.project_service.require_project(project.id), state_machine
                )
                return
        runtime_env = self._runtime_env_for_project(
            project.id,
            agent_type=agent_type,
            allowed_capability_fact_ids={
                str(fact.get("fact_id"))
                for fact in relevant_facts
                if fact.get("type") == "capability" and fact.get("fact_id")
            },
        )
        granted_capabilities = self._runtime_capability_grants_for(
            project, agent_type, state
        )
        effective_runtime_capabilities = sorted(
            set(granted_capabilities)
            | {
                capability
                for capability in ("HostAutomation", "SandboxWrite")
                if capability in self.prompt_renderer.get_allowed_tools(agent_type)
            }
        )
        configured_capabilities = self._available_runtime_capability_contracts(
            project, agent_type, state
        )
        if runtime_env or configured_capabilities or effective_runtime_capabilities:
            task_input = dict(task.input_data or {})
            contract = dict(task_input.get("work_item_contract") or {})

            if runtime_env:
                contract["available_runtime_capability_env_vars"] = sorted(
                    runtime_env
                )
            if configured_capabilities:
                contract["available_runtime_capabilities"] = configured_capabilities
            if effective_runtime_capabilities:
                contract["effective_runtime_capabilities"] = (
                    effective_runtime_capabilities
                )
            task_input["work_item_contract"] = contract
            task.input_data = task_input
            self.project_service.session.add(task)
            self.project_service.session.commit()

        # Resume the exact working tree when this run continues an answered
        # Agent clarification.  Creating a fresh branch here silently discards
        # every edit made before ask_user and forces the Agent to redo work.
        resumable = self._find_resumable_agent_worktree(project, state, agent_type)
        if resumable is not None:
            previous_task = self.project_service.session.get(
                Task, resumable["task_id"]
            )
            previous_output = (
                previous_task.output_data
                if previous_task is not None
                and isinstance(previous_task.output_data, dict)
                else {}
            )
            task_input = dict(task.input_data or {})
            contract = dict(task_input.get("work_item_contract") or {})
            continuation_mode = str(
                resumable.get("mode") or "answered_agent_clarification"
            )
            artifact_revision_changed = bool(
                contract.get("artifact_revision_changed")
            )
            resolved_questions = self._resolved_questions_for_continuation(
                project.id,
                resumable["task_id"],
                artifact_revision_changed=artifact_revision_changed,
                continuation_mode=continuation_mode,
            )
            contract["continuation"] = {
                "mode": continuation_mode,
                "prior_task_id": resumable["task_id"],
                "same_worktree": True,
                "prior_result": self._compact_continuation_output(previous_output),
                "resolved_questions": resolved_questions,
                "instruction": (
                    (
                        "Continue in the same worktree after a Tester-owned quality "
                        "failure. Preserve and inspect every test change already made "
                        "by the prior attempt, fix the reported harness defect, then "
                        "run the revised formal tests. Do not replace repository test "
                        "changes with ad-hoc evidence."
                    )
                    if continuation_mode == "quality_self_retry"
                    else (
                        "Continue in the same worktree, but treat the current "
                        "Requirements, Architecture, Specs, WorkItem scope, and stage "
                        "ownership as authoritative because their artifact revision "
                        "changed after the prior attempt. Earlier task-specific "
                        "Coordinator answers are intentionally omitted; durable facts "
                        "remain available through relevant_facts. Preserve compatible "
                        "edits, reconcile only the current affected acceptance scope, "
                        "and do not repeat unchanged repository discovery or baseline "
                        "verification."
                    )
                    if artifact_revision_changed
                    else (
                        "Continue from the prior result instead of restarting the stage. "
                        "Read and execute every resolved_questions instruction; these are "
                        "the Coordinator's answers to the prior blocker, not optional notes. "
                        "Reuse prior passing evidence when the clarified answer did not "
                        "invalidate it. First verify the resolved blocker, then run only "
                        "checks that were failed, unknown, uncovered for internal quality, "
                        "or logically affected by the answer. Do not repeat repository "
                        "discovery or an unchanged full regression suite."
                    )
                ),
            }
            task_input["work_item_contract"] = contract
            task.input_data = task_input
            self.project_service.session.add(task)
            self.project_service.session.commit()
        try:
            if resumable is not None:
                worktree = self.worktree_manager.adopt_worktree(
                    project.id,
                    resumable["task_id"],
                    task.id,
                    agent_type,
                    Path(resumable["worktree_path"]),
                    workspace_path=project.workspace_path,
                )
                task.input_data = {
                    **(task.input_data or {}),
                    "resumed_from_task_id": resumable["task_id"],
                }
                self.project_service.session.add(task)
                ctx = dict(project.context)
                ctx.pop("_resumable_agent_worktree", None)
                project.context = ctx
                self.project_service.session.commit()
                logger.info(
                    "Resumed clarification worktree %s from %s for %s",
                    worktree,
                    resumable["task_id"],
                    task.id,
                )
            else:
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
        state_context = dict(state_doc.get("context") or {})
        state_context["open_feedback"] = open_feedback
        state_doc = {**state_doc, "context": state_context}
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
            reference_snapshot = self._sync_reference_snapshot(
                project, worktree, agent_type
            )
            if reference_snapshot is not None:
                task_input = dict(task.input_data or {})
                contract = dict(task_input.get("work_item_contract") or {})
                full_reference = dict(contract.get("reference") or {})
                reference_contract_path = (
                    reference_snapshot.parent / "REFERENCE_CONTRACT.json"
                )
                reference_digest = hashlib.sha256(
                    json.dumps(
                        full_reference,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                contract["reference"] = {
                    "mode": full_reference.get("mode", "full"),
                    "contract_path": str(reference_contract_path),
                    "contract_sha256": reference_digest,
                    "decision_keys": sorted(
                        str(key)
                        for key in full_reference
                        if str(key) != "mode"
                    ),
                    "loading_policy": (
                        "Read the manifest first, then load only the contract "
                        "sections and source files needed by the current WorkItem."
                    ),
                }
                contract["reference_sources"] = [{
                    "path": str(reference_snapshot),
                    "manifest_path": str(
                        reference_snapshot.parent / "REFERENCE_MANIFEST.json"
                    ),
                    "contract_path": str(reference_contract_path),
                    "access": "read_only_source_copy",
                    "reuse_policy": "copy_or_adapt_into_delivery_files",
                }]
                task_input["work_item_contract"] = contract
                task.input_data = task_input
                self.project_service.session.add(task)
                self.project_service.session.commit()
        except Exception as e:
            await self.project_service.fail_task(task.id, f"MAC sync failed: {e}")
            await self._handle_agent_failure(project, state, task, f"MAC sync failed: {e}")
            return

        # Render prompts
        try:
            context_files = self._context_files_for_state(project, state)
            if agent_type in {"coder", "tester"}:
                # Writer roles receive authoritative artifacts copied into the
                # task worktree. Supplying the main-workspace paths in the
                # prompt teaches an unrestricted CLI exactly where to bypass
                # the worktree, even when ``--add-dir`` is absent.
                context_files = list(dict.fromkeys(
                    candidate
                    for source in context_files
                    if (candidate := worktree / ".mac" / source.name).exists()
                ))
            output_schema = self.prompt_renderer.get_output_schema_path(agent_type, stage)
            artifact_schema = self.prompt_renderer.get_artifact_schema_path(
                agent_type, stage
            )
            task_prompt = self.prompt_renderer.render_task_prompt(
                agent_type=agent_type,
                stage=stage,
                project=project,
                state=state,
                worktree=worktree,
                context_files=context_files,
                task_id=task.id,
                output_schema_path=str(output_schema),
                artifact_schema_path=str(artifact_schema or output_schema),
                rejection_note=rejection_note,
                open_feedback=open_feedback,
                round_number=round_number,
                # The WorkItem already carries the authoritative, current
                # facts produced from resolved questions. Re-appending the
                # full historical Q/A trail duplicates stale/superseded
                # decisions and can add tens of thousands of prompt chars.
                qa_history=(
                    []
                    if task.input_data.get("work_item_contract")
                    else qa_history
                ),
                work_item_contract=task.input_data.get("work_item_contract"),
                extra_variables={"retry_feedback": retry_feedback},
            )
            system_prompt = self.prompt_renderer.render_system_prompt(
                agent_type, stage, rendered_base_prompt=task_prompt
            )
            allowed_tools = self.prompt_renderer.get_allowed_tools(agent_type)
            for capability in self._runtime_capability_grants_for(
                project, agent_type, state
            ):
                if capability not in allowed_tools:
                    allowed_tools.append(capability)
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Prompt rendering failed: {e}")
            await self._handle_agent_failure(project, state, task, f"Prompt rendering failed: {e}")
            return

        # Execute agent
        preferred_provider = self._loop_alternate_provider(project, agent_type, state)
        runtime_provider = preferred_provider or self.runtime.provider_for(agent_type)
        task.input_data = {**(task.input_data or {}), "runtime_provider": runtime_provider}
        self.project_service.session.add(task)
        self.project_service.session.commit()
        logger.info(
            "Agent %s(%s) started for %s (task=%s provider=%s)",
            agent_type, stage, project.id, task.id, runtime_provider,
        )
        workspace_before = None
        coder_worktree_before = None
        if state in {"CODING", "TESTING"}:
            try:
                workspace_before = self.worktree_manager.workspace_fingerprint(
                    project.workspace_path
                )
                coder_worktree_before = self._coder_worktree_snapshot(worktree)
            except Exception as exc:
                error = f"Workspace isolation preflight failed: {exc}"
                await self.project_service.fail_task(task.id, error)
                await self._handle_agent_failure(project, state, task, error)
                return
        import time as _time
        task.input_data = {
            **(task.input_data or {}),
            "_profiling": {
                "execution_started_at": datetime.utcnow().isoformat(),
                "runtime_provider": runtime_provider,
                "rendered_contract_chars": len(task_prompt),
                "system_prompt_chars": len(system_prompt),
                "runtime_user_prompt_chars": len(
                    self.prompt_renderer.render_runtime_user_prompt(agent_type, stage)
                ),
            },
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()
        _agent_start = _time.monotonic()
        max_tool_calls = self._max_tool_calls_for_attempt(
            agent_type,
            retry_count=retry_count,
            retry_feedback=retry_feedback,
        )
        try:
            runtime_kwargs: dict[str, Any] = {}
            if preferred_provider:
                runtime_kwargs["preferred_provider"] = preferred_provider
            result = await self.runtime.execute(
                agent_type=agent_type,
                task_prompt=self.prompt_renderer.render_runtime_user_prompt(
                    agent_type, stage
                ),
                system_prompt=system_prompt,
                workspace=Path(project.workspace_path),
                worktree=worktree,
                context_files=context_files,
                output_schema=output_schema,
                allowed_tools=allowed_tools,
                # Coder and Tester both own repository files (implementation
                # and tests respectively) and already run in a complete task
                # worktree. Claude Code's ``--add-dir`` is not read-only in
                # practice: an auto-approved Edit can mutate the main
                # workspace. Never expose it to either writing role.
                readonly_dirs=(
                    []
                    if agent_type in {"coder", "tester"}
                    else [Path(project.workspace_path)]
                ),
                timeout_seconds=self.settings.agent_timeout_seconds,
                max_tool_calls=max_tool_calls,
                task_id=task.id,
                progress_callback=self._make_progress_callback(project, task),
                runtime_env=runtime_env,
                **runtime_kwargs,
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
        fallback_provider = None
        first_log_line = str(result.logs or "").splitlines()[0] if result.logs else ""
        if " fallback=" in first_log_line:
            fallback_provider = first_log_line.split(" fallback=", 1)[1].split()[0]
        logger.info(
            "Agent %s completed for %s in %.1fs (success=%s)",
            agent_type, project.id, _elapsed, result.success,
        )
        task.input_data = {
            **(task.input_data or {}),
            "_profiling": {
                **((task.input_data or {}).get("_profiling") or {}),
                "tool_call_count": len(result.tool_calls or []),
                "fallback_provider": fallback_provider,
                "runtime_success": bool(result.success),
            },
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()

        if workspace_before is not None:
            try:
                workspace_after = self.worktree_manager.workspace_fingerprint(
                    project.workspace_path
                )
            except Exception as exc:
                workspace_error = f"Workspace isolation verification failed: {exc}"
            else:
                workspace_error = None
                if workspace_after["digest"] != workspace_before["digest"]:
                    workspace_error = (
                        f"Workspace isolation violation: {agent_type} changed the main "
                        "workspace while assigned to a task worktree "
                        f"(before={workspace_before['paths']}, "
                        f"after={workspace_after['paths']}). The worktree and "
                        "workspace were preserved for recovery."
                    )
            if workspace_error:
                await self.project_service.fail_task(task.id, workspace_error)
                await self._handle_agent_failure(project, state, task, workspace_error)
                return

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
                    "agent_type": agent_type,
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
            recovered_planner_output = self._recover_planner_turn_from_artifact(
                project=project,
                worktree=worktree,
                state=state,
                task=task,
                runtime_logs=result.logs,
            )
            if recovered_planner_output is not None:
                result.success = True
                result.output_json = recovered_planner_output
                logger.info(
                    "Recovered planner completion from schema-valid artifact; "
                    "task_id=%s state=%s",
                    task.id,
                    state,
                )

        if not result.success or not result.output_json:
            error = _concise_agent_failure(result.logs)
            failure_metadata = (
                dict(result.failure)
                if isinstance(result.failure, dict)
                else None
            )
            await self.project_service.fail_task(
                task.id,
                error,
                evidence={
                    "runtime_success": result.success,
                    "runtime_logs": _concise_agent_failure(result.logs),
                    "tool_calls": result.tool_calls[-12:],
                    **(
                        {"failure": failure_metadata}
                        if failure_metadata
                        else {}
                    ),
                },
                suggested_resolver=str(
                    (failure_metadata or {}).get(
                        "suggested_resolver", "agent"
                    )
                ),
            )
            await self._handle_agent_failure(project, state, task, error)
            return

        result.output_json = redact_secrets(result.output_json)
        self._canonicalize_feedback_references(project.id, result.output_json)

        quality_normalization = self._normalize_quality_gate_output(
            project,
            result.output_json,
            runtime_tool_calls=result.tool_calls,
        )
        if quality_normalization:
            await self.project_service.log_event(
                project_id=project.id,
                level="WARNING",
                message="Normalized inconsistent quality verdict from structured evidence",
                meta={
                    "task_id": task.id,
                    "state": state,
                    "reason": quality_normalization,
                },
            )

        try:
            self.worktree_manager.validate_worktree_identity(
                project.id,
                task.id,
                task.agent_type,
                worktree,
                workspace_path=project.workspace_path,
            )
        except Exception as exc:
            identity_error = f"Worktree integrity failure: {exc}"
            await self.project_service.fail_task(task.id, identity_error)
            await self._handle_agent_failure(
                project, state, task, identity_error
            )
            return

        worktree_contract_error = None
        if self.runtime.materializes_worktree_changes:
            worktree_contract_error = self._coder_worktree_completion_error(
                state,
                worktree,
                result.output_json,
                baseline_snapshot=coder_worktree_before,
                allow_inherited_changes=bool(
                    (task.input_data or {}).get("resumed_from_task_id")
                ),
            )
        if worktree_contract_error:
            await self.project_service.fail_task(
                task.id,
                worktree_contract_error,
                evidence={
                    "agent_output": result.output_json,
                    "worktree_path": str(worktree),
                },
            )
            await self._handle_agent_failure(
                project, state, task, worktree_contract_error
            )
            return

        contract_error = self._agent_completion_error(
            project,
            state_machine,
            task,
            result.output_json,
            runtime_tool_calls=result.tool_calls,
        )
        if contract_error:
            await self.project_service.fail_task(
                task.id,
                contract_error,
                evidence={
                    "agent_output": result.output_json,
                    "tool_calls": result.tool_calls[-12:],
                },
            )
            await self._handle_agent_failure(project, state, task, contract_error)
            return

        freshness = self.project_service.assess_task_freshness(task.id)
        if not freshness["fresh"]:
            logger.warning(
                "Task %s completed against stale project context: %s",
                task.id,
                freshness["reasons"],
            )
            await self.project_service.log_event(
                project_id=project.id,
                level="WARNING",
                message=f"Task completed against stale context: {task.id}",
                meta={"task_id": task.id, **freshness},
            )
            await self._discard_stale_task_result(
                project,
                state_machine,
                task,
                worktree,
                result.output_json,
                freshness,
            )
            return

        # A correction or scope change received while this task was running is
        # already the missing context.  It invalidates this result, but must not
        # be converted into another question for the user.
        blocking_targets = await self._consume_pending_user_inputs(
            project, task, state, result.output_json
        )
        if blocking_targets is not None:
            await self._rerun_stale_agent_result(
                project,
                state_machine,
                task,
                worktree,
                result.output_json,
                "blocking user input received while Agent was running",
                route_target_stage=self._select_rework_target(
                    state_machine, blocking_targets
                ),
            )
            return

        self._persist_stage_output(project, worktree, result.output_json, state)

        # The transition gate above has now validated the runtime result,
        # worktree identity, WorkItem semantics, context freshness and pending
        # user-input boundary.  Persist a compact audit beside the result and
        # emit it before any state transition so operators can review every
        # round's actual input contract and output decision as it happens.
        round_audit = self._round_audit_meta(
            task,
            verdict="accepted",
            output=result.output_json,
        )
        result.output_json["_round_audit"] = round_audit
        logger.info(
            "Agent round audit accepted: %s",
            json.dumps(round_audit, ensure_ascii=False, sort_keys=True),
        )
        await self.project_service.log_event(
            project_id=project.id,
            level="INFO",
            message="Agent round audit accepted",
            meta=round_audit,
        )

        # Complete task
        await self.project_service.complete_task(
            task.id,
            {**result.output_json, "_context_freshness": freshness},
            result.cost_info,
        )
        await self._finalize_progress_card(project, task, succeeded=True)

        # Handle agent output
        await self._handle_agent_output(
            project,
            state_machine,
            task,
            worktree,
            result.output_json,
            runtime_tool_calls=result.tool_calls,
        )

        # In night/auto modes, quality-gate confirmations do not block the
        # next tick.  Resolve them immediately while still emitting report cards.
        await self._auto_resolve_quality_gates(project, state_machine)
        self._schedule_followup_tick(project.id)

    @staticmethod
    def _reference_source_root(project: Project) -> Path | None:
        """Return a validated full-reference repository selected by Coordinator."""
        brief = (project.context or {}).get("confirmed_brief")
        if not isinstance(brief, dict) or brief.get("reference_mode") != "full":
            return None
        decision = next(
            (
                item
                for item in brief.get("decisions") or []
                if isinstance(item, dict)
                and item.get("key") == "reference.source_repo"
                and item.get("status", "confirmed") == "confirmed"
            ),
            None,
        )
        details = decision.get("details") if isinstance(decision, dict) else None
        source_text = (
            str(details.get("source_path") or "").strip()
            if isinstance(details, dict)
            else ""
        )
        if not source_text:
            return None
        source = Path(source_text)
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            return None
        allowed_roots = get_allowed_bind_roots()
        if not resolved.is_dir() or (
            allowed_roots
            and not any(
                resolved == root.resolve() or root.resolve() in resolved.parents
                for root in allowed_roots
            )
        ):
            return None
        return resolved

    @staticmethod
    def _reference_contract_from_brief(project: Project) -> dict[str, Any]:
        """Compile the immutable user/Coordinator reference decisions."""
        brief = (project.context or {}).get("confirmed_brief")
        if not isinstance(brief, dict) or brief.get("reference_mode") != "full":
            return {}
        reference: dict[str, Any] = {"mode": "full"}
        for decision in brief.get("decisions") or []:
            if not isinstance(decision, dict) or decision.get("status") != "confirmed":
                continue
            key = str(decision.get("key") or "")
            if not key.startswith("reference."):
                continue
            reference[key.removeprefix("reference.")] = dict(
                decision.get("details") or {}
            )
        return reference

    @classmethod
    def _reference_contract_for_project(cls, project: Project) -> dict[str, Any]:
        """Return the current durable reference contract for a WorkItem.

        The confirmed brief establishes the immutable source and user choices.
        Requirements may compile a large evidence inventory into a separate
        versioned artifact so it can be loaded on demand instead of repeated
        in every prompt.  Never let that derived artifact change the source
        repository selected by the user/Coordinator.
        """
        base = cls._reference_contract_from_brief(project)
        if not base:
            return {}
        try:
            artifact_path = project_mac_dir(project) / "REFERENCE_CONTRACT.json"
        except (AttributeError, TypeError):
            return base
        try:
            compiled = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return base
        if not isinstance(compiled, dict) or compiled.get("mode") != "full":
            return base
        if compiled.get("source_repo") != base.get("source_repo"):
            return base
        return compiled

    @classmethod
    def _reference_contract_artifact_error(
        cls, project: Project, worktree: Path
    ) -> str | None:
        """Validate the secondary full-reference planner artifact.

        This is deliberately structural and provenance-based.  Capability
        categories and product vocabulary remain open-ended; routing never
        depends on names or keywords.
        """
        base = cls._reference_contract_from_brief(project)
        if not base:
            return None
        path = worktree / ".mac" / "reference" / "REFERENCE_CONTRACT.json"
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Reference contract artifact is missing or invalid JSON: {exc}"
        if not isinstance(contract, dict) or contract.get("mode") != "full":
            return "Reference contract artifact must be a full-mode JSON object"
        if contract.get("source_repo") != base.get("source_repo"):
            return "Reference contract artifact changed the confirmed source repository"

        inventory = contract.get("capability_inventory")
        capabilities = (
            inventory.get("capabilities") if isinstance(inventory, dict) else None
        )
        if not isinstance(capabilities, list) or not capabilities:
            return (
                "Reference contract capability_inventory.capabilities must be "
                "a non-empty array"
            )
        manifest_path = worktree / ".mac" / "reference" / "REFERENCE_MANIFEST.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Reference manifest is missing or invalid JSON: {exc}"
        manifest_files = {
            str(item.get("path"))
            for item in manifest.get("files") or []
            if isinstance(item, dict) and item.get("path")
        }
        try:
            requirements_doc = json.loads(
                (worktree / ".mac" / "REQUIREMENTS.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            requirements_doc = {}
        active_criterion_ids = {
            str(item.get("id"))
            for item in requirements_doc.get("acceptance_criteria") or []
            if isinstance(item, dict) and item.get("id")
        }

        def manifest_contains(path_text: str, *, allow_directory: bool) -> bool:
            normalized = Path(path_text).as_posix().strip("/")
            if not normalized or normalized == "." or ".." in Path(normalized).parts:
                return False
            if normalized in manifest_files:
                return True
            prefix = normalized.rstrip("/") + "/"
            return allow_directory and any(
                candidate.startswith(prefix) for candidate in manifest_files
            )

        capability_ids: set[str] = set()
        in_scope_visible: set[str] = set()
        allowed_scopes = {"in_scope", "future_iteration", "carrier_excluded"}
        allowed_reuse = {"copy", "adapt", "reimplement", "exclude", "external_runtime"}
        required_fields = (
            "id",
            "name",
            "category",
            "observable_behavior",
            "reuse_policy",
        )
        for index, capability in enumerate(capabilities):
            if not isinstance(capability, dict):
                return f"Reference capability[{index}] must be an object"
            missing = [
                field for field in required_fields
                if not str(capability.get(field) or "").strip()
            ]
            if missing:
                return f"Reference capability[{index}] missing fields: {', '.join(missing)}"
            capability_id = str(capability["id"]).strip()
            if capability_id in capability_ids:
                return f"Reference contract has duplicate capability id: {capability_id}"
            capability_ids.add(capability_id)
            scope = str(capability.get("scope") or "in_scope")
            if scope not in allowed_scopes:
                return f"Reference capability[{index}] has invalid scope: {scope}"
            reuse_policy = str(capability.get("reuse_policy") or "")
            if reuse_policy not in allowed_reuse:
                return (
                    f"Reference capability[{index}] has invalid reuse_policy: "
                    f"{reuse_policy}"
                )
            source_files = capability.get("source_files")
            if not isinstance(source_files, list) or not source_files:
                return f"Reference capability[{index}].source_files must not be empty"
            invalid_sources = [
                str(item)
                for item in source_files
                if not manifest_contains(str(item), allow_directory=True)
            ]
            if invalid_sources:
                return (
                    f"Reference capability[{index}] cites files absent from manifest: "
                    + ", ".join(invalid_sources)
                )
            if scope == "in_scope" and capability.get("user_visible") is not False:
                in_scope_visible.add(capability_id)
                mapped_criteria = {
                    str(value)
                    for value in capability.get("acceptance_criteria") or []
                    if value
                }
                if not mapped_criteria:
                    return (
                        f"Reference capability[{index}].acceptance_criteria "
                        "must map an in-scope capability to the current contract"
                    )
                if active_criterion_ids and mapped_criteria.isdisjoint(
                    active_criterion_ids
                ):
                    return (
                        f"Reference capability[{index}] is not mapped to a current "
                        "active acceptance criterion"
                    )

        probes_section = contract.get("behavioral_probes")
        probes = (
            probes_section.get("probes")
            if isinstance(probes_section, dict)
            else None
        )
        if not isinstance(probes, list) or not probes:
            return "Reference contract behavioral_probes.probes must be a non-empty array"
        covered: set[str] = set()
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict):
                return f"Reference probe[{index}] must be an object"
            missing = [
                field
                for field in (
                    "capability_id",
                    "input",
                    "expected_output",
                    "acceptance_criterion",
                    "source_file",
                    "source_quote",
                )
                if not str(probe.get(field) or "").strip()
            ]
            if missing:
                return f"Reference probe[{index}] missing fields: {', '.join(missing)}"
            capability_id = str(probe["capability_id"]).strip()
            if capability_id not in capability_ids:
                return (
                    f"Reference probe[{index}].capability_id is not in the inventory: "
                    f"{capability_id}"
                )
            covered.add(capability_id)
            if not manifest_contains(
                str(probe["source_file"]), allow_directory=False
            ):
                return f"Reference probe[{index}].source_file is absent from manifest"
        missing_probes = sorted(in_scope_visible - covered)
        if missing_probes:
            return (
                "Reference contract is missing probes for in-scope user-visible "
                "capabilities: " + ", ".join(missing_probes)
            )
        return None

    @classmethod
    def _planning_confirmation_integrity_error(
        cls, project: Project, state: str
    ) -> str | None:
        """Reject a confirmation boundary with missing authoritative outputs."""
        if state != "REQUIREMENT_CONFIRM_PENDING":
            return None
        if not cls._reference_contract_from_brief(project):
            return None
        path = project_mac_dir(project) / "REFERENCE_CONTRACT.json"
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return (
                "The confirmed full-reference Requirements stage is missing its "
                "durable REFERENCE_CONTRACT.json companion artifact."
            )
        inventory = contract.get("capability_inventory")
        capabilities = (
            inventory.get("capabilities") if isinstance(inventory, dict) else None
        )
        probes_section = contract.get("behavioral_probes")
        probes = (
            probes_section.get("probes")
            if isinstance(probes_section, dict)
            else None
        )
        if not isinstance(capabilities, list) or not capabilities:
            return (
                "The durable full-reference contract has no canonical "
                "capability_inventory.capabilities."
            )
        if not isinstance(probes, list) or not probes:
            return (
                "The durable full-reference contract has no canonical "
                "behavioral_probes.probes."
            )
        return None

    @classmethod
    def _sync_reference_snapshot(
        cls,
        project: Project,
        worktree: Path,
        agent_type: str,
    ) -> Path | None:
        """Copy an authorized reference into the task boundary for safe reuse.

        The original repository is never exposed as a writer root. Agents may
        inspect this snapshot dynamically and copy or adapt useful files into
        delivery paths without bloating the prompt with the whole repository.
        """
        if agent_type not in {
            "requirements_analyst",
            "architect",
            "spec_writer",
            "coder",
            "tester",
            "reviewer",
            "acceptance_agent",
        }:
            return None
        source = cls._reference_source_root(project)
        if source is None:
            return None
        destination = worktree / ".mac" / "reference" / "source"
        try:
            destination.resolve().relative_to(source)
        except ValueError:
            pass
        else:
            # Never recursively copy a repository into one of its own task
            # directories, even if a malformed contract names an ancestor.
            return None
        excluded_dirs = {
            ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "dist", "build",
        }
        generated_asset_dirs = {
            "cache", "logs", "screenshots", "models", "debug", "exports",
            "reports", "review_drafts", "private_benchmarks", "vector_indexes",
        }
        reusable_extensions = {
            ".c", ".cc", ".cpp", ".css", ".csv", ".db", ".go", ".h",
            ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".jsx",
            ".md", ".py", ".rs", ".sh", ".sql", ".sqlite", ".toml",
            ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
        }
        max_asset_bytes = 16 * 1024 * 1024
        max_snapshot_bytes = 512 * 1024 * 1024

        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in excluded_dirs}
            ignored.update(
                name
                for name in names
                if name.startswith(".env") and name != ".env.example"
            )
            ignored.update(name for name in names if name.endswith(".pyc"))
            ignored.update(
                name for name in names if Path(directory, name).is_symlink()
            )
            return ignored

        destination.parent.mkdir(parents=True, exist_ok=True)
        tracked_relatives: set[Path] = set()
        if (source / ".git").exists():
            completed = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=source,
                capture_output=True,
                check=False,
            )
            if completed.returncode == 0:
                tracked_relatives = {
                    Path(os.fsdecode(raw))
                    for raw in completed.stdout.split(b"\0")
                    if raw
                }

        # A complete-reference repository can legitimately keep private local
        # knowledge, fixtures and few-shot corpora outside git.  A tracked-only
        # snapshot silently erases those capabilities.  Inventory every safe
        # regular file, while materializing a bounded, deterministic subset of
        # reusable source/text/data assets into the isolated task worktree.
        inventory: list[tuple[Path, int, bool]] = []
        for root, dirnames, filenames in os.walk(source):
            dirnames[:] = sorted(name for name in dirnames if name not in excluded_dirs)
            for filename in sorted(filenames):
                candidate = Path(root) / filename
                try:
                    relative = candidate.resolve(strict=True).relative_to(source)
                    size = candidate.stat().st_size
                except (OSError, ValueError):
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                lowered = relative.name.lower()
                if (
                    (lowered.startswith(".env") and lowered != ".env.example")
                    or lowered in {"id_rsa", "id_ed25519"}
                    or relative.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
                    or any(token in lowered for token in ("secret", "credential", "api_key"))
                ):
                    continue
                inventory.append((relative, size, relative in tracked_relatives))

        destination.mkdir(parents=True, exist_ok=True)
        copied_paths: list[str] = []
        copied_bytes = 0
        manifest_files: list[dict[str, Any]] = []
        for relative, size, tracked in sorted(
            inventory,
            key=lambda item: (
                0 if item[2] else 1,
                0 if item[0].suffix.lower() in reusable_extensions else 1,
                item[0].as_posix(),
            ),
        ):
            generated = any(part.lower() in generated_asset_dirs for part in relative.parts)
            reusable = tracked or relative.name == ".env.example" or (
                not generated
                and relative.suffix.lower() in reusable_extensions
                and size <= max_asset_bytes
            )
            copied = reusable and copied_bytes + size <= max_snapshot_bytes
            if copied:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
                copied_paths.append(relative.as_posix())
                copied_bytes += size
            manifest_files.append({
                "path": relative.as_posix(),
                "size_bytes": size,
                "git_tracked": tracked,
                "copied_to_snapshot": copied,
            })
        (destination.parent / "REFERENCE_MANIFEST.json").write_text(
            json.dumps(
                {
                    "source_repository": str(source),
                    "snapshot_policy": "bounded_safe_repository_inventory",
                    "copy_budget_bytes": max_snapshot_bytes,
                    "copied_bytes": copied_bytes,
                    "copied_paths": copied_paths,
                    "files": manifest_files,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        # Keep the complete Coordinator-confirmed reference contract available
        # as a versioned, inspectable file.  The WorkItem prompt will carry a
        # digest and path instead of repeating large inventories, source facts
        # and behavioral probes on every Agent turn.
        reference_contract = cls._reference_contract_for_project(project)
        (destination.parent / "REFERENCE_CONTRACT.json").write_text(
            json.dumps(reference_contract, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return destination.resolve()


    @staticmethod
    def _coder_worktree_snapshot(worktree: Path) -> dict[str, str]:
        """Return content fingerprints for every project-visible worktree diff."""

        def _git(*args: str) -> str:
            completed = subprocess.run(
                ["git", *args],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "git command failed")
            return completed.stdout

        base = _git("merge-base", "HEAD", "main").strip()
        paths = {
            line.strip()
            for line in _git("diff", "--name-only", f"{base}..HEAD").splitlines()
            if line.strip()
        }
        status = _git("status", "--porcelain=v1", "--untracked-files=all")
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.add(path.strip('"'))

        snapshot: dict[str, str] = {}
        for path in paths:
            if path == ".mac" or path.startswith(".mac/"):
                continue
            candidate = worktree / path
            digest = hashlib.sha256()
            if candidate.is_symlink():
                digest.update(f"symlink:{candidate.readlink()}".encode())
            elif candidate.is_file():
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif not candidate.exists():
                digest.update(b"<deleted>")
            else:
                digest.update(b"<non-file>")
            snapshot[path] = digest.hexdigest()
        return snapshot

    @classmethod
    def _coder_worktree_completion_error(
        cls,
        state: str,
        worktree: Path,
        output: dict[str, Any],
        *,
        baseline_snapshot: dict[str, str] | None = None,
        allow_inherited_changes: bool = False,
    ) -> str | None:
        """Require Coder's report to match changes made by this task attempt."""
        if state != "CODING" or output.get("next_state_event") != "complete":
            return None
        try:
            current_snapshot = cls._coder_worktree_snapshot(worktree)
        except Exception as exc:
            return f"Agent contract violation: cannot inspect coder worktree diff: {exc}"
        actual = set(current_snapshot)
        if baseline_snapshot is None or allow_inherited_changes:
            task_actual = actual
        else:
            changed_since_start = {
                path
                for path in set(baseline_snapshot) | actual
                if baseline_snapshot.get(path) != current_snapshot.get(path)
            }
            # A task that reverts an older dirty change leaves no deliverable
            # diff for that path and should not be forced to report one.
            task_actual = changed_since_start & actual
        generated_artifacts = sorted(
            path
            for path in actual
            if path.startswith(("dist/", "build/", ".pytest_cache/"))
            or "/__pycache__/" in f"/{path}"
            or path.endswith((".whl", ".pyc"))
            or ".egg-info/" in f"/{path}"
        )
        if generated_artifacts:
            return (
                "Agent contract violation: generated build/test artifacts must be "
                "cleaned before completion and must not be reported as source changes: "
                f"{generated_artifacts}"
            )
        fixed_feedback_ids = sorted(
            str(item.get("feedback_id"))
            for item in (output.get("addressed_feedback") or [])
            if isinstance(item, dict)
            and item.get("feedback_id")
            and item.get("status") == "fixed"
        )
        if fixed_feedback_ids and not task_actual:
            # A requirements/spec replay can carry still-open feedback whose code
            # fix was merged in an earlier coding round. Models often call that
            # feedback ``fixed`` again even though this task correctly has no diff.
            # Normalize the claim instead of encouraging a synthetic edit. This
            # does not close the feedback: Tester must independently verify it.
            for item in output.get("addressed_feedback") or []:
                if (
                    isinstance(item, dict)
                    and str(item.get("feedback_id")) in fixed_feedback_ids
                    and item.get("status") == "fixed"
                ):
                    item["status"] = "not_applicable"
        declared_bulk_directories: set[str] = set()
        specs_path = worktree / ".mac" / "SPECS.json"
        try:
            specs_doc = json.loads(specs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            specs_doc = {}
        for spec in specs_doc.get("specs") or []:
            if not isinstance(spec, dict):
                continue
            for raw_path in spec.get("affected_files") or []:
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                path = Path(raw_path)
                if path.is_absolute() or ".." in path.parts:
                    continue
                declared_bulk_directories.add(path.as_posix().rstrip("/"))

        claimed: set[str] = set()
        claimed_directories: dict[str, str] = {}
        for item in output.get("modified_files") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = Path(str(item["path"]))
            if path.is_absolute() or ".." in path.parts:
                return (
                    "Agent contract violation: modified_files must contain only "
                    f"worktree-relative paths: {item['path']}"
                )
            normalized = path.as_posix().rstrip("/")
            if item.get("kind") == "directory":
                manifest_raw = str(item.get("manifest_path") or "").strip()
                manifest = Path(manifest_raw)
                if (
                    not normalized
                    or normalized == "."
                    or normalized not in declared_bulk_directories
                    or not (worktree / normalized).is_dir()
                    or not manifest_raw
                    or manifest.is_absolute()
                    or ".." in manifest.parts
                    or not (worktree / manifest).is_file()
                ):
                    return (
                        "Agent contract violation: directory modified_files entries "
                        "must name an existing SPECS.json affected_files directory "
                        "and an existing worktree-relative manifest_path: "
                        f"{item}"
                    )
                claimed_directories[normalized] = manifest.as_posix()
                claimed.add(manifest.as_posix())
            else:
                # ``Path`` already normalizes an explicit ``./`` prefix.  Do not use
                # ``lstrip("./")`` here: it treats the argument as a character set
                # and corrupts legitimate dotfiles such as ``.gitignore``.
                claimed.add(normalized)

        covered = set(claimed)
        empty_directories: list[str] = []
        for directory, manifest_path in claimed_directories.items():
            prefix = directory + "/"
            descendants = {path for path in task_actual if path.startswith(prefix)}
            if not descendants or manifest_path not in task_actual:
                empty_directories.append(directory)
            covered.update(descendants)

        if covered != task_actual or empty_directories:
            missing = sorted(task_actual - covered)
            nonexistent = sorted((claimed - task_actual) | set(empty_directories))
            return (
                "Agent contract violation: modified_files do not match the real "
                f"task worktree diff (unreported={missing}, not_in_worktree={nonexistent})"
            )
        return None

    async def _recover_missing_coordinator_rejection_feedback(
        self, project: Project, execution_state: str
    ) -> None:
        """Backfill a rejected approval reason before its owner reruns.

        This also closes the crash window between recording the Coordinator
        task and creating its FeedbackPacket, and migrates projects that were
        already looping under the pre-v2 automatic approval path.
        """
        confirmation_state, target = {
            "PLANNING_REQUIREMENTS": (
                "REQUIREMENT_CONFIRM_PENDING",
                "requirements",
            ),
            "PLANNING_ARCHITECTURE": (
                "ARCHITECTURE_CONFIRM_PENDING",
                "architecture",
            ),
            "PLANNING_IMPLEMENTATION_SPECS": (
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
                "specs",
            ),
            "BUG_ANALYSIS": ("BUG_FIX_CONFIRM_PENDING", "requirements"),
            "DOC_OUTLINE": ("OUTLINE_CONFIRM_PENDING", "requirements"),
        }.get(execution_state, (None, None))
        if confirmation_state is None or target is None:
            return
        coordinator_task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "conversation_coordinator",
                Task.state == confirmation_state,
                Task.status == "completed",
            )
            .order_by(Task.created_at.desc())
        ).first()
        if coordinator_task is None:
            return
        output = coordinator_task.output_data or {}
        if not str(output.get("action") or "").startswith("reject"):
            return
        existing = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.source_task_id == coordinator_task.id,
            )
        ).first()
        reason = str(output.get("reason") or "").strip()
        if existing is not None or not reason:
            return
        rounds = project.context.get("confirmation_rounds", {})
        await self.project_service.create_feedback_packet(
            project_id=project.id,
            source_stage=confirmation_state,
            source_task_id=coordinator_task.id,
            source_round=int(rounds.get(confirmation_state, 0) or 0),
            kind="coordinator_rejection",
            summary=reason,
            blocking=True,
            failure_class="contract_conflict",
            recommended_rework_target=target,
            evidence=[
                {"ref": str(ref)}
                for ref in (output.get("evidence_refs") or [])
                if ref
            ],
        )

    def _loop_alternate_provider(
        self, project: Project, agent_type: str, execution_state: str
    ) -> str | None:
        """Change provider after a proven no-progress stage loop."""
        loop = project.context.get("loop_supervisor") or {}
        state_target = {
            "PLANNING_REQUIREMENTS": "requirements",
            "PLANNING_ARCHITECTURE": "architecture",
            "PLANNING_IMPLEMENTATION_SPECS": "specs",
            "CODING": "coding",
            "TESTING": "testing",
        }.get(execution_state)
        if not state_target or loop.get("prohibited_default_target") != state_target:
            return None
        providers = getattr(self.runtime, "providers", None)
        if not isinstance(providers, dict):
            return None
        current = self.runtime.provider_for(agent_type)
        for candidate in ("codex", "claude_code", "pi", "kimi_code"):
            if candidate != current and candidate in providers:
                return candidate
        return None

    async def _discard_stale_task_result(
        self,
        project: Project,
        state_machine: StateMachine,
        task: Task,
        worktree: Path,
        output: dict[str, Any],
        freshness: dict[str, Any],
    ) -> None:
        """Persist a stale result for audit without applying its side effects."""
        stale_output = dict(output)
        blocking_targets = await self._consume_pending_user_inputs(
            project, task, task.state, stale_output
        )
        reason_text = "; ".join(freshness.get("reasons") or ["context changed"])
        stale_output["_context_freshness"] = freshness
        stale_output.setdefault("notes", []).append(
            f"任务上下文已过期，结果未应用并自动重跑：{reason_text}"
        )
        await self._rerun_stale_agent_result(
            project,
            state_machine,
            task,
            worktree,
            stale_output,
            reason_text,
            route_target_stage=self._select_rework_target(
                state_machine, blocking_targets
            ),
        )

    async def _rerun_stale_agent_result(
        self,
        project: Project,
        state_machine: StateMachine,
        task: Task,
        worktree: Path,
        output: dict[str, Any],
        reason: str,
        route_target_stage: str | None = None,
    ) -> None:
        """Discard an obsolete result and rerun the same Agent state."""
        await self.project_service.mark_task_stale(task.id, dict(output), reason)
        try:
            self.worktree_manager.remove_worktree(
                project.id,
                task.id,
                task.agent_type,
                worktree,
                workspace_path=project.workspace_path,
            )
        except Exception as exc:
            logger.warning("Failed to remove stale worktree %s: %s", worktree, exc)
        await self.project_service.log_event(
            project_id=project.id,
            level="INFO",
            message=f"Automatically rerunning stale Agent task: {task.id}",
            meta={"task_id": task.id, "state": task.state, "reason": reason},
        )
        refreshed = self.project_service.require_project(project.id)
        if refreshed.current_state == task.state:
            if route_target_stage:
                route_event = state_machine.route_event_to_stage(
                    refreshed.current_state, route_target_stage
                )
                if route_event:
                    await self.project_service.transition_state(
                        refreshed.id,
                        route_event,
                        triggered_by="conversation_coordinator",
                    )
                    refreshed = self.project_service.require_project(project.id)
            await self._execute_agent(refreshed, state_machine)

    @staticmethod
    def _select_rework_target(
        state_machine: StateMachine,
        targets: set[str] | None,
    ) -> str | None:
        """Choose the earliest valid owner when several corrections race."""
        if not targets:
            return None
        stage_order = [
            str(stage.get("id") or "")
            for stage in state_machine.profile.get("stages", [])
        ]
        return next((stage for stage in stage_order if stage in targets), None)

    async def _recover_unapplied_completed_task(
        self, project: Project, state_machine: StateMachine
    ) -> bool:
        """Apply a task result committed just before an orchestrator crash.

        A result is recoverable only while the project is still in the task's
        original state and the task is newer than the project. Normal completed
        transitions update the project timestamp, so they are never replayed.
        """
        expected_agent = state_machine.agent_for(project.current_state)
        if not expected_agent:
            return False
        task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.state == project.current_state,
                Task.agent_type == expected_agent,
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        if task is None or not task.output_data:
            return False
        if task.updated_at <= project.updated_at:
            return False
        output = dict(task.output_data)
        if "next_state_event" not in output:
            logger.warning(
                "Ignoring completed task %s for %s: %s output does not satisfy "
                "the stage-result contract",
                task.id,
                project.id,
                expected_agent,
            )
            return False
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
        *,
        stage: str | None = None,
        extra_variables: dict[str, str] | None = None,
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
        # Auxiliary agents can run while the state machine remains at a human
        # boundary (notably acceptance evaluation and Coordinator mediation).
        # Surface the actual execution posture instead of claiming that the
        # project is already waiting for the user while an Agent is working.
        self.project_service.set_execution_status(project.id, "running")

        is_readonly_aux = agent_type in (
            "project_manager",
            "acceptance_agent",
            "acceptance_evaluator",
            "conversation_coordinator",
        )
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
            return await self._run_auxiliary_agent(
                project,
                task,
                worktree,
                input_data,
                context_files,
                stage=stage,
                extra_variables=extra_variables,
            )
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
        *,
        stage: str | None = None,
        extra_variables: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Render prompts, run the runtime, and complete or fail the task."""
        effective_context_files = (
            context_files
            if context_files is not None
            else self._context_files_for_state(project, project.current_state)
        )
        preferred_provider = str(input_data.get("preferred_provider") or "").strip()
        available_providers = self.runtime.available_providers()
        runtime_provider = (
            preferred_provider
            if preferred_provider in available_providers
            else self.runtime.provider_for(task.agent_type)
        )
        task.input_data = {
            **(task.input_data or {}),
            "runtime_provider": runtime_provider,
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()
        try:
            output_schema = self.prompt_renderer.get_output_schema_path(task.agent_type, stage)
            artifact_schema = self.prompt_renderer.get_artifact_schema_path(
                task.agent_type, stage
            )
            task_prompt = self.prompt_renderer.render_task_prompt(
                agent_type=task.agent_type,
                stage=stage,
                project=project,
                state=project.current_state,
                worktree=worktree,
                context_files=effective_context_files,
                task_id=task.id,
                output_schema_path=str(output_schema),
                artifact_schema_path=str(artifact_schema or output_schema),
                qa_history=(
                    []
                    if task.input_data.get("work_item_contract")
                    else self._collect_qa_history(project.id)
                ),
                work_item_contract=task.input_data.get("work_item_contract"),
                extra_variables=extra_variables,
            )
            system_prompt = self.prompt_renderer.render_system_prompt(
                task.agent_type, stage, rendered_base_prompt=task_prompt
            )
            allowed_tools = self.prompt_renderer.get_allowed_tools(task.agent_type, stage)
            for capability in self._runtime_capability_grants_for(
                project, task.agent_type, project.current_state
            ):
                if capability not in allowed_tools:
                    allowed_tools.append(capability)
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Prompt rendering failed: {e}")
            return None

        task.input_data = {
            **(task.input_data or {}),
            "_profiling": {
                "execution_started_at": datetime.utcnow().isoformat(),
                "runtime_provider": runtime_provider,
                "rendered_contract_chars": len(task_prompt),
                "system_prompt_chars": len(system_prompt),
                "runtime_user_prompt_chars": len(
                    self.prompt_renderer.render_runtime_user_prompt(
                        task.agent_type, stage
                    )
                ),
            },
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()
        try:
            result = await self.runtime.execute(
                agent_type=task.agent_type,
                task_prompt=self.prompt_renderer.render_runtime_user_prompt(
                    task.agent_type, stage
                ),
                system_prompt=system_prompt,
                workspace=Path(project.workspace_path),
                worktree=worktree,
                context_files=effective_context_files,
                output_schema=output_schema,
                allowed_tools=allowed_tools,
                readonly_dirs=[Path(project.workspace_path)],
                timeout_seconds=self.settings.agent_timeout_seconds,
                max_tool_calls=self._max_tool_calls_for(task.agent_type),
                task_id=task.id,
                progress_callback=self._make_progress_callback(project, task),
                runtime_env=self._runtime_env_for_project(
                    project.id,
                    agent_type=task.agent_type,
                    allowed_capability_fact_ids={
                        str(fact.get("fact_id"))
                        for fact in (
                            (task.input_data or {})
                            .get("work_item_contract", {})
                            .get("relevant_facts", [])
                            or []
                        )
                        if isinstance(fact, dict)
                        and fact.get("type") == "capability"
                        and fact.get("fact_id")
                    },
                ),
                preferred_provider=(runtime_provider or None),
            )
        except Exception as e:
            await self.project_service.fail_task(task.id, f"Runtime execution failed: {e}")
            return None

        if not result.success or not result.output_json:
            await self.project_service.fail_task(task.id, result.logs or "Agent returned failure")
            return None

        task.input_data = {
            **(task.input_data or {}),
            "_profiling": {
                **((task.input_data or {}).get("_profiling") or {}),
                "tool_call_count": len(result.tool_calls or []),
            },
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()

        acceptance_output_files = {
            "acceptance_agent": "ACCEPTANCE_AGENT_OUTPUT.json",
            "acceptance_evaluator": "ACCEPTANCE_EVALUATOR_OUTPUT.json",
        }
        if task.agent_type in acceptance_output_files:
            # Persist provider-observed execution traces with the structured
            # result.  A later evaluator may audit them, but may never replace
            # a failed executor result with its own unaudited prose.
            result.output_json["_runtime"] = {
                "provider": runtime_provider,
                "tool_calls": list(result.tool_calls or []),
            }
            evidence_path = project_mac_dir(project) / acceptance_output_files[task.agent_type]
            evidence_path.write_text(
                json.dumps(result.output_json, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        completed_task = await self.project_service.complete_task(
            task.id, result.output_json, result.cost_info
        )
        await self._finalize_progress_card(project, task, succeeded=True)
        # Return the same persisted representation that cache hits expose.
        # Profiling metadata is added by complete_task, so returning the raw
        # runtime object made the first call observably differ from a cache hit.
        return dict(completed_task.output_data or {})

    async def run_chat_scoped_agent(
        self,
        agent_type: str,
        stage: str | None,
        input_data: dict[str, Any],
        extra_variables: dict[str, str] | None = None,
        *,
        worktree: Path | None = None,
        readonly_dirs: list[Path] | None = None,
        max_tool_calls: int | None = None,
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
                output_schema = self.prompt_renderer.get_output_schema_path(agent_type, stage)
                artifact_schema = self.prompt_renderer.get_artifact_schema_path(
                    agent_type, stage
                )
                task_prompt = self.prompt_renderer.render_task_prompt(
                    agent_type=agent_type,
                    stage=stage,
                    project=None,
                    state="chat_scoped",
                    worktree=worktree,
                    context_files=[],
                    output_schema_path=str(output_schema),
                    artifact_schema_path=str(artifact_schema or output_schema),
                    extra_variables=extra_variables,
                )
                system_prompt = self.prompt_renderer.render_system_prompt(
                    agent_type, stage, rendered_base_prompt=task_prompt
                )
                allowed_tools = self.prompt_renderer.get_allowed_tools(agent_type, stage)
                if max_tool_calls == 0:
                    # A schema-only retry must not repeat repository scanning.
                    # Hiding tools is stronger and clearer than exposing them
                    # only to terminate the first attempted call.
                    allowed_tools = []
                if (
                    agent_type == "conversation_coordinator"
                    and owns_worktree
                    and not readonly_dirs
                ):
                    # An unbound conversation with no authorized read roots has
                    # no filesystem capability. Tool availability is derived
                    # from explicit system context, never from user wording.
                    allowed_tools = []
            except Exception as e:
                logger.error(
                    "Chat-scoped agent %s/%s prompt rendering failed: %s",
                    agent_type, stage, e,
                )
                return {"success": False, "error": f"Prompt rendering failed: {e}"}

            try:
                result = await self.runtime.execute(
                    agent_type=agent_type,
                    task_prompt=self.prompt_renderer.render_runtime_user_prompt(
                        agent_type, stage
                    ),
                    system_prompt=system_prompt,
                    workspace=worktree,
                    worktree=worktree,
                    context_files=[],
                    output_schema=output_schema,
                    allowed_tools=allowed_tools,
                    readonly_dirs=list(readonly_dirs or [worktree]),
                    timeout_seconds=self.settings.agent_timeout_seconds,
                    max_tool_calls=(
                        max_tool_calls
                        if max_tool_calls is not None
                        else self._max_tool_calls_for(agent_type)
                    ),
                    progress_callback=(
                        progress_callback if self.settings.debug_agent_progress else None
                    ),
                )
            except Exception as e:
                logger.error(
                    "Chat-scoped agent %s/%s runtime failed: %s",
                    agent_type, stage, e,
                )
                return {"success": False, "error": f"Runtime execution failed: {e}"}

            if not result.success or not result.output_json:
                failure_reason = _concise_agent_failure(result.logs)
                logger.warning(
                    "Chat-scoped agent %s/%s returned failure: %s",
                    agent_type, stage, failure_reason,
                )
                return {
                    "success": False,
                    "error": failure_reason,
                    # Preserve read-only evidence even when the model-authored
                    # protocol object is malformed. The bounded repair retry
                    # must not lose verified Reads or regain filesystem tools.
                    "_runtime": {
                        "provider": self.runtime.provider_for(agent_type),
                        "tool_calls": list(result.tool_calls or []),
                    },
                    "result_text": result.raw_result,
                }
            output = dict(result.output_json)
            # Runtime evidence is internal metadata, not model-authored output.
            # The conversation handler uses it to verify that an inspection
            # actually performed a Read before accepting its conclusions.
            output["_runtime"] = {
                "provider": self.runtime.provider_for(agent_type),
                "tool_calls": list(result.tool_calls or []),
            }
            return output
        finally:
            # Best-effort cleanup of the temp dir. Don't raise on failure.
            try:
                if owns_worktree and worktree.exists():
                    import shutil
                    shutil.rmtree(worktree, ignore_errors=True)
            except Exception:
                pass

    async def _choose_action_with_coordinator(
        self,
        project: Project,
        state: str,
        question: str,
        options: list[dict],
    ) -> str | None:
        """Ask the Coordinator to decide an AI-first confirmation boundary."""
        valid_actions = {opt["action"] for opt in options}
        owner_state, owner_target = {
            "REQUIREMENT_CONFIRM_PENDING": ("PLANNING_REQUIREMENTS", "requirements"),
            "ARCHITECTURE_CONFIRM_PENDING": ("PLANNING_ARCHITECTURE", "architecture"),
            "IMPLEMENTATION_SPEC_CONFIRM_PENDING": (
                "PLANNING_IMPLEMENTATION_SPECS",
                "specs",
            ),
            "BUG_FIX_CONFIRM_PENDING": ("BUG_ANALYSIS", "requirements"),
            "OUTLINE_CONFIRM_PENDING": ("DOC_OUTLINE", "requirements"),
        }.get(state, (None, None))
        owner_task = None
        if owner_state is not None:
            owner_task = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.state == owner_state,
                    Task.status == "completed",
                )
                .order_by(Task.created_at.desc())
            ).first()
        owner_output = (
            dict(owner_task.output_data or {}) if owner_task is not None else {}
        )
        owner_contract = (
            dict((owner_task.input_data or {}).get("work_item_contract") or {})
            if owner_task is not None
            else {}
        )
        owner_feedback_refs = {
            str(value)
            for value in (owner_contract.get("feedback_refs") or [])
            if value
        }
        active_feedback = [
            packet
            for packet in self.project_service.get_open_feedback_packets(project.id)
            if (
                packet.id in owner_feedback_refs
                if owner_feedback_refs
                else (
                    owner_target is None
                    or packet.recommended_rework_target in {None, "", owner_target}
                    or packet.source_stage in {state, owner_state}
                )
            )
        ]
        if state == "REWORK":
            active_feedback = self._current_feedback_frontier(active_feedback)
        decision_context = {
            "owner_task_id": owner_task.id if owner_task is not None else None,
            "owner_state": owner_state,
            "owner_result": {
                key: owner_output.get(key)
                for key in (
                    "next_state_event",
                    "addressed_feedback",
                    "completion_evidence",
                    "blockers",
                )
                if owner_output.get(key) not in (None, [], {})
            },
            "owner_contract": {
                "artifact_refs": owner_contract.get("artifact_refs") or {},
                "artifact_contract_key": owner_contract.get(
                    "artifact_contract_key"
                ),
                "acceptance_criterion_ids": sorted(
                    str(item.get("id"))
                    for item in (owner_contract.get("acceptance_criteria") or [])
                    if isinstance(item, dict) and item.get("id")
                ),
                "regression_acceptance_criterion_ids": sorted(
                    str(item.get("id"))
                    for item in (
                        owner_contract.get("regression_acceptance_criteria") or []
                    )
                    if isinstance(item, dict) and item.get("id")
                ),
                "in_scope_acceptance_criterion_ids": list(
                    owner_contract.get("in_scope_acceptance_criterion_ids") or []
                ),
                "feedback_refs": sorted(owner_feedback_refs),
            },
            "active_feedback": [
                {
                    "feedback_id": packet.id,
                    "kind": packet.kind,
                    "source_stage": packet.source_stage,
                    "source_task_id": packet.source_task_id,
                    "summary": packet.summary,
                    "recommended_rework_target": packet.recommended_rework_target,
                    "evidence": list(packet.evidence or [])[:5],
                }
                for packet in active_feedback
            ],
        }
        input_data = {
            "mode": "approval_decision",
            "state": state,
            "question": question,
            "options": options,
            "open_feedback": decision_context["active_feedback"],
            "approval_decision_context": decision_context,
            "work_item_contract": {
                # The owner WorkItem has two explicit acceptance namespaces.
                # Recompile those same namespaces for Coordinator instead of
                # making it infer current scope from historical project facts.
                "relevant_facts": [],
                "acceptance_criteria": list(
                    owner_contract.get("acceptance_criteria") or []
                ),
                "regression_acceptance_criteria": list(
                    owner_contract.get("regression_acceptance_criteria") or []
                ),
                "artifact_refs": dict(owner_contract.get("artifact_refs") or {}),
                "feedback_refs": sorted(owner_feedback_refs),
            },
        }
        output = await self._execute_auxiliary_agent(
            project,
            agent_type="conversation_coordinator",
            stage="approval_decision",
            input_data=input_data,
            extra_variables={
                "approval_state": state,
                "approval_question": question,
                "approval_options": json.dumps(options, ensure_ascii=False, indent=2),
                "approval_summary": (
                    self._build_summary_for_state(project, state)
                    + "\n\nLoopSupervisor："
                    + json.dumps(
                        project.context.get("loop_supervisor") or {},
                        ensure_ascii=False,
                    )
                ),
                "approval_decision_context": json.dumps(
                    decision_context, ensure_ascii=False, indent=2
                ),
            },
        )
        if output is None:
            return None
        suggested = output.get("action")
        if (
            state == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
            and suggested == "confirm"
        ):
            unmapped_ids = self._unmapped_current_acceptance_criterion_ids(project)
            if unmapped_ids:
                rejecting_action = next(
                    (
                        action
                        for action in valid_actions
                        if str(action).startswith("reject")
                    ),
                    None,
                )
                if rejecting_action is not None:
                    suggested = rejecting_action
                    output["action"] = rejecting_action
                    output["reason"] = (
                        "实现规格未结构化映射仍处于 active 的验收标准："
                        + ", ".join(sorted(unmapped_ids))
                        + "。如该条件已改为 Orchestrator 完成条件，"
                        "Requirements 必须同步移除或退役该 AC；"
                        "不得只在 definition_of_done 中用文本声称已迁移。"
                    )
                    output["evidence_refs"] = list(dict.fromkeys(
                        [
                            *(output.get("evidence_refs") or []),
                            ".mac/REQUIREMENTS.json",
                            ".mac/SPECS.json",
                        ]
                    ))
        if suggested in valid_actions:
            if state == "REWORK":
                # REWORK may use the Coordinator after LoopSupervisor forbids
                # the packet's default target.  Preserve the packet that
                # caused that escalation on the newly selected state edge;
                # otherwise the upstream Agent sees an empty prompt and does
                # an expensive no-op replay.
                target_state = StateMachine(project.task_type).next_state(
                    state, str(suggested)
                )
                blocking_feedback = sorted(
                    (packet for packet in active_feedback if packet.blocking),
                    key=lambda packet: packet.created_at,
                    reverse=True,
                )
                loop_signature = str(
                    (project.context.get("loop_supervisor") or {}).get(
                        "signature"
                    )
                    or ""
                )
                signature_matches = [
                    packet
                    for packet in blocking_feedback
                    if loop_signature
                    and FailureRouter.signature(packet) == loop_signature
                ]
                causal_feedback_ids = [
                    packet.id
                    for packet in (
                        signature_matches
                        if signature_matches
                        else blocking_feedback[:1]
                    )
                ]
                if target_state and causal_feedback_ids:
                    context = dict(project.context)
                    routed = dict(
                        context.get("routed_feedback_ids_by_state", {})
                    )
                    routed[target_state] = list(dict.fromkeys([
                        *(routed.get(target_state) or []),
                        *causal_feedback_ids,
                    ]))
                    context["routed_feedback_ids_by_state"] = routed
                    reason = str(output.get("reason") or "").strip()
                    if reason:
                        retry = dict(context.get("agent_retry_feedback", {}))
                        retry[target_state] = json.dumps(
                            {
                                "coordinator_resolution": reason,
                                "origin_state": state,
                                "causal_feedback_ids": causal_feedback_ids,
                            },
                            ensure_ascii=False,
                        )
                        context["agent_retry_feedback"] = retry
                    project.context = context
                    self.project_service.session.add(project)
                    self.project_service.session.commit()
            if str(suggested).startswith("reject"):
                # An AI-first rejection is actionable cross-Agent feedback,
                # not merely a state-machine edge.  Previously the
                # Coordinator's reason was discarded here, so the next
                # planner received an empty rejection_note and reproduced the
                # same artifact indefinitely.
                target = {
                    "REQUIREMENT_CONFIRM_PENDING": "requirements",
                    "ARCHITECTURE_CONFIRM_PENDING": "architecture",
                    "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "specs",
                    "BUG_FIX_CONFIRM_PENDING": "requirements",
                    "OUTLINE_CONFIRM_PENDING": "requirements",
                }.get(state)
                reason = str(output.get("reason") or "").strip()
                if target and reason:
                    coordinator_task = self.project_service.session.exec(
                        select(Task)
                        .where(
                            Task.project_id == project.id,
                            Task.agent_type == "conversation_coordinator",
                            Task.state == state,
                        )
                        .order_by(Task.created_at.desc())
                    ).first()
                    rounds = project.context.get("confirmation_rounds", {})
                    packet = await self.project_service.create_feedback_packet(
                        project_id=project.id,
                        source_stage=state,
                        source_task_id=(
                            coordinator_task.id if coordinator_task is not None else None
                        ),
                        source_round=int(rounds.get(state, 0) or 0),
                        kind="coordinator_rejection",
                        summary=reason,
                        blocking=True,
                        failure_class="contract_conflict",
                        recommended_rework_target=target,
                        evidence=[
                            {"ref": str(ref)}
                            for ref in (output.get("evidence_refs") or [])
                            if ref
                        ],
                    )
                    history = list(
                        self.project_service.session.exec(
                            select(FeedbackPacket).where(
                                FeedbackPacket.project_id == project.id
                            )
                        ).all()
                    )
                    assessment = self.loop_supervisor.assess(packet, history)
                    if assessment.repeated:
                        project.context = {
                            **project.context,
                            "loop_supervisor": {
                                "signature": assessment.signature,
                                "occurrences": assessment.occurrences,
                                "reason": assessment.reason,
                                "failure_domain": FailureRouter.for_feedback(
                                    packet
                                ).domain,
                                "prohibited_default_target": target,
                            },
                        }
                        self.project_service.session.add(project)
                        self.project_service.session.commit()
            return suggested
        # Some schema values map to generic confirm/reject.
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

    async def _answer_input_with_coordinator(
        self,
        project: Project,
        state: str,
        question: str,
    ) -> str | None:
        """Resolve a legacy input boundary without impersonating the user."""
        interaction_id = str(
            project.context.get("_pending_question_interaction_id") or ""
        ).strip()
        input_questions: list[dict[str, Any]] = []
        if interaction_id:
            interaction = self.project_service.get_question_interaction(
                interaction_id
            )
            if interaction is not None and interaction.status == "pending":
                input_questions = [
                    {
                        "question_id": item.question_id,
                        "kind": item.kind,
                        "question": item.question,
                        "reason": item.reason,
                        "options": item.options or [],
                        "default_assumption": item.default_assumption,
                        "recommended_option": item.recommended_option,
                        "sensitivity": item.sensitivity,
                        "secret_env_var": item.secret_env_var,
                    }
                    for item in self.project_service.get_questions_for_interaction(
                        interaction_id
                    )
                    if item.status == "pending"
                ]
        capability_contracts = self._project_runtime_capability_contracts(
            project.id
        )
        output = await self._execute_auxiliary_agent(
            project,
            agent_type="conversation_coordinator",
            stage="input_resolution",
            input_data={
                "mode": "input_resolution",
                "state": state,
                "question": question,
                "input_questions": input_questions,
                "runtime_capabilities": capability_contracts,
            },
            extra_variables={
                "input_state": state,
                "input_question": question,
                "input_questions": json.dumps(
                    input_questions, ensure_ascii=False, indent=2
                ),
                "runtime_capabilities": json.dumps(
                    capability_contracts, ensure_ascii=False, indent=2
                ),
                "input_summary": self._build_summary_for_state(project, state),
            },
        )
        if output is None or output.get("disposition") != "answered":
            return None
        answer = str(output.get("answer") or "").strip()
        return answer or None

    async def _evaluate_acceptance(
        self,
        project: Project,
    ) -> dict[str, Any] | None:
        """Run active black-box acceptance, then an independent evaluator."""
        artifact_revision_key = self._acceptance_revision_key(project)
        acceptance_agent_output, acceptance_agent_task_id = (
            await self._run_acceptance_agent(project, artifact_revision_key)
        )
        if acceptance_agent_output is None:
            return {
                "next_state_event": "fail",
                "result": "fail",
                "action": "reject_to_testing",
                "reason": "主动验收 Agent 未产生可复核结果",
                "summary": "主动验收未完成，不能要求用户代替系统执行可自动验证的工作",
                "acceptance_matrix": [],
                "blocking_conditions": ["test_result_unknown"],
                "_acceptance_revision_key": artifact_revision_key,
                "_acceptance_agent_output": None,
                "_cached": False,
            }
        delivery_policy = (project.context or {}).get("delivery_policy")
        if not stage_required(delivery_policy, "acceptance_evaluator"):
            result = self._compile_acceptance_evidence(
                project,
                acceptance_agent_output,
                artifact_revision_key,
            )
            self._overlay_manual_acceptance_evidence(project, result)
            self._reconcile_resolved_manual_acceptance(project, result)
            self._record_acceptance_matrix_evidence(project, result)
            return result
        revision_key = self._acceptance_evaluator_revision_key(
            artifact_revision_key,
            acceptance_agent_task_id,
            self._acceptance_quality_revision(project),
        )
        previous_tasks = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_evaluator",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).all()
        cached_task = next(
            (
                task
                for task in previous_tasks
                if (task.input_data or {}).get("acceptance_revision_key")
                == revision_key
                and task.output_data
            ),
            None,
        )
        ledger_snapshot = self._acceptance_ledger_for_revision(
            self._serialize_acceptance_ledger(project.id), artifact_revision_key
        )
        active_acceptance_contract = (
            self._verification_acceptance_criteria_for_project(project)
        )
        evaluator_evidence_bundle = self._acceptance_evaluator_evidence_bundle(project)
        output: dict[str, Any] | None = None
        contract_feedback = "首次执行；尚无合同覆盖错误。"
        used_cache = False
        for attempt in range(2):
            if attempt == 0 and cached_task is not None:
                output = cached_task.output_data
                used_cache = True
                logger.info(
                    "Reusing acceptance evaluation %s for unchanged revision %s",
                    cached_task.id,
                    revision_key,
                )
            else:
                output = await self._execute_auxiliary_agent(
                    project,
                    agent_type="acceptance_evaluator",
                    input_data={
                        "mode": "acceptance_evaluation",
                        "acceptance_revision_key": revision_key,
                        "acceptance_agent_task_id": acceptance_agent_task_id,
                        "acceptance_agent_output": acceptance_agent_output,
                        "acceptance_evidence_ledger": ledger_snapshot,
                        "acceptance_evaluator_evidence_bundle": evaluator_evidence_bundle,
                        "acceptance_contract_feedback": contract_feedback,
                        "work_item_contract": {
                            "objective": "Evaluate the active delivery acceptance contract",
                            "acceptance_criteria": active_acceptance_contract,
                            "runtime_contract": RuntimeManager.acceptance_contract(
                                project.context, artifact_revision_key
                            ),
                        },
                    },
                    extra_variables={
                        "acceptance_revision_key": revision_key,
                        "acceptance_agent_task_id": acceptance_agent_task_id,
                        "acceptance_agent_output": json.dumps(
                            acceptance_agent_output, ensure_ascii=False, indent=2
                        ),
                        "acceptance_evidence_ledger": json.dumps(
                            ledger_snapshot, ensure_ascii=False, indent=2
                        ),
                        "acceptance_contract_feedback": contract_feedback,
                        "acceptance_evaluator_evidence_bundle": json.dumps(
                            evaluator_evidence_bundle,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                    context_files=[],
                )
            if output is None:
                return None
            coverage_error = self._acceptance_evaluator_id_coverage_error(
                project, output
            )
            if coverage_error is None:
                break
            logger.warning(
                "Acceptance Evaluator ID contract failed for %s (attempt %s): %s",
                project.id,
                attempt + 1,
                coverage_error,
            )
            contract_feedback = coverage_error
            used_cache = False
        if output is None:
            return None
        if (
            coverage_error is not None
            and output.get("next_state_event") == "ask_user"
        ):
            return {
                "next_state_event": "fail",
                "result": "fail",
                "action": "reject_to_testing",
                "reason": coverage_error,
                "summary": "仍有系统内部验收工作，不能先暂停等待用户",
                "acceptance_matrix": [],
                "blocking_conditions": ["test_result_unknown"],
                "_acceptance_revision_key": revision_key,
                "_acceptance_agent_output": acceptance_agent_output,
                "_cached": False,
            }
        output = self._canonicalize_acceptance_test_evidence_refs(project, output)
        recommendation = output.get("recommendation")
        if recommendation:
            result = {
                "next_state_event": output.get("next_state_event"),
                "question_batch": output.get("question_batch"),
                "blockers": output.get("blockers", []),
                "action": recommendation.get("action"),
                "reason": recommendation.get("reason"),
                "result": output.get("result"),
                "summary": output.get("summary"),
                "acceptance_matrix": output.get("acceptance_matrix", []),
                "blocking_conditions": output.get("blocking_conditions", []),
            }
        else:
            result = {
            "next_state_event": output.get("next_state_event"),
            "question_batch": output.get("question_batch"),
            "blockers": output.get("blockers", []),
            "result": output.get("result"),
            "summary": output.get("summary"),
            "acceptance_matrix": output.get("acceptance_matrix", []),
            "blocking_conditions": output.get("blocking_conditions", []),
            }
        result["_acceptance_revision_key"] = revision_key
        result["_acceptance_artifact_revision_key"] = artifact_revision_key
        result["_acceptance_agent_output"] = acceptance_agent_output
        result["_cached"] = used_cache
        self._overlay_manual_acceptance_evidence(project, result)
        self._reconcile_resolved_manual_acceptance(project, result)
        self._record_acceptance_matrix_evidence(project, result)
        return result

    def _compile_acceptance_evidence(
        self,
        project: Project,
        acceptance_output: dict[str, Any],
        revision_key: str,
    ) -> dict[str, Any]:
        """Compile the ordinary acceptance verdict without another LLM call.

        Every link comes from schema-constrained stage output.  This compiler
        does not interpret prose, invent coverage, or weaken the same final
        coverage gate used for an Acceptance Evaluator result.
        """
        bundle = self._acceptance_evaluator_evidence_bundle(project)
        criteria = {
            str(item.get("acceptance_criterion_id")): item
            for item in acceptance_output.get("criteria") or []
            if isinstance(item, dict) and item.get("acceptance_criterion_id")
        }
        specs_by_ac: dict[str, list[dict[str, Any]]] = {}
        for spec in bundle.get("spec_mappings") or []:
            if not isinstance(spec, dict):
                continue
            for ac_id in spec.get("acceptance_criterion_ids") or []:
                specs_by_ac.setdefault(str(ac_id), []).append(spec)

        tester_rows = [
            item
            for item in (bundle.get("tester_output") or {}).get("test_evidence") or []
            if isinstance(item, dict) and item.get("result") == "pass"
        ]
        reviewer_output = bundle.get("reviewer_output") or {}
        reviewer_covered = {
            str(item.get("acceptance_criterion_id"))
            for item in reviewer_output.get("requirement_coverage") or []
            if isinstance(item, dict) and item.get("status") == "covered"
        }
        coder_output = bundle.get("coder_output") or {}
        coder_evidence: list[str] = []
        for item in coder_output.get("modified_files") or []:
            if isinstance(item, dict):
                value = item.get("path") or item.get("file")
            else:
                value = item
            if value:
                coder_evidence.append(str(value))
        for item in coder_output.get("completion_evidence") or []:
            if isinstance(item, str) and item.strip():
                coder_evidence.append(item.strip())

        external_ids = self._external_manual_acceptance_criterion_ids(project)
        acceptance_owned_ids = (
            self._acceptance_stage_manual_criterion_ids(project) - external_ids
        ) | (self._latest_tester_acceptance_handoff_ids(project) - external_ids)
        matrix: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        failures: list[str] = []
        missing_test_evidence = False

        requests = {
            str(item.get("acceptance_criterion_id")): item
            for item in acceptance_output.get("user_input_requests") or []
            if isinstance(item, dict) and item.get("acceptance_criterion_id")
        }
        for contract in self._verification_acceptance_criteria_for_project(project):
            if not isinstance(contract, dict) or not contract.get("id"):
                continue
            ac_id = str(contract["id"])
            observed = criteria.get(ac_id, {})
            linked_specs = specs_by_ac.get(ac_id, [])
            requirement_ids = [
                str(value)
                for value in (
                    contract.get("requirement_ids")
                    or ([contract.get("requirement_id")] if contract.get("requirement_id") else [])
                )
                if value
            ]
            if not requirement_ids:
                requirement_ids = sorted({
                    str(value)
                    for spec in linked_specs
                    for value in spec.get("requirement_ids") or []
                    if value
                })
            spec_ids = sorted({
                str(spec.get("id")) for spec in linked_specs if spec.get("id")
            })
            test_ids = [
                str(item.get("id"))
                for item in tester_rows
                if item.get("id")
                and ac_id in {str(value) for value in item.get("acceptance_criterion_ids") or []}
            ]
            implementation_evidence = list(dict.fromkeys(
                coder_evidence
                + [str(value) for value in observed.get("evidence") or [] if value]
                + ([str(observed.get("actual"))] if observed.get("actual") else [])
            ))
            reviewer_pass = (
                reviewer_output.get("result") == "pass" and ac_id in reviewer_covered
            )
            observed_result = str(observed.get("result") or "blocked")
            if ac_id in external_ids:
                row_result = "unknown"
                request = requests.get(ac_id)
                if request is not None:
                    safe_key = re.sub(r"[^a-z0-9_]+", "_", ac_id.lower()).strip("_") or "fact"
                    questions.append({
                        "question_id": f"acceptance-{safe_key}",
                        "decision_key": f"acceptance.{safe_key}",
                        "kind": "decision",
                        "question": str(request.get("question") or f"请确认 {ac_id}"),
                        "reason": str(request.get("reason") or "该事实只能由用户提供"),
                        "allow_free_text": not bool(request.get("options")),
                        "options": request.get("options") or [],
                        "affected_trace_ids": [ac_id],
                    })
            elif ac_id in acceptance_owned_ids:
                row_result = "pass" if observed_result == "pass" else "fail"
            else:
                row_result = (
                    "pass"
                    if observed_result == "pass" and test_ids and reviewer_pass
                    else "fail"
                )
                if not test_ids:
                    missing_test_evidence = True
            if row_result == "fail":
                failures.append(ac_id)
            matrix.append({
                "acceptance_criterion_id": ac_id,
                "requirement_ids": requirement_ids or [f"contract:{ac_id}"],
                "spec_ids": spec_ids or [f"contract:{ac_id}"],
                "implementation_evidence": implementation_evidence or [
                    f"Acceptance evidence for {ac_id}"
                ],
                "test_evidence_ids": test_ids,
                "review_result": "pass" if reviewer_pass else "unknown",
                "result": row_result,
                "notes": "由确定性 Evidence Policy 编译",
            })

        if failures:
            next_event, result, action = "fail", "fail", "reject_to_coding"
            summary = "验收证据未闭合：" + ", ".join(failures)
        elif questions:
            next_event, result, action = "ask_user", "pending_human_review", None
            summary = "系统可验证工作已完成，仍需用户提供外部事实或偏好"
        else:
            next_event, result, action = "complete", "pass", "confirm"
            summary = "Tester、Reviewer 与主动验收证据已确定性闭合"
        return {
            "next_state_event": next_event,
            "result": result,
            "action": action,
            "reason": summary,
            "summary": summary,
            "question_batch": (
                {"batch_key": f"acceptance_{revision_key}", "questions": questions}
                if questions else None
            ),
            "blockers": [],
            "blocking_conditions": (
                ["missing_test_evidence"] if failures and missing_test_evidence else []
            ),
            "acceptance_matrix": matrix,
            "_acceptance_revision_key": revision_key,
            "_acceptance_artifact_revision_key": revision_key,
            "_acceptance_agent_output": acceptance_output,
            "_cached": False,
            "_compiled_by": "evidence_policy",
        }

    def _acceptance_evaluator_evidence_bundle(
        self, project: Project
    ) -> dict[str, Any]:
        """Embed the evidence a zero-tool Acceptance Evaluator must audit.

        The evaluator prompt intentionally grants no tools. File paths in
        ``context_files`` are therefore only labels, not readable evidence.
        Keep the bundle structured and bounded: exact Spec mappings plus the
        canonical outputs of the latest delivery quality stages.
        """
        spec_mappings: dict[str, dict[str, Any]] = {}
        artifact_versions: dict[str, Any] = {}
        for artifact_dir in self._artifact_dirs_for_project(project):
            path = artifact_dir / "SPECS.json"
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            artifact_versions.setdefault("specs", artifact.get("version"))
            for item in artifact.get("specs") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                spec_id = str(item["id"])
                spec_mappings.setdefault(
                    spec_id,
                    {
                        "id": spec_id,
                        "requirement_ids": item.get("requirement_ids") or [],
                        "acceptance_criterion_ids": (
                            item.get("acceptance_criterion_ids") or []
                        ),
                        "affected_files": item.get("affected_files") or [],
                        "test_case_ids": [
                            str(case.get("id"))
                            for case in (item.get("test_cases") or [])
                            if isinstance(case, dict) and case.get("id")
                        ],
                    },
                )

        mac = project_mac_dir(project)

        def load_output(filename: str, fields: tuple[str, ...]) -> dict[str, Any]:
            try:
                value = json.loads((mac / filename).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return {
                field: value.get(field)
                for field in fields
                if field in value
            }

        coder = load_output(
            "CODER_OUTPUT.json",
            ("modified_files", "addressed_feedback", "completion_evidence", "blockers"),
        )
        tester = load_output(
            "TESTER_OUTPUT.json",
            (
                "passed", "test_evidence", "failures",
                "uncovered_acceptance_criterion_ids", "report",
            ),
        )
        reviewer = load_output(
            "REVIEWER_OUTPUT.json",
            (
                "result", "issues", "requirement_coverage",
                "stale_artifact_ids", "summary",
            ),
        )
        return redact_secrets({
            "spec_mappings": list(spec_mappings.values()),
            "coder_output": coder,
            "tester_output": tester,
            "reviewer_output": reviewer,
            "artifact_versions": artifact_versions,
        })

    def _canonicalize_acceptance_test_evidence_refs(
        self, project: Project, output: dict[str, Any]
    ) -> dict[str, Any]:
        """Canonicalize decorated references against the Tester evidence registry.

        Models sometimes return ``TE-001: description`` where the contract
        requires the exact ID. We only strip a colon suffix when its prefix is
        an actually registered passing evidence ID; unknown prose remains
        untouched and therefore fails closed in the coverage gate.
        """
        latest_tester = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "tester",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        tester_evidence = (
            (latest_tester.output_data or {}).get("test_evidence") or []
            if latest_tester is not None
            else []
        )
        valid_ids = {
            str(item.get("id"))
            for item in tester_evidence
            if isinstance(item, dict)
            and item.get("id")
            and item.get("result") == "pass"
        }
        normalized = dict(output)
        matrix: list[Any] = []
        for raw_item in output.get("acceptance_matrix") or []:
            if not isinstance(raw_item, dict):
                matrix.append(raw_item)
                continue
            item = dict(raw_item)
            refs: list[str] = []
            for raw_ref in item.get("test_evidence_ids") or []:
                ref = str(raw_ref).strip()
                prefix = ref.split(":", 1)[0].strip()
                refs.append(prefix if prefix in valid_ids else ref)
            item["test_evidence_ids"] = refs
            matrix.append(item)
        normalized["acceptance_matrix"] = matrix
        return normalized

    async def _run_acceptance_agent(
        self,
        project: Project,
        revision_key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Run or reuse the Codex black-box acceptance executor."""
        previous = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_agent",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).all()
        retry_generation = int(
            project.context.get("acceptance_retry_generation", 0) or 0
        )
        cached = next(
            (
                task
                for task in previous
                if (task.input_data or {}).get("acceptance_revision_key")
                == revision_key
                and int(
                    (task.input_data or {}).get("acceptance_retry_generation", 0)
                    or 0
                )
                == retry_generation
                and task.output_data
                and self._acceptance_agent_coverage_error(
                    project, dict(task.output_data)
                ) is None
            ),
            None,
        )
        if cached is not None:
            logger.info(
                "Reusing active acceptance %s for unchanged revision %s",
                cached.id,
                revision_key,
            )
            return cached.output_data, cached.id

        ledger_snapshot = self._acceptance_ledger_for_revision(
            self._serialize_acceptance_ledger(project.id), revision_key
        )
        try:
            workspace_snapshot = self.worktree_manager.workspace_fingerprint(
                project.workspace_path
            )
        except Exception:
            workspace_snapshot = {"digest": "unavailable"}
        active_acceptance_contract = (
            self._verification_acceptance_criteria_for_project(project)
        )
        output: dict[str, Any] | None = None
        latest_task_id = "missing"
        contract_feedback = (
            self._latest_acceptance_retry_feedback(project)
            or "无；这是本次验收修订的第一次执行。"
        )
        for attempt in range(2):
            # A previous Acceptance output describes a different delivery
            # revision.  Supplying it as an ordinary context file lets the
            # executor copy stale browser observations instead of exercising
            # the current build.  The revision-scoped evidence ledger is the
            # only supported channel for reusable acceptance evidence.
            acceptance_context_files = [
                path
                for path in self._context_files_for_state(
                    project, project.current_state
                )
                if path.name != "ACCEPTANCE_AGENT_OUTPUT.json"
            ]
            output = await self._execute_auxiliary_agent(
                project,
                agent_type="acceptance_agent",
                input_data={
                    "mode": "active_acceptance",
                    "acceptance_revision_key": revision_key,
                    "acceptance_retry_generation": retry_generation,
                    "acceptance_evidence_ledger": ledger_snapshot,
                    "acceptance_contract_feedback": contract_feedback,
                    # Auxiliary tasks are created outside the normal delivery
                    # task path.  Pass the compiled contract explicitly so
                    # ProjectService cannot fall back to the original chat
                    # brief's stale, differently named AC list.
                    "work_item_contract": {
                        "objective": "Execute the active delivery acceptance contract",
                        "acceptance_criteria": active_acceptance_contract,
                            "runtime_contract": RuntimeManager.acceptance_contract(
                                project.context,
                                revision_key,
                                workspace_snapshot=workspace_snapshot,
                            ),
                    },
                },
                context_files=acceptance_context_files,
                extra_variables={
                    "acceptance_revision_key": revision_key,
                    "acceptance_evidence_ledger": json.dumps(
                        ledger_snapshot, ensure_ascii=False, indent=2
                    ),
                    "acceptance_contract_feedback": contract_feedback,
                },
            )
            task = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == "acceptance_agent",
                )
                .order_by(Task.created_at.desc())
            ).first()
            latest_task_id = task.id if task is not None else "missing"
            if output is None:
                return None, latest_task_id
            coverage_error = self._acceptance_agent_coverage_error(project, output)
            if coverage_error is None:
                environment = output.get("environment") or {}
                project.context = RuntimeManager.record_observed_runtime(
                    project.context,
                    revision=str(workspace_snapshot.get("digest") or "unavailable"),
                    environment=environment if isinstance(environment, dict) else {},
                )
                self.project_service.session.add(project)
                self.project_service.session.commit()
                return output, latest_task_id
            contract_feedback = (
                coverage_error
                + "。重新读取全部验收规划产物，保留已取得的有效证据，并为精确集合中的每个 AC 输出且仅输出一条 criteria。"
            )
            logger.warning(
                "Acceptance Agent coverage contract failed for %s (attempt %s): %s",
                project.id,
                attempt + 1,
                coverage_error,
            )
        return output, latest_task_id

    @staticmethod
    def _acceptance_ledger_for_revision(
        rows: list[dict[str, Any]], revision_key: str
    ) -> list[dict[str, Any]]:
        """Expose active evidence and mark whether it crosses the revision.

        The service passes only active ledger rows here. Coder completion has
        already marked evidence for structurally affected ACs stale. Therefore
        an active old-revision row is precisely an unaffected dependency and
        may be reused; discarding it caused every repair to rerun all browser
        ACs despite scoped invalidation.
        """
        return [
            {
                **row,
                "reusable_for_current_revision": True,
                "evidence_revision_matches": (
                    row.get("artifact_revision") == revision_key
                ),
            }
            for row in rows
        ]

    def _latest_acceptance_retry_feedback(self, project: Project) -> str | None:
        """Return outstanding evaluator rows for a still-current delivery.

        The acceptance card's ``retry`` action intentionally stays in the
        same state.  Without carrying the evaluator's failed matrix forward,
        the next executor sees only artifacts and has to rediscover which
        user paths were never exercised.  Ignore an evaluator older than any
        completed delivery-quality task because subsequent implementation,
        test, or review work may have invalidated its observations.
        """
        evaluator = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_evaluator",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        if evaluator is None or not isinstance(evaluator.output_data, dict):
            return None
        latest_quality = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type.in_(["coder", "tester", "reviewer"]),
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        if (
            latest_quality is not None
            and latest_quality.updated_at > evaluator.updated_at
        ):
            return None
        outstanding = [
            {
                "acceptance_criterion_id": row.get("acceptance_criterion_id"),
                "result": row.get("result"),
                "notes": row.get("notes"),
                "manual_verification": row.get("manual_verification"),
            }
            for row in evaluator.output_data.get("acceptance_matrix") or []
            if isinstance(row, dict) and row.get("result") != "pass"
        ]
        if not outstanding:
            return None
        return (
            "上一轮独立验收评估仍有以下未通过项。复用已通过项的直接证据，"
            "优先亲自补执行其中 system_observable 路径；只有真正依赖实体设备、"
            "外部账号或主观偏好的事实才可请求用户：\n"
            + json.dumps(outstanding, ensure_ascii=False, indent=2)[:12000]
        )

    def _acceptance_agent_coverage_error(
        self, project: Project, output: dict[str, Any]
    ) -> str | None:
        """Require the executor to address the whole active AC contract."""
        required_ids = self._required_acceptance_criterion_ids(project)
        reported_ids = [
            str(item.get("acceptance_criterion_id") or "")
            for item in output.get("criteria") or []
            if isinstance(item, dict)
        ]
        if not required_ids:
            return None
        if len(reported_ids) == len(set(reported_ids)) and set(reported_ids) == required_ids:
            return None
        missing = sorted(required_ids - set(reported_ids))
        unknown = sorted(set(reported_ids) - required_ids)
        duplicates = sorted(
            item for item in set(reported_ids) if reported_ids.count(item) > 1
        )
        return (
            "Acceptance Agent criteria IDs 必须精确匹配本轮有效验收合同"
            f"；missing={missing}, unknown={unknown}, duplicates={duplicates}"
        )

    def _acceptance_evaluator_id_coverage_error(
        self, project: Project, output: dict[str, Any]
    ) -> str | None:
        """Require the evaluator matrix to cover the cumulative AC contract."""
        # The evaluator schema intentionally omits the matrix while it asks
        # for a genuinely external fact.  The full matrix is required on the
        # resumed verdict after that answer is recorded; rejecting the
        # intermediate ask_user envelope forces a pointless second model run
        # and contradicts the declared schema.
        if output.get("next_state_event") == "ask_user":
            question_ids = {
                str(trace_id)
                for question in (output.get("question_batch") or {}).get(
                    "questions", []
                )
                if isinstance(question, dict)
                for trace_id in question.get("affected_trace_ids") or []
            }
            deferred_internal_ids = {
                str(trace_id)
                for blocker in output.get("blockers") or []
                if isinstance(blocker, dict) and blocker.get("blocking") is True
                for trace_id in blocker.get(
                    "affected_acceptance_criterion_ids"
                ) or []
                if str(trace_id) not in question_ids
            }
            if deferred_internal_ids:
                return (
                    "ask_user 不能抢先暂停仍有系统内部待办的验收；这些 AC 没有由本轮"
                    "外部问题覆盖："
                    f"{sorted(deferred_internal_ids)}。先返回 fail 并将它们交回系统执行；"
                    "内部证据完整后，才能单独询问真正的外部事实。"
                )
            return None
        required_ids = self._required_acceptance_criterion_ids(project)
        reported_ids = [
            str(item.get("acceptance_criterion_id") or "")
            for item in output.get("acceptance_matrix") or []
            if isinstance(item, dict)
        ]
        if not required_ids:
            return None
        if len(reported_ids) == len(set(reported_ids)) and set(reported_ids) == required_ids:
            return None
        missing = sorted(required_ids - set(reported_ids))
        unknown = sorted(set(reported_ids) - required_ids)
        duplicates = sorted(
            item for item in set(reported_ids) if reported_ids.count(item) > 1
        )
        return (
            "Acceptance Evaluator matrix IDs 必须精确匹配本轮有效验收合同"
            f"；missing={missing}, unknown={unknown}, duplicates={duplicates}。"
            "保留可复核证据并为精确集合中的每个 AC 输出且仅输出一条矩阵记录。"
        )

    def _acceptance_revision_key(self, project: Project) -> str:
        """Hash every input that can change active acceptance evidence."""
        artifact_digests: dict[str, str] = {}
        for artifact_dir in self._artifact_dirs_for_project(project):
            for name in (
                "ITERATION_BRIEF.json",
                "REQUIREMENTS.json",
                "BUG_ANALYSIS.json",
                "DOC_OUTLINE.json",
                "ARCHITECTURE.json",
                "SPECS.json",
            ):
                path = artifact_dir / name
                if not path.exists():
                    continue
                key = str(path)
                try:
                    artifact_digests[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    artifact_digests[key] = "unreadable"
        # Acceptance execution depends on the delivered behavior, not on the
        # identity of the latest Tester/Reviewer task. Hashing quality task IDs
        # forced a multi-minute browser rerun even when a verifier merely
        # regenerated equivalent evidence. The workspace digest changes only
        # when project-visible delivery files change.
        try:
            workspace_revision = self.worktree_manager.workspace_fingerprint(
                project.workspace_path
            ).get("digest", "")
        except Exception:
            workspace_revision = "unavailable"
        # External/user evidence is an acceptance input in its own right.  It
        # must invalidate a cached evaluator result even when no artifact or
        # quality task changed; otherwise an old ``ask_user`` result can be
        # replayed immediately after the user answered that exact question.
        external_evidence = [
            {
                "acceptance_criterion_id": row.acceptance_criterion_id,
                "version": row.version,
                "verdict": row.verdict,
                "source_type": row.source_type,
                "source_ref": row.source_ref,
                "limitations": row.limitations,
                "observed_at": row.observed_at.isoformat(),
            }
            for row in self.project_service.get_active_acceptance_evidence(
                project.id
            ).values()
            if row.source_type in {"user", "external"}
        ]
        external_evidence.sort(
            key=lambda item: (
                str(item["acceptance_criterion_id"]), int(item["version"])
            )
        )
        execution_contract: dict[str, str] = {}
        try:
            execution_contract["acceptance_prompt"] = hashlib.sha256(
                Path("prompts/acceptance_agent.md").read_bytes()
            ).hexdigest()
        except OSError:
            execution_contract["acceptance_prompt"] = "unreadable"
        acceptance_config = self.prompt_renderer.get_agent_config(
            "acceptance_agent"
        )
        runtime_name = str(acceptance_config.get("runtime") or "")
        runtime_config = (
            self.prompt_renderer.agent_config.get("runtimes", {}).get(
                runtime_name, {}
            )
        )
        # Only configuration that can alter Acceptance Agent execution belongs
        # in its evidence revision.  Hashing the entire agent_config made an
        # evaluator-only routing change invalidate fresh browser evidence and
        # rerun the expensive executor for no semantic reason.
        execution_contract["acceptance_agent_config"] = hashlib.sha256(
            json.dumps(
                {"agent": acceptance_config, "runtime": runtime_config},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        raw = json.dumps(
            {
                "artifacts": artifact_digests,
                "workspace_revision": workspace_revision,
                "external_evidence": external_evidence,
                "execution_contract": execution_contract,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _acceptance_quality_revision(self, project: Project) -> str:
        """Hash semantic verifier inputs without coupling browser execution to them."""
        def semantic_payload(value: Any) -> Any:
            # Profiling, timestamps, task IDs and other underscore-prefixed
            # metadata describe the execution attempt rather than its verdict.
            # Including them made an equivalent verification rerun invalidate
            # the evaluator cache even when its evidence had not changed.
            if isinstance(value, dict):
                return {
                    key: semantic_payload(item)
                    for key, item in value.items()
                    if not str(key).startswith("_")
                }
            if isinstance(value, list):
                return [semantic_payload(item) for item in value]
            return value

        quality: dict[str, Any] = {}
        for agent_type in ("tester", "reviewer"):
            task = self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == agent_type,
                    Task.status == "completed",
                )
                .order_by(Task.updated_at.desc())
            ).first()
            if task is not None:
                quality[agent_type] = semantic_payload(task.output_data or {})
        return hashlib.sha256(
            json.dumps(quality, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]

    @staticmethod
    def _acceptance_evaluator_revision_key(
        artifact_revision_key: str,
        acceptance_agent_task_id: str,
        quality_revision: str = "",
    ) -> str:
        """Include the evaluator contract so prompt fixes cannot reuse stale verdicts."""
        prompt_path = Path("prompts/acceptance_evaluator.md")
        try:
            prompt_digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        except OSError:
            prompt_digest = "unreadable"
        raw = (
            f"{artifact_revision_key}:{acceptance_agent_task_id}:"
            f"{quality_revision}:{prompt_digest}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _acceptance_criteria_affected_by_task(
        self, project: Project, task: Task
    ) -> set[str]:
        """Resolve the ACs structurally implicated by a repair task.

        A coding task created from a feedback packet should invalidate only the
        evidence for the acceptance criteria named by that packet.  If no
        trustworthy structured link exists, callers deliberately fall back to
        invalidating all evidence rather than guessing from prose.
        """
        contract = (task.input_data or {}).get("work_item_contract") or {}
        feedback_refs = {
            str(value) for value in contract.get("feedback_refs", []) if value
        }
        if not feedback_refs:
            return set()

        required_ids = self._required_acceptance_criterion_ids(project)
        found: set[str] = set()
        direct_keys = {
            "acceptance_criterion_id",
            "acceptance_criterion_ids",
            "affected_acceptance_criterion_ids",
            "affected_trace_ids",
        }
        identifier_keys = {"id", "test_id", "test"}

        def collect(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    collect(child_value, str(child_key))
                return
            if isinstance(value, list):
                for child in value:
                    collect(child, key)
                return
            if not isinstance(value, str):
                return
            if key in direct_keys and value in required_ids:
                found.add(value)
                return
            if key in identifier_keys:
                found.update(
                    match
                    for match in re.findall(r"AC-[A-Z0-9]+(?:-[A-Z0-9]+)+", value)
                    if match in required_ids
                )

        packets = self.project_service.session.exec(
            select(FeedbackPacket).where(FeedbackPacket.id.in_(feedback_refs))
        ).all()
        for packet in packets:
            collect(packet.evidence)
        return found

    @staticmethod
    def _coder_changed_delivery_artifacts(output: dict[str, Any]) -> bool:
        """Return whether the Coder reports a real delivery-file change.

        MAC bookkeeping under ``.mac/`` is not product behavior and a
        no-change audit round must not invalidate previously observed user
        evidence.
        """
        for item in output.get("modified_files") or []:
            path = (
                item.get("path") or item.get("file")
                if isinstance(item, dict)
                else item
            )
            normalized = str(path or "").strip().replace("\\", "/")
            if normalized and not normalized.startswith(".mac/"):
                return True
        return False

    def _serialize_acceptance_ledger(self, project_id: str) -> list[dict[str, Any]]:
        return [
            {
                "acceptance_criterion_id": row.acceptance_criterion_id,
                "verdict": row.verdict,
                "verification_method": row.verification_method,
                "evidence_scope": row.evidence_scope,
                "environment": row.environment,
                "evidence": row.evidence,
                "limitations": row.limitations,
                "source_type": row.source_type,
                "source_ref": row.source_ref,
                "artifact_revision": row.artifact_revision,
                "observed_at": row.observed_at.isoformat(),
            }
            for row in self.project_service.get_active_acceptance_evidence(project_id).values()
        ]

    def _overlay_manual_acceptance_evidence(
        self, project: Project, evaluation: dict[str, Any]
    ) -> None:
        manual_ids = self._external_manual_acceptance_criterion_ids(project)
        ledger = self.project_service.get_active_acceptance_evidence(project.id)
        matrix = [
            dict(item)
            for item in (evaluation.get("acceptance_matrix") or [])
            if isinstance(item, dict)
        ]
        by_id = {
            str(item.get("acceptance_criterion_id")): item
            for item in matrix
            if item.get("acceptance_criterion_id")
        }
        for ac_id in manual_ids:
            evidence = ledger.get(ac_id)
            if evidence is None or evidence.verdict != "pass" or evidence.limitations:
                continue
            item = by_id.get(ac_id)
            if item is None:
                item = {"acceptance_criterion_id": ac_id}
                matrix.append(item)
                by_id[ac_id] = item
            item.update(
                {
                    "result": "pass",
                    "manual_verification": {
                        "status": "verified",
                        "source_question_id": evidence.source_ref,
                        "evidence_scope": evidence.evidence_scope,
                        "limitations": [],
                    },
                }
            )
        evaluation["acceptance_matrix"] = matrix

    def _reconcile_resolved_manual_acceptance(
        self, project: Project, evaluation: dict[str, Any]
    ) -> None:
        """Remove stale questions that fresh explicit user evidence resolved.

        The evaluator is advisory.  A cached or imperfect model response must
        not overwrite a newer, structured ``verified_pass`` verdict in the
        durable evidence ledger.  Unrelated questions and blockers remain
        untouched, so this reconciliation is fail-closed outside the exact
        acceptance criteria proven by the user.
        """
        resolved_ids = set(self._verified_manual_acceptance_answers(project))
        if not resolved_ids:
            return

        def affected_ids(item: dict[str, Any]) -> set[str]:
            values = (
                item.get("affected_trace_ids")
                or item.get("affected_acceptance_criterion_ids")
                or []
            )
            return {str(value) for value in values if value}

        batch = evaluation.get("question_batch")
        if isinstance(batch, dict):
            questions = [
                item
                for item in (batch.get("questions") or [])
                if not (
                    isinstance(item, dict)
                    and affected_ids(item)
                    and affected_ids(item).issubset(resolved_ids)
                )
            ]
            evaluation["question_batch"] = (
                {**batch, "questions": questions} if questions else None
            )

        for field in ("blockers", "blocking_conditions"):
            evaluation[field] = [
                item
                for item in (evaluation.get(field) or [])
                if not (
                    isinstance(item, dict)
                    and affected_ids(item)
                    and affected_ids(item).issubset(resolved_ids)
                )
            ]

        matrix = [
            item
            for item in (evaluation.get("acceptance_matrix") or [])
            if isinstance(item, dict)
        ]
        if (
            evaluation.get("next_state_event") == "ask_user"
            and evaluation.get("question_batch") is None
            and not evaluation.get("blockers")
            and not evaluation.get("blocking_conditions")
            and matrix
            and all(item.get("result") == "pass" for item in matrix)
        ):
            evaluation["next_state_event"] = "complete"
            evaluation["result"] = "pass"
            evaluation["action"] = "confirm"

    def _record_acceptance_matrix_evidence(
        self, project: Project, evaluation: dict[str, Any]
    ) -> None:
        """Persist machine evidence; manual verdicts are recorded on user input."""
        manual_ids = self._external_manual_acceptance_criterion_ids(project)
        system_manual_ids = (
            self._acceptance_stage_manual_criterion_ids(project) - manual_ids
        )
        acceptance_owned_ids = (
            system_manual_ids
            | (
                self._latest_tester_acceptance_handoff_ids(project)
                - manual_ids
            )
        )
        direct_acceptance = self._direct_system_acceptance_evidence(evaluation)
        revision = str(
            evaluation.get("_acceptance_artifact_revision_key")
            or evaluation.get("_acceptance_revision_key")
            or ""
        )
        latest_tester = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "tester",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        tester_ids = {
            str(item.get("id"))
            for item in (
                (latest_tester.output_data or {}).get("test_evidence", [])
                if latest_tester is not None
                else []
            )
            if isinstance(item, dict)
            and item.get("id")
            and item.get("result") == "pass"
        }
        latest_reviewer = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "reviewer",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        reviewer_output = (
            latest_reviewer.output_data or {}
            if latest_reviewer is not None
            else {}
        )
        reviewer_covered = {
            str(item.get("acceptance_criterion_id"))
            for item in reviewer_output.get("requirement_coverage", [])
            if isinstance(item, dict)
            and item.get("acceptance_criterion_id")
            and item.get("status") == "covered"
        }
        requires_tester = any(
            isinstance(stage, dict)
            and stage.get("execution_state") == "TESTING"
            for stage in StateMachine(project.task_type).profile.get("stages", [])
        )
        for item in evaluation.get("acceptance_matrix") or []:
            if not isinstance(item, dict):
                continue
            ac_id = str(item.get("acceptance_criterion_id") or "")
            if not ac_id or ac_id in manual_ids:
                continue
            result = str(item.get("result") or "")
            if result not in {"pass", "fail"}:
                continue
            if ac_id in acceptance_owned_ids:
                direct = direct_acceptance.get(ac_id)
                if result == "pass" and direct is None:
                    continue
                self.project_service.record_acceptance_evidence(
                    project.id,
                    ac_id,
                    verdict=result,
                    verification_method="manual",
                    evidence_scope=str(
                        (direct or {}).get("method")
                        or (direct or {}).get("actual")
                        or "Acceptance Agent black-box verification"
                    ),
                    source_type=str(
                        (direct or {}).get("_source_type") or "acceptance_agent"
                    ),
                    source_ref=revision,
                    artifact_revision=revision,
                    evidence=list((direct or {}).get("evidence") or []),
                    limitations=list((direct or {}).get("limitations") or []),
                    commit=False,
                )
                continue
            evidence_refs = [
                str(value) for value in item.get("test_evidence_ids", []) if value
            ]
            if result == "pass" and (
                (requires_tester and (not evidence_refs or not set(evidence_refs).issubset(tester_ids)))
                or reviewer_output.get("result") != "pass"
                or ac_id not in reviewer_covered
            ):
                # The evaluator may describe a pass, but only deterministically
                # linked Tester and Reviewer evidence is allowed into the
                # durable ledger as a pass.
                continue
            self.project_service.record_acceptance_evidence(
                project.id,
                ac_id,
                verdict=result,
                verification_method="automated",
                evidence_scope="Tester/Reviewer acceptance evidence",
                source_type="evaluator",
                source_ref=revision,
                artifact_revision=revision,
                evidence=evidence_refs,
                limitations=[],
                commit=False,
            )
        self.project_service.session.commit()

    @staticmethod
    def _direct_system_acceptance_evidence(
        evaluation: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Return complete system-observable evidence produced at acceptance.

        Manual describes the interaction method, not who owns the missing
        fact.  If the Acceptance Agent can observe the result itself, its
        direct black-box execution is the authoritative final-stage evidence;
        it must not be forced back through Tester/Reviewer or delegated to the
        user.
        """
        def has_material_limitations(values: Any) -> bool:
            for raw in values or []:
                value = str(raw or "").strip().lower()
                if not value:
                    continue
                if value in {"none", "n/a", "na", "not applicable", "no limitations"}:
                    continue
                if re.match(r"^无(?:$|[。.!！—-])", value):
                    continue
                return True
            return False

        output = evaluation.get("_acceptance_agent_output") or {}
        direct = {
            str(item.get("acceptance_criterion_id")): item
            for item in output.get("criteria", [])
            if isinstance(item, dict)
            and item.get("acceptance_criterion_id")
            and item.get("evidence_source") == "system_observable"
            and item.get("result") == "pass"
            and item.get("evidence")
            and not has_material_limitations(item.get("limitations"))
        }
        return direct

    def _check_acceptance_coverage(
        self, project: Project, evaluation: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return missing requirement AC IDs from the evaluator matrix."""
        if evaluation is None:
            return {
                "missing_ids": [],
                "message": "验收评估不可用，不能在缺少证据时放行",
            }
        if (
            evaluation.get("result") == "fail"
            or evaluation.get("blocking_conditions")
            or str(evaluation.get("action", "")).startswith("reject_")
        ):
            # A failed verdict is already persisted and routed by
            # _record_acceptance_evaluator_failure. Treating the same verdict
            # as a coverage violation created a second conflicting Specs
            # feedback packet for every genuine browser failure.
            return None

        deferred_ids = self._explicitly_deferred_acceptance_criterion_ids(project)
        required_ids = self._required_acceptance_criterion_ids(project) - deferred_ids
        if not required_ids:
            return {
                "missing_ids": [],
                "message": "需求产物没有可核验的验收标准 ID",
            }
        covered_ids = {
            item.get("acceptance_criterion_id")
            for item in evaluation.get("acceptance_matrix", [])
            if isinstance(item, dict)
            and item.get("acceptance_criterion_id")
            and item.get("result") == "pass"
        }
        missing_ids = sorted(required_ids - covered_ids)
        if missing_ids:
            return {
                "missing_ids": missing_ids,
                "message": f"验收矩阵未覆盖以下验收标准: {', '.join(missing_ids)}",
            }

        workflow_stages = StateMachine(project.task_type).profile.get("stages", [])
        requires_tester = any(
            isinstance(stage, dict)
            and stage.get("execution_state") == "TESTING"
            for stage in workflow_stages
        )
        latest_tester = (
            self.project_service.session.exec(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.agent_type == "tester",
                    Task.status == "completed",
                )
                .order_by(Task.updated_at.desc())
            ).first()
            if requires_tester
            else None
        )
        latest_reviewer = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "reviewer",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        tester_evidence = (
            (latest_tester.output_data or {}).get("test_evidence", [])
            if latest_tester is not None
            else []
        )
        passing_evidence_ids = {
            str(item.get("id"))
            for item in tester_evidence
            if isinstance(item, dict)
            and item.get("id")
            and item.get("result") == "pass"
        }
        reviewer_output = (
            latest_reviewer.output_data or {}
            if latest_reviewer is not None
            else {}
        )
        manual_ids = self._external_manual_acceptance_criterion_ids(project)
        system_manual_ids = (
            self._acceptance_stage_manual_criterion_ids(project) - manual_ids
        )
        acceptance_owned_ids = (
            system_manual_ids
            | (
                self._latest_tester_acceptance_handoff_ids(project)
                - manual_ids
            )
        )
        direct_acceptance = self._direct_system_acceptance_evidence(evaluation)
        verified_manual_answers = self._verified_manual_acceptance_answers(project)
        reviewer_covered = {
            str(item.get("acceptance_criterion_id"))
            for item in reviewer_output.get("requirement_coverage", [])
            if isinstance(item, dict)
            and item.get("acceptance_criterion_id")
            and item.get("status") == "covered"
        }
        broken_links: list[str] = []
        for item in evaluation.get("acceptance_matrix", []):
            if not isinstance(item, dict) or item.get("result") != "pass":
                continue
            ac_id = str(item.get("acceptance_criterion_id") or "")
            if ac_id in acceptance_owned_ids:
                if ac_id in direct_acceptance:
                    continue
                broken_links.append(
                    f"{ac_id}: missing direct Acceptance Agent black-box evidence"
                )
                continue
            # A manual criterion is deliberately outside the Tester/Reviewer
            # evidence boundary. It may pass only after a dedicated structured
            # user verdict, linked back by the evaluator without limitations.
            if ac_id in manual_ids:
                manual = item.get("manual_verification") or {}
                if (
                    verified_manual_answers.get(ac_id)
                    == manual.get("source_question_id")
                    and manual.get("status") == "verified"
                    and not (manual.get("limitations") or [])
                    and str(manual.get("evidence_scope") or "").strip()
                ):
                    continue
                broken_links.append(
                    f"{ac_id}: missing dedicated verified manual verdict"
                )
                continue
            evidence_ids = {
                str(value) for value in item.get("test_evidence_ids", []) if value
            }
            if requires_tester and (
                not evidence_ids
                or not evidence_ids.issubset(passing_evidence_ids)
            ):
                broken_links.append(f"{ac_id}: invalid test evidence refs")
            if reviewer_output.get("result") != "pass" or ac_id not in reviewer_covered:
                broken_links.append(f"{ac_id}: missing reviewer coverage")
        if broken_links:
            return {
                "missing_ids": [],
                "message": "验收证据链无效: " + "; ".join(broken_links[:12]),
            }
        return None

    def _verified_manual_acceptance_answers(
        self, project: Project
    ) -> dict[str, str]:
        """Return AC -> question_id for explicit per-criterion user passes."""
        manual_ids = self._external_manual_acceptance_criterion_ids(project)
        ledger_rows = self.project_service.session.exec(
            select(AcceptanceEvidence).where(
                AcceptanceEvidence.project_id == project.id,
                AcceptanceEvidence.acceptance_criterion_id.in_(manual_ids),
            )
        ).all()
        # Once a criterion has entered the durable evidence ledger, that
        # ledger is authoritative.  In particular, an old answered question
        # must not resurrect evidence that a later delivery revision marked
        # stale.  The QuestionInteraction scan below is retained only as a
        # compatibility path for legacy projects that never had a ledger row.
        ledger_criterion_ids = {
            str(row.acceptance_criterion_id) for row in ledger_rows
        }
        verified: dict[str, str] = {
            ac_id: str(row.source_ref)
            for ac_id, row in self.project_service.get_active_acceptance_evidence(
                project.id
            ).items()
            if ac_id in manual_ids
            and row.verdict == "pass"
            and row.source_type == "user"
            and row.source_ref
            and not row.limitations
        }
        interactions = self.project_service.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project.id,
                QuestionInteraction.status == "answered",
            )
        ).all()
        for interaction in interactions:
            for question in self.project_service.get_questions_for_interaction(
                interaction.id
            ):
                if question.status != "answered":
                    continue
                answer = self.project_service.get_question_answer(question.id)
                if answer is None or answer.skipped or answer.answer_source != "user":
                    continue
                trace_ids = [
                    str(trace_id)
                    for trace_id in (question.affected_trace_ids or [])
                    if trace_id in manual_ids
                    and trace_id not in ledger_criterion_ids
                ]
                option_keys = {
                    str(option.get("key"))
                    for option in (question.options or [])
                    if isinstance(option, dict) and option.get("key")
                }
                # Manual acceptance is fail-closed. A prose answer (even one
                # containing the word "pass") cannot prove which part of a
                # combined question was actually exercised. Each AC therefore
                # needs its own question and the user's explicit pass option.
                if (
                    len(trace_ids) == 1
                    and answer.answer == "verified_pass"
                    and "verified_pass" in option_keys
                ):
                    verified[trace_ids[0]] = question.question_id
        return verified

    def _ensure_structured_manual_acceptance_questions(
        self,
        project: Project,
        recommendation: dict[str, Any],
    ) -> None:
        """Fail closed by adding one structured question per unverified manual AC."""
        manual_ids = self._external_manual_acceptance_criterion_ids(project)
        missing_ids = manual_ids - set(
            self._verified_manual_acceptance_answers(project)
        )
        if not missing_ids:
            return

        requirements_path = project_mac_dir(project) / {
            "feature": "REQUIREMENTS.json",
            "iteration": "ITERATION_BRIEF.json",
            "bug_fix": "BUG_ANALYSIS.json",
            "documentation": "DOC_OUTLINE.json",
        }.get(project.task_type, "REQUIREMENTS.json")
        try:
            artifact = json.loads(requirements_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            artifact = {}
        criterion_text = {
            str(item.get("id")): str(
                item.get("description")
                or item.get("criterion")
                or item.get("title")
                or item.get("statement")
                or item.get("id")
            )
            for item in artifact.get("acceptance_criteria", [])
            if isinstance(item, dict) and item.get("id")
        }

        active_requests = {
            str(item.get("acceptance_criterion_id")): item
            for item in (
                (recommendation.get("_acceptance_agent_output") or {}).get(
                    "user_input_requests", []
                )
            )
            if isinstance(item, dict) and item.get("acceptance_criterion_id")
        }
        question_batch = recommendation.get("question_batch")
        if not isinstance(question_batch, dict):
            question_batch = {
                "batch_key": "manual_acceptance",
                "questions": [],
            }
        questions = list(question_batch.get("questions") or [])
        covered_by_valid_question: set[str] = set()
        required_option_keys = {
            "verified_pass", "verified_fail", "not_verified", "provide_details"
        }
        for question in questions:
            if not isinstance(question, dict):
                continue
            trace_ids = [
                str(value) for value in question.get("affected_trace_ids", []) if value
            ]
            option_keys = {
                str(item.get("key"))
                for item in question.get("options", [])
                if isinstance(item, dict) and item.get("key")
            }
            if len(trace_ids) == 1 and required_option_keys.issubset(option_keys):
                question.setdefault(
                    "decision_key",
                    f"acceptance.{trace_ids[0].lower()}.manual_verdict",
                )
                covered_by_valid_question.add(trace_ids[0])

        options = [
            {
                "key": "verified_pass", "label": "完整通过",
                "impact": "该验收项可进入通过判定", "recommended": False,
            },
            {
                "key": "verified_fail", "label": "验证失败",
                "impact": "记录失败并进入修复", "recommended": False,
            },
            {
                "key": "not_verified", "label": "尚未验证",
                "impact": "保持验收待处理", "recommended": True,
            },
            {
                "key": "provide_details", "label": "补充详情",
                "impact": "记录观察结果后继续判断", "recommended": False,
                "triggers_free_text": True,
            },
        ]
        for ac_id in sorted(missing_ids - covered_by_valid_question):
            request = active_requests.get(ac_id) or {}
            questions.append({
                "question_id": f"q_manual_{ac_id.lower().replace('-', '_')}",
                "decision_key": f"acceptance.{ac_id.lower()}.manual_verdict",
                "kind": "blocking",
                "question": (
                    str(request.get("question") or "").strip()
                    or (
                        f"请确认验收标准 {ac_id} 的外部结果："
                        f"{criterion_text.get(ac_id, ac_id)}，然后选择实际情况。"
                    )
                ),
                "reason": (
                    str(request.get("reason") or "").strip()
                    or "该事实位于系统无法访问的外部环境，且尚无逐项结构化结论。"
                ),
                "allow_free_text": True,
                "options": options,
                "affected_trace_ids": [ac_id],
            })

        recommendation["next_state_event"] = "ask_user"
        recommendation["question_batch"] = {
            **question_batch,
            "batch_key": "manual_acceptance",
            "questions": questions,
        }
        blockers = list(recommendation.get("blockers") or [])
        blocked_ids = {
            str(value)
            for blocker in blockers
            if isinstance(blocker, dict)
            for value in blocker.get("affected_acceptance_criterion_ids", [])
        }
        for ac_id in sorted(missing_ids - blocked_ids):
            blockers.append({
                "id": f"rr_manual_{ac_id.lower().replace('-', '_')}",
                "category": "missing_information",
                "summary": f"{ac_id} 缺少逐项人工验收结论",
                "evidence": ["当前没有该 AC 对应的 verified_pass 用户选择"],
                "required_resolution": "按该验收标准完整执行并选择实际结果",
                "preferred_resolver": "coordinator",
                "blocking": True,
                "affected_acceptance_criterion_ids": [ac_id],
            })
        recommendation["blockers"] = blockers

    async def _record_acceptance_coverage_failure(
        self,
        project: Project,
        violation: dict[str, Any],
        recommended_rework_target: str | None,
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
            recommended_rework_target=recommended_rework_target,
            evidence=[
                {
                    "type": "missing_ac_coverage",
                    "missing_ids": violation["missing_ids"],
                }
            ],
        )

    @staticmethod
    def _acceptance_coverage_rework_route(
        project: Project,
        preferred_action: str | None = None,
    ) -> tuple[str, str | None]:
        """Choose a legal, profile-defined route for an acceptance gate failure."""
        state_machine = StateMachine(project.task_type)
        legal_events = set(state_machine.available_events("ACCEPTANCE_PENDING"))
        configured = [
            item
            for item in state_machine.profile.get("acceptance_targets", [])
            if isinstance(item, dict)
            and item.get("action") in legal_events
            and item.get("target_stage")
        ]
        if preferred_action:
            preferred = next(
                (
                    item
                    for item in configured
                    if item.get("action") == preferred_action
                ),
                None,
            )
            if preferred is not None:
                return str(preferred["action"]), str(preferred["target_stage"])
        if configured:
            # Profiles order targets from broadest to narrowest. Replaying the
            # narrowest legal stage is the least disruptive deterministic fix.
            selected = configured[-1]
            return str(selected["action"]), str(selected["target_stage"])
        return "reject", None

    async def _record_acceptance_evaluator_failure(
        self,
        project: Project,
        recommendation: dict[str, Any],
    ) -> FeedbackPacket | None:
        """Persist a failed acceptance verdict for the selected rework Agent.

        The acceptance report was previously rendered in Feishu without a
        FeedbackPacket.  After the user selected a rework target, the next
        Agent therefore received an empty ``open_feedback`` list and guessed
        from the old specification.  Persist the evaluator's structured
        observations just like Tester and Reviewer failures.
        """
        if (
            recommendation.get("result") != "fail"
            and recommendation.get("next_state_event") != "fail"
        ):
            return None

        source_task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_evaluator",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        source_task_id = source_task.id if source_task is not None else None
        existing = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.source_stage == "ACCEPTANCE",
                FeedbackPacket.source_task_id == source_task_id,
                FeedbackPacket.kind == "acceptance_rejection",
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
        ).first()
        if existing is not None:
            return existing

        action_targets = {
            "reject_to_requirements": "requirements",
            "reject_to_architecture": "architecture",
            "reject_to_specs": "specs",
            "reject_to_coding": "coding",
            "reject_to_testing": "testing",
        }
        target = action_targets.get(str(recommendation.get("action") or ""))
        evidence: list[dict[str, Any]] = []
        for row in recommendation.get("acceptance_matrix") or []:
            if not isinstance(row, dict) or row.get("result") == "pass":
                continue
            compact_row = {
                "type": "acceptance_failure",
                "acceptance_criterion_id": row.get("acceptance_criterion_id"),
                "result": row.get("result"),
                "implementation_evidence": row.get("implementation_evidence", []),
                "test_evidence_ids": row.get("test_evidence_ids", []),
                "manual_verification": row.get("manual_verification"),
                "notes": row.get("notes"),
            }
            evidence.append(
                json.loads(
                    redact_secrets(json.dumps(compact_row, ensure_ascii=False))
                )
            )

        packet = await self.project_service.create_feedback_packet(
            project_id=project.id,
            source_stage="ACCEPTANCE",
            source_task_id=source_task_id,
            source_round=source_task.round_number if source_task is not None else 1,
            kind="acceptance_rejection",
            summary=str(recommendation.get("summary") or "验收未通过"),
            blocking=True,
            failure_class="observed_behavior_failure",
            recommended_rework_target=target,
            evidence=evidence,
            user_comment=str(recommendation.get("reason") or "") or None,
        )
        # Each evaluator verdict is a complete snapshot of the current
        # acceptance matrix. Older acceptance-rejection packets are therefore
        # superseded, not cumulative. Keeping them open makes Coder receive a
        # growing list of already-fixed failures and obscures the one current
        # defect.
        prior_packets = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.source_stage == "ACCEPTANCE",
                FeedbackPacket.kind == "acceptance_rejection",
                FeedbackPacket.status.in_(["open", "disputed"]),
                FeedbackPacket.id != packet.id,
            )
        ).all()
        for prior in prior_packets:
            prior.status = "resolved"
            prior.resolved_by_task_id = source_task_id
            prior.updated_at = datetime.utcnow()
            self.project_service.session.add(prior)
        if prior_packets:
            self.project_service.session.commit()
        return packet

    def _agent_completion_error(
        self,
        project: Project,
        state_machine: StateMachine,
        task: Task,
        output: dict[str, Any],
        runtime_tool_calls: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Enforce semantic completion invariants independently of the LLM.

        JSON Schema proves shape. This gate proves that a claimed ``complete``
        is compatible with the WorkItem, quality evidence, and open blocking
        feedback. Contract violations are runtime failures and are retried;
        they must never advance the workflow as a business success.
        """
        event = output.get("next_state_event")
        if not isinstance(event, str):
            return "Agent contract violation: next_state_event is required"
        if event not in state_machine.available_events(project.current_state):
            return (
                "Agent contract violation: event "
                f"{event!r} is not valid for state {project.current_state}"
            )
        if output.get("success") is not True:
            return "Agent contract violation: a structured business result must set success=true"

        questions = (output.get("question_batch") or {}).get("questions")
        if event == "ask_user" and not questions:
            return "Agent contract violation: ask_user requires a non-empty question_batch"
        if event == "ask_user" and not (output.get("blockers") or []):
            return (
                "Agent contract violation: ask_user requires at least one "
                "structured ResolutionRequest blocker"
            )
        if event == "ask_user" and str(project.task_type) == "iteration":
            active_ids = self._required_acceptance_criterion_ids(project)
            cumulative_ids = {
                str(item.get("id"))
                for item in self._acceptance_criteria_for_project(project)
                if isinstance(item, dict) and item.get("id")
            }
            requested_ids = {
                str(trace_id)
                for question in questions or []
                if isinstance(question, dict)
                for trace_id in question.get("affected_trace_ids") or []
                if trace_id
            } | {
                str(trace_id)
                for blocker in output.get("blockers") or []
                if isinstance(blocker, dict)
                for trace_id in blocker.get(
                    "affected_acceptance_criterion_ids"
                ) or []
                if trace_id
            }
            historical_only_ids = (requested_ids & cumulative_ids) - active_ids
            if historical_only_ids and not (requested_ids & active_ids):
                return (
                    "Agent contract violation: an iteration may not interrupt "
                    "the user for a regression-only obligation. Current "
                    f"delivery ACs are {sorted(active_ids)}; regression-only "
                    f"request referenced {sorted(historical_only_ids)}. Record "
                    "the regression result, route an implementation defect to "
                    "its owning Agent, or continue when the missing external "
                    "capability is outside the current delivery contract."
                )
        if questions and event not in {"ask_user", "clarify", "conflict"}:
            return "Agent contract violation: question_batch requires an input event"
        if event == "fail":
            target = str(output.get("recommended_rework_target") or "").strip()
            if (
                target
                and target != "none"
                and state_machine.route_event_to_stage("REWORK", target) is None
            ):
                return (
                    "Agent contract violation: recommended_rework_target "
                    f"{target!r} is not a stage in the active "
                    f"{project.task_type!r} workflow. Judge the current WorkItem "
                    "with its authoritative artifacts or choose an available "
                    "owner; do not create a blocker that this workflow cannot route."
                )
        if project.current_state == "REVIEW":
            coverage = output.get("requirement_coverage") or []
            required_ids = self._required_acceptance_criterion_ids(project)
            reported_ids = [
                str(item.get("acceptance_criterion_id") or "")
                for item in coverage
                if isinstance(item, dict)
            ]
            if required_ids and (
                len(reported_ids) != len(set(reported_ids))
                or set(reported_ids) != required_ids
            ):
                missing = sorted(required_ids - set(reported_ids))
                unknown = sorted(set(reported_ids) - required_ids)
                return (
                    "Agent contract violation: reviewer requirement_coverage IDs "
                    "must exactly match the cumulative active acceptance contract "
                    "(base requirements plus current iteration artifacts); "
                    f"missing={missing}, unknown={unknown}"
                )
        if event != "complete":
            return None

        # Inputs observed while the task was running intentionally invalidate
        # the result in _consume_pending_user_inputs; do not misclassify that
        # business rework path as a malformed runtime response.
        if self.project_service.get_pending_user_inputs(project.id):
            return None

        blockers = output.get("blockers") or []
        if blockers:
            return "Agent contract violation: complete cannot contain unresolved blockers"
        deferred = output.get("deferred_obligations") or []
        allowed_deferred = {
            str(item.get("id"))
            for item in (
                task.input_data.get("work_item_contract", {}).get(
                    "allowed_deferred_obligations", []
                )
                or []
            )
            if isinstance(item, dict) and item.get("id")
        }
        acceptance_handoff_ids = (
            self._pending_acceptance_evidence_ids(
                [
                    item
                    for item in (output.get("test_evidence") or [])
                    if isinstance(item, dict)
                ]
            )
            | self._latest_tester_acceptance_handoff_ids(project)
        )

        def _is_acceptance_stage_handoff(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            # Older Reviewer outputs used the equally structured
            # acceptance_criterion_id/owner/obligation vocabulary before the
            # shared deferred-obligation contract was introduced.  Treat it
            # as the same handoff instead of spending an LLM retry rewriting
            # field names, while retaining the same criterion/evidence gate.
            target_stage = str(
                item.get("target_stage") or item.get("owner") or ""
            ).strip().lower()
            criterion_ids = {
                str(value)
                for value in (
                    item.get("acceptance_criterion_ids")
                    or (
                        [item.get("acceptance_criterion_id")]
                        if item.get("acceptance_criterion_id")
                        else []
                    )
                )
                if value
            }
            contract = str(
                item.get("contract") or item.get("obligation") or ""
            ).strip()
            return bool(
                target_stage in {"acceptance", "acceptance_agent"}
                and criterion_ids
                and criterion_ids.issubset(acceptance_handoff_ids)
                and contract
            )

        unapproved = [
            item.get("id")
            for item in deferred
            if not isinstance(item, dict)
            or (
                not _is_acceptance_stage_handoff(item)
                and (
                    item.get("allowed_by_contract") is not True
                    or str(item.get("id")) not in allowed_deferred
                )
            )
        ]
        if unapproved:
            return f"Agent contract violation: unapproved deferred obligations: {unapproved}"

        if project.current_state == "CODING":
            assigned_feedback_ids = {
                str(value)
                for value in (
                    task.input_data.get("work_item_contract", {}).get(
                        "feedback_refs", []
                    )
                    or []
                )
                if value
            }
            open_blocking = {
                item.id
                for item in self.project_service.get_open_feedback_packets(project.id)
                if item.blocking and item.id in assigned_feedback_ids
            }
            addressed = {
                str(item.get("feedback_id")): item.get("status")
                for item in output.get("addressed_feedback", [])
                if isinstance(item, dict) and item.get("feedback_id")
            }
            unresolved = sorted(
                feedback_id
                for feedback_id in open_blocking
                if addressed.get(feedback_id) not in {"fixed", "not_applicable"}
            )
            if unresolved:
                return (
                    "Agent contract violation: complete requires every assigned "
                    "blocking feedback item to be fixed or explicitly shown as "
                    f"already satisfied (not_applicable): {unresolved}"
                )

        if project.current_state == "TESTING":
            # A legacy/defensive guard below normalizes an inconsistent
            # complete+failed assessment into the business `fail` route.
            if output.get("passed") is not True:
                return None
            evidence = output.get("test_evidence") or []
            active_ids = self._required_acceptance_criterion_ids(project)
            external_manual_ids = (
                self._acceptance_stage_manual_criterion_ids(project)
                | self._pending_acceptance_evidence_ids(
                    [
                        item
                        for item in evidence
                        if isinstance(item, dict)
                    ]
                )
            )
            bad_evidence = [
                item.get("id", "<unknown>")
                for item in evidence
                if not isinstance(item, dict)
                or (
                    self._evidence_applies_to_active_contract(item, active_ids)
                    and
                    item.get("result") != "pass"
                    and not self._is_pending_external_manual_evidence(
                        item, external_manual_ids
                    )
                )
            ]
            blocking_failures = [
                item for item in (output.get("failures") or [])
                if isinstance(item, dict) and item.get("blocking") is not False
            ]
            uncovered_ids = {
                str(value)
                for value in (
                    output.get("uncovered_acceptance_criterion_ids") or []
                )
                if value
            }
            blocking_uncovered_ids = (
                (uncovered_ids & active_ids)
                - self._explicitly_deferred_acceptance_criterion_ids(project)
                - external_manual_ids
            )
            if (
                output.get("passed") is not True
                or not evidence
                or bad_evidence
                or blocking_uncovered_ids
                or blocking_failures
            ):
                return (
                    "Agent contract violation: tester complete requires passed=true, "
                    "non-empty passing evidence, full AC coverage, and no blocking failures"
                )
            evidence_error = self._tester_evidence_error(
                project,
                evidence,
                runtime_tool_calls or [],
                output.get("verified_feedback_ids") or [],
                require_feedback_closure=not self._tester_only_external_manual_pending(
                    project,
                    output,
                    runtime_tool_calls or [],
                ),
            )
            if evidence_error:
                return f"Agent contract violation: {evidence_error}"

        if project.current_state == "REVIEW":
            coverage = output.get("requirement_coverage") or []
            # Let the quality guard turn an explicit failed verdict into REWORK.
            if output.get("result") != "pass":
                return None
            deferred_ids = self._explicitly_deferred_acceptance_criterion_ids(project)
            external_manual_ids = (
                self._acceptance_stage_manual_criterion_ids(project)
                | self._latest_tester_acceptance_handoff_ids(project)
            )
            incomplete = [
                item.get("acceptance_criterion_id", "<unknown>")
                for item in coverage
                if (
                    not isinstance(item, dict)
                    or (
                        item.get("status") != "covered"
                        and str(item.get("acceptance_criterion_id") or "")
                        not in deferred_ids | external_manual_ids
                    )
                )
            ]
            if (
                output.get("result") != "pass"
                or any(
                    isinstance(item, dict) and item.get("blocking")
                    for item in (output.get("issues") or [])
                )
                or not coverage
                or incomplete
                or output.get("stale_artifact_ids")
            ):
                return (
                    "Agent contract violation: reviewer complete requires pass, "
                    "fully covered requirements, no blocking issues, and no stale artifacts"
                )
            if not self._runtime_observed_read(runtime_tool_calls or []):
                return (
                    "Agent contract violation: reviewer pass requires runtime-"
                    "observed Read evidence"
                )
        return None

    def _canonicalize_feedback_references(
        self, project_id: str, output: dict[str, Any]
    ) -> None:
        """Expand unambiguous shortened feedback IDs to canonical packet IDs.

        Models occasionally copy the timestamp portion of an ID while dropping
        its random suffix. Identifier reconciliation is deterministic and only
        succeeds for an exact or unique prefix; ambiguous/unknown references
        remain unchanged and therefore still fail the normal closure guard.
        """
        known_ids = {
            item.id
            for item in self.project_service.get_open_feedback_packets(project_id)
        }

        def canonical(value: Any) -> str:
            candidate = str(value or "").strip()
            if candidate in known_ids or not candidate.startswith("fb_"):
                return candidate
            matches = sorted(
                known for known in known_ids if known.startswith(candidate + "_")
            )
            return matches[0] if len(matches) == 1 else candidate

        verified = [
            canonical(value) for value in (output.get("verified_feedback_ids") or [])
            if value
        ]
        if verified:
            output["verified_feedback_ids"] = list(dict.fromkeys(verified))
        for item in output.get("test_evidence") or []:
            if not isinstance(item, dict):
                continue
            refs = [
                canonical(value) for value in (item.get("feedback_ids") or [])
                if value
            ]
            if refs:
                item["feedback_ids"] = list(dict.fromkeys(refs))

    def _normalize_quality_gate_output(
        self,
        project: Project,
        output: dict[str, Any],
        runtime_tool_calls: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Reconcile contradictory quality verdicts with structured evidence.

        A Tester/Reviewer invocation that returned valid structured output did
        not crash merely because its verdict contradicts its evidence.
        Retrying it as a runtime failure produces noisy repeated attempts.
        Evidence with a real blocker is normalized to ``fail``; an otherwise
        empty ``fail`` whose complete formal evidence is passing is normalized
        to ``complete``.
        """
        question_batch = output.get("question_batch")
        if isinstance(question_batch, dict) and question_batch.get("questions"):
            return None

        state = project.current_state
        reconciled_reason: str | None = None
        if state == "TESTING":
            self._normalize_undeployed_acceptance_e2e(project, output)
        # Detect the explicit final-acceptance hand-off before generic verdict
        # normalization mutates ``passed``.  The hand-off recognizer expects
        # the model's original failed/pending shape so it can validate the
        # skipped real-entry contract and deliberately waive feedback closure
        # for the Acceptance-owned evidence.
        tester_acceptance_handoff = (
            state == "TESTING"
            and self._tester_only_external_manual_pending(
                project, output, runtime_tool_calls or []
            )
        )
        reviewer_acceptance_handoff = (
            state == "REVIEW"
            and self._reviewer_only_external_manual_pending(
                project, output, runtime_tool_calls or []
            )
        )
        if state == "TESTING" and output.get("next_state_event") == "fail":
            evidence = [
                item for item in (output.get("test_evidence") or [])
                if isinstance(item, dict)
            ]
            active_ids = self._required_acceptance_criterion_ids(project)
            active_evidence = [
                item
                for item in evidence
                if self._evidence_applies_to_active_contract(item, active_ids)
            ]
            blocking_failures = [
                item for item in (output.get("failures") or [])
                if isinstance(item, dict) and item.get("blocking") is not False
            ]
            uncovered_ids = {
                str(value)
                for value in (output.get("uncovered_acceptance_criterion_ids") or [])
                if value
            } & active_ids
            allowed_uncovered = (
                self._explicitly_deferred_acceptance_criterion_ids(project)
                | self._acceptance_stage_manual_criterion_ids(project)
                | self._pending_acceptance_evidence_ids(active_evidence)
            )
            manual_ids = (
                self._acceptance_stage_manual_criterion_ids(project)
                | self._pending_acceptance_evidence_ids(active_evidence)
            )
            all_evidence_admissible = bool(active_evidence) and all(
                item.get("result") == "pass"
                or self._is_pending_external_manual_evidence(item, manual_ids)
                for item in active_evidence
            )
            if (
                not tester_acceptance_handoff
                and output.get("passed") is False
                and not blocking_failures
                and all_evidence_admissible
                and not (uncovered_ids - allowed_uncovered)
            ):
                output["next_state_event"] = "complete"
                output["passed"] = True
                output["failure_class"] = "none"
                output["recommended_rework_target"] = "none"
                reconciled_reason = (
                    "tester returned fail despite complete passing evidence "
                    "or a valid Acceptance handoff, with no blocking failure"
                )
                notes = output.setdefault("notes", [])
                if isinstance(notes, list):
                    notes.append(
                        "系统已按结构化证据消除矛盾裁决：全部正式证据通过、"
                        "无阻塞失败且无未覆盖内部标准，因此内部质量关为通过。"
                    )
        if tester_acceptance_handoff:
            output["next_state_event"] = "complete"
            output["passed"] = True
            output["failure_class"] = "none"
            output["recommended_rework_target"] = "none"
            notes = output.setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(
                    "仅剩 verification_method=manual 的真实入口验收；"
                    "内部质量关已通过，待最终验收阶段执行复测合同。"
                )
        if reviewer_acceptance_handoff:
            output["next_state_event"] = "complete"
            output["result"] = "pass"
            output["recommended_rework_target"] = "none"
            for item in output.get("issues") or []:
                if isinstance(item, dict) and item.get("blocking"):
                    item["blocking"] = False
            for item in output.get("blockers") or []:
                if isinstance(item, dict) and item.get("blocking"):
                    item["blocking"] = False
            notes = output.setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(
                    "仅后续最终验收阶段承接的项目为 partially_covered；"
                    "当前代码审查无阻塞问题，待最终验收关闭。"
                )
        if output.get("next_state_event") != "complete":
            return None

        reasons: list[str] = []
        if state == "TESTING":
            claimed_pass = output.get("passed") is True
            evidence = output.get("test_evidence") or []
            active_ids = self._required_acceptance_criterion_ids(project)
            external_manual_ids = (
                self._acceptance_stage_manual_criterion_ids(project)
                | self._pending_acceptance_evidence_ids(
                    [
                        item
                        for item in evidence
                        if isinstance(item, dict)
                    ]
                )
            )
            if not claimed_pass:
                reasons.append("passed is not true")
            if not evidence:
                reasons.append("passing evidence is empty")
            if any(
                not isinstance(item, dict)
                or (
                    self._evidence_applies_to_active_contract(item, active_ids)
                    and
                    item.get("result") != "pass"
                    and not self._is_pending_external_manual_evidence(
                        item, external_manual_ids
                    )
                )
                for item in evidence
            ):
                reasons.append("test evidence contains non-passing results")
            if any(
                isinstance(item, dict) and item.get("blocking") is not False
                for item in (output.get("failures") or [])
            ):
                reasons.append("blocking failures remain")
            uncovered = {
                str(value)
                for value in (output.get("uncovered_acceptance_criterion_ids") or [])
                if value
            } & active_ids
            if uncovered - self._explicitly_deferred_acceptance_criterion_ids(project) - external_manual_ids:
                reasons.append("acceptance criteria remain uncovered")
            if not reasons:
                evidence_error = self._tester_evidence_error(
                    project,
                    evidence,
                    runtime_tool_calls or [],
                    output.get("verified_feedback_ids") or [],
                    require_feedback_closure=not tester_acceptance_handoff,
                )
                if evidence_error:
                    reasons.append(evidence_error)
            if reasons:
                output["next_state_event"] = "fail"
                output["passed"] = False
                if claimed_pass and output.get("failure_class") in {None, "none"}:
                    has_product_failure = any(
                        isinstance(item, dict) and item.get("blocking") is not False
                        for item in (output.get("failures") or [])
                    )
                    output["failure_class"] = (
                        "implementation_bug" if has_product_failure else "test_bug"
                    )
                if claimed_pass and output.get("recommended_rework_target") in {None, "none"}:
                    output["recommended_rework_target"] = (
                        "coding"
                        if output.get("failure_class") == "implementation_bug"
                        else "testing"
                    )

        elif state == "REVIEW":
            claimed_pass = output.get("result") == "pass"
            if not claimed_pass:
                reasons.append("review result is not pass")
            if any(
                isinstance(item, dict) and item.get("blocking")
                for item in (output.get("issues") or [])
            ):
                reasons.append("blocking review issues remain")
            deferred_ids = self._explicitly_deferred_acceptance_criterion_ids(project)
            external_manual_ids = (
                self._acceptance_stage_manual_criterion_ids(project)
                | self._latest_tester_acceptance_handoff_ids(project)
            )
            if reviewer_acceptance_handoff:
                external_manual_ids |= {
                    str(item.get("acceptance_criterion_id") or "")
                    for item in (output.get("requirement_coverage") or [])
                    if isinstance(item, dict)
                    and item.get("status") == "partially_covered"
                    and item.get("acceptance_criterion_id")
                }
            coverage = output.get("requirement_coverage") or []
            if not coverage:
                reasons.append("requirement coverage is empty")
            elif any(
                not isinstance(item, dict)
                or (
                    item.get("status") != "covered"
                    and str(item.get("acceptance_criterion_id") or "")
                    not in deferred_ids | external_manual_ids
                )
                for item in coverage
            ):
                reasons.append("requirements are not fully covered")
            if output.get("stale_artifact_ids"):
                reasons.append("stale artifacts remain")
            if output.get("result") == "pass" and not self._runtime_observed_read(
                runtime_tool_calls or []
            ):
                reasons.append("runtime-observed Read evidence is missing")
            if reasons:
                output["next_state_event"] = "fail"
                output["result"] = "fail"
                if claimed_pass and output.get("recommended_rework_target") in {None, "none"}:
                    output["recommended_rework_target"] = (
                        "coding"
                        if any("blocking" in reason for reason in reasons)
                        else "testing"
                    )

        return "; ".join(reasons) if reasons else reconciled_reason

    def _normalize_undeployed_acceptance_e2e(
        self,
        project: Project,
        output: dict[str, Any],
    ) -> bool:
        """Turn pre-deployment E2E connection failures into Acceptance handoff.

        Tester runs before Orchestrator merges the worktree and Acceptance
        deploys the canonical service.  A structured environment failure at
        that point is not evidence that the implementation is broken.  This
        normalization is deliberately narrow: every non-passing active row
        must be an Acceptance-owned E2E contract, internal evidence must have
        passed, and the runtime contract must show that the expected revision
        is not currently deployed.  Once the expected revision is observed,
        real-entry failures remain blocking.
        """
        if (
            output.get("next_state_event") != "fail"
            or output.get("passed") is not False
            or output.get("failure_class") != "environment_failure"
        ):
            return False
        runtime_contract = RuntimeManager.verification_contract(project.context)
        deployment_status = str(
            runtime_contract.get("deployment_status") or ""
        ).strip()
        expected_revision = str(
            runtime_contract.get("expected_artifact_revision") or ""
        ).strip()
        observed_revision = str(
            runtime_contract.get("last_observed_running_revision") or ""
        ).strip()
        if (
            deployment_status == "observed"
            and expected_revision
            and observed_revision == expected_revision
        ):
            return False

        active_ids = self._required_acceptance_criterion_ids(project)
        all_evidence = [
            item
            for item in (output.get("test_evidence") or [])
            if isinstance(item, dict)
        ]
        evidence = [
            item
            for item in all_evidence
            if self._evidence_applies_to_active_contract(item, active_ids)
        ]
        passing = [item for item in evidence if item.get("result") == "pass"]
        non_passing = [item for item in evidence if item.get("result") != "pass"]
        if not passing or not non_passing:
            return False
        for item in non_passing:
            criterion_ids = {
                str(value)
                for value in (item.get("acceptance_criterion_ids") or [])
                if value
            }
            if not (
                item.get("type") == "e2e"
                and criterion_ids
                and str(item.get("command_or_method") or "").strip()
                and str(item.get("expected") or "").strip()
            ):
                return False

        for item in non_passing:
            item["result"] = "skipped"
        output["failures"] = []
        notes = output.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(
                "系统依据运行合同将尚未部署当前 revision 的真实入口 E2E "
                "失败归一化为 Acceptance 阶段复测合同；内部质量证据不受影响。"
            )
        return True

    @staticmethod
    def _explicitly_deferred_acceptance_criterion_ids(
        project: Project,
    ) -> set[str]:
        """Return AC IDs that the confirmed brief explicitly marks as deferred.

        A deferred criterion is not silently dropped: the tester must still list it
        in ``uncovered_acceptance_criterion_ids``.  This exemption only applies to
        criteria carrying an explicit contract marker (``-PENDING`` in a string
        criterion, or a structured pending/deferred/external-blocked status).
        Ordinary uncovered criteria remain blocking.
        """
        state_path = project_mac_dir(project) / "STATE.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()

        criteria = (
            state.get("context", {})
            .get("confirmed_brief", {})
            .get("acceptance_criteria", [])
        )
        deferred: set[str] = set()
        deferred_statuses = {"pending", "deferred", "blocked_external"}
        for item in criteria:
            if isinstance(item, str):
                criterion_id = item.split(":", 1)[0].strip()
                if criterion_id.upper().endswith("-PENDING"):
                    deferred.add(criterion_id)
                continue
            if not isinstance(item, dict) or not item.get("id"):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status in deferred_statuses:
                deferred.add(str(item["id"]))
        return deferred

    @staticmethod
    def _artifact_dirs_for_project(project: Project) -> list[Path]:
        """Return current and complete lineage metadata directories.

        Iterations point at their immediate predecessor. Walking only one
        ``base_project_id`` silently drops obligations after the second
        iteration (for example feature -> bug fix -> feature). STATE files are
        the durable lineage source, so this remains deterministic and does not
        require natural-language inference.
        """
        directories = [project_mac_dir(project)]
        base_project_id = str(
            (getattr(project, "context", None) or {}).get("base_project_id") or ""
        ).strip()
        workspace_mac = Path(project.workspace_path) / ".mac"
        seen_project_ids: set[str] = set()
        while base_project_id and base_project_id not in seen_project_ids:
            seen_project_ids.add(base_project_id)
            base_dir = workspace_mac / "projects" / base_project_id
            directories.append(base_dir)
            try:
                state = json.loads((base_dir / "STATE.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
            base_project_id = str(
                (state.get("context") or {}).get("base_project_id") or ""
            ).strip()
        # System-managed original projects use the legacy root .mac layout;
        # bound repositories use namespaced project directories. Keeping the
        # root last preserves current/iteration precedence.
        directories.append(workspace_mac)
        unique: list[Path] = []
        for directory in directories:
            if directory not in unique:
                unique.append(directory)
        return unique

    @staticmethod
    def _acceptance_criteria_for_project(project: Project) -> list[dict[str, Any]]:
        """Load the cumulative acceptance contract for a reused workspace.

        Iterations add BUG_ANALYSIS/DOC_OUTLINE obligations without erasing the
        original product requirements.  A newer requirements artifact may also
        explicitly retire historical criteria that the user has removed or
        replaced.  Reading exactly one file loses either regression criteria or
        the current change criteria; blindly unioning every file resurrects
        obsolete product contracts.
        """
        criteria_by_id: dict[str, dict[str, Any]] = {}
        retired_ids: set[str] = set()
        # Artifact directories are ordered newest/current first.  Once an ID is
        # defined by a newer artifact, an older tombstone or definition cannot
        # overwrite it.
        newer_ids: set[str] = set()
        filenames = [
            "ITERATION_BRIEF.json",
            "REQUIREMENTS.json",
            "BUG_ANALYSIS.json",
            "DOC_OUTLINE.json",
        ]
        artifact_dirs = AgentOrchestrator._artifact_dirs_for_project(project)
        seen_paths: set[Path] = set()
        for artifact_dir in artifact_dirs:
            for filename in filenames:
                path = artifact_dir / filename
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                try:
                    artifact = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for criterion_id in artifact.get(
                    "retired_acceptance_criterion_ids", []
                ):
                    criterion_id = str(criterion_id).strip()
                    if criterion_id and criterion_id not in newer_ids:
                        retired_ids.add(criterion_id)
                for item in artifact.get("acceptance_criteria", []):
                    if isinstance(item, dict) and item.get("id"):
                        criterion_id = str(item["id"])
                        if criterion_id in newer_ids or criterion_id in retired_ids:
                            continue
                        criteria_by_id[criterion_id] = item
                        newer_ids.add(criterion_id)
        return list(criteria_by_id.values())

    @staticmethod
    def _current_acceptance_criteria_for_project(
        project: Project,
    ) -> list[dict[str, Any]]:
        """Load only acceptance criteria authored for the current iteration."""
        criteria_by_id: dict[str, dict[str, Any]] = {}
        artifact_dir = project_mac_dir(project)
        for filename in (
            "ITERATION_BRIEF.json",
            "REQUIREMENTS.json",
            "BUG_ANALYSIS.json",
            "DOC_OUTLINE.json",
        ):
            try:
                artifact = json.loads(
                    (artifact_dir / filename).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            for item in artifact.get("acceptance_criteria", []):
                if isinstance(item, dict) and item.get("id"):
                    criteria_by_id[str(item["id"])] = item
        return list(criteria_by_id.values())

    @classmethod
    def _unmapped_current_acceptance_criterion_ids(
        cls,
        project: Project,
    ) -> set[str]:
        """Return active current ACs not mapped by the confirmed Spec artifact.

        This is a structural traceability check.  It deliberately does not
        infer intent from AC prose: an AC either remains in the authoritative
        requirements contract and has a Spec mapping, or it must first be
        removed/retired by its owning planning stage.
        """
        current_ids = {
            str(item.get("id"))
            for item in cls._current_acceptance_criteria_for_project(project)
            if isinstance(item, dict) and item.get("id")
        }
        if not current_ids:
            return set()
        try:
            artifact = json.loads(
                (project_mac_dir(project) / "SPECS.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError):
            return current_ids
        mapped_ids = {
            str(ac_id)
            for spec in artifact.get("specs", [])
            if isinstance(spec, dict)
            for ac_id in (spec.get("acceptance_criterion_ids") or [])
            if ac_id
        }
        return current_ids - mapped_ids

    @classmethod
    def _work_item_acceptance_contracts(
        cls,
        project: Project,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Separate current delivery scope from inherited regression scope."""
        cumulative = cls._acceptance_criteria_for_project(project)
        current = cls._current_acceptance_criteria_for_project(project)
        if not current:
            return cumulative, []
        current_ids = {
            str(item.get("id"))
            for item in current
            if isinstance(item, dict) and item.get("id")
        }
        regression = [
            item
            for item in cumulative
            if str(item.get("id")) not in current_ids
        ]
        return current, regression

    @classmethod
    def _verification_acceptance_criteria_for_project(
        cls,
        project: Project,
    ) -> list[dict[str, Any]]:
        """Return criteria that require per-item evidence in this delivery.

        An iteration must prove every criterion introduced by the current
        change. Historical criteria remain a regression contract for the test
        suite, but requiring a fresh matrix row for every historical criterion
        turns a bounded iteration into a replay of the original project and can
        resurrect obsolete manual environments. Non-iteration workflows retain
        the cumulative contract because they define the product baseline.
        """
        cumulative = cls._acceptance_criteria_for_project(project)
        if str(getattr(project, "task_type", "")) != "iteration":
            return cumulative
        current = cls._current_acceptance_criteria_for_project(project)
        return current or cumulative

    @classmethod
    def _required_acceptance_criterion_ids(cls, project: Project) -> set[str]:
        return {
            str(item["id"])
            for item in cls._verification_acceptance_criteria_for_project(project)
            if isinstance(item, dict) and item.get("id")
        }

    @classmethod
    def _external_manual_acceptance_criterion_ids(cls, project: Project) -> set[str]:
        """Return only criteria whose facts genuinely live outside MAC.

        Missing evidence-source metadata defaults to system-observable.  The
        Acceptance Agent may still classify a concrete device/account fact as
        external in its structured output, but a bare ``manual`` verification
        method must never by itself transfer MAC's testing work to the user.
        """
        return {
            str(item["id"])
            for item in cls._verification_acceptance_criteria_for_project(project)
            if isinstance(item, dict)
            and item.get("id")
            and str(item.get("evidence_source") or "").strip().lower()
            in {"user_preference", "authorization", "external_fact"}
        }

    @classmethod
    def _acceptance_stage_manual_criterion_ids(cls, project: Project) -> set[str]:
        """Return real-entry criteria intentionally verified at acceptance.

        Both ``manual`` and ``e2e`` can require a real browser/app/chat entry.
        Tester may execute either when capable; otherwise a complete skipped
        black-box contract is handed to the Acceptance Agent. Preferences,
        authorizations, and external facts still belong to the user.
        """
        return {
            str(item["id"])
            for item in cls._verification_acceptance_criteria_for_project(project)
            if isinstance(item, dict)
            and item.get("id")
            and str(item.get("verification_method") or "").strip().lower()
            in {"manual", "e2e"}
        }

    @staticmethod
    def _is_pending_external_manual_evidence(
        item: dict[str, Any], manual_ids: set[str]
    ) -> bool:
        criterion_ids = {
            str(value) for value in (item.get("acceptance_criterion_ids") or []) if value
        }
        return bool(
            item.get("type") in {"manual", "e2e"}
            and item.get("result") == "skipped"
            and criterion_ids
            and criterion_ids.issubset(manual_ids)
            and str(item.get("command_or_method") or "").strip()
            and str(item.get("expected") or "").strip()
        )

    @staticmethod
    def _evidence_applies_to_active_contract(
        item: dict[str, Any],
        active_ids: set[str],
    ) -> bool:
        """Return whether an evidence row can affect the current verdict.

        Rows without trace IDs stay in scope conservatively. A row explicitly
        linked only to inherited regression criteria remains useful diagnostic
        evidence, but it cannot fail or pause the bounded current iteration.
        """
        criterion_ids = {
            str(value)
            for value in (item.get("acceptance_criterion_ids") or [])
            if value
        }
        return not criterion_ids or bool(criterion_ids & active_ids)

    def _tester_only_external_manual_pending(
        self,
        project: Project,
        output: dict[str, Any],
        runtime_tool_calls: list[dict[str, Any]],
    ) -> bool:
        """Recognize a clean internal pass awaiting only real-user QA.

        A conforming Tester may already return ``complete/passed`` for this
        hand-off, while older prompts returned ``fail/not passed`` and relied
        on normalization.  Both shapes describe the same stage boundary.  Do
        not make the already-correct shape re-enter feedback-closure checks
        that include Acceptance-owned packets.
        """
        if output.get("next_state_event") not in {"complete", "fail"}:
            return False
        if output.get("passed") not in {True, False}:
            return False
        if any(
            isinstance(item, dict) and item.get("blocking") is not False
            for item in (output.get("failures") or [])
        ):
            return False
        active_ids = self._required_acceptance_criterion_ids(project)
        all_evidence = [
            item
            for item in (output.get("test_evidence") or [])
            if isinstance(item, dict)
        ]
        evidence = [
            item
            for item in all_evidence
            if self._evidence_applies_to_active_contract(item, active_ids)
        ]
        manual_ids = (
            self._acceptance_stage_manual_criterion_ids(project)
            | self._pending_acceptance_evidence_ids(evidence)
        )
        uncovered = {
            str(value)
            for value in (output.get("uncovered_acceptance_criterion_ids") or [])
            if value
        } & active_ids
        if not evidence or (uncovered and not uncovered.issubset(manual_ids)):
            return False
        pending_manual_ids: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                return False
            if item.get("result") == "pass":
                continue
            if not self._is_pending_external_manual_evidence(item, manual_ids):
                return False
            pending_manual_ids.update(
                str(value)
                for value in (item.get("acceptance_criterion_ids") or [])
                if value
            )
        if not pending_manual_ids or not uncovered.issubset(pending_manual_ids):
            return False
        return self._tester_evidence_error(
            project,
            all_evidence,
            runtime_tool_calls,
            output.get("verified_feedback_ids") or [],
            require_feedback_closure=False,
        ) is None

    def _reviewer_only_external_manual_pending(
        self,
        project: Project,
        output: dict[str, Any],
        runtime_tool_calls: list[dict[str, Any]],
    ) -> bool:
        """Recognize a clean review awaiting only final real-user QA."""
        if output.get("next_state_event") not in {"complete", "fail"}:
            return False
        # A correct Reviewer commonly returns pass while leaving an explicit
        # partially-covered row for the future Acceptance stage.  The handoff
        # recognizer must run for both that normal shape and a contradictory
        # fail shape; stale artifacts remain disqualifying in either case.
        if output.get("stale_artifact_ids"):
            return False
        if not self._runtime_observed_read(runtime_tool_calls):
            return False
        manual_ids = (
            self._acceptance_stage_manual_criterion_ids(project)
            | self._latest_tester_acceptance_handoff_ids(project)
        )
        coverage = output.get("requirement_coverage") or []
        if not coverage:
            return False
        blocking_issues = [
            item
            for item in (output.get("issues") or [])
            if isinstance(item, dict) and item.get("blocking")
        ]
        blocking_requests = [
            item
            for item in (output.get("blockers") or [])
            if isinstance(item, dict) and item.get("blocking")
        ]

        # A failing review must identify an actionable blocking issue.  If all
        # rows are already covered or explicitly handed to the next stage and
        # the Reviewer reports no blocker/stale artifact, its fail bit is a
        # contradictory verdict rather than a reason to re-run Tester.  This
        # rule is based on the structured contract, not wording or AC names.
        if (
            not blocking_issues
            and not blocking_requests
            and all(
                isinstance(item, dict)
                and item.get("status") in {"covered", "partially_covered"}
                and (
                    item.get("status") == "covered"
                    or bool(item.get("evidence"))
                )
                for item in coverage
            )
        ):
            return True

        pending_manual: set[str] = set()
        for item in coverage:
            if not isinstance(item, dict):
                return False
            criterion_id = str(item.get("acceptance_criterion_id") or "")
            status = str(item.get("status") or "")
            if status == "covered":
                continue
            if criterion_id not in manual_ids or status != "partially_covered":
                return False
            if not item.get("evidence"):
                return False
            pending_manual.add(criterion_id)
        if not pending_manual:
            return False

        # A Reviewer may incorrectly label the absence of *future*
        # Acceptance-Agent evidence as a blocking test issue.  Stage ownership
        # is structural: a partially-covered manual/e2e criterion may proceed
        # only when every reported blocker is an evidence/test obligation tied
        # exclusively to those pending criteria.  Product, implementation, or
        # unscoped blockers still fail normally.
        if any(
            str(item.get("category") or "").strip().lower()
            not in {"tests", "test", "verification", "evidence"}
            for item in blocking_issues
        ):
            return False
        for blocker in blocking_requests:
            if not isinstance(blocker, dict) or not blocker.get("blocking"):
                continue
            affected_ids = {
                str(value)
                for value in (
                    blocker.get("affected_acceptance_criterion_ids") or []
                )
                if value
            }
            if not affected_ids or not affected_ids.issubset(pending_manual):
                return False
            if str(blocker.get("category") or "").strip().lower() not in {
                "quality_failure",
                "tests",
                "test",
                "verification",
                "evidence",
            }:
                return False
        return True

    @staticmethod
    def _pending_acceptance_evidence_ids(
        evidence: list[dict[str, Any]],
    ) -> set[str]:
        """Return criteria with a complete real-entry Acceptance handoff.

        Stage ownership comes from the structured evidence row, not solely
        from a planning-time verification label.  A criterion may combine
        automated checks with a later independent Acceptance browser pass; in
        that case its top-level method can legitimately remain
        ``automated_test`` while the pending row is explicitly ``e2e``.
        """
        ids: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                continue
            if (
                item.get("type") not in {"manual", "e2e"}
                or item.get("result") != "skipped"
                or not str(item.get("command_or_method") or "").strip()
                or not str(item.get("expected") or "").strip()
            ):
                continue
            ids.update(
                str(value)
                for value in (item.get("acceptance_criterion_ids") or [])
                if value
            )
        return ids

    def _latest_tester_acceptance_handoff_ids(self, project: Project) -> set[str]:
        """Read pending Acceptance-owned IDs from the latest Tester result."""
        task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "tester",
                Task.status == "completed",
            )
            .order_by(Task.created_at.desc())
        ).first()
        if task is None or not isinstance(task.output_data, dict):
            return set()
        evidence = [
            item
            for item in (task.output_data.get("test_evidence") or [])
            if isinstance(item, dict)
        ]
        return self._pending_acceptance_evidence_ids(evidence)

    @staticmethod
    def _feedback_packet_prompt_payload(
        feedback: FeedbackPacket,
    ) -> dict[str, Any]:
        route = FailureRouter.for_feedback(feedback)
        return {
            "feedback_id": feedback.id,
            "source_stage": feedback.source_stage,
            "source_task_id": feedback.source_task_id,
            "source_round": feedback.source_round,
            "kind": feedback.kind,
            "failure_class": feedback.failure_class,
            "summary": feedback.summary,
            "blocking": feedback.blocking,
            "status": feedback.status,
            "recommended_rework_target": feedback.recommended_rework_target,
            "evidence": feedback.evidence,
            "user_comment": feedback.user_comment,
            "failure_route": {
                "domain": route.domain,
                "target_stage": route.target_stage,
                "is_project_rework": route.is_project_rework,
                "signature": FailureRouter.signature(feedback),
            },
        }

    @staticmethod
    def _compact_feedback_prompt_payloads(
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Collapse equivalent failure rounds without losing stable IDs.

        Repeated quality attempts often produce the same routed failure. Agents
        need every ID for closure, but replaying every near-identical report
        bloats later prompts and encourages redundant verification. Structural
        FailureRouter signatures define equivalence; no prose keyword matching
        is involved.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for index, payload in enumerate(payloads):
            route = payload.get("failure_route")
            signature = (
                str(route.get("signature") or "")
                if isinstance(route, dict)
                else ""
            )
            trace_ids = extract_acceptance_criterion_ids(
                payload.get("evidence") or []
            )
            # Only compact independently produced quality rounds when their
            # structured AC trace proves they describe the same obligation.
            # User corrections commonly share source/target and have no AC
            # metadata; grouping those by an empty structural signature drops
            # one correction's actual content and can send Coder to the wrong
            # defect entirely.
            can_compact = bool(
                payload.get("kind") == "quality_failure"
                and trace_ids
                and signature
            )
            key = signature if can_compact else f"ungrouped:{index}"
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(payload)

        compacted: list[dict[str, Any]] = []
        for key in order:
            items = grouped[key]
            representative = max(
                items,
                key=lambda item: int(item.get("source_round") or 0),
            )
            merged = dict(representative)
            related = [
                str(item.get("feedback_id"))
                for item in items
                if item.get("feedback_id")
                and item.get("feedback_id") != representative.get("feedback_id")
            ]
            if related:
                merged["related_feedback_ids"] = related
                merged["equivalent_round_count"] = len(items)
            compacted.append(merged)
        return compacted

    def _quality_feedback_superseded_by_requirements_scope_change(
        self,
        project_id: str,
    ) -> set[str]:
        """Exclude quality failures that belong to a superseded delivery scope.

        A Requirements-owned scope change creates a new acceptance contract.
        Earlier Tester/Reviewer failures were judgments against the previous
        contract, so replaying them into the new implementation resurrects
        retired work.  User corrections are preserved: they carry durable
        product facts and may still apply to retained criteria.  Current
        quality agents will produce fresh failures against the new scope.
        """
        scope_changes = [
            item
            for item in self.project_service.get_open_feedback_packets(project_id)
            if item.kind == "scope_change"
            and str(item.recommended_rework_target or "").strip()
            == "requirements"
        ]
        if not scope_changes:
            return set()
        boundary = max(item.created_at for item in scope_changes)
        return {
            item.id
            for item in self.project_service.get_open_feedback_packets(project_id)
            if item.kind == "quality_failure" and item.created_at < boundary
        }

    @staticmethod
    def _feedback_relevant_to_state(state: str, feedback: FeedbackPacket) -> bool:
        """Route feedback through the WorkItem instead of broadcasting it.

        Quality agents need the complete open set so they can independently
        verify closure.  Implementation/planning agents receive feedback with
        no explicit owner plus feedback assigned to their current stage.
        """
        if state in {"TESTING", "REVIEW"}:
            return True
        target = str(feedback.recommended_rework_target or "").strip().lower()
        if (
            not target
            and str(feedback.failure_class or "").strip().lower()
            == "environment_failure"
        ):
            # Missing browser/service infrastructure is verification work, not
            # an unowned implementation defect.  Broadcasting this packet to
            # Coder makes completion impossible: Coder cannot honestly mark a
            # Coordinator-owned E2E obligation as fixed.
            return False
        if not target:
            return True
        state_target = {
            "PLANNING_REQUIREMENTS": "requirements",
            "PLANNING_ARCHITECTURE": "architecture",
            "PLANNING_IMPLEMENTATION_SPECS": "specs",
            "CODING": "coding",
        }.get(state)
        return state_target is None or target == state_target

    def _feedback_needed_for_work_item(
        self,
        project_id: str,
        state: str,
        feedback: FeedbackPacket,
        state_machine: StateMachine,
        routed_feedback_ids: set[str],
    ) -> bool:
        """Separate implementation ownership from verification lifecycle."""
        if feedback.id in routed_feedback_ids:
            return True
        if not self._feedback_relevant_to_state(state, feedback):
            return False
        if state in {"TESTING", "REVIEW", "REWORK"}:
            return True
        return not self._feedback_owner_already_addressed(
            project_id, feedback, state_machine
        )

    def _feedback_owner_already_addressed(
        self,
        project_id: str,
        feedback: FeedbackPacket,
        state_machine: StateMachine,
    ) -> bool:
        """Return whether the assigned owner has already handled a correction.

        A routed correction intentionally remains open until an independent
        quality Agent verifies the fix.  That open status must not be confused
        with "the assigned owner has never handled it": after the owner reports
        the packet as fixed (or already satisfied), returning to the source
        stage must continue delivery instead of routing to the owner forever.
        """
        target = str(feedback.recommended_rework_target or "").strip()
        if not target:
            return False
        owner_state = next(
            (
                str(stage.get("execution_state") or "")
                for stage in state_machine.profile.get("stages", [])
                if str(stage.get("id") or "") == target
            ),
            "",
        )
        if not owner_state:
            return False
        owner_tasks = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.state == owner_state,
                Task.status == "completed",
                Task.created_at >= feedback.created_at,
            )
            .order_by(Task.created_at.desc())
        ).all()
        for owner_task in owner_tasks:
            output = (
                owner_task.output_data
                if isinstance(owner_task.output_data, dict)
                else {}
            )
            if any(
                isinstance(item, dict)
                and str(item.get("feedback_id") or "") == feedback.id
                and item.get("status") in {"fixed", "not_applicable"}
                for item in (output.get("addressed_feedback") or [])
            ):
                return True
            assigned_ids = {
                str(value)
                for value in (
                    (owner_task.input_data or {})
                    .get("work_item_contract", {})
                    .get("feedback_refs", [])
                    or []
                )
                if value
            }
            if feedback.id in assigned_ids:
                # Routing and verification have different lifecycles.  Once
                # the assigned owner has actually received this packet and
                # completed a turn, its routing directive is spent even when
                # the packet remains open for independent downstream proof.
                # A later failure gets its own packet and Coordinator route;
                # replaying this older directive can otherwise skip newly
                # revised upstream artifacts and their implementation stages.
                return True
        return False

    def _tester_evidence_error(
        self,
        project: Project,
        evidence: list[dict[str, Any]],
        runtime_tool_calls: list[dict[str, Any]],
        verified_feedback_ids: list[str],
        *,
        require_feedback_closure: bool = True,
    ) -> str | None:
        """Validate test claims against declared verification methods and runtime.

        The Tester chooses prose, but it cannot redefine an AC's required
        verification method or claim automated execution without an observed
        Bash call in the runtime transcript.
        """
        automated_types = {"unit", "integration", "e2e", "failure_injection"}
        if any(item.get("type") in automated_types for item in evidence):
            if not self._runtime_observed_command(runtime_tool_calls):
                return "automated test evidence has no runtime-observed Bash execution"

        required_methods = {
            str(item.get("id")): str(item.get("verification_method"))
            for item in self._verification_acceptance_criteria_for_project(project)
            if isinstance(item, dict)
            and item.get("id")
            and item.get("verification_method")
        }
        if not required_methods:
            return "cannot verify evidence methods because no acceptance artifact is available"
        allowed_types = {
            "automated_test": {"unit", "integration", "e2e", "failure_injection"},
            "integration_test": {"integration", "e2e"},
            "e2e": {"e2e"},
            "manual": {"manual"},
            "inspection": {"inspection"},
            "metric": {"integration", "e2e"},
        }
        deferred_ids = self._explicitly_deferred_acceptance_criterion_ids(project)
        external_manual_ids = self._external_manual_acceptance_criterion_ids(project)
        acceptance_stage_ids = (
            self._acceptance_stage_manual_criterion_ids(project)
            | self._pending_acceptance_evidence_ids(evidence)
        )
        passing_by_ac: dict[str, set[str]] = {}
        pending_acceptance_by_ac: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                continue
            if self._is_pending_external_manual_evidence(item, acceptance_stage_ids):
                pending_acceptance_by_ac.update(
                    str(ac_id)
                    for ac_id in (item.get("acceptance_criterion_ids") or [])
                    if ac_id
                )
            if item.get("result") != "pass":
                continue
            evidence_type = str(item.get("type") or "")
            for ac_id in item.get("acceptance_criterion_ids") or []:
                passing_by_ac.setdefault(str(ac_id), set()).add(evidence_type)

        mismatches: list[str] = []
        for ac_id, method in required_methods.items():
            if ac_id in deferred_ids or ac_id in external_manual_ids:
                continue
            observed = passing_by_ac.get(ac_id, set())
            # A real-entry manual/e2e criterion may be intentionally left for
            # the Acceptance Agent. Internal unit/integration evidence may
            # coexist with that hand-off: it supports the implementation but
            # cannot replace the explicit skipped real-entry contract. The
            # presence of lower-level passing evidence must not accidentally
            # turn an otherwise valid hand-off into a method mismatch.
            if ac_id in pending_acceptance_by_ac:
                continue
            accepted = allowed_types.get(method, set())
            if not observed.intersection(accepted):
                mismatches.append(
                    f"{ac_id} requires {method}, observed {sorted(observed) or ['none']}"
                )
        if mismatches:
            return "test evidence does not satisfy AC verification methods: " + "; ".join(
                mismatches[:12]
            )

        verified_ids = {str(value) for value in verified_feedback_ids if value}
        if require_feedback_closure:
            open_blocking_feedback_ids = {
                item.id
                for item in self.project_service.get_open_feedback_packets(project.id)
                if item.blocking
            }
            unverified = sorted(open_blocking_feedback_ids - verified_ids)
            if unverified:
                return (
                    "tester pass leaves open blocking feedback without verification: "
                    + ", ".join(unverified)
                )
        return self._verified_feedback_evidence_error(
            evidence, runtime_tool_calls, verified_feedback_ids
        )

    def _verified_feedback_evidence_error(
        self,
        evidence: list[dict[str, Any]],
        runtime_tool_calls: list[dict[str, Any]],
        verified_feedback_ids: list[str],
    ) -> str | None:
        """Validate feedback closure independently of the overall quality verdict.

        A Tester may prove several earlier fixes and still fail because a new,
        unrelated blocker remains.  Those proven packets should not be carried
        into every later prompt, but closure still requires an explicit passing
        evidence link and observable execution for automated evidence.
        """
        verified_ids = {str(value) for value in verified_feedback_ids if value}
        if not verified_ids:
            return None
        supporting_evidence = [
            item
            for item in evidence
            if isinstance(item, dict)
            and item.get("result") == "pass"
            and verified_ids.intersection(
                str(value) for value in (item.get("feedback_ids") or []) if value
            )
        ]
        evidence_feedback_ids = {
            str(feedback_id)
            for item in supporting_evidence
            for feedback_id in (item.get("feedback_ids") or [])
            if feedback_id
        }
        unsupported = sorted(verified_ids - evidence_feedback_ids)
        if unsupported:
            return (
                "verified_feedback_ids lack passing test evidence links: "
                + ", ".join(unsupported)
            )
        automated_types = {"unit", "integration", "e2e", "failure_injection"}
        if (
            any(item.get("type") in automated_types for item in supporting_evidence)
            and not self._runtime_observed_command(runtime_tool_calls)
        ):
            return (
                "verified_feedback_ids rely on automated evidence without a "
                "runtime-observed Bash execution"
            )
        return None

    def _resolve_incoherent_tester_feedback(self, project_id: str) -> None:
        """Close quality packets created from a contradictory empty fail.

        Older runs created an open ``test_bug`` packet before self-retrying even
        when the output contained only passing evidence and no failure item.
        Those packets describe no actionable defect and can mislead every later
        Tester into repeating already completed diagnostics.
        """
        packets = self.project_service.get_open_feedback_packets(project_id)
        changed = False
        for packet in packets:
            task = (
                self.project_service.session.get(Task, packet.source_task_id)
                if packet.source_task_id
                else None
            )
            output = task.output_data if task and isinstance(task.output_data, dict) else {}
            if packet.source_stage == "REVIEW" and packet.kind == "quality_failure":
                if self._clean_reviewer_handoff_output(output):
                    packet.status = "resolved"
                    packet.resolved_by_task_id = task.id if task else None
                    packet.updated_at = datetime.utcnow()
                    self.project_service.session.add(packet)
                    changed = True
                    continue
            if not (
                packet.source_stage == "TESTING"
                and packet.kind == "quality_failure"
                and packet.failure_class == "test_bug"
                and packet.source_task_id
            ):
                continue
            evidence = [
                item for item in (output.get("test_evidence") or [])
                if isinstance(item, dict)
            ]
            blocking_failures = [
                item for item in (output.get("failures") or [])
                if isinstance(item, dict) and item.get("blocking") is not False
            ]
            if (
                output.get("next_state_event") == "fail"
                and output.get("passed") is False
                and evidence
                and all(item.get("result") == "pass" for item in evidence)
                and not blocking_failures
            ):
                packet.status = "resolved"
                packet.resolved_by_task_id = task.id if task else None
                packet.updated_at = datetime.utcnow()
                self.project_service.session.add(packet)
                changed = True
        if changed:
            self.project_service.session.commit()

    @staticmethod
    def _clean_reviewer_handoff_output(output: dict[str, Any]) -> bool:
        """Return true for a fail bit unsupported by any blocking evidence."""
        coverage = [
            item
            for item in (output.get("requirement_coverage") or [])
            if isinstance(item, dict)
        ]
        return bool(
            output.get("next_state_event") == "fail"
            and output.get("result") == "fail"
            and not output.get("stale_artifact_ids")
            and not any(
                isinstance(item, dict) and item.get("blocking")
                for item in (output.get("issues") or [])
            )
            and not any(
                isinstance(item, dict) and item.get("blocking")
                for item in (output.get("blockers") or [])
            )
            and coverage
            and all(
                item.get("status") in {"covered", "partially_covered"}
                and (
                    item.get("status") == "covered"
                    or bool(item.get("evidence"))
                )
                for item in coverage
            )
        )

    @staticmethod
    def _runtime_observed_command(tool_calls: list[dict[str, Any]]) -> bool:
        """Recognize shell execution across supported coding runtimes.

        Claude Code reports its shell tool as ``Bash`` while Codex emits the
        provider-neutral event type ``command_execution``. Quality evidence is
        about the capability that ran, not a provider-specific tool label.
        """
        return any(
            isinstance(item, dict)
            and str(item.get("name") or "") in {"Bash", "command_execution"}
            for item in tool_calls
        )

    @staticmethod
    def _runtime_observed_read(tool_calls: list[dict[str, Any]]) -> bool:
        """Recognize repository inspection across supported coding runtimes."""
        return any(
            isinstance(item, dict)
            and str(item.get("name") or "") in {"Read", "command_execution"}
            for item in tool_calls
        )

    async def _handle_agent_output(
        self,
        project: Project,
        state_machine: StateMachine,
        task: Task,
        worktree: Path,
        output: dict[str, Any],
        runtime_tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        state = project.current_state
        event = output["next_state_event"]
        feedback_packet_id: str | None = None
        # A quality failure with no executable rework target is a resolution
        # request, not a reason to make the user guess a delivery stage.  Turn
        # the structured blocking evidence into the normal Agent ->
        # Coordinator clarification protocol.  The Coordinator may answer,
        # route upstream, retry with an internal capability, or escalate the
        # smallest genuinely external question to the user.
        if (
            state in {"TESTING", "REVIEW"}
            and output.get("next_state_event") == "fail"
            and output.get("recommended_rework_target") in {None, "", "none"}
        ):
            blocking_items = [
                item
                for item in (
                    output.get("failures")
                    if state == "TESTING"
                    else output.get("issues")
                )
                or []
                if isinstance(item, dict) and item.get("blocking") is not False
            ]
            if blocking_items:
                questions: list[dict[str, Any]] = []
                resolution_requests: list[dict[str, Any]] = []
                for index, item in enumerate(blocking_items[:3], start=1):
                    item_id = str(item.get("id") or f"quality_blocker_{index}")
                    required = str(
                        item.get("required_resolution")
                        or item.get("expected")
                        or "Provide the unavailable prerequisite or decision."
                    ).strip()
                    reason = str(
                        item.get("error")
                        or item.get("summary")
                        or item.get("actual")
                        or "The quality Agent cannot complete this check with current context."
                    ).strip()
                    affected_ids = [
                        str(value)
                        for value in (
                            item.get("affected_acceptance_criterion_ids")
                            or item.get("acceptance_criterion_ids")
                            or []
                        )
                        if value
                    ]
                    questions.append(
                        {
                            "question_id": f"resolve_{item_id}",
                            "decision_key": (
                                f"quality.{str(task.agent_type).lower()}."
                                + hashlib.sha256(
                                    f"{state}|{item_id}".encode("utf-8")
                                ).hexdigest()[:16]
                            ),
                            "kind": "blocking",
                            "question": required,
                            "reason": reason,
                            "allow_free_text": True,
                            "options": [
                                {
                                    "key": "resolved",
                                    "label": "已完成所需处理",
                                    "description": "确认外部前置条件已经满足后继续验证。",
                                }
                            ],
                            "affected_trace_ids": affected_ids,
                        }
                    )
                    resolution_requests.append(
                        {
                            "id": item_id,
                            "category": str(
                                output.get("failure_class") or "unresolved_blocker"
                            ),
                            "summary": reason,
                            "evidence": [reason],
                            "required_resolution": required,
                            "preferred_resolver": "coordinator",
                            "blocking": True,
                            "affected_acceptance_criterion_ids": affected_ids,
                        }
                    )
                output["question_batch"] = {
                    "batch_key": f"{state.lower()}_{task.id}_resolution",
                    "questions": questions,
                }
                output["blockers"] = resolution_requests
                event = "ask_user"
        # An output carrying real questions is an escalation, not a quality
        # verdict — route it to the unified ask_user path below instead of
        # converting it into a bogus failure FeedbackPacket.
        has_questions = bool(
            (output.get("question_batch") or {}).get("questions")
            if isinstance(output.get("question_batch"), dict)
            else False
        )

        # Quality gates: only an explicit pass is allowed to move forward.
        # A failing assessment is recorded as a structured FeedbackPacket and
        # routed to REWORK via the state machine's "fail" event.
        if state == "TESTING":
            verified_feedback_ids = output.get("verified_feedback_ids", [])
            verification_error = self._verified_feedback_evidence_error(
                output.get("test_evidence") or [],
                runtime_tool_calls or [],
                verified_feedback_ids,
            )
            if verification_error:
                logger.warning(
                    "Ignoring unsupported feedback closure from Tester task %s: %s",
                    task.id,
                    verification_error,
                )
            else:
                # Feedback closure is intentionally independent of the overall
                # Tester verdict.  A new blocker must not keep already-proven
                # fixes open and inflate every later Agent prompt.
                await self._resolve_verified_feedback(
                    verified_feedback_ids, task.id, project.id
                )
            if output.get("passed") is not True and not has_questions:
                event = "fail"
                packet = await self._create_feedback_from_tester_output(project, task, output)
                feedback_packet_id = packet.id if packet else None
                if self._tester_failure_is_self_retriable(output):
                    retry_counts = dict(
                        project.context.get("quality_self_retry_counts", {})
                    )
                    retry_count = int(retry_counts.get(state, 0) or 0)
                    if retry_count < 1:
                        retry_counts[state] = retry_count + 1
                        project.context = {
                            **project.context,
                            "quality_self_retry_counts": retry_counts,
                            "_resumable_agent_worktree": {
                                "task_id": task.id,
                                "agent_type": task.agent_type,
                                "state": state,
                                "worktree_path": str(worktree),
                                "mode": "quality_self_retry",
                            },
                        }
                        # Mark this completed result as applied before the
                        # recursive retry starts.  Otherwise _execute_agent()
                        # sees task.updated_at > project.updated_at, mistakes
                        # the same task for crash recovery, applies it twice,
                        # creates duplicate feedback, and consumes the retry
                        # window without ever launching a new Tester.
                        project.updated_at = datetime.utcnow()
                        self.project_service.session.commit()
                        await self.project_service.log_event(
                            project_id=project.id,
                            level="INFO",
                            message=(
                                "Automatically retrying Tester-owned test harness "
                                "failure before asking the user"
                            ),
                            meta={
                                "task_id": task.id,
                                "feedback_packet_id": feedback_packet_id,
                                "retry_count": retry_count + 1,
                            },
                        )
                        refreshed = self.project_service.require_project(project.id)
                        await self._execute_agent(refreshed, state_machine)
                        return
            else:
                retry_counts = dict(
                    project.context.get("quality_self_retry_counts", {})
                )
                if retry_counts.pop(state, None) is not None:
                    project.context = {
                        **project.context,
                        "quality_self_retry_counts": retry_counts,
                    }
                    self.project_service.session.commit()
        elif state == "REVIEW":
            issues = output.get("issues", [])
            blocking_issue = any(issue.get("blocking") for issue in issues)
            if (output.get("result") != "pass" or blocking_issue) and not has_questions:
                event = "fail"
                packet = await self._create_feedback_from_reviewer_output(project, task, output)
                feedback_packet_id = packet.id if packet else None
            else:
                await self._resolve_verified_feedback(
                    output.get("verified_feedback_ids", []), task.id, project.id
                )

        # Planning-stage guard: only ambiguities explicitly marked as blocking
        # may be promoted to a user question.  The planner artifact schemas also
        # carry informational ambiguities/open questions, and treating those as
        # blockers creates unnecessary approval loops.
        ambiguity_event = self._block_on_unresolved_ambiguities(project, state, worktree, output)
        if ambiguity_event:
            event = ambiguity_event

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
                ctx["_resumable_agent_worktree"] = {
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "state": state,
                    "worktree_path": str(worktree),
                    "interaction_id": interaction.id,
                }
                project.context = ctx
                self.project_service.session.commit()
                unresolved = await self._mediate_agent_questions(
                    project,
                    task,
                    state,
                    question_batch,
                    interaction,
                    output.get("blockers") or [],
                )
                if not unresolved:
                    await self.project_service.resolve_question_interaction(interaction.id)
                    await self.project_service.log_event(
                        project_id=project.id,
                        level="INFO",
                        message="Coordinator resolved all agent questions",
                        meta={"interaction_id": interaction.id, "task_id": task.id},
                    )
                    route_event = self._consume_coordinator_route(project)
                    if route_event:
                        await self.project_service.transition_state(
                            project.id,
                            route_event,
                            triggered_by="conversation_coordinator",
                        )
                    refreshed = self.project_service.require_project(project.id)
                    await self._execute_agent(refreshed, state_machine)
                    return

                interaction.escalated_to_user = True
                self.project_service.session.add(interaction)
                ctx = dict(project.context)
                ctx["_pending_question_interaction_id"] = interaction.id
                project.context = ctx
                self.project_service.session.commit()
                event = "ask_user"
            elif suppressed_questions:
                # Every question in this batch already has an authoritative
                # answer. Treat the Coordinator/history resolution as part of
                # this turn and continue; retrying the Agent here creates an
                # infinite loop when an artifact retains informational
                # ambiguities with accepted default assumptions.
                output.pop("question_batch", None)
                output["next_state_event"] = "complete"
                event = "complete"
                ctx = dict(project.context)
                ctx.pop("question_guard_feedback", None)
                project.context = ctx
                self.project_service.session.commit()
                await self.project_service.log_event(
                    project_id=project.id,
                    level="INFO",
                    message="Resolved duplicate agent questions from confirmed decisions",
                    meta={"task_id": task.id, "questions": suppressed_questions},
                )

        if not question_batch and project.context.get("question_guard_feedback"):
            ctx = dict(project.context)
            ctx.pop("question_guard_feedback", None)
            project.context = ctx
            self.project_service.session.commit()

        if event == "complete":
            expired_questions = (
                self.project_service.expire_superseded_question_interactions(
                    project.id,
                    agent=task.agent_type,
                    resume_state=state,
                    completed_task_id=task.id,
                )
            )
            if expired_questions:
                await self.project_service.log_event(
                    project_id=project.id,
                    level="INFO",
                    message="Expired questions superseded by successful Agent rerun",
                    meta={
                        "task_id": task.id,
                        "state": state,
                        "count": expired_questions,
                    },
                )

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
            artifact_error = self._planner_artifact_error(
                project,
                worktree,
                state,
                expected_round=task.round_number,
            )
            if artifact_error:
                await self.project_service.fail_task(task.id, artifact_error)
                await self._handle_agent_failure(project, state, task, artifact_error)
                return
            if state == "PLANNING_REQUIREMENTS":
                reference_error = self._reference_contract_artifact_error(
                    project, worktree
                )
                if reference_error:
                    await self.project_service.fail_task(task.id, reference_error)
                    await self._handle_agent_failure(
                        project, state, task, reference_error
                    )
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
                            # LoopSupervisor conclusions describe the prior
                            # delivery contract.  Once an upstream artifact is
                            # revised, keeping its prohibited route/provider
                            # can force Coordinator to reject the very path the
                            # new contract just made valid.
                            await self._invalidate_loop_supervisor_after_artifact_revision(
                                project,
                                artifact_id=artifact_id,
                                previous_version=previous.get("version"),
                                new_version=new_version,
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
                # A successful merge supersedes every earlier failed/answered
                # attempt for this project. Remove their legacy worktrees too;
                # visible ``worktrees/`` directories otherwise leak stale source
                # into framework builds (notably Next.js type checking).
                self.worktree_manager.remove_all_worktrees(
                    project.id, workspace_path=project.workspace_path
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove project worktrees after merge: %s", e
                )
            if state == "CODING" and self._coder_changed_delivery_artifacts(output):
                try:
                    delivery_revision = str(
                        self.worktree_manager.workspace_fingerprint(
                            project.workspace_path
                        ).get("digest") or ""
                    )
                except Exception:
                    delivery_revision = "unavailable"
                project.context = RuntimeManager.mark_artifact_revision(
                    project.context, delivery_revision
                )
                self.project_service.session.add(project)
                self.project_service.session.commit()
                affected_criteria = self._acceptance_criteria_affected_by_task(
                    project, task
                )
                self.project_service.invalidate_acceptance_evidence(
                    project.id,
                    reason=f"实现已由任务 {task.id} 更新，需要对新产物重新取证",
                    criterion_ids=affected_criteria or None,
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
        new_state_type = state_machine.state_type(new_state)
        if new_state_type == "human_input":
            await self._handle_human_input(project, state_machine, auto_resolve=False)
        elif new_state_type == "human_confirmation":
            # Agent completion may transition directly to a confirmation state
            # (e.g., PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING).  If
            # we return without ticking, the approval card is never sent and
            # the project appears stuck until an external caller ticks again.
            #
            # Only invoke _handle_human_confirmation when the state requires
            # manual confirmation.  In auto/night mode, _handle_human_confirmation
            # auto-resolves and transitions state (orchestrator.py:2148-2197);
            # calling it here would cascade through multiple stages in a single
            # tick.  Auto-mode states are handled by the next tick's normal
            # dispatch.  _handle_human_confirmation is idempotent (reuses
            # existing pending approval), so invoking it here is safe even if
            # the card already exists.
            if self._state_requires_confirmation(
                new_state, new_state_type, project.approval_config
            ):
                await self._handle_human_confirmation(project, state_machine)

        # Clean up the now-unneeded worktree for non-merge states.
        if state not in ("CODING", "TESTING", "REVIEW") and event != "ask_user":
            try:
                self.worktree_manager.remove_worktree(
                    project.id, task.id, task.agent_type, worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception as e:
                logger.warning(
                    "Failed to remove worktree %s after transition: %s", worktree, e
                )

    async def _invalidate_loop_supervisor_after_artifact_revision(
        self,
        project: Project,
        *,
        artifact_id: str,
        previous_version: Any,
        new_version: Any,
    ) -> bool:
        """Discard loop policy derived from a superseded delivery contract."""
        if not project.context.get("loop_supervisor"):
            return False
        next_context = dict(project.context)
        next_context.pop("loop_supervisor", None)
        project.context = next_context
        self.project_service.session.add(project)
        self.project_service.session.commit()
        await self.project_service.log_event(
            project_id=project.id,
            level="INFO",
            message="Cleared LoopSupervisor after upstream artifact revision",
            meta={
                "artifact_id": artifact_id,
                "previous_version": previous_version,
                "new_version": new_version,
            },
        )
        return True

    def _find_resumable_agent_worktree(
        self,
        project: Project,
        state: str,
        agent_type: str,
    ) -> dict[str, str] | None:
        """Find an answered clarification run whose working tree is still valid."""
        candidate = project.context.get("_resumable_agent_worktree")
        candidates: list[dict[str, str]] = []
        if isinstance(candidate, dict):
            candidates.append(candidate)

        # Crash/restart fallback: reconstruct the continuation from durable
        # QuestionInteraction + Task records when context was not written by an
        # older server version.
        from mac_system.models import QuestionInteraction

        interactions = self.project_service.session.exec(
            select(QuestionInteraction)
            .where(
                QuestionInteraction.project_id == project.id,
                QuestionInteraction.status == "answered",
                QuestionInteraction.resume_state == state,
                QuestionInteraction.agent == agent_type,
            )
            .order_by(QuestionInteraction.answered_at.desc())
        ).all()
        for interaction in interactions:
            if not interaction.origin_task_id:
                continue
            origin = self.project_service.session.get(Task, interaction.origin_task_id)
            if origin is None or not origin.worktree_path:
                continue
            candidates.append(
                {
                    "task_id": origin.id,
                    "agent_type": origin.agent_type,
                    "state": origin.state,
                    "worktree_path": origin.worktree_path,
                    "interaction_id": interaction.id,
                }
            )

        seen: set[str] = set()
        for item in candidates:
            previous_task_id = str(item.get("task_id") or "")
            path = str(item.get("worktree_path") or "")
            if not previous_task_id or not path or previous_task_id in seen:
                continue
            seen.add(previous_task_id)
            if item.get("state") != state or item.get("agent_type") != agent_type:
                continue
            worktree = Path(path)
            if not worktree.exists():
                continue
            try:
                self.worktree_manager.validate_worktree_identity(
                    project.id,
                    previous_task_id,
                    agent_type,
                    worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception:
                continue
            return {
                "task_id": previous_task_id,
                "worktree_path": path,
                "mode": str(item.get("mode") or "answered_agent_clarification"),
            }
        return None

    @staticmethod
    def _compact_continuation_output(output: dict[str, Any]) -> dict[str, Any]:
        """Keep prior evidence useful without replaying an entire Agent artifact."""

        def bounded_text(value: Any, limit: int = 800) -> str:
            return redact_text(str(value or ""))[:limit]

        evidence = []
        for item in (output.get("test_evidence") or [])[:24]:
            if not isinstance(item, dict):
                continue
            # Passing evidence is immutable carry-forward state, not a reason
            # to replay commands and prose into the retry prompt. Preserve its
            # trace IDs and result; retain diagnostic detail only for evidence
            # that the continuation still needs to investigate.
            keys = {
                "id", "spec_id", "acceptance_criterion_ids", "feedback_ids",
                "type", "result",
            }
            if item.get("result") not in {"pass", "skipped"}:
                keys.update({"command_or_method", "expected", "actual"})
            evidence.append({
                key: (bounded_text(value, 500) if isinstance(value, str) else value)
                for key, value in item.items()
                if key in keys
            })
        failures = []
        for item in (output.get("failures") or [])[:20]:
            if not isinstance(item, dict):
                continue
            failures.append({
                key: (bounded_text(value) if isinstance(value, str) else value)
                for key, value in item.items()
                if key in {
                    "id", "test", "error", "expected", "actual", "file", "line",
                    "blocking",
                }
            })
        compact = {
            key: output.get(key)
            for key in (
                "success", "next_state_event", "passed", "failure_class",
                "recommended_rework_target", "coverage_status",
                "uncovered_acceptance_criterion_ids", "verified_feedback_ids",
            )
            if key in output
        }
        compact["test_evidence"] = evidence
        compact["failures"] = failures
        compact["report"] = bounded_text(output.get("report"), 700)
        compact["notes"] = [
            bounded_text(item, 300) for item in (output.get("notes") or [])[:4]
        ]
        # Apply the standard secret scrubber once more to the serialized
        # envelope before it is rendered into the next Agent prompt.
        return json.loads(redact_secrets(json.dumps(compact, ensure_ascii=False)))

    def _resolved_questions_for_task(
        self, project_id: str, origin_task_id: str
    ) -> list[dict[str, Any]]:
        """Return one-shot answers for the exact Agent continuation.

        Coordinator policy decisions, answers grounded in project facts, and
        user answers after escalation all belong to the originating question,
        even though only some of them become durable project facts.  Deliver
        the exact answer to the exact resumed task so the Agent cannot repeat
        an already-resolved blocker or silently choose a different branch.
        """

        # A service restart or bounded runtime retry can add continuation tasks
        # between the task that asked the question and the task that finally
        # resumes after it was answered.  Follow the explicit lineage rather
        # than requiring the immediately previous task to be the asker.
        lineage: list[str] = []
        cursor: str | None = origin_task_id
        while cursor and cursor not in lineage and len(lineage) < 16:
            lineage.append(cursor)
            prior_task = self.project_service.session.get(Task, cursor)
            prior_input = (
                prior_task.input_data
                if prior_task is not None and isinstance(prior_task.input_data, dict)
                else {}
            )
            cursor = str(prior_input.get("resumed_from_task_id") or "").strip() or None

        interactions = self.project_service.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.origin_task_id.in_(lineage),
                QuestionInteraction.status == "answered",
            )
        ).all()
        resolved: list[dict[str, Any]] = []
        for interaction in interactions:
            for question in self.project_service.get_questions_for_interaction(
                interaction.id
            ):
                answer = self.project_service.get_question_answer(question.id)
                if answer is None or answer.skipped:
                    continue
                source_refs = [
                    str(ref) for ref in (answer.source_refs or [])[:16]
                ]
                instruction = str(answer.answer or "").strip()
                if not instruction:
                    continue
                resolved.append({
                    "question_id": question.question_id,
                    "decision_key": question.decision_key,
                    "resolution_instruction": instruction,
                    "answer_source": answer.answer_source,
                    "source_refs": source_refs,
                })
        return json.loads(
            redact_secrets(json.dumps(resolved, ensure_ascii=False))
        )

    def _resolved_questions_for_continuation(
        self,
        project_id: str,
        origin_task_id: str,
        *,
        artifact_revision_changed: bool,
        continuation_mode: str,
    ) -> list[dict[str, Any]]:
        """Scope task answers to the artifact revision that made them valid.

        A question answer is an instruction for one continuation, not a durable
        project fact.  If an upstream artifact changed before the task resumed,
        its new contract supersedes those older instructions.  Confirmed facts
        survive separately through ``relevant_facts``.  Tester-owned retries do
        not cross an artifact boundary and retain their exact resolution.
        """
        if artifact_revision_changed and continuation_mode != "quality_self_retry":
            return []
        return self._resolved_questions_for_task(project_id, origin_task_id)

    @staticmethod
    def _tester_failure_is_self_retriable(output: dict[str, Any]) -> bool:
        """Return whether structured Tester output assigns the fault to itself.

        This deliberately uses the quality contract rather than error-text
        keywords. A Tester-owned harness defect can be repaired by another
        Tester attempt; implementation, environment, architecture and product
        contract failures still enter the normal REWORK decision.
        """
        repository_change_required = any(
            isinstance(item, dict) and str(item.get("file") or "").strip()
            for item in (output.get("failures") or [])
        )
        return (
            output.get("passed") is not True
            and output.get("failure_class") == "test_bug"
            and output.get("recommended_rework_target") == "testing"
            and not repository_change_required
        )

    async def _create_feedback_from_tester_output(
        self,
        project: Project,
        task: Task,
        output: dict[str, Any],
    ) -> Any:
        """Create a FeedbackPacket from a failing tester output."""
        existing = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.source_task_id == task.id,
                FeedbackPacket.kind == "quality_failure",
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
        ).first()
        if existing is not None:
            return existing
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
        existing = self.project_service.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.source_task_id == task.id,
                FeedbackPacket.kind == "quality_failure",
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
        ).first()
        if existing is not None:
            return existing
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
        return await self.project_service.create_question_interaction(
            project_id=project.id, origin_task_id=task.id, origin_round=task.round_number,
            resume_state=state, agent=agent, stage=stage, batch_key=batch_key,
            questions=questions,
        )

    async def _mediate_agent_questions(
        self,
        project: Project,
        task: Task,
        state: str,
        question_batch: dict[str, Any],
        interaction: Any,
        resolution_requests: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Let the Coordinator answer from durable facts before asking the user."""
        active_records = self.project_service.get_active_context_records(
            project.id,
            agent_type=task.agent_type,
            state=state,
        )
        serialized_records = [
            {
                "fact_id": record.id,
                "key": record.record_key,
                "type": record.record_type,
                "version": record.version,
                "value": record.value,
                "source_type": record.source_type,
                "source_ref": record.source_ref,
                "scope_type": record.scope_type,
                "scope_ref": record.scope_ref,
                "confidence": record.confidence,
                "sensitivity": record.sensitivity,
            }
            for record in active_records
        ]
        output = await self._execute_auxiliary_agent(
            project,
            "conversation_coordinator",
            input_data={
                "mode": "agent_clarification",
                "origin_task_id": task.id,
                "interaction_id": interaction.id,
            },
            context_files=[],
            stage="agent_clarification",
            extra_variables={
                "requesting_agent": task.agent_type,
                "question_batch": json.dumps(question_batch, ensure_ascii=False),
                "resolution_requests": json.dumps(
                    resolution_requests or [], ensure_ascii=False
                ),
                "active_context_records": json.dumps(
                    serialized_records, ensure_ascii=False
                ),
                "work_item_contract": json.dumps(
                    task.input_data.get("work_item_contract", {}), ensure_ascii=False
                ),
                "qa_history_json": json.dumps(
                    self._collect_qa_history(project.id), ensure_ascii=False
                ),
                "available_internal_routes": json.dumps(
                    self._available_coordinator_routes(project, state),
                    ensure_ascii=False,
                ),
            },
        )

        resolutions = self._normalize_coordinator_resolutions(output)
        by_question_id = {
            str(item.get("question_id")): item
            for item in resolutions
            if isinstance(item, dict) and item.get("question_id")
        }
        active_fact_ids = {record.id for record in active_records}
        db_questions = {
            question.question_id: question
            for question in self.project_service.get_questions_for_interaction(
                interaction.id
            )
        }
        external_manual_ids = self._external_manual_acceptance_criterion_ids(project)
        resolution_requests = [
            item for item in (resolution_requests or []) if isinstance(item, dict)
        ]
        previous_internal_retry_question_ids: set[str] = set()
        prior_interactions = self.project_service.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project.id,
                QuestionInteraction.id != interaction.id,
            )
        ).all()
        for prior_interaction in prior_interactions:
            for prior_question in self.project_service.get_questions_for_interaction(
                prior_interaction.id
            ):
                prior_answer = self.project_service.get_question_answer(
                    prior_question.id
                )
                if prior_answer and "policy:internal_resolution" in (
                    prior_answer.source_refs or []
                ):
                    previous_internal_retry_question_ids.add(
                        str(prior_question.question_id)
                    )
        unresolved: list[dict[str, Any]] = []
        requested_routes: list[tuple[str, str, str]] = []
        requested_retries: list[dict[str, Any]] = []
        for question in question_batch.get("questions") or []:
            question_id = str(question.get("question_id") or "")
            resolution = by_question_id.get(question_id)
            if (
                resolution
                and resolution.get("status") == "retry_agent"
                and question_id in previous_internal_retry_question_ids
            ):
                # An internal investigation gets one real attempt. Repeating
                # the same instruction after the blocker recurs creates an
                # unbounded Coordinator/Agent loop. Escalate through the
                # existing minimal-question path instead.
                resolution = {
                    **resolution,
                    "status": "ask_user",
                    "question": str(question.get("question") or "").strip(),
                    "options": question.get("options") or [],
                    "allow_free_text": bool(question.get("allow_free_text")),
                    "reason": (
                        "The requesting Agent already completed one internal "
                        "retry and the same blocker remains."
                    ),
                }
            source_refs = set(resolution.get("source_fact_ids") or []) if resolution else set()
            valid_answer = bool(
                resolution
                and resolution.get("status") == "answered"
                and str(resolution.get("answer") or "").strip()
                and source_refs
                and source_refs.issubset(active_fact_ids)
            )
            db_question = db_questions.get(question_id)
            requires_external_user = bool(
                {
                    str(value)
                    for value in (question.get("affected_trace_ids") or [])
                    if value
                }
                & external_manual_ids
            )
            question_trace_ids = {
                str(value)
                for value in (question.get("affected_trace_ids") or [])
                if value
            }
            matching_requests = [
                request
                for request in resolution_requests
                if not request.get("affected_acceptance_criterion_ids")
                or question_trace_ids
                & {
                    str(value)
                    for value in request.get(
                        "affected_acceptance_criterion_ids", []
                    )
                    if value
                }
            ]
            internal_requests = [
                request
                for request in matching_requests
                if request.get("preferred_resolver") in {"system", "runtime"}
            ]
            if (
                resolution
                and resolution.get("status") == "ask_user"
                and internal_requests
                and not requires_external_user
            ):
                resolution = {
                    **resolution,
                    "status": "retry_agent",
                    "resolution_instruction": "；".join(
                        str(request.get("required_resolution") or "").strip()
                        for request in internal_requests
                        if str(request.get("required_resolution") or "").strip()
                    )
                    or "使用系统或运行时能力自行解决该阻塞，不要询问用户。",
                }
            if valid_answer and db_question is not None and not requires_external_user:
                await self.project_service.submit_question_answer(
                    db_question.id,
                    str(resolution["answer"]).strip(),
                    str(resolution.get("answer_label") or "").strip() or None,
                    False,
                    "coordinator",
                    answer_source="coordinator",
                    source_refs=sorted(source_refs),
                    commit=False,
                )
            elif (
                resolution
                and resolution.get("status") == "retry_agent"
                and db_question is not None
                and not requires_external_user
            ):
                instruction = str(
                    resolution.get("resolution_instruction")
                    or resolution.get("reason")
                    or "请使用授权的项目工具和运行环境自行调查后继续，不要要求用户代为调试。"
                ).strip()
                await self.project_service.submit_question_answer(
                    db_question.id,
                    instruction,
                    "由发问 Agent 内部调查",
                    False,
                    "coordinator",
                    answer_source="coordinator",
                    source_refs=["policy:internal_resolution"],
                    commit=False,
                )
                requested_retries.append({
                    "question_id": question_id,
                    "instruction": instruction,
                    "reason": str(resolution.get("reason") or "").strip(),
                    "source_fact_ids": sorted(source_refs),
                })
            elif (
                resolution
                and resolution.get("status") == "route_upstream"
                and db_question is not None
                and not requires_external_user
            ):
                target_stage = str(
                    resolution.get("route_target_stage") or ""
                ).strip()
                route_event = StateMachine(project.task_type).route_event_to_stage(
                    state, target_stage
                )
                if route_event:
                    instruction = str(
                        resolution.get("resolution_instruction")
                        or resolution.get("reason")
                        or f"回到 {target_stage} 阶段同步已确认决策。"
                    ).strip()
                    await self.project_service.submit_question_answer(
                        db_question.id,
                        instruction,
                        f"内部回退到 {target_stage}",
                        False,
                        "coordinator",
                        answer_source="coordinator",
                        source_refs=["policy:coordinator_route"],
                        commit=False,
                    )
                    requested_routes.append((target_stage, route_event, instruction))
                else:
                    unresolved.append(dict(question))
            else:
                user_question = dict(question)
                if (
                    resolution
                    and resolution.get("status") == "ask_user"
                    and db_question is not None
                ):
                    rewritten = str(resolution.get("question") or "").strip()
                    options = resolution.get("options")
                    if rewritten:
                        user_question["question"] = rewritten
                        db_question.question = rewritten
                    if isinstance(options, list) and options:
                        options = self._preserve_option_protocol_metadata(
                            question.get("options") or [], options
                        )
                        user_question["options"] = options
                        db_question.options = options
                        recommended = next(
                            (
                                item.get("key")
                                for item in options
                                if isinstance(item, dict)
                                and item.get("recommended") is True
                            ),
                            None,
                        )
                        db_question.recommended_option = recommended
                    if "allow_free_text" in resolution:
                        allow_free_text = bool(
                            resolution.get("allow_free_text")
                        )
                        user_question["allow_free_text"] = allow_free_text
                        db_question.allow_free_text = allow_free_text
                    self.project_service.session.add(db_question)
                unresolved.append(user_question)

        if requested_routes and not unresolved:
            stage_order = {
                str(item.get("id")): index
                for index, item in enumerate(
                    StateMachine(project.task_type).profile.get("stages", [])
                )
            }
            target_stage, route_event, route_instruction = min(
                requested_routes,
                key=lambda item: stage_order.get(item[0], 10_000),
            )
            state_machine = StateMachine(project.task_type)
            blocking_feedback = sorted(
                (
                    packet
                    for packet in self.project_service.get_open_feedback_packets(
                        project.id
                    )
                    if packet.blocking
                ),
                key=lambda packet: packet.created_at,
                reverse=True,
            )
            loop_signature = str(
                (project.context.get("loop_supervisor") or {}).get("signature")
                or ""
            )
            signature_matches = [
                packet
                for packet in blocking_feedback
                if loop_signature
                and FailureRouter.signature(packet) == loop_signature
            ]
            # Prefer the structurally equivalent loop evidence. If this route
            # was requested for another reason, the newest blocker is the
            # bounded causal handoff; unrelated open packets remain with their
            # own stage owners and still reach downstream quality gates.
            causal_feedback_ids = [
                packet.id
                for packet in (
                    signature_matches
                    if signature_matches
                    else blocking_feedback[:1]
                )
            ]
            project.context = {
                **project.context,
                "_coordinator_route": {
                    "event": route_event,
                    "target_stage": target_stage,
                    "origin_state": state,
                    "target_state": state_machine.next_state(state, route_event),
                    "resolution_instruction": route_instruction,
                    # A Coordinator may deliberately route earlier than the
                    # packet's default owner (for example, Coding -> Specs
                    # after repeated implementation loops). The destination
                    # must receive the evidence that caused that escalation;
                    # changing only the state-machine edge creates an
                    # expensive no-op planning round.
                    "causal_feedback_ids": causal_feedback_ids,
                },
            }
            self.project_service.session.add(project)
        elif requested_retries and not unresolved:
            # A policy answer records why no human input is needed, but it is
            # not itself task input. Persist the Coordinator's concrete retry
            # instruction for the requesting execution state so the next
            # Agent attempt actually receives the resolution. This remains
            # until a successful transition leaves the state.
            context = dict(project.context or {})
            retry_by_state = dict(context.get("agent_retry_feedback", {}))
            retry_by_state[state] = json.dumps(
                {
                    "coordinator_resolution": "\n".join(
                        item["instruction"] for item in requested_retries
                    ),
                    "origin_state": state,
                    "resolved_questions": requested_retries,
                    "instruction": (
                        "These are Coordinator-owned internal resolutions. "
                        "Execute them in this attempt and return evidence; do "
                        "not ask the user to repeat or perform system work."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            context["agent_retry_feedback"] = retry_by_state
            project.context = context
            self.project_service.session.add(project)
        self.project_service.session.commit()
        return unresolved

    @staticmethod
    def _preserve_option_protocol_metadata(
        original_options: list[dict[str, Any]],
        rewritten_options: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep security-relevant option metadata across Coordinator rewrites.

        The match is exclusively the exact protocol option key.  Human labels,
        question text, and recommendations are deliberately ignored so this
        cannot become a permission keyword heuristic.
        """
        originals = {
            str(option.get("key")): option
            for option in original_options
            if isinstance(option, dict) and option.get("key")
        }
        merged: list[dict[str, Any]] = []
        for rewritten in rewritten_options:
            if not isinstance(rewritten, dict):
                continue
            item = dict(rewritten)
            original = originals.get(str(item.get("key") or ""))
            grants = (
                original.get("grants_runtime_capabilities")
                if isinstance(original, dict)
                else None
            )
            if isinstance(grants, list):
                item["grants_runtime_capabilities"] = list(grants)
            merged.append(item)
        return merged

    @staticmethod
    def _available_coordinator_routes(
        project: Project, state: str
    ) -> list[dict[str, str]]:
        state_machine = StateMachine(project.task_type)
        routes: list[dict[str, str]] = []
        for stage in state_machine.profile.get("stages", []):
            stage_id = str(stage.get("id") or "")
            event = state_machine.route_event_to_stage(state, stage_id)
            if event:
                routes.append({"target_stage": stage_id, "event": event})
        return routes

    def _consume_coordinator_route(self, project: Project) -> str | None:
        self.project_service.session.refresh(project)
        context = dict(project.context or {})
        route = context.pop("_coordinator_route", None)
        if not isinstance(route, dict):
            return None
        event = str(route.get("event") or "").strip()
        if not event:
            return None
        target_state = str(route.get("target_state") or "").strip()
        instruction = str(route.get("resolution_instruction") or "").strip()
        if target_state:
            causal_feedback_ids = [
                str(value)
                for value in (route.get("causal_feedback_ids") or [])
                if value
            ]
            if causal_feedback_ids:
                routed_feedback_by_state = dict(
                    context.get("routed_feedback_ids_by_state", {})
                )
                routed_feedback_by_state[target_state] = list(dict.fromkeys([
                    *(routed_feedback_by_state.get(target_state) or []),
                    *causal_feedback_ids,
                ]))
                context["routed_feedback_ids_by_state"] = (
                    routed_feedback_by_state
                )
        if target_state and instruction:
            retry_feedback_by_state = dict(
                context.get("agent_retry_feedback", {})
            )
            retry_feedback_by_state[target_state] = json.dumps(
                {
                    "coordinator_resolution": instruction,
                    "route_target_stage": route.get("target_stage"),
                    "origin_state": route.get("origin_state"),
                    "instruction": (
                        "这是 Coordinator 已经做出的内部解决决定；"
                        "进入目标阶段后必须实际执行并在输出中给出证据，"
                        "不得只重新检查旧产物或将责任再转给下游 Agent。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            context["agent_retry_feedback"] = retry_feedback_by_state
        project.context = context
        self.project_service.session.add(project)
        self.project_service.session.commit()
        return event

    @staticmethod
    def _normalize_coordinator_resolutions(output: Any) -> list[dict[str, Any]]:
        """Normalize the schema-validated Coordinator response variants.

        The canonical form is ``resolutions/status``.  The auxiliary schema also
        admits two tightly constrained, semantically equivalent shapes that LLMs
        commonly emit (``responses/status`` and
        ``clarifications/disposition``).  Unknown fields/shapes are still
        rejected by Runtime before this adapter is reached.
        """
        if not isinstance(output, dict):
            return []
        items = output.get("resolutions")
        status_key = "status"
        if not isinstance(items, list):
            items = output.get("responses")
        if not isinstance(items, list):
            items = output.get("clarifications")
            status_key = "disposition"
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            value = dict(item)
            if status_key == "disposition":
                value["status"] = value.get("disposition")
            normalized.append(value)
        return normalized

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

        # Compatibility for policy answers persisted by older versions: a
        # one-shot retry/route instruction may already be present in
        # confirmed_brief.  Consult the underlying answer provenance and do
        # not use policy-sourced entries to suppress a recurring question.
        policy_keys: set[str] = set()
        interactions = self.project_service.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project.id
            )
        ).all()
        for interaction in interactions:
            for stored_question in self.project_service.get_questions_for_interaction(
                interaction.id
            ):
                answer = self.project_service.get_question_answer(stored_question.id)
                if not answer or not any(
                    str(ref).startswith("policy:")
                    for ref in (answer.source_refs or [])
                ):
                    continue
                key = str(
                    stored_question.decision_key or stored_question.question_id or ""
                ).strip().lower()
                if key:
                    policy_keys.add(key)

        retained: list[dict[str, Any]] = []
        suppressed: list[dict[str, str]] = []
        for question in question_batch.get("questions") or []:
            if not isinstance(question, dict):
                retained.append(question)
                continue
            keys = {
                str(question.get("decision_key") or "").strip().lower(),
                str(question.get("question_id") or "").strip().lower(),
            }
            keys.discard("")
            decision = next(
                (
                    confirmed[k]
                    for k in keys
                    if k in confirmed and k not in policy_keys
                ),
                None,
            )
            if not decision:
                retained.append(question)
                continue
            suppressed.append({
                "decision_key": next((k for k in keys if k in confirmed), ""),
                "question": str(question.get("question") or ""),
                "confirmed_answer": str(decision.get("answer") or ""),
            })

        filtered = dict(question_batch)
        filtered["questions"] = retained
        return filtered, suppressed

    def _extract_unresolved_ambiguities(
        self, artifact: dict[str, Any], state: str
    ) -> list[dict[str, Any]]:
        """Return unresolved ambiguities/open_questions from a planning artifact.

        Requirements use ``ambiguities`` objects and can explicitly mark an
        item ``blocking: true``. Architecture/spec ``open_questions`` are
        informational by contract and are never promoted here; a blocking
        decision must be emitted in the agent turn's ``question_batch``.
        """
        questions: list[dict[str, Any]] = []
        if state == "PLANNING_REQUIREMENTS":
            for idx, item in enumerate(artifact.get("ambiguities") or []):
                if not isinstance(item, dict):
                    continue
                if item.get("blocking") is not True:
                    continue
                question = str(item.get("question") or "").strip()
                if not question:
                    continue
                questions.append({
                    "question": question,
                    "impact": str(item.get("impact") or "").strip(),
                    "default_assumption": item.get("default_assumption"),
                    "id_suffix": f"ambiguity_{idx}",
                })
        return questions

    def _synthesize_question_batch_from_ambiguities(
        self, project: Project, state: str, ambiguities: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Build a question_batch from unresolved artifact ambiguities."""
        stage = state.lower().replace("planning_", "")
        batch_key = f"{stage}_artifact_clarify_r{project.context.get('round_number', 1)}"
        questions: list[dict[str, Any]] = []
        for idx, item in enumerate(ambiguities):
            suffix = item.get("id_suffix") or f"q_{idx}"
            default_assumption = item.get("default_assumption")
            question_text = item["question"]
            options: list[dict[str, Any]] = []
            allow_free_text = True
            if default_assumption:
                question_text += f"\n\n当前默认假设：{default_assumption}"
                options = [
                    {
                        "key": "accept_default",
                        "label": f"接受默认：{default_assumption}",
                        "impact": "按当前默认假设继续",
                        "recommended": True,
                    },
                    {
                        "key": "provide_custom",
                        "label": "我要修改",
                        "impact": "选择后继续填写具体要求",
                        "recommended": False,
                    },
                ]
            questions.append({
                "question_id": f"q_{suffix}",
                "decision_key": f"artifact.{suffix}",
                "kind": "blocking",
                "question": question_text,
                "reason": item.get("impact") or "产物中存在未澄清项，必须明确后才能进入确认阶段",
                "allow_free_text": allow_free_text,
                "options": options,
                "affected_trace_ids": [],
                "default_assumption": default_assumption,
            })
        return {"batch_key": batch_key, "questions": questions}

    def _block_on_unresolved_ambiguities(
        self,
        project: Project,
        state: str,
        worktree: Path,
        output: dict[str, Any],
    ) -> str | None:
        """If a planning artifact contains unresolved ambiguities, force ask_user.

        Returns ``ask_user`` if ambiguities were found and a question_batch was
        synthesized, otherwise None.
        """
        if state not in _PLANNER_ARTIFACTS:
            return None
        if output.get("next_state_event") not in (None, "complete"):
            return None
        filename, _ = _PLANNER_ARTIFACTS[state]
        artifact_path = worktree / ".mac" / filename
        if not artifact_path.exists():
            return None
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        ambiguities = self._extract_unresolved_ambiguities(artifact, state)
        if not ambiguities:
            return None
        batch = self._synthesize_question_batch_from_ambiguities(project, state, ambiguities)
        output["next_state_event"] = "ask_user"
        output["question_batch"] = batch
        logger.info(
            "Forcing ask_user for %s unresolved ambiguities in %s (%s)",
            len(ambiguities), project.id, state,
        )
        return "ask_user"

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

    async def _consume_pending_user_inputs(
        self,
        project: Project,
        task: Task,
        state: str,
        output: dict[str, Any],
    ) -> set[str] | None:
        """Consume free-text user inputs received while an agent was running.

        scope_change / correction inputs are recorded as blocking feedback.
        When such an input arrives in an agent-execution stage, the agent's
        result is treated as stale: it is not merged/copied and the project is
        rerun in the same execution state so the user's input is incorporated
        without asking a duplicate clarification question.
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
                recommended_rework_target=inp.recommended_rework_target,
                user_comment=inp.content,
                commit=False,
            )
            if inp.kind in ("scope_change", "correction"):
                blocking_inputs.append(inp)

        if not blocking_inputs:
            self.project_service.session.commit()
            return None

        # A correction or scope change materially changes the next attempt's
        # evidence and objective.  A LoopSupervisor conclusion derived from
        # the previous contract must not keep forcing an alternate provider
        # or prohibiting a route after that new input is accepted.
        if project.context.get("loop_supervisor"):
            next_context = dict(project.context)
            next_context.pop("loop_supervisor", None)
            project.context = next_context
            self.project_service.session.add(project)

        user_note = "\n".join(f"- {inp.content}" for inp in blocking_inputs)
        stale_note = f"Agent 运行期间收到 blocking 输入，当前结果已丢弃：\n{user_note}"

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
        return {
            str(inp.recommended_rework_target).strip()
            for inp in blocking_inputs
            if str(inp.recommended_rework_target or "").strip()
        }

    async def _handle_agent_failure(
        self,
        project: Project,
        state: str,
        task: Task,
        error: str,
    ) -> None:
        await self._finalize_progress_card(project, task, succeeded=False)
        retry_key = state
        retry_count = project.context.get("retry_counts", {}).get(retry_key, 0)
        max_retries = self._max_retries_for_state(state)

        round_audit = self._round_audit_meta(
            task,
            verdict="rejected",
            output=(task.output_data if isinstance(task.output_data, dict) else None),
            error=error,
        )
        task.output_data = {
            **(task.output_data or {}),
            "_round_audit": round_audit,
        }
        self.project_service.session.add(task)
        self.project_service.session.commit()
        logger.error(
            "Agent round audit rejected: %s",
            json.dumps(round_audit, ensure_ascii=False, sort_keys=True),
        )

        await self.project_service.log_event(
            project_id=project.id,
            level="ERROR",
            message=f"Agent {task.agent_type} failed at {state}: {error}",
            meta={
                "task_id": task.id,
                "state": state,
                "retry_count": retry_count,
                "round_audit": round_audit,
            },
        )

        next_retry_count = retry_count + 1
        failure_history = self.project_service.get_task_failure_history(
            project.id,
            agent_type=task.agent_type,
            state=state,
        )
        current_failure_metadata = {}
        if failure_history and isinstance(failure_history[-1].evidence, dict):
            candidate = failure_history[-1].evidence.get("failure")
            if isinstance(candidate, dict):
                current_failure_metadata = dict(candidate)
        automatic_retry_allowed = (
            current_failure_metadata.get("retryable") is not False
        )
        if not automatic_retry_allowed:
            # Repeating the same request cannot repair a protocol-declared
            # non-retryable upstream condition. Escalate after this attempt;
            # a later explicit retry remains available after the environment
            # or credential has actually changed.
            max_retries = 1
        current_fingerprint = (
            failure_history[-1].fingerprint if failure_history else ""
        )
        same_failure_count = sum(
            1
            for report in failure_history
            if report.fingerprint == current_fingerprint
        )
        failure_feedback = {
            "current_failure": {
                "task_id": task.id,
                "attempt_number": task.attempt_number,
                "summary": _concise_agent_failure(error),
                "fingerprint": current_fingerprint,
                **(
                    {"failure": current_failure_metadata}
                    if current_failure_metadata
                    else {}
                ),
            },
            "previous_attempts": [
                {
                    "task_id": report.task_id,
                    "attempt_number": report.attempt_number,
                    "summary": _concise_agent_failure(report.summary),
                    "evidence": self._compact_failure_evidence(report.evidence),
                    "fingerprint": report.fingerprint,
                }
                # The current failure is already represented above. Replaying
                # five verbose historical tool transcripts buried the newest
                # correction in 30-50K-character retry prompts and made the
                # Agent repeat stale diagnostics. Keep only the two preceding
                # failure summaries; durable task records retain the full log.
                for report in failure_history[:-1][-2:]
            ],
            "same_failure_count": same_failure_count,
            **(
                {"failure": current_failure_metadata}
                if current_failure_metadata
                else {}
            ),
            "instruction": (
                "完整阅读失败证据并自行修正。不要把可由当前 Agent、工作区或运行时调查的问题转给用户。"
                + (
                    " 相同失败已经重复出现；先解释前一策略为什么没有产生进展，再采用不同方法。"
                    if same_failure_count > 1
                    else ""
                )
            ),
        }
        retry_feedback_by_state = dict(
            project.context.get("agent_retry_feedback", {})
        )
        retry_feedback_by_state[state] = json.dumps(
            failure_feedback, ensure_ascii=False, indent=2
        )
        if next_retry_count < max_retries:
            # Contract/runtime retries are continuations of the same attempt,
            # not clean-slate rework rounds. Preserve any files the Agent
            # already changed and transfer that exact worktree to the retry.
            # This uses the same validated adoption path as ask_user resume.
            resumable_worktree: dict[str, str] | None = None
            if task.worktree_path and Path(task.worktree_path).exists():
                try:
                    self.worktree_manager.validate_worktree_identity(
                        project.id,
                        task.id,
                        task.agent_type,
                        Path(task.worktree_path),
                        workspace_path=project.workspace_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "Cannot preserve failed task worktree %s for retry: %s",
                        task.id,
                        exc,
                    )
                else:
                    resumable_worktree = {
                        "task_id": task.id,
                        "agent_type": task.agent_type,
                        "state": state,
                        "worktree_path": task.worktree_path,
                        "mode": "runtime_retry",
                    }
            # Reassign the context dict so SQLAlchemy detects the JSON mutation.
            new_counts = dict(project.context.get("retry_counts", {}))
            new_counts[retry_key] = next_retry_count
            retry_context = {
                **project.context,
                "retry_counts": new_counts,
                "agent_retry_feedback": retry_feedback_by_state,
            }
            if resumable_worktree is not None:
                retry_context["_resumable_agent_worktree"] = resumable_worktree
            project.context = retry_context
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
        # Keep the exact failed worktree for an explicit recovery ``retry`` as
        # well.  Previously we only preserved it for automatic retries, so the
        # recovery card's "retry" action silently started from a clean branch
        # and discarded valid edits made before a contract/runtime failure.
        resumable_worktree: dict[str, str] | None = None
        if task.worktree_path and Path(task.worktree_path).exists():
            try:
                self.worktree_manager.validate_worktree_identity(
                    project.id,
                    task.id,
                    task.agent_type,
                    Path(task.worktree_path),
                    workspace_path=project.workspace_path,
                )
            except Exception as exc:
                logger.warning(
                    "Cannot preserve exhausted task worktree %s for recovery: %s",
                    task.id,
                    exc,
                )
            else:
                resumable_worktree = {
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "state": state,
                    "worktree_path": task.worktree_path,
                    "mode": "runtime_retry",
                }
        new_counts = dict(project.context.get("retry_counts", {}))
        new_counts[retry_key] = next_retry_count
        exhausted_context = {
            **project.context,
            "retry_counts": new_counts,
            "agent_retry_feedback": retry_feedback_by_state,
        }
        if resumable_worktree is not None:
            exhausted_context["_resumable_agent_worktree"] = resumable_worktree
        project.context = exhausted_context
        self.project_service.session.commit()

        failure_context = dict(project.context)
        failure_context["last_agent_failure"] = {
            "state": state,
            "agent": task.agent_type,
            "task_id": task.id,
            "attempts": next_retry_count,
            "error": error[-1200:],
            "failure_fingerprint": current_fingerprint,
            "same_failure_count": same_failure_count,
            "failure_history": failure_feedback["previous_attempts"],
            **(
                {"failure": current_failure_metadata}
                if current_failure_metadata
                else {}
            ),
        }
        project.context = failure_context
        self.project_service.session.commit()

        recovery = await self._coordinate_exhausted_failure(
            project, state, task, failure_feedback
        )
        coordinator_recoveries = dict(
            project.context.get("coordinator_recovery_counts", {})
        )
        recovery_count = int(coordinator_recoveries.get(state, 0) or 0)
        if (
            recovery
            and recovery.get("disposition") == "retry_agent"
            and recovery_count < 1
            and automatic_retry_allowed
        ):
            coordinator_recoveries[state] = recovery_count + 1
            reset_counts = dict(project.context.get("retry_counts", {}))
            reset_counts[state] = 0
            retry_feedback_by_state[state] = json.dumps(
                {
                    **failure_feedback,
                    "coordinator_resolution": recovery.get(
                        "resolution_instruction"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            project.context = {
                **project.context,
                "retry_counts": reset_counts,
                "agent_retry_feedback": retry_feedback_by_state,
                "coordinator_recovery_counts": coordinator_recoveries,
            }
            self.project_service.session.commit()
            await self.project_service.log_event(
                project_id=project.id,
                level="INFO",
                message="Coordinator authorized one guided internal retry",
                meta={
                    "state": state,
                    "agent": task.agent_type,
                    "instruction": recovery.get("resolution_instruction"),
                },
            )
            refreshed = self.project_service.require_project(project.id)
            await self._execute_agent(refreshed, StateMachine(project.task_type))
            return

        if recovery and recovery.get("disposition") == "rework":
            target_stage = str(recovery.get("target_stage") or "")
            action_by_stage = {
                str(item.get("target_stage")): str(item.get("action"))
                for item in StateMachine(project.task_type).profile.get(
                    "rework_targets", []
                )
                if item.get("target_stage") and item.get("action")
            }
            rework_action = action_by_stage.get(target_stage)
            if rework_action:
                await self.project_service.transition_state(
                    project.id,
                    "runtime_exhausted",
                    triggered_by="coordinator",
                )
                await self.project_service.transition_state(
                    project.id,
                    "choose_rework",
                    triggered_by="coordinator",
                )
                await self.project_service.transition_state(
                    project.id,
                    rework_action,
                    triggered_by="coordinator",
                )
                refreshed = self.project_service.require_project(project.id)
                await self._tick_unsafe(refreshed.id)
                return

        # Preserve the coordinator's exhausted-runtime decision across the
        # recovery boundary.  The recovery card may be resolved by the system
        # or by a user later; either way, a retry must receive the instruction
        # that justified it instead of falling back to the same failed prompt.
        if recovery:
            recovery_resolutions = dict(
                project.context.get("coordinator_recovery_resolutions", {})
            )
            recovery_resolutions[state] = recovery
            project.context = {
                **project.context,
                "coordinator_recovery_resolutions": recovery_resolutions,
            }
            self.project_service.session.commit()

        await self.project_service.transition_state(
            project.id, "runtime_exhausted", triggered_by="orchestrator"
        )
        refreshed = self.project_service.require_project(project.id)
        await self._handle_human_confirmation(
            refreshed, StateMachine(project.task_type)
        )

    @staticmethod
    def _compact_failure_evidence(evidence: Any) -> dict[str, Any]:
        """Keep retry diagnosis useful without replaying whole tool histories."""
        if not isinstance(evidence, dict):
            return {"summary": str(evidence)[:500]}
        compact: dict[str, Any] = {}
        if "runtime_success" in evidence:
            compact["runtime_success"] = bool(evidence["runtime_success"])
        if evidence.get("runtime_logs"):
            compact["runtime_logs"] = _concise_agent_failure(
                str(evidence["runtime_logs"])
            )[:500]
        tool_calls = evidence.get("tool_calls")
        if isinstance(tool_calls, list):
            compact["tool_call_count"] = len(tool_calls)
            call_summaries: list[dict[str, Any]] = []
            for call in tool_calls[-3:]:
                if not isinstance(call, dict):
                    continue
                item: dict[str, Any] = {"name": str(call.get("name") or "unknown")}
                call_input = call.get("input")
                if isinstance(call_input, dict):
                    for key in ("command", "file_path", "path"):
                        if call_input.get(key):
                            item[key] = str(call_input[key])[:240]
                            break
                call_summaries.append(item)
            compact["last_tool_calls"] = call_summaries
        return compact

    async def _coordinate_exhausted_failure(
        self,
        project: Project,
        state: str,
        task: Task,
        failure_feedback: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Give the Coordinator one chance to resolve an exhausted loop internally."""
        return await self._execute_auxiliary_agent(
            project,
            agent_type="conversation_coordinator",
            stage="failure_recovery",
            input_data={
                "mode": "failure_recovery",
                "failed_task_id": task.id,
                "failed_agent": task.agent_type,
                "failure_history": failure_feedback,
            },
            context_files=[],
            extra_variables={
                "failed_agent": task.agent_type,
                "failure_history": json.dumps(
                    failure_feedback, ensure_ascii=False, indent=2
                ),
                "work_item_contract": json.dumps(
                    (task.input_data or {}).get("work_item_contract", {}),
                    ensure_ascii=False,
                    indent=2,
                ),
            },
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
        Recovery states remain human-controlled because retry/cancel can expand
        authority or terminate work. REWORK itself may be routed from an
        evidence-backed specialist recommendation in AI-first modes.
        """
        if state.endswith("_RECOVERY_PENDING"):
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
        integrity_error = self._planning_confirmation_integrity_error(project, state)
        if integrity_error:
            existing = next(
                (
                    packet
                    for packet in self.project_service.get_open_feedback_packets(
                        project.id
                    )
                    if packet.kind == "quality_failure"
                    and packet.summary == integrity_error
                ),
                None,
            )
            if existing is None:
                await self.project_service.create_feedback_packet(
                    project_id=project.id,
                    source_stage=state,
                    source_task_id=None,
                    source_round=1,
                    kind="quality_failure",
                    failure_class="contract_mismatch",
                    summary=integrity_error,
                    blocking=True,
                    recommended_rework_target="requirements",
                )
            context = dict(project.context or {})
            retry_by_state = dict(context.get("agent_retry_feedback", {}))
            retry_by_state["PLANNING_REQUIREMENTS"] = json.dumps(
                {
                    "coordinator_resolution": integrity_error,
                    "route_target_stage": "requirements",
                    "origin_state": state,
                    "instruction": (
                        "Regenerate and validate every declared authoritative "
                        "Requirements-stage artifact before completing; do not "
                        "ask the user to reconstruct system-observable evidence."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            project.context = {
                **context,
                "agent_retry_feedback": retry_by_state,
            }
            self.project_service.session.add(project)
            self.project_service.session.commit()
            await self.project_service.transition_state(
                project.id, "reject", triggered_by="artifact_integrity_gate"
            )
            self._schedule_followup_tick(project.id)
            return
        approval_config = project.approval_config
        state_type = state_machine.state_type(state)
        requires_confirmation = self._state_requires_confirmation(
            state, state_type, approval_config
        )
        recovery_for = None
        recovery_recommendation = None
        if state.endswith("_RECOVERY_PENDING"):
            recovery_for = state_machine.get_state(state).get("recovery_for")
            recovery_recommendation = (
                project.context.get("coordinator_recovery_resolutions", {}).get(
                    recovery_for
                )
                if recovery_for
                else None
            )
            recovery_auto_count = int(
                project.context.get("coordinator_recovery_counts", {}).get(
                    recovery_for, 0
                )
                or 0
            )
            # Normal/auto modes are AI-first.  Permit one additional bounded
            # recovery when the coordinator has an evidence-backed retry plan;
            # strict mode and repeated exhaustion still stop for the user.
            if (
                project.context.get("approval_mode", "normal") != "strict"
                and isinstance(recovery_recommendation, dict)
                and recovery_recommendation.get("disposition") == "retry_agent"
                and recovery_auto_count < 2
            ):
                requires_confirmation = False

        # Build question/options based on state
        question, options = self._build_confirmation_question(project, state)

        acceptance_recommendation = None
        acceptance_coverage_violation = None
        acceptance_auto_pass = False
        acceptance_failed = False
        if state == "ACCEPTANCE_PENDING":
            delivery_policy = (project.context or {}).get("delivery_policy")
            if not stage_required(delivery_policy, "acceptance"):
                await self.project_service.log_event(
                    project_id=project.id,
                    level="INFO",
                    message="Acceptance boundary skipped by delivery evidence policy",
                    meta={
                        "event_type": "stage_skipped",
                        "execution_state": state,
                        "stage_id": "acceptance",
                        "policy": delivery_policy,
                    },
                )
                approval = await self.project_service.create_approval(
                    project_id=project.id,
                    task_id=None,
                    state=state,
                    approval_type="confirmation",
                    question="内部变更交付证据是否已满足？",
                    options=[],
                )
                await self.project_service.respond_approval(
                    approval.id,
                    "confirm",
                    "system_auto",
                )
                approval.payload = {
                    **(approval.payload or {}),
                    "resolution_reason": (delivery_policy or {}).get("reason"),
                    "delivery_policy": delivery_policy,
                }
                self.project_service.session.add(approval)
                self.project_service.session.commit()
                await self.project_service.transition_state(
                    project.id,
                    "confirm",
                    triggered_by="delivery_policy",
                )
                refreshed = self.project_service.require_project(project.id)
                await self._send_confirmation_cards(
                    refreshed,
                    approval,
                    "内部变更交付证据已满足",
                    [],
                    show_actions=False,
                    source_state=state,
                    selected_action="confirm",
                    resolved_by="Coordinator（交付策略）",
                    resolved_at=approval.responded_at,
                    resulting_state=refreshed.current_state,
                    resolution_reason=(delivery_policy or {}).get("reason"),
                )
                # tick() already holds this project's non-reentrant lock.
                # Re-entering it here deadlocks the Feishu handler at MERGING.
                # Resume through the normal level-triggered scheduler instead.
                self._schedule_followup_tick(project.id)
                return
            manual_failures = [
                row
                for ac_id, row in self.project_service.get_active_acceptance_evidence(
                    project.id
                ).items()
                if ac_id in self._external_manual_acceptance_criterion_ids(project)
                and row.verdict == "fail"
            ]
            if manual_failures:
                failed_ids = sorted(
                    row.acceptance_criterion_id for row in manual_failures
                )
                summary = (
                    "用户已明确报告人工验收失败：" + ", ".join(failed_ids)
                )
                existing = next(
                    (
                        item
                        for item in self.project_service.get_open_feedback_packets(
                            project.id
                        )
                        if item.kind == "acceptance_rejection"
                        and item.summary == summary
                    ),
                    None,
                )
                if existing is None:
                    existing = await self.project_service.create_feedback_packet(
                        project_id=project.id,
                        source_stage="ACCEPTANCE",
                        source_task_id=None,
                        source_round=1,
                        kind="acceptance_rejection",
                        failure_class="observed_behavior_failure",
                        summary=summary,
                        blocking=True,
                        recommended_rework_target="testing",
                        evidence=[
                            {
                                "acceptance_criterion_id": row.acceptance_criterion_id,
                                "source_question_id": row.source_ref,
                                "evidence_scope": row.evidence_scope,
                                "evidence": row.evidence,
                            }
                            for row in manual_failures
                        ],
                    )
                await self.project_service.transition_state(
                    project.id, "reject", triggered_by="acceptance_evidence"
                )
                rework_events = StateMachine(project.task_type).available_events(
                    "REWORK"
                )
                recommended_event = (
                    f"to_{existing.recommended_rework_target}"
                    if existing is not None
                    and existing.recommended_rework_target
                    else None
                )
                chosen = (
                    recommended_event
                    if recommended_event in rework_events
                    else next(
                        (
                            candidate
                            for candidate in (
                                "to_testing",
                                "to_coding",
                                "to_specs",
                            )
                            if candidate in rework_events
                        ),
                        None,
                    )
                )
                if chosen:
                    await self.project_service.transition_state(
                        project.id, chosen, triggered_by="coordinator"
                    )
                return
            acceptance_recommendation = await self._evaluate_acceptance(project)
            if acceptance_recommendation is not None:
                self._ensure_structured_manual_acceptance_questions(
                    project, acceptance_recommendation
                )
                await self._record_acceptance_evaluator_failure(
                    project, acceptance_recommendation
                )
                acceptance_failed = bool(
                    acceptance_recommendation.get("result") == "fail"
                    or acceptance_recommendation.get("next_state_event") == "fail"
                    or str(acceptance_recommendation.get("action") or "").startswith(
                        "reject_"
                    )
                )
                if acceptance_failed:
                    options = [
                        option
                        for option in options
                        if option.get("action") != "confirm"
                    ]
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
                        Task.agent_type.in_([
                            "acceptance_evaluator", "acceptance_agent"
                        ]),
                        Task.status == "completed",
                    )
                    .order_by(Task.created_at.desc())
                ).first()
                if acceptance_task is not None:
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
                    unresolved = await self._mediate_agent_questions(
                        project,
                        acceptance_task,
                        "ACCEPTANCE_PENDING",
                        question_batch,
                        interaction,
                        acceptance_recommendation.get("blockers") or [],
                    )
                    if not unresolved:
                        await self.project_service.resolve_question_interaction(
                            interaction.id
                        )
                        route_event = self._consume_coordinator_route(project)
                        if route_event:
                            await self.project_service.transition_state(
                                project.id,
                                route_event,
                                triggered_by="conversation_coordinator",
                            )
                            refreshed = self.project_service.require_project(project.id)
                            await self._execute_agent(refreshed, state_machine)
                            return
                        refreshed = self.project_service.require_project(project.id)
                        await self._handle_human_confirmation(refreshed, state_machine)
                        return
                    interaction.escalated_to_user = True
                    self.project_service.session.add(interaction)
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
                existing_action = str(
                    (acceptance_recommendation or {}).get("action") or ""
                )
                coverage_action, coverage_target = (
                    self._acceptance_coverage_rework_route(
                        project,
                        existing_action
                        if existing_action.startswith("reject_to_")
                        else None,
                    )
                )
                await self._record_acceptance_coverage_failure(
                    project,
                    acceptance_coverage_violation,
                    coverage_target,
                )
                acceptance_recommendation = {
                    **(acceptance_recommendation or {}),
                    "action": coverage_action,
                    "reason": acceptance_coverage_violation["message"],
                    "summary": acceptance_coverage_violation["message"],
                }
                # Coverage is a hard gate: do not offer a misleading pass button.
                options = [opt for opt in options if opt.get("action") != "confirm"]
            else:
                acceptance_auto_pass = bool(
                    acceptance_recommendation
                    and acceptance_recommendation.get("next_state_event") == "complete"
                    and acceptance_recommendation.get("result") == "pass"
                    and acceptance_recommendation.get("action") == "confirm"
                    and project.context.get("approval_mode", "normal") != "strict"
                )
                if acceptance_auto_pass:
                    # A fully evidenced acceptance is system work, not a user
                    # confirmation task. Strict mode remains explicitly manual.
                    requires_confirmation = False

        task = (
            self.project_service.session.exec(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
            .first()
        )

        # All auxiliary work needed to prepare this boundary has finished.
        # From this point the project is genuinely waiting for a human action.
        self.project_service.set_execution_status(
            project.id,
            "running" if acceptance_auto_pass else "waiting_user",
            reason=(
                "自动验收通过，正在交付"
                if acceptance_auto_pass
                else state_machine.display_name(state)
            ),
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
        approval_options_changed = False
        if approval is None:
            rounds = dict(project.context.get("confirmation_rounds", {}))
            round_number = rounds.get(state, 0) + 1
            rounds[state] = round_number
            project.context = {**project.context, "confirmation_rounds": rounds}
            approval_payload: dict[str, Any] = {
                "round_number": round_number
            }
            if state.endswith("_RECOVERY_PENDING"):
                failure = project.context.get("last_agent_failure", {})
                failure_metadata = (
                    failure.get("failure")
                    if isinstance(failure, dict)
                    else None
                )
                if isinstance(failure_metadata, dict):
                    approval_payload["recovery"] = {
                        key: failure_metadata.get(key)
                        for key in (
                            "domain",
                            "status_code",
                            "retryable",
                            "suggested_resolver",
                        )
                        if key in failure_metadata
                    }
            approval = await self.project_service.create_approval(
                project_id=project.id,
                task_id=task.id if task else None,
                state=state,
                approval_type="confirmation",
                question=question,
                options=options,
                payload=approval_payload,
            )
        elif (approval.payload or {}).get("options", []) != options:
            approval.payload = {**approval.payload, "options": options}
            approval.question = question
            self.project_service.session.commit()
            approval_options_changed = True

        # Auto-resolve if the current mode does not require confirmation.
        if not requires_confirmation:
            chosen: str | None = None
            if acceptance_coverage_violation:
                chosen = str(acceptance_recommendation.get("action") or "")
            elif acceptance_auto_pass:
                chosen = "confirm"
            elif (
                state.endswith("_RECOVERY_PENDING")
                and isinstance(recovery_recommendation, dict)
                and recovery_recommendation.get("disposition") == "retry_agent"
            ):
                chosen = "retry"
                recovery_counts = dict(
                    project.context.get("coordinator_recovery_counts", {})
                )
                if recovery_for:
                    recovery_counts[recovery_for] = int(
                        recovery_counts.get(recovery_for, 0) or 0
                    ) + 1
                    project.context = {
                        **project.context,
                        "coordinator_recovery_counts": recovery_counts,
                    }
                    self.project_service.session.commit()
            elif state in _AUTO_CONFIRM_STATES:
                # Quality gates are shown to the user (report card) but
                # default to confirm so the flow is not blocked in auto/night.
                chosen = "confirm"
            else:
                strategy = approval_config.get("auto_resolve_strategy", "none")
                if state == "REWORK":
                    chosen = self._recommended_rework_action(project, options)
                if chosen is None and strategy == "conversation_coordinator":
                    chosen = await self._choose_action_with_coordinator(
                        project, state, question, options
                    )
                if chosen is None:
                    logger.warning(
                        "No justified auto-resolution for %s; escalating approval to user",
                        project.id,
                    )
                    self.project_service.set_execution_status(
                        project.id,
                        "waiting_user",
                        reason=state_machine.display_name(state),
                    )
                    if self.feishu_bridge:
                        sent_card_id = await self._send_confirmation_cards(
                            project, approval, question, options, acceptance_recommendation
                        )
                        if sent_card_id:
                            notification = dict(
                                (approval.payload or {}).get("notification") or {}
                            )
                            notification["confirmation_card_message_id"] = sent_card_id
                            approval.payload = {
                                **approval.payload,
                                "notification": notification,
                            }
                            self.project_service.session.commit()
                    return

            # Coordinator/evaluator calls above are asynchronous.  A user
            # correction may legitimately move the project while that agent is
            # thinking, so the returned decision is conditional on the same
            # approval still being current.  Resolve the approval and state
            # transition together; if either side is stale, discard the old
            # decision instead of applying it to the new state.
            self.project_service.session.expire_all()
            try:
                approval, project = await self.project_service.resolve_approval(
                    approval.id,
                    chosen,
                    "system_auto",
                )
            except (ValueError, InvalidStateTransitionError):
                self.project_service.session.rollback()
                logger.info(
                    "Discarded stale automatic approval decision for %s "
                    "(approval=%s source_state=%s action=%s)",
                    project.id,
                    approval.id,
                    state,
                    chosen,
                )
                return

            # Automatic decisions are user-visible decisions too.  Render the
            # same report + decision card pair as a human boundary, but make
            # the decision card a completed read-only projection that states
            # what was selected, by whom, and which backend state resulted.
            if self.feishu_bridge:
                resolved_card_id = await self._send_confirmation_cards(
                    project,
                    approval,
                    question,
                    options,
                    acceptance_recommendation,
                    show_actions=False,
                    source_state=state,
                    selected_action=chosen,
                    resolved_by="Coordinator / 系统策略",
                    resolved_at=approval.responded_at,
                    resulting_state=state_machine.display_name(project.current_state),
                    resolution_reason=str(
                        (acceptance_recommendation or {}).get("reason")
                        or (acceptance_recommendation or {}).get("summary")
                        or "依据已确认事实和当前阶段证据自动处理"
                    ),
                )
                if resolved_card_id:
                    notification = dict(
                        (approval.payload or {}).get("notification") or {}
                    )
                    notification.update({
                        "confirmation_card_message_id": resolved_card_id,
                        "card_status": "resolved",
                        "selected_action": chosen,
                        "resolved_by": "system_auto",
                        "resulting_state": project.current_state,
                    })
                    approval.payload = {
                        **(approval.payload or {}),
                        "notification": notification,
                    }
                    self.project_service.session.add(approval)
                    self.project_service.session.commit()
            return

        # Otherwise, send notification and wait. Human-state ticks are
        # level-triggered and reuse the same pending approval, so notification
        # must be idempotent: exactly one card set per approval instance.
        # A missing marker means the previous send crashed mid-way — retry.
        if self.feishu_bridge:
            notification = dict((approval.payload or {}).get("notification") or {})
            existing_card_id = notification.get("confirmation_card_message_id")
            if existing_card_id and approval_options_changed:
                # Options were refreshed on the reused approval — update the
                # existing card in place instead of posting a duplicate.
                await self._send_confirmation_cards(
                    project, approval, question, options,
                    acceptance_recommendation,
                    update_message_id=existing_card_id,
                )
            elif not existing_card_id:
                sent_card_id = await self._send_confirmation_cards(
                    project, approval, question, options, acceptance_recommendation
                )
                if sent_card_id:
                    notification["confirmation_card_message_id"] = sent_card_id
                    approval.payload = {**approval.payload, "notification": notification}
                    self.project_service.session.commit()

    def _recommended_rework_action(
        self, project: Project, options: list[dict[str, Any]]
    ) -> str | None:
        """Resolve internal rework routing from the newest blocking evidence."""
        valid_actions = {str(option.get("action")) for option in options}
        stage_actions = {
            str(item.get("target_stage")): str(item.get("action"))
            for item in StateMachine(project.task_type).profile.get("rework_targets", [])
        }
        feedback = sorted(
            self.project_service.get_open_feedback_packets(project.id),
            key=lambda item: item.created_at,
            reverse=True,
        )
        all_feedback = list(
            self.project_service.session.exec(
                select(FeedbackPacket).where(FeedbackPacket.project_id == project.id)
            ).all()
        )
        for item in feedback:
            if not item.blocking:
                continue
            assessment = self.loop_supervisor.assess(item, all_feedback)
            if assessment.repeated:
                project.context = {
                    **project.context,
                    "loop_supervisor": {
                        "signature": assessment.signature,
                        "occurrences": assessment.occurrences,
                        "reason": assessment.reason,
                        "failure_domain": FailureRouter.for_feedback(item).domain,
                        "prohibited_default_target": item.recommended_rework_target,
                    },
                }
                self.project_service.session.add(project)
                self.project_service.session.commit()
                return None
            action = stage_actions.get(str(item.recommended_rework_target or ""))
            if action in valid_actions:
                if project.context.get("loop_supervisor"):
                    context = dict(project.context)
                    context.pop("loop_supervisor", None)
                    project.context = context
                    self.project_service.session.add(project)
                    self.project_service.session.commit()
                return action
        return None

    async def _send_confirmation_cards(
        self,
        project: Project,
        approval: Any,
        question: str,
        options: list[dict],
        acceptance_recommendation: dict[str, Any] | None = None,
        show_actions: bool = True,
        update_message_id: str | None = None,
        delivery_key_suffix: str = "",
        source_state: str | None = None,
        selected_action: str | None = None,
        resolved_by: str | None = None,
        resolved_at: datetime | str | None = None,
        resulting_state: str | None = None,
        resolution_reason: str | None = None,
    ) -> str | None:
        """Send a report card followed by the action confirmation card.

        Returns the confirmation card's message id so callers can record it
        for idempotent re-ticks. When ``update_message_id`` is given, skip the
        report cards and refresh that existing card in place instead.
        """
        if self.feishu_bridge is None:
            return None

        sm = StateMachine(project.task_type)
        display_state = source_state or project.current_state
        state_display = sm.display_name(display_state)
        project_name = project.name or project.id
        title = f"[{project_name}] {question}"

        state_details = self._build_details_for_state(project, project.current_state)
        details: dict[str, Any] = {"当前阶段": state_display}
        if state_details:
            details.update(state_details)
        if acceptance_recommendation:
            rec_summary = acceptance_recommendation.get("reason") or acceptance_recommendation.get("summary", "")
            if rec_summary:
                details["验收评估"] = _sanitize_user_facing_summary(
                    str(rec_summary), sm
                )[:200]
            if acceptance_recommendation.get("action") == "confirm":
                details["建议操作"] = "验收通过"

        receive_id, receive_id_type = self._resolve_project_recipient(project)

        if update_message_id:
            from mac_system.feishu.card import build_confirmation_card

            card = build_confirmation_card(
                title=title,
                summary="请查看上方报告后选择操作",
                details=details,
                options=options,
                approval_id=approval.id,
                state=display_state,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                show_actions=show_actions,
                selected_action=selected_action,
                resolved_by=resolved_by,
                resolved_at=(
                    resolved_at.isoformat(timespec="seconds")
                    if isinstance(resolved_at, datetime)
                    else resolved_at
                ),
                resulting_state=resulting_state,
                resolution_reason=resolution_reason,
            )
            try:
                message_id = await deliver_card(
                    self.project_service,
                    self.feishu_bridge,
                    dedupe_key=f"project:{project.id}:approval:{approval.id}:decision:update:{project.context_version}",
                    recipient_id=receive_id,
                    recipient_type=receive_id_type,
                    intent_type="decision",
                    card=card,
                    project_id=project.id,
                    update_message_id=update_message_id,
                    projection_key=f"approval:{approval.id}",
                    subject_type="approval",
                    subject_id=approval.id,
                    lifecycle_status=(
                        "resolved_auto" if selected_action is not None else "pending"
                    ),
                    selected_action=selected_action,
                    resolved_by=resolved_by,
                    resulting_state=resulting_state,
                )
            except Exception:
                logger.warning(
                    "Failed to update confirmation card for %s", project.id,
                    exc_info=True,
                )
                return None
            return update_message_id

        summary_text = self._build_summary_for_state(project, display_state)
        if not summary_text:
            logger.warning(
                "Summary for %s at %s is empty; using fallback", project.id, project.current_state
            )
            summary_text = self._fallback_summary(project, display_state)
        summary_text = _sanitize_user_facing_summary(summary_text, sm)
        # Always send a separate report card first, then the action card.
        # This separates "本轮汇报" from "状态流转/确认".
        from mac_system.feishu.card import build_confirmation_card, build_content_card

        chunks = split_markdown_sections(summary_text, max_chars=1600)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            try:
                await deliver_card(
                    self.project_service,
                    self.feishu_bridge,
                    dedupe_key=(
                        f"project:{project.id}:approval:{approval.id}:report:{idx}"
                        f"{delivery_key_suffix}"
                    ),
                    recipient_id=receive_id,
                    recipient_type=receive_id_type,
                    intent_type="report",
                    card=build_content_card(
                        title, chunk, page=idx, total=total, color="blue"
                    ),
                    project_id=project.id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to send summary content card for %s: %s", project.id, exc
                )
                break

        # The confirmation card carries only metadata and action buttons.
        try:
            card = build_confirmation_card(
                title=title,
                summary="请查看上方报告后选择操作",
                details=details,
                options=options,
                approval_id=approval.id,
                state=display_state,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                show_actions=show_actions,
                selected_action=selected_action,
                resolved_by=resolved_by,
                resolved_at=(
                    resolved_at.isoformat(timespec="seconds")
                    if isinstance(resolved_at, datetime)
                    else resolved_at
                ),
                resulting_state=resulting_state,
                resolution_reason=resolution_reason,
            )
            return await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=(
                    f"project:{project.id}:approval:{approval.id}:decision"
                    f"{delivery_key_suffix}"
                ),
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="decision",
                card=card,
                project_id=project.id,
                projection_key=f"approval:{approval.id}",
                subject_type="approval",
                subject_id=approval.id,
                lifecycle_status=(
                    "resolved_auto" if selected_action is not None else "pending"
                ),
                selected_action=selected_action,
                resolved_by=resolved_by,
                resulting_state=resulting_state,
            )
        except Exception as exc:
            logger.warning(
                "Failed to send confirmation card for %s: %s", project.id, exc
            )
            return None

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
            if strategy == "conversation_coordinator":
                answer = await self._answer_input_with_coordinator(
                    project, state, question
                )
            elif strategy == "project_manager_default":
                answer = await self._answer_input_with_project_manager(
                    project, state, question
                )
            else:
                answer = None
            if answer is not None:
                await self.project_service.respond_approval(
                    approval.id, "answer", "system_auto"
                )
                await self.project_service.transition_state(
                    project.id, "answer",
                    payload={"clarification_answer": answer},
                    triggered_by="conversation_coordinator",
                )
                return
            if strategy in {"conversation_coordinator", "project_manager_default"}:
                logger.warning(
                    "AI coordinator could not resolve input for %s; escalating to user",
                    project.id,
                )

        # Human-state ticks are level-triggered and reuse the same pending
        # approval, so card notification must be idempotent: exactly one card
        # set per approval instance. A missing marker means the previous send
        # crashed mid-way — fall through and retry.
        if self.feishu_bridge and (approval.payload or {}).get("notification"):
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
                from mac_system.feishu.card import build_content_card

                chunks = split_markdown_sections(context_text, max_chars=1600)
                total = len(chunks)
                for idx, chunk in enumerate(chunks, start=1):
                    try:
                        await deliver_card(
                            self.project_service,
                            self.feishu_bridge,
                            dedupe_key=f"project:{project.id}:approval:{approval.id}:input-context:{idx}",
                            recipient_id=receive_id,
                            recipient_type=receive_id_type,
                            intent_type="report",
                            card=build_content_card(
                                f"[{project_tag}] {question}", chunk,
                                page=idx, total=total, color="orange",
                            ),
                            project_id=project.id,
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
                from mac_system.feishu.card import build_confirmation_card

                card = build_confirmation_card(
                    title=f"[{project_tag}] {question}",
                    summary="请选择要回退到哪一层重新处理",
                    details=details,
                    options=options,
                    approval_id=approval.id,
                    state=state,
                    chat_id=project.feishu_chat_id,
                    chat_type=project.feishu_chat_type,
                )
                message_id = await deliver_card(
                    self.project_service,
                    self.feishu_bridge,
                    dedupe_key=f"project:{project.id}:approval:{approval.id}:rework",
                    recipient_id=receive_id,
                    recipient_type=receive_id_type,
                    intent_type="decision",
                    card=card,
                    project_id=project.id,
                    projection_key=f"approval:{approval.id}",
                    subject_type="approval",
                    subject_id=approval.id,
                    lifecycle_status="pending",
                )
                approval.payload = {
                    **approval.payload,
                    "notification": {
                        "kind": "rework_confirmation",
                        "confirmation_card_message_id": message_id,
                        "card_status": "pending",
                    },
                }
                self.project_service.session.commit()
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
                                approval.payload = {
                                    **approval.payload,
                                    "notification": {"kind": "question_batch"},
                                }
                                self.project_service.session.commit()
                            except Exception as exc:
                                logger.warning(
                                    "Failed to send question batch card for %s: %s",
                                    project.id, exc,
                                )
                if not sent_batch_card:
                    from mac_system.feishu.card import build_input_card

                    card = build_input_card(
                        title=f"[{project_tag}] {question}",
                        question=question,
                        context=card_context,
                        approval_id=approval.id,
                        state=state,
                        chat_id=project.feishu_chat_id,
                        chat_type=project.feishu_chat_type,
                    )
                    await deliver_card(
                        self.project_service,
                        self.feishu_bridge,
                        dedupe_key=f"project:{project.id}:approval:{approval.id}:input",
                        recipient_id=receive_id,
                        recipient_type=receive_id_type,
                        intent_type="question",
                        card=card,
                        project_id=project.id,
                    )
                    approval.payload = {
                        **approval.payload,
                        "notification": {"kind": "input_card"},
                    }
                    self.project_service.session.commit()

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
            # Inline free-text optimization: when the batch contains exactly
            # one choice question with ``allow_free_text=True``, render an
            # inline input form on the question card itself. This lets the
            # user type a custom answer directly instead of going through the
            # 2-step "provide_custom -> reply to input card" flow. When
            # multiple free-text-allowed questions exist, fall back to the
            # 2-step flow to honor the "one input per card" UX constraint.
            # Keep option cards on the proven Card 1.0 path. The current Card
            # 2.0 inline-form envelope is rejected by Feishu when combined
            # with legacy choice controls. A custom/detail option still opens
            # the existing follow-up input-card flow after submission.
            inline_free_text_question_id = None
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
                inline_free_text_question_id=inline_free_text_question_id,
            )
            message_id = await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"project:{project.id}:interaction:{interaction_data['interaction_id']}:choices",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="question",
                card=card,
                project_id=project.id,
            )
            if message_id:
                reply_message_ids.append(message_id)
                payload["question_batch_message_id"] = message_id
                payload["selected_options"] = selected_options
                if inline_free_text_question_id:
                    payload["inline_free_text_question_id"] = inline_free_text_question_id

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
            message_id = await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"project:{project.id}:interaction:{interaction_data['interaction_id']}:question:{q.get('question_id')}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="question",
                card=card,
                project_id=project.id,
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
        completed = self.project_service.require_project(project.id)
        if completed.current_state == "COMPLETED":
            await self._notify_delivery_completed(completed)

    async def _notify_delivery_completed(self, project: Project) -> None:
        """Send one idempotent terminal handoff with verified entry facts."""
        if self.feishu_bridge is None:
            return
        acceptance_task = self.project_service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_agent",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        environment = (
            (acceptance_task.output_data or {}).get("environment") or {}
            if acceptance_task is not None else {}
        )
        entry_points = [
            str(value).strip()
            for value in environment.get("entry_points") or []
            if str(value).strip()
        ]
        static_entry = Path(project.workspace_path) / "index.html"
        if static_entry.is_file():
            static_uri = static_entry.resolve().as_uri()
            if static_uri not in entry_points:
                entry_points.append(static_uri)
        entry_lines: list[str] = []
        for value in entry_points[:4]:
            if value.startswith("file://"):
                path = Path(unquote(urlparse(value).path))
                scope = (
                    "本机文件存在，可直接用浏览器打开"
                    if path.is_file()
                    else "验收后文件已不存在"
                )
            elif value.startswith("/"):
                scope = (
                    "本机文件存在，可直接打开"
                    if Path(value).is_file()
                    else "验收后文件已不存在"
                )
            elif value.startswith("http://127.0.0.1") or value.startswith("http://localhost"):
                scope = (
                    "当前服务在线，仅当前电脑可访问"
                    if await self._local_http_entry_is_live(value)
                    else "验收时临时入口，当前服务已停止"
                )
            else:
                scope = "请按验收记录中的网络环境访问"
            entry_lines.append(f"- {value}（{scope}）")
        if not entry_lines:
            entry_lines.append("- 未登记经过验收的可访问入口")

        acceptance_summary = (
            str((acceptance_task.output_data or {}).get("summary") or "").strip()
            if acceptance_task is not None else ""
        )
        content = "\n".join([
            "项目已经完成交付流程。",
            "",
            f"验收结论：{_sanitize_user_facing_summary(acceptance_summary, StateMachine(project.task_type)) or '已通过最终验收'}",
            "",
            "入口状态：",
            *entry_lines,
            "",
            f"项目目录：{project.workspace_path}",
            "",
            "后续直接在本对话提出修改或报告问题，系统会在同一项目目录创建新的迭代运行。",
        ])
        from mac_system.feishu.card import build_content_card

        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"project:{project.id}:delivery-completed:v2",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="result",
                card=build_content_card(
                    f"[{project.name or '项目'}] 交付完成",
                    content,
                    color="green",
                ),
                project_id=project.id,
            )
        except Exception:
            logger.warning(
                "Failed to send terminal delivery card for %s",
                project.id,
                exc_info=True,
            )

    @staticmethod
    async def _local_http_entry_is_live(url: str) -> bool:
        """Verify that a localhost URL is still reachable at handoff time."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
                response = await client.get(url)
            return response.status_code < 500
        except Exception:
            return False

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
                is_secret = q.sensitivity == "secret"
                history.append({
                    "question_id": q.question_id,
                    "question": q.question,
                    "answer": (
                        f"[secret injected as {q.secret_env_var}]"
                        if is_secret
                        else qa.answer
                    ),
                    "answer_label": None if is_secret else qa.answer_label,
                    "skipped": qa.skipped,
                    "state": it.resume_state,
                    "round": it.origin_round,
                    "sensitivity": q.sensitivity,
                    "secret_env_var": q.secret_env_var,
                })
        return history

    def _runtime_env_for_project(
        self,
        project_id: str,
        *,
        agent_type: str | None = None,
        allowed_capability_fact_ids: set[str] | None = None,
    ) -> dict[str, str]:
        """Resolve only the capability facts selected for this WorkItem.

        ``None`` preserves the unscoped compatibility behavior used by older
        callers.  An empty set intentionally means that this Agent receives no
        project capability.  System capabilities are independently restricted
        by their declarative ``applies_to`` metadata.
        """
        from mac_system.models import QuestionInteraction as QI

        capability_facts = self.project_service.get_active_context_records(
            project_id, record_types={"capability"}
        )
        selected_secret_env_vars = {
            str(value.get("secret_ref")).removeprefix("env:")
            for fact in capability_facts
            if (
                allowed_capability_fact_ids is None
                or fact.id in allowed_capability_fact_ids
            )
            for value in [fact.value if isinstance(fact.value, dict) else {}]
            if str(value.get("secret_ref") or "").startswith("env:")
        }
        interactions = self.project_service.session.exec(
            select(QI).where(
                QI.project_id == project_id,
                QI.status == "answered",
            )
        ).all()
        runtime_env = self._configured_runtime_env(agent_type)
        for interaction in interactions:
            for question in self.project_service.get_questions_for_interaction(
                interaction.id
            ):
                if question.sensitivity != "secret" or not question.secret_env_var:
                    continue
                if (
                    allowed_capability_fact_ids is not None
                    and question.secret_env_var not in selected_secret_env_vars
                ):
                    continue
                answer = self.project_service.get_question_answer(question.id)
                if not answer or not answer.answer or answer.skipped:
                    continue
                stored = self.project_service.get_runtime_secret(
                    project_id, question.secret_env_var
                )
                if stored:
                    runtime_env[question.secret_env_var] = stored
                    continue
                # Compatibility for a service instance that has not yet run
                # startup reconciliation. Migrate the legacy row immediately
                # rather than injecting its plaintext value indefinitely.
                if not str(answer.answer).startswith("secret_ref:"):
                    ref = self.project_service.secret_store.set(
                        project_id, question.secret_env_var, str(answer.answer)
                    )
                    runtime_env[question.secret_env_var] = str(answer.answer)
                    answer.answer = f"secret_ref:{ref}"
                    answer.answer_label = "已安全保存"
                    self.project_service.session.add(answer)
                    self.project_service.session.commit()
        for fact in capability_facts:
            if (
                allowed_capability_fact_ids is not None
                and fact.id not in allowed_capability_fact_ids
            ):
                continue
            value = fact.value if isinstance(fact.value, dict) else {}
            source_file = Path(str(value.get("source_file") or ""))
            bindings = value.get("env_bindings")
            if not source_file.is_absolute() or not source_file.is_file() or not isinstance(bindings, dict):
                continue
            allowed_roots = [root.resolve() for root in get_allowed_bind_roots()]
            if allowed_roots:
                try:
                    resolved_source = source_file.resolve()
                except OSError:
                    continue
                if not any(
                    resolved_source == root or root in resolved_source.parents
                    for root in allowed_roots
                ):
                    logger.warning("Rejected runtime capability outside allowed roots: %s", fact.id)
                    continue
            parsed: dict[str, str] = {}
            try:
                if source_file.suffix == ".plist":
                    import plistlib

                    with source_file.open("rb") as stream:
                        payload = plistlib.load(stream)
                    environment = payload.get("EnvironmentVariables") or {}
                    if isinstance(environment, dict):
                        parsed = {
                            str(key): str(value)
                            for key, value in environment.items()
                            if isinstance(value, (str, int, float, bool))
                        }
                else:
                    for raw_line in source_file.read_text(
                        encoding="utf-8"
                    ).splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, raw_value = line.split("=", 1)
                        parsed[key.strip()] = raw_value.strip().strip("\"'")
            except (OSError, ValueError):
                logger.warning("Unable to resolve runtime capability %s", fact.id)
                continue
            for target_name, source_name in bindings.items():
                resolved = parsed.get(str(source_name))
                if resolved is not None:
                    runtime_env[str(target_name)] = resolved
        return runtime_env

    def _project_runtime_capability_contracts(
        self,
        project_id: str,
    ) -> list[dict[str, Any]]:
        """Describe project capability references without resolving values."""
        contracts = list(
            self._configured_runtime_capability_contracts(
                "conversation_coordinator"
            )
        )
        for fact in self.project_service.get_active_context_records(
            project_id,
            record_types={"capability"},
        ):
            value = fact.value if isinstance(fact.value, dict) else {}
            bindings = value.get("env_bindings")
            source_file = str(value.get("source_file") or "")
            if not isinstance(bindings, dict) or not bindings:
                continue
            contracts.append(
                {
                    "capability_id": fact.id,
                    "available": True,
                    "source_type": "secret_reference",
                    "source_file": source_file,
                    "env_vars": sorted(str(name) for name in bindings),
                }
            )
        return contracts

    def _configured_runtime_env(
        self, agent_type: str | None = None
    ) -> dict[str, str]:
        """Resolve declarative system capabilities at the process boundary.

        The registry contains only environment-variable references and safe
        defaults. Secret values are never rendered into a prompt or persisted
        in a task. A capability is omitted unless every referenced source
        variable exists.
        """
        runtime_env: dict[str, str] = {}
        capabilities = self.prompt_renderer.agent_config.get(
            "runtime_capabilities", []
        )
        for capability in capabilities:
            if not isinstance(capability, dict):
                continue
            applies_to = capability.get("applies_to") or []
            if agent_type is not None and applies_to and agent_type not in applies_to:
                continue
            bindings = capability.get("env_bindings") or {}
            static_env = capability.get("static_env") or {}
            if not isinstance(bindings, dict) or not isinstance(static_env, dict):
                continue
            resolved: dict[str, str] = {}
            for target_name, source_name in bindings.items():
                value = os.environ.get(str(source_name))
                if not value:
                    resolved = {}
                    break
                resolved[str(target_name)] = value
            if bindings and not resolved:
                continue
            for target_name, value in static_env.items():
                if isinstance(value, (str, int, float, bool)):
                    resolved[str(target_name)] = str(value)
            runtime_env.update(resolved)
        return runtime_env

    def _configured_runtime_capability_contracts(
        self, agent_type: str
    ) -> list[dict[str, Any]]:
        """Describe usable configured capabilities without exposing values."""
        available_env = set(self._configured_runtime_env(agent_type))
        contracts: list[dict[str, Any]] = []
        capabilities = self.prompt_renderer.agent_config.get(
            "runtime_capabilities", []
        )
        for capability in capabilities:
            if not isinstance(capability, dict):
                continue
            applies_to = capability.get("applies_to") or []
            if applies_to and agent_type not in applies_to:
                continue
            env_names = sorted(
                {
                    str(name)
                    for name in (
                        list((capability.get("env_bindings") or {}).keys())
                        + list((capability.get("static_env") or {}).keys())
                    )
                }
            )
            if not env_names or not set(env_names).issubset(available_env):
                continue
            contracts.append(
                {
                    "capability_id": str(capability.get("id") or "runtime_capability"),
                    "description": str(capability.get("description") or "Runtime capability"),
                    "status": "available",
                    "env_vars": env_names,
                    "usage": str(capability.get("usage") or "Use the injected variables directly."),
                }
            )
        return contracts

    def _runtime_capability_grants_for(
        self, project: Project, agent_type: str, state: str
    ) -> list[str]:
        """Return explicit user-approved runtime capabilities for this run.

        New submissions are persisted in project context.  The structural
        audit fallback also recovers grants from older answered interactions
        where a Coordinator rewrite accidentally stripped option metadata.
        It matches only the origin question id and exact submitted option key.
        """
        allowed = {"HostAutomation"}
        granted: set[str] = set()
        for grant in (project.context or {}).get("runtime_capability_grants", []):
            if not isinstance(grant, dict):
                continue
            if grant.get("agent") not in {None, "", agent_type}:
                continue
            if grant.get("state") not in {None, "", state}:
                continue
            granted.update(
                str(capability)
                for capability in (grant.get("capabilities") or [])
                if str(capability) in allowed
            )
        interactions = self.project_service.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project.id,
                QuestionInteraction.agent == agent_type,
                QuestionInteraction.resume_state == state,
            )
        ).all()
        for interaction in interactions:
            origin_task = (
                self.project_service.session.get(Task, interaction.origin_task_id)
                if interaction.origin_task_id
                else None
            )
            output = (
                origin_task.output_data
                if origin_task is not None
                and isinstance(origin_task.output_data, dict)
                else {}
            )
            origin_questions = {
                str(question.get("question_id")): question
                for question in (
                    (output.get("question_batch") or {}).get("questions") or []
                )
                if isinstance(question, dict) and question.get("question_id")
            }
            for question in self.project_service.get_questions_for_interaction(
                interaction.id
            ):
                answer = self.project_service.get_question_answer(question.id)
                if answer is None or answer.skipped or not answer.answer:
                    continue
                origin_question = origin_questions.get(str(question.question_id))
                if not isinstance(origin_question, dict):
                    continue
                selected = next(
                    (
                        option
                        for option in (origin_question.get("options") or [])
                        if isinstance(option, dict)
                        and str(option.get("key") or "") == str(answer.answer)
                    ),
                    None,
                )
                if not isinstance(selected, dict):
                    continue
                granted.update(
                    str(capability)
                    for capability in (
                        selected.get("grants_runtime_capabilities") or []
                    )
                    if str(capability) in allowed
                )
        return sorted(granted)

    @staticmethod
    def _runtime_capability_grant_contract(capability: str) -> dict[str, Any]:
        """Describe an explicit grant in the same contract used to enforce it."""
        contracts = {
            "HostAutomation": {
                "capability_id": "HostAutomation",
                "description": (
                    "User-approved host automation for this Agent and workflow state"
                ),
                "status": "available",
                "env_vars": [],
                "usage": (
                    "Host filesystem automation, local dependency installation, user-level "
                    "runtime directories, and local socket operations are authorized for this "
                    "run. Inspect actual prerequisites and proceed without asking for this "
                    "permission again."
                ),
            }
        }
        return dict(contracts[capability])

    def _available_runtime_capability_contracts(
        self,
        project: Project,
        agent_type: str,
        state: str,
    ) -> list[dict[str, Any]]:
        """Keep the prompt capability contract aligned with runtime enforcement."""
        contracts = self._configured_runtime_capability_contracts(agent_type)
        contracts.extend(
            self._runtime_capability_grant_contract(capability)
            for capability in self._runtime_capability_grants_for(
                project, agent_type, state
            )
        )
        return contracts

    def _max_tool_calls_for(self, agent_type: str) -> int | None:
        """Return the enforced runtime tool budget declared for an Agent."""
        config = (self.prompt_renderer.agent_config.get("agents") or {}).get(
            agent_type, {}
        )
        value = config.get("max_tool_calls") if isinstance(config, dict) else None
        return int(value) if isinstance(value, int) and value >= 0 else None

    def _max_tool_calls_for_attempt(
        self,
        agent_type: str,
        *,
        retry_count: int,
        retry_feedback: str,
    ) -> int | None:
        """Raise only the resource that caused a bounded internal retry.

        Retrying an Agent after it exhausted the exact same tool ceiling is
        deterministic waste. A single retry gets 50% more calls (at least ten)
        when the persisted failure evidence identifies that ceiling. Other
        failures keep the configured budget unchanged.
        """
        configured = self._max_tool_calls_for(agent_type)
        if (
            configured is not None
            and retry_count > 0
            and "tool-call budget exceeded" in retry_feedback.lower()
        ):
            return configured + max(10, configured // 2)
        return configured

    def _context_files_for_state(self, project: Project, state: str) -> list[Path]:
        mac = project_mac_dir(project)
        files = [mac / "STATE.json"]
        contract_dirs = self._artifact_dirs_for_project(project)

        def authoritative_artifacts(filenames: tuple[str, ...]) -> list[Path]:
            """Select the nearest valid version of each artifact kind.

            The WorkItem already carries the cumulative acceptance contract.
            Passing every artifact from every ancestor duplicates history and
            invites quality agents to resurrect superseded scope.  A role gets
            at most one concrete artifact of each kind, preferring the current
            project and then walking toward the baseline.
            """
            selected: list[Path] = []
            for filename in filenames:
                candidate = next(
                    (
                        artifact_dir / filename
                        for artifact_dir in contract_dirs
                        if (artifact_dir / filename).exists()
                    ),
                    None,
                )
                if candidate is not None:
                    selected.append(candidate)
            return selected

        if state in ("PLANNING_REQUIREMENTS",):
            files.append(mac / "TASK.json")
            if (mac / "REQUIREMENTS.json").exists():
                files.append(mac / "REQUIREMENTS.json")
        elif state in ("PLANNING_ARCHITECTURE",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json"])
        elif state in ("PLANNING_IMPLEMENTATION_SPECS",):
            files.extend([mac / "STATE.json", mac / "REQUIREMENTS.json", mac / "ARCHITECTURE.json"])
        elif state in ("CODING", "TESTING", "REVIEW"):
            files.extend([mac / "STATE.json"])
            files.extend(authoritative_artifacts((
                "ITERATION_BRIEF.json",
                "REQUIREMENTS.json",
                "ARCHITECTURE.json",
                "SPECS.json",
                "BUG_ANALYSIS.json",
                "DOC_OUTLINE.json",
            )))
            if state in ("TESTING", "REVIEW") and (mac / "CODER_OUTPUT.json").exists():
                files.append(mac / "CODER_OUTPUT.json")
            if state == "REVIEW" and (mac / "TESTER_OUTPUT.json").exists():
                files.append(mac / "TESTER_OUTPUT.json")

        elif state == "ACCEPTANCE_PENDING":
            files.extend(authoritative_artifacts((
                "ITERATION_BRIEF.json",
                "REQUIREMENTS.json",
                "ARCHITECTURE.json",
                "SPECS.json",
                "BUG_ANALYSIS.json",
                "DOC_OUTLINE.json",
            )))
            files.extend(
                mac / filename
                for filename in (
                    "CODER_OUTPUT.json",
                    "TESTER_OUTPUT.json",
                    "REVIEWER_OUTPUT.json",
                    "ACCEPTANCE_AGENT_OUTPUT.json",
                )
            )

        return list(dict.fromkeys(f for f in files if f.exists()))

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
        # ``output`` is the schema-validated runtime result.  The file an
        # Agent writes in its worktree is only a transport convenience and can
        # diverge from stdout (for example an unescaped quote can leave an
        # otherwise valid Tester result as malformed JSON).  Copying that file
        # made downstream Reviewer/Acceptance fail even though the
        # Orchestrator had already accepted a valid object.  Persist the one
        # canonical validated representation instead.
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

        if state == "PLANNING_REQUIREMENTS":
            reference_src = (
                worktree / ".mac" / "reference" / "REFERENCE_CONTRACT.json"
            )
            if reference_src.is_file():
                shutil.copy2(reference_src, dst_mac / "REFERENCE_CONTRACT.json")
                logger.debug("Copied REFERENCE_CONTRACT.json from worktree to workspace")

    def _planner_artifact_error(
        self,
        project: Project,
        worktree: Path,
        state: str,
        *,
        expected_round: int | None = None,
    ) -> str | None:
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
        inherited_unchanged = False
        main_artifact = project_mac_dir(project) / filename
        if main_artifact.is_file():
            try:
                if artifact_path.read_bytes() == main_artifact.read_bytes():
                    inherited_unchanged = True
            except OSError:
                pass
        try:
            parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"Planner artifact {filename} is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return f"Planner artifact {filename} must contain a JSON object"
        stage_by_state = {
            "PLANNING_REQUIREMENTS": "requirements",
            "PLANNING_ARCHITECTURE": "architecture",
            "PLANNING_IMPLEMENTATION_SPECS": "specs",
            "BUG_ANALYSIS": "bug_analysis",
            "DOC_OUTLINE": "doc_outline",
        }
        agent_by_state = {
            "PLANNING_REQUIREMENTS": "requirements_analyst",
            "PLANNING_ARCHITECTURE": "architect",
            "PLANNING_IMPLEMENTATION_SPECS": "spec_writer",
            "BUG_ANALYSIS": "bug_analyst",
            "DOC_OUTLINE": "documentation_planner",
        }
        schema_path = self.prompt_renderer.get_artifact_schema_path(
            agent_by_state[state], stage_by_state[state]
        )
        if schema_path is not None:
            contract_error = validate_agent_output(parsed, schema_path)
            if contract_error:
                if inherited_unchanged:
                    # Legacy fallback artifacts are intentionally ignored; old
                    # runtime data is migrated/cleared separately and must not
                    # be mistaken for a new malformed write.
                    return None
                return f"Planner artifact {filename} is invalid: {contract_error}"
        blocking_feedback = any(
            item.blocking
            for item in self.project_service.get_open_feedback_packets(project.id)
        )
        if blocking_feedback and expected_round is not None:
            artifact_round = parsed.get("round")
            if artifact_round != expected_round:
                return (
                    f"Planner artifact {filename} has round {artifact_round!r}; "
                    f"expected round {expected_round} after blocking feedback"
                )
        if inherited_unchanged and blocking_feedback:
            return (
                f"Planner artifact {filename} was not revised despite open "
                "blocking feedback"
            )
        return None

    def _recover_planner_turn_from_artifact(
        self,
        *,
        project: Project,
        worktree: Path,
        state: str,
        task: Task,
        runtime_logs: str | None,
    ) -> dict[str, Any] | None:
        """Recover a completed planner turn when only stdout formatting failed.

        Planner stage artifacts intentionally use a different schema from the
        small turn-result object, so the Runtime cannot use its normal canonical
        output fallback.  A schema-valid, current-round artifact with
        ``pending_confirmation`` status is nevertheless an authoritative
        completion signal.  Recovery is deliberately limited to the Runtime's
        JSON-protocol failure; API, timeout, tool-budget and schema failures
        still take their normal retry path.
        """
        if "Invalid agent output: result is not a JSON object" not in str(
            runtime_logs or ""
        ):
            return None
        filenames = {
            "PLANNING_REQUIREMENTS": "REQUIREMENTS.json",
            "PLANNING_ARCHITECTURE": "ARCHITECTURE.json",
            "PLANNING_IMPLEMENTATION_SPECS": "SPECS.json",
            "BUG_ANALYSIS": "BUG_ANALYSIS.json",
            "DOC_OUTLINE": "DOC_OUTLINE.json",
        }
        filename = filenames.get(state)
        if filename is None:
            return None
        artifact_path = worktree / ".mac" / filename
        if not artifact_path.is_file():
            return None
        if self._planner_artifact_error(
            project,
            worktree,
            state,
            expected_round=task.round_number,
        ):
            return None
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if artifact.get("status") != "pending_confirmation":
            return None
        return {
            "success": True,
            "next_state_event": "complete",
            "modified_files": [
                {
                    "path": f".mac/{filename}",
                    "description": "Schema-valid planner artifact",
                }
            ],
            "notes": [
                "Recovered the turn result from the validated durable artifact "
                "after stdout protocol formatting failed."
            ],
        }

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
            options = state_machine.rework_options()
            # Surface the quality agent's recommended target so users (and the
            # E2E auto-takeover helper) can see which option the failing agent
            # suggested, instead of guessing among equally-styled buttons.
            open_feedback = self._current_feedback_frontier(
                self.project_service.get_open_feedback_packets(project.id)
            )
            recommended_stage = next(
                (fb.recommended_rework_target for fb in open_feedback
                 if fb.recommended_rework_target),
                None,
            )
            stage_by_id = {stage["id"]: stage for stage in state_machine.profile["stages"]}
            action_by_stage = {
                item["target_stage"]: item["action"]
                for item in state_machine.profile.get("rework_targets", [])
                if item.get("target_stage") in stage_by_id
            }
            recommended_action = action_by_stage.get(recommended_stage or "")
            enriched: list[dict] = []
            for opt in options:
                new_opt = {**opt, "option_key": opt["action"]}
                if recommended_action and opt["action"] == recommended_action:
                    new_opt["type"] = "primary"
                enriched.append(new_opt)
            return "本轮未通过，请选择如何处理", enriched

        if state.endswith("_RECOVERY_PENDING"):
            failure = project.context.get("last_agent_failure", {})
            agent = self._agent_type_label(failure.get("agent", "Agent"))
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
            return f"{agent} 连续 {attempts} 次尝试未产生可用结果，请选择恢复方式", options

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
                    {"action": "retry", "label": "重新执行验收", "next_state": "ACCEPTANCE_PENDING"},
                    {"action": "reject", "label": "驳回并选择重做层级", "next_state": "REWORK"},
                    *state_machine.acceptance_rework_options(),
                    restart_option,
                ],
            )

        return (f"请确认 {state}", [{"action": "confirm", "label": "确认", "next_state": "COMPLETED"}])

    def _build_input_question(self, project: Project, state: str) -> str:
        if state == "CLARIFICATION_PENDING":
            from mac_system.models import QuestionInteraction as QI

            interaction = self.project_service.session.exec(
                select(QI)
                .where(
                    QI.project_id == project.id,
                    QI.status == "pending",
                )
                .order_by(QI.created_at.desc())
            ).first()
            if interaction is not None:
                questions = [
                    question.question.strip()
                    for question in self.project_service.get_questions_for_interaction(
                        interaction.id
                    )
                    if question.status == "pending" and question.question.strip()
                ]
                if questions:
                    return "Agent 请求澄清：\n" + "\n".join(
                        f"{index}. {question}"
                        for index, question in enumerate(questions, start=1)
                    )

        pending_input = project.context.get("clarification_answer")
        if pending_input and state in {
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
                f"**执行未完成（运行或输出协议问题）**\n"
                f"- Agent：{self._agent_type_label(failure.get('agent', 'unknown'))}\n"
                f"- 阶段：{failure.get('state', state)}\n"
                f"- 已尝试：{failure.get('attempts', '多次')} 次\n"
                f"- 原因：{error}\n\n"
                "这不是质量验收结论。请选择再次重试、回退处理或终止项目。"
            )

        # Quality gates: pull the latest completed task output for the report card.
        quality_gate_agent = {
            "CODING_CONFIRM_PENDING": "coder",
            "TESTING_CONFIRM_PENDING": "tester",
            "REVIEW_CONFIRM_PENDING": "reviewer",
        }.get(state)
        if quality_gate_agent:
            execution_state = state.removesuffix("_CONFIRM_PENDING")
            skipped = next(
                (
                    event
                    for event in self.project_service.get_project_events(
                        project.id, limit=30
                    )
                    if (event.meta or {}).get("event_type") == "stage_skipped"
                    and (event.meta or {}).get("execution_state") == execution_state
                ),
                None,
            )
            if skipped is not None:
                policy = (skipped.meta or {}).get("policy") or {}
                return (
                    f"**{StateMachine(project.task_type).display_name(state)}："
                    "交付策略未要求独立 Agent 执行**\n"
                    f"- 风险等级：{policy.get('risk_level', '未指定')}\n"
                    f"- 变更表面：{policy.get('change_surface', '未指定')}\n"
                    f"- 依据：{policy.get('reason', '当前验收合同不要求这一类独立证据')}\n"
                    "- 说明：跳过情况会显示在卡片和审计记录中，不会伪装成 Agent 已执行。"
                )
            return self._build_agent_task_summary(project, quality_gate_agent)

        if state == "REWORK":
            open_feedback = self.project_service.get_open_feedback_packets(project.id)
            if open_feedback:
                frontier = self._current_feedback_frontier(open_feedback)
                parts = ["**质量检查未通过，已进入正常返工**", "**未解决的反馈**"]
                for fb in frontier:
                    target = fb.recommended_rework_target or "未指定"
                    parts.append(f"- [{fb.source_stage}] {fb.summary}（建议回退：{target}）")
                historical_count = len(open_feedback) - len(frontier)
                if historical_count:
                    parts.append(
                        f"- 另有 {historical_count} 条历史反馈保留在验证账本中，"
                        "不会参与本轮路由。"
                    )
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
            coverage_status = out.get("coverage_status")
            coverage = out.get("coverage")
            if coverage_status == "not_measured":
                parts.append("**覆盖率**：未测量")
            elif coverage_status == "not_applicable":
                parts.append("**覆盖率**：不适用")
            elif coverage is not None:
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
        active_acceptance_task = next((t for t in tasks if t.agent_type == "acceptance_agent"), None)
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
                coverage_status = out.get("coverage_status")
                coverage = out.get("coverage")
                status = "通过" if passed else "未通过"
                line = f"测试状态：{status}"
                if coverage_status == "not_measured":
                    line += "，覆盖率未测量"
                elif coverage_status == "not_applicable":
                    line += "，覆盖率不适用"
                elif coverage is not None:
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

        if active_acceptance_task and active_acceptance_task.output_data:
            out = active_acceptance_task.output_data
            criteria = [
                item for item in (out.get("criteria") or []) if isinstance(item, dict)
            ]
            passed = sum(1 for item in criteria if item.get("result") == "pass")
            failed = sum(1 for item in criteria if item.get("result") == "fail")
            needs_user = sum(1 for item in criteria if item.get("result") == "needs_user")
            lines = [str(out.get("summary") or "主动黑盒验收已执行")]
            lines.append(
                f"逐项结果：{passed} 通过 / {failed} 失败 / {needs_user} 需要外部事实"
            )
            entry_points = (out.get("environment") or {}).get("entry_points") or []
            if entry_points:
                lines.append("真实入口：" + "、".join(str(value) for value in entry_points[:4]))
            exceptional = [
                item
                for item in criteria
                if item.get("result") in {"fail", "needs_user", "blocked"}
            ]
            for item in exceptional[:3]:
                ac_id = str(item.get("acceptance_criterion_id") or "未知验收项")
                result_label = {
                    "fail": "失败",
                    "needs_user": "需要用户信息",
                    "blocked": "系统阻塞",
                }.get(str(item.get("result")), str(item.get("result")))
                actual = str(item.get("actual") or "未提供实际结果")
                lines.append(f"{ac_id}（{result_label}）：{actual}")
            parts.append("**主动黑盒验收**\n" + "\n".join(lines))

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
        # Recovery cards must label the FAILED agent, not the last *completed*
        # task. When the coder fails, all coder tasks are failed and the most
        # recent completed task is the planner - surfacing "生成者: 需求分析师"
        # would mislabel the failure as a planner failure.
        details: dict[str, str] = {}
        if state.endswith("_RECOVERY_PENDING"):
            failure = project.context.get("last_agent_failure", {}) or {}
            failed_agent = failure.get("agent")
            if failed_agent:
                details["失败 Agent"] = self._agent_type_label(failed_agent)
            return details

        # Agent label for the most recently completed task.
        agent_label = self._last_agent_label(project)
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
            "requirements_analyst": "需求分析师",
            "architect": "架构师",
            "spec_writer": "实现计划 Agent",
            "bug_analyst": "Bug 分析师",
            "documentation_planner": "文档策划 Agent",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理",
            "acceptance_agent": "主动验收 Agent",
            "acceptance_evaluator": "验收评估",
        }
        agent_display = agent_label_map.get(task.agent_type, task.agent_type or "Agent")
        project_tag = project.name or project.id

        task_projection_id = str(
            getattr(task, "id", None)
            or f"{project.id}:{task.agent_type}:{project.current_state}"
        )
        progress_key = f"task:{task_projection_id}:progress"
        progress_projection = self.project_service.get_card_projection(progress_key)
        progress_message_id: str | None = (
            progress_projection.provider_message_id if progress_projection else None
        )

        async def _cb(event_type: str, content: str) -> None:
            nonlocal progress_message_id
            try:
                if event_type == "thinking":
                    title = f"[{project_tag}] {agent_display} · 思考中"
                    # Runtime thinking/text blocks may contain chain-of-thought,
                    # tentative claims, host paths, or a draft JSON result.
                    # Progress is a user-facing status surface, not a debug
                    # stream, so expose only a stable phase description.
                    card_content = "正在分析当前阶段并整理结果"
                    color = "blue"
                elif event_type == "tool":
                    data = json.loads(content)
                    name = data.get("name", "?")
                    inp = data.get("input", {})
                    preview = format_tool_preview(name, inp)
                    title = f"[{project_tag}] {agent_display} · 正在执行"
                    card_content = f"⚙️ {preview}"
                    color = "purple"
                else:
                    return

                from mac_system.feishu.card import build_content_card

                card = build_content_card(title, card_content, color=color)

                if progress_message_id is None:
                    progress_message_id = await deliver_card(
                        self.project_service,
                        self.feishu_bridge,
                        dedupe_key=f"{progress_key}:create",
                        recipient_id=receive_id,
                        recipient_type=receive_id_type,
                        intent_type="progress",
                        card=card,
                        project_id=project.id,
                        projection_key=progress_key,
                        subject_type="task",
                        subject_id=task_projection_id,
                        lifecycle_status="running",
                        resulting_state=project.current_state,
                    )
                else:
                    digest = hashlib.sha256(
                        json.dumps(card, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:20]
                    await deliver_card(
                        self.project_service,
                        self.feishu_bridge,
                        dedupe_key=f"{progress_key}:update:{digest}",
                        recipient_id=receive_id,
                        recipient_type=receive_id_type,
                        intent_type="progress",
                        card=card,
                        project_id=project.id,
                        update_message_id=progress_message_id,
                        projection_key=progress_key,
                        subject_type="task",
                        subject_id=task_projection_id,
                        lifecycle_status="running",
                        resulting_state=project.current_state,
                    )
            except Exception as exc:
                logger.warning("progress_callback failed: %s", exc)
        return _cb

    async def _finalize_progress_card(
        self,
        project: Project,
        task: Any,
        *,
        succeeded: bool,
    ) -> None:
        """Close the durable progress projection and its visible card."""
        if self.feishu_bridge is None or not self.settings.debug_agent_progress:
            return
        task_id = str(getattr(task, "id", "") or "").strip()
        if not task_id:
            return
        projection_key = f"task:{task_id}:progress"
        projection = self.project_service.get_card_projection(projection_key)
        if projection is None or not projection.provider_message_id:
            return
        agent_label_map = {
            "planner": "需求分析师",
            "requirements_analyst": "需求分析师",
            "architect": "架构师",
            "spec_writer": "实现计划 Agent",
            "bug_analyst": "Bug 分析师",
            "documentation_planner": "文档策划 Agent",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理",
            "acceptance_agent": "主动验收 Agent",
            "acceptance_evaluator": "验收评估",
        }
        agent_type = str(getattr(task, "agent_type", "") or "Agent")
        agent_display = agent_label_map.get(agent_type, agent_type)
        project_tag = project.name or project.id
        lifecycle = "completed" if succeeded else "failed"
        color = "green" if succeeded else "red"
        status_text = "已完成" if succeeded else "执行失败，系统正在处理"
        from mac_system.feishu.card import build_content_card

        card = build_content_card(
            f"[{project_tag}] {agent_display} · {status_text}",
            "本阶段执行已结束，后续状态以最新汇报卡和决策卡为准。",
            color=color,
        )
        receive_id, receive_id_type = self._resolve_project_recipient(project)
        try:
            await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"{projection_key}:final:{lifecycle}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="progress",
                card=card,
                project_id=project.id,
                update_message_id=projection.provider_message_id,
                projection_key=projection_key,
                subject_type="task",
                subject_id=task_id,
                lifecycle_status=lifecycle,
                resulting_state=project.current_state,
            )
        except Exception:
            logger.warning(
                "Failed to finalize progress card for task %s",
                task_id,
                exc_info=True,
            )

    def _last_agent_label(self, project: Project) -> str:
        """Human-readable label for the last completed agent (e.g. 需求分析师)."""
        agent_label_map = {
            "planner": "需求分析师",
            "requirements_analyst": "需求分析师",
            "architect": "架构师",
            "spec_writer": "实现计划 Agent",
            "bug_analyst": "Bug 分析师",
            "documentation_planner": "文档策划 Agent",
            "coder": "编码 Agent",
            "tester": "测试 Agent",
            "reviewer": "Review Agent",
            "project_manager": "项目经理 Agent",
            "acceptance_agent": "主动验收 Agent",
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
            from mac_system.feishu.card import build_content_card

            await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=(
                    f"project:{project.id}:auto-resolution:"
                    f"{from_state}:{chosen}:{project.context_version}"
                ),
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="decision",
                card=build_content_card(
                    f"[{name}] 自动决策",
                    line,
                    color="green",
                ),
                project_id=project.id,
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
            existing_message_id = (project.context or {}).get(
                "transition_card_message_id"
            )
            message_id = await deliver_card(
                self.project_service,
                self.feishu_bridge,
                dedupe_key=f"project:{project.id}:transition:{from_state}:{to_state}:{project.updated_at.isoformat()}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="progress",
                card=card,
                project_id=project.id,
                update_message_id=existing_message_id,
            )
            if message_id and message_id != existing_message_id:
                refreshed = self.project_service.require_project(project.id)
                refreshed.context = {
                    **refreshed.context,
                    "transition_card_message_id": message_id,
                }
                self.project_service.session.add(refreshed)
                self.project_service.session.commit()
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

    _AGENT_TYPE_LABELS = {
        "planner": "需求分析师",
        "requirements_analyst": "需求分析师",
        "architect": "架构师",
        "spec_writer": "实现计划 Agent",
        "bug_analyst": "Bug 分析师",
        "documentation_planner": "文档策划 Agent",
        "coder": "编码 Agent",
        "tester": "测试 Agent",
        "reviewer": "Review Agent",
        "project_manager": "项目经理 Agent",
        "acceptance_agent": "主动验收 Agent",
        "acceptance_evaluator": "验收评估 Agent",
    }

    @classmethod
    def _agent_type_label(cls, agent_type: str | None) -> str:
        """Map a raw agent_type string (e.g. ``coder``) to its Chinese label."""
        if not agent_type:
            return "Agent"
        return cls._AGENT_TYPE_LABELS.get(agent_type, agent_type)

    def _agent_label_for_state(self, state: str) -> str:
        """Map a state name to the Chinese label of the agent that handles it."""
        # Try each task type; human_confirmation / terminal states don't have agents.
        agent_type = None
        for tt in ("feature", "iteration", "bug_fix", "documentation"):
            try:
                sm = StateMachine(tt)
                agent_type = sm.agent_for(state)
                if agent_type:
                    break
            except (KeyError, FileNotFoundError, ValueError):
                continue
        return self._agent_type_label(agent_type)

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
