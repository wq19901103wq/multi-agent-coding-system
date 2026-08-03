import json
import asyncio
import os
import time
from pathlib import Path

import psutil
import pytest
from jsonschema import Draft7Validator

from mac_system.agents.runtime import (
    _DetachedToolProcessGuard,
    AgentRuntimeResult,
    ClaudeCodeRuntime,
    CodexRuntime,
    FakeRuntime,
    KimiCodeRuntime,
    PiRuntime,
    RuntimeRouter,
    normalize_optional_nulls,
    validate_agent_output,
)


def test_secret_question_cannot_store_a_choice_option_as_the_secret_value():
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "specs"
        / "schemas"
        / "agent_question_batch.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    question_schema = schema["properties"]["questions"]["items"]
    invalid = {
        "question_id": "q_secret",
        "decision_key": "runtime.secret.api_key",
        "kind": "blocking",
        "question": "请选择如何提供凭证",
        "reason": "运行入口需要外部凭证",
        "allow_free_text": False,
        "options": [{
            "key": "provide_secret",
            "label": "配置凭证",
            "impact": "继续执行",
            "recommended": True,
        }],
        "affected_trace_ids": [],
        "sensitivity": "secret",
        "secret_env_var": "SERVICE_API_KEY",
    }
    valid = {
        **invalid,
        "question": "请安全输入运行凭证",
        "allow_free_text": True,
        "options": [],
    }

    assert list(Draft7Validator(question_schema).iter_errors(invalid))
    assert not list(Draft7Validator(question_schema).iter_errors(valid))


def test_tester_fail_requires_structured_failure_evidence():
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "specs"
        / "schemas"
        / "tester_output.json"
    )
    contradictory = {
        "success": True,
        "next_state_event": "fail",
        "passed": False,
        "test_evidence": [{
            "id": "TE-001",
            "spec_id": "SPEC-001",
            "acceptance_criterion_ids": ["AC-001"],
            "type": "unit",
            "command_or_method": "pytest -q",
            "result": "pass",
        }],
        "failures": [],
    }
    evidenced_failure = {
        **contradictory,
        "failures": [{
            "id": "TF-001",
            "test": "test_contract",
            "error": "assertion failed",
            "blocking": True,
        }],
    }

    assert validate_agent_output(contradictory, schema_path)
    assert validate_agent_output(evidenced_failure, schema_path) is None


def test_optional_non_null_fields_are_omitted_before_schema_validation(tmp_path):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["required_value", "nested"],
        "properties": {
            "required_value": {"type": "string"},
            "optional_value": {"type": "string"},
            "nullable_value": {"type": ["string", "null"]},
            "nested": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"optional_child": {"type": "string"}},
            },
        },
    }
    schema_path = tmp_path / "output.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    normalized = normalize_optional_nulls(
        {
            "required_value": "ok",
            "optional_value": None,
            "nullable_value": None,
            "nested": {"optional_child": None},
        },
        schema_path,
    )

    assert normalized == {
        "required_value": "ok",
        "nullable_value": None,
        "nested": {},
    }
    assert validate_agent_output(normalized, schema_path) is None


def test_required_null_is_not_hidden_by_optional_normalization(tmp_path):
    schema_path = tmp_path / "output.json"
    schema_path.write_text(
        json.dumps({
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        }),
        encoding="utf-8",
    )

    normalized = normalize_optional_nulls({"value": None}, schema_path)

    assert normalized == {"value": None}
    assert "None is not of type 'string'" in validate_agent_output(
        normalized, schema_path
    )


def test_detached_guard_stops_charging_finished_process_groups():
    """Completed short tools consume only their actual lifetime."""
    import subprocess

    owner = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 2"], start_new_session=True
    )
    child = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 0.15"],
        start_new_session=True,
    )
    guard = _DetachedToolProcessGuard(
        owner.pid, max_age_seconds=5, max_total_active_seconds=0.5
    )
    try:
        # The synthetic child is not an owner descendant, so seed the same
        # process-group bookkeeping that discovery would create.
        group_id = os.getpgid(child.pid)
        now = time.monotonic()
        guard._first_seen[group_id] = now
        guard._charged_until[group_id] = now
        time.sleep(0.05)
        guard.poll()
        child.wait(timeout=1)
        charged_at_exit = guard.total_active_seconds
        guard.poll()
        time.sleep(0.55)
        guard.poll()

        assert group_id not in guard._charged_until
        assert guard.total_budget_exceeded is False
        assert guard.total_active_seconds - charged_at_exit < 0.1
    finally:
        if child.poll() is None:
            child.kill()
        owner.terminate()
        owner.wait(timeout=1)


