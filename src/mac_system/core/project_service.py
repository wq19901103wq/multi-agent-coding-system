"""Persistence and project-directory services for the thin MAC host.

This module deliberately contains no workflow, Agent role, prompt, retry, or
state-transition policy.  Codex owns turn execution and context management.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, or_, update
from sqlmodel import Session, select

from mac_system.config import get_settings, path_within_allowed_bind_roots
from mac_system.core.secret_store import LocalSecretStore
from mac_system.models import (
    Approval,
    CardProjection,
    ChatDirectoryBinding,
    ChatUserContext,
    EventLog,
    FeishuEventLog,
    NotificationOutbox,
    PendingIntent,
    ProcessedEvent,
    Project,
    ProjectChat,
    ProjectDecision,
    ProjectHistoryRound,
    ProjectPolicy,
    ProjectOwnerReview,
    ProjectOwnerState,
    ProjectUser,
    Question,
    QuestionAnswer,
    QuestionInteraction,
    RootAgentSession,
    Task,
    UserIdentityLink,
    UserOwnerProfile,
    UserOwnerMemory,
    UserOwnerSource,
)
from mac_system.utils.redaction import redact_secrets, redact_text
from mac_system.utils.identifiers import generate_id as generate_id


def _now() -> datetime:
    return datetime.utcnow()


def _walk_dirs(root: Path, *, max_depth: int) -> Any:
    root = root.resolve()
    yield root
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if (
                child.is_symlink()
                or not child.is_dir()
                or child.name.startswith(".")
                or child.name in {"node_modules", "__pycache__"}
            ):
                continue
            yield child
            queue.append((child, depth + 1))


class ProjectNotFoundError(Exception):
    pass


class ApprovalNotFoundError(Exception):
    pass


class InvalidStateTransitionError(Exception):
    """Compatibility name for removed legacy callers."""


class StaleApprovalStateError(Exception):
    """Compatibility name for removed legacy callers."""


class ProjectService:
    def __init__(self, session: Session, *, bot_account_id: str = "default"):
        self.session = session
        self.settings = get_settings()
        self.bot_account_id = str(bot_account_id or "default")
        database = str(getattr(session.get_bind().url, "database", "") or "")
        secret_path = (
            None
            if database in {"", ":memory:"}
            else self.settings.secret_store_path.resolve()
        )
        self.secret_store = LocalSecretStore(secret_path)

    def _scoped_chat_id(self, chat_id: str) -> str:
        """Reuse legacy keys for the default bot and namespace additional bots."""

        return (
            chat_id
            if self.bot_account_id == "default"
            else f"{self.bot_account_id}\x1f{chat_id}"
        )

    def _scoped_open_id(self, open_id: str) -> str:
        """Namespace singleton legacy user rows without rewriting their PK."""

        return (
            open_id
            if self.bot_account_id == "default"
            else f"{self.bot_account_id}\x1f{open_id}"
        )

    def _scoped_event_id(self, event_id: str) -> str:
        return (
            event_id
            if self.bot_account_id == "default"
            else f"{self.bot_account_id}\x1f{event_id}"
        )

    def _fallback_human_id(self, open_id: str) -> str:
        return f"feishu-open:{self.bot_account_id}:{open_id}"

    def get_human_id(self, open_id: str) -> str:
        """Resolve one app-scoped Feishu user to the shared human identity."""

        row = self.session.exec(
            select(UserIdentityLink).where(
                UserIdentityLink.bot_account_id == self.bot_account_id,
                UserIdentityLink.open_id == open_id,
            )
        ).first()
        return row.human_id if row is not None else self._fallback_human_id(open_id)

    def register_user_identity(
        self,
        open_id: str,
        *,
        user_id: str | None = None,
        union_id: str | None = None,
    ) -> str:
        """Link Feishu app identities without coupling projects or chats.

        ``user_id`` is tenant-wide and is preferred when present; ``union_id``
        is the cross-app fallback. Existing memory/source rows are relabelled,
        never deleted or rewritten.
        """

        open_id = str(open_id or "").strip()
        user_id = str(user_id or "").strip() or None
        union_id = str(union_id or "").strip() or None
        if not open_id:
            return ""

        current = self.session.exec(
            select(UserIdentityLink).where(
                UserIdentityLink.bot_account_id == self.bot_account_id,
                UserIdentityLink.open_id == open_id,
            )
        ).first()
        related = None
        if user_id or union_id:
            clauses = []
            if user_id:
                clauses.append(UserIdentityLink.feishu_user_id == user_id)
            if union_id:
                clauses.append(UserIdentityLink.feishu_union_id == union_id)
            related = self.session.exec(
                select(UserIdentityLink).where(or_(*clauses))
            ).first()

        if related is not None:
            human_id = related.human_id
        elif user_id:
            human_id = f"feishu-user:{user_id}"
        elif union_id:
            human_id = f"feishu-union:{union_id}"
        elif current is not None:
            human_id = current.human_id
        else:
            human_id = self._fallback_human_id(open_id)

        old_ids = {self._fallback_human_id(open_id)}
        if current is None:
            current = UserIdentityLink(
                id=generate_id("identity"),
                human_id=human_id,
                bot_account_id=self.bot_account_id,
                open_id=open_id,
                feishu_user_id=user_id,
                feishu_union_id=union_id,
            )
        else:
            old_ids.add(current.human_id)
            current.human_id = human_id
            current.feishu_user_id = user_id or current.feishu_user_id
            current.feishu_union_id = union_id or current.feishu_union_id
            current.updated_at = _now()
        self.session.add(current)

        # If a later event exposes a stronger identifier, merge every link
        # that carried either Feishu alias into the same human key.
        alias_clauses = []
        if user_id:
            alias_clauses.append(UserIdentityLink.feishu_user_id == user_id)
        if union_id:
            alias_clauses.append(UserIdentityLink.feishu_union_id == union_id)
        if alias_clauses:
            for link in self.session.exec(
                select(UserIdentityLink).where(or_(*alias_clauses))
            ).all():
                old_ids.add(link.human_id)
                link.human_id = human_id
                link.updated_at = _now()
                self.session.add(link)

        old_ids.discard(human_id)
        if old_ids:
            self.session.exec(
                update(UserOwnerMemory)
                .where(UserOwnerMemory.human_id.in_(old_ids))
                .values(human_id=human_id)
            )
            self.session.exec(
                update(UserOwnerSource)
                .where(UserOwnerSource.human_id.in_(old_ids))
                .values(human_id=human_id)
            )
        # Rows written before the human-id migration may have an empty key.
        self.session.exec(
            update(UserOwnerMemory)
            .where(
                UserOwnerMemory.bot_account_id == self.bot_account_id,
                UserOwnerMemory.open_id == open_id,
                UserOwnerMemory.human_id == "",
            )
            .values(human_id=human_id)
        )
        self.session.exec(
            update(UserOwnerSource)
            .where(
                UserOwnerSource.bot_account_id == self.bot_account_id,
                UserOwnerSource.open_id == open_id,
                UserOwnerSource.human_id == "",
            )
            .values(human_id=human_id)
        )
        self.session.commit()
        return human_id

    # Projects and directories -------------------------------------------------

    async def create_project(
        self,
        name: str | None,
        task_type: str,
        description: str,
        approval_mode: str = "normal",
        bot_account_id: str | None = None,
        creator_open_id: str | None = None,
        feishu_chat_id: str | None = None,
        feishu_chat_type: str = "p2p",
        bound_directory: str | None = None,
        initial_context: dict | None = None,
    ) -> Project:
        project_id = generate_id("proj")
        if bound_directory:
            workspace = self._prepare_bound_directory(bound_directory)
        else:
            workspace = (self.settings.projects_dir / project_id).resolve()
            workspace.mkdir(parents=True, exist_ok=False)
            (workspace / ".gitignore").write_text(".mac/\n", encoding="utf-8")
            self._initialize_repository(workspace, [".gitignore"])
        account_id = str(bot_account_id or self.bot_account_id)
        (workspace / ".mac").mkdir(exist_ok=True)

        context = dict(initial_context or {})
        context["orchestration_mode"] = "root_agent"
        project = Project(
            id=project_id,
            name=name,
            task_type=task_type,
            description=description,
            status="active",
            execution_status="idle",
            current_state="ACTIVE",
            workspace_path=str(workspace),
            bound_directory=str(workspace) if bound_directory else None,
            context=context,
            approval_config={"mode": approval_mode},
            bot_account_id=account_id,
            feishu_chat_id=feishu_chat_id,
            feishu_chat_type=feishu_chat_type,
        )
        self.session.add(project)
        self.session.flush()
        if creator_open_id:
            self.session.add(
                ProjectUser(
                    project_id=project.id,
                    open_id=creator_open_id,
                    role="owner",
                )
            )
        if feishu_chat_id:
            self.session.add(
                ProjectChat(
                    id=generate_id("pch"),
                    project_id=project.id,
                    bot_account_id=account_id,
                    chat_id=feishu_chat_id,
                    chat_type=feishu_chat_type,
                    relation="origin",
                    created_by=creator_open_id,
                )
            )
            if creator_open_id:
                self._set_chat_user_context(
                    feishu_chat_id, creator_open_id, project.id
                )
        self.session.add(
            EventLog(
                project_id=project.id,
                level="INFO",
                message="Project created",
                meta={"workspace": str(workspace)},
            )
        )
        self.session.commit()
        self.session.refresh(project)
        return project

    def _initialize_repository(
        self, path: Path, initial_paths: list[str] | None = None
    ) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(
            ["git", "config", "user.name", self.settings.git_user_name],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", self.settings.git_user_email],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "add", *(initial_paths or ["-A"])], cwd=path, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "Initial commit"],
            cwd=path,
            check=True,
        )

    def _prepare_bound_directory(self, directory: str) -> Path:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise ValueError("绑定目录必须是绝对路径")
        path = path.resolve()
        if not path_within_allowed_bind_roots(path):
            raise ValueError("绑定目录不在 MAC_ALLOWED_BIND_ROOTS 允许范围内")
        if path.exists() and not path.is_dir():
            raise ValueError(f"绑定路径不是目录: {path}")
        path.mkdir(parents=True, exist_ok=True)
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            root = Path(probe.stdout.strip()).resolve()
            if root != path:
                raise ValueError(f"绑定目录必须是 Git 仓库根目录；当前根目录为 {root}")
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=path,
                capture_output=True,
            )
            if head.returncode != 0:
                self._initialize_repository(path)
        else:
            self._initialize_repository(path)
        return path

    @staticmethod
    def scan_candidate_repositories(
        allowed_roots: list[Path], *, max_depth: int = 2, limit: int = 12
    ) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        seen: set[Path] = set()
        for root in allowed_roots:
            if not root.exists() or not root.is_dir():
                continue
            for candidate in _walk_dirs(root, max_depth=max_depth):
                if candidate in seen or not (candidate / ".git").exists():
                    continue
                seen.add(candidate)
                output.append({"path": str(candidate), "name": candidate.name})
                if len(output) >= limit:
                    return output
        return output

    def get_project(self, project_id: str) -> Project | None:
        return self.session.get(Project, project_id)

    def require_project(self, project_id: str) -> Project:
        project = self.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    def list_projects(self) -> list[Project]:
        return list(
            self.session.exec(select(Project).order_by(Project.updated_at.desc())).all()
        )

    def get_project_status(self, project_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        interaction = self.session.exec(
            select(QuestionInteraction)
            .where(
                QuestionInteraction.project_id == project.id,
                QuestionInteraction.status == "pending",
            )
            .order_by(QuestionInteraction.created_at.desc())
        ).first()
        return {
            "project_id": project.id,
            "name": project.name,
            "status": project.status,
            "display": project.execution_status,
            "execution_status": project.execution_status,
            "waiting_reason": project.waiting_reason,
            "workspace_path": project.workspace_path,
            "bound_directory": project.bound_directory,
            "pending_question": (
                {
                    "interaction_id": interaction.id,
                    "requested_to": interaction.requested_to,
                    "stage": interaction.stage,
                }
                if interaction
                else None
            ),
        }

    def set_execution_status(
        self,
        project_id: str,
        status: str,
        *,
        reason: str | None = None,
        commit: bool = True,
    ) -> Project:
        project = self.require_project(project_id)
        project.execution_status = status
        project.waiting_reason = reason
        project.waiting_since = _now() if status.startswith("waiting") else None
        project.updated_at = _now()
        self.session.add(project)
        if commit:
            self.session.commit()
            self.session.refresh(project)
        return project

    async def pause_project(self, project_id: str, triggered_by: str = "user") -> Project:
        project = self.require_project(project_id)
        project.status = "paused"
        project.updated_at = _now()
        await self.log_event(project_id, "INFO", "Project paused", {"by": triggered_by})
        self.session.commit()
        return project

    async def resume_project(self, project_id: str, triggered_by: str = "user") -> Project:
        project = self.require_project(project_id)
        changed = False
        if project.status == "paused":
            project.status = "active"
            project.updated_at = _now()
            changed = True
        if changed:
            await self.log_event(project_id, "INFO", "Project resumed", {"by": triggered_by})
            self.session.commit()
        return project

    async def cancel_project(
        self,
        project_id: str,
        reason: str = "",
        triggered_by: str = "user",
    ) -> Project:
        project = self.require_project(project_id)
        project.status = "cancelled"
        project.updated_at = _now()
        self.expire_pending_question_interactions(
            project.id,
            resolution_source="project_cancelled",
            commit=False,
        )
        for context in self.session.exec(
            select(ChatUserContext).where(ChatUserContext.active_project_id == project.id)
        ).all():
            context.active_project_id = None
            context.updated_at = _now()
            self.session.add(context)
        await self.log_event(
            project.id,
            "INFO",
            "Project cancelled",
            {"by": triggered_by, "reason": reason},
        )
        self.session.commit()
        return project

    def expire_pending_question_interactions(
        self,
        project_id: str,
        *,
        resolution_source: str,
        commit: bool = True,
    ) -> list[QuestionInteraction]:
        """Expire unresolved native prompts at an explicit execution boundary."""

        for approval in self.session.exec(
            select(Approval).where(
                Approval.project_id == project_id, Approval.status == "pending"
            )
        ).all():
            approval.status = "expired"
            self.session.add(approval)
        interactions = list(self.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.status == "pending",
            )
        ).all())
        for interaction in interactions:
            interaction.status = "expired"
            interaction.answered_at = _now()
            interaction.resolution_source = resolution_source
            self.session.add(interaction)
        if commit:
            self.session.commit()
        return interactions

    def _running_tasks(self, project_id: str) -> list[Task]:
        return list(
            self.session.exec(
                select(Task).where(Task.project_id == project_id, Task.status == "running")
            ).all()
        )

    # Chat binding -------------------------------------------------------------

    def bind_directory_to_chat(
        self, chat_id: str, directory: str, bound_by: str | None = None
    ) -> ChatDirectoryBinding:
        path = self._prepare_bound_directory(directory)
        scoped_chat_id = self._scoped_chat_id(chat_id)
        binding = self.session.get(ChatDirectoryBinding, scoped_chat_id)
        if binding is None:
            binding = ChatDirectoryBinding(
                chat_id=scoped_chat_id,
                directory=str(path),
                bound_by=bound_by,
            )
        else:
            binding.directory = str(path)
            binding.bound_by = bound_by
            binding.updated_at = _now()
        self.session.add(binding)
        for context in self.session.exec(
            select(ChatUserContext).where(ChatUserContext.chat_id == scoped_chat_id)
        ).all():
            context.active_project_id = None
            context.updated_at = _now()
            self.session.add(context)
        self.session.commit()
        return binding

    def get_chat_directory_binding(self, chat_id: str) -> ChatDirectoryBinding | None:
        return self.session.get(ChatDirectoryBinding, self._scoped_chat_id(chat_id))

    def unbind_directory_from_chat(self, chat_id: str) -> bool:
        binding = self.session.get(
            ChatDirectoryBinding, self._scoped_chat_id(chat_id)
        )
        if binding is None:
            return False
        self.session.delete(binding)
        self.session.commit()
        return True

    def bind_project_to_chat(
        self,
        project_id: str,
        chat_id: str,
        chat_type: str = "p2p",
        relation: str = "subscribed",
        notification_level: str = "all",
        created_by: str | None = None,
    ) -> ProjectChat:
        existing = self.session.exec(
            select(ProjectChat).where(
                ProjectChat.project_id == project_id,
                ProjectChat.bot_account_id == self.bot_account_id,
                ProjectChat.chat_id == chat_id,
            )
        ).first()
        if existing:
            return existing
        row = ProjectChat(
            id=generate_id("pch"),
            project_id=project_id,
            bot_account_id=self.bot_account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            relation=relation,
            notification_level=notification_level,
            created_by=created_by,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def _set_chat_user_context(
        self, chat_id: str, open_id: str, project_id: str | None
    ) -> ChatUserContext:
        scoped_chat_id = self._scoped_chat_id(chat_id)
        context = self.session.get(ChatUserContext, (scoped_chat_id, open_id))
        if context is None:
            context = ChatUserContext(
                chat_id=scoped_chat_id,
                open_id=open_id,
                active_project_id=project_id,
            )
        else:
            context.active_project_id = project_id
            context.selected_at = _now()
            context.updated_at = _now()
        self.session.add(context)
        return context

    def set_chat_user_context(
        self, chat_id: str, open_id: str, project_id: str | None
    ) -> ChatUserContext:
        context = self._set_chat_user_context(chat_id, open_id, project_id)
        self.session.commit()
        return context

    def get_chat_user_selected_project(
        self, chat_id: str, open_id: str
    ) -> Project | None:
        context = self.session.get(
            ChatUserContext, (self._scoped_chat_id(chat_id), open_id)
        )
        if context is None or not context.active_project_id:
            return None
        project = self.get_project(context.active_project_id)
        return (
            project
            if project
            and project.status != "cancelled"
            and project.bot_account_id == self.bot_account_id
            else None
        )

    def get_chat_user_active_project(
        self, chat_id: str, open_id: str
    ) -> Project | None:
        project = self.get_chat_user_selected_project(chat_id, open_id)
        return project if project and project.status in {"active", "paused"} else None

    def get_user_project_for_directory(
        self, open_id: str, directory: str, *, chat_id: str
    ) -> Project | None:
        """Return this chat's latest live project for ``directory``.

        A filesystem directory is a Project workspace, not a second chat-level
        selection state.  Reopening a directory therefore resumes its existing
        Project in the same chat.  A different chat gets a separate Project and
        Codex thread so conversation history cannot cross chat boundaries.
        """

        path = self._prepare_bound_directory(directory)
        return self.session.exec(
            select(Project)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .join(ProjectChat, ProjectChat.project_id == Project.id)
            .where(ProjectUser.open_id == open_id)
            .where(Project.bot_account_id == self.bot_account_id)
            .where(ProjectChat.bot_account_id == self.bot_account_id)
            .where(ProjectChat.chat_id == chat_id)
            .where(Project.workspace_path == str(path))
            .where(Project.status.in_(["active", "paused"]))
            .order_by(Project.updated_at.desc())
        ).first()

    def user_can_access_project(self, open_id: str, project_id: str) -> bool:
        return (
            self.session.exec(
                select(ProjectUser)
                .join(Project, Project.id == ProjectUser.project_id)
                .where(
                    ProjectUser.open_id == open_id,
                    ProjectUser.project_id == project_id,
                    Project.bot_account_id == self.bot_account_id,
                )
            ).first()
            is not None
        )

    def list_projects_for_chat(self, chat_id: str) -> list[Project]:
        links = self.session.exec(
            select(ProjectChat).where(
                ProjectChat.bot_account_id == self.bot_account_id,
                ProjectChat.chat_id == chat_id,
            )
        ).all()
        ids = [link.project_id for link in links]
        if not ids:
            return []
        return list(
            self.session.exec(
                select(Project)
                .where(Project.id.in_(ids), Project.status != "cancelled")
                .order_by(Project.updated_at.desc())
            ).all()
        )

    def get_last_event_at(self, chat_id: str, open_id: str) -> datetime | None:
        context = self.session.get(
            ChatUserContext, (self._scoped_chat_id(chat_id), open_id)
        )
        return context.last_event_at if context else None

    def update_last_event_at(
        self, chat_id: str, open_id: str, event_at: datetime
    ) -> None:
        scoped_chat_id = self._scoped_chat_id(chat_id)
        context = self.session.get(ChatUserContext, (scoped_chat_id, open_id))
        if context is None:
            context = ChatUserContext(
                chat_id=scoped_chat_id,
                open_id=open_id,
                active_project_id=None,
            )
        if context.last_event_at is None or event_at > context.last_event_at:
            context.last_event_at = event_at
            context.updated_at = _now()
        self.session.add(context)
        self.session.commit()

    def list_offline_notice_targets(
        self, *, within_days: int = 14, limit: int = 20
    ) -> list[str]:
        """Chats with an active project that recently exchanged events.

        Used to tell users which conversations may have missed messages while
        the Feishu WebSocket was down. Rows where chat_id == open_id are the
        per-user watermark entries, not real chats, so they are excluded.
        """

        cutoff = _now() - timedelta(days=within_days)
        rows = self.session.exec(
            select(ChatUserContext)
            .where(
                ChatUserContext.active_project_id.is_not(None),
                ChatUserContext.chat_id != ChatUserContext.open_id,
                ChatUserContext.last_event_at >= cutoff,
            )
            .order_by(ChatUserContext.last_event_at.desc())
            .limit(limit)
        ).all()
        chat_ids: list[str] = []
        for row in rows:
            if self.bot_account_id == "default":
                chat_ids.append(row.chat_id)
            elif row.chat_id.startswith(f"{self.bot_account_id}\x1f"):
                chat_ids.append(row.chat_id[len(self.bot_account_id) + 1:])
        return chat_ids

    # AI Project Owner decisions ---------------------------------------------

    def create_project_decision(
        self,
        project_id: str,
        *,
        request_item_id: str | None,
        native_turn_id: str | None,
        question: str,
        options: list[dict[str, Any]],
        decision: str,
        rationale: str,
        impact: str,
        reversible: bool,
        confidence: str,
        answer_payload: dict[str, Any],
        context_refs: list[str],
        authority_refs: list[str] | None = None,
        actor_model: str | None,
        actor_usage: dict[str, int] | None = None,
        actor_duration_seconds: float | None = None,
        status: str = "applied",
        parent_decision_id: str | None = None,
        decision_type: str = "question",
        execution_status: str = "not_applicable",
        execution_rounds: int = 0,
        acceptance_criteria: list[str] | None = None,
    ) -> ProjectDecision:
        row = ProjectDecision(
            id=generate_id("decision"),
            project_id=project_id,
            request_item_id=request_item_id,
            native_turn_id=native_turn_id,
            parent_decision_id=parent_decision_id,
            decision_type=decision_type,
            question=question,
            options=list(options),
            decision=decision,
            rationale=rationale,
            impact=impact,
            reversible=reversible,
            confidence=confidence,
            answer_payload=dict(answer_payload),
            context_refs=list(context_refs),
            authority_refs=list(dict.fromkeys(authority_refs or [])),
            actor_model=actor_model,
            actor_usage=dict(actor_usage or {}),
            actor_duration_seconds=actor_duration_seconds,
            status=status,
            execution_status=execution_status,
            execution_rounds=max(0, int(execution_rounds)),
            acceptance_criteria=[
                str(item).strip()
                for item in (acceptance_criteria or [])
                if str(item).strip()
            ][:12],
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_project_decision(self, decision_id: str) -> ProjectDecision | None:
        return self.session.get(ProjectDecision, decision_id)

    def get_project_decision_by_request_item_id(
        self, project_id: str, request_item_id: str
    ) -> ProjectDecision | None:
        return self.session.exec(
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.request_item_id == request_item_id,
            )
            .order_by(ProjectDecision.created_at.desc())
        ).first()

    def get_project_decision_by_card_message_id(
        self, project_id: str, card_message_id: str
    ) -> ProjectDecision | None:
        return self.session.exec(
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.card_message_id == card_message_id,
            )
            .order_by(ProjectDecision.created_at.desc())
        ).first()

    def get_project_decisions(
        self, project_id: str, *, limit: int = 20
    ) -> list[ProjectDecision]:
        return list(
            self.session.exec(
                select(ProjectDecision)
                .where(ProjectDecision.project_id == project_id)
                .order_by(ProjectDecision.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
        )

    def reconcile_project_owner_assessments(self) -> list[ProjectDecision]:
        """Close assessment records whose native interaction is no longer open.

        The native QuestionInteraction remains the lifecycle authority. This
        repair is intentionally presentation-only: it never answers a Codex
        request or changes a project/turn state.
        """

        repaired: list[ProjectDecision] = []
        rows = self.session.exec(
            select(ProjectDecision).where(
                ProjectDecision.decision_type.in_(
                    ["assessment", "approval_assessment"]
                ),
                ProjectDecision.status == "requires_user",
                ProjectDecision.request_item_id.like("assessment:%"),
            )
        ).all()
        for row in rows:
            request_item_id = str(row.request_item_id or "")[len("assessment:") :]
            interaction = self.get_question_interaction_by_request_item_id(
                row.project_id, request_item_id
            )
            if interaction is None or interaction.status in {"pending", "resolving"}:
                continue
            row.status = (
                "resolved_by_user"
                if interaction.status == "answered"
                else "expired"
            )
            if interaction.status == "answered" and not row.user_override:
                submitted: list[str] = []
                for question in self.get_questions_for_interaction(interaction.id):
                    answer = self.get_question_answer(question.id)
                    if answer is not None and not answer.skipped and answer.answer:
                        submitted.append(str(answer.answer_label or answer.answer))
                if submitted:
                    row.user_override = "；".join(submitted)
                    row.overridden_at = _now()
            row.updated_at = _now()
            self.session.add(row)
            repaired.append(row)
        if repaired:
            self.session.commit()
        return repaired

    def get_open_project_commitments(
        self, project_id: str, *, limit: int = 20
    ) -> list[ProjectDecision]:
        """Return unfinished governance commitments independently of recency."""

        return list(
            self.session.exec(
                select(ProjectDecision)
                .where(
                    ProjectDecision.project_id == project_id,
                    ProjectDecision.decision_type == "governance",
                    ProjectDecision.status.in_(["applied", "adjusted"]),
                    ProjectDecision.execution_status.in_(
                        ["active", "blocked", "needs_revision"]
                    ),
                )
                .order_by(ProjectDecision.updated_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
        )

    def update_project_decision(
        self,
        decision_id: str,
        *,
        status: str,
        card_message_id: str | None = None,
        user_override: str | None = None,
        execution_status: str | None = None,
        result_ref: str | None = None,
        increment_execution_rounds: bool = False,
        user_choice_actor: str | None = None,
    ) -> ProjectDecision:
        row = self.get_project_decision(decision_id)
        if row is None:
            raise ValueError(f"Project decision not found: {decision_id}")
        row.status = status
        if card_message_id is not None:
            row.card_message_id = card_message_id
        if user_override is not None:
            row.user_override = user_override
            row.overridden_at = _now()
            if user_choice_actor:
                self.session.add(
                    EventLog(
                        project_id=row.project_id,
                        level="INFO",
                        message="User choice recorded",
                        meta={
                            "kind": "user_choice",
                            "source": "project_owner_decision",
                            "decision_id": row.id,
                            "source_ref": row.id,
                            "question": row.question,
                            "options": self._public_decision_options(row.options, owner_card=True),
                            "choice": user_override,
                            "responded_by": user_choice_actor,
                            "choice_kind": (
                                "selection" if status == "resolved_by_user" else "correction"
                            ),
                            "choice_action": next(
                                (
                                    str(option.get("action") or "execute")
                                    for option in (row.options or [])
                                    if isinstance(option, dict)
                                    and str(option.get("label") or "").strip() == user_override
                                ),
                                "adjust" if status == "adjusted" else "execute",
                            ),
                            "context": self._public_owner_choice_context(row),
                            "card_message_id": row.card_message_id,
                        },
                    )
                )
        if execution_status is not None:
            row.execution_status = execution_status
        if result_ref:
            refs = [str(item) for item in row.result_refs if str(item)]
            if result_ref not in refs:
                refs.append(result_ref)
            row.result_refs = refs[-30:]
        if increment_execution_rounds:
            row.execution_rounds = max(0, int(row.execution_rounds or 0)) + 1
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def get_active_project_decision(self, project_id: str) -> ProjectDecision | None:
        return self.session.exec(
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.decision_type == "governance",
                ProjectDecision.execution_status == "active",
            )
            .order_by(ProjectDecision.updated_at.desc())
        ).first()

    # Project Owner management history --------------------------------------

    def _next_project_history_sequence(self, project_id: str) -> int:
        latest = self.session.exec(
            select(func.max(ProjectHistoryRound.sequence)).where(
                ProjectHistoryRound.project_id == project_id
            )
        ).one()
        return int(latest or 0) + 1

    def begin_project_history_round(
        self,
        project_id: str,
        *,
        user_request: str,
        actor_open_id: str | None = None,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        reply_context: dict[str, Any] | None = None,
        native_thread_id: str | None = None,
        started_at: datetime | None = None,
    ) -> ProjectHistoryRound:
        """Open one durable instruction/result round without execution detail."""

        self._bootstrap_project_history(project_id)
        now = started_at or _now()
        request = redact_text(str(user_request or "").strip())
        instruction = {
            "text": request,
            "origin": request_origin or "human",
            "time": now.isoformat(timespec="seconds"),
        }
        if actor_open_id:
            instruction["actor_open_id"] = actor_open_id
        reply_to = self._history_reply_context(reply_context)
        if reply_to:
            instruction["reply_to"] = reply_to
        row = ProjectHistoryRound(
            id=generate_id("history_round"),
            project_id=project_id,
            sequence=self._next_project_history_sequence(project_id),
            native_thread_id=native_thread_id,
            request_origin=request_origin or "human",
            parent_decision_id=parent_decision_id,
            user_instructions=([instruction] if request else []),
            report_status="pending",
            source="native_turn",
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def append_project_history_instruction(
        self,
        round_id: str,
        text: str,
        *,
        actor_open_id: str | None = None,
        origin: str = "human_steer",
        reply_context: dict[str, Any] | None = None,
        received_at: datetime | None = None,
    ) -> ProjectHistoryRound | None:
        row = self.session.get(ProjectHistoryRound, round_id)
        request = redact_text(str(text or "").strip())
        if row is None or not request:
            return row
        timestamp = received_at or _now()
        instructions = list(row.user_instructions or [])
        instruction = {
            "text": request,
            "origin": origin,
            "time": timestamp.isoformat(timespec="seconds"),
        }
        if actor_open_id:
            instruction["actor_open_id"] = actor_open_id
        reply_to = self._history_reply_context(reply_context)
        if reply_to:
            instruction["reply_to"] = reply_to
        instructions.append(instruction)
        row.user_instructions = instructions
        row.updated_at = timestamp
        self.session.add(row)
        self.session.commit()
        return row

    @staticmethod
    def _history_reply_context(value: dict[str, Any] | None) -> dict[str, str]:
        """Keep only the public reply target needed to interpret short replies."""

        if not isinstance(value, dict):
            return {}
        bounded = {
            "kind": 80,
            "subject_type": 120,
            "subject_ref": 240,
            "summary": 1_000,
            "request": 2_000,
            "result": 2_000,
        }
        result: dict[str, str] = {}
        for key, limit in bounded.items():
            text = redact_text(str(value.get(key) or "")).strip()
            if text:
                result[key] = text[:limit]
        return result

    def finish_unreported_project_history_round(
        self, round_id: str, *, status: str = "missing_report"
    ) -> ProjectHistoryRound | None:
        row = self.session.get(ProjectHistoryRound, round_id)
        if row is None or row.report_status != "pending":
            return row
        row.report_status = status
        row.completed_at = _now()
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def record_project_history_report(
        self,
        project_id: str,
        *,
        native_turn_id: str,
        title: str,
        content: str,
        report_ref: str,
        report_status: str = "completed",
        fallback_user_request: str = "",
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> ProjectHistoryRound:
        """Attach one delivered logical report to its active project round."""

        root = self.session.exec(
            select(RootAgentSession).where(
                RootAgentSession.project_id == project_id
            )
        ).first()
        row = (
            self.session.get(ProjectHistoryRound, root.active_history_round_id)
            if root is not None and root.active_history_round_id
            else None
        )
        if row is None and native_turn_id:
            row = self.session.exec(
                select(ProjectHistoryRound).where(
                    ProjectHistoryRound.project_id == project_id,
                    ProjectHistoryRound.native_turn_id == native_turn_id,
                )
            ).first()
        if row is None:
            row = self.begin_project_history_round(
                project_id,
                user_request=fallback_user_request,
                request_origin=request_origin,
                parent_decision_id=parent_decision_id,
                native_thread_id=(root.native_thread_id if root is not None else None),
            )
        row.native_turn_id = native_turn_id or row.native_turn_id
        if root is not None and root.native_thread_id:
            row.native_thread_id = root.native_thread_id
        row.report_title = redact_text(str(title or ""))
        row.report_content = redact_text(str(content or ""))
        row.report_status = report_status
        row.report_ref = report_ref
        row.completed_at = _now()
        row.updated_at = _now()
        self.session.add(row)
        if root is not None and root.active_history_round_id == row.id:
            root.active_history_round_id = None
            root.updated_at = _now()
            self.session.add(root)
        self.session.commit()
        self.session.refresh(row)
        return row

    def _bootstrap_project_history(self, project_id: str) -> None:
        """Recover pre-projection rounds from durable delivered-review jobs."""

        known_turn_ids = {
            str(value)
            for value in self.session.exec(
                select(ProjectHistoryRound.native_turn_id).where(
                    ProjectHistoryRound.project_id == project_id,
                    ProjectHistoryRound.native_turn_id.is_not(None),
                )
            ).all()
            if value
        }
        reviews = list(
            self.session.exec(
                select(ProjectOwnerReview)
                .where(
                    ProjectOwnerReview.project_id == project_id,
                    ProjectOwnerReview.native_turn_id.is_not(None),
                    ProjectOwnerReview.request_item_id.like("report:%"),
                )
                .order_by(ProjectOwnerReview.created_at)
            ).all()
        )
        added = False
        sequence = self._next_project_history_sequence(project_id)
        for review in reviews:
            turn_id = str(review.native_turn_id or "")
            if not turn_id or turn_id in known_turn_ids:
                continue
            payload = dict(review.input_payload or {})
            user_request = redact_text(str(payload.get("user_request") or "").strip())
            report_card = payload.get("report_card")
            color = (
                str(report_card.get("color") or "")
                if isinstance(report_card, dict)
                else ""
            )
            row = ProjectHistoryRound(
                id=generate_id("history_round"),
                project_id=project_id,
                sequence=sequence,
                native_turn_id=turn_id,
                request_origin=review.request_origin or "human",
                parent_decision_id=review.parent_decision_id,
                user_instructions=(
                    [{
                        "text": user_request,
                        "origin": review.request_origin or "human",
                        "time": review.created_at.isoformat(timespec="seconds"),
                    }]
                    if user_request
                    else []
                ),
                report_title=redact_text(str(payload.get("title") or "")),
                report_content=redact_text(str(payload.get("content") or "")),
                report_status="failed" if color == "red" else "completed",
                report_ref=review.request_item_id,
                source="owner_review_backfill",
                started_at=review.created_at,
                completed_at=review.created_at,
                created_at=review.created_at,
                updated_at=review.updated_at,
            )
            self.session.add(row)
            known_turn_ids.add(turn_id)
            sequence += 1
            added = True
        if added:
            self.session.commit()

    def get_project_history_rounds(
        self,
        project_id: str,
        *,
        limit: int | None = None,
        before_sequence: int | None = None,
        newest_first: bool = False,
    ) -> list[ProjectHistoryRound]:
        self._bootstrap_project_history(project_id)
        query = select(ProjectHistoryRound).where(
            ProjectHistoryRound.project_id == project_id
        )
        if before_sequence is not None:
            query = query.where(ProjectHistoryRound.sequence < before_sequence)
        query = query.order_by(
            ProjectHistoryRound.sequence.desc()
            if newest_first
            else ProjectHistoryRound.sequence.asc()
        )
        if limit is not None:
            query = query.limit(max(1, limit))
        return list(self.session.exec(query).all())

    def get_project_history_round(
        self,
        project_id: str,
        *,
        round_id: str | None = None,
        native_turn_id: str | None = None,
    ) -> ProjectHistoryRound | None:
        self._bootstrap_project_history(project_id)
        query = select(ProjectHistoryRound).where(
            ProjectHistoryRound.project_id == project_id
        )
        if round_id:
            query = query.where(ProjectHistoryRound.id == round_id)
        elif native_turn_id:
            query = query.where(
                ProjectHistoryRound.native_turn_id == native_turn_id
            )
        else:
            query = query.where(ProjectHistoryRound.report_status == "pending")
        return self.session.exec(
            query.order_by(ProjectHistoryRound.sequence.desc())
        ).first()

    def get_public_result_notifications(
        self, project_id: str, *, limit: int = 20
    ) -> list[NotificationOutbox]:
        """Return cards that were actually delivered and are safe to show again."""

        bounded = max(1, min(limit, 50))
        candidates = self.session.exec(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.project_id == project_id,
                NotificationOutbox.status == "sent",
                NotificationOutbox.intent_type.in_(["report", "decision", "advice"]),
            )
            .order_by(NotificationOutbox.updated_at.desc())
            # Card PATCHes are durable outbox events too. Read enough rows to
            # collapse older snapshots of the same public card below.
            .limit(min(200, bounded * 4))
        ).all()
        latest: list[NotificationOutbox] = []
        seen: set[str] = set()
        for row in candidates:
            payload = row.payload or {}
            projection_key = str(payload.get("projection_key") or "").strip()
            identity = projection_key or row.id
            if identity in seen:
                continue
            seen.add(identity)
            latest.append(row)
            if len(latest) >= bounded:
                break
        return latest

    def create_project_owner_review(
        self,
        project_id: str,
        *,
        request_item_id: str,
        native_turn_id: str | None,
        outcome: str,
        decision: str,
        rationale: str,
        impact: str,
        confidence: str,
        context_refs: list[str],
        authority_refs: list[str] | None = None,
        actor_model: str | None,
        actor_usage: dict[str, int] | None,
        actor_duration_seconds: float | None,
        continue_execution: bool = False,
        memory_updates: list[dict[str, str]] | None = None,
        policy_updates: list[dict[str, str]] | None = None,
        status: str = "completed",
        input_payload: dict[str, Any] | None = None,
        review_id: str | None = None,
        request_origin: str | None = None,
        parent_decision_id: str | None = None,
    ) -> ProjectOwnerReview:
        row = self.session.get(ProjectOwnerReview, review_id) if review_id else None
        if row is None:
            row = ProjectOwnerReview(
                id=review_id or generate_id("owner_review"),
                project_id=project_id,
                request_item_id=request_item_id,
                native_turn_id=native_turn_id,
                request_origin=request_origin or "human",
                parent_decision_id=parent_decision_id,
            )
        elif request_origin is not None:
            row.request_origin = request_origin
        if parent_decision_id is not None:
            row.parent_decision_id = parent_decision_id
        row.outcome = outcome
        row.decision = decision
        row.rationale = rationale
        row.impact = impact
        row.confidence = confidence
        row.continue_execution = bool(continue_execution)
        row.memory_updates = list(memory_updates or [])
        row.policy_updates = list(policy_updates or [])
        row.context_refs = list(context_refs)
        row.authority_refs = list(dict.fromkeys(authority_refs or []))
        row.actor_model = actor_model
        row.actor_usage = dict(actor_usage or {})
        row.actor_duration_seconds = actor_duration_seconds
        row.status = status
        if input_payload is not None:
            row.input_payload = dict(input_payload)
        row.last_error = None
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def ensure_project_owner_review_job(
        self,
        project_id: str,
        *,
        request_item_id: str,
        native_turn_id: str | None,
        title: str,
        content: str,
        user_request: str,
        report_card: dict[str, Any] | None = None,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> ProjectOwnerReview:
        """Persist Owner post-processing before releasing the project turn."""

        existing = self.get_project_owner_review_by_request_item_id(
            project_id, request_item_id
        )
        if existing is not None:
            existing_payload = dict(existing.input_payload or {})
            history_round = None
            history_round_id = str(existing_payload.get("history_round_id") or "")
            if history_round_id:
                history_round = self.session.get(ProjectHistoryRound, history_round_id)
            if native_turn_id and history_round is None:
                existing_card = existing_payload.get("report_card")
                existing_color = (
                    str(existing_card.get("color") or "")
                    if isinstance(existing_card, dict)
                    else ""
                )
                history_round = self.record_project_history_report(
                    project_id,
                    native_turn_id=native_turn_id,
                    title=str(existing_payload.get("title") or title),
                    content=str(existing_payload.get("content") or content),
                    report_ref=request_item_id,
                    report_status=(
                        "failed" if existing_color == "red" else "completed"
                    ),
                    fallback_user_request=str(
                        existing_payload.get("user_request") or user_request
                    ),
                    request_origin=existing.request_origin or request_origin,
                    parent_decision_id=(
                        existing.parent_decision_id or parent_decision_id
                    ),
                )
            if history_round is not None:
                # A restart/recovery is only the transport that resumes a turn.
                # The durable history round remains the source of truth for who
                # authorized the work and which Owner decision it belongs to.
                existing.request_origin = history_round.request_origin
                existing.parent_decision_id = history_round.parent_decision_id
                existing.input_payload = {
                    **existing_payload,
                    "request_origin": history_round.request_origin,
                    "parent_decision_id": history_round.parent_decision_id,
                    "history_round_id": history_round.id,
                }
                existing.updated_at = _now()
                self.session.add(existing)
                self.session.commit()
            return existing
        history_round = None
        if native_turn_id:
            color = (
                str(report_card.get("color") or "")
                if isinstance(report_card, dict)
                else ""
            )
            history_round = self.record_project_history_report(
                project_id,
                native_turn_id=native_turn_id,
                title=title,
                content=content,
                report_ref=request_item_id,
                report_status="failed" if color == "red" else "completed",
                fallback_user_request=user_request,
                request_origin=request_origin,
                parent_decision_id=parent_decision_id,
            )
        effective_request_origin = (
            history_round.request_origin if history_round is not None else request_origin
        )
        effective_parent_decision_id = (
            history_round.parent_decision_id
            if history_round is not None
            else parent_decision_id
        )
        row = ProjectOwnerReview(
            id=generate_id("owner_review"),
            project_id=project_id,
            request_item_id=request_item_id,
            native_turn_id=native_turn_id,
            request_origin=effective_request_origin,
            parent_decision_id=effective_parent_decision_id,
            outcome="pending",
            status="pending",
            input_payload={
                "title": title,
                "content": content,
                "user_request": user_request,
                "report_card": dict(report_card or {}),
                "request_origin": effective_request_origin,
                "parent_decision_id": effective_parent_decision_id,
                "history_round_id": (
                    history_round.id if history_round is not None else None
                ),
            },
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_recoverable_project_owner_reviews(
        self, *, limit: int = 100
    ) -> list[ProjectOwnerReview]:
        return list(
            self.session.exec(
                select(ProjectOwnerReview)
                .where(ProjectOwnerReview.status.in_(["pending", "running"]))
                .order_by(ProjectOwnerReview.created_at)
                .limit(max(1, min(limit, 500)))
            ).all()
        )

    def mark_project_owner_review_running(
        self, review_id: str
    ) -> ProjectOwnerReview | None:
        row = self.session.get(ProjectOwnerReview, review_id)
        if row is None or row.status not in {"pending", "running"}:
            return None
        row.status = "running"
        row.last_error = None
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def fail_project_owner_review(
        self, review_id: str, error: str
    ) -> ProjectOwnerReview | None:
        row = self.session.get(ProjectOwnerReview, review_id)
        if row is None:
            return None
        row.status = "failed"
        row.outcome = "failed"
        row.last_error = str(error)[:1000]
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def supersede_project_owner_review(
        self, review_id: str
    ) -> ProjectOwnerReview | None:
        """Close a stale report review without applying it to newer work."""

        row = self.session.get(ProjectOwnerReview, review_id)
        if row is None:
            return None
        row.status = "superseded"
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def is_project_owner_review_current(self, review_id: str) -> bool:
        """Return whether a report review still describes the latest idle turn."""

        self.session.expire_all()
        row = self.session.get(ProjectOwnerReview, review_id)
        if row is None:
            return False
        # RootAgentSession is the execution source of truth. Project status is
        # a denormalized UI projection and can briefly lag a terminal native
        # event; using it here incorrectly supersedes a valid Owner review.
        root = self.session.exec(
            select(RootAgentSession).where(
                RootAgentSession.project_id == row.project_id
            )
        ).first()
        if root is None:
            project = self.get_project(row.project_id)
            if not (
                row.request_origin == "owner_audit"
                and project is not None
                and project.execution_status == "idle"
            ):
                return False
        elif root.status != "idle":
            return False
        newer = self.session.exec(
            select(ProjectOwnerReview)
            .where(
                ProjectOwnerReview.project_id == row.project_id,
                ProjectOwnerReview.created_at > row.created_at,
            )
            .order_by(ProjectOwnerReview.created_at.desc())
        ).first()
        return newer is None

    def get_project_owner_review_by_request_item_id(
        self, project_id: str, request_item_id: str
    ) -> ProjectOwnerReview | None:
        return self.session.exec(
            select(ProjectOwnerReview)
            .where(
                ProjectOwnerReview.project_id == project_id,
                ProjectOwnerReview.request_item_id == request_item_id,
            )
            .order_by(ProjectOwnerReview.created_at.desc())
        ).first()

    def set_project_owner_review_card(
        self, review_id: str, card_message_id: str
    ) -> ProjectOwnerReview:
        row = self.session.get(ProjectOwnerReview, review_id)
        if row is None:
            raise ValueError(f"Project Owner review not found: {review_id}")
        row.card_message_id = card_message_id
        self.session.add(row)
        self.session.commit()
        return row

    def get_project_owner_state(self, project_id: str) -> ProjectOwnerState | None:
        return self.session.get(ProjectOwnerState, project_id)

    def is_project_owner_enabled(self, project_id: str) -> bool:
        """Return the project-scoped Owner switch; legacy projects default on."""

        row = self.get_project_owner_state(project_id)
        return row is None or row.enabled is not False

    def set_project_owner_enabled(
        self, project_id: str, enabled: bool
    ) -> ProjectOwnerState:
        """Persist the project-scoped Owner switch."""

        self.require_project(project_id)
        row = self.get_project_owner_state(project_id)
        created = row is None
        if row is None:
            row = ProjectOwnerState(
                project_id=project_id,
                owner_open_id=self.get_project_owner_open_id(project_id),
            )
        row.enabled = bool(enabled)
        row.version = 1 if created else max(1, int(row.version or 0) + 1)
        row.updated_at = _now()
        self.session.add(row)
        if not enabled:
            in_flight = list(
                self.session.exec(
                    select(ProjectOwnerReview).where(
                        ProjectOwnerReview.project_id == project_id,
                        ProjectOwnerReview.status.in_(["pending", "running"]),
                    )
                ).all()
            )
            for review in in_flight:
                review.status = "superseded"
                review.updated_at = _now()
                self.session.add(review)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_owner_memories(
        self,
        open_id: str,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[UserOwnerMemory]:
        """Return active global policy plus memories scoped to this project."""

        self._bootstrap_owner_memories(open_id)
        human_id = self.get_human_id(open_id)
        query = select(UserOwnerMemory).where(
            or_(
                UserOwnerMemory.human_id == human_id,
                and_(
                    UserOwnerMemory.human_id == "",
                    UserOwnerMemory.bot_account_id == self.bot_account_id,
                    UserOwnerMemory.open_id == open_id,
                ),
            ),
            UserOwnerMemory.status == "active",
        )
        if project_id:
            query = query.where(
                or_(
                    UserOwnerMemory.scope == "global",
                    UserOwnerMemory.project_id == project_id,
                )
            )
        else:
            query = query.where(UserOwnerMemory.scope == "global")
        query = query.order_by(
            (UserOwnerMemory.strength == "hard").desc(),
            UserOwnerMemory.updated_at.desc(),
        )
        if limit is not None:
            query = query.limit(max(1, limit))
        rows = list(self.session.exec(query).all())
        return rows

    def get_effective_owner_memories(
        self,
        open_id: str,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[UserOwnerMemory]:
        """Return only memories backed by durable human evidence.

        Current Owner observations carry both their source project and exact
        human source refs after validation. Imported or legacy observations
        remain usable only when their quoted evidence can be found verbatim in
        the human-input archive. A narrow legacy Feishu check preserves old
        message-backed memories without promoting agent documents or reports.
        No stored history is deleted or rewritten here.
        """

        self._bootstrap_owner_memories(open_id)
        human_id = self.get_human_id(open_id)
        archived_human_evidence = (
            select(UserOwnerSource.id)
            .where(
                UserOwnerSource.human_id == UserOwnerMemory.human_id,
                UserOwnerSource.kind == "human_input",
                UserOwnerMemory.evidence != "",
                func.instr(UserOwnerSource.content, UserOwnerMemory.evidence) > 0,
            )
            .exists()
        )
        legacy_feishu_evidence = (
            select(FeishuEventLog.id)
            .where(
                FeishuEventLog.bot_account_id == self.bot_account_id,
                FeishuEventLog.open_id == open_id,
                FeishuEventLog.event_type == "message",
                FeishuEventLog.status == "received",
                UserOwnerMemory.source_ref
                == func.printf("feishu_event:%d", FeishuEventLog.id),
                UserOwnerMemory.evidence != "",
                func.instr(FeishuEventLog.text, UserOwnerMemory.evidence) > 0,
            )
            .exists()
        )
        current_validated_observation = and_(
            UserOwnerMemory.source_project_id.is_not(None),
            func.json_array_length(UserOwnerMemory.source_refs) > 0,
        )
        query = select(UserOwnerMemory).where(
            or_(
                UserOwnerMemory.human_id == human_id,
                and_(
                    UserOwnerMemory.human_id == "",
                    UserOwnerMemory.bot_account_id == self.bot_account_id,
                    UserOwnerMemory.open_id == open_id,
                ),
            ),
            UserOwnerMemory.status == "active",
            or_(
                current_validated_observation,
                archived_human_evidence,
                legacy_feishu_evidence,
            ),
        )
        if project_id:
            query = query.where(
                or_(
                    UserOwnerMemory.scope == "global",
                    UserOwnerMemory.project_id == project_id,
                )
            )
        else:
            query = query.where(UserOwnerMemory.scope == "global")
        query = query.order_by(
            (UserOwnerMemory.strength == "hard").desc(),
            UserOwnerMemory.updated_at.desc(),
        )
        if limit is not None:
            query = query.limit(max(1, limit))
        return list(self.session.exec(query).all())

    def add_owner_sources(
        self,
        open_id: str,
        sources: list[dict[str, str]],
    ) -> int:
        """Archive original human inputs for evidence-backed Owner retrieval."""

        human_id = self.get_human_id(open_id)
        refs = {
            str(value)
            for value in self.session.exec(
                select(UserOwnerSource.source_ref).where(
                    UserOwnerSource.human_id == human_id
                )
            ).all()
        }
        added = 0
        for source in sources:
            source_ref = str(source.get("source_ref") or "").strip()[:800]
            content = redact_text(str(source.get("content") or "")).strip()[:20_000]
            if not source_ref or not content or source_ref in refs:
                continue
            self.session.add(
                UserOwnerSource(
                    id=generate_id("osrc"),
                    human_id=human_id,
                    bot_account_id=self.bot_account_id,
                    open_id=open_id,
                    provider=str(source.get("provider") or "unknown")[:40],
                    kind=str(source.get("kind") or "human_input")[:40],
                    source_ref=source_ref,
                    source_time=str(source.get("timestamp") or "")[:80],
                    session_ref=str(source.get("session_ref") or "")[:800],
                    turn_ref=str(source.get("turn_ref") or "")[:800],
                    project_ref=str(source.get("project_ref") or "")[:1_000],
                    context_before=redact_text(
                        str(source.get("context_before") or "")
                    ).strip()[:8_000],
                    context_after=redact_text(
                        str(source.get("context_after") or "")
                    ).strip()[:8_000],
                    content=content,
                )
            )
            refs.add(source_ref)
            added += 1
        if added:
            self.session.commit()
        return added

    def sync_owner_sources(
        self,
        open_id: str,
        sources: list[dict[str, str]],
        *,
        providers: set[str],
    ) -> tuple[int, int]:
        """Replace the scanned providers' archive projection atomically.

        A rescan must remove records that newer provenance filters classify as
        provider-generated.  Sources owned by providers outside this scan are
        left untouched.
        """

        normalized_providers = {
            str(provider).strip()[:40] for provider in providers if str(provider).strip()
        }
        if not normalized_providers:
            return 0, 0
        desired_refs = {
            str(source.get("source_ref") or "").strip()[:800]
            for source in sources
            if str(source.get("provider") or "unknown")[:40]
            in normalized_providers
            and str(source.get("source_ref") or "").strip()
        }
        existing = list(
            self.session.exec(
                select(UserOwnerSource).where(
                    UserOwnerSource.bot_account_id == self.bot_account_id,
                    UserOwnerSource.open_id == open_id,
                    UserOwnerSource.provider.in_(normalized_providers),
                )
            ).all()
        )
        stale_ids = [row.id for row in existing if row.source_ref not in desired_refs]
        if stale_ids:
            self.session.exec(
                delete(UserOwnerSource).where(UserOwnerSource.id.in_(stale_ids))
            )
            self.session.commit()
        added = self.add_owner_sources(open_id, sources)
        return added, len(stale_ids)

    def get_owner_sources(
        self,
        open_id: str,
        *,
        query: str = "",
        provider: str | None = None,
        limit: int = 30,
    ) -> list[UserOwnerSource]:
        """Retrieve raw user history; it is never part of default context."""

        human_id = self.get_human_id(open_id)
        statement = select(UserOwnerSource).where(
            UserOwnerSource.human_id == human_id
        )
        if provider:
            statement = statement.where(UserOwnerSource.provider == provider)
        normalized_query = " ".join(str(query or "").split())[:300]
        if normalized_query:
            statement = statement.where(
                UserOwnerSource.content.contains(normalized_query)
            )
        statement = statement.order_by(
            UserOwnerSource.source_time.desc(),
            UserOwnerSource.created_at.desc(),
        ).limit(max(1, min(limit, 5_000)))
        return list(self.session.exec(statement).all())

    def get_owner_source_refs(self, open_id: str) -> set[str]:
        """Return archived provenance keys for incremental memory audits."""

        human_id = self.get_human_id(open_id)
        return {
            str(value)
            for value in self.session.exec(
                select(UserOwnerSource.source_ref).where(
                    UserOwnerSource.human_id == human_id
                )
            ).all()
        }

    def get_contextual_user_history(
        self,
        open_id: str,
        *,
        query: str = "",
        provider: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return human authority together with its bounded public context."""

        return [
            {
                "provider": row.provider,
                "kind": row.kind,
                "time": row.source_time,
                "session_ref": row.session_ref,
                "turn_ref": row.turn_ref,
                "project_ref": row.project_ref,
                "context_before": row.context_before,
                "human_input": row.content,
                "context_after": row.context_after,
                "source_ref": row.source_ref,
            }
            for row in self.get_owner_sources(
                open_id, query=query, provider=provider, limit=limit
            )
        ]

    def contextual_mac_history_sources(self, open_id: str) -> list[dict[str, str]]:
        """Project MAC's durable human turns into the common history archive.

        Completed ProjectHistoryRound rows are preferred because they retain
        both the instruction and the delivered result.  Feishu event rows fill
        the migration-era gap; message_id provenance prevents a text-equality
        dedupe from discarding separate user actions.
        """

        result: list[dict[str, str]] = []
        represented: Counter[str] = Counter()
        rounds = self.session.exec(
            select(ProjectHistoryRound, Project)
            .join(Project, Project.id == ProjectHistoryRound.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
            )
            .order_by(ProjectHistoryRound.started_at.asc())
        ).all()
        rounds_by_turn = {
            (project.id, history.native_turn_id): history
            for history, project in rounds
            if history.native_turn_id
        }
        previous_result_by_project: dict[str, str] = {}
        for history, project in rounds:
            before = previous_result_by_project.get(project.id, "")
            if history.report_content:
                previous_result_by_project[project.id] = history.report_content[-8_000:]
            for index, item in enumerate(history.user_instructions or [], start=1):
                if not isinstance(item, dict):
                    continue
                origin = str(item.get("origin") or history.request_origin).lower()
                if origin != "user" and not origin.startswith("human"):
                    continue
                actor_open_id = str(item.get("actor_open_id") or "").strip()
                if actor_open_id and actor_open_id != open_id:
                    continue
                content = redact_text(str(item.get("text") or "")).strip()
                if not content:
                    continue
                reply_to = item.get("reply_to")
                reply_context = ""
                if isinstance(reply_to, dict):
                    reply_request = redact_text(
                        str(reply_to.get("request") or "")
                    ).strip()
                    reply_result = redact_text(
                        str(reply_to.get("result") or "")
                    ).strip()
                    if (
                        (not reply_request or not reply_result)
                        and reply_to.get("subject_type") == "root_turn"
                        and reply_to.get("subject_ref")
                    ):
                        replied_round = rounds_by_turn.get(
                            (project.id, str(reply_to["subject_ref"]))
                        )
                        if replied_round is not None:
                            if not reply_request:
                                reply_request = "；".join(
                                    redact_text(str(entry.get("text") or "")).strip()
                                    for entry in (replied_round.user_instructions or [])[-3:]
                                    if isinstance(entry, dict)
                                    and str(entry.get("text") or "").strip()
                                )
                            if not reply_result:
                                reply_result = redact_text(
                                    str(
                                        replied_round.report_content
                                        or replied_round.report_title
                                        or ""
                                    )
                                ).strip()
                    parts = [
                        f"原请求：{reply_request}" if reply_request else "",
                        f"当轮结果：{reply_result}" if reply_result else "",
                    ]
                    reply_context = "；".join(part for part in parts if part)
                    if not reply_context:
                        reply_context = redact_text(
                            str(reply_to.get("summary") or "")
                        ).strip()
                source_ref = f"mac:{history.id}:instruction:{index}"
                result.append(
                    {
                        "provider": "mac",
                        "kind": "human_turn",
                        "source_ref": source_ref,
                        "timestamp": str(item.get("time") or history.started_at.isoformat()),
                        "session_ref": str(history.native_thread_id or project.id),
                        "turn_ref": str(history.native_turn_id or history.id),
                        "project_ref": project.id,
                        "context_before": reply_context or before,
                        "content": content,
                        "context_after": history.report_content[-8_000:],
                    }
                )
                represented[" ".join(content.split()).casefold()] += 1

        choices = self.session.exec(
            select(QuestionAnswer, Question, QuestionInteraction, Project)
            .join(Question, Question.id == QuestionAnswer.question_id)
            .join(QuestionInteraction, QuestionInteraction.id == Question.interaction_id)
            .join(Project, Project.id == QuestionInteraction.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
                QuestionAnswer.answer_source == "user",
                QuestionAnswer.responded_by == open_id,
                QuestionAnswer.skipped == False,  # noqa: E712
            )
            .order_by(QuestionAnswer.responded_at.asc())
        ).all()
        for answer, question, interaction, project in choices:
            content = redact_text(
                str(answer.answer_label or answer.answer or "")
            ).strip()
            if not content:
                continue
            result.append(
                {
                    "provider": "mac",
                    "kind": "human_choice",
                    "source_ref": f"mac:choice:{answer.id}",
                    "timestamp": answer.responded_at.isoformat(),
                    "session_ref": str(interaction.id),
                    "turn_ref": str(interaction.request_item_id or interaction.id),
                    "project_ref": project.id,
                    "context_before": redact_text(question.question)[:8_000],
                    "content": content,
                    "context_after": "",
                }
            )

        events = self.session.exec(
            select(FeishuEventLog)
            .where(
                FeishuEventLog.bot_account_id == self.bot_account_id,
                FeishuEventLog.open_id == open_id,
                FeishuEventLog.event_type == "message",
                FeishuEventLog.status == "received",
                FeishuEventLog.text.is_not(None),
            )
            .order_by(FeishuEventLog.created_at.asc())
        ).all()
        for event in events:
            content = redact_text(str(event.text or "")).strip()
            if not content:
                continue
            # Matching text is used only to avoid duplicating the migration
            # projection of a round already archived above. Equal Feishu
            # messages with different message IDs remain separate events.
            normalized = " ".join(content.split()).casefold()
            if represented[normalized] > 0:
                represented[normalized] -= 1
                continue
            message_ref = str(event.message_id or event.event_id or event.id)
            result.append(
                {
                    "provider": "mac",
                    "kind": "human_input_legacy",
                    "source_ref": f"mac:feishu:{message_ref}",
                    "timestamp": str(event.feishu_created_at or event.created_at),
                    "session_ref": str(event.chat_id or ""),
                    "turn_ref": message_ref,
                    "project_ref": "",
                    "context_before": "",
                    "content": content,
                    "context_after": "",
                }
            )
        return result

    def get_user_instruction_history(
        self,
        open_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return human-authored MAC inputs across this user's projects."""

        rows = self.session.exec(
            select(ProjectHistoryRound, Project)
            .join(Project, Project.id == ProjectHistoryRound.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
            )
            .order_by(ProjectHistoryRound.started_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        result: list[dict[str, Any]] = []
        for history, project in rows:
            for index, item in enumerate(history.user_instructions or [], start=1):
                if not isinstance(item, dict):
                    continue
                origin = str(item.get("origin") or history.request_origin).lower()
                if origin != "user" and not origin.startswith("human"):
                    continue
                actor_open_id = str(item.get("actor_open_id") or "").strip()
                if actor_open_id and actor_open_id != open_id:
                    continue
                content = redact_text(str(item.get("text") or "")).strip()
                if not content:
                    continue
                result.append(
                    {
                        "project": project.name or project.id,
                        "project_id": project.id,
                        "time": str(item.get("time") or history.started_at.isoformat()),
                        "content": content[:20_000],
                        "source_ref": f"{history.id}:instruction:{index}",
                    }
                )
                if len(result) >= max(1, min(limit, 200)):
                    return result
        return result

    def get_project_policies(
        self,
        project_id: str,
        *,
        include_inactive: bool = False,
        limit: int | None = None,
    ) -> list[ProjectPolicy]:
        """Return the durable human-authority ledger for one project."""

        query = select(ProjectPolicy).where(ProjectPolicy.project_id == project_id)
        if not include_inactive:
            query = query.where(ProjectPolicy.status == "active")
        query = query.order_by(ProjectPolicy.updated_at.desc())
        if limit is not None:
            query = query.limit(max(1, limit))
        return list(self.session.exec(query).all())

    def add_project_policies(
        self,
        project_id: str,
        observations: list[dict[str, str]],
        *,
        source_ref: str,
    ) -> list[ProjectPolicy]:
        """Persist verified human rules and supersede referenced rules.

        Evidence validation happens at the isolated Owner boundary, where the
        corresponding human instruction is available. ``replaces`` accepts the
        stable policy id and keeps exact-content support for in-flight responses;
        this persistence layer deliberately performs no fuzzy matching.
        """

        existing = self.get_project_policies(project_id)
        active_by_id = {row.id: row for row in existing}
        active_by_content = {row.content.casefold(): row for row in existing}
        seen = {row.content.casefold() for row in existing}
        added: list[ProjectPolicy] = []
        for observation in observations[:20]:
            content = " ".join(
                str(observation.get("policy") or "").split()
            )[:500]
            evidence = " ".join(
                str(observation.get("evidence") or "").split()
            )[:500]
            observation_ref = (
                str(observation.get("source_ref") or "").strip() or source_ref
            )
            if not content or not evidence or not observation_ref:
                continue
            if content.casefold() in seen:
                continue
            raw_replaces = observation.get("replaces") or []
            if isinstance(raw_replaces, str):
                raw_replaces = [raw_replaces]
            replacement_refs = [
                " ".join(str(value).split())[:500]
                for value in raw_replaces[:20]
                if " ".join(str(value).split())
            ]
            superseded_rows: list[ProjectPolicy] = []
            for replacement_ref in replacement_refs:
                superseded = active_by_id.get(replacement_ref) or active_by_content.get(
                    replacement_ref.casefold()
                )
                if superseded is not None and superseded not in superseded_rows:
                    superseded_rows.append(superseded)
            source_context = observation.get("source_context")
            source_context = (
                dict(source_context) if isinstance(source_context, dict) else {}
            )
            raw_source_refs = observation.get("source_refs") or []
            if isinstance(raw_source_refs, list):
                source_refs = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in raw_source_refs[:10]
                        if str(value).strip()
                    )
                )
            else:
                source_refs = []
            if source_refs and "supporting_sources" not in source_context:
                source_context["supporting_sources"] = [
                    {"source_ref": value} for value in source_refs
                ]
            source_time = None
            raw_source_time = str(observation.get("source_time") or "").strip()
            if raw_source_time:
                try:
                    source_time = datetime.fromisoformat(
                        raw_source_time.replace("Z", "+00:00")
                    )
                    if source_time.tzinfo is not None:
                        source_time = source_time.replace(tzinfo=None)
                except ValueError:
                    source_time = None
            row = ProjectPolicy(
                id=generate_id("policy"),
                project_id=project_id,
                kind=(str(observation.get("kind") or "explicit")[:40]),
                category=(str(observation.get("category") or "general")[:80]),
                content=content,
                strength=(
                    "hard"
                    if str(observation.get("strength") or "default").lower() == "hard"
                    else "default"
                ),
                evidence=evidence,
                source_ref=observation_ref,
                source_actor="human",
                source_time=source_time,
                source_context=source_context,
                supersedes_id=(superseded_rows[0].id if superseded_rows else None),
                supersedes_refs=[item.id for item in superseded_rows],
            )
            for superseded in superseded_rows:
                superseded.status = "superseded"
                superseded.updated_at = _now()
                self.session.add(superseded)
            self.session.add(row)
            added.append(row)
            seen.add(content.casefold())
            active_by_content[content.casefold()] = row
        if added:
            self.session.commit()
            for row in added:
                self.session.refresh(row)
        return added

    def _bootstrap_owner_memories(self, open_id: str) -> None:
        """Import legacy JSON preferences once, preserving their evidence."""

        existing = self.session.exec(
            select(UserOwnerMemory.id)
            .where(
                UserOwnerMemory.bot_account_id == self.bot_account_id,
                UserOwnerMemory.open_id == open_id,
            )
            .limit(1)
        ).first()
        if existing is not None:
            return
        profile = self.get_user_owner_profile(open_id)
        if profile is None:
            return
        added = False
        for item in profile.preferences:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("preference") or "").split())[:500]
            evidence = " ".join(str(item.get("evidence") or "").split())[:500]
            source_ref = str(item.get("source_ref") or "legacy-owner-profile")
            if not content or not evidence:
                continue
            scope = str(item.get("scope") or "global").strip().lower()
            if scope not in {"global", "project"}:
                scope = "global"
            scoped_project = (
                str(item.get("project_id") or "").strip()
                if scope == "project"
                else ""
            )
            # An old explicitly project-scoped record without its project
            # identity cannot safely be promoted to global memory.
            if scope == "project" and not scoped_project:
                continue
            self.session.add(
                UserOwnerMemory(
                    id=generate_id("omem"),
                    human_id=self.get_human_id(open_id),
                    bot_account_id=self.bot_account_id,
                    open_id=open_id,
                    project_id=(scoped_project or None),
                    source_project_id=(
                        str(item.get("project_id") or "").strip() or None
                    ),
                    scope=scope,
                    category=str(item.get("category") or "general")[:80],
                    content=content,
                    strength=(
                        "hard"
                        if str(item.get("strength") or "default").lower() == "hard"
                        else "default"
                    ),
                    evidence=evidence,
                    source_ref=source_ref,
                )
            )
            added = True
        if added:
            self.session.commit()

    def add_owner_memories(
        self,
        open_id: str,
        observations: list[dict[str, str]],
        *,
        project_id: str | None,
        source_ref: str,
    ) -> list[UserOwnerMemory]:
        """Append explicit policy and supersede one referenced memory.

        ``replaces`` accepts the stable memory id and keeps exact-content
        support for in-flight responses. No fuzzy matching occurs here.
        """

        existing = self.get_owner_memories(open_id, project_id=project_id)
        active_by_id = {row.id: row for row in existing}
        active_by_content = {row.content.casefold(): row for row in existing}
        seen = {
            (row.scope, (row.project_id or ""), row.content.casefold()) for row in existing
        }
        added: list[UserOwnerMemory] = []
        for observation in observations[:10]:
            content = " ".join(str(observation.get("preference") or "").split())[:500]
            evidence = " ".join(str(observation.get("evidence") or "").split())[:500]
            scope = str(observation.get("scope") or "global").strip().lower()
            if scope not in {"global", "project"}:
                scope = "project"
            if scope == "project" and not project_id:
                continue
            scoped_project = project_id if scope == "project" else None
            key = (scope, scoped_project or "", content.casefold())
            if not content or not evidence:
                continue
            if key in seen:
                existing_row = next(
                    (
                        row for row in existing
                        if row.scope == scope
                        and (row.project_id or "") == (scoped_project or "")
                        and row.content.casefold() == content.casefold()
                    ),
                    None,
                )
                if existing_row is not None:
                    refs = list(existing_row.source_refs or [existing_row.source_ref])
                    new_refs = observation.get("source_refs") or [source_ref]
                    for ref in new_refs:
                        normalized_ref = str(ref or "").strip()
                        if normalized_ref and normalized_ref not in refs:
                            refs.append(normalized_ref)
                    existing_row.source_refs = refs[-20:]
                    existing_row.reinforcement_count = max(
                        1, int(existing_row.reinforcement_count or 1)
                    ) + 1
                    existing_row.confidence = (
                        "high"
                        if existing_row.reinforcement_count >= 2
                        or str(observation.get("confidence") or "") == "high"
                        else existing_row.confidence
                    )
                    if str(observation.get("learning_type") or "explicit") == "explicit":
                        existing_row.learning_type = "explicit"
                    existing_row.updated_at = _now()
                    self.session.add(existing_row)
                continue
            replaces = " ".join(
                str(observation.get("replaces") or "").split()
            )[:500]
            superseded = (
                active_by_id.get(replaces)
                or active_by_content.get(replaces.casefold())
                if replaces
                else None
            )
            if superseded is not None and (
                superseded.scope != scope
                or (superseded.project_id or "") != (scoped_project or "")
            ):
                # A project correction may not retire a global policy, and a
                # global correction may not erase one project's rule.
                superseded = None
            row = UserOwnerMemory(
                id=generate_id("omem"),
                human_id=self.get_human_id(open_id),
                bot_account_id=self.bot_account_id,
                open_id=open_id,
                project_id=scoped_project,
                source_project_id=project_id,
                scope=scope,
                category=str(observation.get("category") or "general")[:80],
                content=content,
                strength=(
                    "hard"
                    if str(observation.get("strength") or "").lower() == "hard"
                    else "default"
                ),
                confidence=(
                    "high"
                    if str(observation.get("confidence") or "medium") == "high"
                    else "medium"
                ),
                learning_type=(
                    "repeated_choice"
                    if str(observation.get("learning_type") or "explicit")
                    == "repeated_choice"
                    else "explicit"
                ),
                reinforcement_count=max(
                    1, len(observation.get("source_refs") or [source_ref])
                ),
                evidence=evidence,
                source_ref=source_ref,
                source_refs=[
                    str(ref)
                    for ref in (observation.get("source_refs") or [source_ref])
                    if str(ref).strip()
                ][:20],
                supersedes_id=(superseded.id if superseded is not None else None),
            )
            if superseded is not None:
                superseded.status = "superseded"
                superseded.updated_at = _now()
                self.session.add(superseded)
            self.session.add(row)
            added.append(row)
            seen.add(key)
            active_by_content[content.casefold()] = row
        if added or self.session.dirty:
            self.session.commit()
            for row in added:
                self.session.refresh(row)
        return added

    def claim_question_interaction(
        self, interaction_id: str, *, resolution_source: str
    ) -> bool:
        """Atomically reserve a pending question for one responder.

        This is the first-writer-wins boundary between a human card action and
        an isolated Project Owner result. It avoids posting an AI decision for
        a question the user resolved while the Owner was still thinking.
        """

        result = self.session.exec(
            update(QuestionInteraction)
            .where(
                QuestionInteraction.id == interaction_id,
                QuestionInteraction.status == "pending",
            )
            .values(status="resolving", resolution_source=resolution_source)
        )
        self.session.commit()
        return bool(result.rowcount)

    def release_question_interaction(
        self, interaction_id: str, *, resolution_source: str
    ) -> QuestionInteraction | None:
        """Return an unsuccessfully applied responder reservation to pending."""

        interaction = self.get_question_interaction(interaction_id)
        if (
            interaction is not None
            and interaction.status == "resolving"
            and interaction.resolution_source == resolution_source
        ):
            interaction.status = "pending"
            interaction.resolution_source = None
            self.session.add(interaction)
            self.session.commit()
        return interaction

    def complete_question_interaction_claim(
        self,
        interaction_id: str,
        *,
        claimed_by: str,
        resolution_source: str,
        commit: bool = True,
    ) -> bool:
        """Complete only the reservation owned by ``claimed_by``.

        Answer rows and this conditional transition are committed together by
        the caller.  A stale card handler therefore cannot complete an Owner
        reservation, and an Owner result cannot overwrite a human submission.
        """

        result = self.session.exec(
            update(QuestionInteraction)
            .where(
                QuestionInteraction.id == interaction_id,
                QuestionInteraction.status == "resolving",
                QuestionInteraction.resolution_source == claimed_by,
            )
            .values(
                status="answered",
                resolution_source=resolution_source,
                answered_at=_now(),
            )
        )
        if commit:
            self.session.commit()
        return bool(result.rowcount)

    def get_project_owner_open_id(self, project_id: str) -> str | None:
        """Return the human owner used for cross-project preference memory."""

        row = self.session.exec(
            select(ProjectUser).where(
                ProjectUser.project_id == project_id,
                ProjectUser.role == "owner",
            )
        ).first()
        return row.open_id if row is not None else None

    def get_user_choice_history(
        self, open_id: str, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Return bounded, explicit human choices across this user's projects.

        This is preference evidence, not shared project context.  It excludes
        messages, Agent traces, tool output and inferred facts; the isolated
        Project Owner decides whether a past choice is relevant to the current
        decision.
        """

        bounded = max(1, min(limit, 50))
        rows: list[dict[str, Any]] = []
        answers = self.session.exec(
            select(QuestionAnswer, Question, QuestionInteraction, Project)
            .join(Question, Question.id == QuestionAnswer.question_id)
            .join(
                QuestionInteraction,
                QuestionInteraction.id == Question.interaction_id,
            )
            .join(Project, Project.id == QuestionInteraction.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
                QuestionAnswer.answer_source == "user",
                QuestionAnswer.responded_by == open_id,
                QuestionAnswer.skipped == False,  # noqa: E712
            )
            .order_by(QuestionAnswer.responded_at.desc())
            .limit(bounded)
        ).all()
        for answer, question, _interaction, project in answers:
            value = str(answer.answer_label or answer.answer or "").strip()
            if not value:
                continue
            rows.append(
                {
                    "time": answer.responded_at.isoformat(timespec="seconds"),
                    "project": project.name or project.id,
                    "kind": "user_choice",
                    "question": question.question,
                    "choice": value,
                    "source_ref": answer.id,
                }
            )

        adjustments = self.session.exec(
            select(ProjectDecision, Project)
            .join(Project, Project.id == ProjectDecision.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
                ProjectDecision.user_override.is_not(None),
            )
            .order_by(ProjectDecision.overridden_at.desc())
            .limit(bounded)
        ).all()
        for decision, project in adjustments:
            override = str(decision.user_override or "").strip()
            if not override:
                continue
            observed_at = decision.overridden_at or decision.updated_at
            rows.append(
                {
                    "time": observed_at.isoformat(timespec="seconds"),
                    "project": project.name or project.id,
                    "kind": "decision_adjustment",
                    "question": decision.question,
                    "choice": override,
                    "source_ref": decision.id,
                }
            )
        rows.sort(key=lambda row: str(row["time"]), reverse=True)
        return rows[:bounded]

    def get_user_decision_cases(
        self,
        open_id: str,
        *,
        project_id: str | None = None,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Project existing human choices into query-time decision examples.

        This is not another authority store. User adjustments retain both the
        rejected Owner decision and the human correction, allowing later
        turns to use them as counterexamples. Cross-project rows expose only
        the public decision context, never project reports or hidden traces.
        """

        bounded = max(1, min(limit, 200))
        answer_query = (
            select(QuestionAnswer, Question, QuestionInteraction, Project)
            .join(Question, Question.id == QuestionAnswer.question_id)
            .join(
                QuestionInteraction,
                QuestionInteraction.id == Question.interaction_id,
            )
            .join(Project, Project.id == QuestionInteraction.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
                QuestionAnswer.answer_source == "user",
                QuestionAnswer.responded_by == open_id,
                QuestionAnswer.skipped == False,  # noqa: E712
            )
            .order_by(QuestionAnswer.responded_at.desc())
            .limit(bounded)
        )
        if before is not None:
            answer_query = answer_query.where(QuestionAnswer.responded_at < before)

        rows: list[dict[str, Any]] = []
        for answer, question, interaction, project in self.session.exec(
            answer_query
        ).all():
            user_choice = str(answer.answer_label or answer.answer or "").strip()
            if not user_choice:
                continue
            rows.append(
                {
                    "time": answer.responded_at.isoformat(timespec="microseconds"),
                    "project": project.name or project.id,
                    "project_id": project.id,
                    "kind": "user_choice",
                    "question": question.question,
                    "options": self._public_decision_options(question.options),
                    "context": self._public_question_choice_context(question, interaction),
                    "responded_by": answer.responded_by,
                    "source": "native_question",
                    "user_choice": user_choice,
                    "rejected_owner_decision": "",
                    "is_correction": False,
                    "source_ref": answer.id,
                    "authority_class": "explicit_human_choice",
                }
            )

        adjustment_query = (
            select(ProjectDecision, Project)
            .join(Project, Project.id == ProjectDecision.project_id)
            .join(ProjectUser, ProjectUser.project_id == Project.id)
            .where(
                ProjectUser.open_id == open_id,
                Project.bot_account_id == self.bot_account_id,
                ProjectDecision.user_override.is_not(None),
            )
            .order_by(ProjectDecision.overridden_at.desc())
            .limit(bounded)
        )
        if before is not None:
            adjustment_query = adjustment_query.where(
                ProjectDecision.overridden_at < before
            )
        decision_rows = self.session.exec(adjustment_query).all()
        choice_events = self._owner_choice_events(
            [decision for decision, _project in decision_rows]
        )
        for decision, project in decision_rows:
            choice_event = choice_events.get(decision.id)
            if choice_event and choice_event.get("responded_by") != open_id:
                continue
            user_choice = str(decision.user_override or "").strip()
            if not user_choice:
                continue
            observed_at = decision.overridden_at or decision.updated_at
            is_correction = (
                choice_event.get("choice_kind", "correction") == "correction"
                if choice_event else decision.status != "resolved_by_user"
            )
            context = (
                choice_event.get("context") if choice_event else None
            ) or self._public_owner_choice_context(decision)
            if decision.project_id != project_id:
                context = {
                    key: value for key, value in context.items()
                    if key not in {"source_request", "human_context", "source_report"}
                }
            rows.append(
                {
                    "time": observed_at.isoformat(timespec="microseconds"),
                    "project": project.name or project.id,
                    "project_id": project.id,
                    "kind": "owner_correction" if is_correction else "user_choice",
                    "question": decision.question,
                    "options": (
                        choice_event["options"] if choice_event
                        else self._public_decision_options(decision.options, owner_card=True)
                    ),
                    "context": context,
                    "responded_by": choice_event.get("responded_by") if choice_event else None,
                    "source": "project_owner_decision",
                    "choice_action": choice_event.get("choice_action") if choice_event else None,
                    "user_choice": user_choice,
                    "rejected_owner_decision": (
                        str(decision.decision or "")[:500] if is_correction else ""
                    ),
                    "is_correction": is_correction,
                    "source_ref": decision.id,
                    "authority_class": (
                        "explicit_human_correction" if is_correction else "explicit_human_choice"
                    ),
                }
            )
        rows.sort(key=lambda row: str(row["time"]), reverse=True)
        return rows[:bounded]

    def _owner_choice_events(self, decisions: list[ProjectDecision]) -> dict[str, dict[str, Any]]:
        decision_ids = [decision.id for decision in decisions]
        if not decision_ids:
            return {}
        events = self.session.exec(
            select(EventLog)
            .where(
                EventLog.message == "User choice recorded",
                EventLog.meta["decision_id"].as_string().in_(decision_ids),
            )
            .order_by(EventLog.created_at.desc(), EventLog.id.desc())
        ).all()
        choices: dict[str, dict[str, Any]] = {}
        for event in events:
            choices.setdefault(str(event.meta["decision_id"]), dict(event.meta))
        return choices

    @staticmethod
    def _public_owner_choice_context(decision: ProjectDecision) -> dict[str, Any]:
        payload = decision.answer_payload or {}
        return redact_secrets({
            "decision_type": decision.decision_type,
            "recommendation": decision.decision,
            "rationale": decision.rationale,
            "impact": decision.impact,
            **{
                key: payload[key]
                for key in ("source_request", "human_context", "source_report")
                if key in payload
            },
            "context_refs": list(decision.context_refs),
        })

    @staticmethod
    def _public_question_choice_context(
        question: Question, interaction: QuestionInteraction,
    ) -> dict[str, Any]:
        return redact_secrets({
            "reason": question.reason,
            "default_assumption": question.default_assumption,
            "recommended_option": question.recommended_option,
            "stage": interaction.stage,
            "request_item_id": interaction.request_item_id,
        })

    @staticmethod
    def _public_decision_options(
        options: Any, *, owner_card: bool = False,
    ) -> list[dict[str, str]]:
        """Keep only option text a human saw, never callback payloads."""

        return [
            {
                "label": (
                    str(option.get("label") or "").strip()[:80]
                    if owner_card else str(option.get("label") or option.get("key") or "")
                ),
                "description": (
                    str(option.get("description") or "").strip()[:240]
                    if owner_card else str(option.get("description") or "")
                ),
            }
            for option in (options or [])
            if isinstance(option, dict)
            and str(option.get("label") or option.get("key") or "").strip()
        ]

    def get_project_user_choice_history(
        self,
        project_id: str,
        *,
        open_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return explicit human choices that are authoritative in one project."""

        query = (
            select(QuestionAnswer, Question, QuestionInteraction)
            .join(Question, Question.id == QuestionAnswer.question_id)
            .join(
                QuestionInteraction,
                QuestionInteraction.id == Question.interaction_id,
            )
            .where(
                QuestionInteraction.project_id == project_id,
                QuestionAnswer.answer_source == "user",
                QuestionAnswer.skipped == False,  # noqa: E712
            )
            .order_by(QuestionAnswer.responded_at.desc())
        )
        if open_id:
            query = query.where(QuestionAnswer.responded_by == open_id)
        if limit is not None:
            query = query.limit(max(1, limit))
        rows: list[dict[str, Any]] = []
        for answer, question, interaction in self.session.exec(query).all():
            value = str(answer.answer_label or answer.answer or "").strip()
            if not value:
                continue
            rows.append(
                {
                    "time": answer.responded_at.isoformat(timespec="seconds"),
                    "kind": "user_choice",
                    "question": question.question,
                    "choice": value,
                    "options": self._public_decision_options(question.options),
                    "context": self._public_question_choice_context(question, interaction),
                    "responded_by": answer.responded_by,
                    "source": "native_question",
                    "source_ref": answer.id,
                }
            )

        adjustment_query = select(ProjectDecision).where(
            ProjectDecision.project_id == project_id,
            ProjectDecision.user_override.is_not(None),
        ).order_by(ProjectDecision.overridden_at.desc())
        if limit is not None:
            adjustment_query = adjustment_query.limit(max(1, limit))
        decisions = self.session.exec(adjustment_query).all()
        choice_events = self._owner_choice_events(decisions)
        for decision in decisions:
            choice_event = choice_events.get(decision.id)
            if open_id and choice_event and choice_event.get("responded_by") != open_id:
                continue
            override = str(decision.user_override or "").strip()
            if not override:
                continue
            observed_at = decision.overridden_at or decision.updated_at
            is_correction = (
                choice_event.get("choice_kind", "correction") == "correction"
                if choice_event else decision.status != "resolved_by_user"
            )
            rows.append(
                {
                    "time": observed_at.isoformat(timespec="seconds"),
                    "kind": "decision_adjustment" if is_correction else "user_choice",
                    "question": decision.question,
                    "choice": override,
                    "options": (
                        choice_event["options"] if choice_event
                        else self._public_decision_options(decision.options, owner_card=True)
                    ),
                    "context": (
                        choice_event.get("context") if choice_event else None
                    ) or self._public_owner_choice_context(decision),
                    "responded_by": choice_event.get("responded_by") if choice_event else None,
                    "source": "project_owner_decision",
                    "choice_action": choice_event.get("choice_action") if choice_event else None,
                    "source_ref": decision.id,
                }
            )
        rows.sort(key=lambda row: str(row["time"]), reverse=True)
        return rows[: max(1, limit)] if limit is not None else rows

    def get_user_owner_profile(self, open_id: str) -> UserOwnerProfile | None:
        row = self.session.get(UserOwnerProfile, self._scoped_open_id(open_id))
        if row is None or row.bot_account_id != self.bot_account_id:
            return None
        return row

    def add_explicit_user_preferences(
        self,
        open_id: str,
        observations: list[dict[str, str]],
        *,
        project_id: str,
        source_ref: str,
    ) -> UserOwnerProfile:
        """Append bounded user-stated preference evidence without inference."""

        row = self.get_user_owner_profile(open_id)
        created = row is None
        if row is None:
            row = UserOwnerProfile(
                open_id=self._scoped_open_id(open_id),
                bot_account_id=self.bot_account_id,
            )
        existing = [dict(item) for item in row.preferences if isinstance(item, dict)]
        seen = {
            " ".join(str(item.get("preference") or "").split()).casefold()
            for item in existing
        }
        observed_at = _now().isoformat(timespec="seconds")
        for observation in observations[:10]:
            preference = " ".join(
                str(observation.get("preference") or "").split()
            )[:500]
            evidence = " ".join(str(observation.get("evidence") or "").split())[:500]
            normalized = preference.casefold()
            if not preference or not evidence or normalized in seen:
                continue
            existing.append(
                {
                    "preference": preference,
                    "evidence": evidence,
                    "category": " ".join(
                        str(observation.get("category") or "general").split()
                    )[:80],
                    "scope": (
                        "global"
                        if str(observation.get("scope") or "global").lower()
                        == "global"
                        else "project"
                    ),
                    "strength": (
                        "hard"
                        if str(observation.get("strength") or "default").lower()
                        == "hard"
                        else "default"
                    ),
                    "time": observed_at,
                    "project_id": project_id,
                    "source_ref": source_ref,
                }
            )
            seen.add(normalized)
        row.preferences = existing[-30:]
        row.version = 1 if created else max(1, int(row.version or 0) + 1)
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    # Native request_user_input persistence -----------------------------------

    async def create_approval(
        self,
        project_id: str,
        task_id: str | None,
        state: str,
        approval_type: str,
        question: str,
        options: list[dict[str, Any]],
        *,
        payload: dict[str, Any] | None = None,
        enforce_current_state: bool = True,
    ) -> Approval:
        del enforce_current_state
        approval = Approval(
            id=generate_id("approval"),
            project_id=project_id,
            task_id=task_id,
            state=state,
            approval_type=approval_type,
            question=question,
            payload={**(payload or {}), "options": options},
        )
        self.session.add(approval)
        self.session.commit()
        self.session.refresh(approval)
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
        approval = self.get_approval(approval_id)
        if approval.status == "pending":
            approval.status = "responded"
            approval.response = response
            approval.responded_by = responded_by
            approval.responded_at = _now()
            self.session.add(approval)
            if commit:
                self.session.commit()
        return approval

    async def create_question_interaction(
        self,
        project_id: str,
        origin_task_id: str | None,
        origin_round: int,
        resume_state: str,
        agent: str,
        stage: str,
        batch_key: str,
        questions: list[dict[str, Any]],
        *,
        approval_id: str | None = None,
        request_item_id: str | None = None,
        requested_to: str = "user",
        escalated_to_user: bool = True,
        commit: bool = True,
    ) -> QuestionInteraction:
        interaction = QuestionInteraction(
            id=generate_id("qint"),
            project_id=project_id,
            origin_task_id=origin_task_id,
            origin_round=origin_round,
            resume_state=resume_state,
            agent=agent,
            stage=stage,
            batch_key=batch_key,
            approval_id=approval_id,
            request_item_id=request_item_id,
            requested_to=requested_to,
            escalated_to_user=escalated_to_user,
        )
        self.session.add(interaction)
        self.session.flush()
        for index, raw in enumerate(questions):
            options = list(raw.get("options") or [])
            recommended = raw.get("recommended_option")
            if recommended is None and options:
                recommended = options[0].get("key")
            self.session.add(
                Question(
                    id=generate_id("q"),
                    interaction_id=interaction.id,
                    question_id=str(raw.get("question_id") or generate_id("question")),
                    decision_key=raw.get("decision_key"),
                    kind=str(raw.get("kind") or "decision"),
                    question=str(raw.get("question") or ""),
                    reason=str(raw.get("reason") or ""),
                    allow_free_text=bool(raw.get("allow_free_text")),
                    options=options,
                    multiple=bool(raw.get("multiple")),
                    default_assumption=raw.get("default_assumption"),
                    recommended_option=recommended,
                    affected_trace_ids=list(raw.get("affected_trace_ids") or []),
                    sensitivity=str(raw.get("sensitivity") or "internal"),
                    secret_env_var=raw.get("secret_env_var"),
                    order_index=index,
                )
            )
        if commit:
            self.session.commit()
        return interaction

    def set_question_selections(
        self, interaction_id: str, selected_options: dict[str, str]
    ) -> QuestionInteraction:
        interaction = self.get_question_interaction(interaction_id)
        if interaction is None:
            raise ValueError(f"Question interaction not found: {interaction_id}")
        interaction.selected_options = dict(selected_options)
        self.session.add(interaction)
        self.session.commit()
        return interaction

    def get_question_interaction(
        self, interaction_id: str
    ) -> QuestionInteraction | None:
        return self.session.get(QuestionInteraction, interaction_id)

    def get_question_interaction_by_request_item_id(
        self, project_id: str, request_item_id: str
    ) -> QuestionInteraction | None:
        return self.session.exec(
            select(QuestionInteraction)
            .where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.request_item_id == request_item_id,
            )
            .order_by(QuestionInteraction.created_at.desc())
        ).first()

    def get_pending_question_interaction(
        self, project_id: str
    ) -> QuestionInteraction | None:
        """Return the latest unresolved native interaction for one project."""

        return self.session.exec(
            select(QuestionInteraction)
            .where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.status == "pending",
            )
            .order_by(QuestionInteraction.created_at.desc())
        ).first()

    def get_questions_for_interaction(self, interaction_id: str) -> list[Question]:
        return list(
            self.session.exec(
                select(Question)
                .where(Question.interaction_id == interaction_id)
                .order_by(Question.order_index)
            ).all()
        )

    def get_question_answer(self, question_id: str) -> QuestionAnswer | None:
        return self.session.exec(
            select(QuestionAnswer)
            .where(QuestionAnswer.question_id == question_id)
            .order_by(QuestionAnswer.responded_at.desc())
        ).first()

    async def submit_question_answer(
        self,
        question_db_id: str,
        answer: str | None,
        answer_label: str | None,
        skipped: bool,
        responded_by: str,
        answer_source: str = "user",
        source_refs: list[str] | None = None,
        commit: bool = True,
        keep_pending: bool = False,
    ) -> QuestionAnswer:
        question = self.session.get(Question, question_db_id)
        if question is None:
            raise ValueError(f"Question not found: {question_db_id}")
        stored_answer = answer
        stored_label = answer_label
        if question.sensitivity == "secret" and question.secret_env_var and answer:
            interaction = self.get_question_interaction(question.interaction_id)
            if interaction:
                reference = self.secret_store.set(
                    interaction.project_id, question.secret_env_var, answer
                )
                stored_answer = f"secret_ref:{reference}"
                stored_label = "已安全保存"
        row = QuestionAnswer(
            id=generate_id("qa"),
            question_id=question.id,
            answer=stored_answer,
            answer_label=stored_label,
            skipped=skipped,
            answer_source=answer_source,
            source_refs=list(source_refs or []),
            responded_by=responded_by,
        )
        self.session.add(row)
        if answer_source == "user":
            interaction = self.get_question_interaction(question.interaction_id)
            if interaction is not None:
                self.session.add(
                    EventLog(
                        project_id=interaction.project_id,
                        level="INFO",
                        message="User choice recorded",
                        meta={
                            "kind": "user_choice",
                            "source": "native_question",
                            "interaction_id": interaction.id,
                            "question_id": question.id,
                            "question": question.question,
                            "options": self._public_decision_options(question.options),
                            "context": self._public_question_choice_context(question, interaction),
                            "choice": stored_label or stored_answer or "",
                            "skipped": bool(skipped),
                            "responded_by": responded_by,
                            "answer_ref": row.id,
                        },
                    )
                )
        question.status = "pending" if keep_pending else ("skipped" if skipped else "answered")
        self.session.add(question)
        if commit:
            self.session.commit()
        return row

    def interaction_is_resolvable(self, interaction_id: str) -> bool:
        return all(
            question.status != "pending"
            for question in self.get_questions_for_interaction(interaction_id)
            if question.kind in {"blocking", "decision"}
        )

    async def resolve_question_interaction(
        self,
        interaction_id: str,
        commit: bool = True,
        resolution_source: str = "user",
    ) -> QuestionInteraction | None:
        interaction = self.get_question_interaction(interaction_id)
        if interaction is None:
            return None
        interaction.status = "answered"
        interaction.answered_at = _now()
        interaction.resolution_source = resolution_source
        self.session.add(interaction)
        if commit:
            self.session.commit()
        return interaction

    # Durable notification projection ----------------------------------------

    def enqueue_notification(
        self,
        *,
        dedupe_key: str,
        recipient_id: str,
        recipient_type: str,
        intent_type: str,
        payload: dict[str, Any],
        project_id: str | None = None,
        conversation_id: str | None = None,
        bot_account_id: str | None = None,
    ) -> NotificationOutbox:
        existing = self.session.exec(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == dedupe_key)
        ).first()
        if existing:
            return existing
        account_id = str(bot_account_id or self.bot_account_id)
        if project_id:
            project = self.get_project(project_id)
            if project is not None:
                account_id = project.bot_account_id
        row = NotificationOutbox(
            id=generate_id("notification"),
            bot_account_id=account_id,
            project_id=project_id,
            conversation_id=conversation_id,
            recipient_id=recipient_id,
            recipient_type=recipient_type,
            intent_type=intent_type,
            dedupe_key=dedupe_key,
            payload=dict(payload),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def claim_notification(self, notification_id: str) -> NotificationOutbox | None:
        row = self.session.get(NotificationOutbox, notification_id)
        if row is None or row.status not in {"pending", "failed"}:
            return None
        row.status = "sending"
        row.attempt_count += 1
        row.last_error = None
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def finish_notification(
        self,
        notification_id: str,
        *,
        provider_message_id: str | None = None,
        error: str | None = None,
        retryable: bool = True,
    ) -> NotificationOutbox:
        row = self.session.get(NotificationOutbox, notification_id)
        if row is None:
            raise ValueError(notification_id)
        row.status = (
            ("failed" if retryable else "terminal_failed")
            if error is not None
            else "sent"
        )
        row.provider_message_id = provider_message_id or row.provider_message_id
        row.last_error = error
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def prune_sent_progress_notifications(
        self,
        *,
        projection_key: str | None = None,
        keep_notification_id: str | None = None,
    ) -> int:
        """Remove redundant sent snapshots while preserving durable UI state.

        ``CardProjection`` is the current-state source of truth and retryable
        outbox rows are never touched. Report, question, approval and decision
        cards remain an immutable public audit trail.
        """

        statement = select(NotificationOutbox).where(
            NotificationOutbox.intent_type == "progress",
            NotificationOutbox.status == "sent",
        )
        if projection_key is not None:
            statement = statement.where(
                func.json_extract(
                    NotificationOutbox.payload, "$.projection_key"
                )
                == projection_key
            )
        rows = list(self.session.exec(statement).all())
        by_projection: dict[str, list[NotificationOutbox]] = {}
        for row in rows:
            key = str((row.payload or {}).get("projection_key") or "").strip()
            if not key or (projection_key is not None and key != projection_key):
                continue
            by_projection.setdefault(key, []).append(row)
        removed = 0
        for grouped in by_projection.values():
            grouped.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
            keep_id = keep_notification_id or grouped[0].id
            for row in grouped:
                if row.id == keep_id:
                    continue
                self.session.delete(row)
                removed += 1
        if removed:
            self.session.commit()
        return removed

    def recover_inflight_notifications(
        self,
        *,
        older_than_seconds: float | None = None,
    ) -> dict[str, int]:
        """Close delivery claims orphaned by restart or an expired send.

        A PATCH against a known Feishu message is safe to retry.  A POST that
        may already have reached Feishu is deliberately not replayed because
        the provider offers no idempotency key and doing so can create a
        duplicate card.  Both outcomes leave an explicit durable status rather
        than an immortal ``sending`` row.  With no age threshold this is the
        startup recovery path; the live outbox uses the same semantics for a
        claim whose sender never returned.
        """

        recovered = 0
        ambiguous = 0
        statement = select(NotificationOutbox).where(
            NotificationOutbox.status == "sending"
        )
        if older_than_seconds is not None:
            statement = statement.where(
                NotificationOutbox.updated_at
                <= _now() - timedelta(seconds=max(1, older_than_seconds))
            )
        rows = list(self.session.exec(statement).all())
        recovery_reason = (
            "delivery claim expired"
            if older_than_seconds is not None
            else "service restarted"
        )
        for row in rows:
            payload = dict(row.payload or {})
            update_message_id = str(payload.get("update_message_id") or "").strip()
            if not update_message_id:
                projection_key = str(payload.get("projection_key") or "").strip()
                projection = (
                    self.get_card_projection(projection_key)
                    if projection_key
                    else None
                )
                if projection is not None and projection.provider_message_id:
                    update_message_id = projection.provider_message_id
                    payload["update_message_id"] = update_message_id
                    row.payload = payload
            row.updated_at = _now() - timedelta(seconds=10)
            if update_message_id:
                row.status = "failed"
                row.last_error = (
                    f"{recovery_reason} during a retryable card update"
                )
                recovered += 1
            else:
                row.status = "terminal_failed"
                row.last_error = (
                    f"{recovery_reason} during a card post; "
                    "delivery outcome is ambiguous"
                )
                ambiguous += 1
            self.session.add(row)
        if rows:
            self.session.commit()
        return {"retryable_updates": recovered, "ambiguous_posts": ambiguous}

    def list_retryable_notifications(
        self,
        *,
        max_attempts: int = 6,
        min_age_seconds: float = 5,
        max_age_seconds: float = 3600,
        limit: int = 20,
    ) -> list[NotificationOutbox]:
        newest = _now() - timedelta(seconds=min_age_seconds)
        oldest = _now() - timedelta(seconds=max_age_seconds)
        return list(
            self.session.exec(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(["pending", "failed"]),
                    NotificationOutbox.attempt_count < max_attempts,
                    NotificationOutbox.updated_at <= newest,
                    NotificationOutbox.updated_at >= oldest,
                )
                .order_by(NotificationOutbox.updated_at)
                .limit(limit)
            ).all()
        )

    def supersede_projection_notifications(
        self,
        projection_keys: set[str],
        *,
        reason: str,
    ) -> int:
        """Prevent stale pending card updates from overwriting a newer state."""

        if not projection_keys:
            return 0
        changed = 0
        rows = self.session.exec(
            select(NotificationOutbox).where(
                NotificationOutbox.status.in_(["pending", "failed"])
            )
        ).all()
        for row in rows:
            if (row.payload or {}).get("projection_key") not in projection_keys:
                continue
            row.status = "terminal_failed"
            row.last_error = reason
            row.updated_at = _now()
            self.session.add(row)
            changed += 1
        if changed:
            self.session.commit()
        return changed

    def upsert_card_projection(
        self,
        *,
        projection_key: str,
        subject_type: str,
        subject_id: str,
        card_kind: str,
        provider_message_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        lifecycle_status: str = "published",
        selected_action: str | None = None,
        view_state: str | None = None,
        resolved_by: str | None = None,
        resulting_state: str | None = None,
        last_error: str | None = None,
    ) -> CardProjection:
        row = self.session.exec(
            select(CardProjection).where(CardProjection.projection_key == projection_key)
        ).first()
        if row is None:
            row = CardProjection(
                id=generate_id("card"),
                projection_key=projection_key,
                project_id=project_id,
                conversation_id=conversation_id,
                subject_type=subject_type,
                subject_id=subject_id,
                card_kind=card_kind,
            )
        else:
            row.projection_version += 1
        row.provider_message_id = provider_message_id or row.provider_message_id
        row.lifecycle_status = lifecycle_status
        if selected_action is not None:
            row.selected_action = selected_action
        if view_state is not None:
            row.view_state = view_state
        if resolved_by is not None:
            row.resolved_by = resolved_by
        row.resolved_at = _now() if lifecycle_status.startswith("resolved") else row.resolved_at
        row.resulting_state = resulting_state
        row.last_error = last_error
        row.updated_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def get_card_projection(self, projection_key: str) -> CardProjection | None:
        return self.session.exec(
            select(CardProjection).where(CardProjection.projection_key == projection_key)
        ).first()

    def get_card_projection_by_message_id(
        self, provider_message_id: str
    ) -> CardProjection | None:
        """Resolve a Feishu reply target to its durable card projection."""

        if not provider_message_id:
            return None
        return self.session.exec(
            select(CardProjection)
            .where(CardProjection.provider_message_id == provider_message_id)
            .order_by(CardProjection.updated_at.desc())
        ).first()

    def get_card_visible_summary(self, provider_message_id: str) -> str | None:
        """Return the bounded public summary already rendered on a card."""

        if not provider_message_id:
            return None
        notification = self.session.exec(
            select(NotificationOutbox)
            .where(NotificationOutbox.provider_message_id == provider_message_id)
            .order_by(NotificationOutbox.updated_at.desc())
        ).first()
        if notification is None:
            return None
        card = (notification.payload or {}).get("card")
        if not isinstance(card, dict):
            return None
        config = card.get("config")
        summary = config.get("summary") if isinstance(config, dict) else None
        content = summary.get("content") if isinstance(summary, dict) else None
        if not isinstance(content, str):
            return None
        compact = " ".join(content.split())
        return compact[:500] or None

    def get_project_card_projections(self, project_id: str) -> list[CardProjection]:
        return list(
            self.session.exec(
                select(CardProjection)
                .where(CardProjection.project_id == project_id)
                .order_by(CardProjection.updated_at.desc())
            ).all()
        )

    # Event logs ---------------------------------------------------------------

    async def log_event(
        self,
        project_id: str,
        level: str,
        message: str,
        meta: dict[str, Any] | None = None,
    ) -> EventLog:
        row = EventLog(
            project_id=project_id,
            level=level,
            message=message,
            meta=dict(meta or {}),
        )
        self.session.add(row)
        return row

    def get_project_events(
        self, project_id: str, limit: int | None = None
    ) -> list[EventLog]:
        statement = (
            select(EventLog)
            .where(EventLog.project_id == project_id)
            .order_by(EventLog.created_at.desc())
        )
        if limit:
            statement = statement.limit(limit)
        return list(self.session.exec(statement).all())

    def get_latest_turn_profile(self, project_id: str) -> dict[str, Any] | None:
        row = self.session.exec(
            select(EventLog)
            .where(
                EventLog.project_id == project_id,
                EventLog.message == "Root turn performance",
            )
            .order_by(EventLog.created_at.desc())
        ).first()
        return dict(row.meta) if row is not None and isinstance(row.meta, dict) else None

    def is_event_processed(self, event_id: str) -> bool:
        return bool(
            event_id
            and self.session.get(ProcessedEvent, self._scoped_event_id(event_id))
        )

    def mark_event_processed(self, event_id: str, event_type: str) -> None:
        if event_id:
            self.session.merge(
                ProcessedEvent(
                    event_id=self._scoped_event_id(event_id),
                    event_type=event_type,
                )
            )
            # Event ownership must be durable before the handler crosses an
            # async boundary (turn/steer, card delivery, or a native tool
            # response). Leaving this INSERT pending lets SQLAlchemy autoflush
            # it on the next read and hold SQLite's single writer lock for the
            # whole network/model await, blocking Root event projection and
            # external AgentSession creation.
            self.session.commit()

    def log_feishu_event(
        self,
        *,
        open_id: str,
        chat_id: str | None,
        chat_type: str,
        event_type: str,
        event_id: str | None,
        message_id: str | None,
        text: str | None,
        action: str | None,
        feishu_created_at: datetime | None,
        status: str,
        detail: str | None = None,
    ) -> None:
        self.session.add(
            FeishuEventLog(
                bot_account_id=self.bot_account_id,
                open_id=open_id,
                chat_id=chat_id,
                chat_type=chat_type,
                event_type=event_type,
                event_id=event_id,
                message_id=message_id,
                text=text[:2000] if text else None,
                action=action,
                feishu_created_at=feishu_created_at,
                status=status,
                detail=detail[:1000] if detail else None,
            )
        )
        self.session.commit()

    # Explicit command/input continuations -----------------------------------

    def _pending_intent_key(self, open_id: str, chat_id: str | None) -> str:
        """Keep prompt continuations isolated without requiring a schema rewrite.

        ``pending_intents.open_id`` predates chat-scoped project selection and is
        the table primary key in existing installations.  Encoding the chat in
        that key lets old databases support one pending prompt per chat/user
        immediately; legacy rows remain readable with ``chat_id=None``.
        """

        base = f"{open_id}\x1f{chat_id}" if chat_id else open_id
        return (
            base
            if self.bot_account_id == "default"
            else f"{self.bot_account_id}\x1f{base}"
        )

    def get_pending_intent(
        self, open_id: str, chat_id: str | None = None
    ) -> PendingIntent | None:
        row = self.session.get(
            PendingIntent, self._pending_intent_key(open_id, chat_id)
        )
        if row is None and chat_id is not None and self.bot_account_id == "default":
            legacy = self.session.get(PendingIntent, open_id)
            if legacy is not None and legacy.chat_id in {None, chat_id}:
                row = legacy
        if row and row.created_at < _now() - timedelta(hours=1):
            self.session.delete(row)
            self.session.commit()
            return None
        return row

    def upsert_pending_intent(
        self,
        open_id: str,
        description: str | None,
        stage: str,
        chat_id: str | None = None,
        chat_type: str = "p2p",
    ) -> PendingIntent:
        key = self._pending_intent_key(open_id, chat_id)
        row = self.session.get(PendingIntent, key) or PendingIntent(
            open_id=key, stage=stage
        )
        row.description = description
        row.stage = stage
        row.chat_id = chat_id
        row.chat_type = chat_type
        row.created_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def delete_pending_intent(
        self, open_id: str, chat_id: str | None = None
    ) -> None:
        row = self.session.get(
            PendingIntent, self._pending_intent_key(open_id, chat_id)
        )
        if row is None and chat_id is not None and self.bot_account_id == "default":
            legacy = self.session.get(PendingIntent, open_id)
            if legacy is not None and legacy.chat_id in {None, chat_id}:
                row = legacy
        if row:
            self.session.delete(row)
            self.session.commit()
