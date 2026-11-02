import asyncio
import json
import socket

import pytest

from mac_system.agents.root_runtime import (
    AgentEvent,
    AppServerProtocolError,
    CodexAppServerThread,
    DynamicTool,
    ProjectLeadRuntime,
    ProjectLeadRuntimeContext,
    ProjectLeadRuntimeError,
    ProjectLeadRuntimeRegistry,
    TurnResult,
    _https_reachable_blocking,
    _isolated_project_process_env,
    _probe_tcp_reachable,
    _turn_error_code,
)

_ORIGINAL_PROVIDER_PROBE = CodexAppServerThread.probe_provider_reachable


@pytest.fixture(autouse=True)
def _reachable_provider_probe(monkeypatch):
    """Continuation tests never touch the real provider network path."""

    async def fake_probe(self, timeout_seconds: float = 5.0):
        return True

    monkeypatch.setattr(
        CodexAppServerThread, "probe_provider_reachable", fake_probe
    )


class _FakeStdin:
    def __init__(self, process):
        self.process = process
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        while b"\n" in self.buffer:
            raw, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            await self.process.receive(json.loads(raw))


def test_codex_runtime_implements_project_lead_contract(tmp_path):
    assert issubclass(CodexAppServerThread, ProjectLeadRuntime)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    assert runtime.provider_id == "codex"
    assert runtime.display_name == "Codex"


def test_project_lead_registry_selects_registered_provider(tmp_path):
    context = ProjectLeadRuntimeContext(cwd=tmp_path, tools=())
    expected = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    registry = ProjectLeadRuntimeRegistry({"codex": lambda _context: expected})

    assert registry.available_providers() == ("codex",)
    assert registry.create("codex", context) is expected
    with pytest.raises(ProjectLeadRuntimeError, match="not registered: pi"):
        registry.create("pi", context)


def test_project_process_does_not_inherit_mac_virtualenv(tmp_path):
    host_env = tmp_path / "mac-host-venv"
    host_bin = host_env / "bin"
    source = {
        "VIRTUAL_ENV": str(host_env),
        "VIRTUAL_ENV_PROMPT": "(mac)",
        "UV_ACTIVE": "1",
        "UV_PROJECT_ENVIRONMENT": str(host_env),
        "PYTHONPATH": "/host/python",
        "PATH": f"{host_bin}:/usr/local/bin:/usr/bin",
        "HTTPS_PROXY": "http://proxy.example",
    }

    result = _isolated_project_process_env(source)

    assert "VIRTUAL_ENV" not in result
    assert "UV_ACTIVE" not in result
    assert "UV_PROJECT_ENVIRONMENT" not in result
    assert "PYTHONPATH" not in result
    assert str(host_bin) not in result["PATH"].split(":")
    assert result["HTTPS_PROXY"] == "http://proxy.example"


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin(self)
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.messages = []
        self.tool_result = None

    def _feed(self, message):
        self.stdout.feed_data((json.dumps(message) + "\n").encode())

    async def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method in {"thread/start", "thread/resume"}:
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "thread": {"id": "thread-1"},
                        "model": "gpt-5.6-codex",
                    },
                }
            )
        elif method == "turn/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"turn": {"id": "turn-1"}},
                }
            )
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "item/tool/call",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "callId": "call-1",
                        "namespace": None,
                        "tool": "run_external_agent",
                        "arguments": {"provider": "claude_code", "message": "inspect"},
                    },
                }
            )
        elif request_id == 900 and "result" in message:
            self.tool_result = message["result"]
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "completedAtMs": 1,
                        "item": {
                            "type": "agentMessage",
                            "id": "message-1",
                            "text": "done",
                            "phase": "final_answer",
                        },
                    },
                }
            )
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                }
            )
        elif method in {"turn/steer", "turn/interrupt"}:
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
            if method == "turn/interrupt":
                turn_id = message["params"]["turnId"]
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": turn_id, "status": "interrupted"},
                        },
                    }
                )
        elif method == "thread/backgroundTerminals/clean":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})

    def terminate(self):
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize('activity', ['silent', 'tool', 'approval'])
async def test_model_watchdog_respects_tools_and_user_waits(monkeypatch, tmp_path, activity):
    class SilentProcess(_FakeProcess):
        async def receive(self, message):
            if message.get('method') == 'turn/start':
                self.messages.append(message)
                self._feed({'id': message['id'], 'result': {'turn': {'id': 'quiet-turn'}}})
            else:
                await super().receive(message)

    process = SilentProcess()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    waits = []
    async def no_wait(delay):
        waits.append(delay)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    release, requested = asyncio.Event(), asyncio.Event()
    async def handler(method, params):
        requested.set()
        await release.wait()
        return {'decision': 'decline'}
    runtime = CodexAppServerThread(codex_bin='codex', cwd=tmp_path, tools=[],
                                   server_request_handler=handler, silence_timeout_seconds=0.02)
    task = asyncio.create_task(runtime.run_turn('continue'))
    try:
        while not any(m.get('method') == 'turn/start' for m in process.messages):
            await asyncio.sleep(0)
        params = {'threadId': 'thread-1', 'turnId': 'quiet-turn'}
        if activity == 'tool':
            params['item'] = {'id': 'long-command', 'type': 'commandExecution'}
            process._feed({'method': 'item/started', 'params': params})
            await asyncio.sleep(0.08)
            assert not task.done()
            process._feed({'method': 'item/completed', 'params': params})
        elif activity == 'approval':
            process._feed({'id': 990, 'method': 'item/commandExecution/requestApproval', 'params': params})
            await asyncio.wait_for(requested.wait(), 0.5)
            await asyncio.sleep(0.08)
            assert not task.done()
            release.set()
        with pytest.raises(AppServerProtocolError, match='bounded same-thread'):
            await asyncio.wait_for(task, 1)
        starts = [m for m in process.messages if m.get('method') == 'turn/start']
        assert len(starts) == 9
        assert starts[0]['params']['input'][0]['text'] == 'continue'
        assert all(m['params']['input'] == [] for m in starts[1:])
        assert waits == [5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


class _ReconnectingProcess(_FakeProcess):
    """A live app-server whose network retries never end the turn."""

    def __init__(self, failures=1, *, acknowledge_only=False, progress=False,
                 retry_error=None, retry_interval=0.005):
        super().__init__()
        self.failures = failures
        self.acknowledge_only = acknowledge_only
        self.turn_attempts = 0
        self.retry_task = None
        self.progress = progress
        self.retry_error = retry_error or {
            "message": "Reconnecting... waiting for network",
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
        }
        self.retry_interval = retry_interval

    async def receive(self, message):
        method = message.get("method")
        if method == "turn/start":
            self.messages.append(message)
            self.turn_attempts += 1
            turn_id = f"reconnect-{self.turn_attempts}"
            self._feed({"id": message["id"], "result": {"turn": {"id": turn_id}}})
            self._feed({"method": "turn/started", "params": {
                "threadId": "thread-1", "turn": {"id": turn_id, "status": "inProgress"},
            }})
            if self.turn_attempts <= self.failures:
                self.retry_task = asyncio.create_task(self.repeat_errors(turn_id))
            else:
                self.complete(turn_id)
        elif method == "turn/interrupt":
            if self.retry_task:
                self.retry_task.cancel()
                await asyncio.gather(self.retry_task, return_exceptions=True)
            if self.acknowledge_only:
                self.messages.append(message)
                self._feed({"id": message["id"], "result": {}})
            else:
                await super().receive(message)
        else:
            await super().receive(message)

    async def repeat_errors(self, turn_id):
        while True:
            self._feed({"method": "error", "params": {
                "threadId": "thread-1", "turnId": turn_id, "willRetry": True,
                "error": self.retry_error,
            }})
            # Incidental usage notifications must not postpone recovery.
            self._feed({"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": turn_id,
            }})
            if self.progress:
                self._feed({"method": "item/agentMessage/delta", "params": {
                    "threadId": "thread-1", "turnId": turn_id,
                    "itemId": "partial", "delta": ".",
                }})
            await asyncio.sleep(self.retry_interval)

    def complete(self, turn_id="reconnect-1"):
        if self.retry_task:
            self.retry_task.cancel()
        self._feed({"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "answer", "text": "done", "phase": "final_answer"},
        }})
        self._feed({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": turn_id, "status": "completed"},
        }})

    def terminate(self):
        if self.retry_task:
            self.retry_task.cancel()
        super().terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reachable", [
    ("responseStreamDisconnected", True),
    ("responseStreamDisconnected", False),
    ("responseStreamDisconnected", None),
    ("httpConnectionFailed", True),
    ("responseStreamConnectionFailed", True),
])
async def test_native_reconnect_repairs_proxy_without_replaying_turn(monkeypatch, tmp_path, code, reachable):
    process = _ReconnectingProcess(progress=True, retry_error={"codexErrorInfo": code})
    async def spawn(*args, **kwargs):
        return process
    calls = []
    async def recover(urls, **kwargs):
        calls.append((urls, kwargs))
        return [("selector", "old", "healthy")]
    async def probe(self):
        return reachable
    recovered = asyncio.Event()
    async def on_event(event):
        if event.kind == "mac/continuationRetry" and event.payload["phase"] == "proxyRecovered":
            recovered.set()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(CodexAppServerThread, "probe_provider_reachable", probe)
    monkeypatch.setattr("mac_system.agents.root_runtime.recover_dead_proxy_nodes", recover)
    monkeypatch.setattr("mac_system.agents.root_runtime._NATIVE_RECONNECT_PROXY_GRACE_SECONDS", 0.02)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=0.04,
        proxy_recovery_enabled=True, event_handler=on_event,
    )
    task = asyncio.create_task(runtime.run_turn("original request"))
    try:
        await asyncio.wait_for(recovered.wait(), 0.5)
        # Intermittent model progress must not defer recovery forever, and
        # more retry errors inside the cooldown must not switch again.
        await asyncio.sleep(0.06)
        process.complete()
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == "completed" and result.final_message == "done"
        assert len(calls) == 1
        assert calls[0][0] == ("https://chatgpt.com", "https://auth.openai.com")
        assert calls[0][1]["force"] is (reachable is not False)
        starts = [m["params"] for m in process.messages if m.get("method") == "turn/start"]
        assert len(starts) == 1
        assert starts[0]["input"][0]["text"] == "original request"
        assert result.turn_id == "reconnect-1"
        assert not any(m.get("method") in {"turn/interrupt", "thread/resume"} for m in process.messages)
        assert any(e.payload.get("nativeRetry") is True for e in result.events)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["single", "stale", "overload", "auth", "generic", "disabled"])
