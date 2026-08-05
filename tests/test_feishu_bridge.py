from __future__ import annotations

import pytest

from mac_system.feishu import bridge as bridge_module
from mac_system.feishu.bridge import FeishuBridge


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
