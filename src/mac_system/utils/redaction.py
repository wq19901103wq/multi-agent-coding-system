"""Boundary redaction for conversation memory and user-visible output."""

from __future__ import annotations

import re
import logging
from typing import Any


_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:api[_-]?key|app[_-]?secret|client[_-]?secret|access[_-]?(?:token|key)|"
    r"refresh[_-]?token|password|passwd|secret|token|ticket)\b\s*[=:]\s*)"
    r"([\"']?)([^\s,;&\"'}]+)(\2)"
)
_TOKEN_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|ak|rk|pk|Bearer)[-_ ][A-Za-z0-9._-]{16,}"
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(\bauthorization\s*:\s*)(?:bearer\s+)?([^\s\"']+)"
)


def redact_text(value: str) -> str:
    """Mask likely secret values while retaining names and surrounding context."""
    masked = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    masked = _AUTHORIZATION_HEADER.sub(r"\1[REDACTED]", masked)
    return _TOKEN_LIKE.sub("[REDACTED]", masked)


def redact_secrets(value: Any) -> Any:
    """Recursively redact strings without changing the surrounding shape."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


class SecretRedactionFilter(logging.Filter):
    """Redact third-party log messages before any handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def install_global_log_redaction() -> None:
    """Redact every record in a process before any handler can emit it.

    Spawned Feishu workers do not pass through uvicorn's logging.yaml setup,
    and third-party SDKs may attach their own handlers. A record factory is the
    only common boundary shared by all of those handlers.
    """

    current = logging.getLogRecordFactory()
    if getattr(current, "_mac_secret_redaction", False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return record

    redacting_factory._mac_secret_redaction = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(redacting_factory)
