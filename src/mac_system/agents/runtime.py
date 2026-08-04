import asyncio
import json
import logging
import os
import re
import signal
import shutil
import threading
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil
from jsonschema import RefResolver
from jsonschema.validators import validator_for
from mac_system.utils.redaction import redact_secrets, redact_text

logger = logging.getLogger(__name__)


def normalize_optional_nulls(
    output: dict[str, Any], output_schema: Path | dict[str, Any]
) -> dict[str, Any]:
    """Omit nulls only where the schema declares an optional non-null field.

    Model providers commonly serialize an absent optional value as ``null``.
    JSON Schema distinguishes that from omission, but retrying a full Agent
    turn for this representation-only difference is wasteful. Required fields,
    unknown fields, and fields whose schema accepts null remain untouched and
    therefore still fail or pass through the normal validator.
    """
    try:
        root = (
            output_schema
            if isinstance(output_schema, dict)
            else json.loads(output_schema.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        return output

    def allows_null(schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        schema_type = schema.get("type")
        if schema_type == "null" or (
            isinstance(schema_type, list) and "null" in schema_type
        ):
            return True
        if schema.get("const", object()) is None:
            return True
        if None in (schema.get("enum") or []):
            return True
        return any(
            allows_null(candidate)
            for keyword in ("anyOf", "oneOf")
            for candidate in (schema.get(keyword) or [])
        )

    def visit(value: Any, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return value
        if isinstance(value, dict):
            properties = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                property_schema = properties.get(key)
                if (
                    item is None
                    and key not in required
                    and isinstance(property_schema, dict)
                    and not allows_null(property_schema)
                ):
                    continue
                normalized[key] = visit(item, property_schema)
            return normalized
        if isinstance(value, list):
            item_schema = schema.get("items")
            return [visit(item, item_schema) for item in value]
        return value

    normalized = visit(output, root)
    return normalized if isinstance(normalized, dict) else output


def validate_agent_output(
    output: dict[str, Any], output_schema: Path
) -> str | None:
    """Return a compact contract error, or ``None`` for a valid output."""
    try:
        schema = json.loads(output_schema.read_text(encoding="utf-8"))
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        schema_store: dict[str, Any] = {}
        for sibling in output_schema.resolve().parent.glob("*.json"):
            try:
                candidate = json.loads(sibling.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            schema_store[sibling.resolve().as_uri()] = candidate
            schema_store[sibling.name] = candidate
            if candidate.get("$id"):
                schema_store[str(candidate["$id"])] = candidate
        resolver = RefResolver(
            base_uri=output_schema.resolve().parent.as_uri() + "/",
            referrer=schema,
            store=schema_store,
        )
        errors = sorted(
            validator_cls(schema, resolver=resolver).iter_errors(output),
            key=lambda item: list(item.absolute_path),
        )
    except Exception as exc:
        return f"unable to validate output schema {output_schema}: {exc}"
    if not errors:
        return None
    # Return a bounded set instead of only the first error.  A model otherwise
    # sees that an alias is forbidden but not which canonical required fields
    # are missing, and has to guess a second invalid shape on retry.
    messages: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        message = f"schema violation at {location}: {error.message}"
        if message not in messages:
            messages.append(message)
        if len(messages) >= 8:
            break
    return "; ".join(messages)


# Runtime instances are intentionally short-lived: API requests and Feishu
# events each construct their own orchestrator/runtime.  Cancellation must
# therefore use a process registry shared by all instances in this process.
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_CANCELLED_TASK_IDS: set[str] = set()
_STREAM_READER_LIMIT = 16 * 1024 * 1024


class _DetachedToolProcessGuard:
    """Track and reap tool process groups spawned outside the CLI group.

    Claude Code starts Bash tools in a new session. Consequently killing the
    Claude process group does not kill a timed-out command: it becomes an
    orphan and a later model turn can launch a duplicate. Track every detached
    descendant while ancestry is still observable, enforce a wall-clock limit,
    and reap all recorded groups when the Agent turn ends.
    """

    def __init__(
        self,
        owner_pid: int,
        max_age_seconds: float,
        max_total_active_seconds: float | None = None,
    ):
        self.owner_pid = owner_pid
        self.max_age_seconds = max_age_seconds
        self.max_total_active_seconds = max_total_active_seconds
        self._first_seen: dict[int, float] = {}
        self._charged_until: dict[int, float] = {}
        self.total_active_seconds = 0.0
        self.total_budget_exceeded = False

    def poll(self) -> list[int]:
        now = time.monotonic()
        wall_now = time.time()
        try:
            owner_pgid = os.getpgid(self.owner_pid) if os.name != "nt" else None
            descendants = psutil.Process(self.owner_pid).children(recursive=True)
        except (ProcessLookupError, psutil.Error):
            descendants = []
            owner_pgid = None

        observed_groups: set[int] = set()
        for child in descendants:
            try:
                group_id = os.getpgid(child.pid) if os.name != "nt" else child.pid
            except (ProcessLookupError, psutil.Error):
                continue
            if group_id == owner_pgid:
                continue
            observed_groups.add(group_id)
            try:
                observed_age = max(0.0, wall_now - child.create_time())
            except (ProcessLookupError, psutil.Error):
                observed_age = 0.0
            self._first_seen.setdefault(group_id, now - observed_age)
            self._charged_until.setdefault(group_id, now - observed_age)

        # Finished short-lived commands disappear from the owner's descendant
        # tree.  Keeping their process groups in the charging map billed them
        # forever, so several completed one-second probes could exhaust a
        # five-minute budget in under a minute.  Retain a detached group only
        # while the OS still reports that group as alive.
        for group_id in list(self._charged_until):
            if group_id in observed_groups:
                continue
            try:
                if os.name != "nt":
                    os.killpg(group_id, 0)
                else:
                    psutil.Process(group_id)
            except (ProcessLookupError, PermissionError, psutil.Error):
                self._first_seen.pop(group_id, None)
                self._charged_until.pop(group_id, None)

        # Charge subprocess time independently of command spelling. This
        # bounds repeated, equivalent, or newly invented expensive commands
        # without encoding test-framework keywords in MAC.
        for group_id, charged_until in list(self._charged_until.items()):
            self.total_active_seconds += max(0.0, now - charged_until)
            self._charged_until[group_id] = now
        if (
            self.max_total_active_seconds is not None
            and self.total_active_seconds >= self.max_total_active_seconds
        ):
            self.total_budget_exceeded = True

        overdue: list[int] = []
        for group_id, first_seen in list(self._first_seen.items()):
            if (
                not self.total_budget_exceeded
                and now - first_seen < self.max_age_seconds
            ):
                continue
            if self._kill_group(group_id):
                overdue.append(group_id)
            self._first_seen.pop(group_id, None)
            self._charged_until.pop(group_id, None)
        return overdue

    def cleanup(self) -> None:
        for group_id in list(self._first_seen):
            self._kill_group(group_id)
            self._first_seen.pop(group_id, None)
            self._charged_until.pop(group_id, None)

    @staticmethod
    def _kill_group(group_id: int) -> bool:
        try:
            if os.name != "nt":
                os.killpg(group_id, signal.SIGKILL)
            else:
                psutil.Process(group_id).kill()
            return True
        except (ProcessLookupError, psutil.Error):
            return False
@dataclass
class AgentRuntimeResult:
    success: bool
    output_json: dict[str, Any] | None
    output_files: list[Path]
    logs: str
    cost_info: dict[str, Any] | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Machine-readable failure ownership. Callers use this to distinguish a
    # transient execution failure from an upstream condition that cannot be
    # repaired by immediately rerunning the same Agent.
    failure: dict[str, Any] | None = None
    # Provider prose is retained for explicitly read-only callers that can
    # safely degrade to an answer when the JSON envelope is malformed.
    raw_result: str | None = None


class AgentRuntime(ABC):
    # Runtime capability used by the Orchestrator's Coder artifact contract.
    # Production runtimes execute filesystem tools in the task worktree. Test
    # doubles may intentionally return only structured outputs.
    materializes_worktree_changes: bool = True

    @abstractmethod
    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path | None,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
        max_tool_calls: int | None = None,
        preferred_provider: str | None = None,
    ) -> AgentRuntimeResult:
        ...

    @abstractmethod
    def cancel(self, task_id: str) -> None:
        """Best-effort cancellation of a running task."""
        ...

    def provider_for(self, agent_type: str) -> str:
        """Return the provider that will execute an Agent."""
        return self.__class__.__name__

    def available_providers(self) -> set[str]:
        """Return provider identifiers the runtime can actually execute."""
        return {self.provider_for("")}

    def provider_profiles(self) -> dict[str, dict[str, Any]]:
        """Return model-visible, non-secret provider selection metadata."""
        return {provider: {} for provider in self.available_providers()}


class ClaudeCodeRuntime(AgentRuntime):
    """Drive Claude Code CLI via a stream-json subprocess session.

    Claude Code's stream-json mode (``--input-format stream-json
    --output-format stream-json``) emits JSON events on stdout and accepts
    JSON events on stdin.  Running with piped stdio makes Claude Code treat
    the session as non-interactive, which also skips the workspace trust
    dialog. Claude Code CLI is the sole executor of built-in tools. The
    runtime observes tool events for progress reporting but never executes a
    tool_use or writes a synthetic tool_result back to the CLI.
    """

    def __init__(
        self,
        claude_bin: Path,
        anthropic_api_key: str | None = None,
        claude_model: str | None = None,
        anthropic_base_url: str | None = None,
        anthropic_auth_token: str | None = None,
        anthropic_default_opus_model: str | None = None,
        anthropic_default_sonnet_model: str | None = None,
        anthropic_default_haiku_model: str | None = None,
        claude_code_effort_level: str | None = None,
        api_timeout_ms: int | None = None,
    ):
        self.claude_bin = Path(claude_bin)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self.anthropic_api_key = anthropic_api_key
        self.anthropic_auth_token = anthropic_auth_token
        self.claude_model = claude_model
        self.anthropic_base_url = anthropic_base_url
        self.anthropic_default_opus_model = anthropic_default_opus_model
        self.anthropic_default_sonnet_model = anthropic_default_sonnet_model
        self.anthropic_default_haiku_model = anthropic_default_haiku_model
        self.claude_code_effort_level = claude_code_effort_level
        self.api_timeout_ms = api_timeout_ms

    def provider_for(self, agent_type: str) -> str:
        return "claude_code"

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        if self.anthropic_auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.anthropic_auth_token
            if not self.anthropic_api_key:
                # Claude Code CLI checks ANTHROPIC_API_KEY for auth; when using
                # a third-party Anthropic-compatible provider (e.g. DeepSeek),
                # the auth token doubles as the API key.
                env["ANTHROPIC_API_KEY"] = self.anthropic_auth_token
        if self.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        if self.claude_model:
            env["ANTHROPIC_MODEL"] = self.claude_model
        if self.anthropic_default_opus_model:
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = self.anthropic_default_opus_model
        if self.anthropic_default_sonnet_model:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = self.anthropic_default_sonnet_model
        if self.anthropic_default_haiku_model:
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = self.anthropic_default_haiku_model
        if self.claude_code_effort_level:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = self.claude_code_effort_level
        if self.api_timeout_ms is not None:
            env["API_TIMEOUT_MS"] = str(self.api_timeout_ms)
        # Claude Code otherwise lets a model-started Bash command outlive the
        # useful turn for many minutes. Bound the default at the process layer;
        # the CLI may request a longer timeout only up to the hard ceiling.
        env["BASH_DEFAULT_TIMEOUT_MS"] = "180000"
        env["BASH_MAX_TIMEOUT_MS"] = "300000"
        # A sequence of different/equivalent commands can otherwise consume
        # the full per-command timeout repeatedly. Enforce one generic total
        # subprocess budget for the Agent turn as well.  The total must still
        # accommodate a bounded integration suite with several independent
        # real-API calls: a five-minute ceiling killed healthy Tester turns
        # after their third/fourth 45-60 second external check and forced the
        # entire evidence-gathering pass to restart.  Per-command limits and
        # the Agent deadline remain the primary runaway guards.
        env["MAC_TOOL_TOTAL_TIMEOUT_MS"] = "900000"
        # Disable non-essential traffic to reduce overhead.
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def build_command(
        self,
        task_prompt: str,
        system_prompt_file: Path,
        worktree: Path,
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
    ) -> list[str]:
        """Backward-compatible command builder retained for callers/tests."""
        cmd = [
            str(self.claude_bin),
            "-p",
            task_prompt,
            "--system-prompt-file",
            str(system_prompt_file),
            "--output-format",
            "json",
            "--json-schema",
            str(output_schema),
            "--allowedTools",
            ",".join(allowed_tools),
            "--dangerously-skip-permissions",
            "--setting-sources",
            "",
            "--bare",
        ]
        if self.claude_model:
            cmd.extend(["--model", self.claude_model])
        for d in readonly_dirs:
            cmd.extend(["--add-dir", str(d)])
        return cmd

    def _build_command(
        self,
        system_prompt_file: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
    ) -> list[str]:
        cmd = [
            str(self.claude_bin),
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--replay-user-messages",
            "--verbose",
            "--system-prompt-file", str(system_prompt_file),
            # MAC owns the model endpoint and credential configuration. Do not
            # load ~/.claude or project-local settings, whose env block can
            # silently replace the explicitly configured provider and cause
            # authentication failures.
            "--setting-sources", "",
            "--bare",
        ]
        if self.claude_model:
            cmd.extend(["--model", self.claude_model])
        # Claude treats an empty --allowedTools value as the default tool set.
        # Its explicit no-tools form is `--tools ""`.
        if allowed_tools:
            tool_list = ",".join(allowed_tools)
            # --allowedTools only controls which tools skip an approval prompt;
            # it is not a capability whitelist. --tools is the actual surface.
            cmd.extend(["--tools", tool_list, "--allowedTools", tool_list])
        else:
            cmd.extend(["--tools", "", "--strict-mcp-config"])
        for d in readonly_dirs:
            cmd.extend(["--add-dir", str(d)])
        # The orchestrator observes and auto-approves tool calls centrally.
        cmd.append("--dangerously-skip-permissions")
        return cmd

    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path | None,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
        max_tool_calls: int | None = None,
        preferred_provider: str | None = None,
    ) -> AgentRuntimeResult:
        # Write system prompt to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_system_prompt.md", delete=False, encoding="utf-8"
        ) as f:
            f.write(system_prompt)
            system_prompt_file = Path(f.name)

        try:
            return await self._run_session(
                agent_type=agent_type,
                task_prompt=task_prompt,
                system_prompt_file=system_prompt_file,
                worktree=worktree,
                output_schema=output_schema,
                allowed_tools=allowed_tools,
                readonly_dirs=readonly_dirs,
                timeout_seconds=timeout_seconds,
                task_id=task_id,
                progress_callback=progress_callback,
                runtime_env=runtime_env,
                max_tool_calls=max_tool_calls,
            )
        finally:
            try:
                system_prompt_file.unlink()
            except FileNotFoundError:
                pass

    async def _run_session(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt_file: Path,
        worktree: Path,
        output_schema: Path | None,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
        max_tool_calls: int | None = None,
        preferred_provider: str | None = None,
    ) -> AgentRuntimeResult:
        cmd = self._build_command(
            system_prompt_file=system_prompt_file,
            allowed_tools=allowed_tools,
            readonly_dirs=readonly_dirs,
        )

        def _result_output_files() -> list[Path]:
            # A read-only turn cannot have materialized artifacts. Recursively
            # scanning a large repository here made a correctly terminated
            # Coordinator spend minutes in cleanup after hitting its budget.
            if set(allowed_tools).issubset({"Read"}):
                return []
            return self._scan_output_files(worktree)

        process_env = self._env()
        process_env.update(runtime_env or {})
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree),
            env=process_env,
            # Claude tool_use events are one JSON line and Write/Edit inputs
            # can contain an entire artifact. The asyncio default (~64 KiB)
            # is too small for second-round planning artifacts.
            limit=_STREAM_READER_LIMIT,
            # Put Claude and any tool subprocesses it launches in their own
            # process group so cancellation terminates the whole execution.
            start_new_session=os.name != "nt",
        )
        if task_id:
            self._processes[task_id] = proc
            with _ACTIVE_PROCESS_LOCK:
                _ACTIVE_PROCESSES[task_id] = proc
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        result_event: dict[str, Any] | None = None
        tool_calls: list[dict[str, Any]] = []
        # The CLI emits cumulative assistant snapshots: the same thinking /
        # text / tool_use block reappears in every later snapshot of the same
        # message.  Forward each block once, otherwise every thinking card is
        # shown to the user N times.
        seen_progress_texts: set[str] = set()
        seen_tool_use_keys: set[str] = set()

        stdout_queue: asyncio.Queue[Any] = asyncio.Queue()
        stderr_queue: asyncio.Queue[Any] = asyncio.Queue()

        async def _read_stream(stream: asyncio.StreamReader, queue: asyncio.Queue[Any]) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                await queue.put(text)

        async def _send_message(msg: dict[str, Any]) -> None:
            line = json.dumps(msg, ensure_ascii=False)
            if proc.stdin is None:
                return
            proc.stdin.write((line + "\n").encode("utf-8"))
            await proc.stdin.drain()

        # Sentinel: when _read_stream finishes (stdout pipe closed),
        # put this into the queue so the main loop can exit early
        # instead of waiting forever.
        _STREAM_CLOSED = object()

        class _StreamError:
            def __init__(self, error: BaseException):
                self.error = error

        async def _read_stream_with_sentinel():
            try:
                await _read_stream(proc.stdout, stdout_queue)
            except BaseException as exc:
                await stdout_queue.put(_StreamError(exc))
            finally:
                await stdout_queue.put(_STREAM_CLOSED)

        stdout_task = asyncio.create_task(_read_stream_with_sentinel())
        stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_queue))
        tool_process_guard = _DetachedToolProcessGuard(
            proc.pid,
            max_age_seconds=float(process_env["BASH_DEFAULT_TIMEOUT_MS"]) / 1000.0,
            max_total_active_seconds=float(
                process_env["MAC_TOOL_TOTAL_TIMEOUT_MS"]
            ) / 1000.0,
        )
        tool_total_budget_logged = False

        def _cleanup_process() -> None:
            if not task_id:
                return
            if self._processes.get(task_id) is proc:
                del self._processes[task_id]
            with _ACTIVE_PROCESS_LOCK:
                if _ACTIVE_PROCESSES.get(task_id) is proc:
                    del _ACTIVE_PROCESSES[task_id]

        # Send the user prompt immediately.  The stream-json process may not
        # emit any output (including the init event) until it sees input.
        await _send_message({
            "type": "user",
            "message": {"role": "user", "content": task_prompt},
        })

        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout_seconds


            while True:
                overdue_groups = tool_process_guard.poll()
                if overdue_groups:
                    logger.warning(
                        "terminated overdue detached tool process groups; "
                        "task_id=%s groups=%s",
                        task_id,
                        overdue_groups,
                    )
                if (
                    tool_process_guard.total_budget_exceeded
                    and not tool_total_budget_logged
                ):
                    tool_total_budget_logged = True
                    logger.warning(
                        "agent cumulative subprocess budget exceeded; "
                        "task_id=%s budget_seconds=%.1f observed_seconds=%.1f",
                        task_id,
                        tool_process_guard.max_total_active_seconds,
                        tool_process_guard.total_active_seconds,
                    )
                if tool_process_guard.total_budget_exceeded:
                    # The cumulative budget is a turn-level deadline, not a
                    # per-command timeout.  Merely killing the current detached
                    # command lets the model immediately issue another Bash
                    # call and spin until the separate tool-count limit.  Stop
                    # the owning CLI as soon as the cumulative budget is spent.
                    if proc.returncode is None:
                        if os.name != "nt":
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    return AgentRuntimeResult(
                        success=False,
                        output_json=None,
                        output_files=_result_output_files(),
                        logs=(
                            "Cumulative subprocess budget exceeded: "
                            f"allowed {tool_process_guard.max_total_active_seconds:.1f}s"
                        ),
                        cost_info=None,
                        tool_calls=tool_calls,
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.info("EXIT deadline reached (task=%s)", task_id)
                    break
                # Poll in short intervals so we can detect a dead process
                # early instead of waiting until the deadline.
                # Keep the guard responsive even while the CLI is quiet; a
                # five-second queue wait allowed detached commands to overshoot
                # a turn budget and still return a seemingly successful result.
                poll_timeout = min(0.25, remaining)
                try:
                    line = await asyncio.wait_for(stdout_queue.get(), timeout=poll_timeout)
                except asyncio.TimeoutError:
                    # If the subprocess has already exited, there will be
                    # no more events — bail out instead of waiting until
                    # the deadline.
                    if proc.returncode is not None:
                        break

                    continue

                # Stream-closed sentinel — the subprocess stdout pipe
                # has been closed, so no more events will arrive.  Bail out
                # even if the asyncio process object hasn't updated yet.
                if line is _STREAM_CLOSED:
                    break
                if isinstance(line, _StreamError):
                    raise RuntimeError(
                        f"Claude stdout reader failed: {line.error}"
                    ) from line.error

                # Strip ANSI escape sequences for diagnostics.
                clean_line = self._strip_ansi(line).rstrip()
                stdout_lines.append(clean_line)

                event = self._try_parse_json(clean_line)
                if event is None:
                    continue

                event_type = event.get("type")

                logger.info(
                    "EVENT %s (task=%s)", event_type, task_id,
                )

                if event_type == "assistant":
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, dict):
                        content = [content]
                    for block in content:
                        if block.get("type") in ("thinking", "text") and progress_callback:
                            full_text = block.get("text", "")
                            txt = redact_text(full_text)[:500]
                            if txt.strip() and full_text not in seen_progress_texts:
                                seen_progress_texts.add(full_text)
                                await progress_callback("thinking", txt.strip())
                        if block.get("type") == "tool_use":
                            tool_key = block.get("id") or json.dumps(
                                [block.get("name"), block.get("input")],
                                sort_keys=True,
                                default=str,
                            )
                            if tool_key in seen_tool_use_keys:
                                continue
                            seen_tool_use_keys.add(tool_key)
                            tool_calls.append({
                                "name": block.get("name"),
                                "input": self._summarize_tool_input(block),
                            })
                            if (
                                max_tool_calls is not None
                                and len(tool_calls) > max_tool_calls
                            ):
                                logger.warning(
                                    "agent tool-call budget exceeded; task_id=%s "
                                    "budget=%s observed=%s",
                                    task_id,
                                    max_tool_calls,
                                    len(tool_calls),
                                )
                                if os.name != "nt":
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                else:
                                    proc.kill()
                                return AgentRuntimeResult(
                                    success=False,
                                    output_json=None,
                                    output_files=_result_output_files(),
                                    logs=(
                                        "Tool-call budget exceeded: "
                                        f"allowed {max_tool_calls}"
                                    ),
                                    cost_info=None,
                                    tool_calls=tool_calls,
                                )
                            if progress_callback:
                                await progress_callback("tool", json.dumps({
                                    "name": block.get("name"),
                                    "input": self._summarize_tool_input(block),
                                }))
                            logger.info(
                                "TOOL %s delegated to Claude CLI (task=%s)",
                                block.get("name"), task_id,
                            )



                elif event_type == "result":
                    result_event = event
                    break

            # Drain stderr for diagnostics.
            while not stderr_task.done():
                try:
                    line = await asyncio.wait_for(stderr_queue.get(), timeout=1.0)
                    stderr_lines.append(line)
                except asyncio.TimeoutError:
                    break

            logs = "\n".join(stdout_lines + ["--- stderr ---"] + stderr_lines)

            if result_event:
                # Detect API-level errors (e.g. DeepSeek 402 Insufficient Balance,
                # rate limits, network failures) before attempting JSON parse.
                # Claude Code CLI surfaces these as ``is_error: true`` plus a
                # ``result`` string like "API Error: 402 Insufficient Balance".
                # Without this check, we report a misleading "result is not a
                # JSON object" message that hides the real cause.
                api_error = self._extract_api_error(result_event)
                if api_error:
                    api_failure = self._api_failure_metadata(
                        result_event, api_error
                    )
                    logger.warning(
                        "agent API error; task_id=%s error=%s",
                        task_id,
                        api_error,
                    )
                    return AgentRuntimeResult(
                        success=False,
                        output_json=None,
                        output_files=_result_output_files(),
                        logs=logs + f"\nAPI error: {api_error}",
                        cost_info=self._extract_cost_info(result_event),
                        tool_calls=tool_calls,
                        failure=api_failure,
                    )
                raw_result = str(result_event.get("result", "") or "")
                if output_schema is None:
                    return AgentRuntimeResult(
                        success=not result_event.get("is_error", False),
                        output_json=None,
                        output_files=_result_output_files(),
                        logs=logs,
                        cost_info=self._extract_cost_info(result_event),
                        tool_calls=tool_calls,
                        raw_result=raw_result.strip() or None,
                    )
                output_json = self._parse_json_output(raw_result)
                if output_json is None:
                    preview = raw_result[:500]
                    logger.warning(
                        "agent output JSON parse failed; task_id=%s preview=%r",
                        task_id,
                        preview,
                    )
                    return AgentRuntimeResult(
                        success=False,
                        output_json=None,
                        output_files=_result_output_files(),
                        logs=logs + "\nInvalid agent output: result is not a JSON object",
                        cost_info=self._extract_cost_info(result_event),
                        tool_calls=tool_calls,
                        raw_result=raw_result.strip() or None,
                    )
                output_json = normalize_optional_nulls(
                    redact_secrets(output_json), output_schema
                )
                contract_error = validate_agent_output(output_json, output_schema)
                if contract_error:
                    try:
                        preview = json.dumps(output_json, ensure_ascii=False)[:800]
                    except (TypeError, ValueError):
                        preview = repr(output_json)[:800]
                    logger.warning(
                        "agent output schema validation failed; task_id=%s error=%s preview=%s",
                        task_id,
                        contract_error,
                        preview,
                    )
                    return AgentRuntimeResult(
                        success=False,
                        output_json=None,
                        output_files=_result_output_files(),
                        logs=logs + f"\nInvalid agent output: {contract_error}",
                        cost_info=self._extract_cost_info(result_event),
                        tool_calls=tool_calls,
                        raw_result=str(raw_result or "").strip() or None,
                    )
                return AgentRuntimeResult(
                    success=not result_event.get("is_error", False) and output_json is not None,
                    output_json=output_json,
                    output_files=_result_output_files(),
                    logs=logs,
                    cost_info=self._extract_cost_info(result_event),
                    tool_calls=tool_calls,
                )

            return AgentRuntimeResult(
                success=False,
                output_json=None,
                output_files=[],
                logs="No result event received before timeout",
                cost_info=None,
                tool_calls=tool_calls,
            )

        finally:
            tool_process_guard.poll()
            tool_process_guard.cleanup()
            _cleanup_process()
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                if proc.returncode is None:
                    if os.name != "nt":
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
            stdout_task.cancel()
            stderr_task.cancel()

    def cancel(self, task_id: str) -> None:
        """Kill a task's process group, including from another Runtime instance."""
        proc = self._processes.pop(task_id, None)
        with _ACTIVE_PROCESS_LOCK:
            shared_proc = _ACTIVE_PROCESSES.pop(task_id, None)
        proc = shared_proc or proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def cancel_all_active() -> list[str]:
        """Kill every process-backed Agent during service shutdown.

        Runtime instances used by startup recovery are short-lived and are not
        retained on the FastAPI app. The shared registry is therefore the only
        reliable shutdown boundary; without this sweep, stopping uvicorn can
        orphan a Claude process that keeps editing after its DB task has been
        marked interrupted.
        """
        with _ACTIVE_PROCESS_LOCK:
            active = list(_ACTIVE_PROCESSES.items())
            _ACTIVE_PROCESSES.clear()
        for _, proc in active:
            if proc.returncode is not None:
                continue
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
        return [task_id for task_id, _ in active]

    @staticmethod
    def active_task_ids() -> list[str]:
        """Return process-backed task IDs for test/diagnostic observability."""
        with _ACTIVE_PROCESS_LOCK:
            return sorted(_ACTIVE_PROCESSES)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        return ansi_escape.sub("", text)

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    @staticmethod
    def _summarize_tool_input(block: dict[str, Any]) -> dict[str, Any]:
        """Return progress-safe metadata without embedding artifact contents."""
        name = block.get("name")
        value = block.get("input")
        if not isinstance(value, dict):
            return {}
        if name == "Write":
            content = value.get("content", "")
            return {
                "file_path": value.get("file_path"),
                "content_length": len(content) if isinstance(content, str) else None,
            }
        if name == "Edit":
            old = value.get("old_string", "")
            new = value.get("new_string", "")
            return {
                "file_path": value.get("file_path"),
                "old_length": len(old) if isinstance(old, str) else None,
                "new_length": len(new) if isinstance(new, str) else None,
                "replace_all": bool(value.get("replace_all", False)),
            }
        if name == "Bash":
            return {"command": str(value.get("command", ""))[:200]}
        summary: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                summary[key] = item[:200]
            elif isinstance(item, (int, float, bool)) or item is None:
                summary[key] = item
        return summary

    def _parse_json_output(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        # Try to unwrap a Claude Code result envelope first.
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            if envelope.get("type") == "result" and isinstance(envelope.get("result"), str):
                try:
                    return json.loads(envelope["result"])
                except json.JSONDecodeError:
                    return None
            return envelope
        # Models occasionally wrap the one protocol object in a short preface
        # and/or a Markdown fence. Decode from each object boundary and allow a
        # harmless suffix instead of requiring the entire remainder to be JSON.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                parsed, _ = decoder.raw_decode(text, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _extract_api_error(result_event: dict[str, Any]) -> str | None:
        """Return a human-readable API error message if the result event
        represents an upstream API failure, else ``None``.

        Claude Code CLI reports upstream API errors (402 Insufficient Balance,
        429 rate limit, 5xx server errors, network failures) by setting
        ``is_error: true`` and putting a plain-text message in ``result`` like
        ``"API Error: 402 Insufficient Balance"``. The optional
        ``api_error_status`` field carries the HTTP code. We surface these
        explicitly so callers do not mistake them for malformed LLM JSON.
        """
        if not isinstance(result_event, dict):
            return None
        if not result_event.get("is_error"):
            return None
        result_text = str(result_event.get("result", "") or "")
        api_status = result_event.get("api_error_status")
        # Strip surrounding JSON envelope if the result text itself is JSON.
        if result_text.startswith("{"):
            try:
                inner = json.loads(result_text)
                if isinstance(inner, dict) and isinstance(inner.get("result"), str):
                    result_text = inner["result"]
            except json.JSONDecodeError:
                pass
        if "API Error" in result_text or "Insufficient Balance" in result_text:
            message = result_text.strip()
            if api_status and str(api_status) not in message:
                message = f"{message} (status={api_status})"
            return message[:300]
        # Generic fallback: is_error=true with no recognizable API string.
        if api_status:
            return f"upstream API error (status={api_status})"
        return result_text.strip()[:300] or "upstream API error"

    @staticmethod
    def _api_failure_metadata(
        result_event: dict[str, Any], message: str
    ) -> dict[str, Any]:
        """Classify an upstream HTTP failure from protocol semantics.

        This deliberately does not route on product names or prose keywords.
        A status supplied by the runtime is authoritative; the standardized
        three-digit status in an ``API Error`` envelope is only a compatibility
        fallback for providers that omit ``api_error_status``.
        """
        raw_status = result_event.get("api_error_status")
        status: int | None = None
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        if status is None:
            match = re.search(r"\b([1-5]\d{2})\b", str(message))
            if match:
                status = int(match.group(1))

        retryable = (
            status is None
            or status in {408, 409, 425, 429}
            or 500 <= status <= 599
        )
        return {
            "domain": "upstream_api",
            "status_code": status,
            "retryable": retryable,
            "suggested_resolver": "root_agent",
        }

    def _extract_cost_info(self, data: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if not data:
            return None
        # Stream-json result event
        if isinstance(data, dict):
            usage = data.get("usage", {})
            total_cost = data.get("total_cost_usd")
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            total_tokens = None
            if input_tokens is not None and output_tokens is not None:
                total_tokens = input_tokens + output_tokens
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": total_cost,
                "model_usage": data.get("modelUsage"),
            }
        # Legacy stderr scraping
        text = str(data)
        tokens_match = re.search(
            r"input tokens[:\s]+(\d+).*?output tokens[:\s]+(\d+)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        cost_match = re.search(
            r"(?:estimated cost|cost)[:\s]+\$?([\d.]+)",
            text,
            re.IGNORECASE,
        )
        if tokens_match:
            input_tokens = int(tokens_match.group(1))
            output_tokens = int(tokens_match.group(2))
            cost = None
            if cost_match:
                cost = float(cost_match.group(1))
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "estimated_cost_usd": cost,
            }
        return None

    def _scan_output_files(self, worktree: Path) -> list[Path]:
        files: list[Path] = []
        for path in worktree.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(worktree)
            parts = rel.parts
            if parts and parts[0] in (".git", ".mac"):
                continue
            files.append(rel)
        return files


