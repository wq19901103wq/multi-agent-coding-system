from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Field as SQLField, SQLModel, JSON, Column


class TaskType(str, Enum):
    FEATURE = "feature"
    ITERATION = "iteration"
    BUG_FIX = "bug_fix"
    DOCUMENTATION = "documentation"
    REFACTOR = "refactor"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HumanConfirmation(str, Enum):
    CONFIRM = "confirm"
    REJECT = "reject"


# SQLModel tables

class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = SQLField(primary_key=True)
    name: Optional[str] = None
    task_type: str
    description: Optional[str] = None
    status: str = SQLField(default=ProjectStatus.ACTIVE.value)
    # ``status`` is the durable lifecycle (active/completed/failed).  An active
    # project can still be waiting for a user or an external observation.  Keep
    # that execution posture separate so schedulers do not repeatedly wake an
    # LLM merely because a human has not yet performed a manual check.
    execution_status: str = SQLField(default="running", index=True)
    waiting_reason: Optional[str] = SQLField(default=None, nullable=True)
    waiting_since: Optional[datetime] = SQLField(default=None, nullable=True)
    current_state: str
    previous_state: Optional[str] = None
    workspace_path: str
    bound_directory: Optional[str] = SQLField(default=None, nullable=True, index=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)
    context_version: int = SQLField(default=1)
    context: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    approval_config: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    total_cost_usd: Optional[float] = SQLField(default=0.0, nullable=True)
    feishu_chat_id: Optional[str] = SQLField(default=None, nullable=True)
    feishu_chat_type: str = SQLField(default="p2p", nullable=True)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    agent_type: str
    state: str
    round_number: int = SQLField(default=1)
    attempt_number: int = SQLField(default=1)
    context_version: int = SQLField(default=1)
    attempt_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    superseded_by_task_id: Optional[str] = SQLField(
        default=None, foreign_key="tasks.id", nullable=True
    )
    cancellation_reason: Optional[str] = SQLField(default=None, nullable=True)
    status: str = SQLField(
        default="pending"
    )  # pending, running, completed, failed, stale, cancelled
    worktree_path: Optional[str] = None
    input_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    output_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    error_message: Optional[str] = None
    retry_count: int = SQLField(default=0)
    cost_usd: Optional[float] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class CoordinatorSession(SQLModel, table=True):
    """Durable identity of the root Coordinator's native provider thread."""

    __tablename__ = "coordinator_sessions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(
        foreign_key="projects.id", unique=True, index=True
    )
    provider: str = SQLField(default="codex", index=True)
    native_thread_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    active_turn_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    status: str = SQLField(default="idle", index=True)
    event_cursor: int = SQLField(default=0)
    last_error: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class CoordinatorToolCall(SQLModel, table=True):
    """Idempotency ledger for one provider-issued MAC host tool call."""

    __tablename__ = "coordinator_tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "native_thread_id",
            "native_turn_id",
            "native_call_id",
            name="uix_coordinator_native_tool_call",
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    coordinator_session_id: str = SQLField(
        foreign_key="coordinator_sessions.id", index=True
    )
    native_thread_id: str = SQLField(index=True)
    native_turn_id: str = SQLField(index=True)
    native_call_id: str = SQLField(index=True)
    tool_name: str = SQLField(index=True)
    arguments: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    status: str = SQLField(default="pending", index=True)
    result: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    error_message: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class CoordinatorAgentSession(SQLModel, table=True):
    """One dynamically spawned coding-agent session owned by a Coordinator."""

    __tablename__ = "coordinator_agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "task_name", name="uix_coordinator_agent_task_name"
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    coordinator_session_id: str = SQLField(
        foreign_key="coordinator_sessions.id", index=True
    )
    task_id: Optional[str] = SQLField(
        default=None, foreign_key="tasks.id", nullable=True, index=True
    )
    task_name: str = SQLField(index=True)
    provider: str = SQLField(index=True)
    access: str = SQLField(default="read_only", index=True)
    native_session_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    status: str = SQLField(default="pending", index=True)
    objective: str
    latest_message: Optional[str] = SQLField(default=None, nullable=True)
    result: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    worktree_path: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class TaskFailureReport(SQLModel, table=True):
    """Append-only evidence explaining why an Agent attempt was rejected.

    The report deliberately does not enumerate every possible failure.  Its
    purpose is to carry raw, structured evidence back to the actor that can
    resolve it and to let the Coordinator detect a no-progress retry loop.
    """

    __tablename__ = "task_failure_reports"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    task_id: str = SQLField(foreign_key="tasks.id", index=True)
    agent_type: str = SQLField(index=True)
    state: str = SQLField(index=True)
    attempt_number: int = SQLField(default=1)
    fingerprint: str = SQLField(index=True)
    summary: str
    evidence: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    suggested_resolver: str = SQLField(default="agent")
    status: str = SQLField(default="open", index=True)  # open / resolved / superseded / escalated
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = SQLField(default=None, nullable=True)