def _write_turn_schema(path: Path) -> Path:
    path.write_text(json.dumps({
        "type": "object",
        "required": ["success", "next_state_event"],
        "properties": {
            "success": {"const": True},
            "next_state_event": {"enum": ["complete", "ask_user", "fail"]},
        },
    }), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_cancel_from_another_runtime_instance_stops_running_process(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    running_runtime = ClaudeCodeRuntime(fake_cli)
    cancelling_runtime = ClaudeCodeRuntime(fake_cli)

    execution = asyncio.create_task(
        running_runtime.execute(
            agent_type="planner",
            task_prompt="test",
            system_prompt="system",
            workspace=tmp_path,
            worktree=tmp_path,
            context_files=[],
            output_schema=tmp_path / "schema.json",
            allowed_tools=[],
            readonly_dirs=[],
            timeout_seconds=60,
            task_id="task_cross_runtime_cancel",
        )
    )
    for _ in range(100):
        if "task_cross_runtime_cancel" in running_runtime._processes:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("runtime subprocess did not start")

    cancelling_runtime.cancel("task_cross_runtime_cancel")
    result = await asyncio.wait_for(execution, timeout=3)

    assert result.success is False
    assert "task_cross_runtime_cancel" not in running_runtime._processes


@pytest.mark.asyncio
async def test_cancel_all_active_stops_processes_from_unretained_runtime(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    execution = asyncio.create_task(
        runtime.execute(
            agent_type="planner",
            task_prompt="test",
            system_prompt="system",
            workspace=tmp_path,
            worktree=tmp_path,
            context_files=[],
            output_schema=tmp_path / "schema.json",
            allowed_tools=[],
            readonly_dirs=[],
            timeout_seconds=60,
            task_id="task_shutdown_sweep",
        )
    )
    for _ in range(100):
        if "task_shutdown_sweep" in ClaudeCodeRuntime.active_task_ids():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("runtime subprocess did not start")

    assert ClaudeCodeRuntime.cancel_all_active() == ["task_shutdown_sweep"]
    result = await asyncio.wait_for(execution, timeout=3)

    assert result.success is False
    assert "task_shutdown_sweep" not in ClaudeCodeRuntime.active_task_ids()


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
    setting_sources_index = cmd.index("--setting-sources")
    assert cmd[setting_sources_index + 1] == ""
    assert "--model" in cmd
    assert "kimi-for-coding" in cmd
    assert "--add-dir" in cmd
    assert "/tmp/ref" in cmd


def test_stream_command_empty_allowed_tools_disables_tools():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    cmd = runtime._build_command(
        system_prompt_file=Path("/tmp/sys.md"),
        allowed_tools=[],
        readonly_dirs=[],
    )

    index = cmd.index("--tools")
    assert cmd[index + 1] == ""
    assert "--allowedTools" not in cmd
    assert "--bare" in cmd
    assert "--strict-mcp-config" in cmd
    setting_sources_index = cmd.index("--setting-sources")
    assert cmd[setting_sources_index + 1] == ""


def test_claude_runtime_bounds_bash_tool_duration():
    env = ClaudeCodeRuntime(Path("/usr/bin/claude"))._env()

    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "180000"
    assert env["BASH_MAX_TIMEOUT_MS"] == "300000"
    assert env["MAC_TOOL_TOTAL_TIMEOUT_MS"] == "900000"


@pytest.mark.asyncio
async def test_claude_runtime_reaps_detached_tool_process_group(tmp_path):
    """A CLI tool launched in its own session must not survive the Agent."""
    child_pid_file = tmp_path / "child.pid"
    fake_cli = tmp_path / "fake-claude-detached-child"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys, time\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'running'}]}}), flush=True)\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'{\\\"success\\\":true,\\\"next_state_event\\\":\\\"complete\\\"}'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    schema_path = _write_turn_schema(tmp_path / "schema.json")

    result = await runtime.execute(
        agent_type="planner",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=schema_path,
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
        runtime_env={"BASH_DEFAULT_TIMEOUT_MS": "50"},
    )

    assert result.success
    child_pid = int(child_pid_file.read_text())
    for _ in range(100):
        if not psutil.pid_exists(child_pid):
            break
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        os.killpg(child_pid, 9)
        pytest.fail("detached tool process survived the Agent turn")


