"""Stop a provider and its writers before another provider can take over."""

import asyncio
import os
import signal

import psutil


def snapshot_host_execution() -> dict:
    """Read-only witness for a reset that may outlive its owning MAC worker.

    This is deliberately NOT a kill list: siblings may serve other projects.
    It only permits recovery after all witnessed old execution has exited.
    Native providers spawn isolated groups, so their surviving children keep
    the witness live even when the original provider parent has exited.
    """
    host = psutil.Process()
    host_group = os.getpgrp()
    processes = []
    groups = set()
    for process in [host, *host.children(recursive=True)]:
        try:
            processes.append({"pid": process.pid, "created_at": process.create_time()})
            group = os.getpgid(process.pid)
            if group != host_group:
                groups.add(group)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return {"processes": processes, "groups": sorted(groups)}


def host_execution_has_exited(witness: dict) -> bool:
    """Verify absence only; never signal an old/reused PID or a sibling."""
    processes = witness.get("processes")
    if not isinstance(processes, list) or not processes:
        return False
    for identity in processes:
        try:
            process = psutil.Process(int(identity["pid"]))
            if (
                process.create_time() == float(identity["created_at"])
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                return False
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, KeyError, TypeError, ValueError):
            return False
    for group in witness.get("groups", []):
        if not isinstance(group, int) or group <= 0:
            return False
        try:
            os.killpg(group, 0)  # existence check, not a signal
        except ProcessLookupError:
            continue
        except PermissionError:
            return False
        return False
    return True


async def stop_process_tree(process, *, isolated_group: bool = False) -> None:
    pid = getattr(process, "pid", None)
    children = []
    if isinstance(pid, int):
        try:
            children = psutil.Process(pid).children(recursive=True)
        except psutil.NoSuchProcess:
            pass

    def signal_tree(sig):
        if isolated_group and isinstance(pid, int):
            # The group is allocated with start_new_session at spawn time.
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass
        for child in reversed(children):
            try:
                child.send_signal(sig)
            except psutil.NoSuchProcess:
                pass
        if process.returncode is None:
            try:
                process.terminate() if sig == signal.SIGTERM else process.kill()
            except ProcessLookupError:
                pass

    signal_tree(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        signal_tree(signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 5)
    # The parent can exit before its children, including background shells.
    signal_tree(signal.SIGKILL)
    if children:
        _, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=5)
        if any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in alive):
            raise RuntimeError("Cannot confirm previous provider writers stopped")
    if isolated_group and isinstance(pid, int):
        for _ in range(50):
            live = []
            for proc in psutil.process_iter(["pid", "status"]):
                try:
                    if os.getpgid(proc.pid) == pid and proc.status() != psutil.STATUS_ZOMBIE:
                        live.append(proc.pid)
                except (ProcessLookupError, psutil.NoSuchProcess):
                    pass
            if not live:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Previous provider process group still has live writers")
