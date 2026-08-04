import json
import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import build_agent_runtime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.database import get_session
from mac_system.feishu import FeishuBridge
from mac_system.feishu.handlers import FeishuHandlerContext, handle_card_action, handle_text_message

router = APIRouter(prefix="/feishu", tags=["feishu"])


def get_feishu_bridge() -> FeishuBridge | None:
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return None
    return FeishuBridge(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        encrypt_key=settings.feishu_encrypt_key,
        verification_token=settings.feishu_verification_token,
    )


def get_orchestrator_with_bridge(
    session: Session = Depends(get_session),
    bridge: FeishuBridge | None = Depends(get_feishu_bridge),
) -> AgentOrchestrator:
    settings = get_settings()
    service = ProjectService(session)
    agent_config = get_agent_config()
    runtime = build_agent_runtime(settings, agent_config)
    return AgentOrchestrator(service, runtime, feishu_bridge=bridge)


@router.post("/events")
async def feishu_events(
    request: Request,
    session: Session = Depends(get_session),
    bridge: FeishuBridge | None = Depends(get_feishu_bridge),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator_with_bridge),
) -> dict[str, Any]:
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8")

    # Require either a valid request signature or the legacy verification
    # token.  Merely omitting the signature must never bypass authentication.
    signature = request.headers.get("X-Lark-Signature", "")
    timestamp = request.headers.get("X-Lark-Timestamp", "")
    nonce = request.headers.get("X-Lark-Nonce", "")
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="INVALID_JSON") from exc

    if bridge:
        if signature:
            if not bridge.verify_request(timestamp, nonce, body_bytes, signature):
                raise HTTPException(status_code=401, detail="INVALID_SIGNATURE")
        else:
            supplied_token = payload.get("token") or payload.get("header", {}).get("token")
            expected_token = bridge.verification_token
            if (
                not supplied_token
                or not expected_token
                or not hmac.compare_digest(str(supplied_token), str(expected_token))
            ):
                raise HTTPException(status_code=401, detail="MISSING_SIGNATURE")

    # Decrypt if needed
    if bridge:
        payload = bridge.decrypt_payload(payload)

    # URL verification challenge
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event_type = payload.get("header", {}).get("event_type", "")
    event_id = payload.get("header", {}).get("event_id")

    # Text message event
    if event_type == "im.message.receive_v1":
        if bridge is None:
            raise HTTPException(status_code=503, detail="FEISHU_NOT_CONFIGURED")
        msg = bridge.parse_text_message(payload)
        service = ProjectService(session)
        if event_id and service.is_event_processed(event_id):
            return {"status": "ok"}
        if event_id:
            service.mark_event_processed(event_id, "message")
        ctx = FeishuHandlerContext(
            bridge=bridge,
            service=service,
            orchestrator=orchestrator,
            chat_id=msg.chat_id,
            chat_type=msg.chat_type,
        )
        await handle_text_message(ctx, msg)
        session.commit()
        return {"status": "ok"}

    # Card button action
    if event_type == "card.action.trigger":
        if bridge is None:
            raise HTTPException(status_code=503, detail="FEISHU_NOT_CONFIGURED")
        action = bridge.parse_button_action(payload)
        service = ProjectService(session)
        if event_id and service.is_event_processed(event_id):
            return {"status": "ok"}
        if event_id:
            service.mark_event_processed(event_id, "card")
        ctx = FeishuHandlerContext(
            bridge=bridge,
            service=service,
            orchestrator=orchestrator,
        )
        await handle_card_action(ctx, action)
        session.commit()
        return {"status": "ok"}

    return {"status": "ignored"}
