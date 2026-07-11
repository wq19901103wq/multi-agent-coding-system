#!/usr/bin/env python3
"""Feishu end-to-end driver for MAC System.

This script drives the MAC bot through real Feishu conversations using a
second "simulator" bot.  Card button clicks are injected via the test-only
``/test/inject_card_action`` endpoint, so the simulator bot does not need to
emulate an interactive Feishu client.

Environment variables (or CLI flags):
  FEISHU_SIMULATOR_APP_ID      simulator bot app_id
  FEISHU_SIMULATOR_APP_SECRET  simulator bot app_secret
  FEISHU_SIMULATOR_CHAT_ID     optional test group chat_id
  FEISHU_APP_ID / FEISHU_APP_SECRET  MAC bot credentials (from .env)

Examples:
  python scripts/feishu_e2e.py --list
  python scripts/feishu_e2e.py --scenario p2p_create
  python scripts/feishu_e2e.py --scenario group_create --chat-id oc_xxx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

# Allow running from the repo root without installing the package.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from mac_system.config import get_settings
from mac_system.feishu.bridge import FeishuBridge


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


ID_RE = re.compile(r"\b(proj_[a-zA-Z0-9_]+)\b")
APPROVAL_RE = re.compile(r"\b(appr_[a-zA-Z0-9_]+)\b")


def extract_project_id(text: str) -> str | None:
    m = ID_RE.search(text)
    return m.group(1) if m else None


def extract_approval_id(text: str) -> str | None:
    m = APPROVAL_RE.search(text)
    return m.group(1) if m else None


def _extract_message_text(content: dict[str, Any]) -> str:
    """Extract searchable text from text or interactive card content."""
    if "text" in content:
        return content["text"]
    parts: list[str] = []
    _TEXT_KEYS = {"title", "text", "content"}

    def collect(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _TEXT_KEYS and isinstance(v, str):
                    parts.append(v)
                else:
                    collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)

    collect(content)
    return "\n".join(parts)


@dataclass
class FeishuMessage:
    message_id: str
    sender_id: str
    text: str


class FeishuSimulator:
    """A second bot that talks to the MAC bot in Feishu."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        mac_bot_open_id: str,
        chat_id: str | None = None,
        mac_app_id: str | None = None,
    ):
        self.bridge = FeishuBridge(app_id, app_secret)
        self.mac_bot_open_id = mac_bot_open_id
        self.mac_app_id = mac_app_id
        self.chat_id = chat_id

    async def close(self) -> None:
        await self.bridge.close()

    async def send_text(self, text: str, mention: bool = True) -> str:
        """Send a text message. In a group chat the MAC bot is @mentioned."""
        if self.chat_id:
            receive_id = self.chat_id
            receive_id_type = "chat_id"
            # Use Feishu <at> tag to mention the bot, matching feishu-connect.
            display_text = (
                f'<at user_id="{self.mac_bot_open_id}">@MAC</at> {text}'
                if mention
                else text
            )
            content: dict[str, Any] = {"text": display_text}
        else:
            receive_id = self.mac_bot_open_id
            receive_id_type = "open_id"
            content = {"text": text}

        data = await self.bridge._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_data={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        return data["data"]["message_id"]

    async def get_messages(self) -> list[FeishuMessage]:
        """Return recent messages in the chat (best-effort Feishu API)."""
        container_id = self.chat_id or self.mac_bot_open_id
        container_id_type = "chat" if self.chat_id else "user"
        try:
            data = await self.bridge._request(
                "GET",
                "/open-apis/im/v1/messages",
                params={
                    "container_id_type": container_id_type,
                    "container_id": container_id,
                    "page_size": 20,
                    "sort_type": "ByCreateTimeDesc",
                },
            )
        except Exception as exc:
            eprint(f"  [warn] failed to fetch messages: {exc}")
            return []

        items = data.get("data", {}).get("items") or []
        messages: list[FeishuMessage] = []
        for item in items:
            sender = item.get("sender", {}).get("sender_id", {})
            body = item.get("body", {})
            content_text = ""
            try:
                content = json.loads(body.get("content", "{}"))
                content_text = _extract_message_text(content)
            except json.JSONDecodeError:
                pass
            sender_id = (
                item.get("sender", {}).get("sender_id", {}).get("open_id", "")
                or item.get("sender", {}).get("id", "")
            )
            messages.append(
                FeishuMessage(
                    message_id=item.get("message_id", ""),
                    sender_id=sender_id,
                    text=content_text,
                )
            )
        return messages

    async def wait_for_reply(
        self,
        pattern: str | None = None,
        after_message_id: str | None = None,
        timeout: float = 60.0,
        interval: float = 2.0,
    ) -> FeishuMessage | None:
        """Poll chat history until the MAC bot replies (optionally matching).

        Messages are returned newest-first; we stop walking once we reach the
        message identified by ``after_message_id`` to avoid matching stale
        replies from earlier conversations.
        """
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        while time.monotonic() < deadline:
            for msg in await self.get_messages():
                if after_message_id and msg.message_id == after_message_id:
                    break
                if msg.sender_id not in (
                    self.mac_bot_open_id,
                    self.mac_app_id,
                ):
                    continue
                if msg.message_id in seen:
                    continue
                seen.add(msg.message_id)
                if pattern is None or re.search(pattern, msg.text):
                    return msg
            await asyncio.sleep(interval)
        return None


class MacApiClient:
    """Client for the MAC System REST API."""

    def __init__(self, base_url: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        # Ticks can run the orchestrator for a while; allow up to 5 minutes.
        self._client = httpx.AsyncClient(timeout=300.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_status(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"current_state": "DRYRUN", "pending_approval": None}
        resp = await self._client.get(f"{self.base_url}/projects/{project_id}/status")
        resp.raise_for_status()
        return resp.json()

    async def create_project(self, **fields: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"id": f"proj_dryrun_{fields.get('name', 'x')}"}
        resp = await self._client.post(f"{self.base_url}/projects", json=fields)
        resp.raise_for_status()
        return resp.json()

    async def submit_message(self, project_id: str, content: str) -> dict[str, Any]:
        if self.dry_run:
            return {"reply": "[dry-run reply]", "state": "DRYRUN"}
        resp = await self._client.post(
            f"{self.base_url}/projects/{project_id}/messages",
            json={"content": content},
        )
        resp.raise_for_status()
        return resp.json()

    async def inject_card_action(self, **fields: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        resp = await self._client.post(
            f"{self.base_url}/test/inject_card_action",
            json=fields,
        )
        resp.raise_for_status()
        return resp.json()

    async def clear_pending_intent(self, open_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        resp = await self._client.post(
            f"{self.base_url}/test/clear_pending_intent/{open_id}",
        )
        # Ignore 404 if the endpoint is not present; best-effort cleanup.
        if resp.status_code == 404:
            return {"status": "skipped"}
        resp.raise_for_status()
        return resp.json()

    async def tick(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        resp = await self._client.post(f"{self.base_url}/test/tick/{project_id}")
        resp.raise_for_status()
        return resp.json()


@dataclass
class ScenarioContext:
    sim: FeishuSimulator | None
    mac: MacApiClient
    args: argparse.Namespace
    dry_run: bool


async def tick_until_state(
    ctx: ScenarioContext,
    project_id: str,
    state: str,
    max_ticks: int = 20,
    timeout: float = 600.0,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """Repeatedly tick the project until it reaches *state* or times out."""
    if ctx.dry_run:
        eprint(f"  [dry-run] tick until {state}")
        return {"current_state": state, "pending_approval": None}

    deadline = time.monotonic() + timeout
    for _ in range(max_ticks):
        if time.monotonic() >= deadline:
            return None
        status = await ctx.mac.get_status(project_id)
        if status["current_state"] == state:
            return status
        eprint(f"  [tick] {status['current_state']} -> ...")
        try:
            await ctx.mac.tick(project_id)
        except httpx.HTTPStatusError as exc:
            eprint(f"  [warn] tick HTTP {exc.response.status_code}: {exc.response.text[:200]}")
            await asyncio.sleep(interval)
            continue
        # The tick endpoint returns after the orchestrator processes one state.
        # Poll briefly for the state to update before looping around.
        poll_deadline = time.monotonic() + 10.0
        while time.monotonic() < poll_deadline:
            await asyncio.sleep(interval)
            status = await ctx.mac.get_status(project_id)
            if status["current_state"] == state:
                eprint(f"  [tick] -> {status['current_state']}")
                return status
    return None


async def wait_for_state(
    ctx: ScenarioContext,
    project_id: str,
    state: str,
    timeout: float = 300.0,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """Poll MAC API until project reaches the expected state."""
    if ctx.dry_run:
        eprint(f"  [dry-run] wait for state {state}")
        return {
            "current_state": state,
            "pending_approval": {"approval_id": "appr_dryrun", "type": "confirmation"},
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        if status["current_state"] == state:
            return status
        await asyncio.sleep(interval)
    return None


async def wait_for_pending_approval(
    ctx: ScenarioContext,
    project_id: str,
    timeout: float = 120.0,
    interval: float = 2.0,
) -> dict[str, Any] | None:
    """Poll until the project has a non-null pending approval."""
    if ctx.dry_run:
        return {
            "current_state": "DRYRUN",
            "pending_approval": {"approval_id": "appr_dryrun", "type": "confirmation"},
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        if status.get("pending_approval"):
            return status
        await asyncio.sleep(interval)
    return None


async def tick_until_pending(
    ctx: ScenarioContext,
    project_id: str,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Repeatedly tick until a pending approval appears, or time out."""
    if ctx.dry_run:
        return {
            "current_state": "DRYRUN",
            "pending_approval": {"approval_id": "appr_dryrun", "type": "confirmation"},
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        if status.get("pending_approval"):
            return status
        try:
            await ctx.mac.tick(project_id)
        except httpx.HTTPStatusError:
            await asyncio.sleep(2.0)
            continue
        # Poll briefly for the tick to take effect.
        poll_deadline = time.monotonic() + 15.0
        while time.monotonic() < poll_deadline:
            await asyncio.sleep(2.0)
            status = await ctx.mac.get_status(project_id)
            if status.get("pending_approval"):
                return status
    raise RuntimeError(f"Project {project_id} did not create a pending approval within {timeout}s")


def _project_create_kwargs(ctx: ScenarioContext, **fields: Any) -> dict[str, Any]:
    """Build kwargs for MAC API create_project, injecting Feishu context."""
    if not ctx.dry_run:
        fields.setdefault("creator_open_id", ctx.args.open_id)
        if ctx.args.chat_id:
            fields["feishu_chat_id"] = ctx.args.chat_id
            fields["feishu_chat_type"] = "group"
    return fields


async def inject_action(
    ctx: ScenarioContext,
    open_id: str,
    approval_id: str,
    action: str,
    option_key: str | None = None,
    state: str = "",
) -> None:
    """Inject a card button click via the test endpoint."""
    if ctx.dry_run:
        eprint(f"  [dry-run] inject action={action} approval={approval_id}")
        return
    chat_id = ctx.args.chat_id
    chat_type = "group" if chat_id else "p2p"
    await ctx.mac.inject_card_action(
        open_id=open_id,
        approval_id=approval_id,
        action=action,
        option_key=option_key,
        state=state,
        chat_id=chat_id,
        chat_type=chat_type,
    )


async def send_feishu_text(ctx: ScenarioContext, text: str) -> str | None:
    """Send a Feishu text and return message_id, or None in dry-run."""
    if ctx.dry_run:
        eprint(f"  [dry-run] send: {text}")
        return None
    assert ctx.sim is not None
    return await ctx.sim.send_text(text)


async def ensure_ws_ready(ctx: ScenarioContext, timeout: float = 90.0) -> None:
    """Wait until the MAC bot is receiving Feishu messages.

    Sends a ``/whoami`` ping and waits for a reply.  The reply contains the
    simulator's open_id as seen by the MAC app; we capture it and use it for
    all subsequent API calls so that pending-intent lookups and project
    bindings match the WS events.
    """
    if ctx.dry_run or ctx.sim is None:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg_id = await ctx.sim.send_text("/whoami")
        reply = await ctx.sim.wait_for_reply(
            pattern=r"你的 open_id",
            after_message_id=msg_id,
            timeout=10.0,
            interval=1.0,
        )
        if reply is not None:
            match = re.search(r"你的 open_id 是：(\S+)", reply.text)
            if match:
                mac_open_id = match.group(1)
                if mac_open_id != ctx.args.open_id:
                    eprint(f"  [info] using MAC-context open_id: {mac_open_id}")
                    ctx.args.open_id = mac_open_id
            return
        eprint("  [warn] WS not ready, retrying ping...")
        await asyncio.sleep(2.0)
    raise RuntimeError(f"MAC bot did not respond to WS ping within {timeout}s")


async def feishu_create_project(ctx: ScenarioContext, text: str) -> str:
    """Create a project via Feishu message and return its project_id."""
    msg_id = await send_feishu_text(ctx, text)
    if ctx.dry_run:
        return "proj_dryrun_000"
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"已创建项目|确认任务类型", after_message_id=msg_id
    )
    if reply is None:
        raise RuntimeError("MAC bot did not reply with project creation")
    # Natural-language descriptions may trigger an intent confirmation card.
    if "确认任务类型" in reply.text:
        for t in ("feature", "bug_fix", "documentation"):
            if t in reply.text:
                await inject_action(
                    ctx,
                    open_id=ctx.args.open_id,
                    approval_id="",
                    action="menu",
                    option_key=f"intent_confirm:{t}",
                )
                break
        reply = await ctx.sim.wait_for_reply(
            pattern=r"已创建项目", after_message_id=reply.message_id
        )
        if reply is None:
            raise RuntimeError("MAC bot did not reply after intent confirmation")
    project_id = extract_project_id(reply.text)
    if not project_id:
        raise RuntimeError(f"Could not extract project_id from reply: {reply.text!r}")
    eprint(f"  created project {project_id}")
    return project_id


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_p2p_create(ctx: ScenarioContext) -> None:
    """1. Create a project by sending a plain description in p2p."""
    eprint("Scenario 1: p2p_create")
    project_id = await feishu_create_project(ctx, "实现一个 config-validator CLI")
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if status is None:
        raise RuntimeError("Project did not reach REQUIREMENT_CONFIRM_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_p2p_create_with_type(ctx: ScenarioContext) -> None:
    """2. Create a project with explicit task type via /create."""
    eprint("Scenario 2: p2p_create_with_type")
    project_id = await feishu_create_project(
        ctx, "/create --type=bug_fix 修复 port 校验范围 bug"
    )
    status = await wait_for_state(ctx, project_id, "BUG_ANALYSIS")
    eprint(f"  reached {status['current_state']}")


async def scenario_group_create_at_mention(ctx: ScenarioContext) -> None:
    """3. Create a project in a group chat by @mentioning the MAC bot."""
    eprint("Scenario 3: group_create_at_mention")
    if not ctx.dry_run and not ctx.args.chat_id:
        raise RuntimeError("--chat-id is required for group scenarios")
    project_id = await feishu_create_project(ctx, "写一份项目 README")
    status = await wait_for_state(ctx, project_id, "OUTLINE_CONFIRM_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_group_create_menu(ctx: ScenarioContext) -> None:
    """4. Start a create flow from the interactive menu in a group."""
    eprint("Scenario 4: group_create_menu")
    if not ctx.dry_run and not ctx.args.chat_id:
        raise RuntimeError("--chat-id is required for group scenarios")
    msg_id = await send_feishu_text(ctx, "/menu")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(pattern=r"请选择你要执行的操作", after_message_id=msg_id)
    if reply is None:
        raise RuntimeError("MAC bot did not send menu card")
    chat_id = ctx.args.chat_id
    chat_type = "group" if chat_id else "p2p"
    await ctx.mac.inject_card_action(
        open_id=ctx.args.open_id,
        approval_id="",
        action="menu",
        option_key="create_prompt",
        chat_id=chat_id,
        chat_type=chat_type,
    )
    reply2 = await ctx.sim.wait_for_reply(pattern=r"请直接回复", after_message_id=reply.message_id)
    if reply2 is None:
        raise RuntimeError("MAC bot did not prompt for description")
    project_id = await feishu_create_project(ctx, "实现一个 group 测试项目")
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_intent_confirm_override(ctx: ScenarioContext) -> None:
    """5. Override the inferred task type from the intent confirmation card."""
    eprint("Scenario 5: intent_confirm_override")
    await ctx.mac.clear_pending_intent(ctx.args.open_id)
    # Use a description that only hits the feature fallback hints so the router
    # asks for confirmation instead of creating the project immediately.
    msg_id = await send_feishu_text(ctx, "新增一个登录接口 500 处理")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(pattern=r"确认任务类型", after_message_id=msg_id)
    if reply is None:
        raise RuntimeError("MAC bot did not send intent confirm card")
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id="",
        action="menu",
        option_key="intent_confirm:bug_fix",
    )
    reply2 = await ctx.sim.wait_for_reply(pattern=r"已创建项目", after_message_id=reply.message_id)
    project_id = extract_project_id(reply2.text) if reply2 else None
    if not project_id:
        raise RuntimeError("Could not find created project after override")
    eprint(f"  created project {project_id}")


async def scenario_requirement_confirm(ctx: ScenarioContext) -> None:
    """6. Confirm the requirement understanding and proceed to architecture."""
    eprint("Scenario 6: requirement_confirm")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="req-confirm",
            task_type="feature",
            description="实现 config-validator CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]
    await ctx.mac.tick(project_id)
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if not status or not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="confirm",
        state="REQUIREMENT_CONFIRM_PENDING",
    )
    status = await wait_for_state(ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not reach ARCHITECTURE_CONFIRM_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_requirement_reject(ctx: ScenarioContext) -> None:
    """7. Reject the requirement understanding and roll back."""
    eprint("Scenario 7: requirement_reject")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="req-reject",
            task_type="feature",
            description="实现 config-validator CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]
    await ctx.mac.tick(project_id)
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if not status or not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not reach REQUIREMENT_CONFIRM_PENDING with a pending approval")
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="reject",
        state="REQUIREMENT_CONFIRM_PENDING",
    )
    # The reject handler ticks the orchestrator, which may immediately re-plan
    # and return to REQUIREMENT_CONFIRM_PENDING.  Verify rollback by looking at
    # the recent event log for the REQUIREMENT_CONFIRM_PENDING -> PLANNING_REQUIREMENTS
    # transition.
    if ctx.dry_run:
        eprint("  [dry-run] rolled back to PLANNING_REQUIREMENTS")
        return
    status = await ctx.mac.get_status(project_id)
    rolled_back = any(
        "REQUIREMENT_CONFIRM_PENDING -> PLANNING_REQUIREMENTS" in e.get("message", "")
        for e in status.get("recent_events", [])
    )
    if not rolled_back:
        raise RuntimeError(
            f"Project did not roll back to PLANNING_REQUIREMENTS (current={status['current_state']})"
        )
    eprint(f"  rolled back to PLANNING_REQUIREMENTS (current={status['current_state']})")


async def scenario_night_mode_bug_fix(ctx: ScenarioContext) -> None:
    """8. Bug fix in night mode auto-confirms until acceptance."""
    eprint("Scenario 8: night_mode_bug_fix")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="night-bug",
            task_type="bug_fix",
            description="修复 port 校验范围 bug",
            approval_mode="night",
        )
    )
    project_id = project["id"]
    status = await tick_until_state(ctx, project_id, "ACCEPTANCE_PENDING", timeout=600.0)
    if status is None:
        raise RuntimeError("Project did not reach ACCEPTANCE_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_clarification_input(ctx: ScenarioContext) -> None:
    """9. Walk through requirement→architecture→specs confirm flow."""
    eprint("Scenario 9: clarification_input")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="clarify",
            task_type="feature",
            description="实现一个计算器 CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]
    await ctx.mac.tick(project_id)

    # Requirement → Architecture
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if not status or not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="confirm",
        state="REQUIREMENT_CONFIRM_PENDING",
    )

    # Architecture → Specs
    status = await wait_for_state(ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not reach ARCHITECTURE_CONFIRM_PENDING")
    if not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="confirm",
        state="ARCHITECTURE_CONFIRM_PENDING",
    )

    # Specs → Coding
    status = await wait_for_state(ctx, project_id, "SPEC_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not reach SPEC_CONFIRM_PENDING")
    if not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="confirm",
        state="SPEC_CONFIRM_PENDING",
    )

    status = await wait_for_state(ctx, project_id, "CODING", timeout=300.0)
    if status is None:
        raise RuntimeError("Project did not reach CODING")
    eprint(f"  reached {status['current_state']}")


async def scenario_restart_with_type(ctx: ScenarioContext) -> None:
    """10. Restart a bug_fix project as a feature at acceptance time."""
    eprint("Scenario 10: restart_with_type")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="restart",
            task_type="bug_fix",
            description="修复登录接口偶尔 500 的问题",
            approval_mode="night",
        )
    )
    project_id = project["id"]
    status = await tick_until_state(ctx, project_id, "ACCEPTANCE_PENDING", timeout=600.0)
    if status is None:
        raise RuntimeError("Project did not reach ACCEPTANCE_PENDING")
    if not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not receive a pending approval at acceptance")
    approval_id = status["pending_approval"]["approval_id"]
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="restart_with_type",
        state="ACCEPTANCE_PENDING",
    )
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(pattern=r"重新归类", after_message_id=None)
    if reply is None:
        raise RuntimeError("MAC bot did not send restart type card")
    chat_id = ctx.args.chat_id
    chat_type = "group" if chat_id else "p2p"
    await ctx.mac.inject_card_action(
        open_id=ctx.args.open_id,
        approval_id="",
        action="menu",
        option_key=f"restart_with_type:{project_id}:feature",
        chat_id=chat_id,
        chat_type=chat_type,
    )
    reply2 = await ctx.sim.wait_for_reply(pattern=r"重建为新项目", after_message_id=reply.message_id)
    if reply2 is None:
        raise RuntimeError("MAC bot did not confirm restart")
    new_project_id = extract_project_id(reply2.text)
    if not new_project_id:
        raise RuntimeError("Could not find restarted project")
    status = await wait_for_state(ctx, new_project_id, "REQUIREMENT_CONFIRM_PENDING")
    if status is None:
        raise RuntimeError("Restarted project did not reach REQUIREMENT_CONFIRM_PENDING")
    eprint(f"  restarted as {new_project_id} at {status['current_state']}")


async def scenario_bind_and_status(ctx: ScenarioContext) -> None:
    """11. Bind a user to a project and query status via Feishu commands."""
    eprint("Scenario 11: bind_and_status")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="bind-test",
            task_type="feature",
            description="实现 config-validator CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]
    await send_feishu_text(ctx, f"/bind {project_id}")
    await send_feishu_text(ctx, f"/status {project_id}")
    eprint(f"  bind/status commands sent for {project_id}")


async def scenario_group_status_at_mention(ctx: ScenarioContext) -> None:
    """12. Query project status in a group chat by @mentioning the MAC bot."""
    eprint("Scenario 12: group_status_at_mention")
    if not ctx.dry_run and not ctx.args.chat_id:
        raise RuntimeError("--chat-id is required for group scenarios")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="group-status",
            task_type="feature",
            description="实现 group status 测试",
            approval_mode="normal",
        )
    )
    await send_feishu_text(ctx, f"/status {project['id']}")
    eprint(f"  status command sent for {project['id']}")


async def scenario_api_message_and_decision(ctx: ScenarioContext) -> None:
    """13. Send a user message via API and let the project_manager resolve it."""
    eprint("Scenario 13: api_message_and_decision")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="api-msg",
            task_type="feature",
            description="实现 config-validator CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if not status or not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not receive a pending approval")
    response = await ctx.mac.submit_message(project_id, "确认需求，进入下一步")
    eprint(f"  api reply: {response['reply']}")
    # The endpoint now processes the project_manager call asynchronously, so we
    # poll the project status until the suggested decision is applied.
    status = await wait_for_state(ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not advance after API message")
    eprint(f"  state after message: {status['current_state']}")


async def scenario_full_feature_flow(ctx: ScenarioContext) -> None:
    """14. Walk a feature project through requirements->specs->coding->acceptance."""
    eprint("Scenario 14: full_feature_flow")
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="full-flow",
            task_type="feature",
            description="实现一个 Todo List CLI",
            approval_mode="normal",
        )
    )
    project_id = project["id"]

    # Requirements -> Architecture
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if not status or not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not receive a pending approval at requirements")
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=status["pending_approval"]["approval_id"],
        action="confirm",
        state="REQUIREMENT_CONFIRM_PENDING",
    )

    # Architecture -> Specs
    status = await wait_for_state(ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not reach ARCHITECTURE_CONFIRM_PENDING")
    if not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not receive a pending approval at architecture")
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=status["pending_approval"]["approval_id"],
        action="confirm",
        state="ARCHITECTURE_CONFIRM_PENDING",
    )

    # Specs -> Coding
    status = await wait_for_state(ctx, project_id, "SPEC_CONFIRM_PENDING", timeout=500.0)
    if status is None:
        raise RuntimeError("Project did not reach SPEC_CONFIRM_PENDING")
    if not status.get("pending_approval"):
        status = await tick_until_pending(ctx, project_id)
    if status is None or not status.get("pending_approval"):
        raise RuntimeError("Project did not receive a pending approval at specs")
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id=status["pending_approval"]["approval_id"],
        action="confirm",
        state="SPEC_CONFIRM_PENDING",
    )

    # Coding -> Testing -> Review -> Acceptance (requires multiple ticks)
    status = await tick_until_state(ctx, project_id, "ACCEPTANCE_PENDING", timeout=600.0)
    if status is None:
        raise RuntimeError("Project did not reach ACCEPTANCE_PENDING")
    eprint(f"  reached {status['current_state']}")


SCENARIOS: dict[str, Any] = {
    "p2p_create": scenario_p2p_create,
    "p2p_create_with_type": scenario_p2p_create_with_type,
    "group_create_at_mention": scenario_group_create_at_mention,
    "group_create_menu": scenario_group_create_menu,
    "intent_confirm_override": scenario_intent_confirm_override,
    "requirement_confirm": scenario_requirement_confirm,
    "requirement_reject": scenario_requirement_reject,
    "night_mode_bug_fix": scenario_night_mode_bug_fix,
    "clarification_input": scenario_clarification_input,
    "restart_with_type": scenario_restart_with_type,
    "bind_and_status": scenario_bind_and_status,
    "group_status_at_mention": scenario_group_status_at_mention,
    "api_message_and_decision": scenario_api_message_and_decision,
    "full_feature_flow": scenario_full_feature_flow,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feishu end-to-end driver for MAC")
    parser.add_argument("--list", action="store_true", help="list scenarios")
    parser.add_argument("--scenario", help="scenario name to run")
    parser.add_argument(
        "--all", action="store_true", help="run all scenarios serially"
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("MAC_API_BASE", "http://127.0.0.1:18080"),
        help="MAC API base URL",
    )
    parser.add_argument(
        "--open-id",
        default=os.getenv("FEISHU_SIMULATOR_OPEN_ID", "ou_simulator"),
        help="open_id used for injected card actions",
    )
    parser.add_argument(
        "--chat-id",
        default=os.getenv("FEISHU_SIMULATOR_CHAT_ID"),
        help="group chat_id (omit for p2p-only scenarios)",
    )
    parser.add_argument(
        "--sim-app-id",
        default=os.getenv("FEISHU_SIMULATOR_APP_ID"),
        help="simulator bot app_id",
    )
    parser.add_argument(
        "--sim-app-secret",
        default=os.getenv("FEISHU_SIMULATOR_APP_SECRET"),
        help="simulator bot app_secret",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print steps without calling Feishu or the MAC API",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="comma-separated scenario names to skip when using --all",
    )
    parser.add_argument(
        "--scenario-timeout",
        type=float,
        default=600.0,
        help="per-scenario timeout in seconds for --all (default: 600)",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()

    if args.list:
        print("Available scenarios:")
        for name in SCENARIOS:
            print(f"  {name}")
        return 0

    if not args.scenario and not args.all:
        eprint("error: --scenario or --all is required (use --list to see options)")
        return 2

    if args.scenario and args.scenario not in SCENARIOS:
        eprint(f"error: unknown scenario {args.scenario!r}")
        return 2

    dry_run = args.dry_run
    mac = MacApiClient(args.api_base, dry_run=dry_run)
    sim: FeishuSimulator | None = None

    settings = get_settings()
    if dry_run:
        eprint("[dry-run] skipping Feishu authentication")
    else:
        if not args.sim_app_id or not args.sim_app_secret:
            eprint(
                "error: simulator credentials required; set FEISHU_SIMULATOR_APP_ID "
                "and FEISHU_SIMULATOR_APP_SECRET or use --dry-run"
            )
            return 2
        if not settings.feishu_app_id or not settings.feishu_app_secret:
            eprint(
                "error: MAC bot credentials required in .env; set FEISHU_APP_ID "
                "and FEISHU_APP_SECRET or use --dry-run"
            )
            return 2
        mac_bridge = FeishuBridge(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
        )
        try:
            mac_bot_open_id = await mac_bridge.get_bot_open_id()
        finally:
            await mac_bridge.close()
        if not mac_bot_open_id:
            eprint("error: failed to fetch MAC bot open_id")
            return 2
        sim = FeishuSimulator(
            app_id=args.sim_app_id,
            app_secret=args.sim_app_secret,
            mac_bot_open_id=mac_bot_open_id,
            chat_id=args.chat_id,
            mac_app_id=settings.feishu_app_id,
        )
        eprint(f"[info] MAC bot open_id: {mac_bot_open_id}")

    ctx = ScenarioContext(sim=sim, mac=mac, args=args, dry_run=dry_run)
    try:
        if args.all:
            skipped = {s.strip() for s in args.skip.split(",") if s.strip()}
            failures: list[str] = []
            for name, fn in SCENARIOS.items():
                if name in skipped:
                    eprint(f"[info] skipping scenario: {name}")
                    continue
                eprint(f"[info] running scenario: {name}")
                try:
                    await ensure_ws_ready(ctx)
                    await ctx.mac.clear_pending_intent(ctx.args.open_id)
                    await asyncio.wait_for(fn(ctx), timeout=args.scenario_timeout)
                    eprint(f"[ok] scenario {name} completed")
                except asyncio.TimeoutError:
                    eprint(f"[fail] scenario {name}: timed out after {args.scenario_timeout}s")
                    failures.append(name)
                except Exception as exc:
                    eprint(f"[fail] scenario {name}: {exc}")
                    failures.append(name)
                await asyncio.sleep(2.0)
            if failures:
                eprint(f"[summary] failed scenarios: {', '.join(failures)}")
                return 1
            eprint("[summary] all scenarios passed")
            return 0

        if args.scenario not in SCENARIOS:
            eprint(f"error: unknown scenario {args.scenario!r}")
            return 2

        await ensure_ws_ready(ctx)
        await ctx.mac.clear_pending_intent(ctx.args.open_id)
        await SCENARIOS[args.scenario](ctx)
        eprint(f"[ok] scenario {args.scenario} completed")
        return 0
    except Exception as exc:
        eprint(f"[fail] scenario {args.scenario}: {exc}")
        return 1
    finally:
        if sim is not None:
            await sim.close()
        await mac.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
