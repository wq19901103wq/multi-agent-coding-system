"""Adaptive local-proxy recovery for the Codex provider network path.

When the ambient forward proxy (Clash Verge / mihomo) accepts CONNECT but its
selected upstream node is dead, Codex turns die at the transport layer
("error sending request") and bounded continuation retries cannot help. This
module asks the mihomo external controller to health-check the concrete nodes
behind each manual Selector group against the real provider host and switches
a dead selection to the lowest-latency healthy node.

Only manual ``Selector`` groups whose current selection is a concrete node are
touched: groups pointing at ``DIRECT``/``REJECT`` (explicit user choice) or at
another group (chain pointer) are left alone, and self-healing group types
(``URLTest``/``Fallback``) are skipped. Recovery attempts are serialized and
rate-limited so concurrent turns from different projects do not stampede the
controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from http.client import HTTPConnection
from typing import Any, Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

_DEFAULT_SOCKET_PATH = "/tmp/verge/verge-mihomo.sock"
_GROUP_TEST_TIMEOUT_SECONDS = 2.0
_GROUP_WIDE_TIMEOUT_SECONDS = 15.0
_CONTROLLER_TIMEOUT_SECONDS = 5.0

_RESERVED_SELECTIONS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}

_recovery_lock: asyncio.Lock | None = None
_last_recovery_attempt_monotonic = 0.0


class ProxyRecoveryUnavailable(Exception):
    """The mihomo controller is not reachable or misbehaving."""


class ProxyControllerClient(Protocol):
    """Minimal mihomo external-controller client used by recovery."""

    async def list_proxies(self) -> dict[str, Any]: ...

    async def test_proxy_delay(self, name: str, url: str, timeout: float) -> float | None: ...

    async def test_group_delays(self, group: str, url: str, timeout: float) -> dict[str, Any]: ...

    async def select_proxy(self, group: str, name: str) -> None: ...


class MihomoUnixSocketClient:
    """mihomo external-controller client over its unix socket."""

    def __init__(self, socket_path: str = _DEFAULT_SOCKET_PATH) -> None:
        self._socket_path = socket_path

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = _CONTROLLER_TIMEOUT_SECONDS,
    ) -> Any:
        connection = UnixHTTPConnection(self._socket_path, timeout=timeout)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise ProxyRecoveryUnavailable(str(exc)) from exc
        finally:
            connection.close()
        if response.status >= 400:
            raise ProxyRecoveryUnavailable(
                f"mihomo controller {method} {path} -> HTTP {response.status}"
            )
        try:
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise ProxyRecoveryUnavailable(f"invalid mihomo response for {path}") from exc

    async def list_proxies(self) -> dict[str, Any]:
        data = await asyncio.to_thread(self._request, "GET", "/proxies")
        if not isinstance(data, dict) or not isinstance(data.get("proxies"), dict):
            raise ProxyRecoveryUnavailable("unexpected /proxies payload")
        return data["proxies"]

    async def test_proxy_delay(self, name: str, url: str, timeout: float) -> float | None:
        path = f"/proxies/{quote(name, safe='')}/delay?timeout={int(timeout * 1000)}&url={quote(url, safe='')}"
        try:
            data = await asyncio.to_thread(self._request, "GET", path, None, timeout + 1)
        except ProxyRecoveryUnavailable:
            return None
        delay = data.get("delay") if isinstance(data, dict) else None
        return delay if isinstance(delay, (int, float)) else None

    async def test_group_delays(self, group: str, url: str, timeout: float) -> dict[str, Any]:
        path = f"/group/{quote(group, safe='')}/delay?timeout={int(timeout * 1000)}&url={quote(url, safe='')}"
        data = await asyncio.to_thread(
            self._request, "GET", path, None, timeout + _GROUP_WIDE_TIMEOUT_SECONDS
        )
        if not isinstance(data, dict):
            raise ProxyRecoveryUnavailable(f"unexpected /group delay payload for {group}")
        return data

    async def select_proxy(self, group: str, name: str) -> None:
        await asyncio.to_thread(
            self._request,
            "PUT",
            f"/proxies/{quote(group, safe='')}",
            {"name": name},
        )


class UnixHTTPConnection(HTTPConnection):
    """HTTPConnection speaking HTTP over a unix domain socket."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        import socket

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def eligible_selector_groups(proxies: dict[str, Any]) -> list[str]:
    """Selector groups whose current selection is a concrete, switchable node."""

    group_names = {
        name
        for name, info in proxies.items()
        if isinstance(info, dict) and info.get("type") in {"Selector", "URLTest", "Fallback"}
    }
    eligible: list[str] = []
    for name, info in proxies.items():
        if not isinstance(info, dict) or info.get("type") != "Selector":
            continue
        now = info.get("now")
        members = info.get("all")
        if not isinstance(now, str) or not isinstance(members, list):
            continue
        if now in _RESERVED_SELECTIONS or now in group_names:
            continue
        if now not in members:
            continue
        eligible.append(name)
    return eligible


def pick_best_node(
    delays: dict[str, Any], *, exclude: str | None = None
) -> str | None:
    """Lowest-latency node from a mihomo group delay map; errors/skips ignored.

    ``exclude`` drops one candidate (the current selection) so a forced
    re-evaluation always lands on a different node when one is healthy.
    """

    best_name: str | None = None
    best_delay = float("inf")
    for name, delay in delays.items():
        if name == exclude:
            continue
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            continue
        if delay < best_delay:
            best_name = name
            best_delay = delay
    return best_name


