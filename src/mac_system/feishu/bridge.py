import asyncio
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from mac_system.feishu.card import build_content_card
from mac_system.feishu.crypto import decrypt_feishu, verify_signature
from mac_system.utils.formatting import (
    split_markdown_sections,
    split_text,
)
from mac_system.feishu.models import ButtonAction, FeishuEvent, MessageAttachment, TextMessage


_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")
_MENTION_AT_TAG_RE = re.compile(r'<at[^>]*>.*?</at>')
_TENANT_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TENANT_TOKEN_CACHE_LOCK = threading.Lock()


def _strip_mention_placeholders(text: str) -> str:
    """Remove Feishu @ user placeholders and <at> tags from message text."""
    text = _MENTION_PLACEHOLDER_RE.sub("", text)
    text = _MENTION_AT_TAG_RE.sub("", text)
    return text.strip()


def parse_message_content(message_type: str, content: dict[str, Any]) -> tuple[str, list[MessageAttachment]]:
    """Share content decoding between HTTP events and the SDK WebSocket."""
    attachments: list[MessageAttachment] = []
    text = str(content.get("text") or "")
    if message_type == "image" and content.get("image_key"):
        attachments.append(MessageAttachment(str(content["image_key"]), "image", "照片"))
    elif message_type == "file" and content.get("file_key"):
        attachments.append(MessageAttachment(str(content["file_key"]), "file", str(content.get("file_name") or "文件")))
    elif message_type == "post":
        # Events normally contain a single post; tolerate localized payloads.
        post = content if "content" in content else next(
            (value for value in content.values() if isinstance(value, dict) and "content" in value), {}
        )
        lines = [str(post.get("title") or "")]
        for row in post.get("content", []):
            parts = []
            for item in row:
                if item.get("tag") in {"text", "a"}:
                    parts.append(str(item.get("text") or item.get("href") or ""))
                elif item.get("tag") == "img" and item.get("image_key"):
                    attachments.append(MessageAttachment(str(item["image_key"]), "image", "照片"))
            lines.append("".join(parts))
        text = "\n".join(line for line in lines if line)
    return _strip_mention_placeholders(text), attachments


class FeishuBridgeError(Exception):
    pass


class FeishuHTTPStatusError(httpx.HTTPStatusError):
    """HTTP error that preserves Feishu's structured response body."""


