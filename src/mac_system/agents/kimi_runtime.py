"""Persistent Kimi Project Lead using Kimi CLI's native Wire 1.3 protocol.

Kimi owns tools, context and its agent loop. Only public events are projected;
ThinkPart and subagent conversations never enter MAC's handoff/history.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from uuid import uuid4

from mac_system.agents.process_lifecycle import stop_process_tree
from mac_system.agents.root_runtime import (
    AgentEvent, ProjectLeadRuntime, ProjectLeadRuntimeError, ToolContext,
    TurnResult, _isolated_project_process_env,
)


class KimiWireError(ProjectLeadRuntimeError):
    pass


class KimiWireThread(ProjectLeadRuntime):
    provider_id = "kimi_code"
    display_name = "Kimi"

    def __init__(self, *, kimi_bin, cwd, tools, event_handler=None,
                 server_request_handler=None, model=None, request_timeout_seconds=60,
                 silence_timeout_seconds=300, agent_file=None, mcp_config_file=None):
        self.kimi_bin = str(kimi_bin)
        self.cwd = Path(cwd)
        self.active_model = model
        self.thread_id = None
        self.active_turn_id = None
        self.request_timeout_seconds = request_timeout_seconds
        self.silence_timeout_seconds = silence_timeout_seconds
        self.agent_file = agent_file
        self.mcp_config_file = mcp_config_file
        self.update_bindings(tools=tools, event_handler=event_handler,
                             server_request_handler=server_request_handler)
        self._process = None
        self._reader = None
        self._stderr = None
        self._pending = {}
        self._sequence = 0
        self._prompt_id = None
        self._inbound = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._terminal = asyncio.Event()
        self._terminal.set()

    def update_bindings(self, *, tools, event_handler, server_request_handler):
        self.tools = {t.name: t for t in tools}
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler

    async def create_thread(self, *, session_start_source=None):
        return await self.resume_thread(str(uuid4()))

    async def resume_thread(self, thread_id):
        if self._process is not None:
            await self.close()
        self.thread_id = thread_id
        self._inbound = asyncio.Queue()
        command = [self.kimi_bin, "--work-dir", str(self.cwd), "--session", thread_id, "--wire"]
        if self.agent_file is not None:
            command += ["--agent-file", str(self.agent_file)]
        if self.mcp_config_file is not None:
            command += ["--mcp-config-file", str(self.mcp_config_file)]
        if self.active_model:
            command += ["--model", self.active_model]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command, cwd=str(self.cwd), env=_isolated_project_process_env(os.environ),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
                limit=16 * 1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())
            result = await self._request("initialize", {
                "protocol_version": "1.3", "client": {"name": "mac-system", "version": "1.0"},
                "capabilities": {"supports_question": True},
                "external_tools": [{"name": t.name, "description": t.description,
                                    "parameters": t.input_schema} for t in self.tools.values()],
            })
            if (result.get("external_tools") or {}).get("rejected"):
                raise KimiWireError("Kimi rejected MAC extension tools")
            return thread_id
        except (Exception, asyncio.CancelledError):
            await self.close()
            raise

    def _next_id(self):
        self._sequence += 1
        return str(self._sequence)

    async def _write(self, message):
        async with self._write_lock:
            if self._process is None or self._process.returncode is not None:
                raise KimiWireError("Kimi process is not running")
            self._process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
            await self._process.stdin.drain()

    async def _request(self, method, params=None):
        request_id = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, self.request_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise KimiWireError(f"Kimi {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _read(self):
        error = "Kimi wire closed"
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                request_id = message.get("id")
                if not message.get("method") and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        if message.get("error"):
                            future.set_exception(KimiWireError(str(message["error"])))
                        else:
                            future.set_result(message.get("result") or {})
                else:
                    await self._inbound.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"Kimi wire error: {exc}"
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(KimiWireError(error))
            await self._inbound.put({"closed": error})

    async def _drain_stderr(self):
        # Native structured errors are surfaced through prompt responses.
        while await self._process.stderr.readline():
            pass

    async def _emit(self, kind, payload, events):
        event = AgentEvent(kind, self.thread_id, self.active_turn_id, payload)
        events.append(event)
        if self.event_handler:
            value = self.event_handler(event)
            if inspect.isawaitable(value):
                await value

    async def run_turn(self, text, *, output_schema=None):
        if output_schema is not None:
            raise KimiWireError("Kimi Wire does not support native output schemas")
        async with self._turn_lock:
            if self._process is None:
                await self.resume_thread(self.thread_id or str(uuid4()))
            self.active_turn_id = str(uuid4())
            turn_id = self.active_turn_id
            self._terminal.clear()
            events, parts, requests, tool_ids = [], [], set(), {}
            self._prompt_id = self._next_id()
            try:
                await self._emit("turn/started", {
                    "threadId": self.thread_id, "turn": {"id": turn_id, "status": "inProgress"},
                }, events)
                await self._write({"id": self._prompt_id, "method": "prompt",
                                   "params": {"user_input": text}})
                while True:
                    try:
                        message = await asyncio.wait_for(
                            self._inbound.get(), self.silence_timeout_seconds,
                        )
                    except asyncio.TimeoutError as exc:
                        for task in list(requests):
                            if task.done():
                                requests.remove(task)
                                task.result()
                        if requests or tool_ids:
                            continue
                        raise KimiWireError("Kimi model stream stopped responding") from exc
                    if "closed" in message:
                        raise KimiWireError(message["closed"])
                    if message.get("id") == self._prompt_id and not message.get("method"):
                        error = message.get("error")
                        native_status = (message.get("result") or {}).get("status")
                        final = "".join(parts).strip()
                        status = ("interrupted" if native_status == "cancelled" else
                                  "completed" if native_status == "finished" and final and not error
                                  else "failed")
                        payload = {"id": turn_id, "status": status}
                        if status == "failed":
                            payload["error"] = {"message": str(error or (
                                f"Kimi returned {native_status or 'no status'} without a final answer"))}
                        if status == "completed":
                            await self._emit("item/completed", {
                                "threadId": self.thread_id, "turnId": turn_id,
                                "item": {"id": f"{turn_id}:final", "type": "agentMessage",
                                         "text": final, "phase": "final_answer"},
                            }, events)
                        await self._emit("turn/completed", {
                            "threadId": self.thread_id, "turn": payload,
                        }, events)
                        return TurnResult(self.thread_id, turn_id, status,
                                          final if status == "completed" else None, tuple(events))
                    envelope = message.get("params") or {}
                    kind, payload = envelope.get("type"), envelope.get("payload") or {}
                    if message.get("method") == "request":
                        task = asyncio.create_task(self._answer(message))
                        requests.add(task)
                        # Keep tasks until their result is checked (no lost exceptions).
                    elif kind == "StepBegin":
                        parts.clear()
                    elif kind == "ContentPart" and payload.get("type") == "text":
                        delta = str(payload.get("text") or "")
                        parts.append(delta)
                        await self._emit("item/agentMessage/delta", {
                            "threadId": self.thread_id, "turnId": turn_id, "delta": delta,
                        }, events)
                    elif kind == "ToolCall":
                        parts.clear()
                        name = (payload.get("function") or {}).get("name")
                        item_type = ("dynamicToolCall" if name in {"run_external_agent", "run_external_agents"}
                                     else "nativeToolCall")
                        tool_ids[payload["id"]] = (item_type, name)
                        await self._emit("item/started", {"item": {
                            "id": payload["id"], "type": item_type, "tool": name,
                        }}, events)
                    elif kind == "ToolResult":
                        item_type, name = tool_ids.pop(payload.get("tool_call_id"), ("nativeToolCall", None))
                        await self._emit("item/completed", {"item": {
                            "id": payload.get("tool_call_id"), "type": item_type, "tool": name,
                            "status": "failed" if (payload.get("return_value") or {}).get("is_error") else "completed",
                        }}, events)
                    elif kind in {"CompactionBegin", "CompactionEnd"}:
                        await self._emit("item/started" if kind == "CompactionBegin" else "item/completed",
                                         {"item": {"id": "compaction", "type": "contextCompaction"}}, events)
                    # Other ContentParts include private thinking. Never
                    # persist them, SubagentEvent, or raw tool outputs.
                    for task in list(requests):
                        if task.done():
                            requests.remove(task)
                            task.result()
            finally:
                for task in requests:
                    task.cancel()
                await asyncio.gather(*requests, return_exceptions=True)
                self._prompt_id = None
                self.active_turn_id = None
                self._terminal.set()

    async def _answer(self, message):
        request_id = message["id"]
        envelope = message["params"]
        kind, payload = envelope["type"], envelope["payload"]
        common = {"threadId": self.thread_id, "turnId": self.active_turn_id,
                  "itemId": f"kimi:{self.thread_id}:{request_id}", "provider": "Kimi"}
        try:
            if kind == "ToolCallRequest":
                tool = self.tools[payload["name"]]
                arguments = payload.get("arguments") or "{}"
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                context = ToolContext(self.thread_id, self.active_turn_id,
                                      payload["id"], payload["name"])
                value = (tool.handler(arguments, context) if len(inspect.signature(tool.handler).parameters) >= 2
                         else tool.handler(arguments))
                if inspect.isawaitable(value):
                    value = await value
                result = {"tool_call_id": payload["id"], "return_value": {
                    "is_error": False, "output": json.dumps(value, ensure_ascii=False, default=str),
                    "message": "", "display": [],
                }}
            elif kind == "ApprovalRequest":
                response = await self.server_request_handler("item/commandExecution/requestApproval", {
                    **common, "approvalId": common["itemId"], "cwd": str(self.cwd),
                    "command": str(payload.get("description") or payload.get("action") or ""),
                    "reason": "Kimi 请求授权：" + str(payload.get("sender") or ""),
                    "availableDecisions": ["accept", "acceptForSession", "decline"],
                })
                decision = {"accept": "approve", "acceptForSession": "approve_for_session"}.get(
                    response.get("decision"), "reject")
                result = {"request_id": payload["id"], "response": decision}
            elif kind == "QuestionRequest":
                questions = []
                for index, q in enumerate(payload["questions"]):
                    multi = q.get("multi_select", False)
                    text = q["question"]
                    if multi:
                        text += "\n可多选，请输入选项名称并以逗号分隔：\n" + "\n".join(
                            f"{o['label']}：{o.get('description', '')}" for o in q.get("options", []))
                    questions.append({"id": str(index), "question": text,
                                      "header": q.get("header", ""), "isOther": True,
                                      "options": [] if multi else q.get("options", [])})
                response = await self.server_request_handler("item/tool/requestUserInput", {
                    **common, "questions": questions,
                })
                result = {"request_id": payload["id"], "answers": {
                    q["question"]: ",".join((response.get("answers", {}).get(str(i)) or {}).get("answers", []))
                    for i, q in enumerate(payload["questions"])
                }}
            else:
                raise KimiWireError(f"Unsupported Kimi request: {kind}")
            await self._write({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._write({"id": request_id, "error": {"code": -32603, "message": str(exc)}})

    async def steer(self, turn_id, text):
        if not self.active_turn_id or turn_id != self.active_turn_id:
            raise KimiWireError("No active Kimi turn to steer")
        await self._request("steer", {"user_input": text})

    async def interrupt(self, turn_id=None):
        if not self.active_turn_id:
            return True
        if turn_id is not None and turn_id != self.active_turn_id:
            raise KimiWireError("Kimi turn changed before interrupt")
        await self._request("cancel")
        try:
            await asyncio.wait_for(self._terminal.wait(), self.request_timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self):
        async with self._close_lock:
            await self._close()

    async def _close(self):
        if self._process is not None:
            await stop_process_tree(self._process, isolated_group=True)
        for task in (self._reader, self._stderr):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._reader, self._stderr) if t), return_exceptions=True)
        self._process = self._reader = self._stderr = None
        self.active_turn_id = None
        self._terminal.set()
