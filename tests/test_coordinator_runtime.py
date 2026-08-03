import asyncio
import json

import pytest

from mac_system.agents.coordinator_runtime import (
    CodexAppServerCoordinator,
    CoordinatorTool,
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
                    "result": {"thread": {"id": "thread-1"}},
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
                        "tool": "mac_spawn_agent",
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
        elif method == "turn/steer":
            self._feed({"jsonrpc": "2.0", "id": request_id, "result": {}})

    def terminate(self):
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self):
        self.terminate()

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_codex_coordinator_keeps_tool_result_in_same_turn(monkeypatch, tmp_path):
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

    runtime = CodexAppServerCoordinator(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Coordinate with MAC tools.",
        tools=[
            CoordinatorTool(
                name="mac_spawn_agent",
                description="Start a coding-agent backend",
                input_schema={"type": "object"},
                handler=spawn,
            )
        ],
    )
    try:
        result = await runtime.run_turn("implement the request")
    finally:
        await runtime.close()

    assert calls == [{"provider": "claude_code", "message": "inspect"}]
    assert process_args[-2:] == ("--disable", "code_mode_host")
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
    assert any(event.kind == "tool_call" for event in result.events)


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
    runtime = CodexAppServerCoordinator(
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
async def test_codex_coordinator_registers_native_tools_and_can_resume_and_steer(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerCoordinator(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Coordinate.",
        tools=[
            CoordinatorTool(
                name="mac_spawn_agent",
                description="Start an agent",
                input_schema={"type": "object", "additionalProperties": False},
                handler=lambda _: "ok",
            )
        ],
    )
    try:
        await runtime.create_thread()
        await runtime.resume_thread("thread-1")
        await runtime.steer("turn-active", "new user correction")
    finally:
        await runtime.close()

    start = next(message for message in process.messages if message.get("method") == "thread/start")
    assert start["params"]["ephemeral"] is False
    assert start["params"]["dynamicTools"][0]["name"] == "mac_spawn_agent"
    resume = next(
        message for message in process.messages if message.get("method") == "thread/resume"
    )
    assert resume["params"]["threadId"] == "thread-1"
    steer = next(message for message in process.messages if message.get("method") == "turn/steer")
    assert steer["params"]["expectedTurnId"] == "turn-active"
    assert steer["params"]["input"][0]["text"] == "new user correction"


@pytest.mark.asyncio
async def test_codex_coordinator_can_disable_execution_environment_without_hiding_host_tools(
    monkeypatch, tmp_path
):
    process = _FakeProcess()

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    runtime = CodexAppServerCoordinator(
        codex_bin="codex",
        cwd=tmp_path,
        developer_instructions="Delegate all project work.",
        tools=[
            CoordinatorTool(
                name="mac_spawn_agent",
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
    assert [tool["name"] for tool in start["params"]["dynamicTools"]] == ["mac_spawn_agent"]
