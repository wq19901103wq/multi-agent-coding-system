import json
import hashlib
import re
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select, update

from mac_system.config import (
    get_agent_config,
    get_settings,
    path_within_allowed_bind_roots,
)
from mac_system.models import (
    AcceptanceEvidence,
    Approval,
    CardProjection,
    ChatDirectoryBinding,
    ChatUserContext,
    ConversationInteraction,
    ConversationSession,
    CoordinatorAgentSession,
    CoordinatorSession,
    DefaultAuthorization,
    EventLog,
    FeedbackPacket,
    FeishuEventLog,
    NotificationOutbox,
    PendingIntent,
    PendingConversationAction,
    PendingUserInput,
    ProcessedEvent,
    Project,
    ProjectChat,
    ProjectContextRecord,
    ProjectUser,
    Question,
    QuestionAnswer,
    QuestionInteraction,
    Task,
    TaskFailureReport,
)
from mac_system.utils.redaction import redact_secrets, redact_text
from mac_system.state_machine import StateMachine
from mac_system.core.project_paths import project_mac_dir
from mac_system.core.secret_store import LocalSecretStore
from mac_system.core.delivery_policy import compile_delivery_policy
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.core.work_item import WorkItemCompiler


def generate_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    # Four hex digits produced birthday collisions during the full test suite
    # when several approvals were created in the same second.  Keep the
    # readable timestamp while using 64 random bits for process-safe IDs.
    rand = secrets.token_hex(8)
    return f"{prefix}_{ts}_{rand}"


def _now() -> datetime:
    return datetime.utcnow()


