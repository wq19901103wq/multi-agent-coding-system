"""Local runtime-secret storage kept outside conversational database rows."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock


class LocalSecretStore:
    """Persist runtime secrets in a dedicated owner-only file.

    The store is intentionally small and local: MAC is a single-user desktop
    service.  Its purpose is to keep secret values out of QuestionAnswer,
    prompts, exports and ordinary database inspection while preserving them
    across service restarts.  Production can replace this adapter with an OS
    keychain without changing the question protocol.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._memory: dict[str, str] = {}
        self._lock = RLock()

    @staticmethod
    def reference(project_id: str, env_var: str) -> str:
        return f"project:{project_id}:env:{env_var}"

    def set(self, project_id: str, env_var: str, value: str) -> str:
        ref = self.reference(project_id, env_var)
        with self._lock:
            values = self._load()
            values[ref] = value
            self._save(values)
        return ref

    def get(self, project_id: str, env_var: str) -> str | None:
        with self._lock:
            return self._load().get(self.reference(project_id, env_var))

    def _load(self) -> dict[str, str]:
        if self.path is None:
            return dict(self._memory)
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        } if isinstance(payload, dict) else {}

    def _save(self, values: dict[str, str]) -> None:
        if self.path is None:
            self._memory = dict(values)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(values, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temp, 0o600)
        temp.replace(self.path)
        os.chmod(self.path, 0o600)