@pytest.mark.asyncio
async def test_claude_runtime_enforces_cumulative_tool_process_budget(tmp_path):
    """The turn budget is generic and does not depend on command keywords."""
    child_pid_file = tmp_path / "child.pid"
    fake_cli = tmp_path / "fake-claude-cumulative-budget"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys, time\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'running'}]}}), flush=True)\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'{\\\"success\\\":true,\\\"next_state_event\\\":\\\"complete\\\"}'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    schema_path = _write_turn_schema(tmp_path / "schema.json")

    result = await runtime.execute(
        agent_type="tester",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=schema_path,
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
        runtime_env={
            "BASH_DEFAULT_TIMEOUT_MS": "5000",
            "MAC_TOOL_TOTAL_TIMEOUT_MS": "50",
        },
    )

    assert result.success is False
    assert "Cumulative subprocess budget exceeded" in result.logs
    child_pid = int(child_pid_file.read_text())
    for _ in range(100):
        if not psutil.pid_exists(child_pid):
            break
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        os.killpg(child_pid, 9)
        pytest.fail("detached tool process exceeded the cumulative turn budget")


def test_stream_command_with_tools_still_ignores_user_settings():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    cmd = runtime._build_command(
        system_prompt_file=Path("/tmp/sys.md"),
        allowed_tools=["Read", "Write"],
        readonly_dirs=[Path("/tmp/ref")],
    )

    assert "--allowedTools" in cmd
    assert "--tools" in cmd
    tools_index = cmd.index("--tools")
    assert cmd[tools_index + 1] == "Read,Write"
    assert "Read,Write" in cmd
    assert "--bare" in cmd
    setting_sources_index = cmd.index("--setting-sources")
    assert cmd[setting_sources_index + 1] == ""


def test_stream_command_uses_post_validation_not_cli_schema():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    cmd = runtime._build_command(
        system_prompt_file=Path("/tmp/sys.md"),
        allowed_tools=[],
        readonly_dirs=[],
    )
    # Some Anthropic-compatible providers hang before the first stream event
    # when stream-json and --json-schema are combined. Runtime validates the
    # parsed final object against the schema instead.
    assert "--json-schema" not in cmd


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


def test_parse_json_output_with_preface_and_markdown_fence():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    stdout = (
        "I have the result.\n```json\n"
        '{"success": true, "reply": "done"}'
        "\n```\n"
    )
    assert runtime._parse_json_output(stdout) == {"success": True, "reply": "done"}


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
    schema_path = _write_turn_schema(tmp_path / "schema.json")

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
        output_schema=schema_path,
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
    assert result.tool_calls == [{
        "name": "Write",
        "input": {
            "file_path": str(target),
            "content_length": 100000,
        },
    }]


@pytest.mark.asyncio
async def test_stream_tool_call_budget_terminates_runaway_agent(tmp_path):
    fake_cli = tmp_path / "fake-claude-budget"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        "for index in range(3):\n"
        "    event = {'type':'assistant','message':{'content':["
        "{'type':'tool_use','id':f'tool-{index}','name':'Read',"
        "'input':{'file_path':f'file-{index}.txt'}}]}}\n"
        "    print(json.dumps(event), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    runtime._scan_output_files = lambda _path: pytest.fail(
        "read-only runtime must not scan repository outputs"
    )

    result = await runtime.execute(
        agent_type="conversation_coordinator",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "schema.json",
        allowed_tools=["Read"],
        readonly_dirs=[tmp_path],
        timeout_seconds=60,
        max_tool_calls=2,
    )

    assert result.success is False
    assert "Tool-call budget exceeded" in result.logs
    assert len(result.tool_calls) == 3


