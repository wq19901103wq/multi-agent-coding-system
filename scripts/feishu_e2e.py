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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# Allow running from the repo root without installing the package.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from mac_system.config import get_settings  # noqa: E402
from mac_system.feishu.bridge import FeishuBridge  # noqa: E402


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


ID_RE = re.compile(r"\b(proj_[a-zA-Z0-9_]+)\b")
APPROVAL_RE = re.compile(r"\b(appr_[a-zA-Z0-9_]+)\b")
BIND_DIR_INTERACTION_RE = re.compile(
    r"bind_dir_for_create_(?:skip|select)|(?:选择|绑定).{0,24}目录|系统工作区"
)


def extract_project_id(text: str) -> str | None:
    m = ID_RE.search(text)
    return m.group(1) if m else None


def extract_approval_id(text: str) -> str | None:
    m = APPROVAL_RE.search(text)
    return m.group(1) if m else None


def _is_bind_dir_interaction(text: str) -> bool:
    """Recognize the interaction contract across card copy/schema versions."""
    return BIND_DIR_INTERACTION_RE.search(text) is not None


def _extract_message_text(content: dict[str, Any]) -> str:
    """Extract searchable text and callback metadata from a Feishu message.

    Opaque approval ids intentionally stay out of the user-facing card body,
    but a real button click still carries one in its callback value.  The E2E
    simulator needs to read that structured value to emulate the click; it
    must not depend on the id also being rendered as visible prose.
    """
    if "text" in content:
        return content["text"]
    parts: list[str] = []
    _TEXT_KEYS = {"title", "text", "content"}

    def collect(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _TEXT_KEYS and isinstance(v, str):
                    parts.append(v)
                elif k == "approval_id" and isinstance(v, str) and v:
                    parts.append(f"审批ID：{v}")
                elif k == "option_key" and isinstance(v, str) and v:
                    # Button callback metadata is the stable contract for an
                    # interaction.  Visible card copy is deliberately free to
                    # evolve and must not be the simulator's only selector.
                    parts.append(f"动作：{v}")
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
        # Trust env is disabled so macOS system HTTP proxies do not intercept
        # localhost traffic and break status polling.
        control_token = os.getenv("CONTROL_API_TOKEN") or os.getenv("ADMIN_TOKEN")
        headers = {"Authorization": f"Bearer {control_token}"} if control_token else {}
        self._client = httpx.AsyncClient(
            timeout=300.0, trust_env=False, headers=headers
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_status(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"current_state": "DRYRUN", "pending_approval": None}
        # The API may briefly become unresponsive while an agent tick is running;
        # retry a few times before giving up.
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                resp = await self._client.get(
                    f"{self.base_url}/projects/{project_id}/status",
                    timeout=30.0,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(1.0 * (attempt + 1))
        raise last_exc or RuntimeError("Failed to get project status")

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

    async def inject_text_message(self, **fields: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        resp = await self._client.post(
            f"{self.base_url}/test/inject_text_message",
            json=fields,
        )
        resp.raise_for_status()
        return resp.json()

    async def seed_feishu_interaction(self, **fields: Any) -> dict[str, Any]:
        if self.dry_run:
            return {
                "project_id": "proj_dryrun_interaction",
                "approval_id": "appr_dryrun_interaction",
                "interaction_id": "qi_dryrun_interaction",
                "state": "DRYRUN",
            }
        resp = await self._client.post(
            f"{self.base_url}/test/seed_feishu_interaction", json=fields
        )
        resp.raise_for_status()
        return resp.json()

    async def clear_pending_intent(self, open_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        # Best-effort cleanup; the test endpoint may be disabled or the server
        # may be briefly unresponsive between ticks.
        for attempt in range(5):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/test/clear_pending_intent/{open_id}",
                    timeout=15.0,
                )
                if resp.status_code == 404:
                    return {"status": "skipped"}
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404,):
                    return {"status": "skipped"}
                eprint(f"  [warn] clear_pending_intent HTTP {exc.response.status_code}, retry {attempt + 1}")
                await asyncio.sleep(1.0 * (attempt + 1))
            except Exception as exc:
                eprint(f"  [warn] clear_pending_intent failed: {exc}, retry {attempt + 1}")
                await asyncio.sleep(1.0 * (attempt + 1))
        return {"status": "skipped"}

    async def tick(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "ok"}
        # A healthy Agent attempt may run for the configured 20-minute limit.
        # Keep the control request alive longer than that instead of turning a
        # successful late result into an empty httpx.ReadTimeout failure.
        resp = await self._client.post(
            f"{self.base_url}/test/tick/{project_id}", timeout=2700.0
        )
        resp.raise_for_status()
        return resp.json()

    async def cancel_project(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"status": "cancelled"}
        resp = await self._client.post(
            f"{self.base_url}/projects/{project_id}/commands",
            json={"command": "cancel"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_runtime_activity(self, project_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"db_running_task_ids": [], "process_task_ids": []}
        resp = await self._client.get(
            f"{self.base_url}/test/projects/{project_id}/runtime"
        )
        resp.raise_for_status()
        return resp.json()

    async def get_project_cards(self, project_id: str) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        resp = await self._client.get(f"{self.base_url}/test/projects/{project_id}/cards")
        resp.raise_for_status()
        return list(resp.json().get("items") or [])

    async def get_feishu_event(self, message_id: str) -> dict[str, Any]:
        if self.dry_run:
            return {"found": True, "status": "processed", "open_id": "ou_dryrun"}
        resp = await self._client.get(f"{self.base_url}/test/feishu/events/{message_id}")
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
        try:
            status = await ctx.mac.get_status(project_id)
        except Exception as exc:
            eprint(f"  [warn] get_status failed, retrying: {exc}")
            await asyncio.sleep(interval)
            continue
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
            try:
                status = await ctx.mac.get_status(project_id)
            except Exception as exc:
                eprint(f"  [warn] get_status failed, retrying: {exc}")
                continue
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
        try:
            status = await ctx.mac.get_status(project_id)
        except Exception as exc:
            eprint(f"  [warn] get_status failed, retrying: {exc}")
            await asyncio.sleep(interval)
            continue
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
        try:
            status = await ctx.mac.get_status(project_id)
        except Exception as exc:
            eprint(f"  [warn] get_status failed, retrying: {exc}")
            await asyncio.sleep(interval)
            continue
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
        try:
            status = await ctx.mac.get_status(project_id)
            if status.get("pending_approval"):
                return status
        except Exception as exc:
            eprint(f"  [warn] get_status failed, retrying: {exc}")
            await asyncio.sleep(2.0)
            continue
        try:
            await ctx.mac.tick(project_id)
        except httpx.HTTPStatusError:
            await asyncio.sleep(2.0)
            continue
        # Poll briefly for the tick to take effect.
        poll_deadline = time.monotonic() + 15.0
        while time.monotonic() < poll_deadline:
            await asyncio.sleep(2.0)
            try:
                status = await ctx.mac.get_status(project_id)
            except Exception as exc:
                eprint(f"  [warn] get_status failed, retrying: {exc}")
                continue
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
    decision_id: str | None = None,
    state: str = "",
    extras: dict[str, Any] | None = None,
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
        decision_id=decision_id,
        state=state,
        chat_id=chat_id,
        chat_type=chat_type,
        extras=extras or {},
    )


async def send_feishu_text(ctx: ScenarioContext, text: str) -> str | None:
    """Send a Feishu text and return message_id, or None in dry-run."""
    if ctx.dry_run:
        eprint(f"  [dry-run] send: {text}")
        return None
    assert ctx.sim is not None
    message_id = await ctx.sim.send_text(text)
    if ctx.args.scenario == "full_feature_flow":
        # The message POST only proves Feishu accepted the user's message. Wait
        # until MAC durably finishes the WS callback before reading the next
        # approval; otherwise the driver can mistake the previous pending card
        # for a newly generated one after reject/confirm.
        deadline = time.monotonic() + 2700.0
        while time.monotonic() < deadline:
            receipt = await ctx.mac.get_feishu_event(message_id)
            status = str(receipt.get("status") or "")
            if receipt.get("found") and status == "processed":
                return message_id
            if receipt.get("found") and status == "error":
                raise RuntimeError(
                    f"MAC failed to process Feishu message {message_id}: "
                    f"{receipt.get('detail') or 'unknown error'}"
                )
            await asyncio.sleep(1.0)
        raise RuntimeError(f"MAC did not finish Feishu message {message_id}")
    return message_id


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
        receipt_deadline = min(deadline, time.monotonic() + 12.0)
        while time.monotonic() < receipt_deadline:
            receipt = await ctx.mac.get_feishu_event(msg_id)
            if receipt.get("found") and receipt.get("status") == "processed":
                mac_open_id = str(receipt.get("open_id") or "")
                if mac_open_id and mac_open_id != ctx.args.open_id:
                    eprint(f"  [info] using MAC-context open_id: {mac_open_id}")
                    ctx.args.open_id = mac_open_id
                return
            await asyncio.sleep(1.0)
        eprint("  [warn] WS not ready, retrying ping...")
        await asyncio.sleep(2.0)
    raise RuntimeError(f"MAC bot did not respond to WS ping within {timeout}s")


async def feishu_create_project(ctx: ScenarioContext, text: str) -> str:
    """Create a project via Feishu message and return its project_id.

    Handles three pre-creation interruptions:
    1. Intent confirmation card (ambiguous task type) -> click confirm.
    2. bind_dir recommendation card (MAC_ALLOWED_BIND_ROOTS set, no binding)
       -> click "skip" to use system workspace.
    """
    msg_id = await send_feishu_text(ctx, text)
    if ctx.dry_run:
        return "proj_dryrun_000"
    assert ctx.sim is not None
    # CardKit 2.0 message history returns only a client-upgrade placeholder,
    # so creation is correlated through the durable inbound-event receipt.
    # The message itself still travels through real Feishu, and later checks
    # require provider message ids for the resulting cards.
    deadline = time.monotonic() + 180.0
    bind_dir_skipped = False
    while time.monotonic() < deadline:
        receipt = await ctx.mac.get_feishu_event(msg_id)
        project_id = str(receipt.get("project_id") or "")
        if project_id:
            eprint(f"  created project {project_id}")
            return project_id
        if (
            receipt.get("pending_intent_stage") == "awaiting_bind_dir_for_create"
            and not bind_dir_skipped
        ):
            await inject_action(
                ctx,
                open_id=ctx.args.open_id,
                approval_id="",
                action="menu",
                option_key="bind_dir_for_create_skip",
            )
            bind_dir_skipped = True
        if receipt.get("status") == "error":
            raise RuntimeError(f"MAC failed to process project creation: {receipt}")
        await asyncio.sleep(1.0)
    raise RuntimeError("MAC did not correlate the Feishu message with a new project")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_p2p_create(ctx: ScenarioContext) -> None:
    """1. Create a project by sending /create with a description in p2p."""
    eprint("Scenario 1: p2p_create")
    project_id = await feishu_create_project(ctx, "/create 实现一个 config-validator CLI")
    status = await wait_for_state(ctx, project_id, "REQUIREMENT_CONFIRM_PENDING")
    if status is None:
        raise RuntimeError("Project did not reach REQUIREMENT_CONFIRM_PENDING")
    eprint(f"  reached {status['current_state']}")


async def scenario_agent_loop_delivery(ctx: ScenarioContext) -> None:
    """Drive the native Coordinator loop through the real Feishu p2p UI."""
    eprint("Scenario: agent_loop_delivery")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await ctx.sim.send_text("/whoami")
    project_id = await feishu_create_project(
        ctx,
        (
            "/create --type=feature 创建一个最小 Python CLI：运行 `python -m hello_loop` "
            "必须输出 `hello-loop`；补自动化测试。实现后从真实命令入口独立验证。"
        ),
    )
    deadline = time.monotonic() + 900.0
    status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        eprint(
            f"  [agent-loop] lifecycle={status.get('status')} "
            f"posture={status.get('execution_status')}"
        )
        if status.get("status") == "completed":
            break
        if status.get("status") in {"failed", "cancelled"}:
            raise RuntimeError(f"agent-loop project terminated: {status}")
        await asyncio.sleep(3.0)
    if not status or status.get("status") != "completed":
        raise RuntimeError(f"agent-loop project did not complete: {status}")
    if status.get("display") != "交付完成":
        raise RuntimeError(f"completed project has inconsistent visible status: {status}")
    if status.get("pending_approval"):
        raise RuntimeError("completed agent-loop project still has a pending approval")

    completion = await ctx.sim.wait_for_reply(
        pattern=r"已完成|hello-loop",
        after_message_id=anchor,
        timeout=120.0,
    )
    if completion is None:
        raise RuntimeError("Feishu did not receive a completion/result card")
    cards = await ctx.mac.get_project_cards(project_id)
    if not any(
        card.get("lifecycle_status") == "published"
        and card.get("card_kind") in {"report", "progress"}
        for card in cards
    ):
        raise RuntimeError(f"no published report projection for agent-loop: {cards}")
    agent_cards = [
        card for card in cards
        if card.get("subject_type") == "coordinator_agent"
    ]
    if len(agent_cards) < 2 or any(
        not card.get("provider_message_id")
        or card.get("lifecycle_status") != "completed"
        for card in agent_cards
    ):
        raise RuntimeError(
            f"Coordinator agent cards are missing or out of sync: {agent_cards}"
        )

    status_message_id = await send_feishu_text(ctx, f"/status {project_id}")
    status_card = await ctx.sim.wait_for_reply(
        pattern=r"项目状态|交付完成|COMPLETED",
        after_message_id=status_message_id,
        timeout=120.0,
    )
    if status_card is None:
        raise RuntimeError("Feishu status card was not observable after completion")
    eprint(f"  completed native agent-loop project {project_id}")


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
    project_id = await feishu_create_project(ctx, "/create 写一份项目 README")
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
    """6. Confirm the unified requirement spec and proceed to architecture."""
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
    """7. Reject the requirement understanding via /reject and roll back."""
    eprint("Scenario 7: requirement_reject")
    msg_id = await send_feishu_text(
        ctx, "/create --type=feature 实现 config-validator CLI"
    )
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=(
            r"正在根据需求描述生成候选目录名|正在根据需求描述查找|"
            r"选择目录|跳过|自动创建并初始化|已创建项目"
        ),
        after_message_id=msg_id,
        timeout=120.0,
    )
    if reply is None:
        raise RuntimeError("MAC bot did not start project creation")
    if "正在根据需求描述生成候选目录名" in reply.text or "正在根据需求描述查找" in reply.text:
        reply = await ctx.sim.wait_for_reply(
            pattern=r"选择目录|跳过|自动创建并初始化|已创建项目",
            after_message_id=reply.message_id,
            timeout=120.0,
        )
        if reply is None:
            raise RuntimeError("MAC bot did not send directory selection card")
    if "已创建项目" not in reply.text:
        await inject_action(
            ctx,
            open_id=ctx.args.open_id,
            approval_id="",
            action="menu",
            option_key="bind_dir_for_create_skip",
        )
        reply = await ctx.sim.wait_for_reply(
            pattern=r"已创建项目",
            after_message_id=reply.message_id,
            timeout=120.0,
        )
        if reply is None:
            raise RuntimeError("MAC bot did not create project after directory skip")
    project_id = extract_project_id(reply.text)
    if not project_id:
        raise RuntimeError(f"Could not extract project_id from reply: {reply.text!r}")
    eprint(f"  created project {project_id}")

    approval_id, _ = await wait_for_next_approval(ctx, after_message_id=reply.message_id)
    eprint(f"  requirement approval {approval_id}")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} 需求范围太大了，先只做核心功能"
    )

    # The reject handler ticks the orchestrator, which may immediately re-plan
    # and return to REQUIREMENT_CONFIRM_PENDING.  Verify rollback by looking at
    # the recent event log for the REQUIREMENT_CONFIRM_PENDING -> PLANNING_REQUIREMENTS
    # transition.
    assert ctx.sim is not None
    # Wait for the rejection summary / re-plan confirmation card.
    reply = await ctx.sim.wait_for_reply(
        pattern=r"反馈汇总|重新设计|第.*轮", after_message_id=msg_id, timeout=120.0
    )
    if reply is None:
        eprint("  [warn] did not see rejection feedback card")
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
    """9. Walk through specify→architecture→implementation spec confirmation."""
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

    # Unified requirement spec → Architecture
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
    status = await wait_for_state(ctx, project_id, "IMPLEMENTATION_SPEC_CONFIRM_PENDING", timeout=500.0)
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
        state="IMPLEMENTATION_SPEC_CONFIRM_PENDING",
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
    """11. Switch to a project and query status via Feishu commands.

    Originally tested /bind <project_id> to bind a user to a project; that
    command was renamed to /switch <project_id>. /bind now binds a directory.
    """
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
    await send_feishu_text(ctx, f"/switch {project_id}")
    await send_feishu_text(ctx, f"/status {project_id}")
    eprint(f"  switch/status commands sent for {project_id}")


async def scenario_bind_dir_agent(ctx: ScenarioContext) -> None:
    """16. Test /bind (no-args) and /create agent-recommended bind dir flow.

    Requires MAC_ALLOWED_BIND_ROOTS to be configured. Verifies:
    1. /bind (no-args) deterministically discovers repos and shows a candidate card.
    2. /create <description> triggers agent recommendation and shows pick card.
    3. Clicking 'skip' on the pick card creates the project with system workspace.
    """
    eprint("Scenario 16: bind_dir_agent")
    # 1. /bind (no-args) -> candidate card -> cancel
    msg1 = await send_feishu_text(ctx, "/bind")
    reply1 = await ctx.sim.wait_for_reply(
        pattern=r"选择要绑定的目录|未配置|没有检测到",
        after_message_id=msg1,
        timeout=120.0,
    )
    if reply1 is None:
        raise RuntimeError("MAC bot did not reply to /bind (no-args)")
    if "未配置" in reply1.text or "没有检测到" in reply1.text:
        eprint("  MAC_ALLOWED_BIND_ROOTS not configured or no repos found; skipping agent test")
        return
    eprint("  /bind candidate card received")
    await inject_action(
        ctx,
        open_id=ctx.args.open_id,
        approval_id="",
        action="menu",
        option_key="bind_dir_cancel",
    )
    eprint("  cancelled /bind candidate card")

    # 2. /create <description> -> agent recommendation -> skip -> project created
    msg2 = await send_feishu_text(ctx, "/create --type=feature 实现 config-validator CLI")
    reply2 = await ctx.sim.wait_for_reply(
        pattern=r"正在根据需求描述生成候选目录名|正在根据需求描述查找|选择要绑定的目录|已创建项目",
        after_message_id=msg2,
        timeout=120.0,
    )
    if reply2 is None:
        raise RuntimeError("MAC bot did not reply to /create")
    if "已创建项目" in reply2.text:
        eprint("  /create skipped agent (binding exists or no allowed roots); project created directly")
        return
    # Agent returned candidates; wait for the pick card.
    if "正在根据需求描述生成候选目录名" in reply2.text or "正在根据需求描述查找" in reply2.text:
        reply2 = await ctx.sim.wait_for_reply(
            pattern=r"选择目录|跳过|已创建项目|自动创建并初始化",
            after_message_id=reply2.message_id,
            timeout=120.0,
        )
        if reply2 is None:
            raise RuntimeError("MAC bot did not send pick card after agent scan")
    if "已创建项目" in reply2.text:
        project_id = extract_project_id(reply2.text)
        if not project_id:
            raise RuntimeError(f"Could not extract project_id from reply: {reply2.text!r}")
        eprint(f"  recommendation timed out or returned no candidates; project created: {project_id}")
        await _cancel_seeded_project(ctx, project_id)
        eprint(f"  cleaned up test project: {project_id}")
        return
    if "选择目录" in reply2.text or "跳过" in reply2.text:
        await inject_action(
            ctx,
            open_id=ctx.args.open_id,
            approval_id="",
            action="menu",
            option_key="bind_dir_for_create_skip",
        )
        reply3 = await ctx.sim.wait_for_reply(
            pattern=r"已创建项目",
            after_message_id=reply2.message_id,
            timeout=120.0,
        )
        if reply3 is None:
            raise RuntimeError("MAC bot did not create project after skip")
        project_id = extract_project_id(reply3.text)
        if not project_id:
            raise RuntimeError(f"Could not extract project_id from reply: {reply3.text!r}")
        eprint(f"  project created after skip: {project_id}")
        # Keep the shared Feishu E2E chat reusable.  An active project blocks
        # later /bind and /unbind checks by design, so this scenario must clean
        # up the project it created after the behavior has been verified.
        await _cancel_seeded_project(ctx, project_id)
        eprint(f"  cleaned up test project: {project_id}")
    else:
        raise RuntimeError(f"Unexpected reply from /create: {reply2.text!r}")


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


async def wait_for_next_approval(
    ctx: ScenarioContext,
    after_message_id: str | None = None,
    timeout: float = 300.0,
) -> tuple[str, FeishuMessage]:
    """Wait for the bot to send a card containing an approval id."""
    # Real coding/rework rounds can legitimately run until the configured
    # 20-minute Agent timeout. The full flow must not fail at 10 minutes while
    # the task is still healthy and deliver its approval moments later.
    if ctx.args.scenario == "full_feature_flow":
        # Two legal 20-minute attempts plus recovery/card delivery overhead.
        timeout = max(timeout, 2700.0)
    if ctx.dry_run:
        return "appr_dryrun", FeishuMessage("msg_dryrun", "", "")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"审批ID.*appr_",
        after_message_id=after_message_id,
        timeout=timeout,
    )
    if reply is None:
        raise RuntimeError("Did not receive an approval card from MAC bot")
    approval_id = extract_approval_id(reply.text)
    if not approval_id:
        raise RuntimeError(f"Could not extract approval_id from reply: {reply.text!r}")
    return approval_id, reply


