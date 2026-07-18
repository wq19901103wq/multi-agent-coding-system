from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Field as SQLField, SQLModel, JSON, Column


class TaskType(str, Enum):
    FEATURE = "feature"
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
    current_state: str
    previous_state: Optional[str] = None
    workspace_path: str
    bound_directory: Optional[str] = SQLField(default=None, nullable=True, index=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)
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
    status: str = SQLField(default="pending")  # pending, running, completed, failed, cancelled
    worktree_path: Optional[str] = None
    input_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    output_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    error_message: Optional[str] = None
    retry_count: int = SQLField(default=0)
    cost_usd: Optional[float] = None
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


class ProjectOut(BaseModel):
    id: str
    name: Optional[str]
    task_type: str
    description: Optional[str]
    status: str
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