@pytest.mark.asyncio
async def test_non_json_result_retains_read_only_fallback_text(tmp_path):
    fake_cli = tmp_path / "fake-claude-prose"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'result','is_error':False,"
        "'result':'这是已经完成的只读分析。'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    runtime._scan_output_files = lambda _path: pytest.fail(
        "read-only runtime must not scan repository outputs"
    )

    result = await runtime.execute(
        agent_type="conversation_coordinator",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "schema.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.raw_result == "这是已经完成的只读分析。"


@pytest.mark.asyncio
async def test_cumulative_assistant_snapshots_are_deduplicated(tmp_path):
    """The CLI re-emits completed blocks in later snapshots of the same
    message; each block must be forwarded exactly once."""
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "thinking = {'type':'thinking','text':'从测试报告来看，都跑通了'}\n"
        "tool = {'type':'tool_use','id':'tool-1','name':'Read','input':{'file_path':'/tmp/x'}}\n"
        "# snapshot 1: thinking only; snapshot 2: same thinking + tool_use\n"
        "print(json.dumps({'type':'assistant','message':{'content':[thinking]}}), flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[thinking, tool]}}), flush=True)\n"
        "print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool-1','content':'ok','is_error':False}]},'isReplay':True}), flush=True)\n"
        "# second turn: new thinking, then same thinking + final text\n"
        "thinking2 = {'type':'thinking','text':'汇总结果'}\n"
        "text = {'type':'text','text':'最终答复'}\n"
        "print(json.dumps({'type':'assistant','message':{'content':[thinking2]}}), flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[thinking2, text]}}), flush=True)\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'{\\\"success\\\":true,\\\"next_state_event\\\":\\\"complete\\\"}'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(fake_cli)
    schema_path = _write_turn_schema(tmp_path / "schema.json")

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
        output_schema=schema_path,
        allowed_tools=["Read"],
        readonly_dirs=[],
        timeout_seconds=5,
        progress_callback=on_progress,
    )

    assert result.success
    thinking_events = [content for kind, content in progress if kind == "thinking"]
    assert thinking_events == ["从测试报告来看，都跑通了", "汇总结果", "最终答复"]
    tool_events = [content for kind, content in progress if kind == "tool"]
    assert len(tool_events) == 1
    assert result.tool_calls == [
        {"name": "Read", "input": {"file_path": "/tmp/x"}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_result, expected_error",
    [
        ("not json", "not a JSON object"),
        ('{"success":true,"next_state_event":"invented"}', "schema violation"),
    ],
)
async def test_runtime_rejects_non_json_and_schema_violations(
    tmp_path, raw_result, expected_error
):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"print(json.dumps({{'type':'result','is_error':False,'result':{raw_result!r}}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="coder",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.success is False
    assert result.output_json is None
    assert expected_error in result.logs
    assert result.raw_result == raw_result


@pytest.mark.asyncio
async def test_runtime_uses_valid_canonical_artifact_when_stdout_is_prose(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'Output validated.'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    artifact = {"success": True, "next_state_event": "complete"}
    (mac_dir / "TESTER_OUTPUT.json").write_text(
        json.dumps(artifact), encoding="utf-8"
    )

    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="tester",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.output_json == artifact


@pytest.mark.asyncio
async def test_runtime_prefers_valid_canonical_artifact_over_invalid_stdout_schema(
    tmp_path,
):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'result','is_error':False,'result':"
        "json.dumps({'success': True, 'next_state_event': 'invented'})}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    artifact = {"success": True, "next_state_event": "complete"}
    (mac_dir / "CODER_OUTPUT.json").write_text(
        json.dumps(artifact), encoding="utf-8"
    )

    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="coder",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.output_json == artifact


@pytest.mark.asyncio
async def test_runtime_does_not_use_artifact_fallback_for_planner(tmp_path):
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'result','is_error':False,'result':'Output validated.'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    mac_dir = tmp_path / ".mac"
    mac_dir.mkdir()
    (mac_dir / "TESTER_OUTPUT.json").write_text(
        json.dumps({"success": True, "next_state_event": "complete"}),
        encoding="utf-8",
    )

    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="planner",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )

    assert result.success is False
    assert "not a JSON object" in result.logs


@pytest.mark.asyncio
async def test_runtime_detects_api_error_before_json_parse(tmp_path):
    """When Claude Code CLI reports ``is_error: true`` with an API error string
    in ``result`` (e.g. DeepSeek 402 Insufficient Balance), the runtime must
    surface the real cause instead of the misleading "result is not a JSON
    object" message."""
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({"
        "'type':'result','is_error':True,'api_error_status':402,"
        "'result':'API Error: 402 Insufficient Balance'"
        "}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="coder",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.success is False
    assert "API error: API Error: 402 Insufficient Balance" in result.logs
    assert result.failure == {
        "domain": "upstream_api",
        "status_code": 402,
        "retryable": False,
        "suggested_resolver": "coordinator",
    }
    # Must NOT contain the misleading JSON parse error.
    assert "not a JSON object" not in result.logs


@pytest.mark.asyncio
async def test_runtime_detects_api_error_without_status_field(tmp_path):
    """An is_error=true result without api_error_status should still surface
    the API error text rather than a JSON parse failure."""
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({"
        "'type':'result','is_error':True,"
        "'result':'API Error: network timeout'"
        "}), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    result = await ClaudeCodeRuntime(fake_cli).execute(
        agent_type="coder",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=_write_turn_schema(tmp_path / "schema.json"),
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.success is False
    assert "API error: API Error: network timeout" in result.logs
    assert result.failure == {
        "domain": "upstream_api",
        "status_code": None,
        "retryable": True,
        "suggested_resolver": "agent",
    }
    assert "not a JSON object" not in result.logs


def test_concise_agent_failure_recognizes_api_error():
    """``_concise_agent_failure`` should prefer the API error line over CLI
    metadata when surfacing a retry reason."""
    from mac_system.agents.orchestrator import _concise_agent_failure

    logs = (
        '{"type":"system","subtype":"init","session_id":"abc"}\n'
        '--- stderr ---\n'
        'API error: API Error: 402 Insufficient Balance\n'
    )
    assert "API Error: 402 Insufficient Balance" in _concise_agent_failure(logs)


def test_kimi_code_command_and_stream_parser():
    runtime = KimiCodeRuntime(Path("/usr/local/bin/kimi"))
    command = runtime.build_command("contract", Path("/tmp/project"), ["Read", "Bash"])
    assert command[:3] == ["/usr/local/bin/kimi", "--work-dir", "/tmp/project"]
    assert "--print" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    final, tools, cost, error = runtime.parse_events([
        json.dumps({
            "role": "assistant",
            "content": [
                {"type": "think", "text": "hidden"},
                {"type": "text", "text": '{"success":true,"next_state_event":"complete"}'},
            ],
        })
    ])
    assert json.loads(final) == {"success": True, "next_state_event": "complete"}
    assert tools == []
    assert cost is None
    assert error is None


def test_pi_command_is_ephemeral_context_isolated_and_capability_scoped():
    runtime = PiRuntime(
        Path("/usr/local/bin/pi"),
        "deepseek-v4-flash",
        provider="deepseek",
        thinking="high",
    )
    readonly = runtime.build_command("contract", Path("/tmp/project"), ["Read"])
    writable = runtime.build_command(
        "contract", Path("/tmp/project"), ["Read", "Edit", "Write", "Bash"]
    )
    no_tools = runtime.build_command("contract", Path("/tmp/project"), [])

    assert readonly[0] == "/usr/local/bin/pi"
    assert readonly[readonly.index("--mode") + 1] == "json"
    assert "--no-session" in readonly
    assert "--no-context-files" in readonly
    assert "--no-skills" in readonly
    assert "--no-extensions" in readonly
    assert readonly[readonly.index("--tools") + 1] == "read,grep,find,ls"
    assert writable[writable.index("--tools") + 1] == (
        "read,grep,find,ls,edit,write,bash"
    )
    assert "--no-tools" in no_tools
    assert readonly[readonly.index("--provider") + 1] == "deepseek"
    assert readonly[readonly.index("--model") + 1] == "deepseek-v4-flash"
    assert readonly[readonly.index("--thinking") + 1] == "high"


def test_pi_jsonl_parser_extracts_final_text_tools_and_aggregated_cost():
    runtime = PiRuntime(Path("pi"))
    final, tools, cost, error = runtime.parse_events([
        json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "toolCall", "name": "read",
                    "arguments": {"path": "README.md"},
                }],
                "usage": {
                    "input": 100, "output": 10, "cacheRead": 20,
                    "cacheWrite": 5, "totalTokens": 135,
                    "cost": {"total": 0.01},
                },
            },
        }),
        json.dumps({
            "type": "tool_execution_start",
            "toolName": "read",
            "args": {"path": "README.md"},
        }),
        json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": '{"success":true,"next_state_event":"complete"}',
                }],
                "usage": {
                    "input": 30, "output": 8, "cacheRead": 100,
                    "cacheWrite": 0, "totalTokens": 138,
                    "cost": {"total": 0.002},
                },
            },
        }),
    ])

    assert json.loads(final)["success"] is True
    assert tools == [{"name": "read", "input": {"path": "README.md"}}]
    assert cost == {
        "input_tokens": 130,
        "output_tokens": 18,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 5,
        "total_tokens": 273,
        "estimated_cost_usd": pytest.approx(0.012),
    }
    assert error is None


