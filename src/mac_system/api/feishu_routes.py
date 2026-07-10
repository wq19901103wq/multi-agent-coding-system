import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import ClaudeCodeRuntime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.database import get_session
from mac_system.feishu import FeishuBridge
from mac_system.feishu.handlers import FeishuHandlerContext, handle_card_action, handle_text_message
from mac_system.feishu.models import ButtonAction, TextMessage

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
    worktree_manager = WorktreeManager(settings.projects_dir)
    runtime = ClaudeCodeRuntime(
        claude_bin=settings.claude_bin,
        anthropic_api_key=settings.anthropic_api_key,
        anthropic_auth_token=settings.anthropic_auth_token,
        claude_model=settings.claude_model,
        anthropic_base_url=settings.anthropic_base_url,
        anthropic_default_opus_model=settings.anthropic_default_opus_model,
        anthropic_default_sonnet_model=settings.anthropic_default_sonnet_model,
        anthropic_default_haiku_model=settings.anthropic_default_haiku_model,
        claude_code_effort_level=settings.claude_code_effort_level,
        api_timeout_ms=settings.api_timeout_ms,
    )
    renderer = PromptRenderer(get_agent_config())
    return AgentOrchestrator(service, worktree_manager, runtime, renderer, feishu_bridge=bridge)


@router.post("/events")
async def feishu_events(
    request: Request,
    session: Session = Depends(get_session),
    bridge: FeishuBridge | None = Depends(get_feishu_bridge),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator_with_bridge),
) -> dict[str, Any]:
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8")

    # Signature verification when app_secret is available
    signature = request.headers.get("X-Lark-Signature", "")
    timestamp = request.headers.get("X-Lark-Timestamp", "")
    nonce = request.headers.get("X-Lark-Nonce", "")
    if bridge and signature:
        if not bridge.verify_request(timestamp, nonce, body_bytes, signature):
            raise HTTPException(status_code=401, detail="INVALID_SIGNATURE")

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="INVALID_JSON") from exc

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
        return {"status": "ok"}

    return {"status": "ignored"}
