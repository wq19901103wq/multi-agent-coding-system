from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
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


class ProjectOut(BaseModel):
    id: str
    name: Optional[str]
    task_type: str
    description: Optional[str]
    status: str
    current_state: str
    previous_state: Optional[str]
    workspace_path: str
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
