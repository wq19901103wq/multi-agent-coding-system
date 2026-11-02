"""DeepSeek Harness Project Lead over its native ACP profile.

DSH owns execution, tools, context, persistence and subagents. ACP has no steer:
the existing host fallback queues running input. MAC extensions use native MCP.
Protocol reference: deepseek-harness 86a402200d95fecef4529d969836f1fe13c6cb0d.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import hmac
import inspect
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
from uuid import uuid4

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount
import uvicorn

from mac_system.agents.process_lifecycle import stop_process_tree
from mac_system.agents.root_runtime import (
    AgentEvent, ProjectLeadRuntime, ProjectLeadRuntimeError, ToolContext, TurnResult,
    _isolated_project_process_env,
)

_QUESTION_SCHEMA = {
    "type": "object", "required": ["questions"], "additionalProperties": False,
    "properties": {"questions": {
        "type": "array", "minItems": 1, "maxItems": 3,
        "items": {"type": "object", "required": ["id", "header", "question"],
                  "properties": {
                      "id": {"type": "string"}, "header": {"type": "string"},
                      "question": {"type": "string"},
                      "options": {"type": "array", "items": {
                          "type": "object", "required": ["label", "description"],
                          "properties": {"label": {"type": "string"},
                                         "description": {"type": "string"}}}},
                  }},
    }},
}


class _LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Only the MAC service owns process signal handlers.
        yield


class _MacMCP:
    """Private, per-runtime loopback endpoint using the official MCP server."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.task = None
        self.sock = None

    def schema(self, name):
        if name == "request_user_input":
            return _QUESTION_SCHEMA
        tool = self.runtime.tools.get(name)
        return tool.input_schema if tool else None

    async def list_tools(self, context, params):
        result = [types.Tool(name=t.name, description=t.description, inputSchema=t.input_schema)
                  for t in self.runtime.tools.values() if t.name != "request_user_input"]
        result.append(types.Tool(
            name="request_user_input", inputSchema=_QUESTION_SCHEMA,
            description="Ask the user up to three questions through MAC and wait for their answers."))
        return types.ListToolsResult(tools=result)

    async def call_tool(self, context, params):
        runtime = self.runtime
        if runtime.active_turn_id is None:
            raise ValueError("No active DSH turn")
        runtime._requests += 1
        try:
            arguments = params.arguments or {}
            schema = self.schema(params.name)
            if schema is None:
                raise ValueError(f"Unknown MAC tool: {params.name}")
            try:
                jsonschema.validate(arguments, schema)
            except jsonschema.ValidationError as exc:
                return types.CallToolResult(isError=True, content=[types.TextContent(
                    type="text", text=f"Invalid arguments for {params.name}: {exc.message}")])
            call_id = str(uuid4())
            if params.name == "request_user_input":
                result = await runtime._host_request("item/tool/requestUserInput", {
                    "itemId": call_id, "questions": arguments["questions"],
                })
            else:
                tool = runtime.tools.get(params.name)
                if tool is None:
                    raise ValueError(f"Unknown MAC tool: {params.name}")
                tool_context = ToolContext(runtime.thread_id, runtime.active_turn_id, call_id, params.name)
                signature = inspect.signature(tool.handler)
                result = (tool.handler(arguments, tool_context) if len(signature.parameters) >= 2
                          else tool.handler(arguments))
                if inspect.isawaitable(result):
                    result = await result
            return types.CallToolResult(content=[types.TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, default=str))])
        finally:
            runtime._requests -= 1

    async def start(self):
        app = Server("mac", on_list_tools=self.list_tools, on_call_tool=self.call_tool,
                     get_tool_input_schema=self.schema)
        manager = StreamableHTTPSessionManager(app, json_response=True, stateless=True)

        async def endpoint(scope, receive, send):
            headers = dict(scope.get("headers", []))
            expected = f"Bearer {self.token}".encode()
            if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                await Response(status_code=401)(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def lifespan(app):
            async with manager.run():
                yield

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        port = self.sock.getsockname()[1]
        application = Starlette(routes=[Mount("/mcp", app=endpoint)], lifespan=lifespan)
        self.server = _LocalServer(uvicorn.Config(application, log_level="error", access_log=False))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        async def ready():
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise ProjectLeadRuntimeError("MAC MCP endpoint failed to start")
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), 10)
        return {"type": "http", "name": "mac", "url": f"http://127.0.0.1:{port}/mcp/",
                "headers": [{"name": "Authorization", "value": f"Bearer {self.token}"}]}

    async def close(self):
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except Exception:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.sock:
            self.sock.close()