def _confirmation_path_for_rework(target: str, expected_state: str) -> list[str]:
    """Return upstream confirmations to pass before ``expected_state``."""
    confirmation_chain = [
        "REQUIREMENT_CONFIRM_PENDING",
        "ARCHITECTURE_CONFIRM_PENDING",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
        "CODING_CONFIRM_PENDING",
        "TESTING_CONFIRM_PENDING",
        "REVIEW_CONFIRM_PENDING",
    ]
    target_state = {
        "to_requirements": "REQUIREMENT_CONFIRM_PENDING",
        "to_architecture": "ARCHITECTURE_CONFIRM_PENDING",
        "to_specs": "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
        "to_coding": "CODING_CONFIRM_PENDING",
        "to_testing": "TESTING_CONFIRM_PENDING",
    }.get(target)
    if target_state not in confirmation_chain:
        raise RuntimeError(f"Unsupported structured rework target: {target}")
    if expected_state not in confirmation_chain:
        raise RuntimeError(f"Unsupported expected confirmation: {expected_state}")
    start = confirmation_chain.index(target_state)
    stop = confirmation_chain.index(expected_state)
    if start > stop:
        raise RuntimeError(
            f"Rework target {target} is downstream of expected {expected_state}"
        )
    return confirmation_chain[start:stop]