def _status_error(exc: httpx.HTTPStatusError) -> FeishuHTTPStatusError:
    body = exc.response.text.strip()
    detail = f"{exc}; response={body[:2000]}" if body else str(exc)
    return FeishuHTTPStatusError(
        detail,
        request=exc.request,
        response=exc.response,
    )


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
        self._client = self._create_client()
        self._client_requests: dict[httpx.AsyncClient, int] = {}
        self._closed = False
        self._token: str | None = None
        self._token_expire_at: float = 0.0

    def _create_client(self) -> httpx.AsyncClient:
        # Bounded pool with a short pool timeout: after a network flap or
        # host sleep, a wedged acquisition must fail fast so the existing
        # retry loop rotates to a fresh connection instead of stacking
        # 30-second pool waits. Short keepalive expiry reaps half-open
        # connections left behind by sleep/wake cycles.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
                keepalive_expiry=15.0,
            ),
        )
    async def close(self) -> None:
        self._closed = True
        clients = set(self._client_requests) | {self._client}
        await asyncio.gather(*(client.aclose() for client in clients))

    async def _get_tenant_token(
        self, client: httpx.AsyncClient | None = None
    ) -> str:
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token

        cache_key = (self.base_url, self.app_id)
        with _TENANT_TOKEN_CACHE_LOCK:
            cached = _TENANT_TOKEN_CACHE.get(cache_key)
        if cached is not None and now < cached[1]:
            self._token, self._token_expire_at = cached
            return self._token

        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = await (client if client is not None else self._client).post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuBridgeError(f"Failed to get tenant token: {data}")

        self._token = data["tenant_access_token"]
        # expire is in seconds; keep a 60s buffer before sharing the token.
        self._token_expire_at = time.time() + max(
            int(data.get("expire", 7200)) - 60,
            0,
        )
        with _TENANT_TOKEN_CACHE_LOCK:
            _TENANT_TOKEN_CACHE[cache_key] = (
                self._token,
                self._token_expire_at,
            )
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
            client = self._client
            self._client_requests[client] = self._client_requests.get(client, 0) + 1
            try:
                token = await self._get_tenant_token(client)
                resp = await client.request(
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
                httpx.ProxyError,
            ) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status < 500 and status != 429:
                        raise _status_error(exc) from exc
                last_exc = exc
                if (
                    isinstance(
                        exc,
                        (
                            httpx.ConnectError,
                            httpx.ConnectTimeout,
                            httpx.PoolTimeout,
                            httpx.ProxyError,
                        ),
                    )
                    and client is self._client
                    and not self._closed
                ):
                    self._client = self._create_client()
            finally:
                remaining = self._client_requests[client] - 1
                if remaining:
                    self._client_requests[client] = remaining
                else:
                    del self._client_requests[client]
                    if client is not self._client:
                        await client.aclose()
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))
        if isinstance(last_exc, httpx.HTTPStatusError):
            raise _status_error(last_exc) from last_exc
        raise last_exc

    async def get_bot_open_id(self) -> str | None:
        """Return the open_id of the bot itself, caching the result."""
        if getattr(self, "_bot_open_id", None):
            return self._bot_open_id
        data = await self._request("GET", "/open-apis/bot/v3/info")
        bot = data.get("bot") or data.get("data", {}).get("bot", {})
        self._bot_open_id = bot.get("open_id")
        return self._bot_open_id

    async def download_message_resource(
        self,
        message_id: str,
        attachment: MessageAttachment,
        destination: Path,
        *,
        max_bytes: int = 30 * 1024 * 1024,
    ) -> str:
        """Stream the received message's resource to a new file; return its MIME type."""
        if not message_id or not attachment.key or attachment.kind not in {"image", "file"}:
            raise FeishuBridgeError("附件缺少有效的消息或资源标识")
        client = self._client
        self._client_requests[client] = self._client_requests.get(client, 0) + 1
        created = completed = False
        try:
            token = await self._get_tenant_token(client)
            path = f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/resources/{quote(attachment.key, safe='')}"
            async with client.stream(
                "GET", f"{self.base_url}{path}",
                params={"type": attachment.kind},
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.is_error:
                    # Do not include response bodies, resource keys or credentials in cards.
                    raise FeishuBridgeError(f"飞书附件下载失败（HTTP {response.status_code}），请检查消息资源读取权限")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise FeishuBridgeError("附件超过 30 MB，请缩小后重新发送")
                size = 0
                with destination.open("xb") as output:
                    created = True
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FeishuBridgeError("附件超过 30 MB，请缩小后重新发送")
                        output.write(chunk)
                if not size:
                    raise FeishuBridgeError("飞书返回了空附件，请重新发送")
                if (
                    response.headers.get("content-type", "").startswith("application/json")
                    and not response.headers.get("content-disposition")
                    and size <= 8192
                ):
                    try:
                        error = json.loads(destination.read_bytes())
                    except (ValueError, UnicodeDecodeError):
                        error = None
                    if isinstance(error, dict) and error.get("code") and "msg" in error:
                        raise FeishuBridgeError(f"飞书附件下载失败（错误码 {error['code']}），请检查消息资源读取权限")
                completed = True
                return response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
        finally:
            if created and not completed:
                destination.unlink(missing_ok=True)
            remaining = self._client_requests[client] - 1
            if remaining:
                self._client_requests[client] = remaining
            else:
                del self._client_requests[client]
                if client is not self._client:
                    await client.aclose()

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> str:
        """Send a plain text message, splitting if it exceeds Feishu's limit.

        Returns the message_id of the last chunk (or the only chunk).
        """
        last_message_id = ""
        for chunk in split_text(text):
            data = await self._request(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                json_data={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                },
            )
            last_message_id = data["data"]["message_id"]
        return last_message_id

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
    ) -> list[str]:
        """Send one or more display cards for long markdown content.

        Automatically splits oversized Markdown into multiple cards so Feishu
        does not silently truncate it.  Returns the list of message_ids.
        """
        chunks = split_markdown_sections(content)
        total_chunks = len(chunks)
        message_ids: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            card = build_content_card(
                title,
                chunk,
                page=idx,
                total=total_chunks,
                color=color,
            )
            message_id = await self._send_interactive(
                open_id, card, receive_id_type=receive_id_type
            )
            message_ids.append(message_id)
        return message_ids

    async def update_card(self, message_id: str, card_content: dict[str, Any]) -> None:
        """Update an existing interactive card."""
        await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            json_data={"content": json.dumps(card_content, ensure_ascii=False)},
        )

    async def delete_message(self, message_id: str) -> None:
        """Remove a bot-authored interaction once a later event resolves it."""
        await self._request(
            "DELETE",
            f"/open-apis/im/v1/messages/{message_id}",
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
        operator_id = operator.get("operator_id", {})
        open_id = operator_id.get("open_id", "")
        reserved = {
            "approval_id", "action", "state", "decision_id", "option_key",
            "chat_id", "chat_type",
        }
        extras = {k: v for k, v in value.items() if k not in reserved}
        extras.update({k: v for k, v in form_value.items() if k not in extras})
        create_time = action.get("create_time")
        created_at = None
        if create_time:
            try:
                ts = int(create_time)
                if ts > 1_000_000_000_000:
                    ts = ts / 1000
                created_at = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
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
            created_at=created_at,
            user_id=operator_id.get("user_id"),
            union_id=operator_id.get("union_id"),
        )

    def parse_text_message(self, payload: dict[str, Any]) -> TextMessage:
        """Parse text, file, image or rich-text message events."""
        event = self._extract_event(payload)
        message = event.get("message", {})
        sender = event.get("sender") or message.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")
        message_id = message.get("message_id", "")
        content = json.loads(message.get("content", "{}"))
        text, attachments = parse_message_content(message.get("message_type", "text"), content)
        mentions = message.get("mentions", []) or []
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type", "p2p") or "p2p"
        create_time = message.get("create_time")
        created_at = None
        if create_time:
            try:
                ts = int(create_time)
                if ts > 1_000_000_000_000:
                    ts = ts / 1000
                created_at = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
        return TextMessage(
            open_id=open_id,
            message_id=message_id,
            text=text,
            is_command=text.startswith("/"),
            # Only an explicit parent is a reply-routing signal. A thread
            # root is conversation structure, not a selected card.
            parent_id=message.get("parent_id"),
            chat_id=chat_id,
            chat_type=chat_type,
            sender_type=sender.get("sender_type", "user") or "user",
            mentions=mentions,
            created_at=created_at,
            user_id=sender_id.get("user_id"),
            union_id=sender_id.get("union_id"),
            attachments=attachments,
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

    async def update_card(self, message_id: str, card_content: dict[str, Any]) -> None:
        return None

    async def delete_message(self, message_id: str) -> None:
        return None

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str | None:
        return None

    async def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        return None
