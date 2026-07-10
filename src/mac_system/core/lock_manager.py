"""Per-project asyncio locks for orchestrator concurrency control."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


class ProjectLockManager:
    """Manage asyncio locks keyed by project_id and event loop.

    Locks are tied to the event loop that acquires them.  This avoids issues
    when the same process is used by multiple event loops (e.g. FastAPI
    TestClient or multi-threaded servers) while still serializing ticks for
    the same project within a single loop.
    """

    def __init__(self) -> None:
        self._locks: dict[int, dict[str, asyncio.Lock]] = {}
        self._guard = threading.Lock()

    async def acquire(self, project_id: str) -> asyncio.Lock:
        """Return the lock for ``project_id`` on the current loop."""
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        with self._guard:
            locks = self._locks.setdefault(loop_id, {})
            if project_id not in locks:
                locks[project_id] = asyncio.Lock()
            return locks[project_id]

    async def run_locked(self, project_id: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` while holding the project lock."""
        lock = await self.acquire(project_id)
        async with lock:
            return await coro


_default_lock_manager = ProjectLockManager()


def get_default_lock_manager() -> ProjectLockManager:
    """Return the module-level shared lock manager."""
    return _default_lock_manager