def _structured_recovery_blocker(
    pending_approval: dict[str, Any],
) -> str | None:
    """Describe a protocol-declared non-retryable recovery, if present."""
    recovery = pending_approval.get("recovery") or {}
    if recovery.get("retryable") is not False:
        return None
    return (
        "Agent entered recovery for a structured non-retryable external "
        f"failure: domain={recovery.get('domain')}, "
        f"status={recovery.get('status_code')}"
    )


async def wait_for_approval_at_state(
    ctx: ScenarioContext,
    project_id: str,
    expected_state: str,
    *,
    after_message_id: str,
    timeout: float = 300.0,
    recovery_depth: int = 0,
) -> tuple[str, Any]:
    """Wait for one visible approval card and resolve its callback id.

    Feishu's message-history API deliberately omits button callback values.
    Therefore the black-box side observes the card's visible question, while
    the control plane supplies the pending approval id that the simulated
    button click would have carried.  This mirrors a real UI click without
    requiring opaque database ids to be printed in the card body.
    """
    if ctx.args.scenario == "full_feature_flow":
        timeout = max(timeout, 2700.0)
    assert ctx.sim is not None
    deadline = time.monotonic() + timeout
    reply = None
    status: dict[str, Any] = {}
    approval_id = ""
    next_level_tick_at = 0.0
    producer_state = {
        "REQUIREMENT_CONFIRM_PENDING": "PLANNING_REQUIREMENTS",
        "ARCHITECTURE_CONFIRM_PENDING": "PLANNING_ARCHITECTURE",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "PLANNING_IMPLEMENTATION_SPECS",
        "CODING_CONFIRM_PENDING": "CODING",
        "TESTING_CONFIRM_PENDING": "TESTING",
        "REVIEW_CONFIRM_PENDING": "REVIEW",
        "ACCEPTANCE_PENDING": "ACCEPTANCE_PENDING",
    }.get(expected_state)
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        pending = status.get("pending_approval") or {}
        current_state = str(status.get("current_state") or "")
        now = time.monotonic()
        if (
            not pending
            and current_state in {producer_state, expected_state}
            and now >= next_level_tick_at
        ):
            # Agent execution and delivery of the following human boundary are
            # level-triggered. Some Feishu command handlers only commit the
            # transition; the black-box driver must provide the next tick just
            # as the production scheduler does. A short backoff avoids polling
            # an already-running task with repeated ticks.
            next_level_tick_at = now + 10.0
            await ctx.mac.tick(project_id)
            continue
        approval_id = str(pending.get("approval_id") or "")
        visible_question = str(pending.get("question") or "").strip()
        approval_type = str(pending.get("type") or "")
        option_labels = {
            str(option.get("label") or "").strip()
            for option in pending.get("options") or []
            if str(option.get("label") or "").strip()
        }
        if approval_id:
            # Card 2.0 history bodies are intentionally replaced by Feishu
            # with an upgrade placeholder. The durable projection is the
            # authoritative receipt that the real provider accepted a visible
            # card and gives us its real message id.
            for projection in await ctx.mac.get_project_cards(project_id):
                if (
                    projection.get("subject_type") == "approval"
                    and projection.get("subject_id") == approval_id
                    and projection.get("provider_message_id")
                ):
                    reply = FeishuMessage(
                        message_id=str(projection["provider_message_id"]),
                        sender_id=ctx.sim.mac_app_id or ctx.sim.mac_bot_open_id,
                        text=visible_question,
                    )
                    break
        if approval_id and reply is None:
            for message in await ctx.sim.get_messages():
                if message.message_id == after_message_id:
                    break
                if message.sender_id not in (
                    ctx.sim.mac_bot_open_id,
                    ctx.sim.mac_app_id,
                ):
                    continue
                question_matches = bool(
                    visible_question and visible_question in message.text
                )
                option_matches = bool(
                    option_labels
                    and option_labels.issubset(set(message.text.splitlines()))
                )
                # Batched question cards show the individual questions rather
                # than the generic Approval.question. Their submit control is
                # the stable, user-visible indication that the card is ready.
                input_card_matches = approval_type == "input" and any(
                    label in message.text
                    for label in ("提交答案", "提交回答", "提交自定义答案")
                )
                if question_matches or option_matches or input_card_matches:
                    reply = message
                    break
        if reply is not None:
            break
        await asyncio.sleep(2.0)
    if reply is None or not approval_id:
        raise RuntimeError("Did not receive a visible pending approval card from MAC bot")

    actual_state = status.get("current_state")
    pending = status.get("pending_approval") or {}
    if (
        actual_state != expected_state
        and (
            str(actual_state).endswith("_INPUT_PENDING")
            or actual_state == "CLARIFICATION_PENDING"
        )
        and recovery_depth < 8
    ):
        # Planning/quality agents may legitimately ask questions before the
        # confirmation state. Submit the card's recommended choices first;
        # the handler resolves the linked QuestionInteraction from approval
        # payload, so the test does not need a private interaction id.
        eprint(
            f"  agent requested input in {actual_state}; "
            "auto-submit recommended choices"
        )
        await inject_action(
            ctx,
            open_id=ctx.args.open_id,
            approval_id=approval_id,
            action="submit_selected_options",
            state=str(actual_state),
        )
        await asyncio.sleep(1.0)
        anchor_message_id = reply.message_id
        # Pure free-text batches have no recommended choice. Answer one
        # question at a time through the public command path until the same
        # interaction actually advances; intermediate answers do not create a
        # new Approval card.
        for _ in range(20):
            refreshed = await ctx.mac.get_status(project_id)
            refreshed_pending = refreshed.get("pending_approval") or {}
            if (
                refreshed.get("current_state") != actual_state
                or refreshed_pending.get("approval_id") != approval_id
            ):
                break
            sent_message_id = await send_feishu_text(
                ctx, f"/input {approval_id} 按推荐方案处理"
            )
            if not sent_message_id:
                raise RuntimeError(f"Failed to send input for {approval_id}")
            # Do not flood the shared Feishu event queue. Wait until MAC has
            # durably consumed this answer before sending the next question's
            # answer; a fixed sleep allowed the driver to outrun WS handling.
            consumed = await ctx.sim.wait_for_reply(
                pattern=r"已记录该问题的回答|已收到你的回答",
                after_message_id=sent_message_id,
                timeout=120.0,
            )
            if consumed is None:
                raise RuntimeError(
                    f"Input {approval_id} was not acknowledged before timeout"
                )
            anchor_message_id = consumed.message_id
        else:
            raise RuntimeError(
                f"Input interaction {approval_id} did not advance after 20 answers"
            )
        return await wait_for_approval_at_state(
            ctx,
            project_id,
            expected_state,
            after_message_id=anchor_message_id,
            timeout=timeout,
            recovery_depth=recovery_depth + 1,
        )
    if (
        actual_state == "REWORK"
        and expected_state in {
            "CODING_CONFIRM_PENDING",
            "TESTING_CONFIRM_PENDING",
            "REVIEW_CONFIRM_PENDING",
        }
        and recovery_depth < 3
    ):
        fallback_target = {
            "CODING_CONFIRM_PENDING": "to_coding",
            "TESTING_CONFIRM_PENDING": "to_testing",
            "REVIEW_CONFIRM_PENDING": "to_coding",
        }[expected_state]
        recommended = next(
            (
                option.get("option_key")
                for option in pending.get("options", [])
                if option.get("type") == "primary"
                and option.get("option_key")
            ),
            None,
        )
        target = str(recommended or fallback_target)
        eprint(
            f"  quality agent entered REWORK before {expected_state}; "
            f"auto-select {target}"
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} {target}")

        # Follow the normal dependency chain from the Agent-recommended
        # rework target back to the confirmation this caller is awaiting.
        # Do not enumerate failure classes: the primary option is structured
        # routing output from MAC and may point to any upstream stage.
        for intermediate_state in _confirmation_path_for_rework(
            target, expected_state
        ):
            intermediate_id, _ = await wait_for_approval_at_state(
                ctx,
                project_id,
                intermediate_state,
                after_message_id=msg_id,
                timeout=timeout,
                recovery_depth=recovery_depth + 1,
            )
            msg_id = await send_feishu_text(ctx, f"/confirm {intermediate_id}")
        return await wait_for_approval_at_state(
            ctx,
            project_id,
            expected_state,
            after_message_id=msg_id,
            timeout=timeout,
            recovery_depth=recovery_depth + 1,
        )
    if str(actual_state).endswith("_RECOVERY_PENDING"):
        recovery_blocker = _structured_recovery_blocker(pending)
        if recovery_blocker:
            raise RuntimeError(recovery_blocker)
    if (
        str(actual_state).endswith("_RECOVERY_PENDING")
        and recovery_depth < 3
    ):
        # A real model can exhaust its automatic attempts with malformed
        # stdout even after writing a valid artifact. Exercise the same
        # user-visible recovery action instead of treating the recovery card
        # as the confirmation card expected by this scenario.
        eprint(
            f"  agent entered {actual_state}; auto-select retry before "
            f"waiting for {expected_state}"
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} retry")
        return await wait_for_approval_at_state(
            ctx,
            project_id,
            expected_state,
            after_message_id=msg_id,
            timeout=timeout,
            recovery_depth=recovery_depth + 1,
        )
    if actual_state != expected_state or pending.get("approval_id") != approval_id:
        raise RuntimeError(
            "Approval/state mismatch: "
            f"expected={expected_state}, actual={actual_state}, "
            f"card_approval={approval_id}, pending_approval={pending.get('approval_id')}"
        )
    return approval_id, reply


