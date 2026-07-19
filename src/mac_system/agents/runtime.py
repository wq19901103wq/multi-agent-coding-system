import asyncio
import json
import logging
import os
import re
import signal
import threading
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Runtime instances are intentionally short-lived: API requests and Feishu
# events each construct their own orchestrator/runtime.  Cancellation must
# therefore use a process registry shared by all instances in this process.
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_STREAM_READER_LIMIT = 16 * 1024 * 1024
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    _h.setLevel(logging.INFO)
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


@dataclass
class AgentRuntimeResult:
    success: bool
    output_json: dict[str, Any] | None
    output_files: list[Path]
    logs: str
    cost_info: dict[str, Any] | None


class AgentRuntime(ABC):
    @abstractmethod
    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentRuntimeResult:
        ...

    @abstractmethod
    def cancel(self, task_id: str) -> None:
        """Best-effort cancellation of a running task."""
        ...


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
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
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
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentRuntimeResult:
        # Write system prompt to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix="_system_prompt.md", delete=False, encoding="utf-8"
        ) as f:
            f.write(system_prompt)
            system_prompt_file = Path(f.name)

        try:
            return await self._run_session(
                task_prompt=task_prompt,
                system_prompt_file=system_prompt_file,
                worktree=worktree,
                allowed_tools=allowed_tools,
                readonly_dirs=readonly_dirs,
                timeout_seconds=timeout_seconds,
                task_id=task_id,
                progress_callback=progress_callback,
            )
        finally:
            try:
                system_prompt_file.unlink()
            except FileNotFoundError:
                pass

    async def _run_session(
        self,
        task_prompt: str,
        system_prompt_file: Path,
        worktree: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentRuntimeResult:
        cmd = self._build_command(
            system_prompt_file=system_prompt_file,
            allowed_tools=allowed_tools,
            readonly_dirs=readonly_dirs,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree),
            env=self._env(),
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
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.info("EXIT deadline reached (task=%s)", task_id)
                    break
                # Poll in short intervals so we can detect a dead process
                # early instead of waiting until the deadline.
                poll_timeout = min(5.0, remaining)
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
                            txt = block.get("text", "")[:500]
                            if txt.strip():
                                await progress_callback("thinking", txt.strip())
                        if block.get("type") == "tool_use":
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
                output_json = self._parse_json_output(result_event.get("result", ""))
                if output_json is None:
                    output_json = {
                        "success": not result_event.get("is_error", False),
                        "result_text": result_event.get("result", ""),
                    }
                return AgentRuntimeResult(
                    success=not result_event.get("is_error", False) and output_json is not None,
                    output_json=output_json,
                    output_files=self._scan_output_files(worktree),
                    logs=logs,
                    cost_info=self._extract_cost_info(result_event),
                )

            return AgentRuntimeResult(
                success=False,
                output_json=None,
                output_files=[],
                logs="No result event received before timeout",
                cost_info=None,
            )

        finally:
            _cleanup_process()
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                if proc.returncode is None:
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
        start = text.find("{")
        if start == -1:
            return None
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
        return None

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


class FakeRuntime(AgentRuntime):
    """Runtime for testing that returns predefined outputs."""

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
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
        task_id: str | None = None,
        progress_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> AgentRuntimeResult:
        output = self.outputs.get(agent_type, {"success": True, "next_state_event": "complete"})
        return AgentRuntimeResult(
            success=output.get("success", True),
            output_json=output,
            output_files=[],
            logs="fake runtime",
            cost_info=None,
        )

    def cancel(self, task_id: str) -> None:
        pass
