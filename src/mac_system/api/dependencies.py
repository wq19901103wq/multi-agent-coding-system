"""Shared FastAPI dependencies for MAC System API routes."""

from __future__ import annotations

from fastapi import Depends
from sqlmodel import Session

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import ClaudeCodeRuntime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.database import get_session
from mac_system.feishu import FeishuBridge


def get_project_service(session: Session = Depends(get_session)) -> ProjectService:
    return ProjectService(session)


def _build_orchestrator(session: Session) -> AgentOrchestrator:
    """Build a fresh AgentOrchestrator for the given DB session."""
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
    bridge = None
    if settings.feishu_app_id and settings.feishu_app_secret:
        bridge = FeishuBridge(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            encrypt_key=settings.feishu_encrypt_key,
            verification_token=settings.feishu_verification_token,
        )
    return AgentOrchestrator(service, worktree_manager, runtime, renderer, feishu_bridge=bridge)


def get_orchestrator(session: Session = Depends(get_session)) -> AgentOrchestrator:
    return _build_orchestrator(session)