async def scenario_full_feature_flow(ctx: ScenarioContext) -> None:
    """14. Drive a feature project through all stages with full branch coverage.

    Branch coverage:
    - Requirement:  reject → re-plan → confirm
    - Architecture: confirmation card exposes only confirm/reject → confirm
    - Spec:         reject → re-plan → confirm
    - Coding:       reject → REWORK → to_coding → re-plan → confirm
    - Testing:      reject → REWORK → to_testing → re-plan → confirm
    - Review:       reject → REWORK → to_coding → re-plan → confirm
    - Acceptance:   reject → REWORK → to_requirements → full replay → COMPLETED
    """
    eprint("Scenario 14: full_feature_flow (with full branch coverage)")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    # Create through the control plane so this exhaustive workflow remains
    # isolated from unrelated active projects in the shared E2E chat. Separate
    # scenarios cover Feishu /create and bind-dir interactions.
    anchor_message_id = await _seed_card_anchor(ctx)
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="full-feature-flow",
            task_type="feature",
            description=(
                "实现最小 Todo 交互式 CLI：一次启动后从标准输入连续接收命令，"
                "只支持 add <文本> 和 list；同一进程会话内存储，退出即丢失；"
                "不做编辑、删除、权限、持久化和网络功能"
            ),
            # Keep the exhaustive branch test explicitly manual so it can
            # exercise acceptance rejection. Normal mode now auto-completes
            # when active acceptance and independent evaluation both pass.
            approval_mode="strict",
        )
    )
    project_id = project["id"]
    eprint(f"  created isolated project {project_id}")

    # Drive both halves of the initial level-triggered transition. The first
    # tick runs the Agent; when it completes directly (without ask_user), a
    # second tick creates and sends the human-confirmation Approval card.
    await ctx.mac.tick(project_id)
    initial_status = await ctx.mac.get_status(project_id)
    if not initial_status.get("pending_approval"):
        await ctx.mac.tick(project_id)

    # ── 1. Requirements: reject then confirm ──────────────────────────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REQUIREMENT_CONFIRM_PENDING", after_message_id=anchor_message_id
    )
    eprint(f"  requirement approval {approval_id} (will reject)")
    msg_id = await send_feishu_text(
        ctx,
        f"/reject {approval_id} 请补充空文本 add 的错误行为、空列表输出和基本启动时间验收标准",
    )
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REQUIREMENT_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  requirement re-approval {approval_id} (now confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 2. Architecture: final review is confirm/reject only ──────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  architecture approval {approval_id} (confirm/reject only)")
    status = await ctx.mac.get_status(project_id)
    architecture_actions = {
        option.get("action")
        for option in (status.get("pending_approval") or {}).get("options", [])
    }
    if architecture_actions != {"confirm", "reject"}:
        raise RuntimeError(
            "Architecture confirmation must expose only confirm/reject; "
            f"got {sorted(architecture_actions)}"
        )
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 3. Implementation Spec: reject then confirm ───────────────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "IMPLEMENTATION_SPEC_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  spec approval {approval_id} (will reject)")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} Spec 未覆盖边界条件和异常场景"
    )
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "IMPLEMENTATION_SPEC_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  spec re-approval {approval_id} (now confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 4. Coding: reject → REWORK → to_coding → re-run ───────────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "CODING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  coding approval {approval_id} (will reject)")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} 代码缺少单元测试和错误处理"
    )
    # After reject → REWORK, wait for REWORK selection card
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REWORK", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  coding rework approval {approval_id} (select to_coding)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} to_coding")
    # Coder re-runs, lands in CODING_CONFIRM_PENDING
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "CODING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  coding re-approval {approval_id} (now confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 5. Testing: reject → REWORK → to_testing → re-run ─────────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "TESTING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  testing approval {approval_id} (will reject)")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} 测试用例未覆盖边界值和异常输入"
    )
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REWORK", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  testing rework approval {approval_id} (select to_testing)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} to_testing")
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "TESTING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  testing re-approval {approval_id} (now confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 6. Review: reject → REWORK → to_coding → re-run ───────────────
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REVIEW_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  review approval {approval_id} (will reject)")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} 错误信息存在重复硬编码，请集中定义并补回归测试"
    )
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REWORK", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  review rework approval {approval_id} (select to_coding)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} to_coding")
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "CODING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  post-review coding approval {approval_id} (confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "TESTING_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  post-review testing approval {approval_id} (confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REVIEW_CONFIRM_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  review re-approval {approval_id} (now confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # ── 7. Acceptance: reject → REWORK → to_requirements → full replay ──
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "ACCEPTANCE_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  acceptance approval {approval_id} (will reject to_requirements)")
    msg_id = await send_feishu_text(
        ctx, f"/reject {approval_id} 需求未明确空文本 add 的进程退出码，请补充为非零退出码"
    )
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "REWORK", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  acceptance rework approval {approval_id} (select to_requirements)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id} to_requirements")

    # Full replay: unified requirement spec → architecture → implementation
    # spec → coding → testing → review → acceptance.
    replay_states = (
        ("replay-requirement", "REQUIREMENT_CONFIRM_PENDING"),
        ("replay-architecture", "ARCHITECTURE_CONFIRM_PENDING"),
        ("replay-spec", "IMPLEMENTATION_SPEC_CONFIRM_PENDING"),
        ("replay-coding", "CODING_CONFIRM_PENDING"),
        ("replay-testing", "TESTING_CONFIRM_PENDING"),
        ("replay-review", "REVIEW_CONFIRM_PENDING"),
    )
    for stage_label, expected_state in replay_states:
        approval_id, reply = await wait_for_approval_at_state(
            ctx, project_id, expected_state, after_message_id=msg_id, timeout=600.0
        )
        eprint(f"  {stage_label} approval {approval_id} (confirm)")
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    # Final acceptance → COMPLETED
    approval_id, reply = await wait_for_approval_at_state(
        ctx, project_id, "ACCEPTANCE_PENDING", after_message_id=msg_id, timeout=600.0
    )
    eprint(f"  final acceptance approval {approval_id} (confirm)")
    msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

    status = await wait_for_state(ctx, project_id, "COMPLETED", timeout=1500.0)
    if status is None:
        status = await ctx.mac.get_status(project_id)
        raise RuntimeError(
            f"Project did not complete (current={status.get('current_state')})"
        )
    eprint(f"  reached {status['current_state']}")


async def scenario_p2p_create_interactive(ctx: ScenarioContext) -> None:
    """Create a project by sending /create then a generic description."""
    eprint("Scenario: p2p_create_interactive")
    msg_id = await send_feishu_text(ctx, "/create")
    if ctx.dry_run:
        return
    reply = await ctx.sim.wait_for_reply(
        pattern=r"请直接回复|描述一下你的需求", after_message_id=msg_id
    )
    if reply is None:
        raise RuntimeError("MAC bot did not prompt for description")
    project_id = await feishu_create_project(ctx, "帮我做一个个人网站")
    try:
        # This scenario verifies interactive creation, not a particular
        # approval policy.  A user's default authorization may automatically
        # pass the requirements boundary before the observer polls it, so do
        # not require one transient state/card here.  The exhaustive workflow
        # scenario separately validates every visible approval boundary.
        status = await ctx.mac.get_status(project_id)
        if status.get("status") != "active":
            raise RuntimeError(f"Created project is not active: {status!r}")
        eprint(f"  reached {status['current_state']}")
    finally:
        await _cancel_seeded_project(ctx, project_id)


async def scenario_conversation_delivery(ctx: ScenarioContext) -> None:
    """Discuss first, then confirm one bounded delivery action exactly once."""
    eprint("Scenario: conversation_delivery")
    msg_id = await send_feishu_text(
        ctx,
        "先帮我分析一个最小书签整理 CLI 应该怎么设计，只讨论方案，不要开始开发。",
    )
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(after_message_id=msg_id, timeout=600.0)
    if reply is None:
        raise RuntimeError("Conversation coordinator did not answer the discussion")

    msg_id = await send_feishu_text(
        ctx,
        "按刚才的最小范围开始开发，只做 add 和 list，完成标准是单元测试通过。",
    )
    action_card = await ctx.sim.wait_for_reply(
        pattern=r"确认执行范围|执行请求ID.*cact_",
        after_message_id=msg_id,
        timeout=600.0,
    )
    if action_card is None:
        raise RuntimeError("Conversation coordinator did not send an execution confirmation card")
    match = re.search(r"执行请求ID[：:]\s*(cact_[A-Za-z0-9_]+)", action_card.text)
    if match is None:
        raise RuntimeError(f"Could not extract pending action id: {action_card.text!r}")
    action_id = match.group(1)

    await inject_action(
        ctx,
        ctx.args.open_id,
        "",
        "conversation_action",
        extras={"pending_action_id": action_id, "decision": "confirm"},
    )
    created = await ctx.sim.wait_for_reply(
        pattern=r"已创建项目.*proj_",
        after_message_id=action_card.message_id,
        timeout=180.0,
    )
    if created is None:
        raise RuntimeError("Confirmed conversation action did not create a project")
    project_id = extract_project_id(created.text)
    if project_id is None:
        raise RuntimeError(f"Could not extract created project id: {created.text!r}")

    await inject_action(
        ctx,
        ctx.args.open_id,
        "",
        "conversation_action",
        extras={"pending_action_id": action_id, "decision": "confirm"},
    )
    duplicate_receipt = await ctx.sim.wait_for_reply(
        pattern=r"已经处理|无需重复点击",
        after_message_id=created.message_id,
        timeout=60.0,
    )
    if duplicate_receipt is None:
        raise RuntimeError("Repeated conversation action did not return an idempotent receipt")
    await send_feishu_text(ctx, f"/cancel {project_id}")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        if status.get("status") == "cancelled":
            break
        await asyncio.sleep(2.0)
    else:
        raise RuntimeError("Conversation delivery project was not cancelled")
    # Leave a short quiet window so a late Feishu card cannot leak into the
    # next scenario when --all is run in the same group chat.
    await asyncio.sleep(5.0)


