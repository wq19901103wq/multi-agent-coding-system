from pathlib import Path

import pytest

from mac_system.agents.runtime import ClaudeCodeRuntime, FakeRuntime


def test_build_claude_command():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
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
    assert "--workdir" in cmd
    assert "--add-dir" in cmd
    assert "/tmp/ref" in cmd


def test_parse_json_output_single():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    output = runtime._parse_json_output('{"success": true}')
    assert output == {"success": True}


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
