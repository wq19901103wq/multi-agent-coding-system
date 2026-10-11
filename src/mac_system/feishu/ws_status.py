"""Cross-process Feishu WebSocket connection status.

The lark WS client lives in a per-account child process while the API process
serves /health. A tiny JSON status file per account is the narrow channel
between them: the child process rewrites it on connection transitions and the
API side reads it to classify each account as connected, reconnecting or
unknown. The file is advisory only; delivery correctness never depends on it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# A status file untouched for longer than this means the account process is
# gone or its poller is wedged; report "unknown" instead of a stale state.
STALE_AFTER_SECONDS = 120.0

# Reconnects after an offline window shorter than this send no notice: brief
# DarkWake flaps would otherwise spam every active chat.
OFFLINE_NOTICE_MINUTES = 15.0

# Repo-root state directory shared with other MAC runtime files.
STATUS_DIR = Path(__file__).resolve().parents[3] / ".mac"


def status_path(account_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account_id)
    return STATUS_DIR / f"ws_status_{safe}.json"


def write_status(
    account_id: str,
    *,
    connected: bool,
    disconnected_since: float | None = None,
    connected_since: float | None = None,
    pid: int | None = None,
) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "account_id": account_id,
        "connected": connected,
        "disconnected_since": disconnected_since,
        "connected_since": connected_since,
        "pid": pid,
        "updated_at": time.time(),
    }
    path = status_path(account_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_status(account_id: str) -> dict[str, Any] | None:
    try:
        raw = status_path(account_id).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def remove_status(account_id: str) -> None:
    try:
        status_path(account_id).unlink()
    except OSError:
        pass


def classify(payload: dict[str, Any] | None, *, now: float | None = None) -> str:
    """connected / reconnecting / unknown from one status payload."""
    if payload is None:
        return "unknown"
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return "unknown"
    if (now if now is not None else time.time()) - updated_at > STALE_AFTER_SECONDS:
        return "unknown"
    return "connected" if payload.get("connected") else "reconnecting"


def list_statuses(*, now: float | None = None) -> dict[str, str]:
    """Map every known account id to its current classification."""
    try:
        names = sorted(STATUS_DIR.glob("ws_status_*.json"))
    except OSError:
        return {}
    statuses: dict[str, str] = {}
    for name in names:
        account_id = name.stem[len("ws_status_"):]
        try:
            payload = json.loads(name.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("account_id"), str):
            account_id = payload["account_id"]
        statuses[account_id] = classify(payload if isinstance(payload, dict) else None, now=now)
    return statuses
