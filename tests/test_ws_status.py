"""Tests for the cross-process Feishu WS status files."""

from __future__ import annotations

import time
from types import SimpleNamespace

from mac_system.feishu import ws_status
from mac_system.feishu.ws_client import FeishuWebSocketClient


def test_roundtrip_and_classify_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_status, "STATUS_DIR", tmp_path)
    ws_status.write_status(
        "default", connected=True, connected_since=time.time(), pid=123
    )
    payload = ws_status.read_status("default")
    assert payload is not None
    assert payload["connected"] is True
    assert payload["pid"] == 123
    assert ws_status.classify(payload) == "connected"


def test_classify_reconnecting(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_status, "STATUS_DIR", tmp_path)
    ws_status.write_status("acct_a", connected=False)
    payload = ws_status.read_status("acct_a")
    assert payload is not None
    assert ws_status.classify(payload) == "reconnecting"


def test_classify_unknown_when_missing_or_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_status, "STATUS_DIR", tmp_path)
    assert ws_status.classify(None) == "unknown"
    ws_status.write_status("acct_b", connected=True)
    payload = ws_status.read_status("acct_b")
    payload["updated_at"] = time.time() - (ws_status.STALE_AFTER_SECONDS + 1)
    assert ws_status.classify(payload) == "unknown"


def test_list_statuses_uses_embedded_account_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_status, "STATUS_DIR", tmp_path)
    assert ws_status.list_statuses() == {}
    ws_status.write_status("bot/one", connected=True)
    statuses = ws_status.list_statuses()
    assert statuses == {"bot/one": "connected"}


def test_remove_status(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_status, "STATUS_DIR", tmp_path)
    ws_status.write_status("acct_c", connected=False)
    ws_status.remove_status("acct_c")
    assert ws_status.read_status("acct_c") is None


def test_status_poller_refreshes_unchanged_connected_state(monkeypatch):
    writes = []
    monkeypatch.setattr(ws_status, "write_status", lambda *args, **kwargs: writes.append((args, kwargs)))

    class ThreePolls:
        count = 0

        def is_set(self):
            return self.count >= 3

        def wait(self, _seconds):
            self.count += 1

    client = SimpleNamespace(
        account=SimpleNamespace(id="default"),
        _stop_event=ThreePolls(),
        _notify_offline_window=lambda *_args: None,
    )
    FeishuWebSocketClient._poll_connection_status(
        client, SimpleNamespace(_conn=object())
    )

    assert len(writes) == 3
    assert all(args == ("default",) and kwargs["connected"] for args, kwargs in writes)
    assert len({kwargs["connected_since"] for _, kwargs in writes}) == 1
