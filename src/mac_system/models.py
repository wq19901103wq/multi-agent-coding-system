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
    # Legacy columns retained for in-place compatibility with existing SQLite
    # databases. They no longer drive routing, workflow, API output, or cards.
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
    # ``None`` means no provider has reported a verifiable monetary amount.
    # Never present an unknown native Codex cost as a real $0 charge.
    total_cost_usd: Optional[float] = None
    bot_account_id: str = SQLField(default="default", index=True)
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
    # The visible request is retained only so a process restart can start a
    # fresh recovery turn in the same Codex thread. An in-flight Codex turn
    # itself cannot be reattached after its app-server process exits.
    last_user_request: Optional[str] = None
    last_request_origin: str = SQLField(default="human", index=True)
    last_parent_decision_id: Optional[str] = SQLField(default=None, index=True)
    active_history_round_id: Optional[str] = SQLField(default=None, index=True)
    restart_recovery_pending: bool = SQLField(default=False, index=True)
    provider_handoff: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectHistoryRound(SQLModel, table=True):
    """User-visible management history for one project turn.

    This projection intentionally excludes reasoning, tool calls, diffs and
    child-Agent conversations. It survives provider thread replacement and
    gives the isolated Project Owner the same instruction/result history a
    human project owner could have read in cards.
    """

    __tablename__ = "project_history_rounds"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "sequence", name="uix_project_history_round_sequence"
        ),
    )

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    sequence: int = SQLField(index=True)
    native_thread_id: Optional[str] = SQLField(default=None, index=True)
    native_turn_id: Optional[str] = SQLField(default=None, index=True)
    request_origin: str = SQLField(default="human", index=True)
    parent_decision_id: Optional[str] = SQLField(default=None, index=True)
    user_instructions: list = SQLField(default_factory=list, sa_column=Column(JSON))
    report_title: str = ""
    report_content: str = ""
    report_status: str = SQLField(default="pending", index=True)
    report_ref: Optional[str] = SQLField(default=None, index=True)
    source: str = SQLField(default="native_turn", index=True)
    started_at: datetime = SQLField(default_factory=datetime.utcnow, index=True)
    completed_at: Optional[datetime] = SQLField(default=None, index=True)
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
    request_item_id: Optional[str] = SQLField(default=None, index=True)
    card_message_id: Optional[str] = None
    selected_options: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    # Compatibility link for question cards created before QuestionInteraction
    # became the lifecycle source of truth.
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


class ProjectDecision(SQLModel, table=True):
    """A user-visible Owner decision and its execution lifecycle."""

    __tablename__ = "project_decisions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    request_item_id: Optional[str] = SQLField(default=None, index=True)
    native_turn_id: Optional[str] = SQLField(default=None, index=True)
    parent_decision_id: Optional[str] = SQLField(default=None, index=True)
    decision_type: str = SQLField(default="question", index=True)
    question: str
    options: list = SQLField(default_factory=list, sa_column=Column(JSON))
    decision: str
    rationale: str
    impact: str = ""
    reversible: bool = True
    confidence: str = "medium"
    answer_payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    context_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    # Exact human-rule or human-source refs that authorized this decision.
    # ``context_refs`` remains the broader audit context and is not authority.
    authority_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    actor: str = "project_owner"
    actor_model: Optional[str] = None
    actor_usage: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    actor_duration_seconds: Optional[float] = None
    status: str = SQLField(default="applied", index=True)
    execution_status: str = SQLField(default="not_applicable", index=True)
    execution_rounds: int = 0
    acceptance_criteria: list = SQLField(default_factory=list, sa_column=Column(JSON))
    result_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    card_message_id: Optional[str] = SQLField(default=None, index=True)
    user_override: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)
    overridden_at: Optional[datetime] = None


