"""Per-project asyncio locks for orchestrator concurrency control."""

from __future__ import annotations

import threading
import asyncio
from typing import Any, Coroutine


class _CrossLoopAsyncLock:
    """Async context adapter around one process-wide threading lock."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._acquired = False

    async def __aenter__(self) -> "_CrossLoopAsyncLock":
        # Cancelling ``asyncio.to_thread(lock.acquire)`` does not cancel the
        # underlying blocking call. It may acquire later after the coroutine
        # has disappeared, leaking the process-wide project lock forever.
        # Shield the acquisition and attach a compensating release when its
        # waiter is cancelled.
        acquisition = asyncio.create_task(asyncio.to_thread(self._lock.acquire))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            def release_orphan(completed: asyncio.Task) -> None:
                if completed.cancelled():
                    return
                try:
                    acquired = bool(completed.result())
                except Exception:
                    return
                if acquired:
                    self._lock.release()

            acquisition.add_done_callback(release_orphan)
            raise
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            self._acquired = False
            self._lock.release()


class ProjectLockManager:
    """Serialize project ticks across every event loop in this process.

    Feishu WebSocket handling and FastAPI control requests run on different
    threads/event loops. Per-loop asyncio locks therefore allowed the same
    project to start duplicate Coder/Reviewer/Acceptance tasks. A threading
    lock is the correct process-wide ownership primitive; the adapter keeps
    acquisition non-blocking for each event loop.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    async def acquire(self, project_id: str) -> _CrossLoopAsyncLock:
        """Return a cross-loop async adapter for ``project_id``."""
        with self._guard:
            lock = self._locks.setdefault(project_id, threading.Lock())
        return _CrossLoopAsyncLock(lock)

    async def run_locked(self, project_id: str, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coro`` while holding the project lock."""
        lock = await self.acquire(project_id)
        async with lock:
            return await coro


_default_lock_manager = ProjectLockManager()


def get_default_lock_manager() -> ProjectLockManager:
    """Return the module-level shared lock manager."""
    return _default_lock_manager
