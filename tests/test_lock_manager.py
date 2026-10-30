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


@pytest.mark.asyncio
async def test_try_acquire_does_not_wait_or_release_another_holder():
    manager = ProjectLockManager()
    holder = await manager.acquire("busy-project")

    async def probe(project_id):
        async with manager.try_acquire(project_id) as acquired:
            return acquired

    async with holder:
        assert await asyncio.wait_for(probe("busy-project"), 0.5) is False
        assert await probe("other-project") is True
        assert await probe("busy-project") is False
    assert await probe("busy-project") is True


@pytest.mark.asyncio
async def test_try_acquire_releases_on_exception():
    manager = ProjectLockManager()
    with pytest.raises(RuntimeError, match="failed inside lock"):
        async with manager.try_acquire("project") as acquired:
            assert acquired
            raise RuntimeError("failed inside lock")
    async with manager.try_acquire("project") as acquired:
        assert acquired
        async with manager.try_acquire("project") as nested:
            assert not nested


def test_try_acquire_respects_holder_on_another_event_loop():
    manager = ProjectLockManager()
    entered, release = threading.Event(), threading.Event()

    async def hold():
        lock = await manager.acquire("cross-loop-project")
        async with lock:
            entered.set()
            await asyncio.to_thread(release.wait, 2)

    async def probe():
        async with manager.try_acquire("cross-loop-project") as acquired:
            return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(asyncio.run, hold())
        try:
            assert entered.wait(1)
            assert asyncio.run(probe()) is False
        finally:
            release.set()
            holder.result(timeout=3)
    assert asyncio.run(probe()) is True
