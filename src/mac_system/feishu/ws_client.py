"""Feishu WebSocket long-connection client.

Mirrors the pattern used in cc-connect's collector: use lark.ws.Client to
maintain an outbound WebSocket to Feishu and receive im.message.receive_v1 and
card.action.trigger events without exposing a public HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from typing import Any

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import ClaudeCodeRuntime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.database import session_scope
from mac_system.feishu.bridge import FeishuBridge
from datetime import datetime, timedelta, timezone

from mac_system.feishu.handlers import (
    FeishuHandlerContext,
    handle_card_action,
    handle_text_message,
)
from mac_system.feishu.models import ButtonAction, TextMessage

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


_bot_open_id: str | None = None


async def _get_bot_open_id() -> str | None:
    """Return the MAC bot's own open_id, fetching and caching it once."""
    global _bot_open_id
    if _bot_open_id is not None:
        return _bot_open_id
    settings = get_settings()
    bridge = FeishuBridge(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        encrypt_key=settings.feishu_encrypt_key,
        verification_token=settings.feishu_verification_token,
    )
    try:
        _bot_open_id = await bridge.get_bot_open_id()
    except Exception:
        logger.exception("Failed to fetch bot open_id")
        _bot_open_id = None
    finally:
        try:
            await bridge.close()
        except Exception:
            pass
    return _bot_open_id


def _strip_mention_placeholders(text: str) -> str:
    """Remove Feishu @ user placeholders and <at> tags from message text."""
    text = re.sub(r"@_user_\d+", "", text)
    text = re.sub(r'<at[^>]*>.*?</at>', "", text)
    return text.strip()


