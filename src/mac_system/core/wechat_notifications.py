"""Private reminders through the existing OpenClaw iLink bot login."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from urllib.parse import urlencode, urlparse

import httpx

from mac_system.core.project_service import ProjectService
from mac_system.models import NotificationOutbox

logger = logging.getLogger(__name__)


def _account(service: ProjectService) -> dict:
    directory = service.settings.wechat_credentials_dir.expanduser()
    account = json.loads((directory / "account.json").read_text())
    if not all(account.get(key) for key in ("botToken", "botId", "userId")):
        raise ValueError("WeChat bot login is incomplete")
    base_url = account.get("baseUrl") or "https://ilinkai.weixin.qq.com/"
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.netloc != "ilinkai.weixin.qq.com":
        raise ValueError("Unsupported WeChat bot API host")
    account["baseUrl"] = base_url.rstrip("/") + "/"
    return account


def queue_wechat_reminder(
    service: ProjectService,
    *,
    event_key: str,
    project_id: str,
    interaction_id: str | None = None,
    decision_id: str | None = None,
) -> NotificationOutbox | None:
    """Commit a reminder after Owner review, or for a terminal turn failure.

    Do not copy questions, commands, model output or credentials into reminders.
    Notification errors must not change the native turn's outcome.
    """
    if not service.settings.wechat_notifications_enabled:
        return None
    try:
        project = service.require_project(project_id)
        if interaction_id and decision_id:
            return None
        interaction = None
        if interaction_id:
            interaction = service.get_question_interaction(interaction_id)
            if (interaction is None or interaction.project_id != project_id
                    or interaction.status != "pending"
                    or interaction.requested_to != "user"):
                return None
        if decision_id:
            decision = service.get_project_decision(decision_id)
            if (decision is None or decision.project_id != project_id
                    or decision.status != "requires_user"
                    or (decision.answer_payload or {}).get("continue_execution") is True):
                return None
        account = _account(service)
        if decision_id:
            summary = "有一项选择需要你确认，请到飞书处理。"
        elif interaction is None:
            summary = "本轮执行失败，请到飞书查看错误。"
        elif interaction.stage == "native_approval":
            summary = "有一项审批需要你确认，请到飞书处理。"
        else:
            summary = "有问题需要你回答，请到飞书处理。"
        text = f"MAC · {project.name or project.id}\n{summary}"
        if project.feishu_chat_id:
            text += "\nhttps://applink.feishu.cn/client/chat/open?" + urlencode(
                {"openChatId": project.feishu_chat_id}
            )
        return service.enqueue_notification(
            channel="wechat",
            dedupe_key=f"wechat:{event_key}",
            recipient_id=account["userId"],
            recipient_type="ilink_user_id",
            intent_type="reminder",
            project_id=project_id,
            payload={"text": text, "interaction_id": interaction_id,
                     "decision_id": decision_id},
        )
    except Exception:
        service.session.rollback()
        logger.warning("Could not queue WeChat reminder project=%s", project_id)
        return None


async def retry_wechat_reminder(
    service: ProjectService, notification: NotificationOutbox
) -> None:
    """Drain one intent; ambiguous POST results are never automatically resent."""
    if notification.status not in {"pending", "failed"}:
        return
    if not service.settings.wechat_notifications_enabled:
        service.finish_notification(
            notification.id, error="WeChat reminders disabled", retryable=False,
        )
        return
    payload = notification.payload or {}
    interaction_id = payload.get("interaction_id")
    if interaction_id:
        interaction = service.get_question_interaction(interaction_id)
        if (interaction is None or interaction.status != "pending"
                or interaction.requested_to != "user"
                or interaction.project_id != notification.project_id):
            service.finish_notification(
                notification.id, error="Reminder superseded: interaction resolved",
                retryable=False,
            )
            return
    decision_id = payload.get("decision_id")
    if decision_id:
        decision = service.get_project_decision(decision_id)
        if (decision is None or decision.status != "requires_user"
                or decision.project_id != notification.project_id
                or (decision.answer_payload or {}).get("continue_execution") is True):
            service.finish_notification(
                notification.id, error="Reminder superseded: decision resolved or nonblocking",
                retryable=False,
            )
            return
    claimed = service.claim_notification(notification.id)
    if claimed is None:
        return
    try:
        account = _account(service)
        if account["userId"] != claimed.recipient_id:
            raise ValueError("WeChat recipient changed; refusing queued delivery")
        state_path = service.settings.wechat_credentials_dir.expanduser() / "state.json"
        state = json.loads(state_path.read_text())
        context = (state.get("contextTokens") or {}).get(
            f"{account['botId']}:{claimed.recipient_id}"
        )
        if not context:
            raise ValueError("WeChat conversation context is unavailable")
        client_id = "mac-" + hashlib.sha256(claimed.id.encode()).hexdigest()[:32]
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                account["baseUrl"] + "ilink/bot/sendmessage",
                headers={
                    "AuthorizationType": "ilink_bot_token",
                    "Authorization": "Bearer " + account["botToken"],
                    "X-WECHAT-UIN": base64.b64encode(
                        str(secrets.randbits(32)).encode()
                    ).decode(),
                },
                json={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": claimed.recipient_id,
                        "client_id": client_id,
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": payload["text"]}}],
                        "context_token": context,
                    },
                    "base_info": {"channel_version": "1.0.0"},
                },
            )
        response.raise_for_status()
        result = response.json()
        codes = {key: result[key] for key in ("ret", "errcode") if key in result}
        message_id = result.get("message_id")
        confirmed_id = str(message_id) if (
            (type(message_id) is int and message_id > 0)
            or (isinstance(message_id, str) and message_id.strip())
        ) else None
        # iLink may omit the default success code and return only message_id.
        if ((not codes and not confirmed_id)
                or any(type(value) is not int or value != 0 for value in codes.values())):
            # Never persist an arbitrary provider message containing private data.
            safe_codes = {key: value for key, value in codes.items() if type(value) is int}
            raise ValueError(f"WeChat send rejected or unconfirmed: {safe_codes}")
    except Exception as exc:
        retryable = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        if isinstance(exc, httpx.HTTPStatusError):
            retryable = exc.response.status_code == 429
            error = f"WeChat send HTTP {exc.response.status_code}"
        elif isinstance(exc, ValueError):
            error = str(exc)
        else:
            error = f"WeChat send failed ({type(exc).__name__}); delivery may be unknown"
        service.finish_notification(claimed.id, error=error, retryable=retryable)
        logger.warning("WeChat reminder failed id=%s: %s", claimed.id, error)
        return
    service.finish_notification(claimed.id, provider_message_id=confirmed_id or client_id)
