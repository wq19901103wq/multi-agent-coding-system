"""Shared FastAPI dependencies for MAC System API routes."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import build_agent_runtime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.database import get_session
from mac_system.feishu import FeishuBridge


def get_project_service(session: Session = Depends(get_session)) -> ProjectService:
    return ProjectService(session)


def require_control_api_token(
    authorization: str | None = Header(default=None),
    x_control_api_token: str | None = Header(default=None),
) -> None:
    """Authenticate REST control-plane requests, failing closed if unconfigured."""
    settings = get_settings()
    expected = settings.control_api_token or settings.admin_token
    if not expected:
        raise HTTPException(status_code=503, detail="CONTROL_API_TOKEN_NOT_CONFIGURED")
    supplied = x_control_api_token
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="INVALID_CONTROL_API_TOKEN")


def _build_orchestrator(session: Session) -> AgentOrchestrator:
    """Build a fresh AgentOrchestrator for the given DB session."""
    settings = get_settings()
    service = ProjectService(session)
    agent_config = get_agent_config()
    runtime = build_agent_runtime(settings, agent_config)
    bridge = None
    if settings.feishu_app_id and settings.feishu_app_secret:
        bridge = FeishuBridge(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            encrypt_key=settings.feishu_encrypt_key,
            verification_token=settings.feishu_verification_token,
        )
    return AgentOrchestrator(service, runtime, feishu_bridge=bridge)


def get_orchestrator(session: Session = Depends(get_session)) -> AgentOrchestrator:
    return _build_orchestrator(session)
