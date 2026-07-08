import asyncio
import json
import os
import re
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


class ClaudeCodeRuntime(AgentRuntime):
    def __init__(self, claude_bin: Path, anthropic_api_key: str | None = None):
        self.claude_bin = Path(claude_bin)
        self.anthropic_api_key = anthropic_api_key

    def build_command(
        self,
        task_prompt: str,
        system_prompt_file: Path,
        worktree: Path,
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
    ) -> list[str]:
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
            "--workdir",
            str(worktree),
        ]
        for d in readonly_dirs:
            cmd.extend(["--add-dir", str(d)])
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

        env = os.environ.copy()
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key

        cmd = self.build_command(
            task_prompt=task_prompt,
            system_prompt_file=system_prompt_file,
            worktree=worktree,
            output_schema=output_schema,
            allowed_tools=allowed_tools,
            readonly_dirs=readonly_dirs,
        )

        logs_lines: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                return AgentRuntimeResult(
                    success=False,
                    output_json=None,
                    output_files=[],
                    logs="Agent execution timed out",
                    cost_info=None,
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            logs_lines.append(stdout)
            logs_lines.append(stderr)

            if proc.returncode != 0:
                return AgentRuntimeResult(
                    success=False,
                    output_json=None,
                    output_files=[],
                    logs=f"\n".join(logs_lines),
                    cost_info=None,
                )

            output_json = self._parse_json_output(stdout)
            if output_json is None:
                return AgentRuntimeResult(
                    success=False,
                    output_json=None,
                    output_files=[],
                    logs=f"Failed to parse JSON output:\n{stdout}\n{stderr}",
                    cost_info=None,
                )

            cost_info = self._extract_cost_info(stderr)
            output_files = self._scan_output_files(worktree)

            return AgentRuntimeResult(
                success=output_json.get("success", True),
                output_json=output_json,
                output_files=output_files,
                logs=f"\n".join(logs_lines),
                cost_info=cost_info,
            )
        finally:
            try:
                system_prompt_file.unlink()
            except FileNotFoundError:
                pass

    def _parse_json_output(self, stdout: str) -> dict[str, Any] | None:
        # Try to parse the last JSON object in stdout
        text = stdout.strip()
        if not text:
            return None
        # If stdout contains multiple lines, try last non-empty line first
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for candidate in reversed(lines):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    def _extract_cost_info(self, stderr: str) -> dict[str, Any] | None:
        # Best-effort extraction of token/cost info from Claude Code stderr
        # Looks for patterns like: "Input tokens: 1234, Output tokens: 567"
        tokens_match = re.search(
            r"input tokens[:\s]+(\d+).*?output tokens[:\s]+(\d+)",
            stderr,
            re.IGNORECASE | re.DOTALL,
        )
        cost_match = re.search(
            r"(?:estimated cost|cost)[:\s]+\$?([\d.]+)",
            stderr,
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
        # Return files modified/created in worktree, excluding .git and .mac
        files: list[Path] = []
        for path in worktree.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(worktree)
            parts = rel.parts
            if parts[0] in (".git", ".mac"):
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
