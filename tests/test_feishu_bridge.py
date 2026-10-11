from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime

import httpx
import pytest
from types import SimpleNamespace

from mac_system.feishu import bridge as bridge_module
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.accounts import FeishuBotAccount, get_feishu_bot_accounts
from mac_system.feishu.handlers import ButtonAction
from mac_system.feishu.models import TextMessage
from mac_system.feishu import ws_client as ws_client_module
from mac_system.feishu.ws_client import (
    FeishuWebSocketClient,
    _cap_sdk_reconnect_delay,
    _card_action_audit_detail,
)


class _TokenResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "code": 0,
            "tenant_access_token": "shared-token",
            "expire": 7200,
        }


class _TokenClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def post(self, url: str, json: dict[str, str]) -> _TokenResponse:
        self.calls.append(url)
        return _TokenResponse()

    async def aclose(self) -> None:
        return None


async def _skip_retry_delay(delay: float) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError],
)
async def test_request_replaces_failed_connection_pool(monkeypatch, error_type):
    monkeypatch.setattr(bridge_module, "_TENANT_TOKEN_CACHE", {})
    monkeypatch.setattr(bridge_module.asyncio, "sleep", _skip_retry_delay)
    bridge = FeishuBridge("app-id", "secret")
    await bridge._client.aclose()

    def fail(request):
        raise error_type("transport unavailable", request=request)

    def succeed(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json=_TokenResponse().json())
        return httpx.Response(200, json={"code": 0, "bot": {"open_id": "bot-id"}})

    failed_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    recovered_client = httpx.AsyncClient(transport=httpx.MockTransport(succeed))
    bridge._client = failed_client
    monkeypatch.setattr(bridge, "_create_client", lambda: recovered_client)

    try:
        assert await bridge.get_bot_open_id() == "bot-id"
        assert failed_client.is_closed
        assert bridge._client is recovered_client
        assert bridge._client_requests == {}
    finally:
        await bridge.close()
    assert recovered_client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_pending", [False, True])
async def test_pool_replacement_preserves_inflight_requests(monkeypatch, cancel_pending):
    monkeypatch.setattr(bridge_module.asyncio, "sleep", _skip_retry_delay)
    bridge = FeishuBridge("app-id", "secret")
    await bridge._client.aclose()
    bridge._token = "token"
    bridge._token_expire_at = float("inf")
    pending_started = asyncio.Event()
    release_pending = asyncio.Event()

    async def fail(request):
        if request.url.path == "/pending":
            pending_started.set()
            await release_pending.wait()
        raise httpx.ConnectError("broken proxy tunnel", request=request)

    def succeed(request):
        return httpx.Response(200, json={"code": 0})

    failed_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    recovered_client = httpx.AsyncClient(transport=httpx.MockTransport(succeed))
    bridge._client = failed_client
    replacements = []

    def replacement():
        replacements.append(recovered_client)
        return recovered_client

    monkeypatch.setattr(bridge, "_create_client", replacement)
    pending = asyncio.create_task(bridge._request("GET", "/pending"))
    try:
        await asyncio.wait_for(pending_started.wait(), timeout=1)
        assert await bridge._request("GET", "/failed") == {"code": 0}
        assert not failed_client.is_closed
        if cancel_pending:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            release_pending.set()
            assert await pending == {"code": 0}
        assert failed_client.is_closed
        assert replacements == [recovered_client]
        assert bridge._client_requests == {}
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await bridge.close()


