import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from mac_system.config import get_agent_config, get_settings
from mac_system.database import session_scope
from mac_system.models import Approval, EventLog, Project, ProjectUser, Task
from mac_system.state_machine import StateMachine


def generate_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)
    return f"{prefix}_{ts}_{rand}"


def _now() -> datetime:
    return datetime.utcnow()


class InvalidStateTransitionError(Exception):
    def __init__(self, current_state: str, event: str):
        self.current_state = current_state
        self.event = event
        super().__init__(f"Invalid transition from '{current_state}' on event '{event}'")


class ProjectNotFoundError(Exception):
    pass


class ApprovalNotFoundError(Exception):
    pass


class ProjectService:
    def __init__(self, session: Session):
        self.session = session
        self.settings = get_settings()

    async def create_project(
        self,
        name: str | None,
        task_type: str,
        description: str,
        approval_mode: str = "normal",
        creator_open_id: str | None = None,
    ) -> Project:
        project_id = generate_id("proj")
        workspace_path = self.settings.projects_dir / project_id
        workspace_path.mkdir(parents=True, exist_ok=False)

        # Create subdirectories
        (workspace_path / ".mac").mkdir()
        (workspace_path / "src").mkdir()
        (workspace_path / "tests").mkdir()
        (workspace_path / "worktrees").mkdir()

        # Git init
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=workspace_path, check=True)
        subprocess.run(
            ["git", "config", "user.name", self.settings.git_user_name],
            cwd=workspace_path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", self.settings.git_user_email],
            cwd=workspace_path,
            check=True,
        )

        approval_config = self._load_approval_mode_config(approval_mode)

        project = Project(
            id=project_id,
            name=name,
            task_type=task_type,
            description=description,
            status="active",
            current_state="INIT",
            previous_state=None,
            workspace_path=str(workspace_path),
            context={
                "task_type": task_type,
                "description": description,
                "approval_mode": approval_mode,
                "retry_counts": {},
            },
            approval_config=approval_config,
        )
        self.session.add(project)
        self.session.flush()

        if creator_open_id:
            self.session.add(
                ProjectUser(project_id=project_id, open_id=creator_open_id)
            )

        # Write initial STATE.json
        self._write_state_json(project)

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message="Project created",
            meta={"task_type": task_type, "approval_mode": approval_mode},
        )

        # Trigger INIT -> initial planning state
        await self.transition_state(project_id, "start", triggered_by="system")

        self.session.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        return self.session.get(Project, project_id)

    def require_project(self, project_id: str) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    def get_project_status(self, project_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        state_machine = StateMachine(project.task_type)

        recent_events = list(
            self.session.exec(
                select(EventLog)
                .where(EventLog.project_id == project_id)
                .order_by(EventLog.created_at.desc())
                .limit(5)
            ).all()
        )

        pending_approval = self.session.exec(
            select(Approval)
            .where(
                Approval.project_id == project_id,
                Approval.status == "pending",
            )
            .order_by(Approval.created_at.desc())
        ).first()

        return {
            "project_id": project_id,
            "current_state": project.current_state,
            "display": state_machine.display_name(project.current_state),
            "status": project.status,
            "pending_approval": {
                "approval_id": pending_approval.id,
                "type": pending_approval.approval_type,
                "question": pending_approval.question,
                "options": pending_approval.payload.get("options", []),
            }
            if pending_approval
            else None,
            "recent_events": [
                {
                    "level": e.level,
                    "message": e.message,
                    "created_at": e.created_at.isoformat(),
                }
                for e in recent_events
            ],
        }

    async def transition_state(
        self,
        project_id: str,
        event: str,
        payload: dict | None = None,
        triggered_by: str = "system",
    ) -> Project:
        project = self.require_project(project_id)
        state_machine = StateMachine(project.task_type)

        next_state = state_machine.next_state(project.current_state, event)
        if next_state is None:
            raise InvalidStateTransitionError(project.current_state, event)

        previous_state = project.current_state
        project.previous_state = previous_state
        project.current_state = next_state
        project.updated_at = _now()

        # Update context if payload provided rollback/resolved decisions
        if payload:
            if "resolved_decisions" in payload:
                project.context.setdefault("resolved_decisions", []).extend(
                    payload["resolved_decisions"]
                )
            if "clarification_answer" in payload:
                project.context["clarification_answer"] = payload["clarification_answer"]
            if "conflict_resolution" in payload:
                project.context["conflict_resolution"] = payload["conflict_resolution"]

        # Reset retry counts when leaving TESTING/REVIEW successfully
        if previous_state in ("TESTING", "REVIEW") and next_state not in (
            "CODING",
            "FAILED",
        ):
            project.context.setdefault("retry_counts", {})[previous_state] = 0

        self._write_state_json(project)

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"State transition: {previous_state} -> {next_state}",
            meta={
                "from_state": previous_state,
                "to_state": next_state,
                "event": event,
                "triggered_by": triggered_by,
            },
        )

        self.session.commit()
        return project

    async def create_task(
        self,
        project_id: str,
        agent_type: str,
        state: str,
        input_data: dict,
    ) -> Task:
        project = self.require_project(project_id)
        task_id = generate_id("task")
        task = Task(
            id=task_id,
            project_id=project_id,
            agent_type=agent_type,
            state=state,
            status="pending",
            input_data=input_data,
        )
        self.session.add(task)
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Task created: {task_id} for {agent_type} at {state}",
            meta={"task_id": task_id, "agent_type": agent_type, "state": state},
        )
        self.session.commit()
        return task

    async def start_task(self, task_id: str, worktree_path: str | None = None) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task.status = "running"
        if worktree_path:
            task.worktree_path = worktree_path
        task.updated_at = _now()
        self.session.commit()
        return task

    async def complete_task(
        self,
        task_id: str,
        output_data: dict,
        cost_info: dict | None = None,
    ) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task.status = "completed"
        task.output_data = output_data
        if cost_info:
            task.cost_usd = cost_info.get("estimated_cost_usd")
        task.updated_at = _now()
        self.session.commit()
        return task

    async def fail_task(self, task_id: str, error_message: str) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task.status = "failed"
        task.error_message = error_message
        task.retry_count += 1
        task.updated_at = _now()
        self.session.commit()
        return task

    async def create_approval(
        self,
        project_id: str,
        task_id: str | None,
        state: str,
        approval_type: str,
        question: str,
        options: list[dict],
        payload: dict | None = None,
    ) -> Approval:
        approval_id = generate_id("appr")
        approval_payload = dict(payload or {})
        approval_payload["options"] = options

        approval = Approval(
            id=approval_id,
            project_id=project_id,
            task_id=task_id,
            state=state,
            approval_type=approval_type,
            question=question,
            payload=approval_payload,
            status="pending",
        )
        self.session.add(approval)
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Approval created: {approval_id} for {state}",
            meta={"approval_id": approval_id, "state": state, "type": approval_type},
        )
        self.session.commit()
        return approval

    async def respond_approval(
        self,
        approval_id: str,
        response: str,
        responded_by: str,
    ) -> Approval:
        approval = self.session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalNotFoundError(approval_id)
        if approval.status != "pending":
            raise ValueError(f"Approval already responded: {approval_id}")
        approval.response = response
        approval.responded_by = responded_by
        approval.status = "responded"
        approval.responded_at = _now()
        self.session.commit()
        return approval

    async def log_event(
        self,
        project_id: str,
        level: str,
        message: str,
        meta: dict | None = None,
    ) -> None:
        event = EventLog(
            project_id=project_id,
            level=level,
            message=message,
            meta=meta or {},
        )
        self.session.add(event)

    def _write_state_json(self, project: Project) -> None:
        workspace = Path(project.workspace_path)
        state_path = workspace / ".mac" / "STATE.json"
        pending = self._build_pending_approval(project)

        state_doc = {
            "version": "1.0",
            "project_id": project.id,
            "current_state": project.current_state,
            "previous_state": project.previous_state,
            "pending_approval": pending,
            "context": project.context,
            "history": project.context.get("history", []),
        }

        # Append current state to history
        history = state_doc["history"]
        history.append({"state": project.current_state, "entered_at": _now().isoformat()})
        project.context["history"] = history

        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state_doc, f, ensure_ascii=False, indent=2)

    def _build_pending_approval(self, project: Project) -> dict | None:
        approval = self.session.exec(
            select(Approval)
            .where(
                Approval.project_id == project.id,
                Approval.status == "pending",
            )
            .order_by(Approval.created_at.desc())
        ).first()
        if approval is None:
            return None
        return {
            "approval_id": approval.id,
            "type": approval.approval_type,
            "state": approval.state,
            "question": approval.question,
            "options": approval.payload.get("options", []),
            "payload": approval.payload,
        }

    def _load_approval_mode_config(self, mode: str) -> dict:
        agent_config = get_agent_config()
        modes_path_str = agent_config.get("approval_modes_path", "specs/approval_modes.json")
        modes_path = Path(modes_path_str)
        if not modes_path.exists():
            return {"mode": mode}
        with open(modes_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("modes", {}).get(mode, {"mode": mode})