_MEMBER_TEST_CONCURRENCY = 16


async def _test_group_members(
    controller: ProxyControllerClient,
    info: dict[str, Any],
    current: str,
    target_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Per-node delay tests for one group, bounded and partially concurrent.

    Used only when the controller's group-wide test is unusable. Each test
    carries its own timeout, and a semaphore keeps a large group (airport
    subscriptions can list a hundred nodes) from stampeding the controller.
    """

    members = info.get("all")
    if not isinstance(members, list):
        return {}
    candidates = [
        member
        for member in members
        if isinstance(member, str)
        and member != current
        and member not in _RESERVED_SELECTIONS
    ]
    semaphore = asyncio.Semaphore(_MEMBER_TEST_CONCURRENCY)

    async def test(member: str) -> tuple[str, float | None]:
        async with semaphore:
            try:
                delay = await controller.test_proxy_delay(
                    member, target_url, timeout_seconds
                )
            except ProxyRecoveryUnavailable:
                return member, None
        return member, delay

    delays: dict[str, Any] = {}
    for member, delay in await asyncio.gather(*(test(m) for m in candidates)):
        if delay is not None:
            delays[member] = delay
    return delays


async def _group_delays_for_url(
    controller: ProxyControllerClient,
    info: dict[str, Any],
    group: str,
    current: str,
    target_url: str,
) -> dict[str, Any]:
    try:
        return await controller.test_group_delays(
            group, target_url, _GROUP_TEST_TIMEOUT_SECONDS
        )
    except ProxyRecoveryUnavailable as exc:
        logger.warning(
            "Proxy group %s delay test failed for %s: %s", group, target_url, exc
        )
    # The controller's group-wide test can time out exactly when the network
    # is bad (HTTP 504), leaving no way to find an alternative node. Fall back
    # to bounded per-node tests.
    return await _test_group_members(
        controller, info, current, target_url, _GROUP_TEST_TIMEOUT_SECONDS
    )


def _healthy_across_targets(
    delay_maps: Sequence[dict[str, Any]],
) -> dict[str, float]:
    """Nodes with a numeric delay for every target, keyed by summed latency."""

    combined: dict[str, float] | None = None
    for delay_map in delay_maps:
        numeric = {
            name: delay
            for name, delay in delay_map.items()
            if isinstance(delay, (int, float)) and not isinstance(delay, bool)
        }
        if combined is None:
            combined = numeric
        else:
            combined = {
                name: total + numeric[name]
                for name, total in combined.items()
                if name in numeric
            }
    return combined or {}


async def recover_dead_proxy_nodes(
    target_urls: Sequence[str],
    *,
    socket_path: str = _DEFAULT_SOCKET_PATH,
    cooldown_seconds: float = 120.0,
    force: bool = False,
    client: ProxyControllerClient | None = None,
) -> list[tuple[str, str, str]]:
    """Health-check eligible selector groups and switch dead selections.

    A node is healthy only when it reaches *every* target URL: Codex needs
    both the chat endpoint and the auth host, and a node can pass short
    probes for one while failing the other.

    Returns ``(group, old_node, new_node)`` tuples for every switch made.
    An empty list means no switch was needed or possible; concurrent calls
    within the cooldown window are coalesced into one no-op attempt.

    With ``force=True`` the current selection is re-evaluated even when it
    passes the probes: a node can answer short health checks yet keep killing
    long-lived model streams, so the best *other* healthy node is picked.
    When the current node is the only healthy choice it is kept.
    """

    global _recovery_lock, _last_recovery_attempt_monotonic
    if _recovery_lock is None:
        _recovery_lock = asyncio.Lock()
    async with _recovery_lock:
        now = time.monotonic()
        if now - _last_recovery_attempt_monotonic < cooldown_seconds:
            return []
        _last_recovery_attempt_monotonic = now
        controller: ProxyControllerClient = client or MihomoUnixSocketClient(socket_path)
        try:
            proxies = await controller.list_proxies()
        except ProxyRecoveryUnavailable as exc:
            logger.warning("Proxy recovery unavailable: %s", exc)
            return []
        switches: list[tuple[str, str, str]] = []
        for group in eligible_selector_groups(proxies):
            info = proxies[group]
            current = info["now"]
            if not force:
                healthy = True
                for target_url in target_urls:
                    if (
                        await controller.test_proxy_delay(
                            group, target_url, _GROUP_TEST_TIMEOUT_SECONDS
                        )
                        is None
                    ):
                        healthy = False
                        break
                if healthy:
                    continue
            delays = {}
            for target_url in target_urls:
                delays[target_url] = await _group_delays_for_url(
                    controller, info, group, current, target_url
                )
            combined = _healthy_across_targets(list(delays.values()))
            best = pick_best_node(combined, exclude=current if force else None)
            if best is None or (best == current and not force):
                continue
            try:
                await controller.select_proxy(group, best)
            except ProxyRecoveryUnavailable as exc:
                logger.warning("Proxy switch %s -> %s failed: %s", group, best, exc)
                continue
            logger.warning(
                "Proxy recovery: switched group %r from %r to %r (%.0fms across %s)",
                group,
                current,
                best,
                combined[best],
                ", ".join(target_urls),
            )
            switches.append((group, current, best))
        return switches
