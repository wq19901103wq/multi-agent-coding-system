"""Persistence and project-directory services for the thin MAC host.

This module deliberately contains no workflow, Agent role, prompt, retry, or
state-transition policy.  Codex owns turn execution and context management.
"""

from __future__ import annotations

import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
    ProjectUser,
    Question,
    QuestionAnswer,
    QuestionInteraction,
    Task,
)


def generate_id(prefix: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}_{secrets.token_hex(8)}"


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
    def __init__(self, session: Session):
        self.session = session
        self.settings = get_settings()
        database = str(getattr(session.get_bind().url, "database", "") or "")
        secret_path = (
            None
            if database in {"", ":memory:"}
            else self.settings.secret_store_path.resolve()
        )
        self.secret_store = LocalSecretStore(secret_path)

    # Projects and directories -------------------------------------------------

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
        initial_context: dict | None = None,
    ) -> Project:
        project_id = generate_id("proj")
        if bound_directory is None and feishu_chat_id:
            binding = self.get_chat_directory_binding(feishu_chat_id)
            bound_directory = binding.directory if binding else None
        if bound_directory:
            workspace = self._prepare_bound_directory(bound_directory)
        else:
            workspace = (self.settings.projects_dir / project_id).resolve()
            workspace.mkdir(parents=True, exist_ok=False)
            (workspace / ".gitignore").write_text(".mac/\n", encoding="utf-8")
            self._initialize_repository(workspace, [".gitignore"])
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
        approval = self.session.exec(
            select(Approval)
            .where(Approval.project_id == project.id, Approval.status == "pending")
            .order_by(Approval.created_at.desc())
        ).first()
        return {
            "project_id": project.id,
            "name": project.name,
            "status": project.status,
            "current_state": project.current_state,
            "display": project.execution_status,
            "execution_status": project.execution_status,
            "waiting_reason": project.waiting_reason,
            "workspace_path": project.workspace_path,
            "bound_directory": project.bound_directory,
            "pending_approval": (
                {
                    "approval_id": approval.id,
                    "type": approval.approval_type,
                    "question": approval.question,
                    "payload": approval.payload,
                }
                if approval
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
        if project.status == "paused":
            project.status = "active"
            project.updated_at = _now()
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
        project.execution_status = "idle"
        project.waiting_reason = None
        project.updated_at = _now()
        for approval in self.session.exec(
            select(Approval).where(
                Approval.project_id == project.id, Approval.status == "pending"
            )
        ).all():
            approval.status = "expired"
            self.session.add(approval)
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
        binding = self.session.get(ChatDirectoryBinding, chat_id)
        if binding is None:
            binding = ChatDirectoryBinding(
                chat_id=chat_id,
                directory=str(path),
                bound_by=bound_by,
            )
        else:
            binding.directory = str(path)
            binding.bound_by = bound_by
            binding.updated_at = _now()
        self.session.add(binding)
        for context in self.session.exec(
            select(ChatUserContext).where(ChatUserContext.chat_id == chat_id)
        ).all():
            context.active_project_id = None
            context.updated_at = _now()
            self.session.add(context)
        self.session.commit()
        return binding

    def get_chat_directory_binding(self, chat_id: str) -> ChatDirectoryBinding | None:
        return self.session.get(ChatDirectoryBinding, chat_id)

    def unbind_directory_from_chat(self, chat_id: str) -> bool:
        binding = self.session.get(ChatDirectoryBinding, chat_id)
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
                ProjectChat.project_id == project_id, ProjectChat.chat_id == chat_id
            )
        ).first()
        if existing:
            return existing
        row = ProjectChat(
            id=generate_id("pch"),
            project_id=project_id,
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
        context = self.session.get(ChatUserContext, (chat_id, open_id))
        if context is None:
            context = ChatUserContext(
                chat_id=chat_id,
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
        context = self.session.get(ChatUserContext, (chat_id, open_id))
        if context is None or not context.active_project_id:
            return None
        project = self.get_project(context.active_project_id)
        return project if project and project.status != "cancelled" else None

    def get_chat_user_active_project(
        self, chat_id: str, open_id: str
    ) -> Project | None:
        project = self.get_chat_user_selected_project(chat_id, open_id)
        return project if project and project.status in {"active", "paused"} else None

    def list_projects_for_chat(self, chat_id: str) -> list[Project]:
        links = self.session.exec(
            select(ProjectChat).where(ProjectChat.chat_id == chat_id)
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
        context = self.session.get(ChatUserContext, (chat_id, open_id))
        return context.last_event_at if context else None

    def update_last_event_at(
        self, chat_id: str, open_id: str, event_at: datetime
    ) -> None:
        context = self.session.get(ChatUserContext, (chat_id, open_id))
        if context is None:
            context = ChatUserContext(
                chat_id=chat_id,
                open_id=open_id,
                active_project_id=None,
            )
        if context.last_event_at is None or event_at > context.last_event_at:
            context.last_event_at = event_at
            context.updated_at = _now()
        self.session.add(context)
        self.session.commit()

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

    def get_question_interaction(
        self, interaction_id: str
    ) -> QuestionInteraction | None:
        return self.session.get(QuestionInteraction, interaction_id)

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
    ) -> NotificationOutbox:
        existing = self.session.exec(
            select(NotificationOutbox).where(NotificationOutbox.dedupe_key == dedupe_key)
        ).first()
        if existing:
            return existing
        row = NotificationOutbox(
            id=generate_id("notification"),
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
        row.selected_action = selected_action
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

    def is_event_processed(self, event_id: str) -> bool:
        return bool(event_id and self.session.get(ProcessedEvent, event_id))

    def mark_event_processed(self, event_id: str, event_type: str) -> None:
        if event_id:
            self.session.merge(ProcessedEvent(event_id=event_id, event_type=event_type))

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

    def get_pending_intent(self, open_id: str) -> PendingIntent | None:
        row = self.session.get(PendingIntent, open_id)
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
        row = self.session.get(PendingIntent, open_id) or PendingIntent(
            open_id=open_id, stage=stage
        )
        row.description = description
        row.stage = stage
        row.chat_id = chat_id
        row.chat_type = chat_type
        row.created_at = _now()
        self.session.add(row)
        self.session.commit()
        return row

    def delete_pending_intent(self, open_id: str) -> None:
        row = self.session.get(PendingIntent, open_id)
        if row:
            self.session.delete(row)
            self.session.commit()
