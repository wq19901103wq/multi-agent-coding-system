#!/usr/bin/env python3
"""External watchdog for the MAC server.

launchd KeepAlive only covers jobs that remain loaded; a failed
``launchctl bootstrap`` (e.g. a stale disabled state) leaves the service
silently stopped, which KeepAlive cannot observe. This watchdog polls the
health endpoint from an independent LaunchAgent and runs start_server.sh when
the server is down, with cooldowns so a persistently broken service never
spins. Downtime and recovery are reported through the WeChat iLink bot.

Stdlib only: the watchdog must stay runnable even when the project venv or
dependencies are broken.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RECOVERY_COOLDOWN_S = 300
FAIL_ALERT_INTERVAL_S = 1800
START_TIMEOUT_S = 240
HEALTH_TIMEOUT_S = 5

WECHAT_BASE_URL = "https://ilinkai.weixin.qq.com/"
WECHAT_GETUPDATES_TIMEOUT_S = 10

# WeChat iLink is reachable directly; routing alerts through the local proxy
# made them fail exactly when the proxy (a common outage cause) was down.
_WECHAT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M:%S} {message}",
          flush=True)


def check_health(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT_S
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _wechat_headers(account: dict) -> dict:
    return {
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer " + account["botToken"],
        "X-WECHAT-UIN": base64.b64encode(
            str(secrets.randbits(32)).encode()).decode(),
    }


def _wechat_post(account: dict, endpoint: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        account["baseUrl"] + "ilink/bot/" + endpoint,
        data=json.dumps(body).encode(),
        headers={**_wechat_headers(account), "Content-Type": "application/json"},
        method="POST",
    )
    with _WECHAT_OPENER.open(request, timeout=timeout) as response:
        text = response.read().decode() or "{}"
    return json.loads(text)


def _harvest_context_tokens(account: dict, state_path: Path) -> None:
    """Refresh context tokens from inbound messages, mirroring the main app.

    iLink only issues context tokens inside inbound messages; without a fresh
    token sendmessage is rejected (ret=-2). Persist whatever arrives so later
    sends work again after the user messages the bot.
    """
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        result = _wechat_post(
            account, "getupdates", {"get_updates_buf": ""},
            WECHAT_GETUPDATES_TIMEOUT_S,
        )
    except (urllib.error.URLError, OSError, ValueError):
        return
    tokens = dict(state.get("contextTokens") or {})
    changed = False
    for msg in result.get("msgs") or []:
        token = msg.get("context_token")
        from_user = msg.get("from_user_id")
        if token and from_user:
            tokens[f"{account['botId']}:{from_user}"] = token
            changed = True
    if changed:
        state["contextTokens"] = tokens
        state_path.write_text(json.dumps(state, indent=2))


def send_wechat(root: Path, text: str) -> None:
    """Best-effort WeChat notification; never raises."""
    credentials = root / ".env"
    directory = Path("~/.mcp-wechat-server").expanduser()
    for line in credentials.read_text().splitlines():
        if line.startswith("WECHAT_CREDENTIALS_DIR="):
            directory = Path(line.split("=", 1)[1].strip().strip('"')
                             .strip("'")).expanduser()
    account = json.loads((directory / "account.json").read_text())
    state_path = directory / "state.json"
    base_url = (account.get("baseUrl") or WECHAT_BASE_URL).rstrip("/") + "/"
    if not base_url.startswith("https://ilinkai.weixin.qq.com"):
        raise ValueError("Unsupported WeChat bot API host")
    account["baseUrl"] = base_url
    for key in ("botToken", "botId", "userId"):
        if not account.get(key):
            raise ValueError("WeChat bot login is incomplete")
    _harvest_context_tokens(account, state_path)
    context = (json.loads(state_path.read_text()).get("contextTokens") or {}).get(
        f"{account['botId']}:{account['userId']}"
    )
    if not context:
        raise ValueError("WeChat conversation context is unavailable")
    result = _wechat_post(account, "sendmessage", {
        "msg": {
            "from_user_id": "",
            "to_user_id": account["userId"],
            "client_id": "mac-watchdog-" + secrets.token_hex(8),
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context,
        },
        "base_info": {"channel_version": "1.0.0"},
    }, timeout=20)
    codes = {k: result[k] for k in ("ret", "errcode") if k in result}
    if codes and any(type(v) is not int or v != 0 for v in codes.values()):
        raise ValueError(f"WeChat send rejected: {codes}")
    log(f"wechat notification delivered: {text.splitlines()[0]}")


def notify(root: Path, mode: str, text: str) -> None:
    if mode == "log":
        log(f"NOTIFY {text}")
        return
    try:
        send_wechat(root, text)
    except Exception as exc:  # notification must never break recovery
        log(f"wechat notification failed: {exc}")


def recover(root: Path, start_script: Path, port: int) -> bool:
    log(f"attempting recovery via {start_script}")
    try:
        result = subprocess.run(
            ["bash", str(start_script)], cwd=root, capture_output=True,
            text=True, timeout=START_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log(f"recovery timed out after {START_TIMEOUT_S}s")
        return False
    for line in (result.stdout + result.stderr).splitlines()[-45:]:
        log(f"start_server: {line}")
    if result.returncode != 0:
        log(f"recovery failed exit={result.returncode}")
        return False
    # The listener may need a moment to bind after a successful start.
    healthy = False
    for _ in range(30):
        if check_health(port):
            healthy = True
            break
        time.sleep(0.5)
    log(f"recovery finished healthy={healthy}")
    return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--start-script", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--notify", choices=["wechat", "log"], default="wechat")
    parser.add_argument("--recovery-cooldown", type=int, default=RECOVERY_COOLDOWN_S)
    parser.add_argument("--fail-alert-interval", type=int, default=FAIL_ALERT_INTERVAL_S)
    args = parser.parse_args()

    root = Path(args.root)
    state_file = Path(args.state_file) if args.state_file else root / ".mac/watchdog/state.json"
    start_script = Path(args.start_script) if args.start_script else root / "scripts/start_server.sh"
    env_text = ""
    try:
        env_text = (root / ".env").read_text()
    except OSError:
        pass
    port = args.port
    if port is None:
        port = 18080
        for line in env_text.splitlines():
            if line.startswith("MCS_PORT="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value.isdigit():
                    port = int(value)

    lock_file = state_file.with_suffix(".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another watchdog run holds the lock; skipping tick")
            return 0

        if check_health(port):
            state = load_state(state_file)
            if state.get("down_since"):
                log(f"server recovered after "
                    f"{int(time.time() - state['down_since'])}s downtime")
                if state.get("alerted_down"):
                    notify(root, args.notify, "MAC 服务已恢复 ✅\n看门狗自动拉起成功。")
                save_state(state_file, {})
            return 0

        now = time.time()
        state = load_state(state_file)
        if not state.get("down_since"):
            # New incident: arm the alert once; a resumed incident (down_since
            # already present) was already reported by an earlier tick.
            state["down_since"] = now
            state["attempts"] = 0
            state["alerted_down"] = True
            notify(root, args.notify,
                   "⚠️ MAC 服务宕机\n看门狗正在尝试自动拉起。")
        can_attempt = (now - state.get("last_attempt_at", 0)
                       >= args.recovery_cooldown)
        if can_attempt:
            state["last_attempt_at"] = now
            state["attempts"] = state.get("attempts", 0) + 1
            save_state(state_file, state)  # persist before the slow recovery
            if recover(root, start_script, port):
                save_state(state_file, {})
                if state.get("alerted_down"):
                    notify(root, args.notify, "MAC 服务已恢复 ✅\n看门狗自动拉起成功。")
                return 0
            state = load_state(state_file)
            if now - state.get("last_fail_alert_at", 0) >= args.fail_alert_interval:
                state["last_fail_alert_at"] = now
                notify(root, args.notify,
                       f"🚨 MAC 服务自动拉起失败（已尝试 {state.get('attempts', 1)} 次）\n"
                       "请人工检查：ssh 到本机查看 server_launchctl.log。")
        save_state(state_file, state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