class FeishuWebSocketClient:
    """Runs a lark.ws.Client in a background thread.

    The SDK's WebSocket client blocks the thread it starts on, so this wrapper
    spawns a daemon thread and reconnects automatically on disconnect.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Serialize handling of all events from the same user so that
        # WebSocket messages/cards are processed in the order they were sent.
        self._user_locks: dict[str, threading.Lock] = {}
        # Watermarks for rejecting buffered/old events after startup or reconnect.
        self._started_at = datetime.utcnow()
        self._last_disconnect_at: datetime | None = None

    def start(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            logger.info("Skipping Feishu WS client start during tests")
            return
        if self._thread is not None and self._thread.is_alive():
            logger.info("Feishu WS client already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Feishu WS client thread started")

    def stop(self) -> None:
        self._stop_event.set()
        # The lark.ws.Client only exposes start(); it will reconnect internally
        # until the process exits. Relying on daemon thread termination is
        # sufficient for local development and graceful enough for this MVP.
        logger.info("Feishu WS client stop requested")

    def _run(self) -> None:
        # Import inside the thread so lark_oapi.ws.client captures this thread's
        # event loop, not the FastAPI / uvicorn loop. This avoids the
        # "This event loop is already running" RuntimeError in newer SDKs.
        import asyncio

        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTrigger,
            P2CardActionTriggerResponse,
        )

        # Run the SDK on a loop owned by this thread. lark_oapi captures the
        # current event loop when its ws.client submodule is first imported
        # (which happens in the main thread via lark_oapi.__init__), so we
        # force it to use the loop created for this WS thread.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        lark.ws.client.loop = loop

        def _run_handler_in_thread(coro: Any) -> None:
            """Run an async handler in its own thread/loop so the WS loop stays free."""
            def runner() -> None:
                handler_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(handler_loop)
                try:
                    handler_loop.run_until_complete(coro)
                finally:
                    handler_loop.close()

            threading.Thread(target=runner, daemon=True).start()

        def on_message(event: P2ImMessageReceiveV1) -> None:
            msg = _parse_text_message(event)
            event_id = event.header.event_id if event.header else None
            logger.info(
                "[Feishu WS] received text message open_id=%s text=%s",
                msg.open_id,
                msg.text,
            )
            _run_handler_in_thread(self._handle_message(msg, event_id))

        def on_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse:
            action = _parse_button_action(event)
            event_id = event.header.event_id if event.header else None
            logger.info(
                "[Feishu WS] received card action open_id=%s approval_id=%s action=%s",
                action.open_id,
                action.approval_id,
                action.action,
            )
            # Option selection should update the card in-place and not show a toast.
            # Run synchronously so we can return the updated card in the response.
            if action.action == "select_question_option":
                coro = self._handle_card_action(action, event_id)
                try:
                    card = self._run_sync(coro)
                except Exception:
                    logger.exception("Failed to handle select_question_option")
                    return P2CardActionTriggerResponse()
                if card:
                    return P2CardActionTriggerResponse({"card": {"type": "raw", "data": card}})
                return P2CardActionTriggerResponse()

            if action.action == "conversation_clarification":
                _run_handler_in_thread(self._handle_card_action(action, event_id))
                return P2CardActionTriggerResponse()

            _run_handler_in_thread(self._handle_card_action(action, event_id))
            return P2CardActionTriggerResponse(
                {"toast": {"type": "success", "content": "已收到"}}
            )

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card_action)
            .build()
        )

        cli = lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        def _on_reconnecting() -> None:
            logger.warning("[Feishu WS] connection lost, SDK is reconnecting...")
            self._last_disconnect_at = datetime.utcnow()

        def _on_reconnected() -> None:
            logger.info("[Feishu WS] SDK reconnected")

        cli.on_reconnecting = _on_reconnecting
        cli.on_reconnected = _on_reconnected

        while not self._stop_event.is_set():
            try:
                logger.info("Connecting to Feishu WebSocket...")
                cli.start()
            except Exception as exc:
                logger.exception("Feishu WS client exited: %s", exc)
            if self._stop_event.is_set():
                break
            logger.warning("Feishu WS disconnected, retrying in 5s...")
            time.sleep(5)

    def _user_lock(self, open_id: str) -> threading.Lock:
        """Return a lock that serializes all events from the same user."""
        return self._user_locks.setdefault(open_id, threading.Lock())

    def _is_stale_event(
        self,
        service: ProjectService,
        chat_id: str | None,
        open_id: str,
        event_at: datetime | None,
    ) -> bool:
        """Return True if this event is older than the user's last handled event.

        Staleness is checked against:
        - the per-chat watermark,
        - a global per-user watermark (newer traffic in any chat blocks old events),
        - the process start time and last disconnect time (buffered events from
          before the bot started or before a reconnect are dropped).

        A small grace window allows for sub-second delivery jitter.
        """
        if event_at is None:
            return False

        # Drop events created before the bot started or before the last disconnect.
        # A small grace window avoids rejecting messages that were in flight.
        oldest_allowed = self._started_at - timedelta(minutes=1)
        if self._last_disconnect_at is not None:
            oldest_allowed = max(
                oldest_allowed, self._last_disconnect_at - timedelta(seconds=5)
            )
        if event_at < oldest_allowed:
            return True

        chat_watermark = service.get_last_event_at(chat_id or open_id, open_id)
        global_watermark = service.get_last_event_at(open_id, open_id)
        watermark = chat_watermark
        if watermark is None:
            watermark = global_watermark
        elif global_watermark is not None and global_watermark > watermark:
            watermark = global_watermark

        if watermark is None:
            return False
        grace = 5  # seconds
        watermark_naive = watermark.replace(tzinfo=None) if watermark.tzinfo else watermark
        return event_at < watermark_naive - timedelta(seconds=grace)

    def _update_event_watermark(
        self,
        service: ProjectService,
        chat_id: str,
        open_id: str,
        event_at: datetime,
    ) -> None:
        """Update both the chat-specific and global per-user last-event watermark."""
        service.update_last_event_at(chat_id, open_id, event_at)
        service.update_last_event_at(open_id, open_id, event_at)

    async def _handle_message(self, msg: TextMessage, event_id: str | None = None) -> None:
        lock = self._user_lock(msg.open_id)
        await asyncio.to_thread(lock.acquire)
        try:
            logger.info(
                "[Feishu WS] raw message chat_type=%s chat_id=%s sender=%s mentions=%s text=%r",
                msg.chat_type,
                msg.chat_id,
                msg.open_id,
                msg.mentions,
                msg.text,
            )
            if msg.chat_type == "group":
                bot_open_id = await _get_bot_open_id()
                mentioned_by_list = bot_open_id and any(
                    m.get("id", {}).get("open_id") == bot_open_id
                    or m.get("id") == bot_open_id
                    for m in msg.mentions
                )
                mentioned_by_text = bot_open_id and (
                    f'user_id="{bot_open_id}"' in (msg.text or "")
                )
                if bot_open_id and not (mentioned_by_list or mentioned_by_text):
                    logger.info(
                        "[Feishu WS] ignoring group message without @bot (bot_open_id=%s)",
                        bot_open_id,
                    )
                    return
                msg.is_at_bot = bool(mentioned_by_list or mentioned_by_text)
                msg.text = _strip_mention_placeholders(msg.text)
                msg.is_command = msg.text.startswith("/")

            async with self._handler_context(msg) as ctx:
                if event_id and ctx.service.is_event_processed(event_id):
                    logger.info("[Feishu WS] skipping duplicate message event_id=%s", event_id)
                    return
                service = getattr(ctx, "service", None)
                if service and self._is_stale_event(service, msg.chat_id, msg.open_id, msg.created_at):
                    logger.info(
                        "[Feishu WS] skipping stale message created_at=%s for %s",
                        msg.created_at, msg.open_id,
                    )
                    return
                if event_id and service:
                    service.mark_event_processed(event_id, "message")
                await handle_text_message(ctx, msg)
                if msg.created_at and service:
                    self._update_event_watermark(
                        service, msg.chat_id or msg.open_id, msg.open_id, msg.created_at
                    )
        except Exception:
            logger.exception("Failed to handle Feishu text message")
        finally:
            lock.release()

    async def _handle_card_action(self, action: ButtonAction, event_id: str | None = None) -> dict[str, Any] | None:
        # Selection-only clicks update the card in-place and must return quickly
        # to Feishu. They are idempotent, so skip the per-user serialization lock
        # to avoid blocking the synchronous card response path.
        use_lock = action.action != "select_question_option"
        lock = self._user_lock(action.open_id) if use_lock else None
        if lock:
            await asyncio.to_thread(lock.acquire)
        try:
            async with self._handler_context() as ctx:
                if event_id and ctx.service.is_event_processed(event_id):
                    logger.info("[Feishu WS] skipping duplicate card event_id=%s", event_id)
                    return None
                service = getattr(ctx, "service", None)
                if service and self._is_stale_event(service, action.chat_id, action.open_id, action.created_at):
                    logger.info(
                        "[Feishu WS] skipping stale card action created_at=%s for %s",
                        action.created_at, action.open_id,
                    )
                    return None
                if event_id and service:
                    service.mark_event_processed(event_id, "card")
                result = await handle_card_action(ctx, action)
                if action.created_at and service:
                    self._update_event_watermark(
                        service, action.chat_id or action.open_id, action.open_id, action.created_at
                    )
                return result
        except Exception:
            logger.exception("Failed to handle Feishu card action")
            return None
        finally:
            if lock:
                lock.release()

    def _run_sync(self, coro: Any) -> Any:
        """Run an async coroutine synchronously in a fresh event loop/thread.

        Used for card actions that must return a response (e.g. card update).
        """

        def runner() -> Any:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()


        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runner)
            return future.result(timeout=10.0)

    def _handler_context(self, msg: TextMessage | None = None) -> Any:
        """Return an async context manager that builds handler dependencies."""
        return _WsHandlerContext(self.settings, msg)

    async def inject_text_message(
        self, msg: TextMessage, event_id: str | None = None
    ) -> None:
        """Process a text message through the same path as a real WS event.

        Used by test endpoints so that injected messages respect the same
        staleness, deduplication and per-user serialization logic as live
        Feishu traffic.
        """
        await self._handle_message(msg, event_id)

    async def inject_card_action(
        self, action: ButtonAction, event_id: str | None = None
    ) -> dict[str, Any] | None:
        """Process a card action through the same path as a real WS event."""
        return await self._handle_card_action(action, event_id)


class _WsHandlerContext:
    """Async context manager that creates a fresh handler context per event."""

    def __init__(self, settings: Any, msg: TextMessage | None = None) -> None:
        self.settings = settings
        self.msg = msg
        self._scope = None

    async def __aenter__(self) -> FeishuHandlerContext:
        self._scope = session_scope()
        self.session = self._scope.__enter__()

        bridge = FeishuBridge(
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            encrypt_key=self.settings.feishu_encrypt_key,
            verification_token=self.settings.feishu_verification_token,
        )
        service = ProjectService(self.session)
        worktree_manager = WorktreeManager(self.settings.projects_dir)
        runtime = ClaudeCodeRuntime(
            claude_bin=self.settings.claude_bin,
            anthropic_api_key=self.settings.anthropic_api_key,
            anthropic_auth_token=self.settings.anthropic_auth_token,
            claude_model=self.settings.claude_model,
            anthropic_base_url=self.settings.anthropic_base_url,
            anthropic_default_opus_model=self.settings.anthropic_default_opus_model,
            anthropic_default_sonnet_model=self.settings.anthropic_default_sonnet_model,
            anthropic_default_haiku_model=self.settings.anthropic_default_haiku_model,
            claude_code_effort_level=self.settings.claude_code_effort_level,
            api_timeout_ms=self.settings.api_timeout_ms,
        )
        renderer = PromptRenderer(get_agent_config())
        orchestrator = AgentOrchestrator(
            service,
            worktree_manager,
            runtime,
            renderer,
            feishu_bridge=bridge,
        )
        self._bridge = bridge
        return FeishuHandlerContext(
            bridge=bridge,
            service=service,
            orchestrator=orchestrator,
            chat_id=self.msg.chat_id if self.msg else None,
            chat_type=self.msg.chat_type if self.msg else "p2p",
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            await self._bridge.close()
        except Exception:
            logger.exception("Failed to close FeishuBridge")
        finally:
            self._scope.__exit__(exc_type, exc, tb)


def _parse_feishu_create_time(raw: Any) -> datetime | None:
    """Parse Feishu's millisecond/second create_time string to UTC datetime."""
    if raw is None:
        return None
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return None
    # Feishu uses milliseconds since epoch.
    if ts > 1_000_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _parse_text_message(event: Any) -> TextMessage:
    """Convert a P2ImMessageReceiveV1 event to TextMessage."""
    msg = event.event.message
    content = json.loads(msg.content or "{}")
    text = _strip_mention_placeholders(content.get("text", "").strip())
    sender = event.event.sender
    open_id = ""
    sender_type = "user"
    if sender:
        if sender.sender_id:
            open_id = sender.sender_id.open_id or ""
        sender_type = getattr(sender, "sender_type", "user") or "user"

    chat_id = getattr(msg, "chat_id", None)
    chat_type = getattr(msg, "chat_type", "p2p") or "p2p"
    raw_mentions = getattr(msg, "mentions", []) or []
    mentions: list[dict[str, Any]] = []
    for m in raw_mentions:
        if isinstance(m, dict):
            mentions.append(m)
        else:
            # lark_oapi turns mentions into MentionEvent objects over WebSocket
            mention_id = getattr(m, "id", None)
            mentions.append(
                {
                    "key": getattr(m, "key", ""),
                    "id": {"open_id": getattr(mention_id, "open_id", "")} if mention_id else {},
                    "name": getattr(m, "name", ""),
                }
            )

    return TextMessage(
        open_id=open_id,
        message_id=msg.message_id or "",
        text=text,
        is_command=text.startswith("/"),
        parent_id=getattr(msg, "parent_id", None) or getattr(msg, "root_id", None),
        chat_id=chat_id,
        chat_type=chat_type,
        sender_type=sender_type,
        mentions=mentions,
        created_at=_parse_feishu_create_time(getattr(msg, "create_time", None)),
    )