async def test_native_reconnect_proxy_recovery_requires_repeated_recent_transport_errors(monkeypatch, tmp_path, case):
    errors = {
        "overload": {"codexErrorInfo": "serverOverloaded"},
        "auth": {"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 403}}},
        "generic": {"message": "Reconnecting... waiting for network", "codexErrorInfo": "other"},
    }
    process = _ReconnectingProcess(
        retry_error=errors.get(case),
        retry_interval=1 if case == "single" else 0.03 if case == "stale" else 0.005,
    )
    async def spawn(*args, **kwargs):
        return process
    calls = []
    async def recover(*args, **kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("mac_system.agents.root_runtime.recover_dead_proxy_nodes", recover)
    monkeypatch.setattr("mac_system.agents.root_runtime._NATIVE_RECONNECT_PROXY_GRACE_SECONDS", 0)
    monkeypatch.setattr("mac_system.agents.root_runtime._NATIVE_RECONNECT_PROXY_WINDOW_SECONDS", 0.02)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=2,
        proxy_recovery_enabled=case != "disabled",
    )
    task = asyncio.create_task(runtime.run_turn("original request"))
    try:
        while process.turn_attempts == 0:
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        process.complete()
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == "completed"
        assert calls == []
        assert process.turn_attempts == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "steer", "tool", "approval", "complete", "failure"])
async def test_native_proxy_recovery_does_not_block_turn_or_user_actions(monkeypatch, tmp_path, action):
    class Process(_ReconnectingProcess):
        async def receive(self, message):
            if message.get("method") == "turn/steer":
                self.messages.append(message)
                self._feed({"id": message["id"], "result": {"turnId": "reconnect-1"}})
            else:
                await super().receive(message)
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    recovering, cancelled, waiting, release = (asyncio.Event() for _ in range(4))
    calls = []
    async def recover(*args, **kwargs):
        calls.append(kwargs)
        recovering.set()
        if action == "failure":
            raise RuntimeError("controller unavailable")
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def handler(*args):
        waiting.set()
        await release.wait()
        return {"decision": "decline"}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("mac_system.agents.root_runtime.recover_dead_proxy_nodes", recover)
    monkeypatch.setattr("mac_system.agents.root_runtime._NATIVE_RECONNECT_PROXY_GRACE_SECONDS", 0)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=2,
        proxy_recovery_enabled=True, server_request_handler=handler,
    )
    task = asyncio.create_task(runtime.run_turn("original request"))
    try:
        await asyncio.wait_for(recovering.wait(), 0.5)
        params = {"threadId": "thread-1", "turnId": "reconnect-1"}
        if action == "stop":
            await asyncio.wait_for(runtime.interrupt(), 0.5)
        elif action == "steer":
            await asyncio.wait_for(runtime.steer(None, "additional request"), 0.5)
            steer = next(m for m in process.messages if m.get("method") == "turn/steer")
            assert steer["params"]["expectedTurnId"] == "reconnect-1"
        elif action == "tool":
            process._feed({"method": "item/started", "params": {
                **params, "item": {"id": "long", "type": "commandExecution"},
            }})
            await asyncio.wait_for(cancelled.wait(), 0.5)
        elif action == "approval":
            process._feed({"id": 990, "method": "item/commandExecution/requestApproval", "params": params})
            await asyncio.wait_for(waiting.wait(), 0.5)
            await asyncio.wait_for(cancelled.wait(), 0.5)
        if action != "stop":
            process.complete()
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == ("interrupted" if action == "stop" else "completed")
        assert process.turn_attempts == 1 and len(calls) == 1
        if action != "failure":
            assert cancelled.is_set()
        assert runtime._native_proxy_recovery_task is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_silent_model_stream_continues_after_native_interrupt(monkeypatch, tmp_path):
    class SilentOnceProcess(_FakeProcess):
        def __init__(self):
            super().__init__()
            self.turn_attempts = 0

        async def receive(self, message):
            if message.get("method") == "turn/start":
                self.messages.append(message)
                self.turn_attempts += 1
                turn_id = f"turn-{self.turn_attempts}"
                self._feed({"id": message["id"], "result": {"turn": {"id": turn_id}}})
                self._feed({"method": "turn/started", "params": {
                    "threadId": "thread-1", "turn": {"id": turn_id, "status": "inProgress"},
                }})
                if self.turn_attempts == 2:
                    self._feed({"method": "item/completed", "params": {
                        "threadId": "thread-1", "turnId": turn_id,
                        "item": {"type": "agentMessage", "id": "answer", "text": "done"},
                    }})
                    self._feed({"method": "turn/completed", "params": {
                        "threadId": "thread-1", "turn": {"id": turn_id, "status": "completed"},
                    }})
                return
            await super().receive(message)

    process = SilentOnceProcess()
    async def spawn(*args, **kwargs):
        return process
    waits = []
    async def no_wait(delay):
        waits.append(delay)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=0.02,
    )
    try:
        result = await asyncio.wait_for(runtime.run_turn("original request"), 1)
        assert result.status == "completed"
        assert result.final_message == "done"
        assert waits == [5.0]
        starts = [m["params"] for m in process.messages if m.get("method") == "turn/start"]
        assert starts[0]["input"][0]["text"] == "original request"
        assert starts[1]["input"] == []
        assert [m["params"]["turnId"] for m in process.messages
                if m.get("method") == "turn/interrupt"] == ["turn-1"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 10])
async def test_repeated_reconnects_use_bounded_native_continuations(monkeypatch, tmp_path, failures):
    process = _ReconnectingProcess(failures)
    async def spawn(*args, **kwargs):
        return process
    waits = []
    async def no_wait(delay):
        waits.append(delay)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=0.025)
    try:
        if failures == 1:
            result = await asyncio.wait_for(runtime.run_turn("original request"), 1)
            assert result.status == "completed" and result.final_message == "done"
            assert [e.payload["turn"]["status"] for e in result.events if e.kind == "turn/completed"] == ["interrupted", "completed"]
            assert waits == [5.0]
        else:
            with pytest.raises(AppServerProtocolError, match="bounded same-thread"):
                await asyncio.wait_for(runtime.run_turn("original request"), 1)
            assert waits == [5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0]
        starts = [m["params"] for m in process.messages if m.get("method") == "turn/start"]
        assert len(starts) == (2 if failures == 1 else 9)
        assert starts[0]["input"][0]["text"] == "original request"
        assert all(p["threadId"] == "thread-1" and p["input"] == [] for p in starts[1:])
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_reconnect_interrupt_ack_without_terminal_never_continues(monkeypatch, tmp_path):
    process = _ReconnectingProcess(acknowledge_only=True)
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[],
                                   silence_timeout_seconds=0.025, request_timeout_seconds=0.05)
    try:
        with pytest.raises(AppServerProtocolError, match="no terminal event"):
            await asyncio.wait_for(runtime.run_turn("continue"), 1)
        assert process.turn_attempts == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["recovered", "tool", "approval", "user_stop", "backoff_stop"])
