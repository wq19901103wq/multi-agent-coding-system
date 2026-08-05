"""MAC HTTP surface around persistent native Codex project threads."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.config import get_settings
from mac_system.core.notification_service import deliver_card, retry_notification
from mac_system.core.project_service import ProjectNotFoundError, ProjectService
from mac_system.database import init_db, session_scope
from mac_system.feishu import NullFeishuBridge
from mac_system.feishu.card import build_content_card
from mac_system.feishu.handlers import (
    FeishuHandlerContext,
    handle_card_action,
    handle_text_message,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.feishu.ws_client import FeishuWebSocketClient
from mac_system.models import (
    FeishuEventLog,
    Project,
    ProjectCreate,
    ProjectOut,
    ProjectUser,
    RootAgentSession,
)

from mac_system.api import admin_routes, feishu_routes
from mac_system.api.dependencies import (
    _build_orchestrator,
    get_orchestrator,
    get_project_service,
    require_control_api_token,
)


logger = logging.getLogger(__name__)


async def _reconcile_interrupted_root_cards(project_ids: list[str]) -> None:
    """Make durable Feishu projections match restart-recovered root turns."""

    if not project_ids:
        return
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return
    from mac_system.feishu.bridge import FeishuBridge

    bridge = FeishuBridge(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        encrypt_key=settings.feishu_encrypt_key,
        verification_token=settings.feishu_verification_token,
    )
    try:
        with session_scope() as session:
            service = ProjectService(session)
            for project_id in project_ids:
                project = service.get_project(project_id)
                if project is None or not project.feishu_chat_id:
                    continue
                projections = service.get_project_card_projections(project_id)
                progress_key = f"root-agent:{project_id}:activity"
                recoverable = {
                    projection.projection_key
                    for projection in projections
                    if projection.projection_key == progress_key
                    or (
                        projection.card_kind == "question"
                        and projection.lifecycle_status == "pending"
                    )
                }
                service.supersede_projection_notifications(
                    recoverable,
                    reason="superseded by native turn restart recovery",
                )
                display_name = str(project.name or "").strip()
                if not display_name or len(display_name) > 24 or "\n" in display_name:
                    display_name = "当前项目"
                for projection in projections:
                    if (
                        projection.projection_key not in recoverable
                        or not projection.provider_message_id
                    ):
                        continue
                    is_progress = projection.projection_key == progress_key
                    title = (
                        f"[{display_name}] 项目进展"
                        if is_progress
                        else f"[{display_name}] 问题已失效"
                    )
                    content = (
                        "**当前进展**：服务重启中止了上一轮；项目已恢复为空闲，"
                        "可以直接继续发送消息。"
                        if is_progress
                        else "服务重启中止了提出该问题的 Codex turn；旧答案不会再提交，"
                        "请直接继续发送消息。"
                    )
                    lifecycle = "interrupted" if is_progress else "expired"
                    try:
                        await deliver_card(
                            service,
                            bridge,
                            dedupe_key=(
                                f"restart-recovery:{projection.projection_key}:"
                                f"{projection.projection_version}"
                            ),
                            recipient_id=project.feishu_chat_id,
                            recipient_type="chat_id",
                            intent_type="progress" if is_progress else "question",
                            card=build_content_card(title, content, color="orange"),
                            project_id=project_id,
                            update_message_id=projection.provider_message_id,
                            projection_key=projection.projection_key,
                            subject_type=projection.subject_type,
                            subject_id=projection.subject_id,
                            lifecycle_status=lifecycle,
                            resolved_by="system_restart" if not is_progress else None,
                            resulting_state="idle",
                        )
                    except Exception:
                        logger.warning(
                            "Failed to reconcile recovered card %s",
                            projection.projection_key,
                            exc_info=True,
                        )
    finally:
        await bridge.close()


async def _drain_notification_outbox() -> None:
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return
    from mac_system.feishu.bridge import FeishuBridge

    bridge = FeishuBridge(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        encrypt_key=settings.feishu_encrypt_key,
        verification_token=settings.feishu_verification_token,
    )
    try:
        while True:
            await asyncio.sleep(5)
            with session_scope() as session:
                service = ProjectService(session)
                for notification in service.list_retryable_notifications():
                    try:
                        await retry_notification(service, bridge, notification)
                    except Exception:
                        logger.warning(
                            "Notification retry failed id=%s",
                            notification.id,
                            exc_info=True,
                        )
    finally:
        await bridge.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from mac_system.core.root_agent import (
        close_root_threads,
        register_root_service_loop,
    )

    register_root_service_loop(asyncio.get_running_loop())
    init_db()
    from mac_system.core.root_sessions import (
        recover_interrupted_external_agents,
        recover_interrupted_native_turns,
    )

    with session_scope() as recovery_session:
        recovered_projects = recover_interrupted_native_turns(recovery_session)
        recovered_external_agents = recover_interrupted_external_agents(
            recovery_session
        )
    if recovered_projects:
        logger.warning(
            "Recovered %s native Codex turn(s) interrupted by service restart",
            len(recovered_projects),
        )
        await _reconcile_interrupted_root_cards(recovered_projects)
    if recovered_external_agents:
        logger.warning(
            "Recovered %s external provider execution(s) interrupted by service restart",
            len(recovered_external_agents),
        )
    settings = get_settings()
    ws_client: FeishuWebSocketClient | None = None
    if (
        settings.feishu_transport == "websocket"
        and settings.feishu_app_id
        and settings.feishu_app_secret
    ):
        ws_client = FeishuWebSocketClient()
        ws_client.start()
    app.state.ws_client = ws_client
    notification_task = asyncio.create_task(
        _drain_notification_outbox(), name="notification-outbox"
    )
    try:
        yield
    finally:
        notification_task.cancel()
        await asyncio.gather(notification_task, return_exceptions=True)
        await close_root_threads()
        register_root_service_loop(None)
        if ws_client is not None:
            ws_client.stop()
        app.state.ws_client = None


app = FastAPI(title="MAC System API", lifespan=lifespan)
app.include_router(feishu_routes.router)
app.include_router(admin_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/projects", response_model=ProjectOut)
async def create_project(
    req: ProjectCreate,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> ProjectOut:
    try:
        project = await service.create_project(
            name=req.name,
            task_type=req.task_type.value,
            description=req.description,
            approval_mode=req.approval_mode,
            creator_open_id=req.creator_open_id,
            feishu_chat_id=req.feishu_chat_id,
            feishu_chat_type=req.feishu_chat_type,
            bound_directory=req.bound_directory,
            initial_context={"orchestration_mode": "root_agent"},
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectOut.model_validate(project)


@app.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> ProjectOut:
    project = service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return ProjectOut.model_validate(project)


@app.get("/projects/{project_id}/status")
def get_status(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    try:
        return service.get_project_status(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from exc


class UserMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class UserMessageResponse(BaseModel):
    project_id: str
    reply: str
    state: str


async def _run_project_message(project_id: str, content: str) -> None:
    with session_scope() as session:
        orchestrator = _build_orchestrator(session)
        try:
            project = orchestrator.project_service.require_project(project_id)
            if project.execution_status == "running" and await orchestrator.steer_root_agent(
                project_id, content
            ):
                return
            await orchestrator.run_root_agent(project_id, content)
        finally:
            if orchestrator.feishu_bridge is not None:
                await orchestrator.feishu_bridge.close()


@app.post("/projects/{project_id}/messages", response_model=UserMessageResponse)
async def submit_user_message(
    project_id: str,
    req: UserMessageRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> UserMessageResponse:
    try:
        project = service.require_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from exc
    if project.status == "cancelled":
        raise HTTPException(status_code=409, detail="PROJECT_CANCELLED")
    background_tasks.add_task(_run_project_message, project.id, req.content)
    return UserMessageResponse(
        project_id=project.id,
        reply="收到，正在处理。",
        state=project.execution_status,
    )


class CommandRequest(BaseModel):
    command: Literal["pause", "resume", "cancel", "status"]


class CommandResponse(BaseModel):
    project_id: str
    command: str
    status: str
    message: str


@app.post("/projects/{project_id}/commands", response_model=CommandResponse)
async def submit_command(
    project_id: str,
    req: CommandRequest,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> CommandResponse:
    try:
        project = service.require_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from exc
    if req.command == "pause":
        project = await service.pause_project(project.id, triggered_by="api")
        message = "项目已暂停"
    elif req.command == "resume":
        project = await service.resume_project(project.id, triggered_by="api")
        message = "项目已恢复"
    elif req.command == "cancel":
        await orchestrator.cancel_project(project.id, reason="API user cancelled")
        project = service.require_project(project.id)
        message = "项目已取消"
    else:
        message = "项目状态查询成功"
    return CommandResponse(
        project_id=project.id,
        command=req.command,
        status=project.status,
        message=message,
    )


def _parse_created_at(raw: str | datetime | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    try:
        if raw.replace(".", "", 1).isdigit():
            value = float(raw)
            if value > 1_000_000_000_000:
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


class InjectCardActionRequest(BaseModel):
    open_id: str
    approval_id: str = ""
    action: str
    state: str = ""
    option_key: str | None = None
    decision_id: str | None = None
    chat_id: str | None = None
    chat_type: str = "p2p"
    message_id: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    created_at: str | datetime | None = None
    event_id: str | None = None


@app.post("/test/inject_card_action")
async def inject_card_action(
    req: InjectCardActionRequest,
    request: Request,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, str]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    action = ButtonAction(
        open_id=req.open_id,
        approval_id=req.approval_id,
        action=req.action,
        state=req.state,
        decision_id=req.decision_id,
        option_key=req.option_key,
        extras=req.extras,
        chat_id=req.chat_id,
        chat_type=req.chat_type,
        message_id=req.message_id,
        created_at=_parse_created_at(req.created_at),
    )
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        asyncio.create_task(ws_client.inject_card_action(action, req.event_id))
    else:
        ctx = FeishuHandlerContext(
            bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
            service=orchestrator.project_service,
            orchestrator=orchestrator,
            chat_id=req.chat_id,
            chat_type=req.chat_type,
        )
        await handle_card_action(ctx, action)
    return {"status": "ok"}


class InjectTextMessageRequest(BaseModel):
    open_id: str
    text: str
    chat_id: str | None = None
    chat_type: str = "p2p"
    message_id: str = "msg_test"
    created_at: str | datetime | None = None
    event_id: str | None = None


@app.post("/test/inject_text_message")
async def inject_text_message(
    req: InjectTextMessageRequest,
    request: Request,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, str]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    message = TextMessage(
        open_id=req.open_id,
        message_id=req.message_id,
        text=req.text,
        is_command=req.text.startswith("/"),
        chat_id=req.chat_id,
        chat_type=req.chat_type,
        created_at=_parse_created_at(req.created_at),
    )
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        asyncio.create_task(ws_client.inject_text_message(message, req.event_id))
    else:
        ctx = FeishuHandlerContext(
            bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
            service=orchestrator.project_service,
            orchestrator=orchestrator,
            chat_id=req.chat_id,
            chat_type=req.chat_type,
        )
        await handle_text_message(ctx, message)
    return {"status": "ok"}


@app.post("/test/tick/{project_id}")
async def test_tick(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, str]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    await orchestrator.tick(project_id)
    return {"status": "ok"}


@app.get("/test/projects/{project_id}/runtime")
def test_project_runtime(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    service.require_project(project_id)
    root = service.session.exec(
        select(RootAgentSession).where(RootAgentSession.project_id == project_id)
    ).first()
    return {
        "native_thread_id": root.native_thread_id if root else None,
        "active_turn_id": root.active_turn_id if root else None,
        "status": root.status if root else "not_started",
        "event_cursor": root.event_cursor if root else 0,
        "last_error": root.last_error if root else None,
    }


@app.get("/test/projects/{project_id}/cards")
def test_project_cards(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    service.require_project(project_id)
    return {
        "items": [item.model_dump() for item in service.get_project_card_projections(project_id)]
    }


@app.get("/test/feishu/events/{message_id}")
def test_feishu_event(
    message_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    event = service.session.exec(
        select(FeishuEventLog)
        .where(FeishuEventLog.message_id == message_id)
        .order_by(FeishuEventLog.created_at.desc())
    ).first()
    if event is None:
        return {"found": False}
    candidate = service.session.exec(
        select(Project)
        .join(ProjectUser, ProjectUser.project_id == Project.id)
        .where(ProjectUser.open_id == event.open_id)
        .where(Project.feishu_chat_id == event.chat_id)
        .order_by(Project.created_at.desc())
    ).first()
    return {
        "found": True,
        "status": event.status,
        "detail": event.detail,
        "project_id": candidate.id if candidate else None,
        "created_at": event.created_at.isoformat(),
    }


@app.post("/test/clear_pending_intent/{open_id}")
async def clear_pending_intent(
    open_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, str]:
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    service.delete_pending_intent(open_id)
    return {"status": "ok"}