def test_codex_command_uses_local_validation_not_incompatible_output_schema():
    runtime = CodexRuntime(Path("/usr/local/bin/codex"))
    writable = runtime.build_command("contract", Path("/tmp/project"), ["Write"])
    readonly = runtime.build_command("contract", Path("/tmp/project"), ["Read"])
    shell_readonly = runtime.build_command(
        "contract", Path("/tmp/project"), ["Read", "Bash"]
    )
    validation = runtime.build_command(
        "contract", Path("/tmp/project"), ["Read", "Bash", "SandboxWrite"]
    )
    host_automation = runtime.build_command(
        "contract", Path("/tmp/project"),
        ["Read", "Bash", "SandboxWrite", "HostAutomation"],
    )
    assert writable[:2] == ["/usr/local/bin/codex", "exec"]
    assert writable[writable.index("-s") + 1] == "workspace-write"
    assert readonly[readonly.index("-s") + 1] == "read-only"
    assert shell_readonly[shell_readonly.index("-s") + 1] == "read-only"
    assert validation[validation.index("-s") + 1] == "workspace-write"
    assert host_automation[host_automation.index("-s") + 1] == "danger-full-access"
    assert writable[writable.index("--disable") + 1] == "code_mode_host"
    # MAC's schemas use valid Draft 7 constructs that Codex structured output
    # currently rejects (notably allOf), so MAC validates the final object.
    assert "--output-schema" not in writable


