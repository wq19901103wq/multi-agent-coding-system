import json
import secrets
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select, update

from mac_system.config import get_agent_config, get_settings
from mac_system.database import session_scope
from mac_system.models import (
    Approval,
    ChatDirectoryBinding,
    ChatUserContext,
    ConversationInteraction,
    DefaultAuthorization,
    EventLog,
    FeedbackPacket,
    PendingIntent,
    PendingUserInput,
    ProcessedEvent,
    Project,
    ProjectChat,
    ProjectUser,
    Question,
    QuestionAnswer,
    QuestionInteraction,
    Task,
)
from mac_system.state_machine import StateMachine
from mac_system.core.project_paths import project_mac_dir
from mac_system.core.worktree_manager import WorktreeManager


def generate_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)
    return f"{prefix}_{ts}_{rand}"


def _now() -> datetime:
    return datetime.utcnow()


def _walk_dirs(root: Path, *, max_depth: int = 2):
    """Yield subdirectories of ``root`` up to ``max_depth`` levels deep.

    ``root`` itself is yielded first (depth 0). Symlinks are skipped to avoid
    cycles. Used only by ``scan_candidate_repositories`` to enforce that the
    bot never scans outside ``MAC_ALLOWED_BIND_ROOTS``.
    """
    root = root.resolve()
    yield root
    if max_depth <= 0:
        return
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if not entry.is_dir():
                continue
            # Skip hidden directories like .git, .venv, node_modules.
            if entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__"}:
                continue
            yield entry
            queue.append((entry, depth + 1))


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
        bound_directory: str | None = None,
    ) -> Project:
        project_id = generate_id("proj")
        if bound_directory is None and feishu_chat_id:
            binding = self.get_chat_directory_binding(feishu_chat_id)
            bound_directory = binding.directory if binding else None

        if bound_directory:
            workspace_path = self._prepare_bound_directory(bound_directory)
            (workspace_path / ".mac" / "projects" / project_id).mkdir(
                parents=True, exist_ok=True
            )
            (workspace_path / ".mac" / "worktrees" / project_id).mkdir(
                parents=True, exist_ok=True
            )
            exclude_path = workspace_path / ".git" / "info" / "exclude"
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            exclude_text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
            if ".mac/worktrees/" not in exclude_text.splitlines():
                exclude_path.write_text(
                    exclude_text.rstrip() + "\n.mac/worktrees/\n",
                    encoding="utf-8",
                )
        else:
            workspace_path = (self.settings.projects_dir / project_id).resolve()
            workspace_path.mkdir(parents=True, exist_ok=False)
            (workspace_path / ".mac").mkdir()
            (workspace_path / "src").mkdir()
            (workspace_path / "tests").mkdir()
            (workspace_path / "worktrees").mkdir()
            (workspace_path / ".gitignore").write_text(
                "worktrees/\n.mac/\n__pycache__/\n*.pyc\n*.pyo\n*.egg-info/\n.coverage\n",
                encoding="utf-8",
            )
            (workspace_path / "pyproject.toml").write_text(
                '[build-system]\nrequires = ["hatchling"]\n'
                'build-backend = "hatchling.build"\n\n'
                '[project]\nname = "generated-project"\nversion = "0.1.0"\n'
                'requires-python = ">=3.10"\n\n'
                '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
                'testpaths = ["tests"]\n',
                encoding="utf-8",
            )
            self._initialize_repository(workspace_path, initial_paths=[
                ".gitignore", "pyproject.toml", "src", "tests"
            ])

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
            bound_directory=str(workspace_path) if bound_directory else None,
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
                ProjectUser(
                    project_id=project_id,
                    open_id=creator_open_id,
                    role="owner",
                )
            )

        if feishu_chat_id:
            self.session.add(
                ProjectChat(
                    id=generate_id("pch"),
                    project_id=project_id,
                    chat_id=feishu_chat_id,
                    chat_type=feishu_chat_type,
                    relation="origin",
                    notification_level="all",
                    created_by=creator_open_id,
                )
            )
            if creator_open_id:
                now = _now()
                existing = self.session.get(
                    ChatUserContext, (feishu_chat_id, creator_open_id)
                )
                if existing:
                    existing.active_project_id = project_id
                    existing.selected_at = now
                    existing.updated_at = now
                else:
                    self.session.add(
                        ChatUserContext(
                            chat_id=feishu_chat_id,
                            open_id=creator_open_id,
                            active_project_id=project_id,
                            selected_at=now,
                            updated_at=now,
                        )
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

    def _initialize_repository(
        self, path: Path, initial_paths: list[str] | None = None
    ) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(
            ["git", "config", "user.name", self.settings.git_user_name],
            cwd=path, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", self.settings.git_user_email],
            cwd=path, check=True,
        )
        subprocess.run(
            ["git", "add", *(initial_paths or ["-A"])], cwd=path, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "Initial commit"],
            cwd=path, check=True,
        )

    def _prepare_bound_directory(self, directory: str) -> Path:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise ValueError("绑定目录必须是绝对路径")
        path = path.resolve()
        if path.exists() and not path.is_dir():
            raise ValueError(f"绑定路径不是目录: {path}")
        path.mkdir(parents=True, exist_ok=True)

        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path, capture_output=True, text=True,
        )
        if probe.returncode == 0:
            repository_root = Path(probe.stdout.strip()).resolve()
            if repository_root != path:
                raise ValueError(
                    f"绑定目录必须是 Git 仓库根目录；当前仓库根目录为 {repository_root}"
                )
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"], cwd=path,
                capture_output=True,
            )
            if head.returncode != 0:
                self._initialize_repository(path)
        else:
            self._initialize_repository(path)
        return path

    def bind_directory_to_chat(
        self, chat_id: str, directory: str, bound_by: str | None = None
    ) -> ChatDirectoryBinding:
        path = self._prepare_bound_directory(directory)
        binding = self.session.get(ChatDirectoryBinding, chat_id)
        now = _now()
        if binding is None:
            binding = ChatDirectoryBinding(
                chat_id=chat_id,
                directory=str(path),
                bound_by=bound_by,
                created_at=now,
                updated_at=now,
            )
            self.session.add(binding)
        else:
            binding.directory = str(path)
            binding.bound_by = bound_by
            binding.updated_at = now
        self.session.commit()
        return binding

    def get_chat_directory_binding(
        self, chat_id: str
    ) -> ChatDirectoryBinding | None:
        return self.session.get(ChatDirectoryBinding, chat_id)

    def unbind_directory_from_chat(self, chat_id: str) -> bool:
        binding = self.session.get(ChatDirectoryBinding, chat_id)
        if binding is None:
            return False
        self.session.delete(binding)
        self.session.commit()
        return True

    @staticmethod
    def scan_candidate_repositories(
        allowed_roots: list[Path], *, max_depth: int = 2, limit: int = 12
    ) -> list[dict[str, str]]:
        """Walk each allowed root up to ``max_depth`` levels and return git repos.

        Returns a list of ``{"path": <abs path>, "name": <dir name>}`` dicts,
        deduplicated and truncated to ``limit``. The bot is only allowed to
        scan inside ``allowed_roots`` - never anywhere else on the host.
        """
        seen: set[Path] = set()
        out: list[dict[str, str]] = []
        for root in allowed_roots:
            if not root.exists() or not root.is_dir():
                continue
            for entry in _walk_dirs(root, max_depth=max_depth):
                if entry in seen:
                    continue
                if not (entry / ".git").exists():
                    continue
                seen.add(entry)
                out.append({"path": str(entry), "name": entry.name})
                if len(out) >= limit:
                    return out
        return out

    async def cancel_project(
        self,
        project_id: str,
        reason: str = "",
        triggered_by: str = "user",
    ) -> Project:
        """Cancel a project and clean up its running tasks, approvals and worktrees.

        Idempotent: calling it again on an already cancelled project is a no-op.
        """
        project = self.require_project(project_id)
        if project.status == "cancelled":
            return project

        # Mark running tasks as failed so they are not recovered later.
        for task in self._running_tasks(project_id):
            task.status = "failed"
            task.error_message = reason or "Project cancelled"

        # Expire pending approvals.
        for approval in self._pending_approvals(project_id):
            approval.status = "expired"

        project.status = "cancelled"
        project.updated_at = _now()

        # Remove any lingering worktrees for the project.
        try:
            WorktreeManager(self.settings.projects_dir).remove_all_worktrees(
                project_id, workspace_path=project.workspace_path
            )
        except Exception:
            pass

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Project cancelled",
            meta={"reason": reason, "triggered_by": triggered_by},
        )
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
        await self.cancel_project(
            project_id,
            reason=f"Restarted as {target_task_type}",
            triggered_by=triggered_by,
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
            bound_directory=project.bound_directory,
        )

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Restarted project as {new_project.id} ({target_task_type})",
            meta={"new_project_id": new_project.id, "target_task_type": target_task_type},
        )
        return new_project

    def get_project(self, project_id: str) -> Project | None:
        project = self.session.get(Project, project_id)
        if project is not None:
            self._migrate_legacy_state_names(project)
        return project

    def _migrate_legacy_state_names(self, project: Project) -> None:
        """Lazily migrate old state names in the DB to their new names.

        ``PLANNING_SPECS`` -> ``PLANNING_IMPLEMENTATION_SPECS`` etc.  Done
        on read so existing projects keep working without a batch migration
        script.  Also migrates ``previous_state``.
        """
        from mac_system.state_machine import _STATE_ALIASES
        changed = False
        if project.current_state in _STATE_ALIASES:
            project.current_state = _STATE_ALIASES[project.current_state]
            changed = True
        if project.previous_state and project.previous_state in _STATE_ALIASES:
            project.previous_state = _STATE_ALIASES[project.previous_state]
            changed = True
        if changed:
            self.session.commit()

    def require_project(self, project_id: str) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    # ------------------------------------------------------------------
    # Artifact version management
    # ------------------------------------------------------------------

    DOWNSTREAM_ARTIFACTS: dict[str, tuple[str, ...]] = {
        "requirements": ("architecture", "specs"),
        "architecture": ("specs",),
        "specs": (),
    }

    def get_artifact_version(
        self, project_id: str, artifact_id: str
    ) -> dict[str, Any] | None:
        project = self.require_project(project_id)
        value = project.context.get("artifact_versions", {}).get(artifact_id)
        return dict(value) if value else None

    def update_artifact_version(
        self,
        project_id: str,
        artifact_id: str,
        version: int,
        round_number: int | None,
        status: str,
        *,
        commit: bool = True,
    ) -> None:
        project = self.require_project(project_id)
        versions = dict(project.context.get("artifact_versions", {}))
        versions[artifact_id] = {
            "version": version,
            "round": round_number,
            "status": status,
        }
        project.context = {**project.context, "artifact_versions": versions}
        if commit:
            self.session.commit()

    def mark_downstream_artifacts_stale(
        self, project_id: str, changed_artifact_id: str, *, commit: bool = True
    ) -> None:
        project = self.require_project(project_id)
        versions = dict(project.context.get("artifact_versions", {}))
        changed = False
        for artifact_id in self.DOWNSTREAM_ARTIFACTS.get(changed_artifact_id, ()):
            current = versions.get(artifact_id)
            if current and current.get("status") != "stale":
                versions[artifact_id] = {**current, "status": "stale"}
                changed = True
        if changed:
            project.context = {**project.context, "artifact_versions": versions}
            if commit:
                self.session.commit()

    # Backward-compatible name used by the original main-branch feature.
    mark_downstream_stale = mark_downstream_artifacts_stale

    def get_stale_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        project = self.require_project(project_id)
        return [
            {"artifact_id": artifact_id, **info}
            for artifact_id, info in project.context.get("artifact_versions", {}).items()
            if info.get("status") == "stale"
        ]

    # ------------------------------------------------------------------
    # Chat / user context routing
    # ------------------------------------------------------------------

    def bind_project_to_chat(
        self,
        project_id: str,
        chat_id: str,
        chat_type: str = "p2p",
        relation: str = "subscribed",
        notification_level: str = "all",
        created_by: str | None = None,
    ) -> ProjectChat:
        """Bind a project to a Feishu chat if not already bound."""
        existing = self.session.exec(
            select(ProjectChat).where(
                ProjectChat.project_id == project_id,
                ProjectChat.chat_id == chat_id,
            )
        ).first()
        if existing:
            return existing
        chat = ProjectChat(
            id=generate_id("pch"),
            project_id=project_id,
            chat_id=chat_id,
            chat_type=chat_type,
            relation=relation,
            notification_level=notification_level,
            created_by=created_by,
        )
        self.session.add(chat)
        self.session.commit()
        return chat

    def set_chat_user_context(
        self,
        chat_id: str,
        open_id: str,
        project_id: str | None,
    ) -> ChatUserContext:
        """Set the active project for a user within a specific chat."""
        ctx = self.session.exec(
            select(ChatUserContext).where(
                ChatUserContext.chat_id == chat_id,
                ChatUserContext.open_id == open_id,
            )
        ).first()
        now = _now()
        if ctx is None:
            ctx = ChatUserContext(
                chat_id=chat_id,
                open_id=open_id,
                active_project_id=project_id,
                selected_at=now,
            )
            self.session.add(ctx)
        else:
            ctx.active_project_id = project_id
            ctx.selected_at = now
            ctx.updated_at = now
        self.session.commit()
        return ctx

    def get_chat_user_active_project(
        self,
        chat_id: str,
        open_id: str,
    ) -> Project | None:
        """Return the active project for a user in a chat, if any."""
        ctx = self.session.exec(
            select(ChatUserContext).where(
                ChatUserContext.chat_id == chat_id,
                ChatUserContext.open_id == open_id,
            )
        ).first()
        if ctx is None:
            return None
        project = self.session.get(Project, ctx.active_project_id)
        if project is None or project.status == "cancelled":
            return None
        return project

    def list_projects_for_chat(self, chat_id: str) -> list[Project]:
        """Return active projects bound to a Feishu chat."""
        chats = self.session.exec(
            select(ProjectChat).where(ProjectChat.chat_id == chat_id)
        ).all()
        project_ids = [c.project_id for c in chats]
        if not project_ids:
            return []
        return list(
            self.session.exec(
                select(Project)
                .where(Project.id.in_(project_ids))
                .where(Project.status != "cancelled")
                .order_by(Project.updated_at.desc())
            ).all()
        )

    # ------------------------------------------------------------------
    # Approval helpers
    # ------------------------------------------------------------------

    async def _expire_pending_approvals(
        self,
        project_id: str,
        commit: bool = True,
    ) -> int:
        """Expire all pending approvals for a project before creating a new one."""
        result = self.session.exec(
            update(Approval)
            .where(
                Approval.project_id == project_id,
                Approval.status == "pending",
            )
            .values(status="expired")
        )
        if commit:
            self.session.commit()
        return result.rowcount

    async def resolve_approval(
        self,
        approval_id: str,
        event: str,
        responded_by: str,
        payload: dict | None = None,
    ) -> tuple[Approval, Project]:
        """Atomically respond to an approval and transition project state.

        Both the approval update and the state transition happen in the same
        database transaction so they cannot diverge.
        """
        approval = self.get_approval(approval_id)
        project = self.require_project(approval.project_id)

        if approval.status != "pending":
            raise ValueError(f"Approval {approval_id} is not pending")
        if approval.state != project.current_state:
            raise ValueError(
                f"Approval state {approval.state} does not match project state {project.current_state}"
            )
        valid_actions = {opt.get("action") for opt in approval.payload.get("options", [])}
        if event not in valid_actions:
            raise ValueError(f"Invalid action {event} for approval {approval_id}")

        approval = await self.respond_approval(
            approval_id, event, responded_by, commit=False
        )
        project = await self.transition_state(
            approval.project_id,
            event,
            payload=payload,
            triggered_by=responded_by,
        )
        return approval, project

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

    def list_projects(
        self,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Project], int]:
        """Return a paginated list of projects and the total count."""
        query = select(Project)
        if status:
            query = query.where(Project.status == status)
        query = query.order_by(Project.updated_at.desc())
        total = len(self.session.exec(query).all())
        projects = list(
            self.session.exec(query.offset(offset).limit(limit)).all()
        )
        return projects, total

    def get_project_tasks(self, project_id: str) -> list[Task]:
        return list(
            self.session.exec(
                select(Task)
                .where(Task.project_id == project_id)
                .order_by(Task.created_at.asc())
            ).all()
        )

    def get_project_approvals(self, project_id: str) -> list[Approval]:
        return list(
            self.session.exec(
                select(Approval)
                .where(Approval.project_id == project_id)
                .order_by(Approval.created_at.asc())
            ).all()
        )

    def get_project_events(self, project_id: str, limit: int | None = None) -> list[EventLog]:
        query = (
            select(EventLog)
            .where(EventLog.project_id == project_id)
            .order_by(EventLog.created_at.desc())
        )
        if limit:
            query = query.limit(limit)
        return list(self.session.exec(query).all())

    def _running_tasks(self, project_id: str) -> list[Task]:
        return list(
            self.session.exec(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status == "running",
                )
            ).all()
        )

    def _pending_approvals(self, project_id: str) -> list[Approval]:
        return list(
            self.session.exec(
                select(Approval).where(
                    Approval.project_id == project_id,
                    Approval.status == "pending",
                )
            ).all()
        )

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
        if next_state == "FAILED":
            project.status = "failed"
        elif next_state == "COMPLETED":
            project.status = "completed"
        elif next_state == "CANCELLED":
            project.status = "cancelled"
        project.updated_at = _now()

        # Update context if payload provided rollback/resolved decisions
        if payload:
            new_context = dict(project.context)
            if "resolved_decisions" in payload:
                resolved = list(new_context.get("resolved_decisions", []))
                resolved.extend(payload["resolved_decisions"])
                new_context["resolved_decisions"] = resolved
            if "clarification_answer" in payload:
                new_context["clarification_answer"] = payload["clarification_answer"]
            if "conflict_resolution" in payload:
                new_context["conflict_resolution"] = payload["conflict_resolution"]
            project.context = new_context

        # Reset retry counts when leaving TESTING/REVIEW successfully
        if previous_state in ("TESTING", "REVIEW") and next_state not in (
            "CODING",
            "FAILED",
        ):
            retry_counts = dict(project.context.get("retry_counts", {}))
            retry_counts[previous_state] = 0
            project.context = {**project.context, "retry_counts": retry_counts}

        if event == "confirm":
            confirmed_artifact = {
                "REQUIREMENT_CONFIRM_PENDING": "requirements",
                "ARCHITECTURE_CONFIRM_PENDING": "architecture",
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "specs",
            }.get(previous_state)
            if confirmed_artifact:
                versions = dict(project.context.get("artifact_versions", {}))
                current = versions.get(confirmed_artifact)
                if current:
                    versions[confirmed_artifact] = {**current, "status": "confirmed"}
                    project.context = {**project.context, "artifact_versions": versions}
            self._resolve_human_rejection_feedback(project_id)

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
        round_number: int = 1,
    ) -> Task:
        project = self.require_project(project_id)
        task_id = generate_id("task")
        task = Task(
            id=task_id,
            project_id=project_id,
            agent_type=agent_type,
            state=state,
            round_number=round_number,
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
        commit: bool = True,
    ) -> Approval:
        # Enforce at most one pending approval per project.
        await self._expire_pending_approvals(project_id, commit=False)

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
        if commit:
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
        commit: bool = True,
    ) -> Approval:
        """Respond to an approval using a compare-and-set update.

        Only the first caller with ``status='pending'`` wins; subsequent calls
        receive the already-responded approval for idempotency.
        """
        result = self.session.exec(
            update(Approval)
            .where(Approval.id == approval_id, Approval.status == "pending")
            .values(
                response=response,
                responded_by=responded_by,
                status="responded",
                responded_at=_now(),
            )
        )
        if result.rowcount == 0:
            approval = self.get_approval(approval_id)
            if approval is not None and approval.status != "pending":
                return approval
            raise ApprovalNotFoundError(approval_id)
        if commit:
            self.session.commit()
        return self.get_approval(approval_id)

    # ------------------------------------------------------------------
    # Feedback packets
    # ------------------------------------------------------------------

    async def create_feedback_packet(
        self,
        project_id: str,
        source_stage: str,
        source_task_id: str | None,
        source_round: int,
        kind: str,
        summary: str,
        blocking: bool = True,
        failure_class: str | None = None,
        recommended_rework_target: str | None = None,
        evidence: list[dict] | None = None,
        user_comment: str | None = None,
        status: str = "open",
        commit: bool = True,
    ) -> FeedbackPacket:
        packet = FeedbackPacket(
            id=generate_id("fb"),
            project_id=project_id,
            source_stage=source_stage,
            source_task_id=source_task_id,
            source_round=source_round,
            kind=kind,
            summary=summary,
            blocking=blocking,
            failure_class=failure_class,
            recommended_rework_target=recommended_rework_target,
            evidence=evidence or [],
            user_comment=user_comment,
            status=status,
        )
        self.session.add(packet)
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Feedback packet created: {packet.id} ({kind})",
            meta={
                "feedback_id": packet.id,
                "source_stage": source_stage,
                "source_task_id": source_task_id,
                "recommended_rework_target": recommended_rework_target,
            },
        )
        if commit:
            self.session.commit()
        return packet

    def _resolve_human_rejection_feedback(self, project_id: str) -> None:
        """Close rejection feedback after the revised artifact is confirmed."""
        packets = self.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project_id,
                FeedbackPacket.kind == "human_rejection",
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
        ).all()
        for packet in packets:
            packet.status = "resolved"
            packet.updated_at = _now()

    def get_open_feedback_packets(self, project_id: str) -> list[FeedbackPacket]:
        return list(
            self.session.exec(
                select(FeedbackPacket)
                .where(
                    FeedbackPacket.project_id == project_id,
                    FeedbackPacket.status.in_(["open", "disputed"]),
                )
                .order_by(FeedbackPacket.created_at.asc())
            ).all()
        )

    async def resolve_feedback_packet(
        self,
        feedback_id: str,
        resolved_by_task_id: str,
        status: str = "resolved",
    ) -> FeedbackPacket:
        packet = self.session.get(FeedbackPacket, feedback_id)
        if packet is None:
            raise ValueError(f"Feedback packet not found: {feedback_id}")
        packet.status = status
        packet.resolved_by_task_id = resolved_by_task_id
        packet.updated_at = _now()
        self.session.commit()
        return packet

    # ------------------------------------------------------------------
    # Pending user inputs (free text received while an agent is running)
    # ------------------------------------------------------------------

    async def create_pending_user_input(
        self,
        project_id: str,
        sender_id: str | None,
        observed_state: str,
        content: str,
        kind: str = "question",
        source_chat_id: str | None = None,
    ) -> PendingUserInput:
        inp = PendingUserInput(
            id=generate_id("input"),
            project_id=project_id,
            sender_id=sender_id,
            source_chat_id=source_chat_id,
            observed_state=observed_state,
            content=content,
            kind=kind,
            status="pending",
        )
        self.session.add(inp)
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Pending user input created: {inp.id} ({kind})",
            meta={
                "input_id": inp.id,
                "observed_state": observed_state,
                "kind": kind,
                "source_chat_id": source_chat_id,
            },
        )
        self.session.commit()
        return inp

    def get_pending_user_inputs(self, project_id: str) -> list[PendingUserInput]:
        return list(
            self.session.exec(
                select(PendingUserInput)
                .where(
                    PendingUserInput.project_id == project_id,
                    PendingUserInput.status == "pending",
                )
                .order_by(PendingUserInput.created_at.asc())
            ).all()
        )

    async def consume_pending_user_input(
        self,
        input_id: str,
        consumed_by_task_id: str,
        commit: bool = True,
    ) -> PendingUserInput:
        inp = self.session.get(PendingUserInput, input_id)
        if inp is None:
            raise ValueError(f"Pending input not found: {input_id}")
        inp.status = "consumed"
        inp.consumed_by_task_id = consumed_by_task_id
        if commit:
            self.session.commit()
        return inp

    # ------------------------------------------------------------------
    # Project control state
    # ------------------------------------------------------------------

    async def pause_project(self, project_id: str, triggered_by: str = "user") -> Project:
        project = self.require_project(project_id)
        if project.status == "paused":
            return project
        project.previous_state = project.current_state
        project.status = "paused"
        project.updated_at = _now()
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message="Project paused",
            meta={"state_at_pause": project.previous_state, "triggered_by": triggered_by},
        )
        self.session.commit()
        return project

    async def resume_project(self, project_id: str, triggered_by: str = "user") -> Project:
        project = self.require_project(project_id)
        if project.status != "paused":
            return project
        project.status = "active"
        project.updated_at = _now()
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message="Project resumed",
            meta={"resume_to_state": project.current_state, "triggered_by": triggered_by},
        )
        self.session.commit()
        return project

    async def rollback_project(
        self,
        project_id: str,
        target_state: str,
        triggered_by: str = "user",
    ) -> Project:
        project = self.require_project(project_id)
        state_machine = StateMachine(project.task_type)
        if target_state not in state_machine.states:
            raise InvalidStateTransitionError(project.current_state, f"rollback_to_{target_state}")

        project.previous_state = project.current_state
        project.current_state = target_state
        project.updated_at = _now()

        # Mark running tasks as failed so their outputs are discarded.
        for task in self._running_tasks(project_id):
            task.status = "failed"
            task.error_message = f"Rolled back to {target_state}"

        # Expire pending approvals.
        for approval in self._pending_approvals(project_id):
            approval.status = "expired"

        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Project rolled back to {target_state}",
            meta={"from_state": project.previous_state, "to_state": target_state, "triggered_by": triggered_by},
        )
        self.session.commit()
        return project

    async def expire_pending_approvals_for_state(
        self, project_id: str, state: str
    ) -> None:
        for approval in self._pending_approvals(project_id):
            if approval.state == state:
                approval.status = "expired"
                approval.updated_at = _now()
        self.session.commit()

    # ------------------------------------------------------------------
    # Question interactions (unified ask_user / AGENT_INPUT_PENDING)
    # ------------------------------------------------------------------

    async def create_question_interaction(
        self, project_id, origin_task_id, origin_round, resume_state,
        agent, stage, batch_key, questions, approval_id=None, commit=True,
    ) -> QuestionInteraction:
        interaction_id = generate_id("qi")
        interaction = QuestionInteraction(
            id=interaction_id, project_id=project_id, origin_task_id=origin_task_id,
            origin_round=origin_round, resume_state=resume_state, agent=agent,
            stage=stage, batch_key=batch_key, status="pending", approval_id=approval_id,
        )
        self.session.add(interaction)
        for idx, q in enumerate(questions):
            qid = q.get("question_id") or generate_id("q")
            options = q.get("options") or []
            recommended = q.get("recommended_option")
            if recommended is None:
                for opt in options:
                    if opt.get("recommended"):
                        recommended = opt.get("key")
                        break
            question = Question(
                id=generate_id("q"), interaction_id=interaction_id, question_id=qid,
                kind=q.get("kind", "blocking"), question=q.get("question", ""),
                reason=q.get("reason", ""), allow_free_text=q.get("allow_free_text", False),
                options=options, multiple=q.get("multiple", False),
                default_assumption=q.get("default_assumption"), recommended_option=recommended,
                affected_trace_ids=q.get("affected_trace_ids", []), status="pending",
                order_index=idx,
            )
            self.session.add(question)
        await self.log_event(
            project_id=project_id, level="INFO",
            message=f"QuestionInteraction created: {interaction_id} ({len(questions)} questions)",
            meta={"interaction_id": interaction_id, "agent": agent, "stage": stage},
        )
        if commit:
            self.session.commit()
        return interaction

    def get_question_interaction(self, interaction_id) -> QuestionInteraction | None:
        return self.session.get(QuestionInteraction, interaction_id)

    def get_open_question_interaction(self, project_id) -> QuestionInteraction | None:
        return self.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.status == "pending",
            ).order_by(QuestionInteraction.created_at.desc())
        ).first()

    def get_questions_for_interaction(self, interaction_id) -> list[Question]:
        return list(self.session.exec(
            select(Question).where(Question.interaction_id == interaction_id)
            .order_by(Question.order_index)
        ).all())

    def get_question_answer(self, question_db_id) -> QuestionAnswer | None:
        return self.session.exec(
            select(QuestionAnswer).where(QuestionAnswer.question_id == question_db_id)
            .order_by(QuestionAnswer.responded_at.desc())
        ).first()

    async def submit_question_answer(
        self, question_db_id, answer, answer_label, skipped, responded_by, commit=True,
    ) -> QuestionAnswer:
        qa = QuestionAnswer(
            id=generate_id("qa"), question_id=question_db_id, answer=answer,
            answer_label=answer_label, skipped=skipped, responded_by=responded_by,
        )
        self.session.add(qa)
        question = self.session.get(Question, question_db_id)
        if question is not None:
            question.status = "skipped" if skipped else "answered"
        if commit:
            self.session.commit()
        return qa

    def interaction_is_resolvable(self, interaction_id) -> bool:
        questions = self.get_questions_for_interaction(interaction_id)
        if not questions:
            return True
        for q in questions:
            if q.kind in ("blocking", "decision") and q.status == "pending":
                return False
        return True

    async def resolve_question_interaction(self, interaction_id, commit=True) -> QuestionInteraction | None:
        interaction = self.session.get(QuestionInteraction, interaction_id)
        if interaction is None:
            return None
        interaction.status = "answered"
        interaction.answered_at = _now()
        if commit:
            self.session.commit()
        return interaction

    def expire_open_question_interactions(self, project_id) -> int:
        interactions = self.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.status == "pending",
            )
        ).all()
        for it in interactions:
            it.status = "expired"
        self.session.commit()
        return len(interactions)

    def get_default_authorization(self, project_id, question_id) -> DefaultAuthorization | None:
        auths = self.session.exec(
            select(DefaultAuthorization).where(DefaultAuthorization.project_id == project_id)
        ).all()
        for auth in auths:
            if question_id.startswith(auth.question_pattern):
                return auth
        return None

    async def create_default_authorization(
        self, project_id, question_pattern, authorized_option, created_by=None, commit=True,
    ) -> DefaultAuthorization:
        auth = DefaultAuthorization(
            id=generate_id("dauth"), project_id=project_id,
            question_pattern=question_pattern, authorized_option=authorized_option,
            created_by=created_by,
        )
        self.session.add(auth)
        if commit:
            self.session.commit()
        return auth

    def serialize_question_interaction(self, interaction_id) -> dict[str, Any] | None:
        interaction = self.session.get(QuestionInteraction, interaction_id)
        if interaction is None:
            return None
        questions = self.get_questions_for_interaction(interaction_id)
        result_questions = []
        for q in questions:
            qa = self.get_question_answer(q.id)
            result_questions.append({
                "db_id": q.id, "question_id": q.question_id, "kind": q.kind,
                "question": q.question, "reason": q.reason,
                "allow_free_text": q.allow_free_text, "options": q.options,
                "multiple": q.multiple, "default_assumption": q.default_assumption,
                "recommended_option": q.recommended_option,
                "affected_trace_ids": q.affected_trace_ids, "status": q.status,
                "answer": qa.answer if qa else None,
                "answer_label": qa.answer_label if qa else None,
                "skipped": qa.skipped if qa else False,
            })
        return {
            "interaction_id": interaction.id, "project_id": interaction.project_id,
            "origin_task_id": interaction.origin_task_id, "origin_round": interaction.origin_round,
            "resume_state": interaction.resume_state, "agent": interaction.agent,
            "stage": interaction.stage, "batch_key": interaction.batch_key,
            "status": interaction.status, "approval_id": interaction.approval_id,
            "questions": result_questions,
            "is_resolvable": self.interaction_is_resolvable(interaction_id),
        }

    # ------------------------------------------------------------------
    # Conversation interactions (project-not-determined disambiguation)
    # ------------------------------------------------------------------

    async def create_conversation_interaction(
        self, chat_id, open_id, source_message_id, source_text, candidates, commit=True,
    ) -> ConversationInteraction:
        existing = self.session.exec(
            select(ConversationInteraction).where(
                ConversationInteraction.chat_id == chat_id,
                ConversationInteraction.open_id == open_id,
                ConversationInteraction.status == "pending",
            )
        ).all()
        for it in existing:
            it.status = "expired"
        interaction = ConversationInteraction(
            id=generate_id("ci"), chat_id=chat_id, open_id=open_id,
            source_message_id=source_message_id, source_text=source_text,
            candidates=candidates,
        )
        self.session.add(interaction)
        if commit:
            self.session.commit()
        return interaction

    def get_pending_conversation_interaction(self, chat_id, open_id) -> ConversationInteraction | None:
        return self.session.exec(
            select(ConversationInteraction).where(
                ConversationInteraction.chat_id == chat_id,
                ConversationInteraction.open_id == open_id,
                ConversationInteraction.status == "pending",
            ).order_by(ConversationInteraction.created_at.desc())
        ).first()

    async def resolve_conversation_interaction(
        self, interaction_id, selected_project_id, commit=True,
    ) -> ConversationInteraction | None:
        interaction = self.session.get(ConversationInteraction, interaction_id)
        if interaction is None:
            return None
        interaction.selected_project_id = selected_project_id
        interaction.status = "resolved"
        interaction.resolved_at = _now()
        if commit:
            self.session.commit()
        return interaction

    # ------------------------------------------------------------------

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
        state_path = project_mac_dir(project) / "STATE.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self._build_pending_approval(project)

        # Build read-only summaries for agents.
        context = dict(project.context)
        context["pending_user_inputs"] = [
            {
                "input_id": inp.id,
                "sender_id": inp.sender_id,
                "source_chat_id": inp.source_chat_id,
                "observed_state": inp.observed_state,
                "content": inp.content,
                "kind": inp.kind,
                "created_at": inp.created_at.isoformat(),
            }
            for inp in self.get_pending_user_inputs(project.id)
        ]
        context["open_feedback"] = [
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
            for fb in self.get_open_feedback_packets(project.id)
        ]

        state_doc = {
            "version": "1.0",
            "project_id": project.id,
            "current_state": project.current_state,
            "previous_state": project.previous_state,
            "pending_approval": pending,
            "context": context,
            "history": project.context.get("history", []),
        }

        # Append current state to history
        history = state_doc["history"]
        history.append({"state": project.current_state, "entered_at": _now().isoformat()})
        project.context = {**project.context, "history": history}

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