def _parse_button_action(event: Any) -> ButtonAction:
    """Convert a P2CardActionTrigger event to ButtonAction."""
    action = event.event.action
    value = action.value or {}
    operator = event.event.operator
    open_id = operator.open_id if operator else ""
    reserved = {
        "approval_id",
        "action",
        "state",
        "decision_id",
        "option_key",
        "chat_id",
        "chat_type",
    }
    extras = {
        k: v
        for k, v in value.items()
        if k not in reserved
    }
    form_value = getattr(action, "form_value", None) or {}
    if hasattr(form_value, "items"):
        extras.update({k: v for k, v in form_value.items() if k not in extras})
    context = getattr(event.event, "context", None)
    message_id = getattr(context, "open_message_id", None) if context else None
    action_obj = getattr(event.event, "action", None)
    return ButtonAction(
        open_id=open_id,
        approval_id=value.get("approval_id", ""),
        action=value.get("action", ""),
        state=value.get("state", ""),
        decision_id=value.get("decision_id"),
        option_key=value.get("option_key"),
        extras=extras,
        chat_id=value.get("chat_id"),
        chat_type=value.get("chat_type", "p2p") or "p2p",
        message_id=message_id,
        created_at=_parse_feishu_create_time(getattr(action_obj, "create_time", None)),
    )
