"""Boundary redaction for conversation memory and user-visible output."""

from __future__ import annotations

import re
import logging
from typing import Any


_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:api[_-]?key|app[_-]?secret|client[_-]?secret|access[_-]?(?:token|key)|"
    r"refresh[_-]?token|password|passwd|secret|token|ticket)\b\s*[=:]\s*)"
    r"([\"']?)([^\s,;\"'}]+)(\2)"
)
_TOKEN_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|ak|rk|pk|Bearer)[-_ ][A-Za-z0-9._-]{16,}"
)


def redact_text(value: str) -> str:
    """Mask likely secret values while retaining names and surrounding context."""
    masked = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
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
