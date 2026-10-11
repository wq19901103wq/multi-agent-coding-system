"""Persistence boundary for the native root thread and MAC extensions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from sqlalchemy import delete, func, literal_column
from sqlmodel import Session, select

from mac_system.agents.root_runtime import AgentEvent, ToolContext
from mac_system.core.project_service import ProjectService, generate_id
from mac_system.models import (
    AgentSession,
    Approval,
    RootAgentSession,
    DynamicToolCall,
    EventLog,
    Project,
    ProjectDecision,
    ProjectHistoryRound,
    QuestionInteraction,
)


def _now() -> datetime:
    return datetime.utcnow()


SHUTDOWN_INTERRUPTED_ERROR = (
    "MAC service shutdown interrupted the native Codex turn"
)


class RootSessionStore:
    """Small transactional API; it does not decide project workflow."""

    def __init__(self, session: Session, *, default_provider: str = "codex"):
        self.session = session
        self.default_provider = default_provider.strip() or "codex"

    def ensure(
        self, project_id: str, provider: str | None = None
    ) -> RootAgentSession:
        existing = self.session.exec(
            select(RootAgentSession).where(
                RootAgentSession.project_id == project_id
            )
        ).first()
        if existing is not None:
            return existing
        selected_provider = (provider or self.default_provider).strip() or self.default_provider
        row = RootAgentSession(
            id=generate_id("coord"),
            project_id=project_id,
            provider=selected_provider,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_native_thread(self, project_id: str, native_thread_id: str) -> RootAgentSession:
        row = self.ensure(project_id)
        row.native_thread_id = native_thread_id
        if row.active_history_round_id:
            history = self.session.get(
                ProjectHistoryRound, row.active_history_round_id
            )
            if history is not None:
                history.native_thread_id = native_thread_id
                history.updated_at = _now()
                self.session.add(history)
        self.session.add(row)
        return self.set_turn(project_id, turn_id=None, status="idle")

    def clear_native_thread(self, project_id: str) -> RootAgentSession:
        """Detach the project from its current Codex thread.

        This is only used by the explicit project-level ``/newthread`` control.
        The old Codex rollout remains on disk; MAC simply stops resuming it.
        """

        row = self.ensure(project_id)
        row.native_thread_id = None
        self.session.add(row)
        return self.set_turn(project_id, turn_id=None, status="idle")

    def switch_provider(self, project_id: str, provider: str) -> tuple[str, str]:
        """Commit an explicit idle-project provider choice without starting work."""

        row = self.ensure(project_id)
        if row.status != "idle" or row.active_turn_id is not None:
            raise RuntimeError("当前项目仍在处理消息，请等本轮结束后再切换助手。")
        previous = row.provider
        if previous == provider:
            return previous, provider
        old_thread_id = row.native_thread_id
        if row.provider_handoff:
            self.session.add(EventLog(
                project_id=project_id,
                level="INFO",
                message="Previous provider handoff archived by explicit user command",
                meta={"provider_handoff": dict(row.provider_handoff)},
            ))
        row.provider = provider
        row.native_thread_id = None
        row.provider_handoff = None
        row.restart_recovery_pending = False
        row.last_error = None
        row.updated_at = _now()
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        project.context = {
            **(project.context or {}),
            "execution_provider": provider,
            "project_lead_provider": provider,
        }
        project.updated_at = _now()
        self.session.add(row)
        self.session.add(project)
        self.session.add(EventLog(
            project_id=project_id,
            level="INFO",
            message="Project Lead provider changed by explicit user command",
            meta={"from_provider": previous, "to_provider": provider,
                  "old_thread_id": old_thread_id},
        ))
        self.session.commit()
        return previous, provider

    def can_prepare_provider_handoff(
        self, project_id: str, *, provider: str,
    ) -> bool:
        row = self.ensure(project_id)
        handoff = row.provider_handoff
        if not handoff:
            return True
        return (
            row.provider == "codex"
            and provider == "kimi_code"
            and (
                handoff.get("to_provider") == "kimi_code"
                or (
                    handoff.get("from_provider") == "kimi_code"
                    and handoff.get("to_provider") == "codex"
                    and handoff.get("status") == "completed"
                )
            )
        )

    def prepare_provider_handoff(
        self, project_id: str, *, provider: str, thread_id: str,
        reason: str, public_context: str,
    ) -> RootAgentSession:
        """Commit target identity + public handoff before starting its process.

        The caller must first stop the previous runtime and all its writers.
        The original provider/thread remain available for audit, never reused
        as a different provider's conversation identifier.
        """
        row = self.ensure(project_id)
        if row.provider_handoff:
            if not self.can_prepare_provider_handoff(project_id, provider=provider):
                raise ValueError("This project already has a provider handoff")
            # Returning to Codex is an explicit provider change, not an
            # automatic retry loop. Preserve the previous cycle for audit.
            self.session.add(EventLog(
                project_id=project_id, level="INFO",
                message="Previous provider handoff archived after return to Codex",
                meta={"provider_handoff": dict(row.provider_handoff)},
            ))
        row.provider_handoff = {
            "id": generate_id("handoff"), "from_provider": row.provider,
            "from_thread_id": row.native_thread_id, "to_provider": provider,
            "to_thread_id": thread_id, "reason": reason,
            "public_context": public_context, "status": "pending",
        }
        row.provider = provider
        row.native_thread_id = thread_id
        row.active_turn_id = None
        row.status = "running"
        row.updated_at = _now()
        project = self.session.get(Project, project_id)
        project.context = {**(project.context or {}), "project_lead_provider": provider}
        project.execution_status = "running"
        if row.active_history_round_id:
            history = self.session.get(ProjectHistoryRound, row.active_history_round_id)
            if history:
                history.native_thread_id = thread_id
                self.session.add(history)
        self.session.add(project)
        self.session.add(row)
        self.session.commit()
        return row

    def finish_provider_handoff(self, project_id: str, status: str) -> None:
        row = self.ensure(project_id)
        if row.provider_handoff:
            row.provider_handoff = {**row.provider_handoff, "status": status}
            self.session.add(row)
            self.session.commit()

    def set_turn(
        self,
        project_id: str,
        *,
        turn_id: str | None,
        status: str,
        error: str | None = None,
        reason: str | None = None,
    ) -> RootAgentSession:
        """Persist the native turn and its user-facing Project projection atomically.

        ``RootAgentSession`` is the execution source of truth.  The Project fields
        are a denormalized UI/API projection and must never be updated separately
        for a native Codex turn.
        """

        row = self.ensure(project_id)
        row.active_turn_id = turn_id
        row.status = status
        row.last_error = error
        row.updated_at = _now()
        project = self.session.get(Project, project_id)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        # Native lifecycle events are projected through a short independent
        # Session. Refresh only the execution fields before assigning them so
        # this owning boundary cannot mistake a stale in-memory value for an
        # unchanged database value and skip the terminal UPDATE.
        self.session.refresh(
            project,
            attribute_names=[
                "execution_status",
                "waiting_reason",
                "waiting_since",
                "updated_at",
            ],
        )
        project.execution_status = status
        project.waiting_reason = reason
        project.waiting_since = _now() if status.startswith("waiting") else None
        project.updated_at = _now()
        self.session.add(row)
        self.session.add(project)
        self.session.commit()
        return row

    def begin_turn(
        self,
        project_id: str,
        *,
        user_request: str,
        actor_open_id: str | None = None,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> RootAgentSession:
        """Persist the request needed for safe restart recovery, then run."""

        row = self.ensure(project_id)
        history_service = ProjectService(self.session)
        active_history = (
            self.session.get(ProjectHistoryRound, row.active_history_round_id)
            if row.active_history_round_id
            else None
        )
        if (
            active_history is None
            or active_history.report_status != "pending"
            or request_origin != "system_recovery"
        ):
            if active_history is not None and active_history.report_status == "pending":
                history_service.finish_unreported_project_history_round(
                    active_history.id
                )
            active_history = history_service.begin_project_history_round(
                project_id,
                user_request=user_request,
                actor_open_id=actor_open_id,
                request_origin=request_origin,
                parent_decision_id=parent_decision_id,
                reply_context=reply_context,
                native_thread_id=row.native_thread_id,
            )
            row.active_history_round_id = active_history.id
        row.last_user_request = user_request.strip() or row.last_user_request
        row.last_request_origin = request_origin
        row.last_parent_decision_id = parent_decision_id
        row.restart_recovery_pending = False
        self.session.add(row)
        self.session.commit()
        return self.set_turn(project_id, turn_id=None, status="running")

    def append_history_instruction(
        self,
        project_id: str,
        text: str,
        *,
        actor_open_id: str | None = None,
        origin: str = "human_steer",
        reply_context: dict[str, Any] | None = None,
    ) -> ProjectHistoryRound | None:
        row = self.ensure(project_id)
        if not row.active_history_round_id:
            return None
        return ProjectService(self.session).append_project_history_instruction(
            row.active_history_round_id,
            text,
            actor_open_id=actor_open_id,
            origin=origin,
            reply_context=reply_context,
        )

    def finish_active_history_round(
        self, project_id: str, *, status: str
    ) -> ProjectHistoryRound | None:
        """Close the active public history round at a known terminal boundary."""

        row = self.ensure(project_id)
        if not row.active_history_round_id:
            return None
        history = ProjectService(self.session).finish_unreported_project_history_round(
            row.active_history_round_id,
            status=status,
        )
        row.active_history_round_id = None
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return history

    def get_restart_recovery(
        self, project_id: str
    ) -> tuple[str, str, str, str | None] | None:
        """Return one startup recovery without clearing it prematurely.

        ``begin_turn`` clears the flag once the scheduled task actually owns
        the project lock. If the service exits between discovery and task
        startup, the next process can still recover it.
        """

        row = self.ensure(project_id)
        project = self.session.get(Project, project_id)
        request = str(row.last_user_request or "").strip()
        if (
            not row.restart_recovery_pending
            or not request
            or not row.native_thread_id
            or project is None
            or project.status != "active"
        ):
            return None
        return (
            row.native_thread_id,
            request,
            row.last_request_origin or "human",
            row.last_parent_decision_id,
        )

    def mark_shutdown_interrupted_for_recovery(
        self, project_id: str, history_round_id: str
    ) -> bool:
        """Remember a gracefully interrupted turn after its terminal event.

        Shutdown first asks the native runtime to interrupt. That can make the
        Root session idle and close its history round before startup recovery
        scans it, so the ordinary running-row test is no longer sufficient.
        """

        row = self.ensure(project_id)
        project = self.session.get(Project, project_id)
        history = self.session.get(ProjectHistoryRound, history_round_id)
        latest_history = self.session.exec(
            select(ProjectHistoryRound)
            .where(ProjectHistoryRound.project_id == project_id)
            .order_by(ProjectHistoryRound.sequence.desc())
            .limit(1)
        ).first()
        if not (
            project is not None
            and project.status == "active"
            and row.status == "idle"
            and row.active_history_round_id is None
            and history is not None
            and history.project_id == project_id
            and history.report_status == "interrupted"
            and latest_history is not None
            and latest_history.id == history_round_id
            and row.native_thread_id
            and str(row.last_user_request or "").strip()
        ):
            return False
        row.restart_recovery_pending = True
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return True

    def append_event(self, project_id: str, event: AgentEvent) -> int:
        row = self.ensure(project_id)
        row.event_cursor += 1
        row.updated_at = _now()
        self.session.add(row)
        self.session.add(
            EventLog(
                project_id=project_id,
                level="INFO",
                message=f"Root Agent event: {event.kind}",
                meta={
                    "profile_type": "root_agent_event",
                    "root_agent_session_id": row.id,
                    "event_cursor": row.event_cursor,
                    "thread_id": event.thread_id,
                    "turn_id": event.turn_id,
                    "kind": event.kind,
                    "payload": event.payload,
                },
            )
        )
        self.session.commit()
        return row.event_cursor

    async def execute_tool_once(
        self,
        project_id: str,
        context: ToolContext,
        arguments: dict[str, Any],
        executor: Callable[[str], Any],
    ) -> Any:
        """Persist before side effects and replay a completed result by call id."""
        root = self.ensure(project_id)
        existing = self.session.exec(
            select(DynamicToolCall).where(
                DynamicToolCall.native_thread_id == context.thread_id,
                DynamicToolCall.native_turn_id == context.turn_id,
                DynamicToolCall.native_call_id == context.call_id,
            )
        ).first()
        if existing is not None and existing.status == "completed":
            return existing.result.get("value")
        if existing is None:
            existing = DynamicToolCall(
                id=generate_id("ctool"),
                project_id=project_id,
                root_agent_session_id=root.id,
                native_thread_id=context.thread_id,
                native_turn_id=context.turn_id,
                native_call_id=context.call_id,
                tool_name=context.tool_name,
                arguments=arguments,
                status="running",
            )
            self.session.add(existing)
            self.session.commit()
        else:
            existing.status = "running"
            existing.updated_at = _now()
            self.session.add(existing)
            self.session.commit()

        try:
            value = executor(existing.id)
            if hasattr(value, "__await__"):
                value = await value
        except Exception as exc:
            existing.status = "failed"
            existing.error_message = str(exc)
            existing.updated_at = _now()
            self.session.add(existing)
            self.session.commit()
            raise
        existing.status = "completed"
        existing.result = {"value": value}
        existing.error_message = None
        existing.updated_at = _now()
        self.session.add(existing)
        self.session.commit()
        return value

    def get_agent(self, project_id: str, task_name: str) -> AgentSession | None:
        return self.session.exec(
            select(AgentSession).where(
                AgentSession.project_id == project_id,
                AgentSession.task_name == task_name,
            )
        ).first()

    def list_agents(self, project_id: str) -> list[AgentSession]:
        return list(
            self.session.exec(
                select(AgentSession)
                .where(AgentSession.project_id == project_id)
                .order_by(AgentSession.created_at)
            ).all()
        )

    def provider_metrics(
        self, providers: list[str], *, sample_limit: int = 20
    ) -> dict[str, dict[str, float | int]]:
        """Aggregate recent process outcomes without exposing task content."""
        wanted = set(providers)
        samples: dict[str, list[AgentSession]] = {provider: [] for provider in providers}
        rows = self.session.exec(
            select(AgentSession)
            .where(AgentSession.status.in_(("completed", "failed")))
            .order_by(AgentSession.created_at.desc())
        ).all()
        for row in rows:
            if row.provider not in wanted or len(samples[row.provider]) >= sample_limit:
                continue
            samples[row.provider].append(row)

        metrics: dict[str, dict[str, float | int]] = {}
        for provider, provider_rows in samples.items():
            if not provider_rows:
                metrics[provider] = {"sample_count": 0}
                continue
            durations = [
                max(0.0, (row.updated_at - row.created_at).total_seconds())
                for row in provider_rows
            ]
            costs = []
            for row in provider_rows:
                cost = (row.result.get("cost") or {}).get("estimated_cost_usd")
                if isinstance(cost, (int, float)):
                    costs.append(float(cost))
            metric: dict[str, float | int] = {
                "sample_count": len(provider_rows),
                "completed_count": sum(
                    row.status == "completed" for row in provider_rows
                ),
                "average_duration_seconds": round(
                    sum(durations) / len(durations), 1
                ),
                "average_tool_calls": round(
                    sum(
                        len(row.result.get("tool_calls") or [])
                        for row in provider_rows
                    )
                    / len(provider_rows),
                    1,
                ),
            }
            if costs:
                metric["average_cost_usd"] = round(sum(costs) / len(costs), 4)
            metrics[provider] = metric
        return metrics

    def create_agent(
        self,
        project_id: str,
        *,
        task_name: str,
        provider: str,
        access: str,
        objective: str,
        tool_call_id: str,
        selection_reason: str | None = None,
    ) -> AgentSession:
        existing = self.get_agent(project_id, task_name)
        if existing is not None:
            raise ValueError(
                f"external agent task name already exists: {task_name}"
            )
        root = self.ensure(project_id)
        row = AgentSession(
            id=generate_id("agent"),
            project_id=project_id,
            root_agent_session_id=root.id,
            task_name=task_name,
            provider=provider,
            access=access,
            objective=objective,
            status="pending",
            result={
                "spawn_tool_call_id": tool_call_id,
                "selection_reason": selection_reason,
            },
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def update_agent(
        self,
        row: AgentSession,
        *,
        status: str,
        latest_message: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> AgentSession:
        row.status = status
        row.latest_message = latest_message
        if result is not None:
            row.result = result
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row


def recover_interrupted_native_turns(session: Session) -> list[str]:
    """Reconcile every native runtime projection after process startup.

    Codex thread ids remain resumable, but an in-flight turn and its server
    requests belong to the app-server process that owned them. On MAC process
    startup there cannot be a live owner for rows marked running, waiting, or
    resolving. Project, session, approval, and question projections are all
    normalized from that single fact, including stale rows left by older MAC
    versions that already marked the Root session idle.
    """

    changed_projects: set[str] = set()
    roots = list(session.exec(select(RootAgentSession)).all())
    root_by_project = {row.project_id: row for row in roots}
    for row in roots:
        project = session.get(Project, row.project_id)
        if project is not None and (project.context or {}).get("runtime_reset_required"):
            row.restart_recovery_pending = False
            row.status = "resetting"
            session.add(row)
            continue
        active_history = (
            session.get(ProjectHistoryRound, row.active_history_round_id)
            if row.active_history_round_id
            else None
        )
        has_pending_history = bool(
            active_history is not None
            and active_history.report_status == "pending"
        )
        was_interrupted = (
            row.status in {"running", "owner_reviewing", "waiting_user"}
            or row.last_error == SHUTDOWN_INTERRUPTED_ERROR
            # A transport can fail before turn/start and leave the project
            # idle with an open history round. After process restart no old
            # writer survives, so this unfinished request needs one safe
            # continuation rather than remaining silently pending forever.
            or (
                row.status == "idle"
                and has_pending_history
                and bool(row.last_error)
            )
        )
        if was_interrupted:
            if (
                row.native_thread_id
                and str(row.last_user_request or "").strip()
                and (has_pending_history or row.restart_recovery_pending)
                and (project is None or project.status == "active")
            ):
                row.restart_recovery_pending = True
            else:
                row.restart_recovery_pending = False
            row.status = "idle"
            row.active_turn_id = None
            row.last_error = (
                "Previous native Codex turn was interrupted by service restart"
            )
            row.updated_at = _now()
            session.add(row)
            changed_projects.add(row.project_id)
        elif row.restart_recovery_pending:
            # Graceful shutdown already emitted turn/completed(interrupted)
            # and projected idle, but still owes one safe continuation.
            changed_projects.add(row.project_id)

    for project in session.exec(select(Project)).all():
        root = root_by_project.get(project.id)
        has_live_projection = bool(
            root is not None
            and root.status in {"running", "owner_reviewing", "waiting_user"}
        )
        if (
            project.execution_status in {"running", "owner_reviewing", "waiting_user"}
            and not has_live_projection
        ):
            project.execution_status = "idle"
            project.waiting_reason = None
            project.waiting_since = None
            project.updated_at = _now()
            session.add(project)
            changed_projects.add(project.id)

    orphaned_interactions = list(
        session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.status.in_(("pending", "resolving")),
                QuestionInteraction.agent == "root_agent",
            )
        ).all()
    )
    for interaction in orphaned_interactions:
        root = root_by_project.get(interaction.project_id)
        if root is not None and root.status == "waiting_user":
            continue
        interaction.status = "expired"
        interaction.resolution_source = "native_turn_interrupted"
        interaction.answered_at = _now()
        session.add(interaction)
        changed_projects.add(interaction.project_id)

    for approval in session.exec(
        select(Approval).where(
            Approval.status == "pending",
            Approval.state == "ROOT_AGENT_WAITING_USER",
        )
    ).all():
        root = root_by_project.get(approval.project_id)
        if root is not None and root.status == "waiting_user":
            continue
        approval.status = "expired"
        approval.response = "native_turn_interrupted"
        approval.responded_by = "system"
        approval.responded_at = _now()
        session.add(approval)
        changed_projects.add(approval.project_id)

    for decision in session.exec(
        select(ProjectDecision).where(ProjectDecision.status == "applying")
    ).all():
        decision.status = "application_failed"
        decision.updated_at = _now()
        session.add(decision)
        changed_projects.add(decision.project_id)

    if changed_projects:
        session.commit()
    return sorted(changed_projects)


def recover_interrupted_external_agents(session: Session) -> list[str]:
    """Close provider rows whose owning MAC process no longer exists.

    External coding-agent processes are children of the MAC service and cannot
    survive a service restart as resumable work. Leaving their durable rows in
    ``pending`` or ``running`` makes the mobile UI advertise work that has no
    owner, so startup records the interruption explicitly.
    """

    rows = list(
        session.exec(
            select(AgentSession).where(
                AgentSession.status.in_(("pending", "running"))
            )
        ).all()
    )
    if not rows:
        return []
    message = "External provider execution was interrupted by MAC service restart"
    for row in rows:
        row.status = "failed"
        row.latest_message = message
        row.result = {**(row.result or {}), "error": message, "interrupted": True}
        row.updated_at = _now()
        session.add(row)
    session.commit()
    return [row.id for row in rows]


def prune_redundant_native_event_snapshots(
    session: Session,
    *,
    project_id: str | None = None,
) -> dict[str, int]:
    """Compact MAC observability snapshots that Codex already persists.

    Native command output deltas and diff streams are transient UI input and
    must not become a second rollout store. For cumulative token counters only
    the newest row per native thread is needed as the next turn's baseline;
    per-turn usage is already stored in compact performance profiles.
    """

    native_event = (
        func.json_extract(EventLog.meta, literal_column("'$.profile_type'"))
        == "root_agent_event"
    )
    event_kind = func.json_extract(EventLog.meta, literal_column("'$.kind'"))
    thread_id = func.json_extract(EventLog.meta, literal_column("'$.thread_id'"))
    verbose_kinds = (
        "item/commandExecution/outputDelta",
        "turn/diff/updated",
    )
    verbose_query = delete(EventLog).where(native_event, event_kind.in_(verbose_kinds))
    token_query = (
        select(EventLog.id, EventLog.project_id, thread_id)
        .where(native_event, event_kind == "thread/tokenUsage/updated")
        .order_by(EventLog.project_id, thread_id, EventLog.created_at.desc(), EventLog.id.desc())
    )
    if project_id is not None:
        verbose_query = verbose_query.where(EventLog.project_id == project_id)
        token_query = token_query.where(EventLog.project_id == project_id)
    removed_verbose = session.exec(
        verbose_query.execution_options(synchronize_session=False)
    ).rowcount
    removed_tokens = 0
    previous_group: tuple[str, str] | None = None
    duplicate_ids: list[int] = []
    for event_id, event_project_id, event_thread_id in session.exec(token_query):
        group = (event_project_id, str(event_thread_id or "").strip())
        if not group[1]:
            continue
        if group == previous_group:
            duplicate_ids.append(event_id)
        else:
            previous_group = group
    for start in range(0, len(duplicate_ids), 500):
        batch = duplicate_ids[start:start + 500]
        removed_tokens += session.exec(
            delete(EventLog)
            .where(EventLog.id.in_(batch))
            .execution_options(synchronize_session=False)
        ).rowcount
    if removed_verbose or removed_tokens:
        session.commit()
    return {
        "verbose_events": removed_verbose,
        "token_snapshots": removed_tokens,
    }
