"""Exercise stop ordering without signalling a real process or launchd job."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest


@pytest.mark.parametrize("disabled_state", ["enabled", "disabled", "true"])
@pytest.mark.parametrize("venv_name", [".venv", ".worktrees/feat-core/.venv"])
def test_stop_waits_for_process_after_port_closes(tmp_path, disabled_state, venv_name):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts" / "stop_server.sh"
    script = scripts / source.name
    shutil.copyfile(source, script)
    shim = tmp_path / "commands.sh"
    shim.write_text(r'''
launchctl() {
    case "$1" in
        print) echo 'pid = 42';;
        print-disabled)
            echo "\"com.mac-system.dev\" => $TEST_DISABLED";;
        *) echo "$1" >> "$TEST_ROOT/actions";;
    esac
}
lsof() { [ -f "$TEST_ROOT/terminated" ] || echo 42; }
ps() { echo "$TEST_ROOT/$TEST_VENV_NAME/bin/uvicorn mac_system.api.main:app"; }
kill() {
    if [ "$1" = -TERM ]; then
        echo TERM >> "$TEST_ROOT/actions"
        touch "$TEST_ROOT/terminated"
        return 0
    fi
    [ "$waits" -lt 2 ]
}
waits=0
sleep() {
    waits=$((waits + 1))
    echo wait >> "$TEST_ROOT/actions"
}
''')
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, "BASH_ENV": str(shim), "TEST_ROOT": str(tmp_path),
             "TEST_DISABLED": disabled_state, "TEST_VENV_NAME": venv_name},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    actions = (tmp_path / "actions").read_text().splitlines()
    expected = ["TERM", "wait", "wait", "bootout"]
    if disabled_state == "enabled":
        expected = ["disable", *expected, "enable"]
    assert actions == expected


@pytest.fixture
def restart_entry(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts" / "restart_server_once.sh"
    script = scripts / source.name
    shutil.copyfile(source, script)
    (scripts / "stop_server.sh").write_text('''#!/bin/bash
echo stop >> actions
if [ "${WAIT_IN_STOP:-0}" = 1 ]; then
    while [ ! -f release ]; do sleep 0.01; done
fi
if [ "${CRASH_AFTER_CLAIM:-0}" = 1 ]; then
    kill -KILL "$PPID"
    exit 0
fi
exit "${STOP_EXIT:-0}"
''')
    (scripts / "start_server.sh").write_text('''#!/bin/bash
if [ "${1:-}" = --check ]; then exit "${PREFLIGHT_EXIT:-0}"; fi
echo start >> actions
if [ "${WAIT_FOR_RELEASE:-0}" = 1 ]; then
    while [ ! -f release ]; do sleep 0.01; done
fi
exit "${START_EXIT:-0}"
''')
    return tmp_path, script


@pytest.mark.parametrize("failure_env", [{}, {"STOP_EXIT": "1"}, {"START_EXIT": "1"}])
def test_restart_attempt_is_never_replayed(restart_entry, failure_env):
    root, script = restart_entry
    first = subprocess.run(["bash", str(script), "deploy-a"],
                           env={**os.environ, **failure_env}, capture_output=True, timeout=10)
    assert (first.returncode != 0) == bool(failure_env)
    actions = (root / "actions").read_text()
    # Includes the case where independent verification fails after a
    # successful restart: invoking the same operation still cannot stop it.
    second = subprocess.run(["bash", str(script), "deploy-a"], capture_output=True, timeout=10)
    assert second.returncode == 75
    assert (root / "actions").read_text() == actions
    assert not (root / ".mac/restart_operations/.active-lock").exists()


def test_restart_claim_survives_process_death(restart_entry):
    root, script = restart_entry
    first = subprocess.run(["bash", str(script), "crash"],
                           env={**os.environ, "CRASH_AFTER_CLAIM": "1"},
                           capture_output=True, timeout=10)
    assert first.returncode < 0
    assert (root / ".mac/restart_operations/crash").is_dir()
    for operation in ("crash", "different-operation"):
        retry = subprocess.run(["bash", str(script), operation], capture_output=True, timeout=10)
        assert retry.returncode == 75
    assert (root / "actions").read_text().splitlines() == ["stop"]


def test_parallel_deployments_cannot_both_restart(restart_entry):
    root, script = restart_entry
    first = subprocess.Popen(["bash", str(script), "first"],
                             env={**os.environ, "WAIT_FOR_RELEASE": "1"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (root / "actions").exists() or "start" not in (root / "actions").read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        other = subprocess.run(["bash", str(script), "second"], capture_output=True, timeout=5)
        assert other.returncode == 75
        assert (root / "actions").read_text().splitlines() == ["stop", "start"]
    finally:
        (root / "release").touch()
        first.communicate(timeout=5)
    assert first.returncode == 0


@pytest.mark.parametrize("operation", ["../escape", "", "/absolute", "a b"])
def test_restart_rejects_invalid_operation_id(restart_entry, operation):
    root, script = restart_entry
    result = subprocess.run(["bash", str(script), operation], capture_output=True, timeout=5)
    assert result.returncode == 64
    assert not (root / "actions").exists()


@pytest.mark.parametrize("name", ["deploy_codex_startup_fix_watchdog.sh", "restart_clear_pool_watchdog.sh"])
def test_expired_watchdogs_have_no_side_effects(tmp_path, name):
    source = Path(__file__).resolve().parents[1] / "scripts" / name
    result = subprocess.run(["bash", str(source)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert "Retired" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_failed_preflight_never_stops_server(restart_entry):
    root, script = restart_entry
    result = subprocess.run(["bash", str(script), "bad-config"],
                            env={**os.environ, "PREFLIGHT_EXIT": "1"},
                            capture_output=True, timeout=5)
    assert result.returncode == 1
    assert not (root / "actions").exists()


@pytest.mark.parametrize("signum,exit_code", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_signal_during_stop_keeps_lock_until_child_exits(restart_entry, signum, exit_code):
    root, script = restart_entry
    process = subprocess.Popen(["bash", str(script), "signal"],
                               env={**os.environ, "WAIT_IN_STOP": "1"},
                               start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (root / "actions").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.send_signal(signum)
        blocked = subprocess.run(["bash", str(script), "other"],
                                 capture_output=True, timeout=5)
        assert blocked.returncode == 75
        assert process.poll() is None
    finally:
        (root / "release").touch()
        process.communicate(timeout=5)
    assert process.returncode == exit_code
    assert (root / "actions").read_text().splitlines() == ["stop"]
    assert f"exit={exit_code}" in (root / ".mac/restart_operations/signal/status").read_text()
    assert not (root / ".mac/restart_operations/.active-lock").exists()


@pytest.fixture
def start_entry(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    repo_scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("start_server.sh", "com.mac-system.dev.plist.template"):
        shutil.copyfile(repo_scripts / name, scripts / name)
    startup = scripts / "start_server.sh"
    startup.write_text(startup.read_text().replace(
        'LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"',
        'LAUNCH_AGENT_DIR="$TEST_ROOT/Library/LaunchAgents"',
    ))
    runtime = tmp_path / ".worktrees/feat-core/.venv/bin"
    runtime.mkdir(parents=True)
    (runtime / "activate").touch()
    for executable in (runtime / "uvicorn", scripts / "run_server.sh"):
        executable.write_text("#!/bin/bash\nexit 0\n")
        executable.chmod(0o755)
    (tmp_path / ".env").write_text("MCS_PORT=18080\n")
    (tmp_path / "logging.yaml").touch()
    shim = tmp_path / "commands.sh"
    shim.write_text(r'''
launchctl() {
    if [ "$1" = print ]; then
        [ "$TEST_MODE" != unmanaged ] || return 1
        echo 'pid = 43'
    else
        echo "launchctl $*" >> "$TEST_ROOT/actions"
    fi
}
lsof() {
    if [ "$TEST_MODE" = unmanaged ]; then echo 42; fi
    if [ -f "$TEST_ROOT/ready" ]; then echo 43; fi
}
ps() { echo "$TEST_ROOT/.worktrees/feat-core/.venv/bin/uvicorn mac_system.api.main:app"; }
kill() {
    if [ "$1" != -0 ]; then echo "kill $*" >> "$TEST_ROOT/actions"; fi
    return 0
}
sleep() { SECONDS=$((SECONDS + 121)); }
curl() {
    echo curl >> "$TEST_ROOT/actions"
    echo "$*" >> "$TEST_ROOT/health_requests"
    if [ "$TEST_MODE" = timeout ]; then
        SECONDS=$((SECONDS + 121))
        return 1
    fi
    touch "$TEST_ROOT/ready"
}
''')
    # Isolate launch-agent writes from the real user's Library.
    env = {**os.environ, "BASH_ENV": str(shim),
           "TEST_ROOT": str(tmp_path), "TEST_MODE": "starting"}
    return tmp_path, scripts / "start_server.sh", env


@pytest.mark.parametrize("missing", [".env", "logging.yaml", ".worktrees/feat-core/.venv/bin/uvicorn"])
def test_start_preflight_fails_without_service_actions(start_entry, missing):
    root, script, env = start_entry
    (root / missing).unlink()
    result = subprocess.run(["bash", str(script), "--check"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (root / "actions").exists()
    assert not (root / "Library").exists()


def test_start_preflight_is_readonly(start_entry):
    root, script, env = start_entry
    result = subprocess.run(["bash", str(script), "--check"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not (root / "actions").exists()
    assert not (root / "Library").exists()


@pytest.mark.parametrize("configuration", ["MCS_PORT=not-a-port\n", "MCS_PORT=65536\n", "MCS_PORT=0\n", "MCS_PORT='unterminated\n"])
def test_real_restart_preflight_rejects_bad_config_before_stop(start_entry, configuration):
    root, _, env = start_entry
    (root / ".env").write_text(configuration)
    source = Path(__file__).resolve().parents[1] / "scripts" / "restart_server_once.sh"
    entry = root / "scripts/restart_server_once.sh"
    shutil.copyfile(source, entry)
    (root / "scripts/stop_server.sh").write_text("#!/bin/bash\necho stop >> actions\n")
    result = subprocess.run(["bash", str(entry), "invalid-config"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (root / "actions").exists()


def test_start_refuses_takeover_of_surviving_process(start_entry):
    root, script, env = start_entry
    result = subprocess.run(["bash", str(script)], env={**env, "TEST_MODE": "unmanaged"},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "has not exited" in result.stderr
    assert (root / "actions").read_text().splitlines() == ["kill 42"]
    assert not (root / "Library").exists()


def test_start_waits_without_killing_existing_startup(start_entry):
    root, script, env = start_entry
    result = subprocess.run(["bash", str(script)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    actions = (root / "actions").read_text()
    assert "kickstart" in actions
    assert " -k " not in actions
    assert "kill " not in actions
    assert "/health" in (root / "health_requests").read_text()
    assert "/admin/projects" not in (root / "health_requests").read_text()


def test_start_health_timeout_uses_elapsed_time(start_entry):
    root, script, env = start_entry
    result = subprocess.run(["bash", str(script)], env={**env, "TEST_MODE": "timeout"},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "within 120s" in result.stderr
    assert (root / "actions").read_text().splitlines().count("curl") == 1


def test_start_supports_standard_venv(start_entry):
    root, script, env = start_entry
    (root / ".worktrees/feat-core/.venv").rename(root / ".venv")
    result = subprocess.run(["bash", str(script), "--check"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not (root / "actions").exists()
    assert not (root / "Library").exists()


def test_start_does_not_hide_broken_standard_venv_with_legacy(start_entry):
    root, script, env = start_entry
    (root / ".venv").mkdir()
    result = subprocess.run(["bash", str(script), "--check"], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not (root / "actions").exists()
    assert not (root / "Library").exists()


@pytest.mark.parametrize("venv_name", [".venv", ".worktrees/feat-core/.venv"])
@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_run_server_uses_selected_venv_and_host(tmp_path, venv_name, host):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_server.sh"
    shutil.copyfile(source, scripts / source.name)
    runtime = tmp_path / venv_name / "bin"
    runtime.mkdir(parents=True)
    (runtime / "activate").touch()
    executable = runtime / "uvicorn"
    executable.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    configuration = "MCS_PORT=18081\n" + (f"MCS_HOST={host}\n" if host else "")
    (tmp_path / ".env").write_text(configuration)
    env = {key: value for key, value in os.environ.items()
           if key not in {"BASH_ENV", "MCS_HOST", "MCS_PORT"}}
    result = subprocess.run(["bash", str(scripts / source.name)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    arguments = result.stdout.splitlines()
    assert arguments[arguments.index("--host") + 1] == (host or "127.0.0.1")
    assert arguments[arguments.index("--port") + 1] == "18081"
    assert "--reload" not in arguments