def _iteration_acceptance_contract(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a confirmed conversational brief into stable, testable AC records.

    The fast iteration profile deliberately skips the three planning Agents.
    It must therefore persist the Coordinator's already-confirmed contract in
    the same structured shape that Tester, Reviewer and Acceptance consume.
    This transformation is mechanical: it assigns stable IDs and conservative
    verification metadata, but does not infer requirements from prose.
    """
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(brief.get("acceptance_criteria") or [], start=1):
        if isinstance(raw, dict):
            statement = str(raw.get("statement") or "").strip()
            if not statement:
                continue
            item = dict(raw)
            item.setdefault("id", f"AC-ITER-{index:03d}")
            item.setdefault("requirement_ids", [f"REQ-ITER-{index:03d}"])
            item.setdefault("verification_method", "automated_test")
            item.setdefault("evidence_source", "system_observable")
            normalized.append(item)
            continue
        statement = str(raw or "").strip()
        if not statement:
            continue
        normalized.append(
            {
                "id": f"AC-ITER-{index:03d}",
                "requirement_ids": [f"REQ-ITER-{index:03d}"],
                "statement": statement,
                "verification_method": "automated_test",
                "evidence_source": "system_observable",
                "evidence_required": "从自动化测试和真实用户入口观察结果",
            }
        )
    return normalized


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


class StaleApprovalStateError(Exception):
    """Raised when a concurrent transition has already left an approval state."""


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

    def enqueue_notification(
        self,
        *,
        dedupe_key: str,
        recipient_id: str,
        recipient_type: str,
        intent_type: str,
        payload: dict,
        project_id: str | None = None,
        conversation_id: str | None = None,
    ) -> NotificationOutbox:
        """Persist a notification intent before any provider side effect."""
        existing = self.session.exec(
            select(NotificationOutbox).where(
                NotificationOutbox.dedupe_key == dedupe_key
            )
        ).first()
        if existing is not None:
            return existing
        notification = NotificationOutbox(
            id=generate_id("notification"),
            project_id=project_id,
            conversation_id=conversation_id,
            recipient_id=recipient_id,
            recipient_type=recipient_type,
            intent_type=intent_type,
            dedupe_key=dedupe_key,
            payload=dict(payload),
        )
        self.session.add(notification)
        self.session.commit()
        self.session.refresh(notification)
        return notification

    def claim_notification(self, notification_id: str) -> NotificationOutbox | None:
        notification = self.session.get(NotificationOutbox, notification_id)
        if notification is None or notification.status not in {"pending", "failed"}:
            return None
        notification.status = "sending"
        notification.attempt_count += 1
        notification.last_error = None
        notification.updated_at = _now()
        self.session.add(notification)
        self.session.commit()
        self.session.refresh(notification)
        return notification

    def finish_notification(
        self,
        notification_id: str,
        *,
        provider_message_id: str | None = None,
        error: str | None = None,
        retryable: bool = True,
    ) -> NotificationOutbox:
        notification = self.session.get(NotificationOutbox, notification_id)
        if notification is None:
            raise ValueError(f"Notification not found: {notification_id}")
        # Exception messages are allowed to be empty (for example some
        # transport-level ConnectError instances).  Presence, not truthiness,
        # is the success/failure boundary.
        notification.status = (
            ("failed" if retryable else "terminal_failed")
            if error is not None
            else "sent"
        )
        if provider_message_id:
            notification.provider_message_id = provider_message_id
        notification.last_error = error
        notification.updated_at = _now()
        self.session.add(notification)
        self.session.commit()
        self.session.refresh(notification)
        return notification

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
        """Create or advance the durable Feishu projection of one subject."""
        projection = self.session.exec(
            select(CardProjection).where(
                CardProjection.projection_key == projection_key
            )
        ).first()
        if projection is None and provider_message_id:
            projection = self.session.exec(
                select(CardProjection).where(
                    CardProjection.provider_message_id == provider_message_id
                )
            ).first()
        if projection is None:
            projection = CardProjection(
                id=generate_id("card"),
                projection_key=projection_key,
                project_id=project_id,
                conversation_id=conversation_id,
                subject_type=subject_type,
                subject_id=subject_id,
                card_kind=card_kind,
                provider_message_id=provider_message_id,
                lifecycle_status=lifecycle_status,
                selected_action=selected_action,
                resolved_by=resolved_by,
                resolved_at=_now() if lifecycle_status.startswith("resolved") else None,
                resulting_state=resulting_state,
                last_error=last_error,
            )
        else:
            projection.projection_version += 1
            projection.project_id = project_id or projection.project_id
            projection.conversation_id = conversation_id or projection.conversation_id
            projection.subject_type = subject_type or projection.subject_type
            projection.subject_id = subject_id or projection.subject_id
            projection.card_kind = card_kind or projection.card_kind
            projection.provider_message_id = (
                provider_message_id or projection.provider_message_id
            )
            projection.lifecycle_status = lifecycle_status
            projection.selected_action = selected_action
            projection.resolved_by = resolved_by
            if lifecycle_status.startswith("resolved") and projection.resolved_at is None:
                projection.resolved_at = _now()
            projection.resulting_state = resulting_state
            projection.last_error = last_error
            projection.updated_at = _now()
        self.session.add(projection)
        self.session.commit()
        self.session.refresh(projection)
        return projection

    def get_card_projection(self, projection_key: str) -> CardProjection | None:
        return self.session.exec(
            select(CardProjection).where(
                CardProjection.projection_key == projection_key
            )
        ).first()

    def recover_stuck_notifications(
        self, *, max_age_seconds: int = 300
    ) -> list[NotificationOutbox]:
        """Make abandoned or invalid delivery claims retryable after restart."""
        cutoff = _now() - timedelta(seconds=max_age_seconds)
        stuck = list(
            self.session.exec(
                select(NotificationOutbox).where(
                    NotificationOutbox.status == "sending",
                    NotificationOutbox.updated_at <= cutoff,
                )
            ).all()
        )
        incomplete_sent = list(
            self.session.exec(
                select(NotificationOutbox).where(
                    NotificationOutbox.status == "sent",
                    NotificationOutbox.provider_message_id.is_(None),
                )
            ).all()
        )
        seen = {notification.id for notification in stuck}
        stuck.extend(
            notification
            for notification in incomplete_sent
            if notification.id not in seen
        )
        for notification in stuck:
            notification.status = "failed"
            notification.last_error = (
                notification.last_error
                or "delivery interrupted before provider acknowledgement"
            )
            notification.updated_at = _now()
            self.session.add(notification)
        if stuck:
            self.session.commit()
        return stuck

    def list_retryable_notifications(
        self,
        *,
        max_attempts: int = 6,
        min_age_seconds: float = 5.0,
        max_age_seconds: float = 3600.0,
        limit: int = 20,
    ) -> list[NotificationOutbox]:
        """Return durable delivery intents eligible for another bounded attempt.

        Both never-claimed ``pending`` rows and explicitly ``failed`` rows are
        retryable.  ``sending`` rows are deliberately excluded: startup
        recovery is responsible for turning abandoned claims back into failed
        rows, while a live sender keeps exclusive ownership of its claim.
        """
        cutoff = _now() - timedelta(seconds=min_age_seconds)
        oldest = _now() - timedelta(seconds=max_age_seconds)
        return list(
            self.session.exec(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(["pending", "failed"]),
                    NotificationOutbox.attempt_count < max_attempts,
                    NotificationOutbox.updated_at <= cutoff,
                    NotificationOutbox.updated_at >= oldest,
                )
                .order_by(NotificationOutbox.updated_at.asc())
                .limit(limit)
            ).all()
        )

    def get_or_create_conversation_session(self, chat_id: str) -> ConversationSession:
        conversation = self.session.exec(
            select(ConversationSession).where(ConversationSession.chat_id == chat_id)
        ).first()
        if conversation is not None:
            return conversation
        conversation = ConversationSession(
            id=generate_id("conv"),
            chat_id=chat_id,
        )
        self.session.add(conversation)
        self.session.commit()
        self.session.refresh(conversation)
        return conversation

    def append_conversation_turn(
        self,
        chat_id: str,
        *,
        role: str,
        content: str,
        open_id: str | None = None,
        message_id: str | None = None,
        max_turns: int = 20,
    ) -> ConversationSession:
        conversation = self.get_or_create_conversation_session(chat_id)
        turns = list(conversation.recent_turns or [])
        turns.append(
            {
                "role": role,
                "content": redact_text(content),
                "open_id": open_id,
                "message_id": message_id,
                "created_at": _now().isoformat(),
            }
        )
        conversation.recent_turns = turns[-max_turns:]
        conversation.version += 1
        conversation.updated_at = _now()
        self.session.add(conversation)
        self.session.commit()
        self.session.refresh(conversation)
        return conversation

    def update_conversation_session(
        self,
        conversation: ConversationSession,
        *,
        summary: str | None = None,
        latest_artifact: dict[str, Any] | None = None,
        active_project_id: str | None = None,
        update_active_project: bool = False,
    ) -> ConversationSession:
        if summary is not None:
            conversation.summary = redact_text(summary)
        if latest_artifact is not None:
            conversation.latest_artifact = redact_secrets(latest_artifact)
        if update_active_project:
            conversation.active_project_id = active_project_id
        conversation.version += 1
        conversation.updated_at = _now()
        self.session.add(conversation)
        self.session.commit()
        self.session.refresh(conversation)
        return conversation

    def create_pending_conversation_action(
        self,
        *,
        session_id: str,
        action_type: str,
        payload: dict[str, Any],
        source_message_ids: list[str],
        idempotency_key: str,
        created_by: str | None,
        target_project_id: str | None = None,
        expire_existing: bool = True,
    ) -> PendingConversationAction:
        existing = self.session.exec(
            select(PendingConversationAction).where(
                PendingConversationAction.idempotency_key == idempotency_key
            )
        ).first()
        if existing is not None:
            return existing

        if expire_existing:
            pending = self.session.exec(
                select(PendingConversationAction).where(
                    PendingConversationAction.session_id == session_id,
                    PendingConversationAction.status == "pending",
                )
            ).all()
            for item in pending:
                item.status = "expired"
                item.resolved_at = _now()
                self.session.add(item)

        action = PendingConversationAction(
            id=generate_id("cact"),
            session_id=session_id,
            action_type=action_type,
            target_project_id=target_project_id,
            payload=payload,
            source_message_ids=source_message_ids,
            idempotency_key=idempotency_key,
            created_by=created_by,
            expires_at=_now() + timedelta(hours=24),
        )
        self.session.add(action)
        conversation = self.session.get(ConversationSession, session_id)
        if conversation is not None:
            conversation.pending_action_id = action.id
            conversation.version += 1
            conversation.updated_at = _now()
            self.session.add(conversation)
        self.session.commit()
        self.session.refresh(action)
        return action

    def get_pending_conversation_action(
        self, action_id: str
    ) -> PendingConversationAction | None:
        action = self.session.get(PendingConversationAction, action_id)
        if action is None or action.status != "pending":
            return None
        if action.expires_at is not None and action.expires_at <= _now():
            action.status = "expired"
            action.resolved_at = _now()
            self.session.add(action)
            self.session.commit()
            return None
        return action

    def get_session_pending_actions(
        self, session_id: str
    ) -> list[PendingConversationAction]:
        actions = list(
            self.session.exec(
                select(PendingConversationAction)
                .where(
                    PendingConversationAction.session_id == session_id,
                    PendingConversationAction.status == "pending",
                )
                .order_by(PendingConversationAction.created_at.desc())
            ).all()
        )
        now = _now()
        active: list[PendingConversationAction] = []
        changed = False
        for action in actions:
            if action.expires_at is not None and action.expires_at <= now:
                action.status = "expired"
                action.resolved_at = now
                self.session.add(action)
                changed = True
            else:
                active.append(action)
        if changed:
            self.session.commit()
        return active

    def resolve_pending_conversation_action(
        self,
        action_id: str,
        *,
        decision: str,
        resolved_by: str,
    ) -> PendingConversationAction:
        action = self.session.get(PendingConversationAction, action_id)
        if action is None:
            raise ValueError("待确认动作不存在")
        if action.status != "pending":
            return action
        action.status = "resolved" if decision == "confirm" else "rejected"
        action.resolved_by = resolved_by
        action.resolved_at = _now()
        self.session.add(action)
        conversation = self.session.get(ConversationSession, action.session_id)
        if conversation is not None and conversation.pending_action_id == action.id:
            conversation.pending_action_id = None
            conversation.version += 1
            conversation.updated_at = _now()
            self.session.add(conversation)
        self.session.commit()
        self.session.refresh(action)
        return action

    def claim_pending_conversation_action(
        self, action_id: str, *, claimed_by: str
    ) -> PendingConversationAction | None:
        """Atomically claim a pending action before executing its side effect."""
        result = self.session.exec(
            update(PendingConversationAction)
            .where(
                PendingConversationAction.id == action_id,
                PendingConversationAction.status == "pending",
            )
            .values(status="processing", resolved_by=claimed_by)
        )
        self.session.commit()
        if getattr(result, "rowcount", 0) != 1:
            return None
        return self.session.get(PendingConversationAction, action_id)

    def finish_pending_conversation_action(
        self,
        action_id: str,
        *,
        status: str,
        target_project_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> PendingConversationAction:
        if status not in {"resolved", "rejected", "pending"}:
            raise ValueError(f"Unsupported pending action status: {status}")
        action = self.session.get(PendingConversationAction, action_id)
        if action is None:
            raise ValueError("待确认动作不存在")
        action.status = status
        action.target_project_id = target_project_id or action.target_project_id
        if payload is not None:
            action.payload = payload
        action.resolved_at = _now() if status in {"resolved", "rejected"} else None
        if status == "pending":
            action.resolved_by = None
        self.session.add(action)
        conversation = self.session.get(ConversationSession, action.session_id)
        if conversation is not None:
            conversation.pending_action_id = action.id if status == "pending" else None
            if target_project_id:
                conversation.active_project_id = target_project_id
            conversation.version += 1
            conversation.updated_at = _now()
            self.session.add(conversation)
        self.session.commit()
        self.session.refresh(action)
        return action

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
        if task_type == "iteration":
            candidate_brief = (initial_context or {}).get("confirmed_brief")
            if not isinstance(candidate_brief, dict):
                raise ValueError("快速迭代需要结构化 confirmed_brief")
            if not _iteration_acceptance_contract(candidate_brief):
                raise ValueError("快速迭代至少需要一条明确验收标准")
        if bound_directory is None and feishu_chat_id:
            binding = self.get_chat_directory_binding(feishu_chat_id)
            bound_directory = binding.directory if binding else None
        if task_type == "iteration" and not bound_directory:
            raise ValueError("快速迭代只能用于已绑定的 Git 仓库")

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

        project_context = dict(initial_context or {})
        confirmed_brief = project_context.get("confirmed_brief")
        if task_type == "iteration":
            if not isinstance(confirmed_brief, dict):
                raise ValueError("快速迭代需要结构化 confirmed_brief")
            confirmed_brief = dict(confirmed_brief)
            iteration_criteria = _iteration_acceptance_contract(confirmed_brief)
            if not iteration_criteria:
                raise ValueError("快速迭代至少需要一条明确验收标准")
            confirmed_brief["acceptance_criteria"] = iteration_criteria
            project_context["confirmed_brief"] = confirmed_brief
        raw_policy = (
            confirmed_brief.get("delivery_policy")
            if isinstance(confirmed_brief, dict)
            else None
        )
        project_context["delivery_policy"] = compile_delivery_policy(
            task_type,
            raw_policy if isinstance(raw_policy, dict) else None,
            reference_mode=(
                str(confirmed_brief.get("reference_mode") or "none")
                if isinstance(confirmed_brief, dict)
                else "none"
            ),
        )
        project_context.update({
            "task_type": task_type,
            "description": description,
            "approval_mode": approval_mode,
            "retry_counts": {},
        })

        uses_agent_loop = project_context.get("orchestration_mode") == "agent_loop"
        project = Project(
            id=project_id,
            name=name,
            task_type=task_type,
            description=description,
            status="active",
            current_state="COORDINATING" if uses_agent_loop else "INIT",
            previous_state=None,
            workspace_path=str(workspace_path),
            bound_directory=str(workspace_path) if bound_directory else None,
            context=project_context,
            approval_config=approval_config,
            feishu_chat_id=feishu_chat_id,
            feishu_chat_type=feishu_chat_type,
        )
        self.session.add(project)
        self.session.flush()

        confirmed_brief = project_context.get("confirmed_brief")
        if isinstance(confirmed_brief, dict):
            self.seed_confirmed_brief_context(
                project_id,
                confirmed_brief,
                source_type="user",
                source_ref=confirmed_brief.get("source_session_id"),
                commit=False,
            )
            if task_type == "iteration":
                artifact_path = project_mac_dir(project) / "ITERATION_BRIEF.json"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    json.dumps(
                        {
                            "artifact_id": f"iteration-brief-{project.id}",
                            "status": "confirmed",
                            "objective": confirmed_brief.get("objective") or description,
                            "scope": list(confirmed_brief.get("scope") or []),
                            "out_of_scope": list(
                                confirmed_brief.get("out_of_scope") or []
                            ),
                            "acceptance_criteria": list(
                                confirmed_brief.get("acceptance_criteria") or []
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self.ensure_iteration_specs(project, confirmed_brief)

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

        if uses_agent_loop:
            project.execution_status = "coordinating"
            project.updated_at = _now()
            self.session.add(project)
        else:
            await self.transition_state(project_id, "start", triggered_by="system")

        self.session.commit()
        return project

    def ensure_iteration_specs(
        self,
        project: Project,
        confirmed_brief: dict[str, Any] | None = None,
    ) -> Path | None:
        """Compile the minimal traceability artifact skipped by fast planning.

        Iterations intentionally do not run a Spec Agent. Tester evidence still
        requires stable spec IDs, so the deterministic service compiles one
        mapping per already-confirmed acceptance criterion. It never infers
        requirements from prose. When the bound repository already has a
        confirmed planning artifact, the iteration entries are appended with
        project-scoped IDs instead of discarding either contract.
        """
        if project.task_type != "iteration":
            return None
        path = project_mac_dir(project) / "SPECS.json"
        brief = dict(
            confirmed_brief
            or (project.context or {}).get("confirmed_brief")
            or {}
        )
        criteria = [
            item
            for item in brief.get("acceptance_criteria") or []
            if isinstance(item, dict) and item.get("id")
        ]
        if not criteria:
            return None
        decision_ids = [
            str(item.get("key"))
            for item in brief.get("decisions") or []
            if isinstance(item, dict) and item.get("key")
        ]
        method_types = {
            "integration_test": "integration",
            "e2e": "e2e",
            "manual": "manual",
        }
        project_scope = project.id.rsplit("_", 1)[-1][:8].upper()
        spec_prefix = f"SPEC-ITER-{project_scope}"
        test_prefix = f"TC-ITER-{project_scope}"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("specs"), list):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        if any(
            isinstance(item, dict)
            and str(item.get("id") or "").startswith(f"{spec_prefix}-")
            for item in existing.get("specs", [])
        ):
            return path
        specs: list[dict[str, Any]] = []
        for index, criterion in enumerate(criteria, start=1):
            ac_id = str(criterion["id"])
            statement = str(criterion.get("statement") or ac_id)
            requirement_ids = [
                str(value)
                for value in criterion.get("requirement_ids") or []
                if value
            ]
            if not requirement_ids:
                # Structured Coordinator output should always provide these.
                # Keep a stable contract reference rather than parsing prose.
                requirement_ids = [f"REQ-ITER-{index:03d}"]
            test_type = method_types.get(
                str(criterion.get("verification_method") or ""), "unit"
            )
            specs.append({
                "id": f"{spec_prefix}-{index:03d}",
                "title": statement,
                "description": statement,
                "requirement_ids": requirement_ids,
                "acceptance_criterion_ids": [ac_id],
                "decision_ids": decision_ids,
                "acceptance_criteria": [statement],
                "dependencies": [],
                "owners": {
                    "implementation": "coder",
                    "verification": "tester",
                    "review": "reviewer",
                },
                "test_cases": [{
                    "id": f"{test_prefix}-{index:03d}",
                    "type": test_type,
                    "scenario": statement,
                    "expected": statement,
                    "acceptance_criterion_ids": [ac_id],
                }],
                "definition_of_done": [statement],
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        combined_specs = [
            item for item in existing.get("specs", []) if isinstance(item, dict)
        ] + specs
        artifact = dict(existing)
        artifact.update({
            "artifact_id": "specs",
            "version": max(1, int(existing.get("version") or 0) + 1),
            "round": max(1, int(existing.get("round") or 0) + 1),
            "status": "confirmed",
            "content_hash": None,
            "specs": combined_specs,
        })
        path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

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
        if not path_within_allowed_bind_roots(path):
            raise ValueError(
                "绑定目录不在 MAC_ALLOWED_BIND_ROOTS 允许范围内；请先配置允许的根目录"
            )
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
        # Directory binding changes the chat's project namespace. Any cached
        # project selection belongs to the previous namespace and must not be
        # reused by the next Coordinator turn. This does not alter the project
        # itself or selections in its original chat.
        for conversation in self.session.exec(
            select(ConversationSession).where(
                ConversationSession.chat_id == chat_id
            )
        ).all():
            conversation.active_project_id = None
            conversation.updated_at = now
            self.session.add(conversation)
        for user_context in self.session.exec(
            select(ChatUserContext).where(ChatUserContext.chat_id == chat_id)
        ).all():
            user_context.active_project_id = None
            user_context.updated_at = now
            self.session.add(user_context)
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

        Idempotent: repeated calls preserve cancelled state and also clean up
        any late task that raced with the original cancellation.
        """
        project = self.require_project(project_id)
        was_cancelled = project.status == "cancelled"

        # Always clean up newly observed running tasks. This closes a race
        # where an already in-flight handler created a retry just as the first
        # cancellation transaction committed.
        running_tasks = self._running_tasks(project_id)
        for task in running_tasks:
            task.status = "cancelled"
            task.cancellation_reason = reason or "Project cancelled"
            task.error_message = reason or "Project cancelled"
        if was_cancelled:
            self.session.commit()
            return project

        # Mark running tasks as failed so they are not recovered later.
        # Expire pending approvals.
        for approval in self._pending_approvals(project_id):
            approval.status = "expired"

        project.status = "cancelled"
        project.execution_status = "idle"
        project.waiting_reason = None
        project.waiting_since = None
        project.updated_at = _now()

        # Clear conversation sessions still pointing to this project so the
        # coordinator no longer treats a cancelled project as active.
        for conv in self.session.exec(
            select(ConversationSession).where(
                ConversationSession.active_project_id == project_id
            )
        ).all():
            conv.active_project_id = None
            conv.pending_action_id = None
            self.session.add(conv)

        # Expire any pending conversation actions tied to this project.
        for action in self.session.exec(
            select(PendingConversationAction).where(
                PendingConversationAction.target_project_id == project_id,
                PendingConversationAction.status == "pending",
            )
        ).all():
            action.status = "expired"
            action.resolved_at = _now()
            self.session.add(action)

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
            message="Project cancelled",
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

    def set_execution_status(
        self,
        project_id: str,
        execution_status: str,
        *,
        reason: str | None = None,
        commit: bool = True,
    ) -> Project:
        """Update the posture of an otherwise active project.

        Lifecycle status remains ``active`` while the project waits.  This
        prevents normal routing/binding code from treating a reversible wait as
        a terminal pause, while giving schedulers a deterministic signal not to
        rerun an Agent until new evidence arrives.
        """
        project = self.require_project(project_id)
        project.execution_status = execution_status
        if execution_status in {"running", "coordinating", "running_agent", "idle"}:
            project.waiting_reason = None
            project.waiting_since = None
        else:
            project.waiting_reason = reason
            project.waiting_since = project.waiting_since or _now()
        project.updated_at = _now()
        self.session.add(project)
        if commit:
            self.session.commit()
        return project

    # ------------------------------------------------------------------
    # Acceptance evidence ledger
    # ------------------------------------------------------------------

    def record_acceptance_evidence(
        self,
        project_id: str,
        acceptance_criterion_id: str,
        *,
        verdict: str,
        verification_method: str,
        evidence_scope: str,
        source_type: str,
        source_ref: str | None = None,
        artifact_revision: str = "",
        environment: dict[str, Any] | None = None,
        evidence: list[Any] | None = None,
        limitations: list[Any] | None = None,
        observed_at: datetime | None = None,
        commit: bool = True,
    ) -> AcceptanceEvidence:
        """Append one criterion verdict and supersede its previous active row."""
        self.require_project(project_id)
        rows = list(
            self.session.exec(
                select(AcceptanceEvidence).where(
                    AcceptanceEvidence.project_id == project_id,
                    AcceptanceEvidence.acceptance_criterion_id
                    == acceptance_criterion_id,
                )
            ).all()
        )
        active = next((row for row in rows if row.status == "active"), None)
        normalized_environment = dict(environment or {})
        normalized_evidence = list(evidence or [])
        normalized_limitations = list(limitations or [])
        if (
            active is not None
            and active.verdict == verdict
            and active.verification_method == verification_method
            and active.evidence_scope == evidence_scope
            and active.source_type == source_type
            and active.source_ref == source_ref
            and active.artifact_revision == artifact_revision
            and active.environment == normalized_environment
            and active.evidence == normalized_evidence
            and active.limitations == normalized_limitations
        ):
            return active
        version = max((row.version for row in rows), default=0) + 1
        now = _now()
        if active is not None:
            active.status = "superseded"
            active.valid_until = now
            active.updated_at = now
            self.session.add(active)
        row = AcceptanceEvidence(
            id=generate_id("acev"),
            project_id=project_id,
            acceptance_criterion_id=acceptance_criterion_id,
            version=version,
            verdict=verdict,
            verification_method=verification_method,
            evidence_scope=evidence_scope,
            environment=normalized_environment,
            evidence=normalized_evidence,
            limitations=normalized_limitations,
            source_type=source_type,
            source_ref=source_ref,
            artifact_revision=artifact_revision,
            status="active",
            supersedes_id=active.id if active is not None else None,
            observed_at=observed_at or now,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        if commit:
            self.session.commit()
            self.session.refresh(row)
        return row

    def get_active_acceptance_evidence(
        self, project_id: str
    ) -> dict[str, AcceptanceEvidence]:
        self.require_project(project_id)
        rows = self.session.exec(
            select(AcceptanceEvidence).where(
                AcceptanceEvidence.project_id == project_id,
                AcceptanceEvidence.status == "active",
            )
        ).all()
        return {row.acceptance_criterion_id: row for row in rows}

    def invalidate_acceptance_evidence(
        self,
        project_id: str,
        *,
        reason: str,
        criterion_ids: set[str] | None = None,
        commit: bool = True,
    ) -> int:
        """Mark evidence stale when the artifact it describes has changed."""
        statement = select(AcceptanceEvidence).where(
            AcceptanceEvidence.project_id == project_id,
            AcceptanceEvidence.status == "active",
        )
        if criterion_ids:
            statement = statement.where(
                AcceptanceEvidence.acceptance_criterion_id.in_(criterion_ids)
            )
        rows = list(self.session.exec(statement).all())
        now = _now()
        for row in rows:
            row.status = "stale"
            row.valid_until = now
            row.updated_at = now
            row.limitations = [*list(row.limitations or []), reason]
            self.session.add(row)
        if rows and commit:
            self.session.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Versioned project context
    # ------------------------------------------------------------------

    def create_context_record(
        self,
        project_id: str,
        *,
        record_type: str,
        record_key: str,
        value: dict[str, Any],
        source_type: str = "system",
        source_ref: str | None = None,
        scope_type: str = "project",
        scope_ref: str | None = None,
        confidence: str = "confirmed",
        sensitivity: str = "internal",
        replace_active: bool = True,
        commit: bool = True,
    ) -> ProjectContextRecord:
        """Append a context record and optionally supersede the active version.

        Records are immutable in meaning: a correction creates a new version and
        preserves the previous value for audit and stale-work detection.
        """
        project = self.require_project(project_id)
        history = list(
            self.session.exec(
                select(ProjectContextRecord).where(
                    ProjectContextRecord.project_id == project_id,
                    ProjectContextRecord.record_key == record_key,
                )
            ).all()
        )
        active = next((item for item in history if item.status == "active"), None)
        version = max((item.version for item in history), default=0) + 1
        now = _now()

        if active is not None and replace_active:
            active.status = "superseded"
            active.valid_until = now
            active.updated_at = now
            self.session.add(active)

        record = ProjectContextRecord(
            id=generate_id("ctx"),
            project_id=project_id,
            record_type=record_type,
            record_key=record_key,
            value=value,
            version=version,
            source_type=source_type,
            source_ref=source_ref,
            scope_type=scope_type,
            scope_ref=scope_ref,
            confidence=confidence,
            sensitivity=sensitivity,
            supersedes_id=active.id if active is not None and replace_active else None,
            valid_from=now,
            created_at=now,
            updated_at=now,
        )
        self.session.add(record)
        project.context_version += 1
        project.updated_at = now
        self.session.add(project)
        if commit:
            self.session.commit()
            self.session.refresh(record)
        return record

    def get_active_context_records(
        self,
        project_id: str,
        *,
        record_types: set[str] | None = None,
        agent_type: str | None = None,
        state: str | None = None,
    ) -> list[ProjectContextRecord]:
        self.require_project(project_id)
        statement = select(ProjectContextRecord).where(
            ProjectContextRecord.project_id == project_id,
            ProjectContextRecord.status == "active",
        )
        if record_types:
            statement = statement.where(ProjectContextRecord.record_type.in_(record_types))
        records = list(
            self.session.exec(
                statement.order_by(ProjectContextRecord.record_key.asc())
            ).all()
        )
        if agent_type is None and state is None:
            return records
        relevant: list[ProjectContextRecord] = []
        for record in records:
            scope_type = record.scope_type or "project"
            scope_ref = str(record.scope_ref or "")
            if scope_type == "agent" and scope_ref != str(agent_type or ""):
                continue
            if scope_type in {"stage", "work_item"} and scope_ref not in {
                str(state or ""),
                str(agent_type or ""),
                f"{agent_type}:{state}",
            }:
                continue
            if scope_type == "session":
                continue
            if scope_type == "acceptance_criterion" and agent_type not in {
                "tester",
                "reviewer",
                "acceptance_agent",
                "acceptance_evaluator",
                "conversation_coordinator",
            }:
                continue
            relevant.append(record)
        return relevant

    def get_context_record_history(
        self, project_id: str, record_key: str
    ) -> list[ProjectContextRecord]:
        self.require_project(project_id)
        return list(
            self.session.exec(
                select(ProjectContextRecord)
                .where(
                    ProjectContextRecord.project_id == project_id,
                    ProjectContextRecord.record_key == record_key,
                )
                .order_by(ProjectContextRecord.version.asc())
            ).all()
        )

    def seed_confirmed_brief_context(
        self,
        project_id: str,
        brief: dict[str, Any],
        *,
        source_type: str,
        source_ref: str | None = None,
        commit: bool = True,
    ) -> list[ProjectContextRecord]:
        """Normalize an approved delivery brief into versioned context records."""
        records: list[ProjectContextRecord] = []
        fields = (
            ("objective", "requirement", "project.objective"),
            ("scope", "requirement", "project.scope"),
            ("out_of_scope", "requirement", "project.out_of_scope"),
            ("acceptance_criteria", "requirement", "project.acceptance_criteria"),
            ("permissions", "decision", "execution.permissions"),
        )
        for field, record_type, record_key in fields:
            value = brief.get(field)
            if value in (None, "", []):
                continue
            records.append(
                self.create_context_record(
                    project_id,
                    record_type=record_type,
                    record_key=record_key,
                    value={"value": value},
                    source_type=source_type,
                    source_ref=source_ref,
                    commit=False,
                )
            )

        for index, decision in enumerate(brief.get("decisions") or []):
            if not isinstance(decision, dict):
                continue
            key = str(decision.get("key") or decision.get("decision_id") or "").strip()
            if not key:
                key = f"decision.legacy_{index + 1}"
            records.append(
                self.create_context_record(
                    project_id,
                    record_type="decision",
                    record_key=key,
                    value=dict(decision),
                    source_type=source_type,
                    source_ref=source_ref,
                    confidence=(
                        "confirmed"
                        if decision.get("status", "confirmed") == "confirmed"
                        else "unverified"
                    ),
                    commit=False,
                )
            )
        if commit:
            self.session.commit()
        return records

    def build_relevant_context(
        self,
        project_id: str,
        *,
        agent_type: str,
        state: str,
    ) -> list[dict[str, Any]]:
        """Select bounded, authoritative context for a WorkItem.

        Observations and hypotheses stay out of ordinary agent contracts unless
        a future Coordinator decision explicitly references them.
        """
        records = self.get_active_context_records(
            project_id,
            record_types={"requirement", "decision", "capability"},
            agent_type=agent_type,
            state=state,
        )
        # Build capability-to-contract links from durable provenance.  Secret
        # questions already carry affected trace IDs; env-binding facts inherit
        # those IDs through the target env reference instead of relying on the
        # capability description or other text matching.
        capability_trace_ids: dict[str, set[str]] = {}
        capability_env_trace_ids: dict[str, set[str]] = {}
        for record in records:
            if record.record_type != "capability":
                continue
            value = record.value if isinstance(record.value, dict) else {}
            trace_ids = {
                str(item) for item in (value.get("affected_trace_ids") or []) if item
            }
            if record.source_ref:
                interaction = self.session.get(
                    QuestionInteraction, str(record.source_ref)
                )
                if interaction is not None:
                    question_id = str(value.get("question_id") or "")
                    trace_ids.update(
                        str(item)
                        for question in self.get_questions_for_interaction(
                            interaction.id
                        )
                        if not question_id or question.question_id == question_id
                        for item in (question.affected_trace_ids or [])
                        if item
                    )
            capability_trace_ids[record.id] = trace_ids
            secret_ref = str(value.get("secret_ref") or "")
            if secret_ref.startswith("env:") and trace_ids:
                capability_env_trace_ids.setdefault(
                    secret_ref.removeprefix("env:"), set()
                ).update(trace_ids)

        relevant: list[dict[str, Any]] = []
        for record in records:
            value = record.value if isinstance(record.value, dict) else {}
            applies_to = value.get("applies_to")
            if (
                not applies_to
                and record.record_type == "capability"
                and record.source_ref
            ):
                # Compatibility for credential facts recorded before
                # ``applies_to`` became mandatory.  Their source interaction
                # already contains the exact requesting Agent/state, so use
                # that durable provenance instead of leaking the fact into all
                # future prompts.
                interaction = self.session.get(
                    QuestionInteraction, str(record.source_ref)
                )
                if interaction is not None:
                    applies_to = [interaction.agent, interaction.resume_state]
            if applies_to and agent_type not in applies_to and state not in applies_to:
                continue
            affected_trace_ids = set(capability_trace_ids.get(record.id, set()))
            if record.record_type == "capability" and not affected_trace_ids:
                bindings = value.get("env_bindings")
                if isinstance(bindings, dict):
                    for target_env in bindings:
                        affected_trace_ids.update(
                            capability_env_trace_ids.get(str(target_env), set())
                        )
            relevant.append(
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
                    **(
                        {"affected_trace_ids": sorted(affected_trace_ids)}
                        if affected_trace_ids
                        else {}
                    ),
                }
            )
        return relevant

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
        """Return the runnable project selected by a user in a chat, if any.

        ``ChatUserContext.active_project_id`` is also the durable selection used
        to target a later iteration.  The selected row can therefore be
        terminal even though this method's callers require an active/paused
        run.  Keep those two meanings separate so a completed Agent-loop run
        cannot consume follow-up text as if it were still executing.
        """
        project = self.get_chat_user_selected_project(chat_id, open_id)
        if project is None or project.status not in {"active", "paused"}:
            return None
        return project

    def get_chat_user_selected_project(
        self,
        chat_id: str,
        open_id: str,
    ) -> Project | None:
        """Return the user's selected project, including terminal history."""
        ctx = self.session.exec(
            select(ChatUserContext).where(
                ChatUserContext.chat_id == chat_id,
                ChatUserContext.open_id == open_id,
            )
        ).first()
        if ctx is None or not ctx.active_project_id:
            return None
        project = self.session.get(Project, ctx.active_project_id)
        if project is None or project.status == "cancelled":
            return None
        return project

    def get_last_event_at(
        self, chat_id: str, open_id: str
    ) -> datetime | None:
        """Return the create_time of the last processed event for a user in a chat."""
        ctx = self.session.exec(
            select(ChatUserContext).where(
                ChatUserContext.chat_id == chat_id,
                ChatUserContext.open_id == open_id,
            )
        ).first()
        return ctx.last_event_at if ctx else None

    def update_last_event_at(
        self, chat_id: str, open_id: str, event_at: datetime
    ) -> None:
        """Update the watermark for the last processed event.

        Only moves forward in time.
        """
        ctx = self.session.exec(
            select(ChatUserContext).where(
                ChatUserContext.chat_id == chat_id,
                ChatUserContext.open_id == open_id,
            )
        ).first()
        if ctx is None:
            ctx = ChatUserContext(
                chat_id=chat_id,
                open_id=open_id,
                last_event_at=event_at,
            )
            self.session.add(ctx)
        elif ctx.last_event_at is None or event_at > ctx.last_event_at:
            ctx.last_event_at = event_at
            ctx.updated_at = _now()
        self.session.commit()

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
        # Free-text input cards intentionally have no button options.  Their
        # protocol event is normally ``answer``.  They may also expose
        # state-machine-declared upstream correction routes such as
        # ``needs_spec_change``.  Those routes let the Coordinator reject a
        # stale premise instead of forcing the user's correction to masquerade
        # as an answer.
        if approval.approval_type == "input":
            valid_actions.add("answer")
            machine = StateMachine(project.task_type)
            valid_actions.update(
                event
                for event in machine.get_state(project.current_state).get(
                    "next_states", {}
                )
                if event.startswith("needs_")
            )
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
        uses_agent_loop = project.context.get("orchestration_mode") == "agent_loop"
        state_machine = None if uses_agent_loop else StateMachine(project.task_type)

        recent_events = list(
            self.session.exec(
                select(EventLog)
                .where(EventLog.project_id == project_id)
                .order_by(EventLog.created_at.desc())
                .limit(50)
            ).all()
        )
        # Native provider protocol notifications are useful for diagnostics,
        # but are not project progress and must not leak into user status.
        recent_events = [
            event
            for event in recent_events
            if (event.meta or {}).get("profile_type") != "coordinator_event"
        ][:5]

        coordinator_agents = []
        if uses_agent_loop:
            coordinator_agents = list(
                self.session.exec(
                    select(CoordinatorAgentSession)
                    .where(CoordinatorAgentSession.project_id == project_id)
                    .order_by(CoordinatorAgentSession.created_at.asc())
                ).all()
            )

        approval_query = select(Approval).where(
            Approval.project_id == project_id,
            Approval.status == "pending",
        )
        if not uses_agent_loop:
            approval_query = approval_query.where(Approval.state == project.current_state)
        pending_approval = self.session.exec(
            approval_query
            .order_by(Approval.created_at.desc())
        ).first()

        return {
            "project_id": project_id,
            "current_state": project.current_state,
            "display": (
                (
                    {
                        "completed": "交付完成",
                        "failed": "执行失败",
                        "cancelled": "已取消",
                    }.get(project.status)
                    or {
                        "coordinating": "协调器处理中",
                        "running_agent": "Agent 执行中",
                        "waiting_user": "等待用户选择",
                        "waiting_external": "等待外部结果",
                        "idle": "等待下一步",
                    }.get(project.execution_status, project.execution_status)
                )
                if uses_agent_loop
                else state_machine.display_name(project.current_state)
            ),
            "status": project.status,
            "execution_status": project.execution_status,
            "waiting_reason": project.waiting_reason,
            "workspace_path": project.workspace_path,
            "bound_directory": project.bound_directory,
            "total_cost_usd": project.total_cost_usd,
            "pending_approval": {
                "approval_id": pending_approval.id,
                "type": pending_approval.approval_type,
                "question": pending_approval.question,
                "options": pending_approval.payload.get("options", []),
                "recovery": pending_approval.payload.get("recovery"),
            }
            if pending_approval
            else None,
            "agents": [
                {
                    "name": row.task_name,
                    "provider": row.provider,
                    "access": row.access,
                    "status": row.status,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in coordinator_agents
            ],
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

    def get_project_profile(
        self,
        project_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return an auditable wall-clock breakdown for one project.

        The categories intentionally distinguish Agent execution from time spent
        waiting on a confirmation/input card.  Intervals are merged before
        totals are calculated so retries or concurrent records cannot inflate
        the project wall clock.
        """
        project = self.require_project(project_id)
        observed_at = now or _now()
        project_end = (
            project.updated_at
            if project.status in {"completed", "failed", "cancelled"}
            else observed_at
        )

        def _parse_timestamp(value: Any) -> datetime | None:
            if not value:
                return None
            if isinstance(value, datetime):
                return value
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            except (TypeError, ValueError):
                return None

        def _merge_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
            valid = sorted((start, end) for start, end in intervals if end > start)
            if not valid:
                return 0.0
            merged: list[list[datetime]] = []
            for start, end in valid:
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                elif end > merged[-1][1]:
                    merged[-1][1] = end
            return sum((end - start).total_seconds() for start, end in merged)

        task_rows: list[dict[str, Any]] = []
        task_intervals: list[tuple[datetime, datetime]] = []
        for task in self.get_project_tasks(project_id):
            profiling_input = (task.input_data or {}).get("_profiling") or {}
            started_at = _parse_timestamp(profiling_input.get("execution_started_at"))
            timing_source = "runtime" if started_at is not None else "task_lifecycle"
            started_at = started_at or task.created_at
            ended_at = observed_at if task.status in {"pending", "running"} else task.updated_at
            ended_at = max(started_at, ended_at)
            task_intervals.append((started_at, ended_at))
            task_rows.append(
                {
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "state": task.state,
                    "status": task.status,
                    "provider": (task.input_data or {}).get("runtime_provider"),
                    "fallback_provider": profiling_input.get("fallback_provider"),
                    "runtime_success": profiling_input.get("runtime_success"),
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "elapsed_seconds": round((ended_at - started_at).total_seconds(), 3),
                    "timing_source": timing_source,
                    "rendered_contract_chars": profiling_input.get(
                        "rendered_contract_chars"
                    ),
                    "system_prompt_chars": profiling_input.get("system_prompt_chars"),
                    "runtime_user_prompt_chars": profiling_input.get(
                        "runtime_user_prompt_chars"
                    ),
                    "tool_call_count": profiling_input.get("tool_call_count"),
                    # Some runtimes do not report cost.  Keep this nullable so
                    # callers do not mistake the sum for a complete invoice.
                    "reported_cost_usd": task.cost_usd,
                    "error_message": task.error_message,
                }
            )

        agent_type_summary: dict[str, dict[str, Any]] = {}
        for row in task_rows:
            summary = agent_type_summary.setdefault(
                row["agent_type"],
                {
                    "agent_type": row["agent_type"],
                    "task_count": 0,
                    "status_counts": {},
                    "elapsed_seconds": 0.0,
                    "reported_cost_usd": 0.0,
                    "cost_reporting_task_count": 0,
                    "tool_call_count": 0,
                },
            )
            summary["task_count"] += 1
            status = row["status"]
            summary["status_counts"][status] = (
                summary["status_counts"].get(status, 0) + 1
            )
            summary["elapsed_seconds"] += row["elapsed_seconds"]
            if row["reported_cost_usd"] is not None:
                summary["reported_cost_usd"] += row["reported_cost_usd"]
                summary["cost_reporting_task_count"] += 1
            if row["tool_call_count"] is not None:
                summary["tool_call_count"] += row["tool_call_count"]
        summary_rows = []
        for summary in agent_type_summary.values():
            count = summary["task_count"]
            summary["elapsed_seconds"] = round(summary["elapsed_seconds"], 3)
            summary["average_seconds"] = round(
                summary["elapsed_seconds"] / count, 3
            )
            summary["reported_cost_usd"] = round(
                summary["reported_cost_usd"], 6
            )
            summary["cost_is_lower_bound"] = (
                summary["cost_reporting_task_count"] < count
            )
            summary_rows.append(summary)
        summary_rows.sort(key=lambda item: item["elapsed_seconds"], reverse=True)

        approval_rows: list[dict[str, Any]] = []
        approval_intervals: list[tuple[datetime, datetime]] = []
        for approval in self.get_project_approvals(project_id):
            ended_at = approval.responded_at or observed_at
            ended_at = max(approval.created_at, ended_at)
            approval_intervals.append((approval.created_at, ended_at))
            approval_rows.append(
                {
                    "approval_id": approval.id,
                    "state": approval.state,
                    "type": approval.approval_type,
                    "status": approval.status,
                    "started_at": approval.created_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "elapsed_seconds": round(
                        (ended_at - approval.created_at).total_seconds(), 3
                    ),
                }
            )

        coordinator_rows: list[dict[str, Any]] = []
        for event in reversed(self.get_project_events(project_id)):
            meta = event.meta or {}
            if meta.get("profile_type") != "conversation_coordinator":
                continue
            try:
                elapsed = max(0.0, float(meta.get("elapsed_seconds") or 0.0))
            except (TypeError, ValueError):
                elapsed = 0.0
            coordinator_rows.append(
                {
                    "event_id": event.id,
                    "finished_at": event.created_at.isoformat(),
                    "elapsed_seconds": round(elapsed, 3),
                    "attempts": meta.get("attempts"),
                    "success": meta.get("success"),
                    "message_id": meta.get("message_id"),
                }
            )

        coordinator_seconds = sum(
            item["elapsed_seconds"] for item in coordinator_rows
        )

        total_seconds = max(0.0, (project_end - project.created_at).total_seconds())
        agent_seconds = _merge_seconds(task_intervals)
        raw_decision_wait_seconds = _merge_seconds(approval_intervals)
        accounted_seconds = _merge_seconds(task_intervals + approval_intervals)
        # Approval records can remain open while an Agent is already running
        # (for example during clarification recovery).  Attribute overlapping
        # time to active execution, not to the user, so the headline buckets
        # form an exact wall-clock partition instead of exceeding total time.
        decision_wait_seconds = max(0.0, accounted_seconds - agent_seconds)
        system_seconds = max(0.0, total_seconds - accounted_seconds)
        return {
            "project_id": project.id,
            "status": project.status,
            "observed_at": observed_at.isoformat(),
            "total_seconds": round(total_seconds, 3),
            "agent_execution_seconds": round(agent_seconds, 3),
            "decision_wait_seconds": round(decision_wait_seconds, 3),
            "raw_decision_wait_seconds": round(raw_decision_wait_seconds, 3),
            "system_unattributed_seconds": round(system_seconds, 3),
            "conversation_coordinator_seconds": round(coordinator_seconds, 3),
            "conversation_coordinator_runs": coordinator_rows,
            "agent_type_summary": summary_rows,
            "reported_cost_usd": round(
                sum(
                    row["reported_cost_usd"] or 0.0
                    for row in task_rows
                ),
                6,
            ),
            "cost_is_lower_bound": any(
                row["reported_cost_usd"] is None for row in task_rows
            ),
            "tasks": task_rows,
            "approvals": approval_rows,
        }

    def get_project_events(self, project_id: str, limit: int | None = None) -> list[EventLog]:
        query = (
            select(EventLog)
            .where(EventLog.project_id == project_id)
            .order_by(EventLog.created_at.desc())
        )
        if limit:
            query = query.limit(limit)
        return list(self.session.exec(query).all())

    def get_project_context_records(self, project_id: str) -> list[ProjectContextRecord]:
        return list(
            self.session.exec(
                select(ProjectContextRecord)
                .where(ProjectContextRecord.project_id == project_id)
                .order_by(ProjectContextRecord.created_at.desc())
            ).all()
        )

    def get_project_notifications(self, project_id: str) -> list[NotificationOutbox]:
        return list(
            self.session.exec(
                select(NotificationOutbox)
                .where(NotificationOutbox.project_id == project_id)
                .order_by(NotificationOutbox.created_at.desc())
            ).all()
        )

    def get_project_card_projections(self, project_id: str) -> list[CardProjection]:
        return list(
            self.session.exec(
                select(CardProjection)
                .where(CardProjection.project_id == project_id)
                .order_by(CardProjection.updated_at.desc())
            ).all()
        )

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
        # A long-running Agent may finish after another request has cancelled
        # or paused the project. Refresh before applying its transition so a
        # cached ORM object cannot resurrect a terminal project.
        self.session.refresh(project)
        if project.status != "active":
            raise InvalidStateTransitionError(project.current_state, event)
        state_machine = StateMachine(project.task_type)

        next_state = state_machine.next_state(project.current_state, event)
        if next_state is None:
            raise InvalidStateTransitionError(project.current_state, event)

        previous_state = project.current_state
        project.previous_state = previous_state
        project.current_state = next_state
        if next_state == "FAILED":
            project.status = "failed"
            project.execution_status = "idle"
        elif next_state == "COMPLETED":
            project.status = "completed"
            project.execution_status = "idle"
        elif next_state == "CANCELLED":
            project.status = "cancelled"
            project.execution_status = "idle"
        elif state_machine.state_type(next_state) in {
            "human_input",
            "human_confirmation",
        }:
            project.execution_status = "waiting_user"
            project.waiting_reason = state_machine.display_name(next_state)
            project.waiting_since = _now()
        else:
            project.execution_status = "running"
            project.waiting_reason = None
            project.waiting_since = None
        project.updated_at = _now()

        if previous_state.endswith("_RECOVERY_PENDING"):
            recovery_for = state_machine.get_state(previous_state).get("recovery_for")
            retry_counts = dict(project.context.get("retry_counts", {}))
            if recovery_for:
                retry_counts[recovery_for] = 0
            new_context = dict(project.context)
            new_context["retry_counts"] = retry_counts
            recovery_resolutions = dict(
                new_context.get("coordinator_recovery_resolutions", {})
            )
            recovery_resolution = (
                recovery_resolutions.pop(recovery_for, None)
                if recovery_for
                else None
            )
            if event == "retry" and recovery_for and recovery_resolution:
                retry_feedback = dict(
                    new_context.get("agent_retry_feedback", {})
                )
                existing = retry_feedback.get(recovery_for)
                if isinstance(existing, str):
                    try:
                        existing_payload = json.loads(existing)
                    except (TypeError, ValueError):
                        existing_payload = {"prior_failure": existing}
                elif isinstance(existing, dict):
                    existing_payload = dict(existing)
                else:
                    existing_payload = {}
                existing_payload["coordinator_resolution"] = (
                    recovery_resolution.get("resolution_instruction")
                    if isinstance(recovery_resolution, dict)
                    else str(recovery_resolution)
                )
                retry_feedback[recovery_for] = json.dumps(
                    existing_payload, ensure_ascii=False, indent=2
                )
                new_context["agent_retry_feedback"] = retry_feedback
            if recovery_resolutions:
                new_context["coordinator_recovery_resolutions"] = (
                    recovery_resolutions
                )
            else:
                new_context.pop("coordinator_recovery_resolutions", None)
            new_context.pop("last_agent_failure", None)
            project.context = new_context

        # A human/system choice to re-enter a quality stage starts a new
        # self-repair window.  The previous implementation kept the one-shot
        # Tester harness retry counter for the entire project lifetime, so a
        # much later and unrelated Tester-owned conclusion error skipped its
        # automatic correction and unnecessarily interrupted the user.
        if previous_state == "REWORK":
            quality_self_retries = dict(
                project.context.get("quality_self_retry_counts", {})
            )
            if quality_self_retries.pop(next_state, None) is not None:
                next_context = dict(project.context)
                if quality_self_retries:
                    next_context["quality_self_retry_counts"] = quality_self_retries
                else:
                    next_context.pop("quality_self_retry_counts", None)
                project.context = next_context

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

        # Retry counts describe consecutive runtime/contract attempts in one
        # visit to an agent state. Once that state produces a valid business
        # result (including a quality ``fail`` routed to REWORK), the attempt
        # sequence is over. Keeping the count makes a later rollback/re-entry
        # inherit stale failures and reach recovery too early.
        if (
            state_machine.state_type(previous_state) == "agent_execution"
            and event != "runtime_exhausted"
            and next_state != previous_state
        ):
            retry_counts = dict(project.context.get("retry_counts", {}))
            retry_counts[previous_state] = 0
            retry_feedback = dict(project.context.get("agent_retry_feedback", {}))
            retry_feedback.pop(previous_state, None)
            coordinator_recoveries = dict(
                project.context.get("coordinator_recovery_counts", {})
            )
            coordinator_recoveries.pop(previous_state, None)
            next_context = {**project.context, "retry_counts": retry_counts}
            if retry_feedback:
                next_context["agent_retry_feedback"] = retry_feedback
            else:
                next_context.pop("agent_retry_feedback", None)
            if coordinator_recoveries:
                next_context["coordinator_recovery_counts"] = coordinator_recoveries
            else:
                next_context.pop("coordinator_recovery_counts", None)
            project.context = next_context

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
                    self._confirm_artifact_file(
                        project,
                        confirmed_artifact,
                        expected_version=current.get("version"),
                    )
            self._resolve_confirmed_stage_feedback(project_id, previous_state)

        # Leaving a state invalidates every still-pending approval issued for
        # it. Normally the selected approval is already responded; this also
        # closes a duplicate that won a cross-loop race immediately before
        # this transition acquired the database writer lock.
        if next_state != previous_state:
            self.session.exec(
                update(Approval)
                .where(
                    Approval.project_id == project_id,
                    Approval.state == previous_state,
                    Approval.status == "pending",
                )
                .values(status="expired")
            )

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

    @staticmethod
    def _confirm_artifact_file(
        project: Project,
        artifact_id: str,
        *,
        expected_version: int | None,
    ) -> None:
        """Keep the durable artifact and project metadata confirmation in sync."""
        filename = {
            "requirements": "REQUIREMENTS.json",
            "architecture": "ARCHITECTURE.json",
            "specs": "SPECS.json",
        }.get(artifact_id)
        if not filename:
            return
        artifact_path = project_mac_dir(project) / filename
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # Legacy/unit-test projects may track versions without materialized
            # artifacts. Preserve that supported metadata-only behavior.
            return
        if not isinstance(artifact, dict):
            raise ValueError(f"Artifact must be an object: {artifact_path}")
        if expected_version is not None and artifact.get("version") != expected_version:
            raise ValueError(
                f"Artifact version mismatch for {artifact_id}: "
                f"expected {expected_version}, found {artifact.get('version')}"
            )
        artifact["status"] = "confirmed"
        temp_path = artifact_path.with_suffix(f"{artifact_path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(artifact_path)

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
        attempt_id = generate_id("attempt")
        previous_tasks = list(
            self.session.exec(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.agent_type == agent_type,
                    Task.state == state,
                )
                .order_by(Task.created_at.desc())
            ).all()
        )
        attempt_number = max(
            (item.attempt_number or 1 for item in previous_tasks), default=0
        ) + 1
        raw_input = dict(input_data)
        supplied_contract = raw_input.get("work_item_contract")
        supplied_contract = supplied_contract if isinstance(supplied_contract, dict) else {}
        current_artifact_refs = dict(project.context.get("artifact_versions", {}))
        if WorkItemCompiler.stage_kind(state) in {
            "implementation",
            "quality",
            "acceptance",
        }:
            def _artifact_identity(value: object) -> dict[str, tuple[object, object]]:
                refs = value if isinstance(value, dict) else {}
                return {
                    str(name): (
                        item.get("version"),
                        item.get("round"),
                    )
                    for name, item in refs.items()
                    if isinstance(item, dict)
                }

            previous_success = next(
                (
                    item
                    for item in previous_tasks
                    if item.status == "completed"
                    and isinstance(item.output_data, dict)
                    and item.output_data.get("next_state_event") == "complete"
                ),
                None,
            )
            previous_contract = (
                (previous_success.input_data or {}).get("work_item_contract", {})
                if previous_success is not None
                else {}
            )
            if not isinstance(previous_contract, dict):
                previous_contract = {}
            previous_refs = previous_contract.get("artifact_refs", {})
            supplied_contract = {
                **supplied_contract,
                "artifact_revision_changed": (
                    previous_success is None
                    or not previous_contract.get("artifact_contract_key")
                    or _artifact_identity(previous_refs)
                    != _artifact_identity(current_artifact_refs)
                ),
            }
        relevant_facts = (
            list(supplied_contract.get("relevant_facts") or [])
            if "relevant_facts" in supplied_contract
            else self.build_relevant_context(
                project_id, agent_type=agent_type, state=state
            )
        )
        acceptance_criteria = list(
            supplied_contract.get("acceptance_criteria") or []
        )
        if not acceptance_criteria:
            for fact in relevant_facts:
                if fact.get("key") != "project.acceptance_criteria":
                    continue
                value = fact.get("value") or {}
                acceptance_criteria = list(value.get("value") or [])
                if acceptance_criteria:
                    break
        if not acceptance_criteria:
            requirements_path = project_mac_dir(project) / "REQUIREMENTS.json"
            try:
                requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                requirements = {}
            acceptance_criteria = list(requirements.get("acceptance_criteria") or [])

        work_item_contract = WorkItemCompiler().compile(
            state=state,
            agent_type=agent_type,
            objective=str(raw_input.get("description") or state),
            supplied_contract=supplied_contract,
            relevant_facts=relevant_facts,
            acceptance_criteria=acceptance_criteria,
            regression_acceptance_criteria=list(
                supplied_contract.get("regression_acceptance_criteria") or []
            ),
            open_feedback=list(raw_input.get("open_feedback") or []),
            artifact_refs=current_artifact_refs,
            context_version=project.context_version,
        )
        raw_input["work_item_contract"] = work_item_contract
        task = Task(
            id=task_id,
            project_id=project_id,
            agent_type=agent_type,
            state=state,
            round_number=round_number,
            attempt_number=attempt_number,
            context_version=project.context_version,
            attempt_id=attempt_id,
            status="pending",
            input_data=raw_input,
        )
        self.session.add(task)
        previous = next(
            (
                item
                for item in previous_tasks
                if item.superseded_by_task_id is None and item.status != "running"
            ),
            None,
        )
        if previous is not None:
            previous.superseded_by_task_id = task.id
            self.session.add(previous)
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Task created: {task_id} for {agent_type} at {state}",
            meta={
                "task_id": task_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "agent_type": agent_type,
                "state": state,
                "context_version": project.context_version,
            },
        )
        self.session.commit()
        return task

    def refresh_task_work_item_feedback(
        self,
        task_id: str,
        open_feedback: list[dict[str, Any]],
    ) -> Task:
        """Recompile a pending task after feedback arrives at dispatch time.

        Feedback IDs, routes, and affected acceptance criteria are compiler
        outputs and must change together. Patching only the human-readable
        feedback list creates a split-brain WorkItem.
        """
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.status != "pending":
            raise ValueError(f"Task {task_id} is no longer pending")
        project = self.require_project(task.project_id)
        raw_input = dict(task.input_data or {})
        supplied = dict(raw_input.get("work_item_contract") or {})
        for derived_key in (
            "feedback_refs",
            "feedback_routes",
            "in_scope_acceptance_criterion_ids",
        ):
            supplied.pop(derived_key, None)
        compiled = WorkItemCompiler().compile(
            state=task.state,
            agent_type=task.agent_type,
            objective=str(raw_input.get("description") or task.state),
            supplied_contract=supplied,
            relevant_facts=list(supplied.get("relevant_facts") or []),
            acceptance_criteria=list(supplied.get("acceptance_criteria") or []),
            regression_acceptance_criteria=list(
                supplied.get("regression_acceptance_criteria") or []
            ),
            open_feedback=list(open_feedback),
            artifact_refs=dict((project.context or {}).get("artifact_versions", {})),
            context_version=project.context_version,
        )
        task.input_data = {
            **raw_input,
            "open_feedback": list(open_feedback),
            "work_item_contract": compiled,
        }
        self.session.add(task)
        self.session.commit()
        return task

    def assess_task_freshness(self, task_id: str) -> dict[str, Any]:
        """Compare a work item's context snapshot with current project state.

        Phase one only reports staleness. The orchestrator records this result
        but keeps the existing state-machine behavior unchanged.
        """
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        project = self.require_project(task.project_id)
        reasons: list[str] = []
        if task.context_version != project.context_version:
            reasons.append(
                f"context_version changed from {task.context_version} "
                f"to {project.context_version}"
            )
        if task.state != project.current_state:
            reasons.append(f"project state changed from {task.state} to {project.current_state}")
        return {
            "fresh": not reasons,
            "task_context_version": task.context_version,
            "project_context_version": project.context_version,
            "reasons": reasons,
        }

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
        completed_at = _now()
        profiling_input = (task.input_data or {}).get("_profiling") or {}
        started_at = profiling_input.get("execution_started_at")
        profiling_output: dict[str, Any] = {}
        if started_at:
            try:
                started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
                profiling_output = {
                    "execution_started_at": started_dt.isoformat(),
                    "execution_completed_at": completed_at.isoformat(),
                    "execution_seconds": round(
                        max(0.0, (completed_at - started_dt).total_seconds()), 3
                    ),
                }
            except (TypeError, ValueError):
                profiling_output = {}
        if cost_info:
            usage_fields = (
                "input_tokens",
                "cached_input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
                "estimated_cost_usd",
            )
            usage = {
                key: cost_info[key]
                for key in usage_fields
                if isinstance(cost_info.get(key), (int, float))
            }
            if (
                "total_tokens" not in usage
                and "input_tokens" in usage
                and "output_tokens" in usage
            ):
                usage["total_tokens"] = (
                    usage["input_tokens"] + usage["output_tokens"]
                )
            if usage:
                profiling_output["usage"] = usage
            runtime_attempts = cost_info.get("runtime_attempts")
            if isinstance(runtime_attempts, list):
                safe_attempts: list[dict[str, Any]] = []
                for raw_attempt in runtime_attempts:
                    if not isinstance(raw_attempt, dict):
                        continue
                    attempt = {
                        key: raw_attempt[key]
                        for key in (
                            "provider",
                            "success",
                            "failure_reason",
                            *usage_fields,
                        )
                        if isinstance(raw_attempt.get(key), (str, bool, int, float))
                    }
                    if attempt:
                        safe_attempts.append(attempt)
                if safe_attempts:
                    profiling_output["runtime_attempts"] = safe_attempts
        task.output_data = {
            **dict(output_data or {}),
            **({"_profiling": profiling_output} if profiling_output else {}),
        }
        if cost_info:
            task.cost_usd = cost_info.get("estimated_cost_usd")
        task.updated_at = completed_at

        project = self.session.get(Project, task.project_id)
        if project is not None and task.cost_usd is not None:
            project.total_cost_usd = (project.total_cost_usd or 0.0) + task.cost_usd

        # A valid result closes the failure reports from the superseded
        # attempts in this same Agent/state sequence.  They remain queryable as
        # audit evidence but are no longer active blockers.
        open_failures = self.session.exec(
            select(TaskFailureReport).where(
                TaskFailureReport.project_id == task.project_id,
                TaskFailureReport.agent_type == task.agent_type,
                TaskFailureReport.state == task.state,
                TaskFailureReport.status == "open",
            )
        ).all()
        for failure in open_failures:
            failure.status = "resolved"
            failure.resolved_at = _now()
            self.session.add(failure)

        self.session.commit()
        return task

    @staticmethod
    def _failure_fingerprint(error_message: str) -> str:
        """Return a stable similarity key without trying to enumerate errors."""
        normalized = error_message.lower()
        normalized = re.sub(
            r"(?:task|attempt|fb|qi|appr|ctx)_[0-9a-z_]+", "<id>", normalized
        )
        normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", normalized)
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]

    def get_task_failure_history(
        self,
        project_id: str,
        *,
        agent_type: str,
        state: str,
        limit: int = 6,
    ) -> list[TaskFailureReport]:
        rows = self.session.exec(
            select(TaskFailureReport)
            .where(
                TaskFailureReport.project_id == project_id,
                TaskFailureReport.agent_type == agent_type,
                TaskFailureReport.state == state,
            )
            .order_by(TaskFailureReport.created_at.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))

    async def fail_task(
        self,
        task_id: str,
        error_message: str,
        *,
        evidence: dict[str, Any] | None = None,
        suggested_resolver: str = "agent",
    ) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        failed_at = _now()
        task.status = "failed"
        task.error_message = error_message
        task.retry_count += 1
        profiling_input = (task.input_data or {}).get("_profiling") or {}
        started_at = profiling_input.get("execution_started_at")
        if started_at:
            try:
                started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
                task.output_data = {
                    **dict(task.output_data or {}),
                    "_profiling": {
                        "execution_started_at": started_dt.isoformat(),
                        "execution_completed_at": failed_at.isoformat(),
                        "execution_seconds": round(
                            max(0.0, (failed_at - started_dt).total_seconds()), 3
                        ),
                    },
                }
            except (TypeError, ValueError):
                pass
        task.updated_at = failed_at
        report = TaskFailureReport(
            id=generate_id("fail"),
            project_id=task.project_id,
            task_id=task.id,
            agent_type=task.agent_type,
            state=task.state,
            attempt_number=task.attempt_number,
            fingerprint=self._failure_fingerprint(error_message),
            summary=error_message,
            evidence=redact_secrets(dict(evidence or {})),
            suggested_resolver=suggested_resolver,
        )
        self.session.add(report)
        self.session.commit()
        return task

    async def mark_task_stale(
        self,
        task_id: str,
        output_data: dict[str, Any],
        reason: str,
    ) -> Task:
        task = self.session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task.status = "stale"
        task.output_data = output_data
        task.error_message = reason
        task.updated_at = _now()
        self.session.commit()
        return task

    def recover_stale_tasks(self) -> list[Task]:
        """Mark interrupted tasks failed while preserving resumable coder edits.

        A process restart must not turn a durable coder worktree into a fresh
        checkout: doing so discards the only copy of edits made before the
        interruption and makes the Agent repeat the implementation.
        """
        tasks = self.session.exec(select(Task).where(Task.status == "running")).all()
        for task in tasks:
            project = self.session.get(Project, task.project_id)
            worktree = Path(task.worktree_path) if task.worktree_path else None
            if (
                project is not None
                and project.status == "active"
                and project.current_state == task.state
            ):
                # _execute_agent consumes a one-shot Coordinator instruction
                # from Project.context before launching the runtime.  If the
                # service stops during that runtime, put the exact instruction
                # back so the recovered attempt does not silently revert to
                # the original project description.
                consumed_retry = str(
                    (task.input_data or {}).get("retry_feedback") or ""
                ).strip()
                if not consumed_retry:
                    # Compatibility recovery for attempts launched by older
                    # versions that popped the instruction before task start.
                    # Only walk the immediately preceding uninterrupted chain
                    # of restart failures for the same stage; a completed or
                    # differently failed attempt is a hard lineage boundary.
                    prior_tasks = self.session.exec(
                        select(Task)
                        .where(Task.project_id == task.project_id)
                        .where(Task.state == task.state)
                        .where(Task.agent_type == task.agent_type)
                        .where(Task.created_at < task.created_at)
                        .order_by(Task.created_at.desc())
                    ).all()
                    for prior in prior_tasks:
                        if not (
                            prior.status == "failed"
                            and prior.error_message
                            == "service restarted while task was running"
                        ):
                            break
                        consumed_retry = str(
                            (prior.input_data or {}).get("retry_feedback") or ""
                        ).strip()
                        if consumed_retry:
                            break
                if consumed_retry:
                    context = dict(project.context or {})
                    retry_by_state = dict(
                        context.get("agent_retry_feedback", {})
                    )
                    retry_by_state.setdefault(task.state, consumed_retry)
                    context["agent_retry_feedback"] = retry_by_state
                    project.context = context
                    project.updated_at = _now()
                    self.session.add(project)
            if (
                task.agent_type == "coder"
                and project is not None
                and project.status == "active"
                and project.current_state == task.state
                and worktree is not None
                and worktree.is_dir()
            ):
                context = dict(project.context or {})
                context["_resumable_agent_worktree"] = {
                    "task_id": task.id,
                    "agent_type": task.agent_type,
                    "state": task.state,
                    "worktree_path": str(worktree),
                    "mode": "service_restart",
                }
                project.context = context
                project.updated_at = _now()
                self.session.add(project)
            task.status = "failed"
            task.error_message = "service restarted while task was running"
            task.updated_at = _now()
            agent_session = self.session.exec(
                select(CoordinatorAgentSession).where(
                    CoordinatorAgentSession.task_id == task.id
                )
            ).first()
            if agent_session is not None:
                agent_session.status = "failed"
                agent_session.latest_message = task.error_message
                agent_session.updated_at = _now()
                self.session.add(agent_session)
        for coordinator in self.session.exec(
            select(CoordinatorSession).where(CoordinatorSession.status == "running")
        ).all():
            coordinator.status = "idle"
            coordinator.active_turn_id = None
            coordinator.last_error = "service restarted during Coordinator turn"
            coordinator.updated_at = _now()
            self.session.add(coordinator)
        if tasks:
            self.session.commit()
        return list(tasks)

    def reconcile_project_invariants(self) -> set[str]:
        """Repair safe status invariants and return states needing a tick.

        Active agent states are executable durable work, not a historical
        record.  If such a state has no running task (and is not explicitly
        waiting for external evidence), startup must schedule a tick.  This
        covers both an interrupted running process and the smaller crash gap
        after a task was persisted as failed/completed but before the
        orchestrator scheduled or applied the next step.

        Human waits and explicit ``waiting_external`` postures remain stable
        boundaries.  Terminal flags, missing interactions, and deterministic
        system actions are repaired here as well.  Every repair is safe and
        idempotent on startup.
        """
        projects = self.session.exec(select(Project)).all()
        projects_needing_tick: set[str] = set()
        changed = False
        terminal_status = {
            "COMPLETED": "completed",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
            "CANCELED": "cancelled",
        }
        for project in projects:
            # Approval policy is system behaviour, not a frozen project fact.
            # Migrate active AI-first projects when the policy evolves so an
            # existing normal/auto project does not remain stuck on a legacy
            # human boundary forever. Strict is an explicit user choice and
            # night is pinned at creation to avoid changing semantics as its
            # time window opens or closes.
            approval_mode = str((project.context or {}).get("approval_mode") or "normal")
            if project.status == "active" and approval_mode in {"normal", "auto"}:
                effective_approval = self._resolve_approval_config(approval_mode)
                if project.approval_config != effective_approval:
                    project.approval_config = effective_approval
                    project.updated_at = _now()
                    self.session.add(project)
                    changed = True
                    projects_needing_tick.add(project.id)
            expected_status = terminal_status.get(project.current_state)
            if expected_status and project.status != expected_status:
                project.status = expected_status
                project.updated_at = _now()
                changed = True
                continue
            if project.status != "active":
                continue
            if project.context.get("orchestration_mode") == "agent_loop":
                running = self.session.exec(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.status == "running",
                    )
                ).first()
                if (
                    running is None
                    and project.execution_status
                    not in {"waiting_user", "waiting_external"}
                ):
                    projects_needing_tick.add(project.id)
                continue
            try:
                state_machine = StateMachine(project.task_type)
            except (ValueError, FileNotFoundError):
                continue
            if state_machine.is_system_action(project.current_state):
                projects_needing_tick.add(project.id)
                continue
            if (
                state_machine.is_human_confirmation(project.current_state)
                or state_machine.is_human_input(project.current_state)
            ) and project.execution_status == "running":
                project.execution_status = "waiting_user"
                project.waiting_reason = state_machine.display_name(
                    project.current_state
                )
                project.waiting_since = project.waiting_since or _now()
                self.session.add(project)
                changed = True
            if state_machine.is_agent_execution(project.current_state):
                running = self.session.exec(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.state == project.current_state,
                        Task.status == "running",
                    )
                ).first()
                if (
                    running is None
                    and project.execution_status != "waiting_external"
                ):
                    projects_needing_tick.add(project.id)
                continue
            if not (
                state_machine.is_human_confirmation(project.current_state)
                or state_machine.is_human_input(project.current_state)
            ):
                continue
            pending = self.session.exec(
                select(Approval).where(
                    Approval.project_id == project.id,
                    Approval.state == project.current_state,
                    Approval.status == "pending",
                )
            ).first()
            if pending is None:
                projects_needing_tick.add(project.id)
        if changed:
            self.session.commit()
        return projects_needing_tick

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
        enforce_current_state: bool = True,
    ) -> Approval:
        # Serialize approval creation with state transitions at the database
        # writer boundary.  The orchestrator lock is intentionally scoped per
        # asyncio loop, while Feishu WS and HTTP control requests may run on
        # different loops.  A no-op conditional UPDATE therefore acts as both
        # an atomic state guard and a SQLite writer lock: a stale tick cannot
        # insert an approval after another request has left ``state``.
        if enforce_current_state:
            state_guard = self.session.exec(
                update(Project)
                .where(
                    Project.id == project_id,
                    Project.current_state == state,
                    Project.status == "active",
                )
                .values(current_state=state)
            )
            if state_guard.rowcount != 1:
                self.session.rollback()
                raise StaleApprovalStateError(
                    f"Project {project_id} is no longer active in {state}"
                )

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

    def _resolve_confirmed_stage_feedback(
        self, project_id: str, confirmation_state: str
    ) -> None:
        """Close user feedback covered by the artifact that was just confirmed.

        A user can reject an artifact through either the interactive feedback
        card or an ordinary natural-language turn.  Those paths deliberately
        produce different feedback ``kind`` values, so lifecycle closure must
        be based on the confirmed stage/owner rather than the input widget that
        happened to capture the feedback.

        Quality failures are excluded: they require explicit verification by a
        Tester/Reviewer and must never be closed merely because a planning
        artifact was confirmed.
        """
        source_stage = {
            "REQUIREMENT_CONFIRM_PENDING": "PLANNING_REQUIREMENTS",
            "ARCHITECTURE_CONFIRM_PENDING": "PLANNING_ARCHITECTURE",
            "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "PLANNING_IMPLEMENTATION_SPECS",
            "BUG_FIX_CONFIRM_PENDING": "BUG_ANALYSIS",
            "OUTLINE_CONFIRM_PENDING": "DOC_OUTLINE",
        }.get(confirmation_state)
        if source_stage is None:
            return
        stage_target = {
            "PLANNING_REQUIREMENTS": "requirements",
            "PLANNING_ARCHITECTURE": "architecture",
            "PLANNING_IMPLEMENTATION_SPECS": "specs",
            "BUG_ANALYSIS": "requirements",
            "DOC_OUTLINE": "requirements",
        }[source_stage]
        # Artifact-local rejection/clarification is closed when that artifact
        # is confirmed.  A correction or scope change is a delivery contract:
        # planning may incorporate it, but only downstream quality evidence can
        # prove the resulting product behavior.  Closing those packets here
        # previously let a Requirements edit erase a still-reproducible UI bug
        # before Coder or Tester ever received it.
        user_feedback_kinds = {
            "human_rejection",
            "coordinator_rejection",
            "clarification",
        }
        packets = self.session.exec(
            select(FeedbackPacket).where(
                FeedbackPacket.project_id == project_id,
                FeedbackPacket.kind.in_(user_feedback_kinds),
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
        ).all()
        for packet in packets:
            owner = str(packet.recommended_rework_target or "").strip().lower()
            if (
                packet.source_stage not in {source_stage, confirmation_state}
                and owner != stage_target
            ):
                continue
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
        project_id: str,
        status: str = "resolved",
    ) -> FeedbackPacket:
        packet = self.session.get(FeedbackPacket, feedback_id)
        if packet is None:
            raise ValueError(f"Feedback packet not found: {feedback_id}")
        if packet.project_id != project_id:
            raise ValueError(
                f"Feedback packet {feedback_id} does not belong to project {project_id}"
            )
        resolving_task = self.session.get(Task, resolved_by_task_id)
        if resolving_task is None or resolving_task.project_id != project_id:
            raise ValueError(
                f"Resolving task {resolved_by_task_id} does not belong to project {project_id}"
            )
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
        recommended_rework_target: str | None = None,
    ) -> PendingUserInput:
        inp = PendingUserInput(
            id=generate_id("input"),
            project_id=project_id,
            sender_id=sender_id,
            source_chat_id=source_chat_id,
            observed_state=observed_state,
            content=content,
            kind=kind,
            recommended_rework_target=recommended_rework_target,
            status="pending",
        )
        self.session.add(inp)
        if kind in ("scope_change", "correction"):
            self.create_context_record(
                project_id,
                record_type="requirement",
                record_key=f"user_input.{inp.id}",
                value={
                    "content": content,
                    "kind": kind,
                    "observed_state": observed_state,
                    "recommended_rework_target": recommended_rework_target,
                },
                source_type="user",
                source_ref=inp.id,
                commit=False,
            )
        await self.log_event(
            project_id=project_id,
            level="INFO",
            message=f"Pending user input created: {inp.id} ({kind})",
            meta={
                "input_id": inp.id,
                "observed_state": observed_state,
                "kind": kind,
                "source_chat_id": source_chat_id,
                "recommended_rework_target": recommended_rework_target,
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
        self.session.commit()

    # ------------------------------------------------------------------
    # Question interactions (unified ask_user / AGENT_INPUT_PENDING)
    # ------------------------------------------------------------------

    async def create_question_interaction(
        self, project_id, origin_task_id, origin_round, resume_state,
        agent, stage, batch_key, questions, approval_id=None,
        requested_to="coordinator", escalated_to_user=False, commit=True,
    ) -> QuestionInteraction:
        existing = self.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.batch_key == batch_key,
                QuestionInteraction.origin_round == origin_round,
                QuestionInteraction.status == "pending",
            )
        ).first()
        if existing is not None:
            return existing
        requested_keys = {
            str(q.get("decision_key") or "").strip().lower()
            for q in questions
            if isinstance(q, dict) and str(q.get("decision_key") or "").strip()
        }
        if requested_keys:
            pending_interactions = self.session.exec(
                select(QuestionInteraction).where(
                    QuestionInteraction.project_id == project_id,
                    QuestionInteraction.status == "pending",
                )
            ).all()
            for candidate in pending_interactions:
                existing_keys = {
                    str(q.decision_key or "").strip().lower()
                    for q in self.get_questions_for_interaction(candidate.id)
                    if str(q.decision_key or "").strip()
                }
                if requested_keys.issubset(existing_keys):
                    return candidate
        interaction_id = generate_id("qi")
        interaction = QuestionInteraction(
            id=interaction_id, project_id=project_id, origin_task_id=origin_task_id,
            origin_round=origin_round, resume_state=resume_state, agent=agent,
            stage=stage, batch_key=batch_key, status="pending", approval_id=approval_id,
            requested_to=requested_to, escalated_to_user=escalated_to_user,
        )
        self.session.add(interaction)
        # These models intentionally have no ORM relationship. With foreign
        # keys enabled, explicitly materialize the parent before its questions.
        self.session.flush()
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
                decision_key=q.get("decision_key"),
                kind=q.get("kind", "blocking"), question=q.get("question", ""),
                reason=q.get("reason", ""), allow_free_text=q.get("allow_free_text", False),
                options=options, multiple=q.get("multiple", False),
                default_assumption=q.get("default_assumption"), recommended_option=recommended,
                affected_trace_ids=q.get("affected_trace_ids", []), status="pending",
                sensitivity=q.get("sensitivity", "internal"),
                secret_env_var=q.get("secret_env_var"),
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

    def _question_project_id(self, question: Question) -> str | None:
        interaction = self.session.get(QuestionInteraction, question.interaction_id)
        return interaction.project_id if interaction is not None else None

    def get_runtime_secret(self, project_id: str, env_var: str) -> str | None:
        return self.secret_store.get(project_id, env_var)

    def reconcile_secret_answers(self) -> int:
        """Move legacy secret values out of ordinary QuestionAnswer rows."""
        migrated = 0
        questions = self.session.exec(
            select(Question).where(
                Question.sensitivity == "secret",
                Question.secret_env_var.is_not(None),
            )
        ).all()
        for question in questions:
            project_id = self._question_project_id(question)
            if not project_id or not question.secret_env_var:
                continue
            answers = self.session.exec(
                select(QuestionAnswer).where(
                    QuestionAnswer.question_id == question.id
                )
            ).all()
            for answer in answers:
                value = str(answer.answer or "")
                if not value or value.startswith("secret_ref:"):
                    continue
                ref = self.secret_store.set(project_id, question.secret_env_var, value)
                answer.answer = f"secret_ref:{ref}"
                answer.answer_label = "已安全保存"
                self.session.add(answer)
                migrated += 1
        if migrated:
            self.session.commit()
        return migrated

    def backfill_question_decision_keys(self) -> int:
        """Give legacy questions a stable key derived only from structured data."""
        updated = 0
        questions = self.session.exec(
            select(Question).where(Question.decision_key.is_(None))
        ).all()
        for question in questions:
            interaction = self.session.get(
                QuestionInteraction, question.interaction_id
            )
            if interaction is None:
                continue
            trace_ids = sorted(
                str(value) for value in (question.affected_trace_ids or []) if value
            )
            structural_identity = (
                f"env:{question.secret_env_var}"
                if question.secret_env_var
                else (
                    "trace:" + "|".join(trace_ids)
                    if trace_ids
                    else f"question:{question.question_id}"
                )
            )
            digest = hashlib.sha256(
                (
                    f"{interaction.agent}|{interaction.resume_state}|"
                    f"{question.kind}|{structural_identity}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            agent = re.sub(r"[^a-z0-9_]+", "_", interaction.agent.lower()).strip("_")
            question.decision_key = f"resolution.{agent or 'agent'}.{digest}"
            self.session.add(question)
            updated += 1
        if updated:
            self.session.commit()
        return updated

    def reconcile_runtime_capability_grants(self) -> int:
        """Recover grants only from exact historical option metadata and answers."""
        changed_projects: set[str] = set()
        interactions = self.session.exec(select(QuestionInteraction)).all()
        for interaction in interactions:
            origin_task = (
                self.session.get(Task, interaction.origin_task_id)
                if interaction.origin_task_id
                else None
            )
            output = (
                origin_task.output_data
                if origin_task is not None and isinstance(origin_task.output_data, dict)
                else {}
            )
            origin_questions = {
                str(item.get("question_id")): item
                for item in ((output.get("question_batch") or {}).get("questions") or [])
                if isinstance(item, dict) and item.get("question_id")
            }
            project = self.session.get(Project, interaction.project_id)
            if project is None:
                continue
            context = dict(project.context or {})
            grants = list(context.get("runtime_capability_grants") or [])
            for question in self.get_questions_for_interaction(interaction.id):
                answer = self.get_question_answer(question.id)
                origin = origin_questions.get(str(question.question_id))
                if answer is None or not isinstance(origin, dict):
                    continue
                selected = next(
                    (
                        option
                        for option in (origin.get("options") or [])
                        if isinstance(option, dict)
                        and str(option.get("key") or "") == str(answer.answer or "")
                    ),
                    None,
                )
                capabilities = sorted(
                    {
                        str(value)
                        for value in (
                            (selected or {}).get("grants_runtime_capabilities") or []
                        )
                        if str(value) == "HostAutomation"
                    }
                )
                if not capabilities:
                    continue
                grant = {
                    "agent": interaction.agent,
                    "state": interaction.resume_state,
                    "question_id": question.question_id,
                    "capabilities": capabilities,
                }
                grants = [
                    item for item in grants
                    if not (
                        isinstance(item, dict)
                        and item.get("agent") == grant["agent"]
                        and item.get("state") == grant["state"]
                        and item.get("question_id") == grant["question_id"]
                    )
                ]
                grants.append(grant)
                context["runtime_capability_grants"] = grants[-20:]
                changed_projects.add(project.id)
            if project.id in changed_projects:
                project.context = context
                self.session.add(project)
        if changed_projects:
            self.session.commit()
        return len(changed_projects)

    def reconcile_feedback_from_passing_tester_evidence(self) -> int:
        """Close legacy packets explicitly linked by passing Tester evidence."""
        resolved = 0
        tasks = self.session.exec(
            select(Task).where(Task.agent_type == "tester", Task.status == "completed")
        ).all()
        for task in tasks:
            output = task.output_data if isinstance(task.output_data, dict) else {}
            feedback_ids = {
                str(feedback_id)
                for evidence in (output.get("test_evidence") or [])
                if isinstance(evidence, dict) and evidence.get("result") == "pass"
                for feedback_id in (evidence.get("feedback_ids") or [])
                if feedback_id
            }
            for feedback_id in feedback_ids:
                packet = self.session.get(FeedbackPacket, feedback_id)
                if (
                    packet is None
                    or packet.project_id != task.project_id
                    or packet.status != "open"
                ):
                    continue
                packet.status = "resolved"
                packet.resolved_by_task_id = task.id
                packet.updated_at = _now()
                self.session.add(packet)
                resolved += 1
        if resolved:
            self.session.commit()
        return resolved

    async def submit_question_answer(
        self, question_db_id, answer, answer_label, skipped, responded_by,
        answer_source="user", source_refs=None, commit=True,
        keep_pending: bool = False,
    ) -> QuestionAnswer:
        question = self.session.get(Question, question_db_id)
        stored_answer = answer
        stored_label = answer_label
        if (
            question is not None
            and question.sensitivity == "secret"
            and question.secret_env_var
            and answer
            and not skipped
        ):
            project_id = self._question_project_id(question)
            if project_id:
                ref = self.secret_store.set(project_id, question.secret_env_var, answer)
                stored_answer = f"secret_ref:{ref}"
                stored_label = "已安全保存"
        qa = QuestionAnswer(
            id=generate_id("qa"), question_id=question_db_id, answer=stored_answer,
            answer_label=stored_label, skipped=skipped, responded_by=responded_by,
            answer_source=answer_source, source_refs=list(source_refs or []),
        )
        self.session.add(qa)
        if question is not None:
            question.status = (
                "pending" if keep_pending else ("skipped" if skipped else "answered")
            )
            interaction = self.session.get(
                QuestionInteraction, question.interaction_id
            )
            trace_ids = [
                str(value)
                for value in (question.affected_trace_ids or [])
                if value
            ]
            if (
                interaction is not None
                and interaction.stage == "acceptance"
                and len(trace_ids) == 1
                and answer_source == "user"
                and answer in {
                    "verified_pass",
                    "verified_fail",
                    "not_verified",
                }
            ):
                project = self.require_project(interaction.project_id)
                verdict = {
                    "verified_pass": "pass",
                    "verified_fail": "fail",
                    "not_verified": "not_verified",
                }[str(answer)]
                self.record_acceptance_evidence(
                    interaction.project_id,
                    trace_ids[0],
                    verdict=verdict,
                    verification_method="external",
                    evidence_scope=question.question,
                    source_type="user",
                    source_ref=question.question_id,
                    artifact_revision=f"context:{project.context_version}",
                    evidence=[
                        {
                            "question_id": question.question_id,
                            "answer": answer,
                            "answer_label": answer_label,
                        }
                    ],
                    limitations=(
                        ["用户明确选择尚未验证"]
                        if answer == "not_verified"
                        else []
                    ),
                    commit=False,
                )
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

    def _sync_answered_questions_to_confirmed_brief(
        self, interaction: QuestionInteraction
    ) -> None:
        """Merge answered/skipped questions from an interaction into confirmed_brief.

        This makes subsequent planner runs treat ask_user answers as authoritative
        confirmed decisions, so the same questions are not asked again.
        """
        # Acceptance answers are observations tied to an AC and artifact
        # revision, not durable project decisions. They live in the evidence
        # ledger and must not leak into every downstream Agent's context.
        if interaction.stage == "acceptance":
            return
        project = self.session.get(Project, interaction.project_id)
        if project is None:
            return
        questions = self.get_questions_for_interaction(interaction.id)
        if not questions:
            return

        ctx = dict(project.context) if isinstance(project.context, dict) else {}
        brief = ctx.get("confirmed_brief")
        brief = dict(brief) if isinstance(brief, dict) else {}
        decisions = list(brief.get("decisions", []))
        existing_indexes = {
            str(d.get("key") or "").strip().lower(): index
            for index, d in enumerate(decisions)
            if isinstance(d, dict) and str(d.get("key") or "").strip()
        }

        updated = False
        for q in questions:
            if q.status not in ("answered", "skipped"):
                continue
            key = (q.decision_key or q.question_id or "").strip().lower()
            if not key:
                continue
            qa = self.get_question_answer(q.id)
            # Coordinator policies such as "retry the requesting Agent" or
            # "route upstream" are one-shot workflow instructions.  They are
            # not answers to the underlying question and must never become a
            # durable confirmed fact; otherwise the next identical unresolved
            # blocker is suppressed as if the prerequisite had been supplied.
            if qa and any(
                str(ref).startswith("policy:") for ref in (qa.source_refs or [])
            ):
                continue
            if q.status == "skipped":
                answer = q.default_assumption or "(跳过)"
            else:
                answer = (qa.answer_label or qa.answer or "") if qa else ""
            if not answer:
                continue
            is_secret = q.sensitivity == "secret"
            safe_answer = (
                f"已提供（env:{q.secret_env_var}）"
                if is_secret and q.secret_env_var
                else answer
            )
            decision = {
                "key": key,
                "question_id": q.question_id,
                "question": q.question,
                "answer": safe_answer,
                "status": "confirmed",
                "source": qa.answer_source if qa else "user",
                "stage": interaction.stage,
                "round": interaction.origin_round,
            }
            existing_index = existing_indexes.get(key)
            if existing_index is None:
                decisions.append(decision)
                existing_indexes[key] = len(decisions) - 1
            else:
                # A later explicit answer supersedes the current value for the
                # stable key. ProjectContextRecord retains the previous version.
                decisions[existing_index] = decision
            self.create_context_record(
                interaction.project_id,
                record_type="capability" if is_secret else "decision",
                record_key=key,
                value=(
                    {
                        "available": True,
                        "secret_ref": f"env:{q.secret_env_var}",
                        "question_id": q.question_id,
                        # A credential was requested to unblock this concrete
                        # Agent turn.  Persist availability for reuse, but do
                        # not broadcast an unrelated secret capability fact to
                        # every later planner and quality Agent.
                        "applies_to": list(
                            dict.fromkeys(
                                [interaction.agent, interaction.resume_state]
                            )
                        ),
                    }
                    if is_secret
                    else {
                        "question_id": q.question_id,
                        "question": q.question,
                        "answer": answer,
                        "status": "confirmed",
                        "stage": interaction.stage,
                        "round": interaction.origin_round,
                    }
                ),
                source_type=(qa.answer_source if qa else "user"),
                source_ref=interaction.id,
                sensitivity="secret_ref" if is_secret else "internal",
                commit=False,
            )
            updated = True

        if updated:
            brief["decisions"] = decisions
            ctx["confirmed_brief"] = brief
            project.context = ctx
            self.session.add(project)

    async def resolve_question_interaction(self, interaction_id, commit=True) -> QuestionInteraction | None:
        interaction = self.session.get(QuestionInteraction, interaction_id)
        if interaction is None:
            return None
        interaction.status = "answered"
        interaction.answered_at = _now()
        answer_sources = {
            answer.answer_source
            for question in self.get_questions_for_interaction(interaction.id)
            if (answer := self.get_question_answer(question.id)) is not None
        }
        if len(answer_sources) == 1:
            interaction.resolution_source = next(iter(answer_sources))
        elif answer_sources:
            interaction.resolution_source = "mixed"
        self._sync_answered_questions_to_confirmed_brief(interaction)
        if commit:
            self.session.commit()
        return interaction

    def expire_question_interaction_for_correction(
        self,
        interaction_id: str,
        *,
        commit: bool = True,
    ) -> QuestionInteraction | None:
        """Expire a question whose premise was superseded by user correction.

        A correction is not an answer to the old question.  Keeping that
        interaction pending after routing to an upstream stage lets the stale
        question resurface and can trap the project in repeated clarification.
        """
        interaction = self.session.get(QuestionInteraction, interaction_id)
        if interaction is None:
            return None
        interaction.status = "expired"
        for question in self.get_questions_for_interaction(interaction.id):
            if question.status == "pending":
                question.status = "expired"

        project = self.require_project(interaction.project_id)
        context = dict(project.context)
        if context.get("_pending_question_interaction_id") == interaction.id:
            context.pop("_pending_question_interaction_id", None)
            project.context = context
            self.session.add(project)
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

    def expire_superseded_question_interactions(
        self,
        project_id: str,
        *,
        agent: str,
        resume_state: str,
        completed_task_id: str,
    ) -> int:
        """Expire older unanswered questions superseded by a successful rerun.

        A service restart can interrupt Coordinator mediation while the stage is
        retried independently.  Once a newer task for that same Agent/state
        completes, its old questions are no longer actionable and must never be
        surfaced by a later, unrelated transition.
        """
        interactions = self.session.exec(
            select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.agent == agent,
                QuestionInteraction.resume_state == resume_state,
                QuestionInteraction.status == "pending",
                QuestionInteraction.origin_task_id != completed_task_id,
            )
        ).all()
        if not interactions:
            return 0
        interaction_ids = {item.id for item in interactions}
        approval_ids = {item.approval_id for item in interactions if item.approval_id}
        for item in interactions:
            item.status = "expired"
        for approval in self._pending_approvals(project_id):
            payload = approval.payload or {}
            if (
                approval.id in approval_ids
                or payload.get("question_interaction_id") in interaction_ids
            ):
                approval.status = "expired"
        project = self.require_project(project_id)
        context = dict(project.context)
        if context.get("_pending_question_interaction_id") in interaction_ids:
            context.pop("_pending_question_interaction_id", None)
            project.context = context
            self.session.add(project)
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
                "sensitivity": q.sensitivity,
                "secret_env_var": q.secret_env_var,
                "answer": qa.answer if qa else None,
                "answer_label": qa.answer_label if qa else None,
                "skipped": qa.skipped if qa else False,
                "answer_source": qa.answer_source if qa else None,
                "source_refs": qa.source_refs if qa else [],
            })
        return {
            "interaction_id": interaction.id, "project_id": interaction.project_id,
            "origin_task_id": interaction.origin_task_id, "origin_round": interaction.origin_round,
            "resume_state": interaction.resume_state, "agent": interaction.agent,
            "stage": interaction.stage, "batch_key": interaction.batch_key,
            "status": interaction.status, "approval_id": interaction.approval_id,
            "requested_to": interaction.requested_to,
            "escalated_to_user": interaction.escalated_to_user,
            "resolution_source": interaction.resolution_source,
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
        """Persist an incoming Feishu event for audit and debugging.

        This is intentionally lightweight: it records metadata and a truncated
        text payload so we can reconstruct what the bot saw and why an event
        was dropped or processed.
        """
        MAX_TEXT_LEN = 2000
        truncated_text = text[:MAX_TEXT_LEN] if text else None
        log = FeishuEventLog(
            open_id=open_id,
            chat_id=chat_id,
            chat_type=chat_type,
            event_type=event_type,
            event_id=event_id,
            message_id=message_id,
            text=truncated_text,
            action=action,
            feishu_created_at=feishu_created_at,
            status=status,
            detail=detail,
            created_at=_now(),
        )
        self.session.add(log)
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
