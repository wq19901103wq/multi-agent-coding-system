"""Thin Codex app-server client used by MAC's persistent root Agent.

The turn loop, built-in tools, context management, multi-Agent control and
sub-Agent sessions remain owned by Codex. MAC only transports user input,
answers app-server requests and projects native events onto its UI/storage.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]
ToolHandler = Callable[..., Awaitable[Any] | Any]
EventHandler = Callable[["AgentEvent"], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, JsonObject], Awaitable[JsonObject] | JsonObject]
logger = logging.getLogger(__name__)
_APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024
_PROCESS_CLOSED_METHOD = "mac/process/closed"


class AppServerProtocolError(RuntimeError):
    """Raised when the Codex app-server violates its JSON-RPC contract."""


@dataclass(frozen=True)
class DynamicTool:
    """One MAC extension advertised through Codex dynamic tools."""

    name: str
    description: str
    input_schema: JsonObject
    handler: ToolHandler
    defer_loading: bool = False

    def codex_spec(self) -> JsonObject:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "deferLoading": self.defer_loading,
        }


@dataclass(frozen=True)
class AgentEvent:
    """Native app-server event projected to MAC storage and UI."""

    kind: str
    thread_id: str
    turn_id: str | None
    payload: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    thread_id: str
    turn_id: str
    call_id: str
    tool_name: str


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    status: str
    final_message: str | None
    events: tuple[AgentEvent, ...]


class CodexAppServerThread:
    """Drive one persistent Codex root thread through app-server JSON-RPC."""

    def __init__(
        self,
        *,
        codex_bin: Path | str,
        cwd: Path,
        tools: list[DynamicTool],
        developer_instructions: str | None = None,
        model: str | None = None,
        event_handler: EventHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        request_timeout_seconds: float = 60.0,
        sandbox: str = "read-only",
        approval_policy: str = "never",
        ephemeral: bool = False,
        environments: list[JsonObject] | None = None,
        thread_config: JsonObject | None = None,
        codex_home: Path | str | None = None,
    ) -> None:
        self.codex_bin = str(codex_bin)
        self.cwd = Path(cwd).resolve()
        self.tools = {tool.name: tool for tool in tools}
        self.developer_instructions = developer_instructions
        self.model = model
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler
        self.request_timeout_seconds = request_timeout_seconds
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.ephemeral = ephemeral
        # Codex app-server treats an explicitly empty environment list as a
        # sticky thread capability boundary. With no execution environment,
        # Codex does not register shell, apply_patch, or view_image for the
        # thread, while host-provided dynamicTools remain available. ``None``
        # preserves Codex's default environment for normal coding agents.
        self.environments = environments
        self.thread_config = dict(thread_config or {})
        self.codex_home = Path(codex_home).expanduser().resolve() if codex_home else None

        self.thread_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        # Server requests and notifications share one ordered stream. Keeping
        # separate queues and racing two ``get`` tasks can drop one message
        # when both become ready in the same loop iteration.
        self._inbound: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._stderr_lines: deque[str] = deque(maxlen=80)

    async def start(self) -> None:
        if self._process is not None:
            return
        process_env = None
        if self.codex_home is not None:
            self.codex_home.mkdir(parents=True, exist_ok=True)
            # Isolate root-thread sessions/configuration from the operator's
            # global MCP servers, plugins, skills and instructions. Reuse only
            # the Codex login file; never copy its secret contents.
            source_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            source_auth = source_home / "auth.json"
            target_auth = self.codex_home / "auth.json"
            if source_auth.exists() and not target_auth.exists():
                target_auth.symlink_to(source_auth)
            process_env = dict(os.environ)
            process_env["CODEX_HOME"] = str(self.codex_home)
        self._process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=process_env,
            # JSON-RPC is newline-delimited, but a restored Codex thread can
            # legitimately return one response larger than asyncio's 64 KiB
            # default StreamReader limit. Keep a bounded ceiling so long-lived
            # projects resume without LimitOverrunError.
            limit=_APP_SERVER_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(
            self._read_messages(), name="mac-codex-root-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="mac-codex-root-stderr"
        )
        await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "mac-system",
                    "title": "MAC",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._notify("initialized")

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        error = AppServerProtocolError("Codex app-server closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def create_thread(self) -> str:
        await self.start()
        params: JsonObject = {
            "cwd": str(self.cwd),
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
            "dynamicTools": [tool.codex_spec() for tool in self.tools.values()],
            "ephemeral": self.ephemeral,
        }
        if self.developer_instructions is not None:
            params["developerInstructions"] = self.developer_instructions
        if self.environments is not None:
            params["environments"] = self.environments
        if self.thread_config:
            params["config"] = self.thread_config
        if self.model:
            params["model"] = self.model
        response = await self._request("thread/start", params)
        self.thread_id = self._extract_thread_id(response)
        return self.thread_id

    async def resume_thread(self, thread_id: str) -> str:
        await self.start()
        params: JsonObject = {
            "threadId": thread_id,
            "cwd": str(self.cwd),
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
        }
        if self.developer_instructions is not None:
            params["developerInstructions"] = self.developer_instructions
        if self.thread_config:
            params["config"] = self.thread_config
        if self.model:
            params["model"] = self.model
        response = await self._request("thread/resume", params)
        self.thread_id = self._extract_thread_id(response)
        return self.thread_id

    async def run_turn(self, text: str) -> TurnResult:
        async with self._turn_lock:
            return await self._run_turn_locked(text)

    async def _run_turn_locked(self, text: str) -> TurnResult:
        if self.thread_id is None:
            await self.create_thread()
        assert self.thread_id is not None
        response = await self._request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": text, "textElements": []}],
            },
        )
        turn = response.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerProtocolError("turn/start response has no turn id")
        turn_id = turn["id"]
        events: list[AgentEvent] = []
        final_message: str | None = None

        while True:
            message = await self._inbound.get()
            method = str(message.get("method") or "")
            if method == _PROCESS_CLOSED_METHOD:
                raise AppServerProtocolError(
                    str((message.get("params") or {}).get("error") or "Codex app-server closed")
                )
            params = message.get("params")
            params = params if isinstance(params, dict) else {}

            if "id" in message:
                request_thread_id = str(params.get("threadId") or self.thread_id or "")
                request_turn_id = str(params.get("turnId") or turn_id or "") or None
                event = AgentEvent(
                    "tool_call" if method == "item/tool/call" else "server_request",
                    request_thread_id,
                    request_turn_id,
                    {"method": method, **params},
                )
                events.append(event)
                await self._emit(event)
                if method == "item/tool/call":
                    await self._execute_tool_request(message["id"], params)
                elif self.server_request_handler is not None:
                    try:
                        response = self.server_request_handler(method, params)
                        if inspect.isawaitable(response):
                            response = await response
                        await self._respond_result(message["id"], response)
                    except Exception as exc:
                        await self._respond_error(message["id"], str(exc))
                else:
                    await self._respond_error(
                        message["id"], f"Unsupported request: {method}"
                    )
                continue

            event = AgentEvent(
                method,
                str(params.get("threadId") or self.thread_id or ""),
                str(params.get("turnId") or turn_id or "") or None,
                params,
            )
            events.append(event)
            await self._emit(event)
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    value = item.get("text")
                    if isinstance(value, str) and value.strip():
                        final_message = value
            if method == "turn/completed" and params.get("threadId") == self.thread_id:
                completed = params.get("turn")
                completed = completed if isinstance(completed, dict) else {}
                if completed.get("id") != turn_id:
                    continue
                return TurnResult(
                    thread_id=self.thread_id,
                    turn_id=turn_id,
                    status=str(completed.get("status") or "unknown"),
                    final_message=final_message,
                    events=tuple(events),
                )

    async def steer(self, turn_id: str, text: str) -> None:
        if self.thread_id is None:
            raise AppServerProtocolError("Cannot steer before creating a thread")
        await self._request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text, "textElements": []}],
            },
        )

    async def interrupt(self, turn_id: str) -> None:
        """Interrupt the active turn using Codex's native v2 protocol."""

        if self.thread_id is None:
            raise AppServerProtocolError("Cannot interrupt before creating a thread")
        await self._request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": turn_id},
        )

    async def _execute_tool_request(self, request_id: Any, params: JsonObject) -> None:
        name = params.get("tool")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            await self._respond_tool(request_id, f"Unknown MAC tool: {name}", success=False)
            return
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            context = ToolContext(
                thread_id=str(params.get("threadId") or ""),
                turn_id=str(params.get("turnId") or ""),
                call_id=str(params.get("callId") or ""),
                tool_name=tool.name,
            )
            handler_parameters = inspect.signature(tool.handler).parameters
            result = (
                tool.handler(arguments, context)
                if len(handler_parameters) >= 2
                else tool.handler(arguments)
            )
            if inspect.isawaitable(result):
                result = await result
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            await self._respond_tool(request_id, text, success=True)
        except Exception as exc:
            await self._respond_tool(request_id, str(exc), success=False)

    async def _respond_tool(self, request_id: Any, text: str, *, success: bool) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "contentItems": [{"type": "inputText", "text": text}],
                    "success": success,
                },
            }
        )

    async def _respond_error(self, request_id: Any, message: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": message},
            }
        )

    async def _respond_result(self, request_id: Any, result: JsonObject) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _request(self, method: str, params: JsonObject) -> JsonObject:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            message = await asyncio.wait_for(future, self.request_timeout_seconds)
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if isinstance(error, dict):
            raise AppServerProtocolError(
                f"{method} failed: {error.get('message') or error}"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerProtocolError(f"{method} returned a non-object result")
        return result

    async def _notify(self, method: str, params: JsonObject | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerProtocolError("Codex app-server is not running")
        data = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            process.stdin.write((data + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AppServerProtocolError("Invalid JSON-RPC from app-server") from exc
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    await self._inbound.put(message)
                    continue
                request_id = message.get("id")
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
            await self._process.wait()
            await asyncio.sleep(0)
            stderr = "\n".join(self._stderr_lines)
            error = AppServerProtocolError(
                "Codex app-server stdout closed"
                + (f"; stderr: {stderr[-4000:]}" if stderr else "")
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            await self._inbound.put(
                {"method": _PROCESS_CLOSED_METHOD, "params": {"error": str(error)}}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            await self._inbound.put(
                {"method": _PROCESS_CLOSED_METHOD, "params": {"error": str(exc)}}
            )

    async def _drain_stderr(self) -> None:
        """Continuously drain stderr so a verbose app-server cannot deadlock."""
        assert self._process is not None and self._process.stderr is not None
        while line := await self._process.stderr.readline():
            message = line.decode("utf-8", errors="replace").rstrip()
            if message:
                self._stderr_lines.append(message)
                logger.debug("Codex app-server stderr: %s", message)

    async def _emit(self, event: AgentEvent) -> None:
        if self.event_handler is None:
            return
        result = self.event_handler(event)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _extract_thread_id(response: JsonObject) -> str:
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerProtocolError("thread response has no thread id")
        return thread["id"]
