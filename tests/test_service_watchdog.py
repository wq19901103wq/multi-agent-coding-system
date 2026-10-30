"""Exercise the external watchdog against a fake HTTP server and start script."""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import threading

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "scripts" / "service_watchdog.py"
WATCHDOG_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "com.mac-system.watchdog.plist.template"
)


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def free_port():
    probe = http.server.HTTPServer(("127.0.0.1", 0), HealthHandler)
    port = probe.server_address[1]
    probe.server_close()
    return port


FAKE_SERVER = '''
import http.server
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
'''


@pytest.fixture
def watchdog_env(tmp_path):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / ".env").write_text("MCS_PORT=18080\n")
    (root / "fake_server.py").write_text(FAKE_SERVER)
    script = shutil.copy(WATCHDOG, root / "scripts/service_watchdog.py")
    script.chmod(0o755)
    return root


def run_watchdog(root, port, *extra_args, extra_env=None):
    return subprocess.run(
        [sys.executable, str(root / "scripts/service_watchdog.py"),
         "--root", str(root), "--port", str(port), "--notify", "log",
         "--recovery-cooldown", "0", "--fail-alert-interval", "0",
         *extra_args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **(extra_env or {})},
    )


def test_healthy_server_is_untouched(watchdog_env, http_server):
    result = run_watchdog(watchdog_env, http_server)
    assert result.returncode == 0, result.stderr
    assert "NOTIFY" not in result.stdout
    assert not (watchdog_env / ".mac/watchdog/state.json").exists()


def test_down_server_is_recovered_and_alerts(watchdog_env, free_port):
    start_script = watchdog_env / "scripts/start_server.sh"
    # recover() runs the start script with cwd=root, so relative paths work.
    start_script.write_text(f"""#!/bin/bash
echo started >> actions
nohup {sys.executable} fake_server.py {free_port} >/dev/null 2>&1 &
""")
    start_script.chmod(0o755)
    actions = watchdog_env / "actions"
    result = run_watchdog(watchdog_env, free_port)
    assert result.returncode == 0, result.stderr + result.stdout
    assert actions.read_text().splitlines() == ["started"]
    assert "宕机" in result.stdout
    assert "已恢复" in result.stdout
    # State is cleared after recovery.
    assert json.loads((watchdog_env / ".mac/watchdog/state.json").read_text()) == {}


def test_failed_recovery_is_throttled_and_counted(watchdog_env, free_port):
    start_script = watchdog_env / "scripts/start_server.sh"
    start_script.write_text("#!/bin/bash\necho attempt >> actions\nexit 1\n")
    start_script.chmod(0o755)
    actions = watchdog_env / "actions"
    first = run_watchdog(watchdog_env, free_port)
    assert first.returncode == 1
    state = json.loads((watchdog_env / ".mac/watchdog/state.json").read_text())
    assert state["attempts"] == 1
    assert state["alerted_down"] is True
    assert actions.read_text().splitlines() == ["attempt"]
    # Second tick within the cooldown must not attempt again; the down alert
    # is also not repeated for the same incident.
    second = run_watchdog(watchdog_env, free_port, "--recovery-cooldown", "3600")
    assert second.returncode == 1
    assert actions.read_text().splitlines() == ["attempt"]
    assert second.stdout.count("NOTIFY") == 0
    state = json.loads((watchdog_env / ".mac/watchdog/state.json").read_text())
    assert state["attempts"] == 1


def test_failed_recovery_keeps_startup_diagnostics(watchdog_env, free_port):
    # start_server.sh prints the launchctl state before 30 log lines; keeping
    # only the last 10 lines hid the actual failure reason from the watchdog.
    start_script = watchdog_env / "scripts/start_server.sh"
    start_script.write_text(
        "#!/bin/bash\n"
        "echo 'Server did not become healthy within 120s'\n"
        "echo '--- launchctl state ---'\n"
        "echo 'state = spawn scheduled'\n"
        "for i in $(seq 1 30); do echo \"log line $i\"; done\n"
        "exit 1\n"
    )
    start_script.chmod(0o755)
    result = run_watchdog(watchdog_env, free_port)
    assert result.returncode == 1
    assert "did not become healthy" in result.stdout
    assert "state = spawn scheduled" in result.stdout


def test_watchdog_launch_agent_uses_interval_without_keepalive():
    # KeepAlive + StartInterval relaunches this one-shot script every 10 s
    # (launchd's throttle), producing thousands of idle runs per day.
    config = plistlib.loads(WATCHDOG_TEMPLATE.read_bytes())
    assert config["StartInterval"] == 60
    assert config["RunAtLoad"] is True
    assert "KeepAlive" not in config


def test_wechat_notifications_bypass_ambient_proxy(monkeypatch):
    # The watchdog's own alert once failed with connection refused because it
    # honored HTTPS_PROXY while the local proxy was down.
    spec = importlib.util.spec_from_file_location("service_watchdog", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ret": 0}'

    class FakeOpener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse()

    def fail_urlopen(*args, **kwargs):
        raise AssertionError("proxy-aware urlopen must not be used")

    monkeypatch.setattr(module, "_WECHAT_OPENER", FakeOpener())
    monkeypatch.setattr(module.urllib.request, "urlopen", fail_urlopen)
    account = {"baseUrl": "https://ilinkai.weixin.qq.com/", "botToken": "test"}
    assert module._wechat_post(account, "sendmessage", {"msg": {}}, 20) == {"ret": 0}
    assert calls == [("https://ilinkai.weixin.qq.com/ilink/bot/sendmessage", 20)]


def test_recovery_alert_only_after_down_alert(watchdog_env, free_port):
    # Without an earlier down alert there is no recovery message to send.
    state_file = watchdog_env / ".mac/watchdog/state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"down_since": 1, "attempts": 1,
                                      "alerted_down": False}))
    start_script = watchdog_env / "scripts/start_server.sh"
    start_script.write_text(f"""#!/bin/bash
nohup {sys.executable} fake_server.py {free_port} >/dev/null 2>&1 &
""")
    start_script.chmod(0o755)
    result = run_watchdog(watchdog_env, free_port)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "已恢复" not in result.stdout
    assert "宕机" not in result.stdout
