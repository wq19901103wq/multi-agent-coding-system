import json
from pathlib import Path

import pytest

from mac_system.agents.runtime import (
    ClaudeCodeRuntime,
    CodexRuntime,
    FakeRuntime,
    KimiCodeRuntime,
    PiRuntime,
    RuntimeRouter,
    normalize_optional_nulls,
)


def test_optional_nulls_are_omitted_without_changing_required_values():
    schema = {
        "type": "object",
        "required": ["required"],
        "properties": {
            "required": {"type": ["string", "null"]},
            "optional": {"type": "string"},
        },
    }
    assert normalize_optional_nulls(
        {"required": None, "optional": None}, schema
    ) == {"required": None}


def test_claude_command_is_isolated_and_capability_scoped():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"), claude_model="model")
    command = runtime.build_command(
        task_prompt="objective",
        system_prompt_file=Path("/tmp/system.md"),
        worktree=Path("/tmp/workspace"),
        output_schema=Path("/tmp/result.json"),
        allowed_tools=["Read", "Edit"],
        readonly_dirs=[Path("/tmp/reference")],
    )
    assert command[0] == "/usr/bin/claude"
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--allowedTools") + 1] == "Read,Edit"
    assert "/tmp/reference" in command


def test_claude_empty_tools_really_disables_tools():
    command = ClaudeCodeRuntime(Path("claude"))._build_command(
        system_prompt_file=Path("/tmp/system.md"),
        allowed_tools=[],
        readonly_dirs=[],
    )
    assert command[command.index("--tools") + 1] == ""
    assert "--allowedTools" not in command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"ok": true}', {"ok": True}),
        ('noise\n{"ok": true}\n', {"ok": True}),
        ('```json\n{"ok": true}\n```', {"ok": True}),
        ("not json", None),
    ],
)
def test_claude_result_parser(text, expected):
    assert ClaudeCodeRuntime(Path("claude"))._parse_json_output(text) == expected


def test_kimi_command_and_event_parser():
    runtime = KimiCodeRuntime(Path("/usr/local/bin/kimi"))
    command = runtime.build_command("objective", Path("/tmp/project"), ["Read", "Bash"])
    assert command[:3] == ["/usr/local/bin/kimi", "--work-dir", "/tmp/project"]
    assert command[command.index("--output-format") + 1] == "stream-json"
    final, tools, cost, error = runtime.parse_events([
        json.dumps({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    ])
    assert final == "done"
    assert tools == []
    assert cost is None
    assert error is None


def test_pi_is_ephemeral_and_capability_scoped():
    runtime = PiRuntime(
        Path("/usr/local/bin/pi"),
        "deepseek-v4-flash",
        provider="deepseek",
        thinking="high",
    )
    readonly = runtime.build_command("objective", Path("/tmp/project"), ["Read"])
    writable = runtime.build_command(
        "objective", Path("/tmp/project"), ["Read", "Edit", "Write", "Bash"]
    )
    assert "--no-session" in readonly
    assert "--no-context-files" in readonly
    assert readonly[readonly.index("--tools") + 1] == "read,grep,find,ls"
    assert writable[writable.index("--tools") + 1] == "read,grep,find,ls,edit,write,bash"


def test_pi_parser_aggregates_usage():
    runtime = PiRuntime(Path("pi"))
    final, tools, cost, error = runtime.parse_events([
        json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "usage": {
                    "input": 10,
                    "output": 2,
                    "cacheRead": 3,
                    "cacheWrite": 1,
                    "totalTokens": 16,
                    "cost": {"total": 0.001},
                },
            },
        })
    ])
    assert final == "done"
    assert tools == []
    assert cost["total_tokens"] == 16
    assert cost["estimated_cost_usd"] == pytest.approx(0.001)
    assert error is None


def test_codex_cli_permissions_are_derived_from_capabilities():
    runtime = CodexRuntime(Path("/usr/local/bin/codex"))
    readonly = runtime.build_command("objective", Path("/tmp/project"), ["Read"])
    writable = runtime.build_command("objective", Path("/tmp/project"), ["Write"])
    host = runtime.build_command(
        "objective", Path("/tmp/project"), ["Read", "HostAutomation"]
    )
    assert readonly[readonly.index("-s") + 1] == "read-only"
    assert writable[writable.index("-s") + 1] == "workspace-write"
    assert host[host.index("-s") + 1] == "danger-full-access"
    assert "--output-schema" not in writable


def test_codex_jsonl_parser_extracts_final_message_and_usage():
    runtime = CodexRuntime(Path("codex"))
    final, tools, cost, error = runtime.parse_events([
        json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "pytest -q"},
        }),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        }),
    ])
    assert final == "done"
    assert tools[0]["name"] == "command_execution"
    assert cost == {"input_tokens": 12, "output_tokens": 8}
    assert error is None


@pytest.mark.asyncio
async def test_runtime_router_honors_explicit_provider(tmp_path):
    class CountingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({"external": {"success": True}})
            self.calls = 0

        async def execute(self, agent_type, *args, **kwargs):
            self.calls += 1
            return await super().execute(agent_type, *args, **kwargs)

    claude = CountingRuntime()
    kimi = CountingRuntime()
    router = RuntimeRouter(
        {"claude_code": claude, "kimi_code": kimi},
        {"external": "claude_code"},
    )
    result = await router.execute(
        agent_type="external",
        preferred_provider="kimi_code",
        task_prompt="objective",
        system_prompt="",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.success
    assert kimi.calls == 1
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_fake_runtime_is_deterministic(tmp_path):
    runtime = FakeRuntime({"external": {"success": True, "reply": "done"}})
    result = await runtime.execute(
        agent_type="external",
        task_prompt="objective",
        system_prompt="",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.success
    assert result.output_json["reply"] == "done"
