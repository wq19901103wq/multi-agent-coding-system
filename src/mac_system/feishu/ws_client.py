"""Feishu WebSocket long-connection client.

Mirrors the pattern used in cc-connect's collector: use lark.ws.Client to
maintain an outbound WebSocket to Feishu and receive im.message.receive_v1 and
card.action.trigger events without exposing a public HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import json
import concurrent.futures
import logging
import multiprocessing
import os
import re
import signal
import threading
import time
import uuid
from multiprocessing.connection import Connection
from typing import Any

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import build_agent_runtime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.database import session_scope
from mac_system.feishu.accounts import (
    FeishuBotAccount,
    create_feishu_bridge,
    get_feishu_bot_account,
)
from datetime import datetime, timezone

from mac_system.feishu.handlers import (
    FeishuHandlerContext,
    _deliver_to_root,
    handle_card_action,
    handle_text_message,
)
from mac_system.feishu import ws_status
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import ProjectOwnerReview

logger = logging.getLogger(__name__)


def _run_account_process(
    account: FeishuBotAccount,
    startup_root_turns: tuple[dict[str, Any], ...] = (),
    startup_owner_reviews: tuple[str, ...] = (),
    message_connection: Connection | None = None,
) -> None:
    """Own one SDK-global event loop in one OS process.

    lark-oapi 1.x keeps its WebSocket loop in module-global state. Threads are
    therefore insufficient for multiple bot accounts: one client can replace
    another client's loop. A process is the smallest reliable isolation
    boundary until the SDK makes the loop instance-local.
    """

    from mac_system.utils.redaction import install_global_log_redaction

    install_global_log_redaction()

    def request_graceful_shutdown(_signum: int, _frame: Any) -> None:
        # multiprocessing.Process.terminate sends SIGTERM. Unwind into the
        # finally block so native turns can reach turn/completed before the
        # account process exits and another process resumes their threads.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, request_graceful_shutdown)

    from mac_system.core.root_agent import (
        close_root_threads,
        register_root_service_loop,
    )

    service_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_service_loop() -> None:
        asyncio.set_event_loop(service_loop)
        register_root_service_loop(service_loop)
        ready.set()
        service_loop.run_forever()

    service_thread = threading.Thread(target=run_service_loop, daemon=True)
    service_thread.start()
    ready.wait(timeout=5)
    if message_connection is not None:
        threading.Thread(
            target=_serve_project_messages,
            args=(account, message_connection, service_loop),
            daemon=True,
        ).start()
    try:
        if startup_root_turns:
            from mac_system.core.root_agent import schedule_root_turn

            settings = get_settings()
            runtime = build_agent_runtime(settings, get_agent_config())
            with session_scope() as startup_session:
                bind = startup_session.get_bind()
            for job in startup_root_turns:
                schedule_root_turn(
                    bind,
                    runtime,
                    settings,
                    str(job["project_id"]),
                    str(job["trigger"]),
                    visible_request=str(job["visible_request"]),
                    request_origin="system_recovery",
                    parent_decision_id=(
                        str(job["parent_decision_id"])
                        if job.get("parent_decision_id")
                        else None
                    ),
                )
            logger.warning(
                "Scheduled %s interrupted native Codex turn(s) on Feishu account process %s",
                len(startup_root_turns),
                account.id,
            )
        if startup_owner_reviews:
            from mac_system.core.root_agent import schedule_project_owner_review

            settings = get_settings()
            runtime = build_agent_runtime(settings, get_agent_config())
            with session_scope() as startup_session:
                bind = startup_session.get_bind()
            for review_id in startup_owner_reviews:
                schedule_project_owner_review(bind, runtime, settings, review_id)
            logger.warning(
                "Scheduled %s interrupted Project Owner review(s) on Feishu account process %s",
                len(startup_owner_reviews),
                account.id,
            )
        FeishuWebSocketClient(account)._run()
    finally:
        try:
            future = asyncio.run_coroutine_threadsafe(
                close_root_threads(), service_loop
            )
            future.result(timeout=45)
        except Exception:
            logger.warning(
                "Failed to close account-scoped Root threads account=%s",
                account.id,
                exc_info=True,
            )
        register_root_service_loop(None)
        service_loop.call_soon_threadsafe(service_loop.stop)
        service_thread.join(timeout=5)


async def _deliver_project_message(
    account: FeishuBotAccount, project_id: str, content: str
) -> None:
    # Run on the account's service loop, where the native handle and project
    # lock already live. Never resume the same thread in the API process.
    async with _WsHandlerContext(get_settings(), account) as ctx:
        project = ctx.service.require_project(project_id)
        if project.bot_account_id != account.id or project.status == "cancelled":
            raise ValueError("Project is unavailable on this account")
        await _deliver_to_root(ctx, project, content)


async def _schedule_account_owner_review(
    account: FeishuBotAccount, project_id: str, review_id: str
) -> None:
    from mac_system.core.root_agent import schedule_project_owner_review

    settings = get_settings()
    with session_scope() as session:
        service = ProjectService(session)
        project = service.require_project(project_id)
        review = session.get(ProjectOwnerReview, review_id)
        if project.bot_account_id != account.id or review is None or review.project_id != project_id:
            raise ValueError("Project Owner review is unavailable on this account")
        bind = session.get_bind()
    runtime = build_agent_runtime(settings, get_agent_config())
    schedule_project_owner_review(bind, runtime, settings, review_id)


def _serve_project_messages(
    account: FeishuBotAccount,
    connection: Connection,
    service_loop: asyncio.AbstractEventLoop,
) -> None:
    try:
        while True:
            request = connection.recv()
            if len(request) == 3:
                request_id, project_id, content = request
                operation = "message"
            else:
                request_id, operation, project_id, content = request
            try:
                if operation == "message":
                    handler = _deliver_project_message(account, project_id, content)
                elif operation == "owner_review":
                    handler = _schedule_account_owner_review(account, project_id, content)
                else:
                    raise ValueError("Unknown account process operation")
                asyncio.run_coroutine_threadsafe(
                    handler, service_loop
                ).result()
            except Exception:
                logger.exception("Account message delivery failed project=%s", project_id)
                connection.send((request_id, False))
            else:
                connection.send((request_id, True))
    except (EOFError, OSError):
        return
    finally:
        connection.close()


def _card_action_audit_detail(action: ButtonAction) -> str:
    """Persist the exact non-free-text choice needed to audit attribution."""

    return json.dumps(
        {
            "decision_id": action.decision_id,
            "interaction_id": (action.extras or {}).get("interaction_id"),
            "option_key": action.option_key,
            "question_id": (action.extras or {}).get("question_id"),
            "state": action.state,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _cap_sdk_reconnect_delay(client: Any) -> None:
    """Keep the Feishu SDK's private reconnect loop responsive.

    The SDK exposes ``auto_reconnect`` but no public retry-delay setting; the
    server-provided defaults can otherwise leave this long-lived client asleep
    for minutes after a transient disconnect. Attribute checks keep this small
    compatibility shim harmless if the SDK changes.
    """

    if hasattr(client, "_reconnect_interval"):
        client._reconnect_interval = 5
    if hasattr(client, "_reconnect_nonce"):
        client._reconnect_nonce = 1


_bot_open_ids: dict[str, str] = {}


async def _get_bot_open_id(account: FeishuBotAccount | None = None) -> str | None:
    """Return the MAC bot's own open_id, fetching and caching it once."""
    settings = get_settings()
    account = account or get_feishu_bot_account(settings)
    if account is None:
        return None
    if account.id in _bot_open_ids:
        return _bot_open_ids[account.id]
    bridge = create_feishu_bridge(settings, account.id)
    if bridge is None:
        return None
    try:
        bot_open_id = await bridge.get_bot_open_id()
        if bot_open_id:
            _bot_open_ids[account.id] = bot_open_id
        return bot_open_id
    except Exception:
        logger.exception("Failed to fetch bot open_id")
    finally:
        try:
            await bridge.close()
        except Exception:
            pass
    return None


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

    def __init__(
        self,
        account: FeishuBotAccount | None = None,
        *,
        startup_root_turns: tuple[dict[str, Any], ...] = (),
        startup_owner_reviews: tuple[str, ...] = (),
    ) -> None:
        self.settings = get_settings()
        self.account = account or get_feishu_bot_account(self.settings)
        if self.account is None:
            raise ValueError("Feishu bot account is not configured")
        self._startup_root_turns = tuple(startup_root_turns)
        self._startup_owner_reviews = tuple(startup_owner_reviews)
        self._thread: threading.Thread | None = None
        self._process: multiprocessing.Process | None = None
        self._message_connection: Connection | None = None
        self._message_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Opportunistically serialize ordinary turns from the same user.  A
        # turn may launch a multi-minute Agent run, so later turns must be
        # allowed through when this lock is already busy; durable interaction
        # and approval records provide the authoritative idempotency boundary.
        self._user_locks: dict[str, threading.Lock] = {}
        self._project_workers: set[str] = set()
        self._project_worker_pending: set[str] = set()
        self._project_worker_lock = threading.Lock()

    def start(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            logger.info("Skipping Feishu WS client start during tests")
            return
        if self._process is not None and self._process.is_alive():
            logger.info("Feishu WS client already running")
            return
        self._stop_event.clear()
        context = multiprocessing.get_context("spawn")
        self._message_connection, child_connection = context.Pipe()
        self._process = context.Process(
            target=_run_account_process,
            args=(
                self.account,
                self._startup_root_turns,
                self._startup_owner_reviews,
                child_connection,
            ),
            name=f"mac-feishu-{self.account.id}",
            daemon=True,
        )
        self._process.start()
        child_connection.close()
        logger.info(
            "Feishu WS account process started account=%s pid=%s",
            self.account.id,
            self._process.pid,
        )

    async def submit_project_message(self, project_id: str, content: str) -> None:
        await asyncio.to_thread(self._submit_project_message, project_id, content)

    async def submit_owner_review(self, project_id: str, review_id: str) -> None:
        await asyncio.to_thread(
            self._submit_project_message, project_id, review_id, "owner_review"
        )

    def _submit_project_message(
        self, project_id: str, content: str, operation: str = "message"
    ) -> None:
        with self._message_lock:
            connection = self._message_connection
            if (
                self._process is None
                or not self._process.is_alive()
                or connection is None
            ):
                raise RuntimeError("Project account process is unavailable")
            request_id = uuid.uuid4().hex
            connection.send(
                (request_id, project_id, content)
                if operation == "message"
                else (request_id, operation, project_id, content)
            )
            deadline = time.monotonic() + 30
            while connection.poll(max(0, deadline - time.monotonic())):
                reply_id, accepted = connection.recv()
                if reply_id != request_id:
                    continue  # Late acknowledgement of an earlier timed-out request.
                if not accepted:
                    raise RuntimeError("Project account rejected message delivery")
                return
            # The request may already have been accepted. Do not fall back to
            # another runtime or replay it after an ambiguous acknowledgement.
            raise TimeoutError("Project message acknowledgement timed out")

    def stop(self) -> None:
        self._stop_event.set()
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=50)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        self._process = None
        if self._message_connection is not None:
            self._message_connection.close()
            self._message_connection = None
        ws_status.remove_status(self.account.id)
        logger.info("Feishu WS client stopped account=%s", self.account.id)

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
                "[Feishu WS:%s] received text message open_id=%s text=%s",
                self.account.id,
                msg.open_id,
                msg.text,
            )
            _run_handler_in_thread(self._handle_message(msg, event_id))

        def on_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse:
            action = _parse_button_action(event)
            event_id = event.header.event_id if event.header else None
            logger.info(
                "[Feishu WS:%s] received card action open_id=%s approval_id=%s action=%s",
                self.account.id,
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
            self.account.app_id,
            self.account.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        def _on_reconnecting() -> None:
            logger.warning("[Feishu WS] connection lost, SDK is reconnecting...")
            _cap_sdk_reconnect_delay(cli)

        def _on_reconnected() -> None:
            logger.info("[Feishu WS] SDK reconnected")

        cli.on_reconnecting = _on_reconnecting
        cli.on_reconnected = _on_reconnected

        status_poller = threading.Thread(
            target=self._poll_connection_status,
            args=(cli,),
            name=f"mac-ws-status-{self.account.id}",
            daemon=True,
        )
        status_poller.start()

        while not self._stop_event.is_set():
            try:
                logger.info(
                    "Connecting to Feishu WebSocket account=%s...", self.account.id
                )
                cli.start()
            except Exception as exc:
                logger.exception("Feishu WS client exited: %s", exc)
            if self._stop_event.is_set():
                break
            logger.warning("Feishu WS disconnected, retrying in 5s...")
            time.sleep(5)

    def _poll_connection_status(self, cli: Any) -> None:
        """Publish WS connection state for /health and notice long offline windows.

        The SDK exposes no reliable connected/disconnected callback for the
        first connect, so poll its private connection handle. This is advisory
        telemetry only; delivery correctness never depends on it.
        """
        account_id = self.account.id
        was_connected: bool | None = None
        disconnected_since: float | None = None
        connected_since: float | None = None
        while not self._stop_event.is_set():
            connected = getattr(cli, "_conn", None) is not None
            now = time.time()
            if connected != was_connected:
                if connected:
                    connected_since = now
                elif was_connected is True:
                    disconnected_since = now
                    connected_since = None
                if (
                    connected
                    and disconnected_since is not None
                    and now - disconnected_since >= ws_status.OFFLINE_NOTICE_MINUTES * 60
                ):
                    self._notify_offline_window(disconnected_since, now)
                if connected:
                    disconnected_since = None
                was_connected = connected
            # /health treats a stale status file as unknown. Refresh the
            # heartbeat even when the socket state has not changed, otherwise
            # every healthy connection appears disconnected after two minutes.
            try:
                ws_status.write_status(
                    account_id,
                    connected=connected,
                    disconnected_since=disconnected_since,
                    connected_since=connected_since,
                    pid=os.getpid(),
                )
            except Exception:
                logger.warning("Failed to write WS status", exc_info=True)
            self._stop_event.wait(5)

    def _notify_offline_window(
        self, disconnected_since: float, connected_at: float
    ) -> None:
        """Best-effort notice to recently active chats after a long offline window.

        Feishu does not replay events forever; messages sent while the WS was
        down may simply be gone. Tell the user honestly which window was
        missed instead of staying silent. Failures never block reconnect.
        """
        minutes = max(1, int(round((connected_at - disconnected_since) / 60)))

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bridge = None
            try:
                bridge = create_feishu_bridge(self.settings, self.account.id)
                with session_scope() as session:
                    service = ProjectService(
                        session, bot_account_id=self.account.id
                    )
                    chat_ids = service.list_offline_notice_targets()
                if not chat_ids:
                    return
                start = datetime.fromtimestamp(disconnected_since).strftime("%H:%M")
                end = datetime.fromtimestamp(connected_at).strftime("%H:%M")
                text = (
                    f"⚠️ MAC 于 {start}–{end} 离线约 {minutes} 分钟，"
                    "期间发来的消息可能未送达，如有需要请重发。"
                )
                for chat_id in chat_ids:
                    try:
                        loop.run_until_complete(
                            bridge.send_text_to_chat(chat_id, text)
                        )
                    except Exception:
                        logger.warning(
                            "Offline notice to chat %s failed", chat_id, exc_info=True
                        )
            except Exception:
                logger.exception("Failed to send offline window notice")
            finally:
                if bridge is not None:
                    try:
                        loop.run_until_complete(bridge.close())
                    except Exception:
                        pass
                loop.close()

        threading.Thread(
            target=runner, name="mac-offline-notice", daemon=True
        ).start()

    def _user_lock(self, open_id: str) -> threading.Lock:
        """Return a lock that serializes all events from the same user."""
        return self._user_locks.setdefault(open_id, threading.Lock())

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

    def _log_feishu_event(
        self,
        service: ProjectService | None,
        *,
        open_id: str,
        chat_id: str | None,
        chat_type: str,
        event_type: str,
        event_id: str | None,
        message_id: str | None,
        text: str | None,
        action: str | None,
        feishu_created_at: datetime | None,
        status: str,
        detail: str | None = None,
    ) -> None:
        """Persist an audit log of a Feishu event. Failures are swallowed."""
        if service is None:
            return
        try:
            service.log_feishu_event(
                open_id=open_id,
                chat_id=chat_id,
                chat_type=chat_type,
                event_type=event_type,
                event_id=event_id,
                message_id=message_id,
                text=text,
                action=action,
                feishu_created_at=feishu_created_at,
                status=status,
                detail=detail,
            )
        except Exception:
            logger.exception("Failed to persist Feishu event log")

    async def _handle_message(self, msg: TextMessage, event_id: str | None = None) -> None:
        # A cancellation command must always be able to interrupt a long-running
        # handler.  Ordinary turns take the per-user lock only when it is free:
        # once a prior turn launches an Agent, later status questions,
        # corrections, and unrelated questions must keep the chat responsive.
        normalized_text = _strip_mention_placeholders(msg.text or "")
        is_priority_cancel = (
            normalized_text in {"/cancel", "/stop", "/x", "/reset"}
            or normalized_text.startswith("/cancel ")
            or normalized_text.startswith("/stop ")
            or normalized_text.startswith("/x ")
        )
        lock = None if is_priority_cancel else self._user_lock(msg.open_id)
        lock_acquired = bool(lock and lock.acquire(blocking=False))
        try:
            logger.info(
                "[Feishu WS:%s] raw message chat_type=%s chat_id=%s sender=%s mentions=%s text=%r",
                getattr(getattr(self, "account", None), "id", "default"),
                msg.chat_type,
                msg.chat_id,
                msg.open_id,
                msg.mentions,
                msg.text,
            )
            if msg.chat_type == "group":
                bot_open_id = await _get_bot_open_id(self.account)
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
                service = getattr(ctx, "service", None)
                register_identity = getattr(service, "register_user_identity", None)
                if register_identity is not None:
                    register_identity(
                        msg.open_id,
                        user_id=msg.user_id,
                        union_id=msg.union_id,
                    )
                self._log_feishu_event(
                    service,
                    open_id=msg.open_id,
                    chat_id=msg.chat_id,
                    chat_type=msg.chat_type,
                    event_type="message",
                    event_id=event_id,
                    message_id=msg.message_id,
                    text=msg.text,
                    action=None,
                    feishu_created_at=msg.created_at,
                    status="received",
                )
                if event_id and ctx.service.is_event_processed(event_id):
                    logger.info("[Feishu WS] skipping duplicate message event_id=%s", event_id)
                    self._log_feishu_event(
                        service,
                        open_id=msg.open_id,
                        chat_id=msg.chat_id,
                        chat_type=msg.chat_type,
                        event_type="message",
                        event_id=event_id,
                        message_id=msg.message_id,
                        text=msg.text,
                        action=None,
                        feishu_created_at=msg.created_at,
                        status="duplicate",
                    )
                    return
                if event_id and service:
                    service.mark_event_processed(event_id, "message")
                await handle_text_message(ctx, msg)
                self._log_feishu_event(
                    service,
                    open_id=msg.open_id,
                    chat_id=msg.chat_id,
                    chat_type=msg.chat_type,
                    event_type="message",
                    event_id=event_id,
                    message_id=msg.message_id,
                    text=msg.text,
                    action=None,
                    feishu_created_at=msg.created_at,
                    status="processed",
                )
                if msg.created_at and service:
                    self._update_event_watermark(
                        service, msg.chat_id or msg.open_id, msg.open_id, msg.created_at
                    )
        except Exception as exc:
            logger.exception("Failed to handle Feishu text message")
            self._log_feishu_event(
                None,
                open_id=msg.open_id,
                chat_id=msg.chat_id,
                chat_type=msg.chat_type,
                event_type="message",
                event_id=event_id,
                message_id=msg.message_id,
                text=msg.text,
                action=None,
                feishu_created_at=msg.created_at,
                status="error",
                detail=str(exc)[:500],
            )
        finally:
            if lock_acquired and lock:
                lock.release()

    async def _handle_card_action(self, action: ButtonAction, event_id: str | None = None) -> dict[str, Any] | None:
        # Card actions are idempotently claimed by their Approval/Interaction
        # records.  They must not hold the per-user text-message lock: a
        # confirmation can launch a multi-minute Agent tick, during which the
        # user still needs to query status, add a correction, or cancel.
        lock = None
        try:
            async with self._handler_context() as ctx:
                service = getattr(ctx, "service", None)
                register_identity = getattr(service, "register_user_identity", None)
                if register_identity is not None:
                    register_identity(
                        action.open_id,
                        user_id=action.user_id,
                        union_id=action.union_id,
                    )
                audit_detail = _card_action_audit_detail(action)
                self._log_feishu_event(
                    service,
                    open_id=action.open_id,
                    chat_id=action.chat_id,
                    chat_type=action.chat_type,
                    event_type="card",
                    event_id=event_id,
                    message_id=action.message_id,
                    text=None,
                    action=action.action,
                    feishu_created_at=action.created_at,
                    status="received",
                    detail=audit_detail,
                )
                if event_id and ctx.service.is_event_processed(event_id):
                    logger.info("[Feishu WS] skipping duplicate card event_id=%s", event_id)
                    self._log_feishu_event(
                        service,
                        open_id=action.open_id,
                        chat_id=action.chat_id,
                        chat_type=action.chat_type,
                        event_type="card",
                        event_id=event_id,
                        message_id=action.message_id,
                        text=None,
                        action=action.action,
                        feishu_created_at=action.created_at,
                        status="duplicate",
                        detail=audit_detail,
                    )
                    return None
                if event_id and service:
                    service.mark_event_processed(event_id, "card")
                result = await handle_card_action(ctx, action)
                self._log_feishu_event(
                    service,
                    open_id=action.open_id,
                    chat_id=action.chat_id,
                    chat_type=action.chat_type,
                    event_type="card",
                    event_id=event_id,
                    message_id=action.message_id,
                    text=None,
                    action=action.action,
                    feishu_created_at=action.created_at,
                    status="processed",
                    detail=audit_detail,
                )
                if action.created_at and service:
                    self._update_event_watermark(
                        service, action.chat_id or action.open_id, action.open_id, action.created_at
                    )
                return result
        except Exception as exc:
            logger.exception("Failed to handle Feishu card action")
            self._log_feishu_event(
                None,
                open_id=action.open_id,
                chat_id=action.chat_id,
                chat_type=action.chat_type,
                event_type="card",
                event_id=event_id,
                message_id=action.message_id,
                text=None,
                action=action.action,
                feishu_created_at=action.created_at,
                status="error",
                detail=str(exc)[:500],
            )
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
        return _WsHandlerContext(
            self.settings,
            self.account,
            msg,
            background_scheduler=self._schedule_project_tick,
        )

    def _schedule_project_tick(self, project_id: str) -> None:
        """Run durable project work outside the short-lived message callback.

        One worker owns a project at a time.  Repeated level-triggered wakeups
        are coalesced into one pending rerun, so a state change that races with
        worker shutdown cannot leave an active project without an Agent task.
        """
        with self._project_worker_lock:
            if project_id in self._project_workers:
                self._project_worker_pending.add(project_id)
                return
            self._project_workers.add(project_id)

        def runner() -> None:
            worker_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(worker_loop)

            async def run_until_boundary() -> None:
                async with _WsHandlerContext(
                    self.settings,
                    self.account,
                    background_scheduler=self._schedule_project_tick,
                ) as ctx:
                    await ctx.orchestrator.tick_until_stable(project_id, max_ticks=50)

            try:
                worker_loop.run_until_complete(run_until_boundary())
            except Exception:
                logger.exception("Project background worker failed for %s", project_id)
            finally:
                worker_loop.close()
                with self._project_worker_lock:
                    self._project_workers.discard(project_id)
                    rerun = project_id in self._project_worker_pending
                    self._project_worker_pending.discard(project_id)
                if rerun and not self._stop_event.is_set():
                    self._schedule_project_tick(project_id)

        threading.Thread(
            target=runner,
            name=f"mac-project-{project_id[-12:]}",
            daemon=True,
        ).start()

    async def inject_text_message(
        self, msg: TextMessage, event_id: str | None = None
    ) -> None:
        """Process a text message through the same path as a real WS event.

        Used by test endpoints so injected messages respect the same event-id
        deduplication and per-user serialization logic as live Feishu traffic.
        """
        await self._handle_message(msg, event_id)

    async def inject_card_action(
        self, action: ButtonAction, event_id: str | None = None
    ) -> dict[str, Any] | None:
        """Process a card action through the same path as a real WS event."""
        return await self._handle_card_action(action, event_id)


class _WsHandlerContext:
    """Async context manager that creates a fresh handler context per event."""

    def __init__(
        self,
        settings: Any,
        account: FeishuBotAccount,
        msg: TextMessage | None = None,
        *,
        background_scheduler: Any = None,
    ) -> None:
        self.settings = settings
        self.account = account
        self.msg = msg
        self.background_scheduler = background_scheduler
        self._scope = None

    async def __aenter__(self) -> FeishuHandlerContext:
        self._scope = session_scope()
        self.session = self._scope.__enter__()

        bridge = create_feishu_bridge(self.settings, self.account.id)
        if bridge is None:
            raise RuntimeError(f"Feishu bot account {self.account.id!r} is unavailable")
        service = ProjectService(self.session, bot_account_id=self.account.id)
        agent_config = get_agent_config()
        runtime = build_agent_runtime(self.settings, agent_config)
        orchestrator = AgentOrchestrator(
            service,
            runtime,
            feishu_bridge=bridge,
            background_scheduler=self.background_scheduler,
        )
        self._bridge = bridge
        return FeishuHandlerContext(
            bridge=bridge,
            service=service,
            orchestrator=orchestrator,
            bot_account_id=self.account.id,
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
            user_id = getattr(sender.sender_id, "user_id", None)
            union_id = getattr(sender.sender_id, "union_id", None)
        sender_type = getattr(sender, "sender_type", "user") or "user"
    else:
        user_id = None
        union_id = None

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
        # ``parent_id`` is the message the user explicitly replied to.
        # ``root_id`` only identifies the root of a Feishu thread and may be
        # present on ordinary follow-up messages; treating it as a reply target
        # silently switches projects back to an old card.
        parent_id=getattr(msg, "parent_id", None),
        chat_id=chat_id,
        chat_type=chat_type,
        sender_type=sender_type,
        mentions=mentions,
        created_at=_parse_feishu_create_time(getattr(msg, "create_time", None)),
        user_id=user_id,
        union_id=union_id,
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
        user_id=getattr(operator, "user_id", None) if operator else None,
        union_id=getattr(operator, "union_id", None) if operator else None,
    )
