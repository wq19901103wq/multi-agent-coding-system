"""Persistent Claude Code sessions hosted through Anthropic's native SDK.

The SDK owns the tool loop, context, permissions and subprocess protocol.
MAC only translates public events and interaction requests. Protocol reference:
claude-agent-sdk-python 0.2.168 and Claude Code sdk-tools.d.ts (2.1.150).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from uuid import uuid4

import psutil
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
    PermissionResultAllow, PermissionResultDeny, ResultMessage, SystemMessage,
    TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
    create_sdk_mcp_server, tool as sdk_tool,
)

from mac_system.agents.root_runtime import (
    AgentEvent, ProjectLeadRuntime, ProjectLeadRuntimeError, ToolContext, TurnResult,
    _isolated_project_process_env,
)


def claude_runtime_env(settings) -> dict[str, str]:
    """Provider credentials/models the Claude CLI needs, from MAC settings."""

    env = {}
    for field, key in (
        ("anthropic_api_key", "ANTHROPIC_API_KEY"),
        ("anthropic_auth_token", "ANTHROPIC_AUTH_TOKEN"),
        ("anthropic_base_url", "ANTHROPIC_BASE_URL"),
        ("anthropic_default_opus_model", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
        ("anthropic_default_sonnet_model", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
        ("anthropic_default_haiku_model", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
        ("claude_code_effort_level", "CLAUDE_CODE_EFFORT_LEVEL"),
    ):
        value = getattr(settings, field, None)
        if value:
            env[key] = value
    return env


class ClaudeCodeThread(ProjectLeadRuntime):
    provider_id = "claude_code"
    display_name = "Claude Code"

    def __init__(self, *, claude_bin, cwd: Path, tools, event_handler=None,
                 server_request_handler=None, model=None, env=None,
                 silence_timeout_seconds=300, append_system_prompt=None,
                 builtin_tools=None, strict_mcp_config=False,
                 setting_sources=None):
        self.claude_bin, self.cwd = str(claude_bin), Path(cwd)
        self.thread_id = self.active_turn_id = None
        self.active_model = model
        self.env = dict(env or {})
        self.silence_timeout_seconds = silence_timeout_seconds
        self.append_system_prompt = append_system_prompt
        self.builtin_tools = builtin_tools
        self.strict_mcp_config = strict_mcp_config
        self.setting_sources = (
            ["user", "project", "local"]
            if setting_sources is None
            else list(setting_sources)
        )
        self.update_bindings(tools=tools, event_handler=event_handler,
                             server_request_handler=server_request_handler)
        self._client = self._reader = None
        self._inbound = asyncio.Queue()
        self._turn_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._terminal = asyncio.Event()
        self._terminal.set()
        self._pending_inputs = set()
        self._requests = 0
        self._interrupted = False
        self._closed = False

    def update_bindings(self, *, tools, event_handler, server_request_handler):
        self.tools = {item.name: item for item in tools}
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler

    def _mcp_tool(self, definition):
        async def invoke(arguments):
            # Resolve the current binding; engines change between host turns.
            current = self.tools[definition.name]
            context = ToolContext(self.thread_id, self.active_turn_id,
                                  str(uuid4()), current.name)
            self._requests += 1
            try:
                value = (current.handler(arguments, context)
                         if len(inspect.signature(current.handler).parameters) >= 2
                         else current.handler(arguments))
                if inspect.isawaitable(value):
                    value = await value
                return {"content": [{"type": "text", "text": (
                    value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                )}]}
            finally:
                self._requests -= 1
        return sdk_tool(definition.name, definition.description, definition.input_schema)(invoke)

    async def _connect(self, *, resume):
        if self._client is not None:
            raise ProjectLeadRuntimeError("Claude session is already attached")
        # SDK merges overrides into os.environ. Empty removed keys as well as
        # cleaning PATH so child tools cannot inherit MAC's virtualenv.
        isolated = _isolated_project_process_env(os.environ)
        env = {key: "" for key in os.environ if key not in isolated}
        env.update(isolated)
        env.update(self.env)
        env["CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"] = "1"
        system_prompt = {"type": "preset", "preset": "claude_code"}
        if self.append_system_prompt:
            system_prompt["append"] = self.append_system_prompt
        options = ClaudeAgentOptions(
            cli_path=self.claude_bin, cwd=self.cwd, model=self.active_model,
            resume=self.thread_id if resume else None,
            session_id=None if resume else self.thread_id,
            system_prompt=system_prompt,
            setting_sources=self.setting_sources,
            permission_mode="default", can_use_tool=self._permission,
            mcp_servers={"mac": create_sdk_mcp_server(
                "mac", tools=[self._mcp_tool(item) for item in self.tools.values()])},
            tools=self.builtin_tools,
            strict_mcp_config=self.strict_mcp_config,
            extra_args={"replay-user-messages": None}, env=env,
            # Provider stderr may contain tool output; it is not a public event.
            stderr=lambda _line: None,
        )
        self._client = ClaudeSDKClient(options)
        self._inbound = asyncio.Queue()
        self._closed = False
        try:
            await self._client.connect()
        except BaseException:
            await self.close()
            raise
        self._reader = asyncio.create_task(self._read_messages())

    async def create_thread(self, *, session_start_source=None):
        self.thread_id = str(uuid4())
        await self._connect(resume=False)
        return self.thread_id

    async def resume_thread(self, thread_id):
        self.thread_id = thread_id
        await self._connect(resume=True)
        return self.thread_id

    async def _read_messages(self):
        try:
            async for message in self._client.receive_messages():
                await self._inbound.put(message)
        except Exception as exc:
            await self._inbound.put(ProjectLeadRuntimeError(str(exc)))
        finally:
            await self._inbound.put(ProjectLeadRuntimeError("Claude Code connection closed"))

    async def _send_input(self, text):
        message_id = str(uuid4())
        self._pending_inputs.add(message_id)
        async def message():
            yield {"type": "user", "uuid": message_id,
                   "message": {"role": "user", "content": text},
                   "origin": {"kind": "human"}, "parent_tool_use_id": None}
        await self._client.query(message(), session_id=self.thread_id)

    async def _emit(self, kind, payload, events):
        event = AgentEvent(kind, self.thread_id, self.active_turn_id, payload)
        events.append(event)
        if self.event_handler:
            value = self.event_handler(event)
            if inspect.isawaitable(value):
                await value

    async def run_turn(self, text, *, output_schema=None):
        if output_schema is not None:
            raise ProjectLeadRuntimeError("Claude session output schema cannot change per turn")
        async with self._turn_lock:
            if self._client is None or self._closed:
                raise ProjectLeadRuntimeError("Claude session is not connected")
            self.active_turn_id = turn_id = str(uuid4())
            self._terminal.clear()
            self._pending_inputs.clear()
            self._interrupted = False
            events, tool_ids = [], {}
            pending_text = None
            result = None
            state = None

            async def complete_text(phase):
                nonlocal pending_text
                if pending_text:
                    await self._emit("item/completed", {"item": {
                        "id": pending_text[0], "type": "agentMessage",
                        "text": pending_text[1], "phase": phase}}, events)
                    pending_text = None

            async def begin_text(value):
                nonlocal pending_text
                await complete_text("commentary")
                pending_text = (str(uuid4()), value)
                await self._emit("item/started", {"item": {
                    "id": pending_text[0], "type": "agentMessage", "text": ""}}, events)
                await self._emit("item/agentMessage/delta", {
                    "itemId": pending_text[0], "delta": value}, events)

            try:
                await self._emit("turn/started", {
                    "turn": {"id": turn_id, "status": "inProgress"}}, events)
                async with self._input_lock:
                    await self._send_input(text)
                while True:
                    try:
                        message = await asyncio.wait_for(
                            self._inbound.get(), self.silence_timeout_seconds)
                    except asyncio.TimeoutError as exc:
                        if self._requests or tool_ids:
                            continue
                        raise ProjectLeadRuntimeError("Claude Code stream stopped responding") from exc
                    if isinstance(message, Exception):
                        raise message
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            native_id = message.data.get("session_id")
                            if native_id and native_id != self.thread_id:
                                raise ProjectLeadRuntimeError("Claude resumed a different session")
                            self.active_model = message.data.get("model") or self.active_model
                        elif message.subtype == "session_state_changed":
                            state = message.data.get("state")
                    elif isinstance(message, UserMessage) and not message.parent_tool_use_id:
                        if message.uuid in self._pending_inputs:
                            self._pending_inputs.remove(message.uuid)
                            result = None
                            state = None
                        if isinstance(message.content, list):
                            for block in message.content:
                                if isinstance(block, ToolResultBlock) and block.tool_use_id in tool_ids:
                                    item = tool_ids.pop(block.tool_use_id)
                                    await self._emit("item/completed", {"item": item}, events)
                    elif isinstance(message, AssistantMessage) and not message.parent_tool_use_id:
                        result = None
                        state = None
                        self.active_model = message.model
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                await begin_text(block.text)
                            elif isinstance(block, ToolUseBlock):
                                await complete_text("commentary")
                                item = {"id": block.id, "type": "dynamicToolCall", "tool": block.name}
                                tool_ids[block.id] = item
                                await self._emit("item/started", {"item": item}, events)
                    elif isinstance(message, ResultMessage):
                        if message.session_id != self.thread_id:
                            raise ProjectLeadRuntimeError("Claude result belongs to another session")
                        # Background notifications have their own native results.
                        if message.origin is None or message.origin.get("kind") == "human":
                            result = message
                    # One result is not enough: queued input/steer and native
                    # background work must reach the CLI's idle boundary first.
                    async with self._input_lock:
                        if (result is None or state != "idle"
                                or (self._pending_inputs and not self._interrupted)):
                            continue
                        interrupted = self._interrupted or result.terminal_reason in {
                            "aborted_streaming", "aborted_tools"}
                        final = (result.result or (pending_text[1] if pending_text else "")).strip()
                        status = ("interrupted" if interrupted else "completed"
                                  if not result.is_error and result.subtype == "success" and final
                                  else "failed")
                        if status == "completed":
                            if not pending_text or pending_text[1] != final:
                                await begin_text(final)
                            await complete_text("final_answer")
                        else:
                            await complete_text("commentary")
                        terminal = {"id": turn_id, "status": status}
                        if status == "failed":
                            terminal["error"] = {"message": "; ".join(result.errors or [])
                                                 or result.result or result.subtype}
                        await self._emit("turn/completed", {"turn": terminal}, events)
                        self.active_turn_id = None
                        return TurnResult(self.thread_id, turn_id, status,
                                          final if status == "completed" else None, tuple(events))
            except BaseException:
                # A dropped transport must not leave a writer running behind a
                # failed host turn. Never silently retry a submitted prompt.
                await self.close()
                raise
            finally:
                self.active_turn_id = None
                self._terminal.set()

    async def _permission(self, name, arguments, context):
        if name in {f"mcp__mac__{key}" for key in self.tools}:
            return PermissionResultAllow()
        if self.server_request_handler is None or self.active_turn_id is None:
            return PermissionResultDeny(message="No active MAC interaction handler")
        common = {"threadId": self.thread_id, "turnId": self.active_turn_id,
                  "itemId": context.tool_use_id, "provider": self.display_name}
        self._requests += 1
        try:
            if name == "AskUserQuestion":
                native_questions = arguments.get("questions", [])
                questions = []
                for index, question in enumerate(native_questions):
                    title = question["question"]
                    options = question.get("options", [])
                    # Existing mobile cards are single-select. Preserve all
                    # native choices in text when Claude requests multiple.
                    if question.get("multiSelect"):
                        title += "\n可选多项，请填写选项名称：\n" + "\n".join(
                            f"{o['label']}：{o.get('description', '')}" for o in options)
                        options = None
                    questions.append({"id": str(index), "header": question.get("header", "选择"),
                                      "question": title, "options": options, "isOther": True})
                response = await self.server_request_handler(
                    "item/tool/requestUserInput", {**common, "questions": questions})
                answers = response.get("answers", {})
                if any(not answers.get(str(i), {}).get("answers") for i in range(len(questions))):
                    return PermissionResultDeny(message="Question was cancelled or unanswered")
                return PermissionResultAllow(updated_input={**arguments, "answers": {
                    q["question"]: ", ".join(answers[str(i)]["answers"])
                    for i, q in enumerate(native_questions)}})
            # Send the exact native operation; do not infer permission from its
            # name or replay another provider's approval policy.
            response = await self.server_request_handler("item/commandExecution/requestApproval", {
                **common, "cwd": str(self.cwd),
                "reason": context.description or f"Claude Code 请求使用 {name}",
                "command": arguments.get("command") if name == "Bash"
                           else f"{name} {json.dumps(arguments, ensure_ascii=False)}",
                "availableDecisions": ["accept", "decline", "cancel"],
            })
            decision = response.get("decision")
            return (PermissionResultAllow() if decision == "accept" else
                    PermissionResultDeny(message="MAC declined the operation", interrupt=decision == "cancel"))
        finally:
            self._requests -= 1

    async def steer(self, turn_id, text):
        async with self._input_lock:
            if not self.active_turn_id or turn_id != self.active_turn_id:
                raise ProjectLeadRuntimeError("Claude active turn changed; input was not submitted")
            await self._send_input(text)

    async def interrupt(self, turn_id=None):
        if not self.active_turn_id:
            return False
        if turn_id is not None and turn_id != self.active_turn_id:
            raise ProjectLeadRuntimeError("Claude active turn changed")
        self._interrupted = True
        await self._client.interrupt()
        await asyncio.wait_for(self._terminal.wait(), 30)
        return True

    async def close(self):
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            # SDK 0.2.168 reaps its CLI. Also retain identities of child tools
            # before disconnect, so a switch cannot leave old writers behind.
            process = getattr(getattr(client, "_transport", None), "_process", None)
            children = []
            if process is not None:
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    pass
            await client.disconnect()
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            if children:
                _, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=3)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
                if any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in alive):
                    raise ProjectLeadRuntimeError("Claude child tools have not stopped")
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        await self._inbound.put(ProjectLeadRuntimeError("Claude Code connection closed"))
        self._terminal.set()