def test_codex_jsonl_parser_extracts_final_message_tools_and_usage():
    runtime = CodexRuntime(Path("codex"))
    final, tools, cost, error = runtime.parse_events([
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pytest -q"
        }}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message",
            "text": '{"success":true,"next_state_event":"complete"}',
        }}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 12, "output_tokens": 8
        }}),
    ])
    assert json.loads(final)["success"] is True
    assert tools[0]["name"] == "command_execution"
    assert cost == {"input_tokens": 12, "output_tokens": 8}
    assert error is None


@pytest.mark.asyncio
async def test_codex_runtime_reaps_detached_command_process_group(tmp_path):
    child_pid_file = tmp_path / "codex-child.pid"
    fake_codex = tmp_path / "fake-codex-detached-child"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\\\"success\\\":true,\\\"next_state_event\\\":\\\"complete\\\"}'}}), flush=True)\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    runtime = CodexRuntime(fake_codex)
    schema_path = _write_turn_schema(tmp_path / "schema.json")

    result = await runtime.execute(
        agent_type="planner",
        task_prompt="test",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=schema_path,
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
        runtime_env={"MAC_TOOL_PROCESS_TIMEOUT_SECONDS": "0.05"},
    )

    assert result.success
    child_pid = int(child_pid_file.read_text())
    for _ in range(100):
        if not psutil.pid_exists(child_pid):
            break
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        os.killpg(child_pid, 9)
        pytest.fail("detached Codex command survived the Agent turn")


def test_runtime_router_assigns_agents_to_independent_providers():
    providers = {
        "claude_code": FakeRuntime({}),
        "codex": FakeRuntime({}),
        "kimi_code": FakeRuntime({}),
    }
    router = RuntimeRouter(
        providers,
        {"architect": "codex", "coder": "codex", "tester": "kimi_code"},
    )
    assert router.provider_for("requirements_analyst") == "claude_code"
    assert router.provider_for("architect") == "codex"
    assert router.provider_for("coder") == "codex"
    assert router.provider_for("tester") == "kimi_code"


