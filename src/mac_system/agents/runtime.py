import asyncio
import json
import os
import re
import shlex
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    ) -> AgentRuntimeResult:
        ...


class ToolExecutionError(Exception):
    """Raised when a local tool invocation fails."""


class ClaudeCodeRuntime(AgentRuntime):
    """Drive Claude Code CLI via a stream-json subprocess session.

    Claude Code's stream-json mode (``--input-format stream-json
    --output-format stream-json``) emits JSON events on stdout and accepts
    JSON events on stdin.  Running with piped stdio makes Claude Code treat
    the session as non-interactive, which also skips the workspace trust
    dialog.  This runtime executes the built-in tools (Bash/Read/Write/
    Edit/Glob/Grep) locally so the model can iterate against the codebase.
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
        ]
        if self.claude_model:
            cmd.extend(["--model", self.claude_model])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
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
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        result_event: dict[str, Any] | None = None

        stdout_queue: asyncio.Queue[str] = asyncio.Queue()
        stderr_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _read_stream(stream: asyncio.StreamReader, queue: asyncio.Queue[str]) -> None:
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

        stdout_task = asyncio.create_task(_read_stream(proc.stdout, stdout_queue))
        stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_queue))

        # Send the user prompt immediately.  The stream-json process may not
        # emit any output (including the init event) until it sees input.
        await _send_message({
            "type": "user",
            "message": {"role": "user", "content": task_prompt},
        })

        def _execute_tool(tool_use: dict[str, Any]) -> dict[str, Any]:
            name = tool_use.get("name")
            tool_use_id = tool_use.get("id")
            input_data = tool_use.get("input", {})
            try:
                if name == "Bash":
                    result = self._tool_bash(input_data, cwd=worktree)
                elif name == "Read":
                    result = self._tool_read(input_data)
                elif name == "Write":
                    result = self._tool_write(input_data, cwd=worktree)
                elif name == "Edit":
                    result = self._tool_edit(input_data, cwd=worktree)
                elif name == "Glob":
                    result = self._tool_glob(input_data, cwd=worktree)
                elif name == "Grep":
                    result = self._tool_grep(input_data, cwd=worktree)
                else:
                    raise ToolExecutionError(f"Tool {name} not implemented")
                return {
                    "tool_use_id": tool_use_id,
                    "type": "tool_result",
                    "content": result,
                    "is_error": False,
                }
            except Exception as e:
                return {
                    "tool_use_id": tool_use_id,
                    "type": "tool_result",
                    "content": f"Error executing {name}: {e}",
                    "is_error": True,
                }

        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout_seconds

            while True:
                timeout = max(0.5, deadline - loop.time())
                try:
                    line = await asyncio.wait_for(stdout_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break

                # Strip ANSI escape sequences for diagnostics.
                clean_line = self._strip_ansi(line).rstrip()
                stdout_lines.append(clean_line)

                event = self._try_parse_json(clean_line)
                if event is None:
                    continue

                event_type = event.get("type")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, dict):
                        content = [content]
                    for block in content:
                        if block.get("type") == "tool_use":
                            tool_result = _execute_tool(block)
                            await _send_message({
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": [tool_result],
                                },
                            })

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
                logs=logs or "No result event received before timeout",
                cost_info=None,
            )

        finally:
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

    def _ensure_path_in_worktree(self, path: Path, cwd: Path) -> Path:
        """Resolve ``path`` and ensure it lies inside ``cwd``.

        This prevents an agent from writing or editing files outside the
        workspace (e.g. via ``../../etc/passwd``).
        """
        resolved = path.resolve()
        resolved_cwd = cwd.resolve()
        if not resolved.is_relative_to(resolved_cwd):
            raise ToolExecutionError(f"Path must be inside workspace: {path}")
        return resolved

    def _tool_bash(self, input_data: dict[str, Any], cwd: Path) -> str:
        command = input_data.get("command", "")
        if not command:
            raise ToolExecutionError("Bash command is empty")
        args = shlex.split(command)
        if not args:
            raise ToolExecutionError("Bash command is empty")
        proc = subprocess.run(
            args,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            raise ToolExecutionError(f"Exit {proc.returncode}\n{stderr}")
        return stdout + (f"\n--- stderr ---\n{stderr}" if stderr else "")

    def _tool_read(self, input_data: dict[str, Any]) -> str:
        path = Path(input_data.get("file_path", ""))
        if not path.is_absolute():
            raise ToolExecutionError("Read requires absolute file_path")
        if not path.exists():
            raise ToolExecutionError(f"File not found: {path}")
        return path.read_text(encoding="utf-8")

    def _tool_write(self, input_data: dict[str, Any], cwd: Path) -> str:
        path = Path(input_data.get("file_path", ""))
        content = input_data.get("content", "")
        if not path.is_absolute():
            raise ToolExecutionError("Write requires absolute file_path")
        path = self._ensure_path_in_worktree(path, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {path}"

    def _tool_edit(self, input_data: dict[str, Any], cwd: Path) -> str:
        path = Path(input_data.get("file_path", ""))
        old_string = input_data.get("old_string", "")
        new_string = input_data.get("new_string", "")
        replace_all = input_data.get("replace_all", False)
        if not path.is_absolute():
            raise ToolExecutionError("Edit requires absolute file_path")
        path = self._ensure_path_in_worktree(path, cwd)
        if not path.exists():
            raise ToolExecutionError(f"File not found: {path}")
        content = path.read_text(encoding="utf-8")
        if replace_all:
            if old_string not in content:
                raise ToolExecutionError("old_string not found")
            content = content.replace(old_string, new_string)
        else:
            count = content.count(old_string)
            if count != 1:
                raise ToolExecutionError(
                    f"old_string appears {count} times (expected 1)"
                )
            content = content.replace(old_string, new_string, 1)
        path.write_text(content, encoding="utf-8")
        return f"Edited {path}"

    def _tool_glob(self, input_data: dict[str, Any], cwd: Path) -> str:
        import fnmatch

        pattern = input_data.get("pattern", "")
        search_path = Path(input_data.get("path") or cwd)
        if not search_path.is_absolute():
            search_path = cwd / search_path
        matches = []
        for root, _dirs, files in os.walk(search_path):
            for filename in files:
                full = Path(root) / filename
                rel = full.relative_to(search_path)
                if fnmatch.fnmatch(str(rel), pattern) or fnmatch.fnmatch(filename, pattern):
                    matches.append(str(full))
        return "\n".join(sorted(matches)) or "(no matches)"

    def _tool_grep(self, input_data: dict[str, Any], cwd: Path) -> str:
        import fnmatch
        import re

        pattern = input_data.get("pattern", "")
        search_path = Path(input_data.get("path") or cwd)
        if not search_path.is_absolute():
            search_path = cwd / search_path
        output_mode = input_data.get("output_mode", "content")
        glob_pattern = input_data.get("glob")
        context_before = input_data.get("-B", 0)
        context_after = input_data.get("-A", 0)
        show_line_numbers = input_data.get("-n", True)
        case_insensitive = input_data.get("-i", False)
        head_limit = input_data.get("head_limit")

        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)

        files = []
        if search_path.is_file():
            files = [search_path]
        else:
            for root, _dirs, filenames in os.walk(search_path):
                for filename in filenames:
                    full = Path(root) / filename
                    if glob_pattern and not fnmatch.fnmatch(filename, glob_pattern):
                        continue
                    files.append(full)

        results: list[str] = []
        seen_files: set[str] = set()
        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines, start=1):
                if regex.search(line):
                    if output_mode == "files_with_matches":
                        seen_files.add(str(file_path))
                        break
                    context_lines = []
                    if context_before:
                        for j in range(max(1, i - context_before), i):
                            context_lines.append(f"{j}: {lines[j-1]}" if show_line_numbers else lines[j-1])
                    main_line = f"{i}: {line}" if show_line_numbers else line
                    context_lines.append(main_line)
                    if context_after:
                        for j in range(i + 1, min(len(lines), i + context_after) + 1):
                            context_lines.append(f"{j}: {lines[j-1]}" if show_line_numbers else lines[j-1])
                    results.append(f"{file_path}:\n" + "\n".join(context_lines))
                    if head_limit and len(results) >= head_limit:
                        break
            if head_limit and len(results) >= head_limit:
                break

        if output_mode == "files_with_matches":
            return "\n".join(sorted(seen_files)) or "(no matches)"
        return "\n\n".join(results) or "(no matches)"

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
    ) -> AgentRuntimeResult:
        output = self.outputs.get(agent_type, {"success": True, "next_state_event": "complete"})
        return AgentRuntimeResult(
            success=output.get("success", True),
            output_json=output,
            output_files=[],
            logs="fake runtime",
            cost_info=None,
        )
