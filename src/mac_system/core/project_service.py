import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from mac_system.config import get_agent_config, get_settings
from mac_system.database import session_scope
from mac_system.models import (
    Approval,
    EventLog,
    PendingIntent,
    ProcessedEvent,
    Project,
    ProjectUser,
    Task,
)
from mac_system.state_machine import StateMachine
from mac_system.core.worktree_manager import WorktreeManager


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
        feishu_chat_id: str | None = None,
        feishu_chat_type: str = "p2p",
    ) -> Project:
        project_id = generate_id("proj")
        workspace_path = (self.settings.projects_dir / project_id).resolve()
        workspace_path.mkdir(parents=True, exist_ok=False)

        # Create subdirectories
        (workspace_path / ".mac").mkdir()
        (workspace_path / "src").mkdir()
        (workspace_path / "tests").mkdir()
        (workspace_path / "worktrees").mkdir()

        # Ignore generated directories so they are not committed as embedded repos
        (workspace_path / ".gitignore").write_text(
            "worktrees/\n.mac/\n__pycache__/\n*.pyc\n*.pyo\n*.egg-info/\n.coverage\n",
            encoding="utf-8",
        )

        # Minimal Python project config so pytest can discover src/ packages
        (workspace_path / "pyproject.toml").write_text(
            '[build-system]\n'
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            '[project]\n'
            'name = "generated-project"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.10"\n\n'
            '[tool.pytest.ini_options]\n'
            'pythonpath = ["src"]\n'
            'testpaths = ["tests"]\n',
            encoding="utf-8",
        )

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
        # Stage project skeleton and create an initial commit so worktrees
        # have a valid HEAD to branch from and merge back into.
        subprocess.run(
            ["git", "add", ".gitignore", "pyproject.toml", "src", "tests"],
            cwd=workspace_path,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "Initial commit"],
            cwd=workspace_path,
            check=True,
        )

        approval_config = self._resolve_approval_config(approval_mode)

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
            feishu_chat_id=feishu_chat_id,
            feishu_chat_type=feishu_chat_type,
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

    async def restart_project_with_type(
        self,
        project_id: str,
        target_task_type: str,
        triggered_by: str = "user",
    ) -> Project:
        """Cancel the original project and create a new one with a different task_type.

        This is used when the user discovers at acceptance time that the project
        was classified into the wrong pipeline (e.g. a bug_fix is actually a
        feature). The new project keeps the same description and creator binding.
        """
        project = self.require_project(project_id)
        if project.status == "cancelled":
            raise ValueError(f"Project {project_id} is already cancelled")

        # Cancel the original project.
        project.status = "cancelled"
        project.updated_at = _now()

        # Remove any lingering worktrees for the old project.
        try:
            WorktreeManager(self.settings.projects_dir).remove_all_worktrees(project_id)
        except Exception:
            pass

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Project cancelled for restart as {target_task_type}",
            meta={"target_task_type": target_task_type},
        )

        # Preserve creator binding if any.
        creator_open_id = None
        existing_binding = self.session.exec(
            select(ProjectUser).where(ProjectUser.project_id == project_id)
        ).first()
        if existing_binding:
            creator_open_id = existing_binding.open_id

        new_project = await self.create_project(
            name=project.name,
            task_type=target_task_type,
            description=project.description or "",
            approval_mode=project.context.get("approval_mode", "normal"),
            creator_open_id=creator_open_id,
            feishu_chat_id=project.feishu_chat_id,
            feishu_chat_type=project.feishu_chat_type,
        )

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Restarted project as {new_project.id} ({target_task_type})",
            meta={"new_project_id": new_project.id, "target_task_type": target_task_type},
        )
        return new_project

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
            "total_cost_usd": project.total_cost_usd,
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

        project = self.session.get(Project, task.project_id)
        if project is not None and task.cost_usd is not None:
            project.total_cost_usd = (project.total_cost_usd or 0.0) + task.cost_usd

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

    def recover_stale_tasks(self) -> list[Task]:
        """Mark all 'running' tasks as failed, typically on service startup."""
        tasks = self.session.exec(select(Task).where(Task.status == "running")).all()
        for task in tasks:
            task.status = "failed"
            task.error_message = "service restarted while task was running"
            task.updated_at = _now()
        if tasks:
            self.session.commit()
        return list(tasks)

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

    def get_approval(self, approval_id: str) -> Approval:
        approval = self.session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalNotFoundError(approval_id)
        return approval

    async def respond_approval(
        self,
        approval_id: str,
        response: str,
        responded_by: str,
    ) -> Approval:
        approval = self.get_approval(approval_id)
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

    def get_pending_intent(self, open_id: str) -> PendingIntent | None:
        """Return a non-expired pending intent for the user, or None."""
        from datetime import timedelta

        intent = self.session.get(PendingIntent, open_id)
        if intent is None:
            return None
        if intent.created_at < _now() - timedelta(hours=1):
            self.session.delete(intent)
            self.session.commit()
            return None
        return intent

    def upsert_pending_intent(
        self,
        open_id: str,
        description: str | None,
        stage: str,
        chat_id: str | None = None,
        chat_type: str = "p2p",
    ) -> PendingIntent:
        """Create or replace the pending intent for the user."""
        intent = self.session.get(PendingIntent, open_id)
        if intent is None:
            intent = PendingIntent(open_id=open_id)
            self.session.add(intent)
        intent.description = description
        intent.stage = stage
        intent.chat_id = chat_id
        intent.chat_type = chat_type
        intent.created_at = _now()
        self.session.commit()
        return intent

    def delete_pending_intent(self, open_id: str) -> None:
        """Remove the pending intent for the user."""
        intent = self.session.get(PendingIntent, open_id)
        if intent is not None:
            self.session.delete(intent)
            self.session.commit()

    def _load_approval_mode_config(self, mode: str) -> dict:
        agent_config = get_agent_config()
        modes_path_str = agent_config.get("approval_modes_path", "specs/approval_modes.json")
        modes_path = Path(modes_path_str)
        if not modes_path.exists():
            return {"mode": mode}
        with open(modes_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("modes", {}).get(mode, {"mode": mode})

    def _resolve_approval_config(self, mode: str) -> dict:
        """Return the effective approval config, resolving night-mode window."""
        config = self._load_approval_mode_config(mode)
        if mode == "night":
            window = config.get("night_window")
            if window and not self._in_night_window(window):
                # Outside the night window, new night-mode projects behave like normal.
                # In test mode, skip the window check so E2E tests can exercise
                # night-mode behaviour regardless of the time of day.
                if not self.settings.feishu_test_mode:
                    config = self._load_approval_mode_config("normal")
        return config

    @staticmethod
    def _in_night_window(window: dict, now: datetime | None = None) -> bool:
        """Return True if ``now`` (or current time) is inside the configured night window."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        tz_name = window.get("timezone", "Asia/Shanghai")
        tz = ZoneInfo(tz_name)
        current = now if now is not None else _dt.now(tz)
        if isinstance(current, datetime) and current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        current_time = current.time()
        start = _dt.strptime(window["start_time"], "%H:%M").time()
        end = _dt.strptime(window["end_time"], "%H:%M").time()
        if start < end:
            return start <= current_time < end
        return current_time >= start or current_time < end

    def is_event_processed(self, event_id: str) -> bool:
        """Return True if a Feishu event has already been processed."""
        if not event_id:
            return False
        return self.session.get(ProcessedEvent, event_id) is not None

    def mark_event_processed(self, event_id: str, event_type: str) -> None:
        """Record a Feishu event as processed so duplicates can be dropped."""
        if not event_id:
            return
        self.session.merge(
            ProcessedEvent(event_id=event_id, event_type=event_type, created_at=_now())
        )