@pytest.mark.asyncio
async def test_runtime_router_honors_loop_supervisor_provider_override(tmp_path):
    class CountingRuntime(FakeRuntime):
        calls = 0

        async def execute(self, agent_type, *args, **kwargs):
            self.calls += 1
            return await super().execute(agent_type, *args, **kwargs)

    claude = CountingRuntime({"spec_writer": {"success": True}})
    codex = CountingRuntime({"spec_writer": {"success": True}})
    router = RuntimeRouter(
        {"claude_code": claude, "codex": codex},
        {"spec_writer": "claude_code"},
    )

    result = await router.execute(
        agent_type="spec_writer",
        preferred_provider="codex",
        task_prompt="task",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )

    assert result.success is True
    assert codex.calls == 1
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_runtime_router_cancellation_between_primary_and_fallback_is_latched(tmp_path):
    class CancellingPrimary(FakeRuntime):
        router = None

        async def execute(self, agent_type, *args, **kwargs):
            self.router.cancel(kwargs["task_id"])
            return AgentRuntimeResult(False, None, [], "primary stopped", None)

    class CountingFallback(FakeRuntime):
        calls = 0

        async def execute(self, agent_type, *args, **kwargs):
            self.calls += 1
            return await super().execute(agent_type, *args, **kwargs)

    primary = CancellingPrimary({})
    fallback = CountingFallback({})
    router = RuntimeRouter(
        {"claude_code": fallback, "codex": primary},
        {"tester": "codex"},
    )
    primary.router = router

    result = await router.execute(
        agent_type="tester",
        task_prompt="task",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
        task_id="task_cancel_gap",
    )

    assert result.success is False
    assert fallback.calls == 0
    assert "cancelled_before_fallback" in result.logs


@pytest.mark.asyncio
async def test_runtime_router_cross_instance_cancellation_does_not_fallback(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingPrimary(FakeRuntime):
        async def execute(self, agent_type, *args, **kwargs):
            started.set()
            await release.wait()
            return AgentRuntimeResult(False, None, [], "primary stopped", None)

    class CountingFallback(FakeRuntime):
        calls = 0

        async def execute(self, agent_type, *args, **kwargs):
            self.calls += 1
            return await super().execute(agent_type, *args, **kwargs)

    primary = WaitingPrimary({})
    fallback = CountingFallback({})
    executing_router = RuntimeRouter(
        {"claude_code": fallback, "codex": primary}, {"coder": "codex"}
    )
    handler_router = RuntimeRouter(
        {"claude_code": FakeRuntime({}), "codex": FakeRuntime({})},
        {"coder": "codex"},
    )
    execution = asyncio.create_task(executing_router.execute(
        agent_type="coder",
        task_prompt="task",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
        task_id="task_cross_router_cancel",
    ))
    await started.wait()
    handler_router.cancel("task_cross_router_cancel")
    release.set()
    result = await execution

    assert result.success is False
    assert fallback.calls == 0
    assert "cancelled_before_fallback" in result.logs


@pytest.mark.asyncio
async def test_runtime_router_really_reroutes_runtime_owned_blocker(tmp_path):
    primary = FakeRuntime({
        "architect": {
            "success": True,
            "next_state_event": "ask_user",
            "blockers": [{
                "blocking": True,
                "preferred_resolver": "runtime",
                "summary": "tool host unavailable",
            }],
        }
    })
    fallback = FakeRuntime({
        "architect": {"success": True, "next_state_event": "complete"}
    })
    router = RuntimeRouter(
        {"claude_code": fallback, "codex": primary},
        {"architect": "codex"},
    )
    result = await router.execute(
        agent_type="architect",
        task_prompt="task",
        system_prompt="system",
        workspace=tmp_path,
        worktree=tmp_path,
        context_files=[],
        output_schema=tmp_path / "unused.json",
        allowed_tools=[],
        readonly_dirs=[],
        timeout_seconds=5,
    )
    assert result.output_json["next_state_event"] == "complete"
    assert "runtime_provider=codex fallback=claude_code" in result.logs
    assert result.cost_info["runtime_attempts"] == [
        {
            "provider": "codex",
            "success": False,
            "failure_reason": "runtime_blocker",
        },
        {"provider": "claude_code", "success": True},
    ]