async def test_reconnect_watchdog_preserves_progress_waits_and_user_stop(monkeypatch, tmp_path, activity):
    process = _ReconnectingProcess()
    async def spawn(*args, **kwargs):
        return process
    release, waiting = asyncio.Event(), asyncio.Event()
    async def handler(*args):
        waiting.set()
        await release.wait()
        return {"decision": "decline"}
    async def backoff(_delay):
        waiting.set()
        await release.wait()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", backoff)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[],
                                   silence_timeout_seconds=0.04, server_request_handler=handler)
    task = asyncio.create_task(runtime.run_turn("continue"))
    try:
        while process.turn_attempts == 0:
            await asyncio.sleep(0)
        params = {"threadId": "thread-1", "turnId": "reconnect-1"}
        if activity == "tool":
            params["item"] = {"id": "long", "type": "commandExecution"}
            process._feed({"method": "item/started", "params": params})
            await asyncio.sleep(0.12)
            assert not task.done()
            process._feed({"method": "item/completed", "params": params})
        elif activity == "approval":
            process._feed({"id": 990, "method": "item/commandExecution/requestApproval", "params": params})
            await asyncio.wait_for(waiting.wait(), 0.5)
            await asyncio.sleep(0.12)
            assert not task.done()
            release.set()
        elif activity == "backoff_stop":
            await asyncio.wait_for(waiting.wait(), 0.5)
        if activity in {"user_stop", "backoff_stop"}:
            await runtime.interrupt()
            release.set()
        else:
            process.complete()
        result = await asyncio.wait_for(task, 0.5)
        assert result.status == ("interrupted" if activity.endswith("stop") else "completed")
        assert process.turn_attempts == 1
        if not activity.endswith("stop"):
            assert result.final_message == "done"
            assert not any(m.get("method") == "turn/interrupt" for m in process.messages)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_user_message_during_reconnect_steers_existing_turn(monkeypatch, tmp_path):
    class Process(_ReconnectingProcess):
        async def receive(self, message):
            if message.get("method") == "turn/steer":
                self.messages.append(message)
                self._feed({"id": message["id"], "result": {"turnId": "reconnect-1"}})
            else:
                await super().receive(message)

    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[], silence_timeout_seconds=5)
    task = asyncio.create_task(runtime.run_turn("original request"))
    try:
        while runtime.active_turn_id is None:
            await asyncio.sleep(0)
        await runtime.steer(None, "补充要求")
        process.complete()
        result = await asyncio.wait_for(task, 1)
        assert result.status == "completed"
        assert process.turn_attempts == 1
        steer = next(m for m in process.messages if m.get("method") == "turn/steer")
        assert steer["params"]["expectedTurnId"] == "reconnect-1"
        assert steer["params"]["input"][0]["text"] == "补充要求"
        assert not any(m.get("method") == "thread/resume" for m in process.messages)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


class _ParallelToolProcess(_FakeProcess):
    def __init__(self):
        super().__init__()
        self.tool_responses = []

    async def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "thread/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"thread": {"id": "thread-1"}, "model": "test-model"},
                }
            )
        elif method == "turn/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"turn": {"id": "turn-parallel"}},
                }
            )
            for tool_request_id, provider in ((901, "claude_code"), (902, "pi")):
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "id": tool_request_id,
                        "method": "item/tool/call",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-parallel",
                            "callId": f"call-{provider}",
                            "tool": "parallel_tool",
                            "arguments": {"provider": provider},
                        },
                    }
                )
        elif request_id in {901, 902} and "result" in message:
            self.tool_responses.append(message)
            if len(self.tool_responses) == 2:
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-parallel",
                            "item": {
                                "type": "agentMessage",
                                "id": "message-parallel",
                                "text": "both done",
                            },
                        },
                    }
                )
                self._feed(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-parallel", "status": "completed"},
                        },
                    }
                )


class _CapacityProcess(_FakeProcess):
    def __init__(
        self, *, failures: int, codex_error_info="serverOverloaded",
        recovery_status: str | None = None,
        error_message: str = "The upstream turn failed.",
    ):
        super().__init__()
        self.failures = failures
        self.codex_error_info = codex_error_info
        self.recovery_status = recovery_status
        self.error_message = error_message
        self.turn_attempts = 0

    async def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        if method == "thread/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"thread": {"id": "capacity-thread"}},
                }
            )
            return
        if method != "turn/start":
            return
        self.turn_attempts += 1
        turn_id = f"capacity-turn-{self.turn_attempts}"
        self._feed(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"turn": {"id": turn_id}},
            }
        )
        if self.turn_attempts <= self.failures:
            error_info = (
                self.codex_error_info[self.turn_attempts - 1]
                if isinstance(self.codex_error_info, list)
                else self.codex_error_info
            )
            error = {
                "message": self.error_message,
                "codexErrorInfo": error_info,
            }
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "error",
                    "params": {
                        "threadId": "capacity-thread",
                        "turnId": turn_id,
                        "error": error,
                        "willRetry": False,
                    },
                }
            )
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "threadId": "capacity-thread",
                        "turn": {
                            "id": turn_id,
                            "status": "failed",
                            "error": error,
                        },
                    },
                }
            )
            return
        recovered_turn = self.turn_attempts == self.failures + 1
        if self.recovery_status is not None and recovered_turn:
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "method": "error",
                    "params": {
                        "threadId": "capacity-thread",
                        "turnId": turn_id,
                        "error": {
                            "message": "Transient error; retrying natively.",
                            "codexErrorInfo": self.codex_error_info,
                        },
                        "willRetry": True,
                    },
                }
            )
        # An unnecessary continuation after recovery can complete without an
        # answer, which used to replace the answer from the recovered turn.
        final_text = "done" if self.recovery_status is None or (
            recovered_turn and self.recovery_status == "completed"
        ) else ""
        self._feed(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "capacity-thread",
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "id": "capacity-result",
                        "text": final_text,
                        "phase": "final_answer",
                    },
                },
            }
        )
        self._feed(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "capacity-thread",
                    "turn": {
                        "id": turn_id,
                        "status": self.recovery_status if recovered_turn
                        and self.recovery_status is not None else "completed",
                    },
                },
            }
        )


class _CompletedWithStaleRequestProcess(_FakeProcess):
    """Emulate a native completion while a local presentation hook is stuck."""

    def __init__(self):
        super().__init__()
        self.allow_completion = asyncio.Event()

    async def _complete_after_hook_starts(self):
        await self.allow_completion.wait()
        self._feed(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-stale-request",
                        "status": "completed",
                    },
                },
            }
        )

    async def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "thread/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"thread": {"id": "thread-1"}, "model": "test-model"},
                }
            )
        elif method == "turn/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"turn": {"id": "turn-stale-request"}},
                }
            )
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": 990,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-stale-request",
                        "itemId": "approval-1",
                    },
                }
            )
            asyncio.create_task(self._complete_after_hook_starts())


class _ScriptedTurnProcess(_FakeProcess):
    def __init__(self, notifications):
        super().__init__()
        self.notifications = notifications

    async def receive(self, message):
        self.messages.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "thread/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"thread": {"id": "thread-1"}},
                }
            )
        elif method == "turn/start":
            self._feed(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"turn": {"id": "turn-response"}},
                }
            )
            for notification in self.notifications:
                self._feed(notification)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "native_retry", "failed", "interrupted"])
