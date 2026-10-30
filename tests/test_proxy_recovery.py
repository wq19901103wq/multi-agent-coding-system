"""Adaptive local-proxy (mihomo) recovery and TLS-level provider probing."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from mac_system.agents import proxy_recovery
from mac_system.agents.proxy_recovery import (
    ProxyRecoveryUnavailable,
    eligible_selector_groups,
    pick_best_node,
    recover_dead_proxy_nodes,
)
from mac_system.agents.root_runtime import _probe_https_reachable


@pytest.fixture(autouse=True)
def _reset_recovery_state(monkeypatch):
    monkeypatch.setattr(proxy_recovery, "_recovery_lock", None)
    # time.monotonic() counts from boot, so a 0.0 baseline looks "recent" when
    # the machine booted less than the test's cooldown window ago.
    monkeypatch.setattr(proxy_recovery, "_last_recovery_attempt_monotonic", float("-inf"))


class FakeController:
    def __init__(
        self,
        proxies: dict,
        group_delays: dict[str, dict] | None = None,
        unavailable: bool = False,
        member_delays: dict[str, float] | None = None,
        group_test_unavailable: bool = False,
    ) -> None:
        self.proxies = proxies
        self.group_delays = group_delays or {}
        self.unavailable = unavailable
        self.member_delays = member_delays
        self.group_test_unavailable = group_test_unavailable
        self.dead_groups: set[str] = set()
        self.selected: dict[str, str] = {}
        self.delay_calls: list[str] = []

    async def list_proxies(self):
        if self.unavailable:
            raise ProxyRecoveryUnavailable("no controller")
        return self.proxies

    async def test_proxy_delay(self, name, url, timeout):
        self.delay_calls.append(name)
        if self.member_delays is not None and name in self.member_delays:
            return self.member_delays[name]
        if name in self.dead_groups:
            return None
        return 42.0

    async def test_group_delays(self, group, url, timeout):
        if self.group_test_unavailable:
            raise ProxyRecoveryUnavailable(f"controller HTTP 504 for {group}")
        return self.group_delays.get(group, {})

    async def select_proxy(self, group, name):
        self.selected[group] = name


def _proxies_payload() -> dict:
    return {
        "节点选择": {
            "type": "Selector",
            "now": "节点A",
            "all": ["节点A", "节点B", "节点C"],
        },
        "番剧": {"type": "Selector", "now": "DIRECT", "all": ["DIRECT", "节点A"]},
        "链式": {"type": "Selector", "now": "节点选择", "all": ["节点选择", "DIRECT"]},
        "自动": {"type": "URLTest", "now": "节点B", "all": ["节点A", "节点B"]},
        "节点A": {"type": "Shadowsocks", "now": ""},
        "节点B": {"type": "Shadowsocks", "now": ""},
        "节点C": {"type": "Shadowsocks", "now": ""},
    }


def test_eligible_selector_groups_skips_reserved_and_chained():
    assert eligible_selector_groups(_proxies_payload()) == ["节点选择"]


def test_pick_best_node_ignores_errors_and_picks_lowest():
    delays = {"节点A": "timeout", "节点B": 900, "节点C": 120, "节点D": True}
    assert pick_best_node(delays) == "节点C"
    assert pick_best_node({"节点A": "error"}) is None
    assert pick_best_node({}) is None


@pytest.mark.asyncio
async def test_recovery_switches_only_dead_concrete_groups():
    client = FakeController(
        _proxies_payload(),
        group_delays={"节点选择": {"节点A": "timeout", "节点B": 880, "节点C": 130}},
    )
    client.dead_groups.add("节点选择")
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0
    )
    assert switches == [("节点选择", "节点A", "节点C")]
    assert client.selected == {"节点选择": "节点C"}


@pytest.mark.asyncio
async def test_recovery_leaves_healthy_groups_untouched():
    client = FakeController(_proxies_payload())
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0
    )
    assert switches == []
    assert client.selected == {}


@pytest.mark.asyncio
async def test_force_recovery_switches_probe_healthy_stream_broken_node():
    # The current node answers the probe (42ms) yet keeps killing long-lived
    # model streams; forced re-evaluation must land on a different node.
    client = FakeController(
        _proxies_payload(),
        group_delays={"节点选择": {"节点A": 42, "节点B": 880, "节点C": 130}},
    )
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0, force=True
    )
    assert switches == [("节点选择", "节点A", "节点C")]
    assert client.selected == {"节点选择": "节点C"}


@pytest.mark.asyncio
async def test_force_recovery_keeps_current_when_only_healthy_choice():
    client = FakeController(
        _proxies_payload(),
        group_delays={"节点选择": {"节点A": 42, "节点B": "timeout"}},
    )
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0, force=True
    )
    assert switches == []
    assert client.selected == {}


@pytest.mark.asyncio
async def test_recovery_falls_back_to_member_tests_when_group_test_504():
    # The mihomo /group/<g>/delay endpoint returns HTTP 504 exactly when the
    # network is bad; per-member tests must still find a healthy node.
    client = FakeController(
        _proxies_payload(),
        member_delays={"节点A": None, "节点B": 880, "节点C": 130},
        group_test_unavailable=True,
    )
    client.dead_groups.add("节点选择")
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0
    )
    assert switches == [("节点选择", "节点A", "节点C")]
    assert client.selected == {"节点选择": "节点C"}


@pytest.mark.asyncio
async def test_recovery_group_504_without_healthy_member_keeps_current():
    client = FakeController(
        _proxies_payload(),
        member_delays={"节点A": None, "节点B": None, "节点C": None},
        group_test_unavailable=True,
    )
    client.dead_groups.add("节点选择")
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0
    )
    assert switches == []
    assert client.selected == {}


@pytest.mark.asyncio
async def test_recovery_coalesced_by_cooldown():
    first = FakeController(
        _proxies_payload(), group_delays={"节点选择": {"节点B": 100}}
    )
    first.dead_groups.add("节点选择")
    switches = await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=first, cooldown_seconds=3600
    )
    assert len(switches) == 1

    second = FakeController(_proxies_payload())
    second.dead_groups.add("节点选择")
    assert await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=second, cooldown_seconds=3600
    ) == []
    # Cooldown means the second caller never even queried the controller.
    assert second.delay_calls == []


@pytest.mark.asyncio
async def test_recovery_unavailable_controller_is_silent_noop():
    client = FakeController({}, unavailable=True)
    assert await recover_dead_proxy_nodes(
        "https://chatgpt.com", client=client, cooldown_seconds=0
    ) == []


@pytest.mark.asyncio
async def test_probe_https_direct_tls_failure_is_unreachable():
    # Plain TCP server that never speaks TLS: handshake is positively broken.
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _probe_https_reachable("127.0.0.1", port, 2) is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_https_via_proxy_connect_200_but_dead_tunnel():
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    old_create_default_context = ssl.create_default_context

    class _DeadTunnelContext:
        def wrap_socket(self, sock, server_hostname=None):
            sock.close()
            raise ssl.SSLError("tunnel dead-dropped before handshake")

    def fake_context():
        return _DeadTunnelContext()

    try:
        # Host must not be localhost so the probe path uses the proxy.
        import os

        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
        ssl.create_default_context = fake_context
        assert await _probe_https_reachable("chatgpt.com", 443, 2) is False
    finally:
        ssl.create_default_context = old_create_default_context
        os.environ.pop("HTTPS_PROXY", None)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_https_via_proxy_bad_gateway_is_unreachable():
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    import os

    os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
    try:
        assert await _probe_https_reachable("chatgpt.com", 443, 2) is False
    finally:
        os.environ.pop("HTTPS_PROXY", None)
        server.close()
        await server.wait_closed()