async def _seed_card_anchor(ctx: ScenarioContext) -> str:
    """Return a fresh MAC reply id so seeded cards cannot match old messages."""
    assert ctx.sim is not None
    message_id = await send_feishu_text(ctx, "/whoami")
    if ctx.args.scenario == "full_feature_flow":
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            receipt = await ctx.mac.get_feishu_event(str(message_id))
            if receipt.get("found") and receipt.get("status") == "processed":
                return str(message_id)
            await asyncio.sleep(1.0)
        raise RuntimeError("MAC bot did not process the interaction anchor")
    reply = await ctx.sim.wait_for_reply(after_message_id=message_id, timeout=60.0)
    if reply is None:
        raise RuntimeError("MAC bot did not answer the interaction anchor")
    return reply.message_id


async def _cancel_seeded_project(ctx: ScenarioContext, project_id: str) -> None:
    """Cancel a seeded project and wait until it can no longer leak work/cards."""
    # Use the Control API so cancellation can interrupt an Agent currently
    # occupying the serialized Feishu handler. Sending /cancel into the same
    # chat would queue behind that handler and let cards leak into the next
    # scenario.
    await ctx.mac.cancel_project(project_id)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        if status.get("status") == "cancelled":
            await asyncio.sleep(2.0)
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(f"Seeded project was not cancelled: {project_id}")


async def scenario_planning_rejection_form(ctx: ScenarioContext) -> None:
    """Render the planning rejection form and submit structured feedback."""
    eprint("Scenario: planning_rejection_form")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="planning_rejection",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    approval_id = seeded["approval_id"]
    confirmation = await ctx.sim.wait_for_reply(
        pattern=rf"审批ID.*{re.escape(approval_id)}",
        after_message_id=anchor,
        timeout=120.0,
    )
    if confirmation is None:
        raise RuntimeError("Planning confirmation card was not rendered")
    await inject_action(
        ctx,
        ctx.args.open_id,
        approval_id,
        "reject",
        state="REQUIREMENT_CONFIRM_PENDING",
    )
    form = await ctx.sim.wait_for_reply(
        pattern=r"填写修改意见|提交并驳回",
        after_message_id=confirmation.message_id,
        timeout=120.0,
    )
    if form is None:
        raise RuntimeError("Planning rejection input form was not rendered")
    await inject_action(
        ctx,
        ctx.args.open_id,
        approval_id,
        "submit_rejection_feedback",
        state="REQUIREMENT_CONFIRM_PENDING",
        extras={"rejection_feedback": "范围太大，先保留核心路径"},
    )
    status = await wait_for_state(
        ctx, seeded["project_id"], "PLANNING_REQUIREMENTS", timeout=60.0
    )
    if status is None:
        raise RuntimeError("Submitted rejection feedback did not restart planning")
    await _cancel_seeded_project(ctx, seeded["project_id"])


async def scenario_architecture_confirmation(ctx: ScenarioContext) -> None:
    """Architecture final review exposes only confirm/reject actions."""
    eprint("Scenario: architecture_confirmation")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="architecture_confirmation",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    approval_id = seeded["approval_id"]
    confirmation = await ctx.sim.wait_for_reply(
        pattern=rf"审批ID.*{re.escape(approval_id)}",
        after_message_id=anchor,
        timeout=120.0,
    )
    if confirmation is None:
        raise RuntimeError("Architecture confirmation card was not rendered")
    status = await ctx.mac.get_status(seeded["project_id"])
    actions = {
        option.get("action")
        for option in (status.get("pending_approval") or {}).get("options", [])
    }
    if actions != {"confirm", "reject"}:
        raise RuntimeError(
            "Architecture confirmation must expose only confirm/reject; "
            f"got {sorted(actions)}"
        )
    await _cancel_seeded_project(ctx, seeded["project_id"])


async def scenario_reject_without_feedback_advances(ctx: ScenarioContext) -> None:
    """Bug #3: /reject on a non-planning confirm state without feedback must advance the project.

    Planning states (REQUIREMENT_CONFIRM_PENDING, ARCHITECTURE_CONFIRM_PENDING,
    IMPLEMENTATION_SPEC_CONFIRM_PENDING) show a feedback form on reject.
    Non-planning states (CODING_CONFIRM_PENDING, TESTING_CONFIRM_PENDING,
    REVIEW_CONFIRM_PENDING) have no form - reject must transition and
    _advance_until_confirm must run so the project does not get stuck.
    """
    eprint("Scenario: reject_without_feedback_advances (Bug #3)")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="coding_confirmation",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    project_id = seeded["project_id"]
    approval_id = seeded["approval_id"]
    confirmation = await ctx.sim.wait_for_reply(
        pattern=rf"审批ID.*{re.escape(approval_id)}",
        after_message_id=anchor,
        timeout=120.0,
    )
    if confirmation is None:
        raise RuntimeError("Coding confirmation card was not rendered")

    pre_state = (await ctx.mac.get_status(project_id)).get("current_state")
    if pre_state != "CODING_CONFIRM_PENDING":
        raise RuntimeError(
            f"Seeded project not in CODING_CONFIRM_PENDING (got {pre_state})"
        )

    await inject_action(
        ctx,
        ctx.args.open_id,
        approval_id,
        "reject",
        state="CODING_CONFIRM_PENDING",
    )

    deadline = time.monotonic() + 120.0
    post_state = pre_state
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(project_id)
        post_state = status.get("current_state")
        if post_state != "CODING_CONFIRM_PENDING":
            break
        await asyncio.sleep(2.0)
    if post_state == "CODING_CONFIRM_PENDING":
        raise RuntimeError(
            "Bug #3 regression: project stuck in CODING_CONFIRM_PENDING "
            "after reject without feedback"
        )
    eprint(f"  state advanced to {post_state}")
    await _cancel_seeded_project(ctx, project_id)


async def scenario_acceptance_question_card(ctx: ScenarioContext) -> None:
    """Render and answer an Acceptance ask_user choice card."""
    eprint("Scenario: acceptance_question_card")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="acceptance_question",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    card = await ctx.sim.wait_for_reply(
        pattern=r"最终验收采用哪种确认方式|严格验收",
        after_message_id=anchor,
        timeout=120.0,
    )
    if card is None:
        raise RuntimeError("Acceptance question card was not rendered")
    await inject_action(
        ctx,
        ctx.args.open_id,
        seeded["approval_id"],
        "select_question_option",
        option_key="strict",
        state="ACCEPTANCE_INPUT_PENDING",
        extras={
            "interaction_id": seeded["interaction_id"],
            "question_id": "q_acceptance_mode",
        },
    )
    await inject_action(
        ctx,
        ctx.args.open_id,
        seeded["approval_id"],
        "submit_selected_options",
        state="ACCEPTANCE_INPUT_PENDING",
        extras={"interaction_id": seeded["interaction_id"]},
    )
    status = await wait_for_state(
        ctx, seeded["project_id"], "ACCEPTANCE_PENDING", timeout=60.0
    )
    if status is None:
        raise RuntimeError("Acceptance answer did not resume the original state")
    await _cancel_seeded_project(ctx, seeded["project_id"])


async def scenario_requirement_custom_question(ctx: ScenarioContext) -> None:
    """Select "我要修改", collect text, and resume the originating Agent."""
    eprint("Scenario: requirement_custom_question")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="requirement_custom_question",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    card = await ctx.sim.wait_for_reply(
        pattern=r"是否接受默认只支持移动端|我要修改",
        after_message_id=anchor,
        timeout=120.0,
    )
    if card is None:
        raise RuntimeError("Requirement custom-choice card was not rendered")
    await inject_action(
        ctx,
        ctx.args.open_id,
        seeded["approval_id"],
        "select_question_option",
        option_key="provide_custom",
        state="REQUIREMENT_INPUT_PENDING",
        extras={
            "interaction_id": seeded["interaction_id"],
            "question_id": "q_requirement_scope",
        },
    )
    await inject_action(
        ctx,
        ctx.args.open_id,
        seeded["approval_id"],
        "submit_selected_options",
        state="REQUIREMENT_INPUT_PENDING",
        extras={"interaction_id": seeded["interaction_id"]},
    )
    input_card = await ctx.sim.wait_for_reply(
        pattern=r"请直接回复此消息作答",
        after_message_id=card.message_id,
        timeout=120.0,
    )
    if input_card is None:
        raise RuntimeError("Selecting custom answer did not render a text-input card")
    await send_feishu_text(
        ctx,
        f"/input {seeded['approval_id']} 改为同时支持移动端和桌面端",
    )
    # The handler may resume and finish the Requirements Agent between two
    # polling intervals.  Accept either the execution state or a legal state
    # produced after that execution, but never mistake the original approval
    # for progress merely because both are REQUIREMENT_INPUT_PENDING.
    deadline = time.monotonic() + 120.0
    status = None
    legal_resumed_states = {
        "PLANNING_REQUIREMENTS",
        "REQUIREMENT_INPUT_PENDING",
        "REQUIREMENT_CONFIRM_PENDING",
    }
    while time.monotonic() < deadline:
        candidate = await ctx.mac.get_status(seeded["project_id"])
        pending = candidate.get("pending_approval") or {}
        approval_changed = pending.get("approval_id") != seeded["approval_id"]
        if (
            candidate.get("current_state") in legal_resumed_states
            and (
                candidate.get("current_state") != "REQUIREMENT_INPUT_PENDING"
                or approval_changed
            )
        ):
            status = candidate
            break
        await asyncio.sleep(1.0)
    if status is None:
        raise RuntimeError("Custom text answer did not resume the requirements Agent")
    eprint("  custom choice collected text and resumed requirements")
    await _cancel_seeded_project(ctx, seeded["project_id"])