async def test_native_compaction_waits_for_turn_terminal_without_replaying_input(
    monkeypatch, tmp_path, outcome
):
    compaction = {"id": "compact-1", "type": "contextCompaction"}
    process = _ScriptedTurnProcess([
        {"method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-response", "status": "inProgress"},
        }},
        {"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-response", "item": compaction,
        }},
    ])

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    observed = asyncio.Queue()
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], event_handler=observed.put,
        thread_config={"model_auto_compact_token_limit": 80_000},
        silence_timeout_seconds=5,
    )

    async def wait_for_event(kind):
        while True:
            event = await asyncio.wait_for(observed.get(), 1)
            if event.kind == kind:
                return event

    def notify(method, **payload):
        process._feed({"method": method, "params": {
            "threadId": "thread-1", "turnId": "turn-response", **payload,
        }})

    task = asyncio.create_task(runtime.run_turn("继续原任务，不要重复已完成的操作"))
    try:
        await wait_for_event("item/started")
        assert not task.done()
        assert runtime.active_turn_id == "turn-response"

        if outcome == "native_retry":
            notify("error", willRetry=True, error={
                "message": "Temporary connection failure during compaction",
                "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
            })
            await wait_for_event("error")
            assert not task.done()

        status = outcome if outcome != "native_retry" else "completed"
        terminal = {"id": "turn-response", "status": status}
        if status == "completed":
            notify("item/completed", item=compaction)
            await wait_for_event("item/completed")
            # A completed compaction item is not a completed user turn.
            assert not task.done()
            notify("item/completed", item={
                "type": "agentMessage", "id": "answer", "text": "原任务已完成",
                "phase": "final_answer",
            })
            await wait_for_event("item/completed")
            assert not task.done()
        elif status == "failed":
            terminal["error"] = {
                "message": "Error running remote compact task: context limit exceeded",
                "codexErrorInfo": "contextWindowExceeded",
            }
            notify("error", willRetry=False, error=terminal["error"])
            await wait_for_event("error")
            assert not task.done()

        # Failure/interruption can finish without a compaction item/completed.
        notify("turn/completed", turn=terminal)
        result = await asyncio.wait_for(task, 1)
        assert result.status == status
        assert result.final_message == ("原任务已完成" if status == "completed" else None)
        compact_events = [event.kind for event in result.events
                          if event.payload.get("item", {}).get("type") == "contextCompaction"]
        assert compact_events == (["item/started", "item/completed"]
                                  if status == "completed" else ["item/started"])
        if status == "failed":
            assert result.events[-1].payload["turn"]["error"] == terminal["error"]
        starts = [message for message in process.messages if message.get("method") == "turn/start"]
        assert len(starts) == 1
        assert starts[0]["params"]["input"] == [{
            "type": "text", "text": "继续原任务，不要重复已完成的操作", "textElements": [],
        }]
        assert not any(message.get("method") == "thread/compact/start" for message in process.messages)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["thread", "turn"])
async def test_foreign_compaction_and_terminal_cannot_finish_current_turn(
    monkeypatch, tmp_path, foreign
):
    process = _ScriptedTurnProcess([
        {"method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-response", "status": "inProgress"},
        }},
    ])

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    observed = asyncio.Queue()
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], event_handler=observed.put,
        silence_timeout_seconds=5,
    )
    task = asyncio.create_task(runtime.run_turn("继续当前任务"))
    try:
        # Ensure the current turn exists before injecting unrelated events.
        event = await asyncio.wait_for(observed.get(), 1)
        assert event.kind == "turn/started"
        thread_id = "foreign-thread" if foreign == "thread" else "thread-1"
        turn_id = "turn-response" if foreign == "thread" else "foreign-turn"
        process._feed({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"type": "contextCompaction", "id": "foreign-compact"},
        }})
        process._feed({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "foreign-answer", "text": "别的任务结果"},
        }})
        process._feed({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {"id": turn_id, "status": "completed"},
        }})
        while True:
            event = await asyncio.wait_for(observed.get(), 1)
            if event.kind == "turn/completed":
                break
        assert not task.done()
        process._feed({"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-response",
            "item": {"type": "agentMessage", "id": "answer", "text": "当前任务结果"},
        }})
        process._feed({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-response", "status": "completed"},
        }})
        result = await asyncio.wait_for(task, 1)
        assert result.final_message == "当前任务结果"
        assert result.status == "completed"
        assert len([message for message in process.messages
                    if message.get("method") == "turn/start"]) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_root_keeps_tool_result_in_same_turn(monkeypatch, tmp_path):
    process = _FakeProcess()
    process_args = None

    async def fake_subprocess(*args, **kwargs):
        nonlocal process_args
        process_args = args
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    calls = []

    async def spawn(arguments):
        calls.append(arguments)
        return {"task": "agent-1", "status": "running"}

    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions=None,
        tools=[
            DynamicTool(
                name="run_external_agent",
                description="Start a coding-agent backend",
                input_schema={"type": "object"},
                handler=spawn,
            )
        ],
    )
    try:
        output_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        result = await runtime.run_turn(
            "implement the request", output_schema=output_schema
        )
    finally:
        await runtime.close()

    assert calls == [{"provider": "claude_code", "message": "inspect"}]
    assert process_args == ("codex", "app-server", "--stdio")
    turn_start = next(
        message for message in process.messages if message.get("method") == "turn/start"
    )
    assert turn_start["params"]["input"] == [
        {"type": "text", "text": "implement the request", "textElements": []}
    ]
    assert turn_start["params"]["outputSchema"] == output_schema
    assert result.thread_id == "thread-1"
    assert result.turn_id == "turn-1"
    assert result.status == "completed"
    assert result.final_message == "done"
    assert process.tool_result == {
        "contentItems": [
            {
                "type": "inputText",
                "text": '{"task": "agent-1", "status": "running"}',
            }
        ],
        "success": True,
    }


@pytest.mark.asyncio
async def test_empty_turn_uses_native_empty_input_without_fake_user_message(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        turn_task = asyncio.create_task(runtime.run_turn(""))
        while not any(
            message.get("method") == "turn/start" for message in process.messages
        ):
            await asyncio.sleep(0)
        turn_start = next(
            message
            for message in process.messages
            if message.get("method") == "turn/start"
        )
        assert turn_start["params"]["input"] == []
        await runtime.interrupt("turn-1")
        await turn_task
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_server_overloaded_retries_same_thread_with_empty_input(
    monkeypatch, tmp_path
):
    process = _CapacityProcess(failures=1)
    waits: list[float] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    starts = [
        message for message in process.messages if message.get("method") == "turn/start"
    ]
    assert result.status == "completed"
    assert result.turn_id == "capacity-turn-2"
    assert result.final_message == "done"
    assert waits == [30.0]
    assert starts[0]["params"]["input"][0]["text"] == "继续当前实验"
    assert starts[1]["params"]["input"] == []


@pytest.mark.asyncio
async def test_server_overloaded_retry_is_bounded(monkeypatch, tmp_path):
    process = _CapacityProcess(failures=99)
    waits: list[float] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    starts = [
        message for message in process.messages if message.get("method") == "turn/start"
    ]
    assert result.status == "failed"
    assert result.turn_id == "capacity-turn-7"
    assert waits == [30.0, 60.0, 120.0, 300.0, 600.0, 900.0]
    assert len(starts) == 7
    assert starts[0]["params"]["input"][0]["text"] == "继续当前实验"
    assert all(message["params"]["input"] == [] for message in starts[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("options, expected_waits, reason", [
    ({"continuation_max_attempts": 0}, [], "disabled"),
    ({"continuation_max_attempts": 1}, [30.0], "attempt_limit"),
    ({"continuation_wait_budget_seconds": 29}, [], "wait_budget"),
    ({"continuation_wait_budget_seconds": 90}, [30.0, 60.0], "wait_budget"),
    ({}, [30.0, 60.0, 120.0, 300.0, 600.0, 900.0], "attempts_exhausted"),
])
async def test_continuation_controls_and_terminal_events(
    monkeypatch, tmp_path, options, expected_waits, reason
):
    process = _CapacityProcess(failures=99)
    waits = []
    observed = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[], event_handler=observed.append, **options
    )
    schema = {"type": "object"}
    try:
        result = await runtime.run_turn("只执行一次原请求", output_schema=schema)
    finally:
        await runtime.close()

    assert waits == expected_waits
    retries = [event for event in result.events if event.kind == "mac/continuationRetry"]
    assert all(event in observed for event in retries)
    assert [event.payload["phase"] for event in retries] == (
        [phase for _delay in waits for phase in ("waiting", "resuming")] + ["exhausted"]
    )
    assert retries[-1].payload["attempt"] == len(waits)
    assert retries[-1].payload["stopReason"] == reason
    assert retries[-1].payload["scheduledWaitSeconds"] <= runtime.continuation_wait_budget_seconds
    starts = [message for message in process.messages if message.get("method") == "turn/start"]
    assert len(starts) == len(waits) + 1
    assert all(message["params"]["threadId"] == "capacity-thread" for message in starts)
    assert all(message["params"]["outputSchema"] == schema for message in starts)
    assert all(message["params"]["input"] == [] for message in starts[1:])
    assert all("model" not in message["params"] for message in starts)
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_steer_without_active_turn_raises_before_request(tmp_path):
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    runtime.thread_id = "thread-1"
    runtime.active_turn_id = None
    try:
        with pytest.raises(
            AppServerProtocolError, match="without an active turn"
        ):
            await runtime.steer(None, "继续")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_continuation_wait_can_be_cancelled_immediately(monkeypatch, tmp_path):
    process = _CapacityProcess(failures=99)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def long_wait(_delay):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", long_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    task = asyncio.create_task(runtime.run_turn("继续"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await runtime.interrupt("capacity-turn-1") is True
        result = await asyncio.wait_for(task, 1)
        assert result.status == "interrupted"
        assert cleaned.is_set()
        assert process.turn_attempts == 1
        assert not any(message.get("method") == "turn/interrupt" for message in process.messages)
        assert result.events[-1].payload["phase"] == "cancelled"
        process.failures = 0
        assert (await runtime.run_turn("新的请求")).status == "completed"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["waiting", "resuming"])
async def test_stop_during_retry_projection_never_starts_another_turn(
    monkeypatch, tmp_path, phase
):
    process = _CapacityProcess(failures=99)

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(_delay):
        return None

    async def on_event(event):
        if event.kind == "mac/continuationRetry" and event.payload["phase"] == phase:
            assert await runtime.interrupt() is True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[], event_handler=on_event)
    try:
        result = await asyncio.wait_for(runtime.run_turn("继续"), 1)
    finally:
        await runtime.close()
    assert result.status == "interrupted"
    assert process.turn_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["unauthorized", "usageLimitExceeded", "badRequest", "sandboxError"])