class DshACPThread(ProjectLeadRuntime):
    provider_id = "dsh"
    display_name = "DeepSeek Harness"

    def __init__(self, *, dsh_bin, cwd, tools, dsh_home=None, env=None,
                 event_handler=None, server_request_handler=None,
                 request_timeout_seconds=60, silence_timeout_seconds=300):
        self.dsh_bin = str(dsh_bin)
        self.cwd = Path(cwd).resolve()
        self.dsh_home = Path(dsh_home).expanduser().resolve() if dsh_home else None
        self.env = dict(env or {})
        self.thread_id = self.active_turn_id = self.active_model = None
        self.request_timeout_seconds = request_timeout_seconds
        self.silence_timeout_seconds = silence_timeout_seconds
        self.update_bindings(tools=tools, event_handler=event_handler,
                             server_request_handler=server_request_handler)
        self._process = self._reader = self._stderr = self._mcp = None
        self._launch_dir = None
        self._pending = {}
        self._sequence = 0
        self._inbound = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._terminal = asyncio.Event()
        self._terminal.set()
        self._requests = 0

    def update_bindings(self, *, tools, event_handler, server_request_handler):
        self.tools = {t.name: t for t in tools}
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler

    async def create_thread(self, *, session_start_source=None):
        return await self._connect(None)

    async def resume_thread(self, thread_id):
        return await self._connect(thread_id)

    async def _connect(self, thread_id):
        await self.close()
        self.thread_id = thread_id
        self._inbound = asyncio.Queue()
        self._mcp = _MacMCP(self)
        try:
            declaration = await self._mcp.start()
            env = {**_isolated_project_process_env(os.environ), **self.env}
            dsh_bin = self.dsh_bin
            if os.path.dirname(dsh_bin) and not Path(dsh_bin).is_absolute():
                dsh_bin = str(self.cwd / dsh_bin)
            binary_dir = Path(dsh_bin).parent
            if binary_dir.is_absolute():
                # Honor the Node installed beside a project-local DSH CLI.
                env["PATH"] = str(binary_dir) + os.pathsep + env.get("PATH", "")
            if self.dsh_home:
                env["DSH_HOME"] = str(self.dsh_home)
            # DSH discovers startup .env here; workspace .env may contain values
            # forbidden at bootstrap. ACP sessions still use the actual project cwd.
            self._launch_dir = tempfile.TemporaryDirectory(prefix="mac-dsh-")
            self._process = await asyncio.create_subprocess_exec(
                dsh_bin, "--profile", "acp", cwd=self._launch_dir.name, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())
            info = await self._request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "mac-system", "version": "1.0"},
            })
            capabilities = info.get("agentCapabilities", {})
            sessions = capabilities.get("sessionCapabilities", {})
            if (info.get("protocolVersion") != 1 or "resume" not in sessions
                    or "close" not in sessions or not capabilities.get("mcpCapabilities", {}).get("http")):
                raise ProjectLeadRuntimeError("DSH ACP requires persistent resume/close and HTTP MCP support")
            params = {"cwd": str(self.cwd), "mcpServers": [declaration]}
            if thread_id:
                params["sessionId"] = thread_id
            result = await self._request("session/resume" if thread_id else "session/new", params)
            if not thread_id:
                self.thread_id = result.get("sessionId")
                if not self.thread_id:
                    raise ProjectLeadRuntimeError("DSH returned no session ID")
            self._config_options(result.get("configOptions", []))
            return self.thread_id
        except BaseException:
            await self.close()
            raise

    def _config_options(self, options):
        for option in options:
            if option.get("id") == "model":
                self.active_model = option.get("currentValue")

    def _next_id(self):
        self._sequence += 1
        return str(self._sequence)

    async def _write(self, message):
        async with self._write_lock:
            if self._process is None or self._process.returncode is not None:
                raise ProjectLeadRuntimeError("DSH ACP process is not running")
            self._process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
            await self._process.stdin.drain()

    async def _request(self, method, params):
        request_id = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, self.request_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProjectLeadRuntimeError(f"DSH {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _read(self):
        error = "DSH ACP connection closed"
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                request_id = message.get("id")
                if not message.get("method") and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        if message.get("error"):
                            future.set_exception(ProjectLeadRuntimeError(str(message["error"])))
                        else:
                            future.set_result(message.get("result") or {})
                else:
                    # Prompt response stays ordered after all preceding updates.
                    await self._inbound.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"DSH ACP transport error: {exc}"
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ProjectLeadRuntimeError(error))
            await self._inbound.put(ProjectLeadRuntimeError(error))

    async def _drain_stderr(self):
        # Never publish provider diagnostics, tool results or hidden reasoning.
        while await self._process.stderr.readline():
            pass

    async def _emit(self, kind, payload, events):
        event = AgentEvent(kind, self.thread_id, self.active_turn_id, payload)
        events.append(event)
        if self.event_handler:
            value = self.event_handler(event)
            if inspect.isawaitable(value):
                await value

    async def _host_request(self, method, params):
        if self.server_request_handler is None or self.active_turn_id is None:
            raise ProjectLeadRuntimeError("No active MAC interaction handler")
        result = self.server_request_handler(method, {
            **params, "threadId": self.thread_id, "turnId": self.active_turn_id,
            "provider": self.display_name,
        })
        return await result if inspect.isawaitable(result) else result

    async def _permission(self, message, tool_calls):
        params = message.get("params", {})
        outcome = {"outcome": "cancelled"}
        self._requests += 1
        try:
            call_id = params.get("toolCall", {}).get("toolCallId")
            call = tool_calls.get(call_id)
            choices = {o.get("kind"): o.get("optionId") for o in params.get("options", [])}
            if (params.get("sessionId") == self.thread_id and call
                    and self.server_request_handler and self.active_turn_id):
                decisions = ["decline", "cancel"]
                if choices.get("allow_once"):
                    decisions.insert(0, "accept")
                response = await self._host_request("item/commandExecution/requestApproval", {
                    "itemId": call_id, "cwd": str(self.cwd),
                    "reason": f"DeepSeek Harness 请求使用 {call.get('title', 'tool')}",
                    "command": f"{call.get('title', 'tool')} " + json.dumps(call.get("rawInput", {}), ensure_ascii=False),
                    "availableDecisions": decisions,
                })
                choice = choices.get({"accept": "allow_once", "decline": "reject_once"}.get(response.get("decision")))
                if choice:
                    outcome = {"outcome": "selected", "optionId": choice}
            await self._write({"id": message["id"], "result": {"outcome": outcome}})
        except Exception as exc:
            await self._inbound.put(ProjectLeadRuntimeError(f"DSH permission request failed: {exc}"))
        finally:
            self._requests -= 1

    async def run_turn(self, text, *, output_schema=None):
        if output_schema is not None:
            raise ProjectLeadRuntimeError("DSH ACP has no native output schema parameter")
        async with self._turn_lock:
            if self._process is None or not self.thread_id:
                raise ProjectLeadRuntimeError("DSH session is not connected")
            self.active_turn_id = turn_id = str(uuid4())
            self._terminal.clear()
            events, tool_calls, busy_tools, requests = [], {}, set(), set()
            pending_text = None
            prompt_id = self._next_id()

            async def complete_text(phase):
                nonlocal pending_text
                if pending_text:
                    await self._emit("item/completed", {"item": {
                        "id": pending_text[0], "type": "agentMessage",
                        "text": pending_text[1], "phase": phase,
                    }}, events)
                    pending_text = None

            try:
                await self._emit("turn/started", {"turn": {"id": turn_id}}, events)
                await self._write({"id": prompt_id, "method": "session/prompt", "params": {
                    "sessionId": self.thread_id, "prompt": [{"type": "text", "text": text}],
                }})
                while True:
                    try:
                        message = await asyncio.wait_for(self._inbound.get(), self.silence_timeout_seconds)
                    except asyncio.TimeoutError as exc:
                        if self._requests or busy_tools:
                            continue
                        raise ProjectLeadRuntimeError("DSH ACP prompt stalled without public activity") from exc
                    if isinstance(message, Exception):
                        raise message
                    method, params = message.get("method"), message.get("params", {})
                    if not method and message.get("id") == prompt_id:
                        if message.get("error"):
                            raise ProjectLeadRuntimeError(str(message["error"]))
                        reason = (message.get("result") or {}).get("stopReason")
                        final = pending_text[1].strip() if pending_text else ""
                        status = ("interrupted" if reason == "cancelled" else "completed"
                                  if reason == "end_turn" and final else "failed")
                        await complete_text("final_answer" if status == "completed" else "commentary")
                        terminal = {"id": turn_id, "status": status}
                        if status == "failed":
                            terminal["error"] = {"message": f"DSH stopped: {reason}; no complete final answer"}
                        await self._emit("turn/completed", {"turn": terminal}, events)
                        return TurnResult(self.thread_id, turn_id, status,
                                          final if status == "completed" else None, tuple(events))
                    if method == "session/request_permission" and "id" in message:
                        task = asyncio.create_task(self._permission(message, tool_calls))
                        requests.add(task)
                        task.add_done_callback(requests.discard)
                    elif method == "session/update" and params.get("sessionId") == self.thread_id:
                        update = params.get("update", {})
                        kind = update.get("sessionUpdate")
                        if kind == "agent_message_chunk" and update.get("content", {}).get("type") == "text":
                            chunk = update["content"]["text"]
                            message_id = update.get("messageId") or (pending_text[0] if pending_text else str(uuid4()))
                            if pending_text and pending_text[0] != message_id:
                                await complete_text("commentary")
                            if not pending_text:
                                pending_text = [message_id, ""]
                                await self._emit("item/started", {"item": {
                                    "id": message_id, "type": "agentMessage"}}, events)
                            pending_text[1] += chunk
                            await self._emit("item/agentMessage/delta", {"itemId": message_id, "delta": chunk}, events)
                        elif kind == "tool_call":
                            await complete_text("commentary")
                            call_id = update["toolCallId"]
                            tool_calls[call_id] = update
                            busy_tools.add(call_id)
                            await self._emit("item/started", {"item": {
                                "id": call_id, "type": "dynamicToolCall", "tool": update.get("title", "tool")}}, events)
                        elif kind == "tool_call_update" and update.get("toolCallId") in tool_calls:
                            call_id = update["toolCallId"]
                            if update.get("status") in {"completed", "failed"}:
                                busy_tools.discard(call_id)
                                await self._emit("item/completed", {"item": {
                                    "id": call_id, "type": "dynamicToolCall",
                                    "tool": tool_calls[call_id].get("title", "tool")}}, events)
                        elif kind == "config_option_update":
                            self._config_options(update.get("configOptions", []))
                        # Thinking, raw tool output and foreign sessions stay private.
                    elif method and "id" in message:
                        await self._write({"id": message["id"], "error": {
                            "code": -32601, "message": f"Unsupported ACP client method: {method}"}})
            except BaseException:
                await self.close()
                raise
            finally:
                for task in requests:
                    task.cancel()
                await asyncio.gather(*requests, return_exceptions=True)
                self.active_turn_id = None
                self._terminal.set()

    async def steer(self, turn_id, text):
        raise ProjectLeadRuntimeError("DSH ACP does not support steer; input was not submitted")

    async def interrupt(self, turn_id=None):
        if not self.active_turn_id:
            return False
        if turn_id is not None and turn_id != self.active_turn_id:
            raise ProjectLeadRuntimeError("DSH active turn changed")
        await self._write({"method": "session/cancel", "params": {"sessionId": self.thread_id}})
        try:
            await asyncio.wait_for(self._terminal.wait(), self.request_timeout_seconds)
        except asyncio.TimeoutError:
            await self.close()
        return True

    async def close(self):
        async with self._close_lock:
            process = self._process
            try:
                if process is not None:
                    if process.returncode is None and self.thread_id and self._reader and not self._reader.done():
                        try:
                            await self._request("session/close", {"sessionId": self.thread_id})
                        except Exception:
                            pass
                    await stop_process_tree(process, isolated_group=True)
                    self._process = None
                if self._launch_dir:
                    self._launch_dir.cleanup()
                    self._launch_dir = None
            finally:
                for task in (self._reader, self._stderr):
                    if task:
                        task.cancel()
                await asyncio.gather(*(t for t in (self._reader, self._stderr) if t), return_exceptions=True)
                self._reader = self._stderr = None
                if self._mcp:
                    await self._mcp.close()
                    self._mcp = None