async def scenario_runtime_recovery_card(ctx: ScenarioContext) -> None:
    """Render a runtime recovery card and terminate through its callback."""
    eprint("Scenario: runtime_recovery_card")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="runtime_recovery",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    card = await ctx.sim.wait_for_reply(
        pattern=r"执行失败|重新执行|终止项目",
        after_message_id=anchor,
        timeout=120.0,
    )
    if card is None:
        raise RuntimeError("Runtime recovery card was not rendered")
    await inject_action(
        ctx,
        ctx.args.open_id,
        seeded["approval_id"],
        "cancel",
        state="CODING_RECOVERY_PENDING",
    )
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        status = await ctx.mac.get_status(seeded["project_id"])
        if status.get("status") == "cancelled":
            return
        await asyncio.sleep(2.0)
    raise RuntimeError("Runtime recovery cancel did not terminate the project")


async def scenario_active_task_cancel(ctx: ScenarioContext) -> None:
    """Cancel a project through Feishu while its planning CLI is still running."""
    eprint("Scenario: active_task_cancel")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="active_task_cancel",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    project_id = seeded["project_id"]
    eprint(f"  seeded project {project_id}")

    tick_task = asyncio.create_task(ctx.mac.tick(project_id))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        activity = await ctx.mac.get_runtime_activity(project_id)
        if activity["process_task_ids"]:
            break
        await asyncio.sleep(0.5)
    else:
        tick_task.cancel()
        raise RuntimeError("Planner process did not start before cancellation")

    started_at = time.monotonic()
    cancel_message_id = await send_feishu_text(ctx, f"/cancel {project_id}")
    reply = await ctx.sim.wait_for_reply(
        pattern=rf"项目 {re.escape(project_id)} 已终止",
        after_message_id=cancel_message_id,
        timeout=20.0,
    )
    if reply is None:
        raise RuntimeError("Feishu /cancel was blocked behind the active Agent task")

    status = await ctx.mac.get_status(project_id)
    if status.get("status") != "cancelled":
        raise RuntimeError(f"Project remained {status.get('status')} after /cancel")
    deadline = time.monotonic() + 10.0
    activity: dict[str, Any] = {}
    while time.monotonic() < deadline:
        activity = await ctx.mac.get_runtime_activity(project_id)
        if not activity["db_running_task_ids"] and not activity["process_task_ids"]:
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"Cancelled project still has runtime activity: {activity}")
    await asyncio.wait_for(tick_task, timeout=10.0)
    elapsed = time.monotonic() - started_at
    eprint(f"  active task cancelled through Feishu in {elapsed:.1f}s")