async def test_non_transient_errors_never_start_continuation(monkeypatch, tmp_path, error_code):
    process = _CapacityProcess(failures=99, codex_error_info=error_code)

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续")
    finally:
        await runtime.close()
    assert result.status == "failed"
    assert process.turn_attempts == 1
    assert not any(event.kind == "mac/continuationRetry" for event in result.events)


@pytest.mark.asyncio
async def test_retry_projection_failure_does_not_replay_success(monkeypatch, tmp_path):
    process = _CapacityProcess(failures=1)

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(_delay):
        return None

    def broken_projection(event):
        if event.kind == "mac/continuationRetry":
            raise RuntimeError("card transport unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[], event_handler=broken_projection)
    try:
        result = await runtime.run_turn("继续")
    finally:
        await runtime.close()
    assert result.status == "completed"
    assert result.final_message == "done"
    assert process.turn_attempts == 2
    assert result.events[-1].payload["phase"] == "recovered"


@pytest.mark.asyncio
async def test_error_change_stops_without_resetting_retry_budget(monkeypatch, tmp_path):
    class ChangedErrorProcess(_CapacityProcess):
        async def receive(self, message):
            if message.get("method") == "turn/start" and self.turn_attempts:
                # A change to a non-retryable classification ends the
                # continuation; retryable forms changing shape no longer do.
                self.codex_error_info = "unauthorized"
            await super().receive(message)

    process = ChangedErrorProcess(failures=99)
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续")
    finally:
        await runtime.close()
    assert waits == [30.0]
    assert process.turn_attempts == 2
    assert result.events[-1].payload["stopReason"] == "error_changed"


@pytest.mark.asyncio
async def test_retryable_error_form_change_keeps_continuation(monkeypatch, tmp_path):
    # The same network outage surfaces under different retryable
    # classifications across turns: an httpConnectionFailed terminal status
    # first, then the upstream "other" message form after Codex exhausts
    # its native stream reconnects. The continuation must ride out the
    # outage on the original bounded plan instead of stopping on the form
    # change.
    class TransportFormProcess(_CapacityProcess):
        async def receive(self, message):
            if message.get("method") == "turn/start" and self.turn_attempts:
                self.codex_error_info = "other"
                self.error_message = (
                    "stream disconnected before completion: "
                    "stream closed before response.completed"
                )
            await super().receive(message)

    process = TransportFormProcess(
        failures=2, codex_error_info={"httpConnectionFailed": {"httpStatusCode": None}},
        error_message="Connection failed: error sending request",
    )
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续")
    finally:
        await runtime.close()
    assert waits == [5.0, 15.0]
    assert process.turn_attempts == 3
    assert result.status == "completed"
    retry_events = [event for event in result.events if event.kind == "mac/continuationRetry"]
    assert [event.payload["phase"] for event in retry_events] == [
        "waiting", "resuming", "waiting", "resuming", "recovered",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fraction, expected", [(0, 27.0), (1, 30.0)])
async def test_retry_jitter_never_exceeds_previous_ceiling(monkeypatch, tmp_path, fraction, expected):
    process = _CapacityProcess(failures=1)
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    monkeypatch.setattr("mac_system.agents.root_runtime.random.uniform", lambda low, high: low + fraction * (high - low))
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[], continuation_jitter_ratio=0.1)
    try:
        result = await runtime.run_turn("继续")
    finally:
        await runtime.close()
    assert waits == [expected]
    assert result.status == "completed"


@pytest.mark.parametrize("terminal_code", [None, "other", "unauthorized", "usageLimitExceeded"])
def test_terminal_failure_does_not_reuse_earlier_transient_classification(terminal_code):
    result = TurnResult(
        thread_id="thread-1", turn_id="turn-1", status="failed", final_message=None,
        events=(
            AgentEvent(
                kind="error", thread_id="thread-1", turn_id="turn-1",
                payload={"willRetry": True, "error": {
                    "message": "Connection failed", "codexErrorInfo": "responseStreamDisconnected",
                }},
            ),
            AgentEvent(
                kind="turn/completed", thread_id="thread-1", turn_id="turn-1",
                payload={"turn": {"status": "failed", "error": {
                    "message": "The terminal failure is not a transport error.",
                    "codexErrorInfo": terminal_code,
                }}},
            ),
        ),
    )
    assert _turn_error_code(result) == terminal_code


@pytest.mark.asyncio
@pytest.mark.parametrize("error_info,message,expected_delay", [
    ("other", "stream disconnected before completion: stream closed before response.completed", 5.0),
    (None, "stream disconnected before completion: stream closed before response.completed", 5.0),
    ("other", "stream disconnected before completion: error sending request", 5.0),
    ({"responseTooManyFailedAttempts": {"httpStatusCode": None}},
     "stream disconnected before completion: stream closed before response.completed", 5.0),
    ("other", "exceeded retry limit, last status: 429 Too Many Requests, request id: test", 30.0),
    (None, "exceeded retry limit, last status: 503 Service Unavailable", 30.0),
    ({"responseTooManyFailedAttempts": {"httpStatusCode": 429}}, "HTTP request failed", 30.0),
    ({"httpConnectionFailed": {"httpStatusCode": 502}}, "HTTP request failed", 30.0),
    ({"responseStreamDisconnected": {"httpStatusCode": 429}}, "HTTP request failed", 30.0),
    ("internalServerError", "Internal server error", 30.0),
    ("other", "The upstream turn failed.", None),
    ("other", "exceeded retry limit, last status: 401 Unauthorized", None),
    ("other", "exceeded retry limit, last status: ", None),
    ("other", "A tool mentioned stream disconnected before completion: an example", None),
    # Account-level concurrency rejection is temporary: retry on the
    # capacity backoff plan. Auth/quota 403s without the upstream
    # signature stay non-retryable.
    (None, "{'code': -32003, 'message': 'Error code: 403 - {\"error\": {\"message\": \"You've reached your concurrent request limit. Please wait for your ongoing requests to finish and try again.\", \"type\": \"access_terminated_error\"}}', 'data': None}", 30.0),
    ("other", "Error code: 403 - concurrent request limit reached", 30.0),
    (None, "Error code: 403 - {'error': {'message': 'Unauthorized request', 'type': 'access_terminated'}}", None),
    (None, "Error code: 403 Forbidden", None),
    ("usageLimitExceeded", "stream disconnected before completion: stream closed", None),
    ("usageLimitExceeded", "You've reached your concurrent request limit.", None),
    ("unauthorized", "exceeded retry limit, last status: 429 Too Many Requests", None),
    ({"responseStreamDisconnected": {"httpStatusCode": 403}},
     "stream disconnected before completion: stream closed", None),
])
async def test_transport_failure_continuation_preserves_original_error(
    monkeypatch, tmp_path, error_info, message, expected_delay
):
    process = _CapacityProcess(
        failures=1, codex_error_info=error_info, error_message=message,
    )
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续已有任务，不重发原请求")
    finally:
        await runtime.close()

    retry_events = [event for event in result.events if event.kind == "mac/continuationRetry"]
    assert waits == ([] if expected_delay is None else [expected_delay])
    assert result.status == ("failed" if expected_delay is None else "completed")
    assert process.turn_attempts == (1 if expected_delay is None else 2)
    assert [event.payload["phase"] for event in retry_events] == (
        [] if expected_delay is None else ["waiting", "resuming", "recovered"]
    )
    error_event = next(event for event in result.events if event.kind == "error")
    assert error_event.payload["error"] == {"message": message, "codexErrorInfo": error_info}
    starts = [message for message in process.messages if message.get("method") == "turn/start"]
    assert all(message["params"]["input"] == [] for message in starts[1:])
    assert all(message["params"]["threadId"] == "capacity-thread" for message in starts)
    assert all("model" not in message["params"] for message in starts)


@pytest.mark.asyncio
async def test_rate_limit_recovers_after_short_retries_without_user_input(monkeypatch, tmp_path):
    process = _CapacityProcess(
        failures=4, codex_error_info="other",
        error_message="exceeded retry limit, last status: 429 Too Many Requests, request id: test",
    )
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr("mac_system.agents.root_runtime._wait_for_server_capacity", no_wait)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("保留已有进度")
    finally:
        await runtime.close()

    assert waits == [30.0, 60.0, 120.0, 300.0]
    assert result.status == "completed"
    assert result.final_message == "done"
    assert process.turn_attempts == 5
    assert result.events[-1].payload["phase"] == "recovered"


@pytest.mark.asyncio
async def test_response_stream_disconnect_continues_same_thread_with_empty_input(
    monkeypatch, tmp_path
):
    process = _CapacityProcess(
        failures=1,
        codex_error_info={
            "responseStreamDisconnected": {"httpStatusCode": None},
        },
    )
    waits: list[float] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    starts = [
        message for message in process.messages if message.get("method") == "turn/start"
    ]
    assert result.status == "completed"
    assert result.turn_id == "capacity-turn-2"
    assert waits == [5.0]
    assert sum(
        message.get("method") == "thread/start" for message in process.messages
    ) == 1
    assert starts[0]["params"]["input"][0]["text"] == "继续当前实验"
    assert starts[1]["params"]["input"] == []


@pytest.mark.asyncio
async def test_response_stream_disconnect_retry_is_bounded(monkeypatch, tmp_path):
    process = _CapacityProcess(
        failures=99,
        codex_error_info={
            "responseStreamDisconnected": {"httpStatusCode": None},
        },
    )
    waits: list[float] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    starts = [
        message for message in process.messages if message.get("method") == "turn/start"
    ]
    assert result.status == "failed"
    assert result.turn_id == "capacity-turn-9"
    assert waits == [5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0]
    assert sum(
        message.get("method") == "thread/start" for message in process.messages
    ) == 1
    assert len(starts) == 9
    assert starts[0]["params"]["input"][0]["text"] == "继续当前实验"
    assert all(message["params"]["input"] == [] for message in starts[1:])


@pytest.mark.asyncio
async def test_stream_disconnect_retry_forces_proxy_recovery_when_probe_passes(
    monkeypatch, tmp_path
):
    process = _CapacityProcess(
        failures=2,
        codex_error_info={
            "responseStreamDisconnected": {"httpStatusCode": None},
        },
    )
    waits: list[float] = []
    recovery_forces: list[bool] = []
    recovery_targets: list[tuple[str, ...]] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    async def reachable_probe(self, timeout_seconds: float = 5.0):
        return True

    async def fake_recovery(
        target_urls, *, socket_path, cooldown_seconds, force=False
    ):
        recovery_targets.append(tuple(target_urls))
        recovery_forces.append(force)
        return [("节点选择", "旧节点", "新节点")]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        CodexAppServerThread, "probe_provider_reachable", reachable_probe
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime.recover_dead_proxy_nodes", fake_recovery
    )
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        proxy_recovery_enabled=True,
    )
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    assert result.status == "completed"
    # Recovery is only forced once two consecutive attempts failed with a
    # transport-class error, even though the probe keeps succeeding, and it
    # must validate nodes against every Codex provider endpoint.
    assert recovery_forces == [True]
    assert recovery_targets == [
        ("https://chatgpt.com", "https://auth.openai.com")
    ]


