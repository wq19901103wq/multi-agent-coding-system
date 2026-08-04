"""Durable models for MAC's thin Codex host."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import UniqueConstraint
from sqlmodel import Column, Field as SQLField, JSON, SQLModel


class TaskType(str, Enum):
    FEATURE = "feature"
    ITERATION = "iteration"
    BUG_FIX = "bug_fix"
    DOCUMENTATION = "documentation"
    REFACTOR = "refactor"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = SQLField(primary_key=True)
    name: Optional[str] = None
    task_type: str = SQLField(default="feature")
    description: Optional[str] = None
    status: str = SQLField(default="active", index=True)
    execution_status: str = SQLField(default="idle", index=True)
    waiting_reason: Optional[str] = None
    waiting_since: Optional[datetime] = None
    current_state: str = SQLField(default="ACTIVE")
    previous_state: Optional[str] = None
    workspace_path: str
    bound_directory: Optional[str] = SQLField(default=None, index=True)
    context: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    context_version: int = SQLField(default=1)
    approval_config: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    total_cost_usd: Optional[float] = 0.0
    feishu_chat_id: Optional[str] = None
    feishu_chat_type: str = "p2p"
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class Task(SQLModel, table=True):
    """A MAC-owned external coding-agent execution, when one is requested."""

    __tablename__ = "tasks"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    agent_type: str
    state: str = "external_agent"
    status: str = SQLField(default="pending", index=True)
    input_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    output_data: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    worktree_path: Optional[str] = None
    error_message: Optional[str] = None
    cost_usd: Optional[float] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class RootAgentSession(SQLModel, table=True):
    __tablename__ = "root_agent_sessions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", unique=True, index=True)
    provider: str = SQLField(default="codex", index=True)
    native_thread_id: Optional[str] = SQLField(default=None, index=True)
    active_turn_id: Optional[str] = SQLField(default=None, index=True)
    status: str = SQLField(default="idle", index=True)
    event_cursor: int = 0
    last_error: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class DynamicToolCall(SQLModel, table=True):
    __tablename__ = "dynamic_tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "native_thread_id",
            "native_turn_id",
            "native_call_id",
            name="uix_root_agent_native_tool_call",
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    root_agent_session_id: str = SQLField(foreign_key="root_agent_sessions.id", index=True)
    native_thread_id: str = SQLField(index=True)
    native_turn_id: str = SQLField(index=True)
    native_call_id: str = SQLField(index=True)
    tool_name: str = SQLField(index=True)
    arguments: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    status: str = SQLField(default="pending", index=True)
    result: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    error_message: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class AgentSession(SQLModel, table=True):
    """Projection of a MAC-owned non-Codex provider session."""

    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint("project_id", "task_name", name="uix_root_agent_agent_task_name"),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    root_agent_session_id: str = SQLField(foreign_key="root_agent_sessions.id", index=True)
    task_id: Optional[str] = SQLField(default=None, foreign_key="tasks.id", index=True)
    task_name: str = SQLField(index=True)
    provider: str = SQLField(index=True)
    access: str = SQLField(default="read_only", index=True)
    objective: str
    native_session_id: Optional[str] = SQLField(default=None, index=True)
    status: str = SQLField(default="pending", index=True)
    latest_message: Optional[str] = None
    result: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    worktree_path: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class QuestionInteraction(SQLModel, table=True):
    __tablename__ = "question_interactions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    origin_task_id: Optional[str] = SQLField(default=None, foreign_key="tasks.id")
    origin_round: int = 1
    resume_state: str = "ACTIVE"
    agent: str = "root_agent"
    stage: Optional[str] = "conversation"
    batch_key: str
    status: str = SQLField(default="pending", index=True)
    approval_id: Optional[str] = SQLField(default=None, foreign_key="approvals.id")
    requested_to: str = "user"
    escalated_to_user: bool = True
    resolution_source: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    answered_at: Optional[datetime] = None


class Question(SQLModel, table=True):
    __tablename__ = "questions"

    id: str = SQLField(primary_key=True)
    interaction_id: str = SQLField(foreign_key="question_interactions.id", index=True)
    question_id: str
    decision_key: Optional[str] = SQLField(default=None, index=True)
    kind: str = "decision"
    question: str
    reason: str = ""
    allow_free_text: bool = False
    options: list = SQLField(default_factory=list, sa_column=Column(JSON))
    multiple: bool = False
    default_assumption: Optional[str] = None
    recommended_option: Optional[str] = None
    affected_trace_ids: list = SQLField(default_factory=list, sa_column=Column(JSON))
    sensitivity: str = "internal"
    secret_env_var: Optional[str] = None
    status: str = SQLField(default="pending", index=True)
    order_index: int = 0
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class QuestionAnswer(SQLModel, table=True):
    __tablename__ = "question_answers"

    id: str = SQLField(primary_key=True)
    question_id: str = SQLField(foreign_key="questions.id", index=True)
    answer: Optional[str] = None
    answer_label: Optional[str] = None
    skipped: bool = False
    answer_source: str = "user"
    source_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    responded_by: Optional[str] = None
    responded_at: datetime = SQLField(default_factory=datetime.utcnow)


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    task_id: Optional[str] = SQLField(default=None, foreign_key="tasks.id")
    state: str
    approval_type: str
    question: Optional[str] = None
    payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    response: Optional[str] = None
    responded_by: Optional[str] = None
    status: str = SQLField(default="pending", index=True)
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
    __tablename__ = "notification_outbox"

    id: str = SQLField(primary_key=True)
    project_id: Optional[str] = SQLField(default=None, foreign_key="projects.id", index=True)
    conversation_id: Optional[str] = None
    recipient_id: str
    recipient_type: str = "open_id"
    channel: str = "feishu"
    intent_type: str = SQLField(index=True)
    dedupe_key: str = SQLField(unique=True, index=True)
    payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    status: str = SQLField(default="pending", index=True)
    provider_message_id: Optional[str] = None
    attempt_count: int = 0
    last_error: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class CardProjection(SQLModel, table=True):
    __tablename__ = "card_projections"

    id: str = SQLField(primary_key=True)
    projection_key: str = SQLField(unique=True, index=True)
    project_id: Optional[str] = SQLField(default=None, foreign_key="projects.id", index=True)
    conversation_id: Optional[str] = None
    subject_type: str = SQLField(default="notification", index=True)
    subject_id: str = SQLField(index=True)
    card_kind: str = SQLField(default="notice", index=True)
    provider_message_id: Optional[str] = SQLField(default=None, index=True)
    projection_version: int = 1
    lifecycle_status: str = SQLField(default="published", index=True)
    selected_action: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resulting_state: Optional[str] = None
    last_error: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectUser(SQLModel, table=True):
    __tablename__ = "project_users"

    project_id: str = SQLField(foreign_key="projects.id", primary_key=True)
    open_id: str = SQLField(primary_key=True)
    role: str = "editor"
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectChat(SQLModel, table=True):
    __tablename__ = "project_chats"
    __table_args__ = (UniqueConstraint("project_id", "chat_id", name="uix_project_chat"),)

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    chat_id: str = SQLField(index=True)
    chat_type: str = "p2p"
    relation: str = "origin"
    notification_level: str = "all"
    created_by: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ChatUserContext(SQLModel, table=True):
    __tablename__ = "chat_user_contexts"

    chat_id: str = SQLField(primary_key=True)
    open_id: str = SQLField(primary_key=True)
    active_project_id: Optional[str] = SQLField(default=None, foreign_key="projects.id", index=True)
    selected_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)
    last_event_at: Optional[datetime] = None


class ChatDirectoryBinding(SQLModel, table=True):
    __tablename__ = "chat_directory_bindings"

    chat_id: str = SQLField(primary_key=True)
    directory: str = SQLField(index=True)
    bound_by: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class PendingIntent(SQLModel, table=True):
    __tablename__ = "pending_intents"

    open_id: str = SQLField(primary_key=True)
    description: Optional[str] = None
    stage: str
    chat_id: Optional[str] = None
    chat_type: str = "p2p"
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProcessedEvent(SQLModel, table=True):
    __tablename__ = "processed_events"

    event_id: str = SQLField(primary_key=True)
    event_type: str
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class FeishuEventLog(SQLModel, table=True):
    __tablename__ = "feishu_event_logs"

    id: Optional[int] = SQLField(default=None, primary_key=True)
    open_id: str = SQLField(index=True)
    chat_id: Optional[str] = SQLField(default=None, index=True)
    chat_type: str = "p2p"
    event_type: str = SQLField(index=True)
    event_id: Optional[str] = SQLField(default=None, index=True)
    message_id: Optional[str] = None
    text: Optional[str] = None
    action: Optional[str] = None
    feishu_created_at: Optional[datetime] = None
    status: str = "received"
    detail: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectCreate(BaseModel):
    name: Optional[str] = None
    task_type: TaskType = TaskType.FEATURE
    description: str
    approval_mode: str = "normal"
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
