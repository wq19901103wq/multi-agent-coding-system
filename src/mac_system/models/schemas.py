"""Pydantic and SQLModel schemas for MAC System."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Column, Field as SQLField, Relationship, SQLModel, JSON


class ProjectStatus(str, Enum):
    """Project-level terminal and active statuses."""

    INIT = "INIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class TaskType(str, Enum):
    """Supported task types."""

    FEATURE = "feature"
    BUG_FIX = "bug_fix"
    REFACTOR = "refactor"
    DOCUMENTATION = "documentation"


class ApprovalMode(str, Enum):
    """Human-in-the-loop approval modes."""

    STRICT = "strict"
    NORMAL = "normal"
    NIGHT = "night"
    AUTO = "auto"


class StateType(str, Enum):
    """Types of states in the state machine."""

    AGENT_EXECUTION = "agent_execution"
    HUMAN_CONFIRMATION = "human_confirmation"
    HUMAN_INPUT = "human_input"
    SYSTEM_ACTION = "system_action"
    TERMINAL = "terminal"
    IDLE = "idle"


class UserDecision(str, Enum):
    """Possible user decisions on confirmation cards."""

    CONFIRM = "confirm"
    REJECT = "reject"
    CLARIFY = "clarify"


class ExecutionStatus(str, Enum):
    """Status of an agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


# ------------------------------
# SQLModel ORM Models
# ------------------------------


class Project(SQLModel, table=True):
    """A project represents a single task workflow."""

    __tablename__ = "projects"

    id: str = SQLField(primary_key=True)
    name: str = SQLField(index=True)
    task_type: str = SQLField(index=True)
    status: str = SQLField(index=True)
    approval_mode: str = SQLField(default="normal")
    workspace_path: str
    repo_url: str | None = None
    branch: str | None = None
    raw_requirement: str
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
    updated_at: datetime = SQLField(default_factory=datetime.utcnow)

    executions: list["Execution"] = Relationship(back_populates="project")
    decisions: list["Decision"] = Relationship(back_populates="project")
    messages: list["Message"] = Relationship(back_populates="project")


class Execution(SQLModel, table=True):
    """Record of a single agent execution."""

    __tablename__ = "executions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    state: str = SQLField(index=True)
    agent_type: str
    stage: str | None = None
    worktree_path: str | None = None
    input_artifact_ids: list[str] | None = SQLField(default=None, sa_column=Column(JSON))
    output_artifact_ids: list[str] | None = SQLField(default=None, sa_column=Column(JSON))
    output_json: dict[str, Any] | None = SQLField(default=None, sa_column=Column(JSON))
    logs: str | None = None
    cost_info: dict[str, Any] | None = SQLField(default=None, sa_column=Column(JSON))
    status: str = SQLField(default="pending")
    retry_count: int = SQLField(default=0)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)

    project: Project = Relationship(back_populates="executions")


class Decision(SQLModel, table=True):
    """Record of a human decision."""

    __tablename__ = "decisions"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    state: str
    presented_summary: str | None = None
    user_decision: str
    user_comment: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)

    project: Project = Relationship(back_populates="decisions")


class Artifact(SQLModel, table=True):
    """A file or document produced by an agent."""

    __tablename__ = "artifacts"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    execution_id: str | None = SQLField(foreign_key="executions.id", index=True)
    artifact_type: str  # code, doc, test, report, schema
    path: str
    content_hash: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class Message(SQLModel, table=True):
    """A Feishu message exchange."""

    __tablename__ = "messages"

    id: str = SQLField(primary_key=True)
    project_id: str | None = SQLField(foreign_key="projects.id", index=True)
    feishu_message_id: str | None = None
    direction: str  # outbound / inbound
    message_type: str  # text, card, action
    content: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)

    project: Project | None = Relationship(back_populates="messages")


class GitOperation(SQLModel, table=True):
    """Record of a git operation."""

    __tablename__ = "git_operations"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    execution_id: str | None = SQLField(foreign_key="executions.id", index=True)
    operation: str  # worktree_add, merge, commit, push
    source_path: str | None = None
    target_path: str | None = None
    result: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


class AgentToolCall(SQLModel, table=True):
    """Audit log of tool calls made by an agent."""

    __tablename__ = "agent_tool_calls"

    id: str = SQLField(primary_key=True)
    project_id: str = SQLField(foreign_key="projects.id", index=True)
    execution_id: str | None = SQLField(foreign_key="executions.id", index=True)
    tool_name: str
    tool_input: str | None = None
    tool_output: str | None = None
    created_at: datetime = SQLField(default_factory=datetime.utcnow)


# ------------------------------
# Pydantic API Models
# ------------------------------


class CreateProjectRequest(BaseModel):
    """Request to create a new project."""

    name: str
    task_type: TaskType = TaskType.FEATURE
    raw_requirement: str
    repo_url: str | None = None
    branch: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.NORMAL


class CreateProjectResponse(BaseModel):
    """Response after creating a project."""

    project_id: str
    status: str
    workspace_path: str
    created_at: datetime


class SubmitDecisionRequest(BaseModel):
    """Request to submit a human decision."""

    state: str
    decision: UserDecision
    comment: str | None = None
    selected_option: str | None = None


class SubmitDecisionResponse(BaseModel):
    """Response after submitting a decision."""

    project_id: str
    previous_state: str
    current_state: str


class UserMessageRequest(BaseModel):
    """Request representing a user message."""

    content: str


class UserMessageResponse(BaseModel):
    """Response to a user message."""

    project_id: str
    reply: str
    state: str


class ProjectStatusResponse(BaseModel):
    """Response for project status query."""

    project_id: str
    name: str
    task_type: str
    status: str
    approval_mode: str
    created_at: datetime
    updated_at: datetime