class _JsonLineCliRuntime(AgentRuntime):
    """Shared process lifecycle for non-interactive JSONL coding CLIs."""

    provider_name = "cli"

    def __init__(self, executable: Path, model: str | None = None):
        self.executable = Path(executable)
        self.model = model
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def provider_for(self, agent_type: str) -> str:
        return self.provider_name

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        worktree: Path,
        allowed_tools: list[str],
    ) -> list[str]:
        ...

    @abstractmethod
    def parse_events(
        self, lines: list[str]
    ) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None, str | None]:
        """Return final text, tool calls, cost info and provider error."""
        ...

    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path | None,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
        max_tool_calls: int | None = None,
        preferred_provider: str | None = None,
    ) -> AgentRuntimeResult:
        prompt = task_prompt
        if system_prompt:
            prompt = (
                "<system_contract>\n"
                + system_prompt
                + "\n</system_contract>\n\n<task>\n"
                + task_prompt
                + "\n</task>"
            )
        if output_schema is not None:
            prompt += (
                "\n\nFollow the system contract above. Return exactly one JSON "
                "object; do not wrap it in Markdown."
            )
        cmd = self.build_command(prompt, worktree, allowed_tools)

        def _result_output_files() -> list[Path]:
            if set(allowed_tools).issubset({"Read"}):
                return []
            return self._scan_output_files(worktree)

        env = os.environ.copy()
        env.update(runtime_env or {})
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree),
            env=env,
            limit=_STREAM_READER_LIMIT,
            start_new_session=os.name != "nt",
        )
        tool_process_guard = _DetachedToolProcessGuard(
            proc.pid,
            max_age_seconds=float(
                env.get("MAC_TOOL_PROCESS_TIMEOUT_SECONDS", "300")
            ),
        )

        subprocess_budget_exceeded = False

        async def _watch_tool_processes() -> None:
            nonlocal subprocess_budget_exceeded
            while proc.returncode is None:
                overdue_groups = tool_process_guard.poll()
                if overdue_groups:
                    logger.warning(
                        "terminated overdue detached tool process groups; "
                        "task_id=%s provider=%s groups=%s",
                        task_id,
                        self.provider_name,
                        overdue_groups,
                    )
                if tool_process_guard.total_budget_exceeded:
                    subprocess_budget_exceeded = True
                    self._terminate(proc)
                    break
                await asyncio.sleep(0.25)

        tool_guard_task = asyncio.create_task(_watch_tool_processes())
        if task_id:
            self._processes[task_id] = proc
            with _ACTIVE_PROCESS_LOCK:
                _ACTIVE_PROCESSES[task_id] = proc
        try:
            stdout_lines: list[str] = []
            stderr_task = asyncio.create_task(proc.stderr.read())
            observed_tool_calls: list[dict[str, Any]] = []
            tool_budget_exceeded = False

            async def _collect_stdout() -> None:
                nonlocal tool_budget_exceeded
                while True:
                    raw_line = await proc.stdout.readline()
                    if not raw_line:
                        break
                    clean = self._strip_ansi(
                        raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    )
                    if not clean.strip():
                        continue
                    stdout_lines.append(clean)
                    if progress_callback:
                        _, line_tools, _, _ = self.parse_events([clean])
                        for call in line_tools:
                            observed_tool_calls.append(call)
                            await progress_callback(
                                "tool", json.dumps(call, ensure_ascii=False)
                            )
                    elif max_tool_calls is not None:
                        _, line_tools, _, _ = self.parse_events([clean])
                        observed_tool_calls.extend(line_tools)
                    if (
                        max_tool_calls is not None
                        and len(observed_tool_calls) > max_tool_calls
                    ):
                        tool_budget_exceeded = True
                        self._terminate(proc)
                        break
                await proc.wait()

            try:
                await asyncio.wait_for(_collect_stdout(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                self._terminate(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                stderr_task.cancel()
                return AgentRuntimeResult(
                    False, None, [], f"{self.provider_name} timed out", None
                )
            stderr = await stderr_task
            if subprocess_budget_exceeded:
                return AgentRuntimeResult(
                    False,
                    None,
                    _result_output_files(),
                    (
                        "Cumulative subprocess budget exceeded: "
                        f"allowed {tool_process_guard.max_total_active_seconds:.1f}s"
                    ),
                    None,
                    observed_tool_calls,
                )
            if tool_budget_exceeded:
                return AgentRuntimeResult(
                    False,
                    None,
                    _result_output_files(),
                    f"Tool-call budget exceeded: allowed {max_tool_calls}",
                    None,
                    observed_tool_calls,
                )
            stdout_text = "\n".join(stdout_lines)
            stderr_text = self._strip_ansi(stderr.decode("utf-8", errors="replace"))
            final_text, tool_calls, cost_info, provider_error = self.parse_events(
                stdout_lines
            )
            logs = stdout_text + ("\n--- stderr ---\n" + stderr_text if stderr_text else "")
            if provider_error or proc.returncode != 0:
                error = provider_error or f"process exited with status {proc.returncode}"
                return AgentRuntimeResult(
                    False,
                    None,
                    _result_output_files(),
                    logs + f"\n{self.provider_name} error: {error}",
                    cost_info,
                    tool_calls,
                )
            if output_schema is None:
                return AgentRuntimeResult(
                    True,
                    None,
                    _result_output_files(),
                    logs,
                    cost_info,
                    tool_calls,
                    raw_result=(final_text or "").strip() or None,
                )
            output_json = self._parse_json_output(final_text or "")
            if output_json is None:
                return AgentRuntimeResult(
                    False,
                    None,
                    _result_output_files(),
                    logs + "\nInvalid agent output: result is not a JSON object",
                    cost_info,
                    tool_calls,
                    raw_result=(final_text or "").strip() or None,
                )
            output_json = normalize_optional_nulls(
                redact_secrets(output_json), output_schema
            )
            contract_error = validate_agent_output(output_json, output_schema)
            if contract_error:
                return AgentRuntimeResult(
                    False,
                    None,
                    _result_output_files(),
                    logs + f"\nInvalid agent output: {contract_error}",
                    cost_info,
                    tool_calls,
                    raw_result=(final_text or "").strip() or None,
                )
            return AgentRuntimeResult(
                True,
                output_json,
                _result_output_files(),
                logs,
                cost_info,
                tool_calls,
            )
        finally:
            tool_guard_task.cancel()
            tool_process_guard.poll()
            tool_process_guard.cleanup()
            if task_id:
                self._processes.pop(task_id, None)
                with _ACTIVE_PROCESS_LOCK:
                    if _ACTIVE_PROCESSES.get(task_id) is proc:
                        _ACTIVE_PROCESSES.pop(task_id, None)
            if proc.returncode is None:
                self._terminate(proc)

    def cancel(self, task_id: str) -> None:
        proc = self._processes.pop(task_id, None)
        with _ACTIVE_PROCESS_LOCK:
            proc = _ACTIVE_PROCESSES.pop(task_id, None) or proc
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return ClaudeCodeRuntime._strip_ansi(text)

    @staticmethod
    def _parse_json_output(text: str) -> dict[str, Any] | None:
        return ClaudeCodeRuntime._parse_json_output(ClaudeCodeRuntime, text)

    @staticmethod
    def _scan_output_files(worktree: Path) -> list[Path]:
        return ClaudeCodeRuntime._scan_output_files(ClaudeCodeRuntime, worktree)


class KimiCodeRuntime(_JsonLineCliRuntime):
    provider_name = "kimi_code"

    def build_command(
        self, prompt: str, worktree: Path, allowed_tools: list[str]
    ) -> list[str]:
        cmd = [
            str(self.executable),
            "--work-dir", str(worktree),
            "--print",
            "--output-format", "stream-json",
            "--yolo",
            "--prompt", prompt,
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        return cmd

    def parse_events(
        self, lines: list[str]
    ) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None, str | None]:
        final_text: str | None = None
        tool_calls: list[dict[str, Any]] = []
        for line in lines:
            event = ClaudeCodeRuntime._try_parse_json(line)
            if not event:
                continue
            if event.get("error"):
                return final_text, tool_calls, None, str(event["error"])
            content = event.get("content", [])
            if isinstance(content, dict):
                content = [content]
            if event.get("role") == "assistant":
                texts = [
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if texts:
                    final_text = "\n".join(texts)
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"tool_use", "tool_call"}:
                    raw_input = block.get("input") or block.get("arguments") or {}
                    safe_input = (
                        ClaudeCodeRuntime._summarize_tool_input({
                            "name": block.get("name") or block.get("tool_name"),
                            "input": raw_input,
                        })
                        if isinstance(raw_input, dict)
                        else {}
                    )
                    tool_calls.append({
                        "name": block.get("name") or block.get("tool_name"),
                        "input": safe_input,
                    })
        return final_text, tool_calls, None, None


class CodexRuntime(_JsonLineCliRuntime):
    provider_name = "codex"

    def build_command(
        self, prompt: str, worktree: Path, allowed_tools: list[str]
    ) -> list[str]:
        # Shell access is also needed by read-only validation agents. Only
        # explicit file-editing tools grant a writable Codex sandbox.
        # Some read-only validation agents must create browser profiles,
        # screenshots and service logs. ``SandboxWrite`` grants the Codex
        # process a writable execution sandbox without exposing Edit/Write as
        # an application-level tool; the agent prompt still forbids changing
        # delivery sources.
        can_write = bool(
            {"Edit", "Write", "SandboxWrite"}.intersection(allowed_tools)
        )
        # Real-entry acceptance on macOS needs to launch a browser, create its
        # profile/Mach ports, and often bind a loopback HTTP server. Those host
        # capabilities cannot work in Codex's Seatbelt workspace sandbox.
        # Keep this an explicit, acceptance-only capability instead of silently
        # weakening every Codex-backed Agent.
        needs_host_automation = "HostAutomation" in allowed_tools
        sandbox_mode = (
            "danger-full-access"
            if needs_host_automation
            else ("workspace-write" if can_write else "read-only")
        )
        cmd = [
            str(self.executable), "exec",
            "--ephemeral",
            "--ignore-user-config",
            # Codex 0.144 ships an unsigned code-mode host on macOS. Disabling
            # that host selects the stable shell tool and avoids a model-level
            # environment_failure loop while preserving real tool execution.
            "--disable", "code_mode_host",
            "--skip-git-repo-check",
            "-C", str(worktree),
            "--json",
            "-s", sandbox_mode,
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append(prompt)
        return cmd

    def parse_events(
        self, lines: list[str]
    ) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None, str | None]:
        final_text: str | None = None
        tool_calls: list[dict[str, Any]] = []
        cost_info: dict[str, Any] | None = None
        for line in lines:
            event = ClaudeCodeRuntime._try_parse_json(line)
            if not event:
                continue
            event_type = event.get("type")
            if event_type in {"turn.failed", "error"}:
                return final_text, tool_calls, cost_info, str(
                    event.get("error") or event.get("message") or event
                )
            if event_type == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type")
                if item_type == "agent_message":
                    final_text = item.get("text") or final_text
                elif item_type in {"command_execution", "file_change", "mcp_tool_call"}:
                    tool_calls.append({
                        "name": item_type,
                        "input": {
                            key: (
                                len(item.get(key))
                                if key == "changes" and isinstance(item.get(key), list)
                                else str(item.get(key))[:200]
                            )
                            for key in ("command", "path", "changes", "server", "tool")
                            if item.get(key) is not None
                        },
                    })
            elif event_type == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    cost_info = dict(usage)
        return final_text, tool_calls, cost_info, None


class PiRuntime(_JsonLineCliRuntime):
    """Drive Pi's ephemeral JSONL mode with MAC-owned context and tools.

    Pi can discover user-level context files, skills and extensions.  MAC turns
    that discovery off deliberately: every stage must receive only the system
    contract, WorkItem context and capabilities selected by the Orchestrator.
    Provider authentication remains Pi-owned and is resolved at the process
    boundary without copying credentials into project state.
    """

    provider_name = "pi"

    def __init__(
        self,
        executable: Path,
        model: str | None = None,
        *,
        provider: str | None = None,
        thinking: str | None = None,
    ):
        super().__init__(executable, model)
        self.provider = provider
        self.thinking = thinking

    @staticmethod
    def _pi_tools(allowed_tools: list[str]) -> list[str]:
        tools: list[str] = []
        if "Read" in allowed_tools:
            tools.extend(["read", "grep", "find", "ls"])
        if "Edit" in allowed_tools:
            tools.append("edit")
        if "Write" in allowed_tools:
            tools.append("write")
        if any(
            capability in allowed_tools
            for capability in ("Bash", "SandboxWrite", "HostAutomation")
        ):
            tools.append("bash")
        return list(dict.fromkeys(tools))

    def build_command(
        self, prompt: str, worktree: Path, allowed_tools: list[str]
    ) -> list[str]:
        cmd = [
            str(self.executable),
            "--mode", "json",
            "--print",
            "--no-session",
            "--no-context-files",
            "--no-skills",
            "--no-extensions",
            "--no-prompt-templates",
            "--offline",
        ]
        tools = self._pi_tools(allowed_tools)
        if tools:
            cmd.extend(["--tools", ",".join(tools)])
        else:
            cmd.append("--no-tools")
        if self.provider:
            cmd.extend(["--provider", self.provider])
        if self.model:
            cmd.extend(["--model", self.model])
        if self.thinking:
            cmd.extend(["--thinking", self.thinking])
        cmd.append(prompt)
        return cmd

    @staticmethod
    def _message_text(message: Any) -> str | None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        content = message.get("content") or []
        if isinstance(content, dict):
            content = [content]
        texts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(texts) if texts else None

    def parse_events(
        self, lines: list[str]
    ) -> tuple[str | None, list[dict[str, Any]], dict[str, Any] | None, str | None]:
        final_text: str | None = None
        tool_calls: list[dict[str, Any]] = []
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
        observed_usage = False
        for line in lines:
            event = ClaudeCodeRuntime._try_parse_json(line)
            if not event:
                continue
            event_type = str(event.get("type") or "")
            if event_type in {"error", "agent_error"}:
                return final_text, tool_calls, (
                    usage_totals if observed_usage else None
                ), str(event.get("error") or event.get("message") or event)

            if event_type == "tool_execution_start":
                raw_args = event.get("args") or {}
                safe_input = (
                    ClaudeCodeRuntime._summarize_tool_input({
                        "name": event.get("toolName"),
                        "input": raw_args,
                    })
                    if isinstance(raw_args, dict)
                    else {}
                )
                tool_calls.append({
                    "name": event.get("toolName"),
                    "input": safe_input,
                })

            if event_type != "message_end":
                continue
            message = event.get("message") or {}
            text = self._message_text(message)
            if text:
                final_text = text
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            observed_usage = True
            input_tokens = int(usage.get("input") or 0)
            output_tokens = int(usage.get("output") or 0)
            cache_read = int(usage.get("cacheRead") or 0)
            cache_write = int(usage.get("cacheWrite") or 0)
            usage_totals["input_tokens"] += input_tokens
            usage_totals["output_tokens"] += output_tokens
            usage_totals["cache_read_input_tokens"] += cache_read
            usage_totals["cache_creation_input_tokens"] += cache_write
            usage_totals["total_tokens"] += int(
                usage.get("totalTokens")
                or input_tokens + output_tokens + cache_read + cache_write
            )
            cost = usage.get("cost") or {}
            if isinstance(cost, dict):
                usage_totals["estimated_cost_usd"] += float(cost.get("total") or 0)

        return (
            final_text,
            tool_calls,
            usage_totals if observed_usage else None,
            None,
        )


class RuntimeRouter(AgentRuntime):
    """Route a request to exactly the provider selected by the caller.

    This transport does not invent retry or fallback policy. A failed external
    tool call is returned to the Codex root Agent, whose native turn loop owns
    the next decision.
    """

    _USAGE_FIELDS = (
        "input_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "estimated_cost_usd",
    )

    def __init__(
        self,
        providers: dict[str, AgentRuntime],
        agent_providers: dict[str, str],
        default_provider: str = "claude_code",
        provider_profiles: dict[str, dict[str, Any]] | None = None,
    ):
        if default_provider not in providers:
            raise ValueError(f"Default runtime provider is unavailable: {default_provider}")
        self.providers = providers
        self.agent_providers = agent_providers
        self.default_provider = default_provider
        self._provider_profiles = {
            provider: dict((provider_profiles or {}).get(provider, {}))
            for provider in providers
        }
        # Cancellation can land in the tiny gap after the primary provider
        # exits and before the fallback provider registers its subprocess.
        # Keep a router-level latch so that gap cannot resurrect cancelled work.
        self._active_task_ids: set[str] = set()
        self._cancelled_task_ids: set[str] = set()

    def provider_for(self, agent_type: str) -> str:
        selected = self.agent_providers.get(agent_type, self.default_provider)
        return selected if selected in self.providers else self.default_provider

    def available_providers(self) -> set[str]:
        return set(self.providers)

    def provider_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            provider: dict(profile)
            for provider, profile in self._provider_profiles.items()
        }

    @classmethod
    def _runtime_attempt(
        cls,
        provider: str,
        result: AgentRuntimeResult,
        *,
        runtime_blocked: bool = False,
    ) -> dict[str, Any]:
        attempt: dict[str, Any] = {
            "provider": provider,
            "success": bool(result.success and not runtime_blocked),
        }
        for key in cls._USAGE_FIELDS:
            value = (result.cost_info or {}).get(key)
            if isinstance(value, (int, float)):
                attempt[key] = value
        if "total_tokens" not in attempt:
            input_tokens = attempt.get("input_tokens")
            output_tokens = attempt.get("output_tokens")
            if isinstance(input_tokens, (int, float)) and isinstance(
                output_tokens, (int, float)
            ):
                attempt["total_tokens"] = input_tokens + output_tokens
        if not attempt["success"]:
            if runtime_blocked:
                reason = "runtime_blocker"
            elif isinstance(result.failure, dict) and result.failure.get("domain"):
                reason = str(result.failure["domain"])
            elif "Cumulative subprocess budget exceeded" in result.logs:
                reason = "subprocess_budget_exceeded"
            elif "Tool-call budget exceeded" in result.logs:
                reason = "tool_call_budget_exceeded"
            elif "Invalid agent output" in result.logs:
                reason = "invalid_agent_output"
            elif " error:" in result.logs:
                reason = "provider_error"
            else:
                reason = "provider_failure"
            attempt["failure_reason"] = reason
        return attempt

    @classmethod
    def _attach_runtime_attempts(
        cls,
        result: AgentRuntimeResult,
        attempts: list[dict[str, Any]],
    ) -> None:
        combined = dict(result.cost_info or {})
        combined["runtime_attempts"] = attempts
        for key in cls._USAGE_FIELDS:
            values = [
                attempt[key]
                for attempt in attempts
                if isinstance(attempt.get(key), (int, float))
            ]
            if values:
                combined[key] = sum(values)
        result.cost_info = combined

    async def execute(self, agent_type: str, *args: Any, **kwargs: Any) -> AgentRuntimeResult:
        preferred_provider = kwargs.pop("preferred_provider", None)
        task_id = kwargs.get("task_id")
        if task_id:
            self._active_task_ids.add(task_id)
            self._cancelled_task_ids.discard(task_id)
            with _ACTIVE_PROCESS_LOCK:
                _CANCELLED_TASK_IDS.discard(task_id)
        if preferred_provider is not None and preferred_provider not in self.providers:
            return AgentRuntimeResult(
                success=False,
                output_json=None,
                output_files=[],
                logs=f"runtime provider is unavailable: {preferred_provider}",
                cost_info=None,
                failure={
                    "domain": "provider_unavailable",
                    "provider": preferred_provider,
                },
            )
        provider = preferred_provider or self.provider_for(agent_type)
        logger.info("runtime route agent_type=%s provider=%s", agent_type, provider)
        try:
            result = await self.providers[provider].execute(agent_type, *args, **kwargs)
            self._attach_runtime_attempts(
                result,
                [self._runtime_attempt(provider, result)],
            )
            result.logs = f"runtime_provider={provider}\n" + result.logs
            return result
        finally:
            if task_id:
                self._active_task_ids.discard(task_id)
                self._cancelled_task_ids.discard(task_id)
                with _ACTIVE_PROCESS_LOCK:
                    _CANCELLED_TASK_IDS.discard(task_id)

    def cancel(self, task_id: str) -> None:
        if task_id in self._active_task_ids:
            self._cancelled_task_ids.add(task_id)
        # Feishu handlers and background project workers can own different
        # RuntimeRouter instances.  Keep the cancellation latch process-wide
        # so one instance cannot kill the primary provider while another
        # mistakes that exit for a provider failure and starts a fallback.
        with _ACTIVE_PROCESS_LOCK:
            _CANCELLED_TASK_IDS.add(task_id)
        for runtime in self.providers.values():
            runtime.cancel(task_id)


def build_agent_runtime(settings: Any, agent_config: dict[str, Any]) -> AgentRuntime:
    """Build the production provider registry and per-Agent router."""
    claude = ClaudeCodeRuntime(
        claude_bin=settings.claude_bin,
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_auth_token=settings.anthropic_auth_token,
        claude_model=settings.claude_model,
        anthropic_base_url=settings.anthropic_base_url,
        anthropic_default_opus_model=settings.anthropic_default_opus_model,
        anthropic_default_sonnet_model=settings.anthropic_default_sonnet_model,
        anthropic_default_haiku_model=settings.anthropic_default_haiku_model,
        claude_code_effort_level=settings.claude_code_effort_level,
        api_timeout_ms=settings.api_timeout_ms,
    )
    providers: dict[str, AgentRuntime] = {"claude_code": claude}
    runtime_cfg = agent_config.get("runtimes", {})

    def _available(path: Path) -> bool:
        return path.is_file() or shutil.which(str(path)) is not None

    if runtime_cfg.get("kimi_code", {}).get("enabled", True) and _available(settings.kimi_bin):
        providers["kimi_code"] = KimiCodeRuntime(
            settings.kimi_bin, runtime_cfg.get("kimi_code", {}).get("model")
        )
    if runtime_cfg.get("codex", {}).get("enabled", True) and _available(settings.codex_bin):
        providers["codex"] = CodexRuntime(
            settings.codex_bin, runtime_cfg.get("codex", {}).get("model")
        )
    if runtime_cfg.get("pi", {}).get("enabled", True) and _available(settings.pi_bin):
        pi_cfg = runtime_cfg.get("pi", {})
        providers["pi"] = PiRuntime(
            settings.pi_bin,
            pi_cfg.get("model"),
            provider=pi_cfg.get("provider"),
            thinking=pi_cfg.get("thinking"),
        )
    assignments = {
        agent: cfg.get("runtime", "claude_code")
        for agent, cfg in agent_config.get("agents", {}).items()
    }
    for agent, selected in list(assignments.items()):
        if selected not in providers:
            logger.warning(
                "runtime provider %s unavailable for %s; falling back to claude_code",
                selected,
                agent,
            )
            assignments[agent] = "claude_code"
    profile_fields = ("description", "mode", "model", "provider")
    provider_profiles = {
        provider: {
            field: runtime_cfg.get(provider, {}).get(field)
            for field in profile_fields
            if runtime_cfg.get(provider, {}).get(field) is not None
        }
        for provider in providers
    }
    return RuntimeRouter(
        providers,
        assignments,
        provider_profiles=provider_profiles,
    )


class FakeRuntime(AgentRuntime):
    """Runtime for testing that returns predefined outputs."""

    materializes_worktree_changes = False

    def __init__(self, outputs: dict[str, dict[str, Any]]):
        self.outputs = outputs

    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path | None,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
        runtime_env: dict[str, str] | None = None,
        max_tool_calls: int | None = None,
        preferred_provider: str | None = None,
    ) -> AgentRuntimeResult:
        output = self.outputs.get(agent_type)
        if output is None:
            output = {"success": True}
        return AgentRuntimeResult(
            success=output.get("success", True),
            output_json=output,
            output_files=[],
            logs=str(output.get("_runtime_logs", "fake runtime")),
            cost_info=None,
            tool_calls=list(output.get("_runtime_tool_calls", [])),
            failure=(
                dict(output["_runtime_failure"])
                if isinstance(output.get("_runtime_failure"), dict)
                else None
            ),
        )

    def cancel(self, task_id: str) -> None:
        pass
