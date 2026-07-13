import json
from pathlib import Path

import pytest

from mac_system.agents.runtime import ClaudeCodeRuntime, FakeRuntime


def test_build_claude_command():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"), claude_model="kimi-for-coding")
    cmd = runtime.build_command(
        task_prompt="hello",
        system_prompt_file=Path("/tmp/sys.md"),
        worktree=Path("/tmp/wt"),
        output_schema=Path("/tmp/schema.json"),
        allowed_tools=["Read", "Edit"],
        readonly_dirs=[Path("/tmp/ref")],
    )
    assert cmd[0] == "/usr/bin/claude"
    assert "-p" in cmd
    assert "hello" in cmd
    assert "--system-prompt-file" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--json-schema" in cmd
    assert "--allowedTools" in cmd
    assert "Read,Edit" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--bare" in cmd
    assert "--model" in cmd
    assert "kimi-for-coding" in cmd
    assert "--add-dir" in cmd
    assert "/tmp/ref" in cmd


def test_parse_json_output_single():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    output = runtime._parse_json_output('{"success": true}')
    assert output == {"success": True}


def test_parse_json_output_envelope():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    stdout = '{"type":"result","subtype":"success","is_error":false,"result":"{\\"success\\": true, \\"next_state_event\\": \\"complete\\"}"}'
    output = runtime._parse_json_output(stdout)
    assert output == {"success": True, "next_state_event": "complete"}


def test_parse_json_output_with_noise():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    stdout = "some log\n{\"success\": true, \"next_state_event\": \"complete\"}\n"
    output = runtime._parse_json_output(stdout)
    assert output == {"success": True, "next_state_event": "complete"}


def test_parse_json_output_invalid():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    assert runtime._parse_json_output("not json") is None


def test_extract_cost_info():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    stderr = "Input tokens: 1000\nOutput tokens: 500\nEstimated cost: $0.015"
    info = runtime._extract_cost_info(stderr)
    assert info["input_tokens"] == 1000
    assert info["output_tokens"] == 500
    assert info["total_tokens"] == 1500
    assert info["estimated_cost_usd"] == 0.015


def test_extract_cost_info_no_match():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    assert runtime._extract_cost_info("some logs") is None


def test_scan_output_files(tmp_path):
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.py").write_text("x")
    (tmp_path / ".mac").mkdir()
    (tmp_path / ".mac" / "TASK.json").write_text("{}")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("")

    files = runtime._scan_output_files(tmp_path)
    assert Path("src/file.py") in files
    assert Path(".mac/TASK.json") not in files
    assert Path(".git/config") not in files


@pytest.mark.asyncio
async def test_fake_runtime():
    runtime = FakeRuntime({
        "coder": {"success": True, "next_state_event": "complete"},
    })
    result = await runtime.execute(
        agent_type="coder",
        task_prompt="test",
        system_prompt="sys",
        workspace=Path("/tmp"),
        worktree=Path("/tmp"),
        context_files=[],
        output_schema=Path("/tmp/schema.json"),
        allowed_tools=["Read"],
        readonly_dirs=[],
        timeout_seconds=10,
    )
    assert result.success
    assert result.output_json["next_state_event"] == "complete"


@pytest.mark.asyncio
async def test_stream_tool_use_is_observed_but_not_executed(tmp_path):
    """Large tool events are read fully but never executed by Runtime."""
    target = tmp_path / "runtime_must_not_write.txt"
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"tool = {{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':'tool-1','name':'Write','input':{{'file_path':{str(target)!r},'content':'x' * 100000}}}}]}}}}\n"
        "print(json.dumps(tool), flush=True)\n"
        "print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool-1','content':'CLI handled it','is_error':False}]},'isReplay':True}), flush=True)\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'{\\\"success\\\":true,\\\"next_state_event\\\":\\\"complete\\\"}'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)

    progress: list[tuple[str, str]] = []

    async def on_progress(event_type: str, content: str) -> None:
        progress.append((event_type, content))

    result = await runtime.execute(
        agent_type="planner",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "schema.json",
        allowed_tools=["Write"],
        readonly_dirs=[],
        timeout_seconds=5,
        progress_callback=on_progress,
    )

    assert result.success
    assert result.output_json["next_state_event"] == "complete"
    assert not target.exists()
    tool_progress = json.loads(next(content for kind, content in progress if kind == "tool"))
    assert tool_progress["input"]["content_length"] == 100000
    assert "content" not in tool_progress["input"]
