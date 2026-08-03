import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mac_system.core.lock_manager import ProjectLockManager


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_leak_cross_loop_lock():
    manager = ProjectLockManager()
    first = await manager.acquire("project-cancel")
    await first.__aenter__()

    entered = asyncio.Event()

    async def cancelled_waiter() -> None:
        lock = await manager.acquire("project-cancel")
        async with lock:
            entered.set()

    waiter = asyncio.create_task(cancelled_waiter())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await first.__aexit__(None, None, None)

    # Let the orphaned worker acquire and run its compensating release.
    await asyncio.sleep(0.1)
    third = await manager.acquire("project-cancel")
    await asyncio.wait_for(third.__aenter__(), timeout=1.0)
    assert not entered.is_set()
    await third.__aexit__(None, None, None)


def test_project_lock_serializes_different_event_loops():
    manager = ProjectLockManager()
    state = {"active": 0, "max_active": 0}
    state_guard = threading.Lock()

    async def worker():
        lock = await manager.acquire("proj_cross_loop")
        async with lock:
            with state_guard:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.05)
            with state_guard:
                state["active"] -= 1

    def run_worker():
        asyncio.run(worker())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_worker) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert state["max_active"] == 1
