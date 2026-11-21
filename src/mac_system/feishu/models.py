from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FeishuEvent:
    """Generic Feishu callback event."""

    type: str
    open_id: str | None
    payload: dict[str, Any]
    raw: dict[str, Any]


@dataclass
class ButtonAction:
    """Parsed card button click event."""

    open_id: str
    approval_id: str
    action: str
    state: str
    decision_id: str | None
    option_key: str | None
    extras: dict[str, Any]
    chat_id: str | None = None
    chat_type: str = "p2p"
    message_id: str | None = None
    created_at: datetime | None = None
    user_id: str | None = None
    union_id: str | None = None


@dataclass
class MessageAttachment:
    """A resource belonging to the original incoming Feishu message."""

    key: str
    kind: str
    name: str


@dataclass
class TextMessage:
    """Parsed incoming message, including files and images."""

    open_id: str
    message_id: str
    text: str
    is_command: bool
    parent_id: str | None = None
    chat_id: str | None = None
    chat_type: str = "p2p"
    sender_type: str = "user"
    is_at_bot: bool = False
    mentions: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None
    user_id: str | None = None
    union_id: str | None = None
    attachments: list[MessageAttachment] = field(default_factory=list)