async def scenario_agent_fail_auto_rework(ctx: ScenarioContext) -> None:
    """Verify organic quality-gate fail routes to REWORK and auto-takeover recovers.

    Unlike full_feature_flow (which drives rejects manually), this scenario
    engineers the project description so the first coding iteration genuinely
    misses one acceptance criterion. The tester agent should therefore emit
    passed=false on its own, orchestrator pushes the project into REWORK, and
    wait_for_approval_at_state auto-confirms the recommended rework target.
    Assertion: REWORK was sighted at least once before TESTING_CONFIRM_PENDING.
    """
    eprint("Scenario: agent_fail_auto_rework")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    anchor_message_id = await _seed_card_anchor(ctx)
    project = await ctx.mac.create_project(
        **_project_create_kwargs(
            ctx,
            name="agent-fail-auto-rework",
            task_type="feature",
            description=(
                "实现最小 Todo CLI：支持 add <文本> 和 list 两个命令，进程内存储，无持久化。"
                "验收标准：add 空文本时报错且退出码非零；add 重复文本时必须拒绝并提示已存在；"
                "list 输出全部条目。"
                "本项目用于验证质量门：第一版实现请刻意不实现「拒绝重复文本」约束"
                "（其余正常实现）；测试阶段应对照验收标准发现重复文本未被拒绝并判定不通过；"
                "返工补齐后第二轮测试全部通过。"
            ),
            # This scenario explicitly observes and drives each planning
            # boundary. Keep it strict; AI-first auto-confirmation is a
            # separate behavior and would legitimately consume those cards.
            approval_mode="strict",
        )
    )
    project_id = project["id"]
    first_tick = asyncio.create_task(ctx.mac.tick(project_id))
    eprint(f"  created isolated project {project_id}")

    rework_states: list[str] = []
    stop_watching = asyncio.Event()

    async def _watch_rework() -> None:
        while not stop_watching.is_set():
            try:
                status = await ctx.mac.get_status(project_id)
                state = status.get("current_state")
                if state == "REWORK":
                    rework_states.append(state)
                    eprint(f"  observed REWORK (sightings={len(rework_states)})")
                    # One sighting is enough to satisfy the assertion; keep
                    # watching briefly so we do not spam logs on every poll.
                    await asyncio.sleep(8.0)
                    continue
            except Exception as exc:  # noqa: BLE001 - observability only
                eprint(f"  rework watcher error: {exc!r}")
            try:
                await asyncio.wait_for(stop_watching.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    watcher = asyncio.create_task(_watch_rework())
    try:
        # Planner can legitimately take >10min on the first pass when it asks
        # a clarification batch and then re-plans with the answers.
        await first_tick
        initial_status = await ctx.mac.get_status(project_id)
        if not initial_status.get("pending_approval"):
            # Agent completion and human-boundary delivery are intentionally
            # level-triggered. Drive the boundary tick before waiting for the
            # visible card; waiting first creates a test-driver deadlock.
            await ctx.mac.tick(project_id)
        approval_id, _ = await wait_for_approval_at_state(
            ctx, project_id, "REQUIREMENT_CONFIRM_PENDING",
            after_message_id=anchor_message_id, timeout=1500.0,
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

        approval_id, _ = await wait_for_approval_at_state(
            ctx, project_id, "ARCHITECTURE_CONFIRM_PENDING",
            after_message_id=msg_id, timeout=600.0,
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

        approval_id, _ = await wait_for_approval_at_state(
            ctx, project_id, "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
            after_message_id=msg_id, timeout=600.0,
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

        approval_id, _ = await wait_for_approval_at_state(
            ctx, project_id, "CODING_CONFIRM_PENDING",
            after_message_id=msg_id, timeout=1200.0,
        )
        msg_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

        # Tester runs. If it fails organically, orchestrator routes to REWORK
        # and wait_for_approval_at_state auto-takes-over (to_testing) and
        # walks back to TESTING_CONFIRM_PENDING. Either way we land here.
        approval_id, _ = await wait_for_approval_at_state(
            ctx, project_id, "TESTING_CONFIRM_PENDING",
            after_message_id=msg_id, timeout=1200.0,
        )
        eprint(f"  reached TESTING_CONFIRM_PENDING (approval {approval_id})")

        if not rework_states:
            raise RuntimeError(
                "Tester passed organically; quality gate fail was not induced. "
                "Tighten the description so the first iteration clearly misses "
                "one acceptance criterion."
            )
        eprint(f"  organic fail confirmed via {len(rework_states)} REWORK sighting(s)")
    finally:
        stop_watching.set()
        try:
            await asyncio.wait_for(watcher, timeout=10.0)
        except asyncio.TimeoutError:
            watcher.cancel()

    # Cleanup: cancel the project so it does not linger in the shared chat.
    cancel_message_id = await send_feishu_text(ctx, f"/cancel {project_id}")
    reply = await ctx.sim.wait_for_reply(
        pattern=rf"项目 {re.escape(project_id)} 已终止",
        after_message_id=cancel_message_id,
        timeout=20.0,
    )
    if reply is None:
        eprint("  warn: /cancel reply not observed; project may still be active")
    await asyncio.sleep(5.0)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_hours_ago(hours: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _chat_id_for_inject(ctx: ScenarioContext) -> str:
    return ctx.args.chat_id or f"p2p:{ctx.args.open_id}"


def _chat_type_for_inject(ctx: ScenarioContext) -> str:
    return "group" if ctx.args.chat_id else "p2p"


async def _seed_requirement_confirmation(
    ctx: ScenarioContext,
) -> tuple[str, str, FeishuMessage]:
    """Seed a project in REQUIREMENT_CONFIRM_PENDING and return project_id/approval/card."""
    if ctx.dry_run:
        return "proj_dryrun", "appr_dryrun", FeishuMessage("", "", "")
    assert ctx.sim is not None
    anchor = await _seed_card_anchor(ctx)
    seeded = await ctx.mac.seed_feishu_interaction(
        kind="requirement_confirmation",
        open_id=ctx.args.open_id,
        chat_id=ctx.args.chat_id or f"p2p:{ctx.args.open_id}",
        chat_type="group" if ctx.args.chat_id else "p2p",
    )
    approval_id = seeded["approval_id"]
    card = await ctx.sim.wait_for_reply(
        pattern=rf"审批ID.*{re.escape(approval_id)}",
        after_message_id=anchor,
        timeout=120.0,
    )
    if card is None:
        raise RuntimeError("Requirement confirmation card was not rendered")
    return seeded["project_id"], approval_id, card


async def scenario_stale_text_message_ignored(ctx: ScenarioContext) -> None:
    """A /cancel with an old timestamp must not cancel the current project."""
    eprint("Scenario: stale_text_message_ignored")
    if ctx.dry_run:
        return
    project_id, _approval_id, _card = await _seed_requirement_confirmation(ctx)

    # Establish a fresh watermark with a real Feishu message.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to fresh /status")

    # Inject a stale /cancel from one hour ago through the same WS path.
    await ctx.mac.inject_text_message(
        open_id=ctx.args.open_id,
        text=f"/cancel {project_id}",
        chat_id=_chat_id_for_inject(ctx),
        chat_type=_chat_type_for_inject(ctx),
        message_id="msg_stale_cancel_text",
        created_at=_iso_hours_ago(1.0),
        event_id="evt_stale_cancel_text",
    )
    await asyncio.sleep(5.0)

    status = await ctx.mac.get_status(project_id)
    if status.get("status") == "cancelled":
        raise RuntimeError("Stale /cancel text message was processed")
    eprint(f"  stale /cancel ignored (status={status.get('status')})")

    # Confirm the bot is still responsive to fresh traffic.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to fresh /status after stale message")
    eprint("  fresh /status handled normally")


async def scenario_stale_card_action_ignored(ctx: ScenarioContext) -> None:
    """A stale card reject must not advance/change the project state."""
    eprint("Scenario: stale_card_action_ignored")
    if ctx.dry_run:
        return
    project_id, approval_id, _card = await _seed_requirement_confirmation(ctx)
    eprint(f"  requirement approval {approval_id}")

    # Establish a fresh watermark with a real Feishu message.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to fresh /status")

    # Reject from one hour ago should be dropped.
    await ctx.mac.inject_card_action(
        open_id=ctx.args.open_id,
        approval_id=approval_id,
        action="reject_without_feedback",
        state="REQUIREMENT_CONFIRM_PENDING",
        chat_id=_chat_id_for_inject(ctx),
        chat_type=_chat_type_for_inject(ctx),
        created_at=_iso_hours_ago(1.0),
        event_id="evt_stale_reject_card",
    )
    await asyncio.sleep(5.0)

    status = await ctx.mac.get_status(project_id)
    if status["current_state"] != "REQUIREMENT_CONFIRM_PENDING":
        raise RuntimeError(
            f"Stale reject changed state to {status['current_state']}"
        )
    if status.get("status") == "cancelled":
        raise RuntimeError("Stale reject cancelled the project")
    eprint("  stale reject ignored")

    # Confirm the bot is still responsive to fresh traffic.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to fresh /status after stale reject")
    eprint("  fresh /status handled normally")


async def scenario_out_of_order_text_message_ignored(ctx: ScenarioContext) -> None:
    """A newer /status sets the watermark; an older /confirm is dropped."""
    eprint("Scenario: out_of_order_text_message_ignored")
    if ctx.dry_run:
        return
    project_id, approval_id, _card = await _seed_requirement_confirmation(ctx)

    # Send a fresh /status to advance the watermark.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to /status")

    # Now inject an older /confirm (timestamp before the /status reply).
    await ctx.mac.inject_text_message(
        open_id=ctx.args.open_id,
        text=f"/confirm {approval_id}",
        chat_id=_chat_id_for_inject(ctx),
        chat_type=_chat_type_for_inject(ctx),
        message_id="msg_old_confirm_text",
        created_at=_iso_hours_ago(1.0),
        event_id="evt_old_confirm_text",
    )
    await asyncio.sleep(5.0)

    status = await ctx.mac.get_status(project_id)
    if status["current_state"] != "REQUIREMENT_CONFIRM_PENDING":
        raise RuntimeError(
            f"Out-of-order /confirm advanced state to {status['current_state']}"
        )
    eprint("  out-of-order /confirm ignored")

    # Confirm the bot is still responsive to fresh traffic.
    msg_id = await send_feishu_text(ctx, f"/status {project_id}")
    assert ctx.sim is not None
    reply = await ctx.sim.wait_for_reply(
        pattern=r"项目状态",
        after_message_id=msg_id,
        timeout=60.0,
    )
    if reply is None:
        raise RuntimeError("Bot did not respond to fresh /status after out-of-order event")
    eprint("  fresh /status handled normally")


async def scenario_duplicate_event_idempotent(ctx: ScenarioContext) -> None:
    """The same event_id must only be processed once."""
    eprint("Scenario: duplicate_event_idempotent")
    if ctx.dry_run:
        return
    project_id, approval_id, _card = await _seed_requirement_confirmation(ctx)

    event_id = f"evt_duplicate_{int(time.time())}"
    for _ in range(2):
        await ctx.mac.inject_card_action(
            open_id=ctx.args.open_id,
            approval_id=approval_id,
            action="confirm",
            state="REQUIREMENT_CONFIRM_PENDING",
            chat_id=_chat_id_for_inject(ctx),
            chat_type=_chat_type_for_inject(ctx),
            event_id=event_id,
        )
        await asyncio.sleep(2.0)

    status = await wait_for_state(
        ctx, project_id, "PLANNING_ARCHITECTURE", timeout=60.0
    )
    if status is None:
        raise RuntimeError("Confirm did not advance state")
    eprint(f"  duplicate confirm idempotent, reached {status['current_state']}")


def _server_db_path() -> str:
    """Resolve the running server's SQLite path from settings."""
    from mac_system.config import get_settings

    url = get_settings().database_url
    assert url.startswith("sqlite:///"), f"unsupported database_url: {url}"
    return url.removeprefix("sqlite:///")


def _latest_assistant_reply(db_path: str, chat_id: str | None) -> str | None:
    """Return the newest assistant turn persisted for this chat, if any."""
    if not chat_id:
        return None
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT recent_turns FROM conversation_sessions "
            "WHERE chat_id=? ORDER BY updated_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    for turn in reversed(json.loads(row[0])):
        if turn.get("role") == "assistant":
            return str(turn.get("content"))
    return None


def _latest_pending_conversation_action(
    db_path: str,
    created_by: str,
    action_type: str,
) -> dict[str, Any] | None:
    """Return the newest pending action for an E2E actor."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, payload, status FROM pending_conversation_actions "
            "WHERE created_by=? AND action_type=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (created_by, action_type),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "payload": json.loads(row[1]), "status": row[2]}


def _pending_conversation_action_ids(db_path: str, created_by: str) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM pending_conversation_actions "
            "WHERE created_by=? AND status='pending' ORDER BY created_at",
            (created_by,),
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


async def _reject_actor_pending_conversation_actions(ctx: ScenarioContext) -> None:
    for action_id in _pending_conversation_action_ids(
        _server_db_path(), ctx.args.open_id
    ):
        await inject_action(
            ctx,
            ctx.args.open_id,
            "",
            "conversation_action",
            extras={"pending_action_id": action_id, "decision": "reject"},
        )
    await asyncio.sleep(1.0)


def _reset_test_group_conversation(ctx: ScenarioContext) -> None:
    """Reset only the explicitly configured E2E group conversation."""
    if not ctx.args.chat_id:
        return
    import sqlite3

    conn = sqlite3.connect(_server_db_path())
    try:
        row = conn.execute(
            "SELECT id FROM conversation_sessions WHERE chat_id=?",
            (ctx.args.chat_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE pending_conversation_actions SET status='expired' "
                "WHERE session_id=? AND status='pending'",
                (row[0],),
            )
            conn.execute(
                "UPDATE conversation_sessions SET summary='', recent_turns='[]', "
                "active_project_id=NULL, latest_artifact='{}', pending_action_id=NULL, "
                "version=version+1 WHERE id=?",
                (row[0],),
            )
        conn.commit()
    finally:
        conn.close()


async def _wait_for_pending_conversation_action(
    ctx: ScenarioContext,
    action_type: str,
    timeout: float = 60.0,
    exclude_id: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    db_path = _server_db_path()
    while time.monotonic() < deadline:
        action = _latest_pending_conversation_action(
            db_path, ctx.args.open_id, action_type
        )
        if action is not None and action["id"] != exclude_id:
            return action
        await asyncio.sleep(1.0)
    raise RuntimeError(f"pending conversation action not found: {action_type}")


async def scenario_personal_site_clarification(ctx: ScenarioContext) -> None:
    """A broad creation request must yield a question card, not a retry failure."""
    eprint("Scenario: personal_site_clarification")
    msg_id = await send_feishu_text(ctx, "我要做一个个人网站")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    card = await ctx.sim.wait_for_reply(
        pattern=r"个人博客|个人简历|作品集|个人主页|哪种类型",
        after_message_id=msg_id,
        timeout=300.0,
    )
    if card is None:
        failed = await ctx.sim.wait_for_reply(
            pattern=r"处理失败|自动重试.*仍无法完成",
            after_message_id=msg_id,
            timeout=10.0,
        )
        if failed is not None:
            raise RuntimeError(f"coordinator failed instead of asking: {failed.text}")
        raise RuntimeError("personal-site request did not produce a clarification card")
    if "处理失败" in card.text:
        raise RuntimeError(f"coordinator failed instead of asking: {card.text}")
    eprint("  personal-site clarification card delivered")


async def scenario_personal_site_agent_revision(ctx: ScenarioContext) -> None:
    """Reproduce the reported multi-turn private-chat contract failure."""
    eprint("Scenario: personal_site_agent_revision")
    if not ctx.dry_run:
        if ctx.args.chat_id:
            _reset_test_group_conversation(ctx)
        else:
            await _reject_actor_pending_conversation_actions(ctx)
    msg_id = await send_feishu_text(ctx, "我要做一个个人网站")
    if ctx.dry_run:
        return
    assert ctx.sim is not None
    clarification_card = await ctx.sim.wait_for_reply(
        pattern=r"个人博客|个人简历|作品集|个人主页|哪种类型",
        after_message_id=msg_id,
        timeout=300.0,
    )
    if clarification_card is None or "处理失败" in clarification_card.text:
        raise RuntimeError("initial personal-site clarification failed")

    clarification = await _wait_for_pending_conversation_action(ctx, "clarification")
    options = clarification["payload"].get("options") or []
    selected = next(
        (
            option
            for option in options
            if re.search(r"综合|博客.*作品集|作品集.*博客", str(option.get("label") or ""))
        ),
        None,
    )
    if selected is None:
        raise RuntimeError(f"comprehensive-site option not found: {options!r}")
    await inject_action(
        ctx,
        ctx.args.open_id,
        "",
        "conversation_clarification",
        option_key=str(selected["key"]),
        extras={"pending_action_id": clarification["id"]},
    )
    first_action_card = await ctx.sim.wait_for_reply(
        pattern=r"确认执行范围|执行请求ID.*cact_",
        after_message_id=clarification_card.message_id,
        timeout=300.0,
    )
    if first_action_card is None or "处理失败" in first_action_card.text:
        raise RuntimeError("site selection did not produce an execution proposal")
    first_action = await _wait_for_pending_conversation_action(ctx, "start_delivery")

    revision_message_id = await send_feishu_text(ctx, "上面需要一个个人agent")
    revised_action_card = await ctx.sim.wait_for_reply(
        pattern=r"确认执行范围|执行请求ID.*cact_",
        after_message_id=revision_message_id,
        timeout=300.0,
    )
    if revised_action_card is None:
        failure = await ctx.sim.wait_for_reply(
            pattern=r"处理失败|schema violation|自动重试.*仍无法完成",
            after_message_id=revision_message_id,
            timeout=10.0,
        )
        raise RuntimeError(
            f"personal-Agent revision failed: {failure.text if failure else 'no card'}"
        )

    revised = await _wait_for_pending_conversation_action(
        ctx, "start_delivery", timeout=60.0, exclude_id=first_action["id"]
    )
    payload_text = json.dumps(revised["payload"], ensure_ascii=False).lower()
    if not re.search(r"个人\s*(?:agent|ai\s*助手|助手|智能体)", payload_text):
        raise RuntimeError(f"revision was lost from delivery payload: {revised['payload']!r}")
    for index, decision in enumerate(revised["payload"].get("decisions") or []):
        required = {"key", "question", "answer", "status"}
        if not isinstance(decision, dict) or not required.issubset(decision):
            raise RuntimeError(f"decision {index} is not canonical: {decision!r}")
        if decision["status"] != "confirmed":
            raise RuntimeError(f"decision {index} is not confirmed: {decision!r}")

    await inject_action(
        ctx,
        ctx.args.open_id,
        "",
        "conversation_action",
        extras={"pending_action_id": revised["id"], "decision": "reject"},
    )
    eprint("  personal-Agent revision produced a canonical execution proposal")


async def scenario_coordinator_read_workspace(ctx: ScenarioContext) -> None:
    """Bound chat + arbitrary phrasing: the coordinator must use Read.

    Regression for the keyword-gating removal: with a bound directory the
    coordinator receives a read-only worktree for ANY phrasing. Proof: the
    answer must contain a sentinel token that exists only inside a file in
    the bound directory — impossible without a real Read call.
    """
    eprint("Scenario: coordinator_read_workspace")
    if ctx.dry_run:
        return
    from pathlib import Path
    import shutil

    token = f"E2E-ZEBRA-{int(time.time()) % 100000}"
    fixture_dir = Path.home() / f"mac-e2e-coord-read-{int(time.time())}"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "SENTINEL.md").write_text(
        f"# 哨兵文件\n\n本文件的口令是：{token}\n", encoding="utf-8"
    )
    try:
        # 1. Bind the chat to the fixture directory.
        msg1 = await send_feishu_text(ctx, f"/bind {fixture_dir}")
        reply1 = await ctx.sim.wait_for_reply(
            pattern=r"已将当前会话绑定到|绑定目录失败|不能切换目录",
            after_message_id=msg1,
            timeout=60.0,
        )
        if reply1 is None:
            raise RuntimeError("MAC bot did not reply to /bind <path>")
        if "已将当前会话绑定到" not in reply1.text:
            raise RuntimeError(f"/bind failed: {reply1.text}")
        eprint(f"  bound chat to {fixture_dir}")

        # 2. Ask a question whose answer exists only inside the bound dir,
        #    phrased without any legacy inspect keyword. The final reply card
        #    is degraded by the Feishu message-list API (new-style cards lose
        #    their body), so verify via the persisted conversation turns in
        #    the server DB instead.
        msg2 = await send_feishu_text(
            ctx, "SENTINEL.md 里写的口令是什么？直接说内容。"
        )
        assert msg2 or ctx.dry_run
        db_path = _server_db_path()
        chat_id = ctx.args.chat_id
        deadline = time.monotonic() + 240.0
        answer: str | None = None
        while time.monotonic() < deadline:
            answer = _latest_assistant_reply(db_path, chat_id)
            if answer and (token in answer or "处理失败" in answer):
                break
            await asyncio.sleep(3.0)
        if answer and "处理失败" in answer:
            raise RuntimeError(f"coordinator failed: {answer}")
        if not answer or token not in answer:
            raise RuntimeError(
                "coordinator did not answer with the sentinel token — "
                "Read tool likely unavailable for this phrasing"
            )
        eprint("  coordinator reply contains the sentinel token (Read verified)")

        # 3. Unbind so later scenarios are unaffected.
        msg3 = await send_feishu_text(ctx, "/unbind")
        reply3 = await ctx.sim.wait_for_reply(
            pattern=r"已解绑|不能解绑",
            after_message_id=msg3,
            timeout=60.0,
        )
        if reply3 is None or "已解绑" not in reply3.text:
            eprint("  [warn] /unbind did not succeed; binding may persist")
        else:
            eprint("  unbound chat directory")
    finally:
        shutil.rmtree(fixture_dir, ignore_errors=True)


async def scenario_existing_repo_incremental(ctx: ScenarioContext) -> None:
    """Run a real delivery against an existing Git repository.

    Verifies that MAC inherits the chat binding, preserves existing content,
    commits incremental changes through isolated worktrees, runs active
    acceptance, and completes without a final user acceptance click.
    """
    eprint("Scenario: existing_repo_incremental")
    if ctx.dry_run:
        return
    from pathlib import Path
    import shutil
    import subprocess

    fixture_dir = Path.home() / f"mac-e2e-existing-{int(time.time())}"
    fixture_dir.mkdir(parents=True, exist_ok=False)
    project_id: str | None = None
    try:
        subprocess.run(["git", "init", "-q"], cwd=fixture_dir, check=True)
        subprocess.run(
            ["git", "config", "user.name", "MAC E2E"], cwd=fixture_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "mac-e2e@example.invalid"],
            cwd=fixture_dir,
            check=True,
        )
        original_readme = "# Existing Project\n\nOriginal content must remain.\n"
        (fixture_dir / "README.md").write_text(original_readme, encoding="utf-8")
        (fixture_dir / "legacy.txt").write_text("legacy-data\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", "legacy.txt"], cwd=fixture_dir, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "existing baseline"],
            cwd=fixture_dir,
            check=True,
        )

        bind_message = await send_feishu_text(ctx, f"/bind {fixture_dir}")
        bind_reply = await ctx.sim.wait_for_reply(
            pattern=r"已将当前会话绑定到|绑定目录失败|不能切换目录",
            after_message_id=bind_message,
            timeout=60.0,
        )
        if bind_reply is None or "已将当前会话绑定到" not in bind_reply.text:
            raise RuntimeError(f"could not bind existing repo: {getattr(bind_reply, 'text', None)}")

        anchor_message_id = await _seed_card_anchor(ctx)
        project = await ctx.mac.create_project(
            **_project_create_kwargs(
                ctx,
                name="existing-repo-incremental",
                task_type="iteration",
                description=(
                    "在现有仓库上增量修改：保留 README.md 全部原文，在末尾新增 "
                    "'## MAC Incremental Update' 小节；新增 incremental_marker.txt，"
                    "内容必须为 'mac-existing-repo-pass'；不得删除或改写 legacy.txt。"
                ),
                approval_mode="strict",
                confirmed_brief={
                    "objective": "在现有仓库上完成一个范围明确的增量修改",
                    "scope": [
                        "保留 README.md 全部原文并追加指定小节",
                        "新增包含指定内容的 incremental_marker.txt",
                        "保持 legacy.txt 不变",
                    ],
                    "out_of_scope": ["删除或改写任何现有功能"],
                    "acceptance_criteria": [
                        "README.md 保留原文并包含 ## MAC Incremental Update",
                        "incremental_marker.txt 内容严格等于 mac-existing-repo-pass",
                        "legacy.txt 内容仍严格等于 legacy-data",
                    ],
                },
            )
        )
        project_id = project["id"]
        if Path(project.get("workspace_path", "")).resolve() != fixture_dir.resolve():
            raise RuntimeError(f"project did not inherit bound directory: {project}")
        await ctx.mac.tick(project_id)
        status = await ctx.mac.get_status(project_id)
        if not status.get("pending_approval"):
            await ctx.mac.tick(project_id)

        message_id = anchor_message_id
        for expected_state in (
            "CODING_CONFIRM_PENDING",
            "TESTING_CONFIRM_PENDING",
            "REVIEW_CONFIRM_PENDING",
        ):
            approval_id, _ = await wait_for_approval_at_state(
                ctx,
                project_id,
                expected_state,
                after_message_id=message_id,
                timeout=900.0,
            )
            eprint(f"  {expected_state}: confirm {approval_id}")
            message_id = await send_feishu_text(ctx, f"/confirm {approval_id}")

        acceptance_id, _ = await wait_for_approval_at_state(
            ctx,
            project_id,
            "ACCEPTANCE_PENDING",
            after_message_id=message_id,
            timeout=1800.0,
        )
        eprint(f"  ACCEPTANCE_PENDING: confirm {acceptance_id}")
        await send_feishu_text(ctx, f"/confirm {acceptance_id}")

        completed = await wait_for_state(ctx, project_id, "COMPLETED", timeout=1800.0)
        if completed is None:
            current = await ctx.mac.get_status(project_id)
            raise RuntimeError(
                "existing repo project did not auto-complete after acceptance: "
                f"{current.get('current_state')}"
            )

        readme = (fixture_dir / "README.md").read_text(encoding="utf-8")
        marker = (fixture_dir / "incremental_marker.txt").read_text(
            encoding="utf-8"
        ).strip()
        legacy = (fixture_dir / "legacy.txt").read_text(encoding="utf-8")
        if original_readme.strip() not in readme or "## MAC Incremental Update" not in readme:
            raise RuntimeError("existing README content was lost or update section is missing")
        if marker != "mac-existing-repo-pass" or legacy != "legacy-data\n":
            raise RuntimeError("incremental marker or legacy content is incorrect")
        commit_count = int(
            subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=fixture_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        if commit_count < 2:
            raise RuntimeError("incremental delivery did not create a repository commit")
        eprint(f"  existing repo completed with {commit_count} commits and preserved legacy files")
    finally:
        if project_id:
            try:
                status = await ctx.mac.get_status(project_id)
                if status.get("current_state") not in {"COMPLETED", "FAILED", "CANCELLED"}:
                    await ctx.mac.cancel_project(project_id)
            except Exception:
                pass
        try:
            unbind_message = await send_feishu_text(ctx, "/unbind")
            await ctx.sim.wait_for_reply(
                pattern=r"已解绑|没有绑定目录|不能切换目录",
                after_message_id=unbind_message,
                timeout=60.0,
            )
        except Exception:
            pass
        shutil.rmtree(fixture_dir, ignore_errors=True)


SCENARIOS: dict[str, Any] = {
    "p2p_create": scenario_p2p_create,
    "agent_loop_delivery": scenario_agent_loop_delivery,
    "p2p_create_with_type": scenario_p2p_create_with_type,
    "p2p_create_interactive": scenario_p2p_create_interactive,
    "conversation_delivery": scenario_conversation_delivery,
    "personal_site_clarification": scenario_personal_site_clarification,
    "personal_site_agent_revision": scenario_personal_site_agent_revision,
    "planning_rejection_form": scenario_planning_rejection_form,
    "architecture_confirmation": scenario_architecture_confirmation,
    "reject_without_feedback_advances": scenario_reject_without_feedback_advances,
    "acceptance_question_card": scenario_acceptance_question_card,
    "requirement_custom_question": scenario_requirement_custom_question,
    "runtime_recovery_card": scenario_runtime_recovery_card,
    "active_task_cancel": scenario_active_task_cancel,
    "stale_text_message_ignored": scenario_stale_text_message_ignored,
    "stale_card_action_ignored": scenario_stale_card_action_ignored,
    "out_of_order_text_message_ignored": scenario_out_of_order_text_message_ignored,
    "duplicate_event_idempotent": scenario_duplicate_event_idempotent,
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
    "agent_fail_auto_rework": scenario_agent_fail_auto_rework,
    "bind_dir_agent": scenario_bind_dir_agent,
    "coordinator_read_workspace": scenario_coordinator_read_workspace,
    "existing_repo_incremental": scenario_existing_repo_incremental,
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
