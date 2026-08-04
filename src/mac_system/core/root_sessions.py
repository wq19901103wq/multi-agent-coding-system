"""Persistence boundary for the native root thread and MAC extensions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from sqlmodel import Session, select

from mac_system.agents.root_runtime import AgentEvent, ToolContext
from mac_system.core.project_service import generate_id
from mac_system.models import (
    AgentSession,
    Approval,
    RootAgentSession,
    DynamicToolCall,
    EventLog,
    Project,
    QuestionInteraction,
)


def _now() -> datetime:
    return datetime.utcnow()


SHUTDOWN_INTERRUPTED_ERROR = (
    "MAC service shutdown interrupted the native Codex turn"
)


class RootSessionStore:
    """Small transactional API; it does not decide project workflow."""

    def __init__(self, session: Session):
        self.session = session

    def ensure(self, project_id: str, provider: str = "codex") -> RootAgentSession:
        existing = self.session.exec(
            select(RootAgentSession).where(
                RootAgentSession.project_id == project_id
            )
        ).first()
        if existing is not None:
            return existing
        row = RootAgentSession(
            id=generate_id("coord"), project_id=project_id, provider=provider
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_native_thread(self, project_id: str, native_thread_id: str) -> RootAgentSession:
        row = self.ensure(project_id)
        row.native_thread_id = native_thread_id
        row.status = "idle"
        row.last_error = None
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def set_turn(
        self, project_id: str, *, turn_id: str | None, status: str, error: str | None = None
    ) -> RootAgentSession:
        row = self.ensure(project_id)
        row.active_turn_id = turn_id
        row.status = status
        row.last_error = error
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

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
    """Normalize native turns whose app-server process no longer exists.

    Codex thread ids remain resumable, but an in-flight turn and its server
    requests belong to the app-server process that owned them. On MAC process
    startup there cannot be a live owner for rows still marked running or
    waiting, so the durable projection must no longer advertise either state.
    """

    interrupted = [
        row
        for row in session.exec(select(RootAgentSession)).all()
        if row.status in {"running", "waiting_user"}
        or row.last_error == SHUTDOWN_INTERRUPTED_ERROR
    ]
    if not interrupted:
        return []

    project_ids = {row.project_id for row in interrupted}
    for row in interrupted:
        row.status = "idle"
        row.active_turn_id = None
        row.last_error = "Previous native Codex turn was interrupted by service restart"
        row.updated_at = _now()
        session.add(row)

    for project in session.exec(
        select(Project).where(Project.id.in_(project_ids))
    ).all():
        project.execution_status = "idle"
        project.waiting_reason = None
        project.waiting_since = None
        project.updated_at = _now()
        session.add(project)

    for approval in session.exec(
        select(Approval).where(
            Approval.project_id.in_(project_ids),
            Approval.status == "pending",
            Approval.state == "ROOT_AGENT_WAITING_USER",
        )
    ).all():
        approval.status = "expired"
        approval.response = "native_turn_interrupted"
        approval.responded_by = "system"
        approval.responded_at = _now()
        session.add(approval)

    for interaction in session.exec(
        select(QuestionInteraction).where(
            QuestionInteraction.project_id.in_(project_ids),
            QuestionInteraction.status == "pending",
            QuestionInteraction.agent == "root_agent",
        )
    ).all():
        interaction.status = "expired"
        interaction.resolution_source = "native_turn_interrupted"
        interaction.answered_at = _now()
        session.add(interaction)

    session.commit()
    return sorted(project_ids)