@pytest.mark.asyncio
async def test_final_transport_failure_retires_pool_for_outbox_retry(monkeypatch):
    bridge = FeishuBridge("app-id", "secret")
    await bridge._client.aclose()
    bridge._token = "token"
    bridge._token_expire_at = float("inf")

    def fail(request):
        raise httpx.PoolTimeout("exhausted pool", request=request)

    failed_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    replacement = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 0})
    ))
    bridge._client = failed_client
    monkeypatch.setattr(bridge, "_create_client", lambda: replacement)
    try:
        with pytest.raises(httpx.PoolTimeout):
            await bridge._request("GET", "/bot", max_retries=1)
        assert failed_client.is_closed
        assert await bridge._request("GET", "/bot", max_retries=1) == {"code": 0}
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_http_error_does_not_replace_healthy_pool(monkeypatch):
    bridge = FeishuBridge("app-id", "secret")
    await bridge._client.aclose()
    bridge._token = "token"
    bridge._token_expire_at = float("inf")
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"code": 1})
    ))
    bridge._client = client

    def unexpected_replacement():
        pytest.fail("HTTP authentication failure must not rotate the transport")

    monkeypatch.setattr(bridge, "_create_client", unexpected_replacement)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await bridge._request("GET", "/bot")
        assert bridge._client is client
        assert not client.is_closed
        assert bridge._client_requests == {}
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_broken_proxy_tunnels_cannot_exhaust_bridge_pool(monkeypatch):
    async def broken_proxy(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(broken_proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    clients = []

    def client():
        connection = httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=httpx.Timeout(0.5, pool=0.1),
            limits=httpx.Limits(max_connections=2),
        )
        clients.append(connection)
        return connection

    bridge = FeishuBridge("test", "test", base_url="https://example.test")
    await bridge._client.aclose()
    bridge._client = client()
    bridge._token = "token"
    bridge._token_expire_at = float("inf")
    monkeypatch.setattr(bridge, "_create_client", client)
    async with server:
        try:
            for attempt in range(3):
                with pytest.raises(httpx.ConnectError):
                    await bridge._request("GET", "/test", max_retries=1)
                assert clients[attempt].is_closed
            assert bridge._client_requests == {}
        finally:
            await bridge.close()
    assert len(clients) == 4
    assert all(connection.is_closed for connection in clients)


@pytest.mark.asyncio
async def test_tenant_token_is_reused_across_short_lived_bridges(monkeypatch):
    monkeypatch.setattr(bridge_module, "_TENANT_TOKEN_CACHE", {})
    calls: list[str] = []
    first = FeishuBridge("app-id", "secret")
    second = FeishuBridge("app-id", "secret")
    await first._client.aclose()
    await second._client.aclose()
    first._client = _TokenClient(calls)
    second._client = _TokenClient(calls)

    assert await first._get_tenant_token() == "shared-token"
    assert await second._get_tenant_token() == "shared-token"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tenant_token_cache_is_isolated_by_app(monkeypatch):
    monkeypatch.setattr(bridge_module, "_TENANT_TOKEN_CACHE", {})
    calls: list[str] = []
    first = FeishuBridge("first-app", "secret")
    second = FeishuBridge("second-app", "secret")
    await first._client.aclose()
    await second._client.aclose()
    first._client = _TokenClient(calls)
    second._client = _TokenClient(calls)

    await first._get_tenant_token()
    await second._get_tenant_token()
    assert len(calls) == 2


def test_multiple_bot_accounts_preserve_legacy_default_and_isolate_credentials():
    settings = SimpleNamespace(
        feishu_bot_accounts_json=(
            '{"research":{"app_id":"app-research",'
            '"app_secret":"secret-research"}}'
        ),
        feishu_default_bot_account_id="default",
        feishu_app_id="app-default",
        feishu_app_secret="secret-default",
        feishu_verification_token=None,
        feishu_encrypt_key=None,
    )

    accounts = get_feishu_bot_accounts(settings)

    assert set(accounts) == {"default", "research"}
    assert accounts["default"].app_id == "app-default"
    assert accounts["research"].app_id == "app-research"


def test_duplicate_app_id_cannot_be_mounted_as_two_bot_accounts():
    settings = SimpleNamespace(
        feishu_bot_accounts_json=(
            '{"one":{"app_id":"same","app_secret":"first"},'
            '"two":{"app_id":"same","app_secret":"second"}}'
        ),
        feishu_default_bot_account_id="default",
        feishu_app_id=None,
        feishu_app_secret=None,
        feishu_verification_token=None,
        feishu_encrypt_key=None,
    )

    with pytest.raises(ValueError, match="configured for both"):
        get_feishu_bot_accounts(settings)


def test_thread_root_is_not_treated_as_explicit_card_reply():
    bridge = FeishuBridge("app-id", "secret")
    payload = {
        "event": {
            "message": {
                "message_id": "om-new",
                "root_id": "om-old-card",
                "chat_id": "oc-chat",
                "chat_type": "p2p",
                "content": '{"text":"继续当前项目"}',
                "sender": {
                    "sender_id": {
                        "open_id": "ou-user",
                        "user_id": "tenant-user",
                        "union_id": "union-user",
                    },
                    "sender_type": "user",
                },
            }
        }
    }

    parsed = bridge.parse_text_message(payload)
    assert parsed.user_id == "tenant-user"
    assert parsed.union_id == "union-user"

    assert parsed.parent_id is None


def test_explicit_parent_remains_the_card_reply_target():
    bridge = FeishuBridge("app-id", "secret")
    payload = {
        "event": {
            "message": {
                "message_id": "om-new",
                "parent_id": "om-question-card",
                "root_id": "om-thread-root",
                "chat_id": "oc-chat",
                "chat_type": "p2p",
                "content": '{"text":"选择 A"}',
                "sender": {
                    "sender_id": {"open_id": "ou-user"},
                    "sender_type": "user",
                },
            }
        }
    }

    parsed = bridge.parse_text_message(payload)

    assert parsed.parent_id == "om-question-card"


def test_card_action_audit_records_exact_choice_without_free_text():
    detail = json.loads(
        _card_action_audit_detail(
            ButtonAction(
                open_id="ou-user",
                approval_id="decision-1",
                decision_id="decision-1",
                action="select_owner_option",
                state="requires_user",
                option_key="继续迭代",
                extras={"inline_free_text_value": "不要写入审计"},
            )
        )
    )

    assert detail["decision_id"] == "decision-1"
    assert detail["option_key"] == "继续迭代"
    assert "不要写入审计" not in str(detail)


def test_feishu_sdk_reconnect_delay_is_capped():
    class Client:
        _reconnect_interval = 120
        _reconnect_nonce = 30

    client = Client()
    _cap_sdk_reconnect_delay(client)

    assert client._reconnect_interval == 5
    assert client._reconnect_nonce == 1


def test_each_bot_account_starts_in_an_isolated_process(monkeypatch):
    from unittest.mock import Mock

    parent_connection, child_connection = Mock(), Mock()

    class Process:
        pid = 4321

        def __init__(self):
            self.started = False
            self.terminated = False
            self.join_timeouts = []

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.terminated

        def terminate(self):
            self.terminated = True

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def kill(self):
            self.terminated = True

    process = Process()

    class Context:
        def Pipe(self):
            return parent_connection, child_connection

        def Process(self, **kwargs):
            assert kwargs["name"] == "mac-feishu-research"
            assert kwargs["args"][0].id == "research"
            return process

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        ws_client_module.multiprocessing,
        "get_context",
        lambda method: Context() if method == "spawn" else None,
    )
    client = FeishuWebSocketClient(
        FeishuBotAccount("research", "app-research", "secret-research")
    )

    client.start()
    assert process.started is True
    child_connection.close.assert_called_once()
    client.stop()
    assert process.terminated is True
    assert process.join_timeouts == [50]
    parent_connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_delayed_human_message_is_processed_and_only_event_id_dedupes(monkeypatch):
    class Service:
        def __init__(self):
            self.processed: list[tuple[str, str]] = []

        def is_event_processed(self, _event_id: str) -> bool:
            return False

        def mark_event_processed(self, event_id: str, kind: str) -> None:
            self.processed.append((event_id, kind))

    service = Service()

    class Context:
        def __init__(self):
            self.service = service

    class Scope:
        async def __aenter__(self):
            return Context()

        async def __aexit__(self, *_args):
            return None

    client = object.__new__(FeishuWebSocketClient)
    client._user_locks = {"ou-user": threading.Lock()}
    client._handler_context = lambda _msg=None: Scope()
    client._log_feishu_event = lambda *_args, **_kwargs: None
    client._update_event_watermark = lambda *_args, **_kwargs: None
    handled: list[str] = []

    async def handle(_ctx, msg):
        handled.append(msg.text)

    monkeypatch.setattr(ws_client_module, "handle_text_message", handle)
    await client._handle_message(
        TextMessage(
            open_id="ou-user",
            message_id="om-delayed",
            text="现在什么情况",
            is_command=False,
            chat_id="oc-chat",
            # Much older than process/reconnect time: this is still a real,
            # previously unseen human event and must not be silently discarded.
            created_at=datetime(2020, 1, 1),
        ),
        event_id="evt-delayed",
    )

    assert handled == ["现在什么情况"]
    assert service.processed == [("evt-delayed", "message")]
