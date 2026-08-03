"""Persistence boundary for the Coordinator's native session and host tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from sqlmodel import Session, select

from mac_system.agents.coordinator_runtime import CoordinatorEvent, CoordinatorToolContext
from mac_system.core.project_service import generate_id
from mac_system.models import (
    CoordinatorAgentSession,
    CoordinatorSession,
    CoordinatorToolCall,
    EventLog,
)


def _now() -> datetime:
    return datetime.utcnow()


class CoordinatorSessionStore:
    """Small transactional API; it does not decide project workflow."""

    def __init__(self, session: Session):
        self.session = session

    def ensure(self, project_id: str, provider: str = "codex") -> CoordinatorSession:
        existing = self.session.exec(
            select(CoordinatorSession).where(
                CoordinatorSession.project_id == project_id
            )
        ).first()
        if existing is not None:
            return existing
        row = CoordinatorSession(
            id=generate_id("coord"), project_id=project_id, provider=provider
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_native_thread(self, project_id: str, native_thread_id: str) -> CoordinatorSession:
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
    ) -> CoordinatorSession:
        row = self.ensure(project_id)
        row.active_turn_id = turn_id
        row.status = status
        row.last_error = error
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def append_event(self, project_id: str, event: CoordinatorEvent) -> int:
        row = self.ensure(project_id)
        row.event_cursor += 1
        row.updated_at = _now()
        self.session.add(row)
        self.session.add(
            EventLog(
                project_id=project_id,
                level="INFO",
                message=f"Coordinator event: {event.kind}",
                meta={
                    "profile_type": "coordinator_event",
                    "coordinator_session_id": row.id,
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
        context: CoordinatorToolContext,
        arguments: dict[str, Any],
        executor: Callable[[str], Any],
    ) -> Any:
        """Persist before side effects and replay a completed result by call id."""
        coordinator = self.ensure(project_id)
        existing = self.session.exec(
            select(CoordinatorToolCall).where(
                CoordinatorToolCall.native_thread_id == context.thread_id,
                CoordinatorToolCall.native_turn_id == context.turn_id,
                CoordinatorToolCall.native_call_id == context.call_id,
            )
        ).first()
        if existing is not None and existing.status == "completed":
            return existing.result.get("value")
        if existing is None:
            existing = CoordinatorToolCall(
                id=generate_id("ctool"),
                project_id=project_id,
                coordinator_session_id=coordinator.id,
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

    def get_agent(self, project_id: str, task_name: str) -> CoordinatorAgentSession | None:
        return self.session.exec(
            select(CoordinatorAgentSession).where(
                CoordinatorAgentSession.project_id == project_id,
                CoordinatorAgentSession.task_name == task_name,
            )
        ).first()

    def list_agents(self, project_id: str) -> list[CoordinatorAgentSession]:
        return list(
            self.session.exec(
                select(CoordinatorAgentSession)
                .where(CoordinatorAgentSession.project_id == project_id)
                .order_by(CoordinatorAgentSession.created_at)
            ).all()
        )

    def create_agent(
        self,
        project_id: str,
        *,
        task_name: str,
        provider: str,
        access: str,
        objective: str,
        tool_call_id: str,
    ) -> CoordinatorAgentSession:
        existing = self.get_agent(project_id, task_name)
        if existing is not None:
            return existing
        coordinator = self.ensure(project_id)
        row = CoordinatorAgentSession(
            id=generate_id("agent"),
            project_id=project_id,
            coordinator_session_id=coordinator.id,
            task_name=task_name,
            provider=provider,
            access=access,
            objective=objective,
            status="pending",
            result={"spawn_tool_call_id": tool_call_id},
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row
