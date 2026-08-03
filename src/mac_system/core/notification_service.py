"""Reliable delivery boundary for user-visible interactive notifications."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from mac_system.core.project_service import ProjectService


def is_retryable_delivery_error(exc: Exception) -> bool:
    """Classify provider failures by transport semantics, never message text."""
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


async def deliver_card(
    service: ProjectService,
    bridge: Any,
    *,
    dedupe_key: str,
    recipient_id: str,
    recipient_type: str,
    intent_type: str,
    card: dict[str, Any],
    project_id: str | None = None,
    conversation_id: str | None = None,
    update_message_id: str | None = None,
    projection_key: str | None = None,
    subject_type: str = "notification",
    subject_id: str | None = None,
    lifecycle_status: str | None = None,
    selected_action: str | None = None,
    resolved_by: str | None = None,
    resulting_state: str | None = None,
) -> str | None:
    """Deliver one card once per stable intent key.

    The intent is committed before calling Feishu. Replaying an already-sent
    intent returns its recorded message id without posting a duplicate.
    """
    notification = service.enqueue_notification(
        dedupe_key=dedupe_key,
        recipient_id=recipient_id,
        recipient_type=recipient_type,
        intent_type=intent_type,
        payload={
            "card": card,
            "update_message_id": update_message_id,
            "projection_key": projection_key,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "lifecycle_status": lifecycle_status,
            "selected_action": selected_action,
            "resolved_by": resolved_by,
            "resulting_state": resulting_state,
        },
        project_id=project_id,
        conversation_id=conversation_id,
    )
    effective_projection_key = (
        projection_key
        or (f"provider:{update_message_id}" if update_message_id else dedupe_key)
    )
    effective_subject_id = subject_id or notification.id
    effective_lifecycle = lifecycle_status or (
        "pending" if intent_type in {"decision", "question"} else "published"
    )
    if notification.status == "sent":
        service.upsert_card_projection(
            projection_key=effective_projection_key,
            subject_type=subject_type,
            subject_id=effective_subject_id,
            card_kind=intent_type,
            provider_message_id=notification.provider_message_id,
            project_id=project_id,
            conversation_id=conversation_id,
            lifecycle_status=effective_lifecycle,
            selected_action=selected_action,
            resolved_by=resolved_by,
            resulting_state=resulting_state,
        )
        return notification.provider_message_id

    claimed = service.claim_notification(notification.id)
    if claimed is None:
        # Another handler owns an in-flight claim. Do not issue a second POST.
        return notification.provider_message_id

    try:
        for attempt in range(2):
            try:
                if update_message_id:
                    await bridge.update_card(update_message_id, card)
                    message_id = update_message_id
                else:
                    message_id = await bridge._send_interactive(
                        recipient_id, card, receive_id_type=recipient_type
                    )
                break
            except httpx.HTTPStatusError as exc:
                # Feishu occasionally returns a transient 400/429 while a
                # sequence of cards is posted. These statuses acknowledge that
                # the card was not accepted, so one bounded retry cannot create
                # a duplicate provider message.
                if attempt or exc.response.status_code not in {400, 429}:
                    raise
                await asyncio.sleep(0.5)
    except Exception as exc:
        retryable = is_retryable_delivery_error(exc)
        service.finish_notification(
            claimed.id, error=str(exc)[:1000], retryable=retryable
        )
        service.upsert_card_projection(
            projection_key=effective_projection_key,
            subject_type=subject_type,
            subject_id=effective_subject_id,
            card_kind=intent_type,
            provider_message_id=update_message_id,
            project_id=project_id,
            conversation_id=conversation_id,
            lifecycle_status=(
                "delivery_failed" if retryable else "delivery_terminal_failed"
            ),
            selected_action=selected_action,
            resolved_by=resolved_by,
            resulting_state=resulting_state,
            last_error=str(exc)[:1000],
        )
        raise
    service.finish_notification(
        claimed.id, provider_message_id=message_id or update_message_id
    )
    service.upsert_card_projection(
        projection_key=effective_projection_key,
        subject_type=subject_type,
        subject_id=effective_subject_id,
        card_kind=intent_type,
        provider_message_id=message_id or update_message_id,
        project_id=project_id,
        conversation_id=conversation_id,
        lifecycle_status=effective_lifecycle,
        selected_action=selected_action,
        resolved_by=resolved_by,
        resulting_state=resulting_state,
    )
    return message_id or update_message_id


async def retry_notification(service: ProjectService, bridge: Any, notification: Any) -> str | None:
    """Replay one persisted outbox intent through the normal idempotent boundary."""
    payload = dict(notification.payload or {})
    card = payload.get("card")
    if not isinstance(card, dict):
        service.finish_notification(
            notification.id, error="persisted notification payload has no card"
        )
        return None
    return await deliver_card(
        service,
        bridge,
        dedupe_key=notification.dedupe_key,
        recipient_id=notification.recipient_id,
        recipient_type=notification.recipient_type,
        intent_type=notification.intent_type,
        card=card,
        project_id=notification.project_id,
        conversation_id=notification.conversation_id,
        update_message_id=payload.get("update_message_id"),
        projection_key=payload.get("projection_key"),
        subject_type=payload.get("subject_type") or "notification",
        subject_id=payload.get("subject_id"),
        lifecycle_status=payload.get("lifecycle_status"),
        selected_action=payload.get("selected_action"),
        resolved_by=payload.get("resolved_by"),
        resulting_state=payload.get("resulting_state"),
    )