@pytest.mark.asyncio
async def test_single_stream_failure_after_overload_does_not_force_proxy_recovery(
    monkeypatch, tmp_path
):
    process = _CapacityProcess(
        failures=2,
        codex_error_info=[
            "serverOverloaded",
            {"responseStreamDisconnected": {"httpStatusCode": None}},
        ],
    )
    waits: list[float] = []
    recovery_forces: list[bool] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    async def fake_recovery(
        target_urls, *, socket_path, cooldown_seconds, force=False
    ):
        recovery_forces.append(force)
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime.recover_dead_proxy_nodes", fake_recovery
    )
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        proxy_recovery_enabled=True,
    )
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    assert result.status == "completed"
    # One upstream stream disconnect after an overload retry is not proxy
    # evidence; a single occurrence must not repoint the selected node.
    assert recovery_forces == []


@pytest.mark.asyncio
async def test_stream_watchdog_silence_does_not_force_proxy_recovery(
    monkeypatch, tmp_path
):
    class SilentTwiceProcess(_FakeProcess):
        def __init__(self):
            super().__init__()
            self.turn_attempts = 0

        async def receive(self, message):
            if message.get("method") == "turn/start":
                self.messages.append(message)
                self.turn_attempts += 1
                turn_id = f"turn-{self.turn_attempts}"
                self._feed({"id": message["id"], "result": {"turn": {"id": turn_id}}})
                self._feed({"method": "turn/started", "params": {
                    "threadId": "thread-1",
                    "turn": {"id": turn_id, "status": "inProgress"},
                }})
                if self.turn_attempts == 3:
                    self._feed({"method": "item/completed", "params": {
                        "threadId": "thread-1",
                        "turnId": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "id": "answer",
                            "text": "done",
                        },
                    }})
                    self._feed({"method": "turn/completed", "params": {
                        "threadId": "thread-1",
                        "turn": {"id": turn_id, "status": "completed"},
                    }})
                return
            await super().receive(message)

    process = SilentTwiceProcess()
    waits: list[float] = []
    recovery_forces: list[bool] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    async def fake_recovery(
        target_urls, *, socket_path, cooldown_seconds, force=False
    ):
        recovery_forces.append(force)
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime.recover_dead_proxy_nodes", fake_recovery
    )
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        silence_timeout_seconds=0.02,
        proxy_recovery_enabled=True,
    )
    try:
        result = await asyncio.wait_for(runtime.run_turn("original request"), 2)
    finally:
        await runtime.close()

    assert result.status == "completed"
    assert waits == [5.0, 15.0]
    # A slow or stalled local stream is ambiguous, not proxy evidence; only a
    # positively broken probe may repoint the node.
    assert recovery_forces == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_result", "auth_result", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (True, OSError("probe timeout"), None),
    ],
)
async def test_provider_probe_requires_chat_and_auth_hosts(
    monkeypatch, tmp_path, chat_result, auth_result, expected
):
    probed: list[str] = []
    results = {"chatgpt.com": chat_result, "auth.openai.com": auth_result}

    async def fake_probe(host, port, timeout_seconds):
        probed.append(host)
        assert port == 443
        result = results[host]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(
        CodexAppServerThread, "probe_provider_reachable", _ORIGINAL_PROVIDER_PROBE
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._probe_https_reachable", fake_probe
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    assert await runtime.probe_provider_reachable() is expected
    assert sorted(probed) == ["auth.openai.com", "chatgpt.com"]


@pytest.mark.asyncio
async def test_overloaded_retry_never_forces_proxy_recovery(monkeypatch, tmp_path):
    process = _CapacityProcess(failures=1, codex_error_info="serverOverloaded")
    waits: list[float] = []
    recovery_calls: list[bool] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    async def reachable_probe(self, timeout_seconds: float = 5.0):
        return True

    async def fake_recovery(
        target_url, *, socket_path, cooldown_seconds, force=False
    ):
        recovery_calls.append(force)
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        CodexAppServerThread, "probe_provider_reachable", reachable_probe
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime.recover_dead_proxy_nodes", fake_recovery
    )
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        proxy_recovery_enabled=True,
    )
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    assert result.status == "completed"
    # serverOverloaded is provider-side, not a broken proxy node.
    assert recovery_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failures, expected_waits, final_phase", [
    (6, [5.0, 15.0, 30.0, 60.0, 120.0, 300.0], "recovered"),
    (99, [5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0], "exhausted"),
])
async def test_provider_outage_keeps_bounded_continuation(
    monkeypatch, tmp_path, failures, expected_waits, final_phase
):
    process = _CapacityProcess(
        failures=failures,
        codex_error_info={
            "responseStreamDisconnected": {"httpStatusCode": None},
        },
    )
    waits: list[float] = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    async def outage_probe(self, timeout_seconds: float = 5.0):
        return failures == 6 and process.turn_attempts >= 6

    async def no_proxy_recovery(*_args, **_kwargs):
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        CodexAppServerThread, "probe_provider_reachable", outage_probe
    )
    monkeypatch.setattr(
        "mac_system.agents.root_runtime.recover_dead_proxy_nodes", no_proxy_recovery
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("继续当前实验")
    finally:
        await runtime.close()

    starts = [
        message for message in process.messages if message.get("method") == "turn/start"
    ]
    assert waits == expected_waits
    assert len(starts) == len(waits) + 1
    assert starts[0]["params"]["input"][0]["text"] == "继续当前实验"
    assert all(message["params"]["input"] == [] for message in starts[1:])
    assert all(message["params"]["threadId"] == "capacity-thread" for message in starts)
    assert all("model" not in message["params"] for message in starts)
    retries = [event.payload for event in result.events if event.kind == "mac/continuationRetry"]
    waiting = [payload for payload in retries if payload["phase"] == "waiting"]
    assert waiting[0]["providerReachable"] is None
    assert all(payload["providerReachable"] is False for payload in waiting[1:5])
    assert retries[-1]["phase"] == final_phase
    assert retries[-1]["attempt"] == len(waits)
    assert retries[-1]["scheduledWaitSeconds"] == sum(waits)
    if final_phase == "recovered":
        assert waiting[-1]["providerReachable"] is True
        assert result.status == "completed"
        assert result.final_message == "done"
    else:
        assert all(payload["providerReachable"] is False for payload in waiting[1:])
        assert result.status == "failed"
        assert retries[-1]["stopReason"] == "attempts_exhausted"
        assert _turn_error_code(result) == "responseStreamDisconnected"


def test_https_probe_treats_refused_local_proxy_as_unreachable():
    # A stopped local proxy (e.g. Clash Verge quit) is a definitive outage,
    # not probe uncertainty; progress should report the observed outage.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert _https_reachable_blocking(
        "chatgpt.com", 443, 1.0, ("127.0.0.1", port, None)
    ) is False


