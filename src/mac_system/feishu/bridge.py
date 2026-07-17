import asyncio
import json
import re
import time
from typing import Any

import httpx

from mac_system.feishu.card import (
    build_confirmation_card,
    build_content_card,
    build_input_card,
    build_status_card,
)
from mac_system.feishu.crypto import decrypt_feishu, verify_signature
from mac_system.feishu.models import ButtonAction, FeishuEvent, TextMessage


_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")
_MENTION_AT_TAG_RE = re.compile(r'<at[^>]*>.*?</at>')


def _strip_mention_placeholders(text: str) -> str:
    """Remove Feishu @ user placeholders and <at> tags from message text."""
    text = _MENTION_PLACEHOLDER_RE.sub("", text)
    text = _MENTION_AT_TAG_RE.sub("", text)
    return text.strip()


class FeishuBridgeError(Exception):
    pass


class FeishuBridge:
    """Minimal Feishu/Lark bridge for MAC System.

    Supports sending text and interactive cards, and parsing event callbacks.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        encrypt_key: str | None = None,
        verification_token: str | None = None,
        base_url: str = "https://open.feishu.cn",
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)
        self._token: str | None = None
        self._token_expire_at: float = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_tenant_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = await self._client.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuBridgeError(f"Failed to get tenant token: {data}")

        self._token = data["tenant_access_token"]
        # expire is in seconds; keep a 60s buffer
        self._token_expire_at = time.time() + data.get("expire", 7200) - 60
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                token = await self._get_tenant_token()
                resp = await self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json_data,
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise FeishuBridgeError(f"Feishu API error: {data}")
                return data
            except (
                httpx.HTTPStatusError,
                httpx.NetworkError,
                httpx.TimeoutException,
            ) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        raise
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise last_exc

    async def get_bot_open_id(self) -> str | None:
        """Return the open_id of the bot itself, caching the result."""
        if getattr(self, "_bot_open_id", None):
            return self._bot_open_id
        data = await self._request("GET", "/open-apis/bot/v3/info")
        bot = data.get("bot") or data.get("data", {}).get("bot", {})
        self._bot_open_id = bot.get("open_id")
        return self._bot_open_id

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> str:
        """Send a plain text message. Returns message_id."""
        data = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_data={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        return data["data"]["message_id"]

    async def send_text_to_chat(self, chat_id: str, text: str) -> str:
        """Convenience helper to send a plain text message to a chat."""
        return await self.send_text(chat_id, text, receive_id_type="chat_id")

    async def _send_interactive(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> str:
        """Send an interactive card. Returns message_id."""
        data = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_data={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        return data["data"]["message_id"]

    async def _send_interactive_to_chat(
        self, chat_id: str, card: dict[str, Any]
    ) -> str:
        """Convenience helper to send an interactive card to a chat."""
        return await self._send_interactive(chat_id, card, receive_id_type="chat_id")

    async def send_content_card(
        self,
        open_id: str,
        title: str,
        content: str,
        page: int = 1,
        total: int = 1,
        color: str = "blue",
        receive_id_type: str = "open_id",
    ) -> str:
        """Send a plain display card for a chunk of markdown content."""
        card = build_content_card(title, content, page=page, total=total, color=color)
        return await self._send_interactive(open_id, card, receive_id_type=receive_id_type)

    async def send_confirmation_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        summary: str,
        details: dict[str, Any],
        options: list[dict[str, Any]],
        receive_id_type: str = "open_id",
        chat_id: str | None = None,
        chat_type: str = "p2p",
        show_actions: bool = True,
    ) -> str:
        state = details.get("state", "")
        card = build_confirmation_card(
            title, summary, details, options, approval_id, state,
            chat_id=chat_id, chat_type=chat_type, show_actions=show_actions,
        )
        return await self._send_interactive(open_id, card, receive_id_type=receive_id_type)

    async def send_input_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        question: str,
        context: str,
        receive_id_type: str = "open_id",
        chat_id: str | None = None,
        chat_type: str = "p2p",
    ) -> str:
        card = build_input_card(
            title, question, context, approval_id, "",
            chat_id=chat_id, chat_type=chat_type,
        )
        return await self._send_interactive(open_id, card, receive_id_type=receive_id_type)

    async def send_status_card(
        self,
        open_id: str,
        project_id: str,
        current_state: str,
        display: str,
        recent_events: list[dict[str, Any]],
    ) -> str:
        card = build_status_card(
            f"项目状态：{project_id}",
            project_id,
            current_state,
            display,
            recent_events,
        )
        return await self._send_interactive(open_id, card)

    async def update_card(self, message_id: str, card_content: dict[str, Any]) -> None:
        """Update an existing interactive card."""
        await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            json_data={"content": json.dumps(card_content, ensure_ascii=False)},
        )

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        """Attach a processing reaction to the user's original message."""
        if not message_id:
            return None
        data = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reactions",
            json_data={"reaction_type": {"emoji_type": emoji_type}},
        )
        return (data.get("data") or {}).get("reaction_id")

    async def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        """Remove a reaction previously created by this bot."""
        if not message_id or not reaction_id:
            return
        await self._request(
            "DELETE",
            f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
        )

    def _extract_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the inner event object from a decrypted payload."""
        if "event" in payload:
            return payload["event"]
        if "action" in payload:
            # card action trigger has action at top level
            return payload
        return payload

    def parse_event(self, payload: dict[str, Any]) -> FeishuEvent:
        """Parse a Feishu callback payload into a generic FeishuEvent."""
        event = self._extract_event(payload)
        event_type = event.get("type", event.get("tag", "unknown"))
        open_id = None
        if "sender" in event:
            open_id = event["sender"].get("sender_id", {}).get("open_id")
        elif "operator" in event:
            open_id = event["operator"].get("operator_id", {}).get("open_id")
        elif "open_id" in event:
            open_id = event["open_id"]
        return FeishuEvent(
            type=event_type,
            open_id=open_id,
            payload=event,
            raw=payload,
        )

    def parse_button_action(self, payload: dict[str, Any]) -> ButtonAction:
        """Parse a card button action callback."""
        event = self._extract_event(payload)
        action = event.get("action", {})
        value = action.get("value", {})
        form_value = action.get("form_value") or {}
        operator = event.get("operator", {})
        open_id = operator.get("operator_id", {}).get("open_id", "")
        reserved = {
            "approval_id", "action", "state", "decision_id", "option_key",
            "chat_id", "chat_type",
        }
        extras = {k: v for k, v in value.items() if k not in reserved}
        extras.update({k: v for k, v in form_value.items() if k not in extras})
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
            message_id=event.get("context", {}).get("open_message_id"),
        )

    def parse_text_message(self, payload: dict[str, Any]) -> TextMessage:
        """Parse a text message event."""
        event = self._extract_event(payload)
        message = event.get("message", {})
        sender = message.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")
        message_id = message.get("message_id", "")
        content = json.loads(message.get("content", "{}"))
        text = _strip_mention_placeholders(content.get("text", "").strip())
        mentions = message.get("mentions", []) or []
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type", "p2p") or "p2p"
        return TextMessage(
            open_id=open_id,
            message_id=message_id,
            text=text,
            is_command=text.startswith("/"),
            parent_id=message.get("parent_id") or message.get("root_id"),
            chat_id=chat_id,
            chat_type=chat_type,
            sender_type=sender.get("sender_type", "user") or "user",
            mentions=mentions,
        )

    def verify_request(
        self,
        timestamp: str,
        nonce: str,
        body: str | bytes,
        signature: str,
    ) -> bool:
        """Verify the signature on an incoming Feishu request."""
        return verify_signature(timestamp, nonce, body, signature, self.app_secret)

    def decrypt_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Decrypt an encrypted payload if present."""
        if "encrypt" in payload and self.encrypt_key:
            return decrypt_feishu(self.encrypt_key, payload["encrypt"])
        return payload