class AcceptanceEvidence(SQLModel, table=True):
    """Versioned evidence ledger for one acceptance criterion."""

    __tablename__ = "acceptance_evidence"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "acceptance_criterion_id",
            "version",
            name="uix_acceptance_evidence_version",
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    acceptance_criterion_id: str = SQLField(index=True)
    version: int = SQLField(default=1)
    verdict: str = SQLField(index=True)  # pass / fail / partial / not_verified / unknown
    verification_method: str = SQLField(default="automated")  # automated / manual / external
    evidence_scope: str = SQLField(default="")
    environment: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    evidence: list = SQLField(default_factory=list, sa_column=Column(JSON))
    limitations: list = SQLField(default_factory=list, sa_column=Column(JSON))
    source_type: str = SQLField(default="system")  # user / tester / reviewer / evaluator / runtime
    source_ref: Optional[str] = SQLField(default=None, nullable=True)
    artifact_revision: str = SQLField(default="")
    status: str = SQLField(default="active", index=True)  # active / superseded / stale
    supersedes_id: Optional[str] = SQLField(
        default=None, foreign_key="acceptance_evidence.id", nullable=True
    )
    observed_at: datetime = SQLField(default_factory=datetime.utcnow)
    valid_until: Optional[datetime] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectContextRecord(SQLModel, table=True):
    """Versioned project knowledge used to build bounded agent context."""

    __tablename__ = "project_context_records"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "record_key", "version", name="uix_project_context_version"
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    record_type: str = SQLField(index=True)  # requirement / decision / observation / hypothesis / capability
    record_key: str = SQLField(index=True)
    value: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    version: int = SQLField(default=1)
    status: str = SQLField(default="active")  # active / superseded / revoked
    source_type: str = SQLField(default="system")  # user / agent / system / artifact / runtime
    source_ref: Optional[str] = SQLField(default=None, nullable=True)
    scope_type: str = SQLField(default="project")  # project / work_item / session
    scope_ref: Optional[str] = SQLField(default=None, nullable=True)
    confidence: str = SQLField(default="confirmed")  # confirmed / inferred / unverified
    sensitivity: str = SQLField(default="internal")  # public / internal / secret_ref
    supersedes_id: Optional[str] = SQLField(
        default=None, foreign_key="project_context_records.id", nullable=True
    )
    valid_from: datetime = SQLField(default_factory=datetime.utcnow)
    valid_until: Optional[datetime] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class FeedbackPacket(SQLModel, table=True):
    __tablename__ = "feedback_packets"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    source_stage: str
    source_task_id: Optional[str] = SQLField(foreign_key="tasks.id", nullable=True)
    source_round: int = SQLField(default=1)
    kind: str = SQLField(default="quality_failure")  # quality_failure, user_revision, acceptance_rejection
    failure_class: Optional[str] = None
    summary: str
    blocking: bool = SQLField(default=True)
    recommended_rework_target: Optional[str] = None
    evidence: list = SQLField(default_factory=list, sa_column=Column(JSON))
    user_comment: Optional[str] = None
    status: str = SQLField(default="open")  # open, resolved, disputed
    resolved_by_task_id: Optional[str] = SQLField(foreign_key="tasks.id", nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class PendingUserInput(SQLModel, table=True):
    __tablename__ = "pending_user_inputs"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    sender_id: Optional[str] = None
    source_chat_id: Optional[str] = None
    observed_state: str
    content: str
    kind: str = SQLField(default="question")  # scope_change, correction, question
    recommended_rework_target: Optional[str] = None
    status: str = SQLField(default="pending")  # pending, consumed
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    consumed_by_task_id: Optional[str] = SQLField(foreign_key="tasks.id", nullable=True)


class QuestionInteraction(SQLModel, table=True):
    """A batch of questions an agent asks the user mid-turn (unified ask_user)."""

    __tablename__ = "question_interactions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    origin_task_id: Optional[str] = SQLField(foreign_key="tasks.id", nullable=True)
    origin_round: int = SQLField(default=1)
    resume_state: str
    agent: str
    stage: Optional[str] = None
    batch_key: str
    status: str = SQLField(default="pending")  # pending, answered, expired
    approval_id: Optional[str] = SQLField(foreign_key="approvals.id", nullable=True)
    requested_to: str = SQLField(default="coordinator")  # coordinator / user
    escalated_to_user: bool = SQLField(default=False)
    resolution_source: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    answered_at: Optional[datetime] = None


class Question(SQLModel, table=True):
    """A single question inside a QuestionInteraction."""

    __tablename__ = "questions"

    id: str = SQLField(primary_key=True)
    interaction_id: str = SQLField(foreign_key="question_interactions.id", index=True)
    question_id: str
    decision_key: Optional[str] = SQLField(default=None, nullable=True, index=True)
    kind: str = SQLField(default="blocking")  # blocking / decision / non_blocking
    question: str
    reason: str = SQLField(default="")
    allow_free_text: bool = SQLField(default=False)
    options: list = SQLField(default_factory=list, sa_column=Column(JSON))
    multiple: bool = SQLField(default=False)
    default_assumption: Optional[str] = None
    recommended_option: Optional[str] = None
    affected_trace_ids: list = SQLField(default_factory=list, sa_column=Column(JSON))
    sensitivity: str = SQLField(default="internal")  # public / internal / secret
    secret_env_var: Optional[str] = SQLField(default=None, nullable=True)
    status: str = SQLField(default="pending")  # pending, answered, skipped
    order_index: int = SQLField(default=0)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class QuestionAnswer(SQLModel, table=True):
    """A user's answer to a single Question."""

    __tablename__ = "question_answers"

    id: str = SQLField(primary_key=True)
    question_id: str = SQLField(foreign_key="questions.id", index=True)
    answer: Optional[str] = None
    answer_label: Optional[str] = None
    skipped: bool = SQLField(default=False)
    answer_source: str = SQLField(default="user")  # user / coordinator / default
    source_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    responded_by: Optional[str] = None
    responded_at: datetime = SQLField(default_factory=datetime.utcnow)


class DefaultAuthorization(SQLModel, table=True):
    """Pre-authorized answers to specific question patterns."""

    __tablename__ = "default_authorizations"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    question_pattern: str
    authorized_option: str
    created_by: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ConversationInteraction(SQLModel, table=True):
    """Conversation-scoped disambiguation when no project is determined."""

    __tablename__ = "conversation_interactions"

    id: str = SQLField(primary_key=True)
    chat_id: str = SQLField(index=True)
    open_id: str = SQLField(index=True)
    source_message_id: Optional[str] = None
    source_text: Optional[str] = None
    candidates: list = SQLField(default_factory=list, sa_column=Column(JSON))
    selected_project_id: Optional[str] = SQLField(default=None, nullable=True)
    status: str = SQLField(default="pending")  # pending, resolved, expired
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None


class ConversationSession(SQLModel, table=True):
    """Durable chat-level state for free-form conversation coordination."""

    __tablename__ = "conversation_sessions"

    id: str = SQLField(primary_key=True)
    chat_id: str = SQLField(unique=True, index=True)
    summary: str = SQLField(default="")
    recent_turns: list = SQLField(default_factory=list, sa_column=Column(JSON))
    active_project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    latest_artifact: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    pending_action_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    status: str = SQLField(default="active")  # active / closed
    version: int = SQLField(default=1)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class PendingConversationAction(SQLModel, table=True):
    """A chat-scoped side-effect proposal awaiting explicit authorization."""

    __tablename__ = "pending_conversation_actions"

    id: str = SQLField(primary_key=True)
    session_id: str = SQLField(foreign_key="conversation_sessions.id", index=True)
    action_type: str  # start_delivery / project_control
    target_project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    source_message_ids: list = SQLField(default_factory=list, sa_column=Column(JSON))
    status: str = SQLField(default="pending")  # pending / processing / resolved / rejected / expired
    idempotency_key: str = SQLField(unique=True, index=True)
    created_by: Optional[str] = None
    resolved_by: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    task_id: Optional[str] = SQLField(foreign_key="tasks.id", nullable=True)
    state: str
    approval_type: str  # confirmation, input
    question: Optional[str] = None
    payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    response: Optional[str] = None
    responded_by: Optional[str] = None
    status: str = SQLField(default="pending")  # pending, responded, expired
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    responded_at: Optional[datetime] = None


class EventLog(SQLModel, table=True):
    __tablename__ = "event_logs"

    id: Optional[int] = SQLField(default=None, primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    level: str
    message: str
    meta: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class NotificationOutbox(SQLModel, table=True):
    """Durable, deduplicated delivery intent for user-visible notifications."""

    __tablename__ = "notification_outbox"

    id: str = SQLField(primary_key=True)
    project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    conversation_id: Optional[str] = SQLField(
        default=None, foreign_key="conversation_sessions.id", nullable=True, index=True
    )
    recipient_id: str
    recipient_type: str = SQLField(default="open_id")
    channel: str = SQLField(default="feishu")
    intent_type: str = SQLField(index=True)  # progress / report / decision / question / receipt
    dedupe_key: str = SQLField(unique=True, index=True)
    payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    # pending / sending / sent / failed(retryable) / terminal_failed
    status: str = SQLField(default="pending", index=True)
    provider_message_id: Optional[str] = SQLField(default=None, nullable=True)
    attempt_count: int = SQLField(default=0)
    last_error: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class CardProjection(SQLModel, table=True):
    """Durable materialized view of one backend object in Feishu.

    NotificationOutbox answers whether a delivery intent was sent.  This
    table answers a different question: which provider card currently
    represents a Project/Approval/Question/notification and which lifecycle
    state/version has actually been rendered.
    """

    __tablename__ = "card_projections"

    id: str = SQLField(primary_key=True)
    projection_key: str = SQLField(unique=True, index=True)
    project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", nullable=True, index=True
    )
    conversation_id: Optional[str] = SQLField(
        default=None, foreign_key="conversation_sessions.id", nullable=True, index=True
    )
    subject_type: str = SQLField(default="notification", index=True)
    subject_id: str = SQLField(index=True)
    card_kind: str = SQLField(default="notice", index=True)
    provider_message_id: Optional[str] = SQLField(default=None, nullable=True, index=True)
    projection_version: int = SQLField(default=1)
    lifecycle_status: str = SQLField(default="published", index=True)
    selected_action: Optional[str] = SQLField(default=None, nullable=True)
    resolved_by: Optional[str] = SQLField(default=None, nullable=True)
    resolved_at: Optional[datetime] = SQLField(default=None, nullable=True)
    resulting_state: Optional[str] = SQLField(default=None, nullable=True)
    last_error: Optional[str] = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectUser(SQLModel, table=True):
    __tablename__ = "project_users"

    project_id: str = SQLField(foreign_key="projects.id", primary_key=True)
    open_id: str = SQLField(primary_key=True)
    role: str = SQLField(default="editor")  # owner / editor / reviewer / viewer
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectChat(SQLModel, table=True):
    __tablename__ = "project_chats"
    __table_args__ = (UniqueConstraint("project_id", "chat_id", name="uix_project_chat"),)

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    chat_id: str = SQLField(index=True)
    chat_type: str = SQLField(default="p2p")
    relation: str = SQLField(default="origin")  # origin / subscribed
    notification_level: str = SQLField(default="all")  # all / approvals / result / none
    created_by: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ChatUserContext(SQLModel, table=True):
    __tablename__ = "chat_user_contexts"

    chat_id: str = SQLField(primary_key=True)
    open_id: str = SQLField(primary_key=True)
    active_project_id: str | None = SQLField(
        foreign_key="projects.id", index=True, nullable=True
    )
    selected_at: datetime = SQLField(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)
    last_event_at: datetime | None = SQLField(default=None, nullable=True)


class ChatDirectoryBinding(SQLModel, table=True):
    """One repository binding per chat, shared by all members of that chat."""

    __tablename__ = "chat_directory_bindings"

    chat_id: str = SQLField(primary_key=True)
    directory: str = SQLField(index=True)
    bound_by: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class PendingIntent(SQLModel, table=True):
    __tablename__ = "pending_intents"

    open_id: str = SQLField(primary_key=True)
    description: Optional[str] = None
    stage: str  # awaiting_description | awaiting_type_confirm
    chat_id: Optional[str] = SQLField(default=None, nullable=True)
    chat_type: str = SQLField(default="p2p", nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProcessedEvent(SQLModel, table=True):
    __tablename__ = "processed_events"

    event_id: str = SQLField(primary_key=True)
    event_type: str  # message | card
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class FeishuEventLog(SQLModel, table=True):
    __tablename__ = "feishu_event_logs"

    id: int | None = SQLField(default=None, primary_key=True)
    open_id: str = SQLField(index=True)
    chat_id: str | None = SQLField(default=None, nullable=True, index=True)
    chat_type: str = SQLField(default="p2p")
    event_type: str = SQLField(index=True)  # message | card
    event_id: str | None = SQLField(default=None, nullable=True, index=True)
    message_id: str | None = SQLField(default=None, nullable=True)
    text: str | None = SQLField(default=None, nullable=True)
    action: str | None = SQLField(default=None, nullable=True)
    feishu_created_at: datetime | None = SQLField(default=None, nullable=True)
    status: str = SQLField(default="received")  # received | duplicate | stale | processed | error
    detail: str | None = SQLField(default=None, nullable=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


# Pydantic API schemas

class ProjectCreate(BaseModel):
    name: Optional[str] = None
    task_type: TaskType
    description: str
    approval_mode: str = "normal"  # strict, normal, night, auto
    creator_open_id: Optional[str] = None
    feishu_chat_id: Optional[str] = None
    feishu_chat_type: str = "p2p"
    bound_directory: Optional[str] = None
    confirmed_brief: Optional[dict] = None


class ProjectOut(BaseModel):
    id: str
    name: Optional[str]
    task_type: str
    description: Optional[str]
    status: str
    execution_status: str
    waiting_reason: Optional[str]
    waiting_since: Optional[datetime]
    current_state: str
    previous_state: Optional[str]
    workspace_path: str
    bound_directory: Optional[str]
    total_cost_usd: Optional[float]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApprovalResponse(BaseModel):
    approval_id: str
    action: str  # confirm, reject, etc.
    message: Optional[str] = None


class UserCommand(BaseModel):
    project_id: str
    command: str  # pause, resume, rollback, status
    message: Optional[str] = None


class RouteDecision(BaseModel):
    task_type: str | None
    initial_state: str | None
    confidence: float
    needs_disambiguation: bool
    reason: str | None = None