@pytest.mark.asyncio
async def test_probe_tcp_reachable_direct_success():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _probe_tcp_reachable("127.0.0.1", port, 2) is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_tcp_reachable_refused_raises():
    probe_socket = socket.socket()
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    with pytest.raises(OSError):
        await _probe_tcp_reachable("127.0.0.1", port, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(b"HTTP/1.1 200 Connection established", True), (b"HTTP/1.1 502 Bad Gateway", False)],
)
async def test_probe_tcp_reachable_via_connect_proxy(monkeypatch, status, expected):
    requests: list[bytes] = []

    async def handle(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(status + b"\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    try:
        assert await _probe_tcp_reachable("chatgpt.com", 443, 2) is expected
    finally:
        server.close()
        await server.wait_closed()
    assert requests and requests[0].startswith(b"CONNECT chatgpt.com:443 ")


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [0, 1])
@pytest.mark.parametrize("terminal_status", ["completed", "interrupted"])
@pytest.mark.parametrize("error_info, retry_delay", [
    ("serverOverloaded", 30.0),
    ({"responseStreamDisconnected": {"httpStatusCode": None}}, 5.0),
    ("responseStreamConnectionFailed", 5.0),
    ("httpConnectionFailed", 5.0),
])
async def test_native_recovery_terminal_status_stops_mac_retries(
    monkeypatch, tmp_path, failures, terminal_status, error_info, retry_delay
):
    process = _CapacityProcess(
        failures=failures, codex_error_info=error_info,
        recovery_status=terminal_status,
    )
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await runtime.run_turn("保留恢复成功的答案")
    finally:
        await runtime.close()

    assert process.turn_attempts == failures + 1
    assert waits == ([retry_delay] if failures else [])
    assert result.status == terminal_status
    assert result.turn_id == f"capacity-turn-{failures + 1}"
    if terminal_status == "completed":
        assert result.final_message == "done"
    else:
        assert not result.final_message
    assert sum(event.kind == "error" for event in result.events) == failures + 1


@pytest.mark.asyncio
async def test_turn_completed_does_not_wait_forever_for_local_request_hook(
    monkeypatch, tmp_path
):
    process = _CompletedWithStaleRequestProcess()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def stuck_hook(_method, _params):
        hook_started.set()
        process.allow_completion.set()
        try:
            await asyncio.Event().wait()
        finally:
            hook_cancelled.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        server_request_handler=stuck_hook,
    )
    try:
        result = await asyncio.wait_for(runtime.run_turn("finish"), timeout=1)
    finally:
        await runtime.close()

    assert hook_started.is_set()
    assert hook_cancelled.is_set()
    assert result.status == "completed"
    assert any(event.kind == "server_request" for event in result.events)


@pytest.mark.asyncio
async def test_run_turn_follows_authoritative_started_turn(monkeypatch, tmp_path):
    process = _ScriptedTurnProcess(
        [
            {
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-live", "status": "inProgress"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-response",
                    "item": {"type": "agentMessage", "text": "stale result"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-response", "status": "completed"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-live",
                    "item": {"type": "agentMessage", "text": "live result"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-live", "status": "completed"},
                },
            },
        ]
    )

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await asyncio.wait_for(runtime.run_turn("report"), timeout=1)
    finally:
        await runtime.close()

    assert result.turn_id == "turn-live"
    assert result.status == "completed"
    assert result.final_message == "live result"


@pytest.mark.asyncio
async def test_run_turn_ignores_unstarted_turn_completion(monkeypatch, tmp_path):
    process = _ScriptedTurnProcess(
        [
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-unrelated", "status": "completed"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-response",
                    "item": {"type": "agentMessage", "text": "expected result"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-response", "status": "completed"},
                },
            },
        ]
    )

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        result = await asyncio.wait_for(runtime.run_turn("report"), timeout=1)
    finally:
        await runtime.close()

    assert result.turn_id == "turn-response"
    assert result.final_message == "expected result"


@pytest.mark.asyncio
async def test_codex_root_answers_parallel_native_tool_requests_concurrently(
    monkeypatch, tmp_path
):
    process = _ParallelToolProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    both_started = asyncio.Event()
    active = 0
    max_active = 0
    providers = []

    async def run_parallel(arguments):
        nonlocal active, max_active
        providers.append(arguments["provider"])
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        await both_started.wait()
        await asyncio.sleep(0)
        active -= 1
        return {"provider": arguments["provider"], "status": "completed"}

    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[
            DynamicTool(
                name="parallel_tool",
                description="Run one independent backend",
                input_schema={"type": "object"},
                handler=run_parallel,
            )
        ],
    )
    try:
        result = await asyncio.wait_for(runtime.run_turn("compare"), timeout=2)
    finally:
        await runtime.close()

    assert max_active == 2
    assert set(providers) == {"claude_code", "pi"}
    assert len(process.tool_responses) == 2
    assert result.final_message == "both done"


@pytest.mark.asyncio
async def test_interrupt_uses_live_notification_turn_id(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        await runtime.create_thread()
        process._feed(
            {
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "live-goal-turn", "status": "inProgress"},
                },
            }
        )
        for _ in range(20):
            if runtime.active_turn_id == "live-goal-turn":
                break
            await asyncio.sleep(0)
        assert await runtime.interrupt("stale-mac-turn") is True
    finally:
        await runtime.close()

    interrupt = next(
        message
        for message in process.messages
        if message.get("method") == "turn/interrupt"
    )
    assert interrupt["params"] == {
        "threadId": "thread-1",
        "turnId": "live-goal-turn",
    }