class NullFeishuBridge:
    """No-op Feishu bridge for headless/test environments.

    All send methods return immediately without making network calls.
    """

    async def send_text(
        self, receive_id: str, text: str, receive_id_type: str = "open_id"
    ) -> str:
        return ""

    async def send_text_to_chat(self, chat_id: str, text: str) -> str:
        return ""

    async def _send_interactive(
        self, receive_id: str, card: dict[str, Any], receive_id_type: str = "open_id"
    ) -> str:
        return ""

    async def _send_interactive_to_chat(
        self, chat_id: str, card: dict[str, Any]
    ) -> str:
        return ""

    async def send_confirmation_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        summary: str,
        details: dict[str, Any],
        options: list[dict[str, Any]],
        receive_id_type: str = "open_id",
    ) -> str:
        return ""

    async def send_input_card(
        self,
        open_id: str,
        approval_id: str,
        title: str,
        question: str,
        context: str,
        receive_id_type: str = "open_id",
    ) -> str:
        return ""

    async def send_status_card(
        self,
        open_id: str,
        project_id: str,
        current_state: str,
        display: str,
        recent_events: list[dict[str, Any]],
    ) -> str:
        return ""

    async def update_card(self, message_id: str, card_content: dict[str, Any]) -> None:
        return None

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        return None

    async def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        return None