class ProjectPolicy(SQLModel, table=True):
    """A compact user principle with traceable human source context.

    Concrete instructions and choices remain in project history.  This table
    keeps only explicit durable principles or conservative generalizations
    supported by multiple human sources; Agent reports are never authority.
    """

    __tablename__ = "project_policies"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    kind: str = SQLField(default="principle", index=True)
    category: str = SQLField(default="general", index=True)
    content: str
    strength: str = SQLField(default="default", index=True)
    evidence: str
    source_ref: str = SQLField(index=True)
    source_actor: str = SQLField(default="human", index=True)
    source_time: Optional[datetime] = SQLField(default=None, index=True)
    source_context: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    status: str = SQLField(default="active", index=True)
    supersedes_id: Optional[str] = SQLField(default=None, index=True)
    supersedes_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectOwnerReview(SQLModel, table=True):
    """Durable audit of every isolated Project Owner report review."""

    __tablename__ = "project_owner_reviews"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    request_item_id: str = SQLField(index=True)
    native_turn_id: Optional[str] = SQLField(default=None, index=True)
    request_origin: str = SQLField(default="human", index=True)
    parent_decision_id: Optional[str] = SQLField(default=None, index=True)
    outcome: str = SQLField(index=True)
    decision: str = ""
    rationale: str = ""
    impact: str = ""
    confidence: str = "low"
    continue_execution: bool = False
    memory_updates: list = SQLField(default_factory=list, sa_column=Column(JSON))
    policy_updates: list = SQLField(default_factory=list, sa_column=Column(JSON))
    context_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    authority_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    actor_model: Optional[str] = None
    actor_usage: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    actor_duration_seconds: Optional[float] = None
    card_message_id: Optional[str] = SQLField(default=None, index=True)
    status: str = SQLField(default="completed", index=True)
    input_payload: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    last_error: Optional[str] = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class ProjectOwnerState(SQLModel, table=True):
    """Project-scoped switch for the optional Owner proxy."""

    __tablename__ = "project_owner_states"

    project_id: str = SQLField(
        foreign_key="projects.id", primary_key=True, index=True
    )
    enabled: bool = SQLField(default=True, index=True)
    owner_open_id: Optional[str] = SQLField(default=None, index=True)
    version: int = 1
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class UserOwnerProfile(SQLModel, table=True):
    """User-level explicit preferences available to isolated Owner reviews.

    Entries are evidence records, not a shared conversation transcript.  They
    contain only preferences the user stated directly, with source/time
    metadata so a later Owner can handle conflicts without importing another
    project's content.
    """

    __tablename__ = "user_owner_profiles"

    open_id: str = SQLField(primary_key=True, index=True)
    bot_account_id: str = SQLField(default="default", index=True)
    preferences: list = SQLField(default_factory=list, sa_column=Column(JSON))
    version: int = 1
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class UserOwnerMemory(SQLModel, table=True):
    """One evidence-backed preference or delegation rule for Project Owner.

    Global memories may cross projects. Project-scoped memories never do.
    One-off answers remain in the decision ledger and are not stored here.
    """

    __tablename__ = "user_owner_memories"

    id: str = SQLField(primary_key=True)
    # Stable human identity shared by every Feishu bot account. ``open_id``
    # remains as source provenance because Feishu scopes it to one app.
    human_id: str = SQLField(default="", index=True)
    bot_account_id: str = SQLField(default="default", index=True)
    open_id: str = SQLField(index=True)
    project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", index=True
    )
    # ``project_id`` is the scope boundary and is intentionally NULL for a
    # global policy. Keep the evidence source separately so cross-project
    # Owner context never receives an unattributed preference.
    source_project_id: Optional[str] = SQLField(
        default=None, foreign_key="projects.id", index=True
    )
    scope: str = SQLField(default="global", index=True)
    category: str = SQLField(default="general", index=True)
    content: str
    strength: str = SQLField(default="default", index=True)
    confidence: str = SQLField(default="medium", index=True)
    # Human-authored rules and repeated choices have different authority.
    # Hypotheses are deliberately never persisted as Owner memory.
    learning_type: str = SQLField(default="explicit", index=True)
    reinforcement_count: int = 1
    evidence: str
    source_ref: str = SQLField(index=True)
    source_refs: list = SQLField(default_factory=list, sa_column=Column(JSON))
    status: str = SQLField(default="active", index=True)
    supersedes_id: Optional[str] = SQLField(default=None, index=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


class UserOwnerSource(SQLModel, table=True):
    """One contextual human event available to Owner only through retrieval.

    ``content`` is always the human-authored authority.  The surrounding
    public question/result is retained only to resolve references such as
    "继续", "1" or "不是这个"; it can explain the human event but can never
    itself become preference evidence.  The archive is deliberately not
    injected into every Owner turn.
    """

    __tablename__ = "user_owner_sources"
    __table_args__ = (
        UniqueConstraint("open_id", "source_ref", name="uix_owner_source_ref"),
    )

    id: str = SQLField(primary_key=True)
    human_id: str = SQLField(default="", index=True)
    bot_account_id: str = SQLField(default="default", index=True)
    open_id: str = SQLField(index=True)
    provider: str = SQLField(default="mac", index=True)
    kind: str = SQLField(default="human_input", index=True)
    source_ref: str = SQLField(index=True)
    source_time: str = SQLField(default="", index=True)
    session_ref: str = SQLField(default="", index=True)
    turn_ref: str = SQLField(default="", index=True)
    project_ref: str = SQLField(default="", index=True)
    context_before: str = ""
    context_after: str = ""
    content: str
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class UserIdentityLink(SQLModel, table=True):
    """Map one bot-scoped Feishu ``open_id`` to a stable human identity."""

    __tablename__ = "user_identity_links"
    __table_args__ = (
        UniqueConstraint(
            "bot_account_id", "open_id", name="uix_user_identity_bot_open"
        ),
    )

    id: str = SQLField(primary_key=True)
    human_id: str = SQLField(index=True)
    bot_account_id: str = SQLField(default="default", index=True)
    open_id: str = SQLField(index=True)
    feishu_user_id: Optional[str] = SQLField(default=None, index=True)
    feishu_union_id: Optional[str] = SQLField(default=None, index=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)


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
    bot_account_id: str = SQLField(default="default", index=True)
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
    view_state: Optional[str] = None
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
    bot_account_id: str = SQLField(default="default", index=True)
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
    bot_account_id: str = SQLField(default="default", index=True)
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
    bot_account_id: str = "default"
    creator_open_id: Optional[str] = None
    feishu_chat_id: Optional[str] = None
    feishu_chat_type: str = "p2p"
    bound_directory: Optional[str] = None
    confirmed_brief: Optional[dict] = None


class ProjectOut(BaseModel):
    id: str
    name: Optional[str]
    description: Optional[str]
    status: str
    execution_status: str
    bot_account_id: str = "default"
    waiting_reason: Optional[str]
    waiting_since: Optional[datetime]
    workspace_path: str
    bound_directory: Optional[str]
    total_cost_usd: Optional[float]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