@pytest.mark.asyncio
async def test_clean_background_terminals_uses_native_codex_protocol(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    try:
        await runtime.create_thread()
        await runtime.clean_background_terminals()
    finally:
        await runtime.close()

    clean = next(
        message
        for message in process.messages
        if message.get("method") == "thread/backgroundTerminals/clean"
    )
    assert clean["params"] == {"threadId": "thread-1"}


@pytest.mark.asyncio
async def test_root_codex_home_isolated_from_global_tools_and_reuses_only_auth(
    monkeypatch, tmp_path
):
    global_home = tmp_path / "global-codex"
    isolated_home = tmp_path / "root-codex"
    global_home.mkdir()
    (global_home / "auth.json").write_text("{}", encoding="utf-8")
    (global_home / "config.toml").write_text(
        '[mcp_servers.node_repl]\ncommand = "node"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(global_home))
    process = _FakeProcess()
    process_kwargs = {}

    async def fake_subprocess(*_args, **kwargs):
        process_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Coordinate.",
        tools=[],
        codex_home=isolated_home,
    )
    try:
        await runtime.start()
    finally:
        await runtime.close()

    assert process_kwargs["env"]["CODEX_HOME"] == str(isolated_home.resolve())
    assert process_kwargs["limit"] == 16 * 1024 * 1024
    assert (isolated_home / "auth.json").resolve() == (global_home / "auth.json").resolve()
    assert not (isolated_home / "config.toml").exists()


@pytest.mark.asyncio
async def test_codex_root_registers_native_tools_and_can_resume_and_steer(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Coordinate.",
        tools=[
            DynamicTool(
                name="run_external_agent",
                description="Start an agent",
                input_schema={"type": "object", "additionalProperties": False},
                handler=lambda _: "ok",
            )
        ],
        thread_config={"model_auto_compact_token_limit": 80_000},
    )
    try:
        await runtime.create_thread()
        assert runtime.active_model == "gpt-5.6-codex"
        await runtime.resume_thread("thread-1")
        assert runtime.active_model == "gpt-5.6-codex"
        await runtime.steer("turn-active", "new user correction")
        await runtime.interrupt("turn-active")
    finally:
        await runtime.close()

    start = next(message for message in process.messages if message.get("method") == "thread/start")
    assert start["params"]["ephemeral"] is False
    assert start["params"]["dynamicTools"][0]["name"] == "run_external_agent"
    resume = next(
        message for message in process.messages if message.get("method") == "thread/resume"
    )
    assert resume["params"]["threadId"] == "thread-1"
    assert resume["params"]["excludeTurns"] is True
    assert start["params"]["config"] == {"model_auto_compact_token_limit": 80_000}
    assert resume["params"]["config"] == start["params"]["config"]
    steer = next(message for message in process.messages if message.get("method") == "turn/steer")
    assert steer["params"]["expectedTurnId"] == "turn-active"
    assert steer["params"]["input"][0]["text"] == "new user correction"
    interrupt = next(
        message for message in process.messages if message.get("method") == "turn/interrupt"
    )
    assert interrupt["params"] == {
        "threadId": "thread-1",
        "turnId": "turn-active",
    }


@pytest.mark.asyncio
async def test_codex_runtime_restarts_when_stdout_reader_has_stopped(
    monkeypatch, tmp_path
):
    processes = [_FakeProcess(), _FakeProcess()]

    async def fake_subprocess(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(codex_bin="codex", cwd=tmp_path, tools=[])
    first = processes[0]
    try:
        await runtime.resume_thread("thread-1")
        first.stderr.feed_data(b"old transport diagnostic\n")
        while not runtime._stderr_lines:
            await asyncio.sleep(0)
        first.stdout.feed_eof()
        assert runtime._reader_task is not None
        await runtime._reader_task
        assert not runtime._inbound.empty()

        await runtime.resume_thread("thread-1")

        assert runtime._process is not first
        assert first.returncode == 0
        assert runtime._reader_task is not None
        assert not runtime._reader_task.done()
        assert not runtime._stderr_lines
        result = await asyncio.wait_for(runtime.run_turn("verify recovery"), 1)
        assert result.thread_id == "thread-1"
        assert result.status == "completed"
        assert result.final_message == "done"
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancelled"])
async def test_codex_runtime_releases_failed_initialize_before_retry(
    monkeypatch, tmp_path, failure
):
    initializing = asyncio.Event()

    class UnresponsiveInitializeProcess(_FakeProcess):
        async def receive(self, message):
            if message.get("method") == "initialize":
                self.messages.append(message)
                initializing.set()
                return
            await super().receive(message)

    first = UnresponsiveInitializeProcess()
    second = _FakeProcess()
    processes = [first, second]

    async def fake_subprocess(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex", cwd=tmp_path, tools=[],
        request_timeout_seconds=0.01 if failure == "timeout" else 60,
        startup_timeout_seconds=0.01 if failure == "timeout" else 60,
    )
    try:
        if failure == "timeout":
            with pytest.raises(AppServerProtocolError, match="initialize timed out"):
                await runtime.resume_thread("thread-1")
        else:
            task = asyncio.create_task(runtime.resume_thread("thread-1"))
            await asyncio.wait_for(initializing.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert first.returncode == 0
        assert runtime._process is None
        assert runtime._reader_task is None
        assert runtime._stderr_task is None
        assert not runtime._pending

        await runtime.resume_thread("thread-1")
        result = await asyncio.wait_for(runtime.run_turn("verify recovery"), 1)
        assert runtime._process is second
        assert result.thread_id == "thread-1"
        assert result.status == "completed"
        assert result.final_message == "done"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_runtime_timeout_names_the_failed_protocol_method(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        request_timeout_seconds=0.01,
    )
    try:
        await runtime.start()
        with pytest.raises(
            AppServerProtocolError,
            match=r"thread/unknown timed out after 0.01s",
        ):
            await runtime._request("thread/unknown", {})
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_resume_waits_for_native_rollout_restore_past_rpc_timeout(
    monkeypatch, tmp_path
):
    class SlowResumeProcess(_FakeProcess):
        async def receive(self, message):
            if message.get("method") == "thread/resume":
                await asyncio.sleep(0.03)
            await super().receive(message)

    process = SlowResumeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        request_timeout_seconds=0.01,
    )
    try:
        assert await runtime.resume_thread("thread-large") == "thread-1"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_startup_handshake_waits_past_rpc_timeout(
    monkeypatch, tmp_path
):
    """Cold-start ``initialize``/``thread/start`` may outlast the 60s RPC
    timeout; a bounded startup budget must let them finish instead of
    tearing the app-server down mid-handshake."""

    class SlowHandshakeProcess(_FakeProcess):
        async def receive(self, message):
            if message.get("method") in {"initialize", "thread/start"}:
                await asyncio.sleep(0.03)
            await super().receive(message)

    process = SlowHandshakeProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        request_timeout_seconds=0.01,
    )
    try:
        assert await runtime.create_thread() == "thread-1"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_thread_start_timeout_names_the_failed_method(
    monkeypatch, tmp_path
):
    class UnresponsiveThreadStartProcess(_FakeProcess):
        async def receive(self, message):
            if message.get("method") == "thread/start":
                self.messages.append(message)
                return
            await super().receive(message)

    process = UnresponsiveThreadStartProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        tools=[],
        request_timeout_seconds=60,
        startup_timeout_seconds=0.01,
    )
    try:
        with pytest.raises(
            AppServerProtocolError,
            match=r"thread/start timed out after 0.01s",
        ):
            await runtime.create_thread()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_codex_root_can_disable_execution_environment_without_hiding_host_tools(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerThread(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Delegate all project work.",
        tools=[
            DynamicTool(
                name="run_external_agent",
                description="Start an agent",
                input_schema={"type": "object"},
                handler=lambda _: "ok",
            )
        ],
        environments=[],
        thread_config={"features.multi_agent": False},
    )
    try:
        await runtime.create_thread()
    finally:
        await runtime.close()

    start = next(message for message in process.messages if message.get("method") == "thread/start")
    assert start["params"]["environments"] == []
    assert start["params"]["config"] == {"features.multi_agent": False}
    assert [tool["name"] for tool in start["params"]["dynamicTools"]] == ["run_external_agent"]
