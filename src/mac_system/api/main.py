"""MAC HTTP surface around persistent native Codex project threads."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.config import get_settings
from mac_system.config import get_agent_config
from mac_system.core.notification_service import deliver_card, retry_notification
from mac_system.core.project_files import (
    resolve_project_file,
    verify_project_file_signature,
)
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
from mac_system.feishu.accounts import create_feishu_bridge, get_feishu_bot_accounts
from mac_system.models import (
    FeishuEventLog,
    Project,
    ProjectCreate,
    ProjectOut,
    ProjectOwnerReview,
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

_PROJECT_TEXT_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".log": "text/plain",
    ".rst": "text/plain",
    ".toml": "text/plain",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}
_UTF8_APPLICATION_MEDIA_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/yaml",
    "application/javascript",
}


async def _reconcile_interrupted_root_cards(
    project_ids: list[str],
    *,
    auto_resume_project_ids: set[str] | None = None,
) -> None:
    """Make durable Feishu projections match restart-recovered root turns."""

    if not project_ids:
        return
    auto_resume_project_ids = auto_resume_project_ids or set()
    settings = get_settings()
    accounts = get_feishu_bot_accounts(settings)
    if not accounts:
        return
    bridges = {
        account_id: create_feishu_bridge(settings, account_id)
        for account_id in accounts
    }
    try:
        with session_scope() as session:
            service = ProjectService(session)
            for project_id in project_ids:
                project = service.get_project(project_id)
                if project is None or not project.feishu_chat_id:
                    continue
                bridge = bridges.get(project.bot_account_id)
                if bridge is None:
                    continue
                projections = service.get_project_card_projections(project_id)
                legacy_progress_key = f"root-agent:{project_id}:activity"
                progress_prefix = f"{legacy_progress_key}:"

                def is_live_progress(projection) -> bool:
                    return bool(
                        projection.lifecycle_status == "running"
                        and (
                            projection.projection_key == legacy_progress_key
                            or projection.projection_key.startswith(progress_prefix)
                        )
                    )

                recoverable = {
                    projection.projection_key
                    for projection in projections
                    if is_live_progress(projection)
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
                    is_progress = is_live_progress(projection)
                    title = f"[{display_name}] 项目进展"
                    content = (
                        (
                            "**当前进展**：服务重启中止了旧 turn；已保留原项目会话，"
                            "并排队自动安全续接。"
                            if project_id in auto_resume_project_ids
                            else "**当前进展**：服务重启中止了上一轮；项目已恢复为空闲，"
                            "可以直接继续发送消息。"
                        )
                        if is_progress
                        else ""
                    )
                    lifecycle = "interrupted" if is_progress else "expired"
                    try:
                        if is_progress:
                            await deliver_card(
                                service,
                                bridge,
                                dedupe_key=(
                                    f"restart-recovery:{projection.projection_key}:"
                                    f"{projection.projection_version}"
                                ),
                                recipient_id=project.feishu_chat_id,
                                recipient_type="chat_id",
                                intent_type="progress",
                                card=build_content_card(title, content, color="orange"),
                                project_id=project_id,
                                update_message_id=projection.provider_message_id,
                                projection_key=projection.projection_key,
                                subject_type=projection.subject_type,
                                subject_id=projection.subject_id,
                                lifecycle_status=lifecycle,
                                resulting_state="idle",
                            )
                        else:
                            # The restart happened later than the original
                            # question. Recall the obsolete form instead of
                            # back-writing that later fact into an old card.
                            await bridge.delete_message(projection.provider_message_id)
                            service.upsert_card_projection(
                                projection_key=projection.projection_key,
                                project_id=project_id,
                                conversation_id=projection.conversation_id,
                                subject_type=projection.subject_type,
                                subject_id=projection.subject_id,
                                card_kind=projection.card_kind,
                                provider_message_id=projection.provider_message_id,
                                lifecycle_status="recalled",
                                resolved_by="system_restart",
                                resulting_state="idle",
                            )
                    except Exception:
                        logger.warning(
                            "Failed to reconcile recovered card %s",
                            projection.projection_key,
                            exc_info=True,
                        )
    finally:
        for bridge in bridges.values():
            if bridge is not None:
                await bridge.close()


async def _reconcile_owner_assessment_cards() -> int:
    """Recall stale Owner companion forms without rewriting chat history."""

    settings = get_settings()
    accounts = get_feishu_bot_accounts(settings)
    if not accounts:
        return 0
    bridges = {
        account_id: create_feishu_bridge(settings, account_id)
        for account_id in accounts
    }
    updated = 0
    try:
        with session_scope() as session:
            service = ProjectService(session)
            for decision in service.reconcile_project_owner_assessments():
                project = service.get_project(decision.project_id)
                if (
                    project is None
                    or not project.feishu_chat_id
                    or not decision.card_message_id
                ):
                    continue
                bridge = bridges.get(project.bot_account_id)
                if bridge is None:
                    continue
                try:
                    await bridge.delete_message(decision.card_message_id)
                    service.upsert_card_projection(
                        project_id=project.id,
                        projection_key=f"decision:{decision.id}",
                        subject_type="project_decision",
                        subject_id=decision.id,
                        card_kind="decision",
                        provider_message_id=decision.card_message_id,
                        lifecycle_status=decision.status,
                        resolved_by="startup_reconciliation",
                        resulting_state=project.execution_status,
                    )
                    updated += 1
                except Exception:
                    logger.warning(
                        "Failed to reconcile Owner assessment card %s",
                        decision.id,
                        exc_info=True,
                    )
    finally:
        for bridge in bridges.values():
            if bridge is not None:
                await bridge.close()
    return updated


async def _reconcile_closed_interaction_projections() -> int:
    """Repair old question projections left pending after their form was recalled."""

    settings = get_settings()
    accounts = get_feishu_bot_accounts(settings)
    if not accounts:
        return 0
    bridges = {
        account_id: create_feishu_bridge(settings, account_id)
        for account_id in accounts
    }
    repaired = 0
    try:
        with session_scope() as session:
            service = ProjectService(session)
            for project in service.list_projects():
                bridge = bridges.get(project.bot_account_id)
                if bridge is None:
                    continue
                for projection in service.get_project_card_projections(project.id):
                    if not (
                        projection.subject_type == "question_interaction"
                        and projection.lifecycle_status == "pending"
                    ):
                        continue
                    interaction = service.get_question_interaction(
                        projection.subject_id
                    )
                    if interaction is None or interaction.status == "pending":
                        continue
                    recalled = False
                    if projection.provider_message_id:
                        try:
                            await bridge.delete_message(projection.provider_message_id)
                            recalled = True
                        except Exception:
                            logger.warning(
                                "Could not recall closed question projection %s; "
                                "the historical card is left unchanged",
                                projection.projection_key,
                            )
                    service.upsert_card_projection(
                        projection_key=projection.projection_key,
                        project_id=projection.project_id,
                        conversation_id=projection.conversation_id,
                        subject_type=projection.subject_type,
                        subject_id=projection.subject_id,
                        card_kind=projection.card_kind,
                        provider_message_id=projection.provider_message_id,
                        lifecycle_status=(
                            "recalled" if recalled else "closed_unrecalled"
                        ),
                        resolved_by=interaction.resolution_source or "reconciliation",
                        resulting_state="idle",
                    )
                    repaired += 1
    finally:
        for bridge in bridges.values():
            if bridge is not None:
                await bridge.close()
    return repaired


async def _drain_notification_outbox(
    bind: Any,
    runtime: Any,
    settings: Any,
    ws_clients: dict[str, FeishuWebSocketClient] | None = None,
) -> None:
    accounts = get_feishu_bot_accounts(settings)
    if not accounts:
        return
    bridges = {
        account_id: create_feishu_bridge(settings, account_id)
        for account_id in accounts
    }
    try:
        while True:
            await asyncio.sleep(5)
            with session_scope() as session:
                service = ProjectService(session)
                recovered_claims = service.recover_inflight_notifications(
                    older_than_seconds=60,
                )
                if any(recovered_claims.values()):
                    logger.warning(
                        "Recovered expired notification claims: %s",
                        recovered_claims,
                    )
                for notification in service.list_retryable_notifications():
                    bridge = bridges.get(notification.bot_account_id)
                    if bridge is None:
                        service.finish_notification(
                            notification.id,
                            error=(
                                "Feishu bot account is not configured: "
                                f"{notification.bot_account_id}"
                            ),
                            retryable=False,
                        )
                        continue
                    try:
                        message_id = await retry_notification(service, bridge, notification)
                        from mac_system.core.notification_service import (
                            reconcile_retried_root_report,
                        )

                        reconcile_retried_root_report(
                            service,
                            notification,
                            message_id=message_id,
                        )
                    except Exception:
                        logger.warning(
                            "Notification retry failed id=%s",
                            notification.id,
                            exc_info=True,
                        )
                # A sent report must never be resent to retry a failed Owner
                # dispatch. Pending reviews are durable independent jobs;
                # the account process deduplicates repeated acknowledgements.
                pending_reviews = list(session.exec(
                    select(ProjectOwnerReview)
                    .where(ProjectOwnerReview.status == "pending")
                    .order_by(ProjectOwnerReview.created_at)
                    .limit(100)
                ).all())
                for review in pending_reviews:
                    try:
                        project = service.require_project(review.project_id)
                        if settings.feishu_transport == "websocket":
                            client = (ws_clients or {}).get(project.bot_account_id)
                            if client is None:
                                raise RuntimeError(
                                    "Project account process is unavailable for Owner review"
                                )
                            await client.submit_owner_review(project.id, review.id)
                        else:
                            from mac_system.core.root_agent import schedule_project_owner_review

                            schedule_project_owner_review(bind, runtime, settings, review.id)
                    except Exception:
                        logger.warning(
                            "Owner review dispatch failed; kept pending id=%s",
                            review.id,
                            exc_info=True,
                        )
    finally:
        for bridge in bridges.values():
            if bridge is not None:
                await bridge.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from mac_system.core.root_agent import (
        close_root_threads,
        build_restart_recovery_trigger,
        latest_project_human_context,
        register_root_service_loop,
        schedule_project_owner_review,
        schedule_root_turn,
    )
    from mac_system.agents.runtime import build_agent_runtime

    register_root_service_loop(asyncio.get_running_loop())
    init_db()
    from mac_system.core.root_sessions import (
        RootSessionStore,
        prune_redundant_native_event_snapshots,
        recover_interrupted_external_agents,
        recover_interrupted_native_turns,
    )

    with session_scope() as recovery_session:
        recovery_bind = recovery_session.get_bind()
        recovered_projects = recover_interrupted_native_turns(recovery_session)
        recovery_jobs = {
            project_id: job
            for project_id in recovered_projects
            if (job := RootSessionStore(recovery_session).get_restart_recovery(project_id))
            is not None
        }
        recovered_external_agents = recover_interrupted_external_agents(
            recovery_session
        )
        recovery_service = ProjectService(recovery_session)
        owner_review_jobs = [
            (
                review.id,
                review.project_id,
                (
                    project.bot_account_id
                    if (project := recovery_service.get_project(review.project_id))
                    else None
                ),
            )
            for review in recovery_service.list_recoverable_project_owner_reviews()
        ]
        recovered_notifications = recovery_service.recover_inflight_notifications()
        pruned_progress_notifications = (
            recovery_service.prune_sent_progress_notifications()
        )
        pruned_native_events = prune_redundant_native_event_snapshots(
            recovery_session
        )
    if pruned_progress_notifications:
        logger.info(
            "Pruned %s redundant sent progress notification snapshot(s)",
            pruned_progress_notifications,
        )
    if any(recovered_notifications.values()):
        logger.warning(
            "Recovered orphaned notification claims: %s",
            recovered_notifications,
        )
    if any(pruned_native_events.values()):
        logger.info(
            "Pruned redundant native event snapshots: %s",
            pruned_native_events,
        )
    if recovered_projects:
        logger.warning(
            "Recovered %s native Codex turn(s) interrupted by service restart",
            len(recovered_projects),
        )
        await _reconcile_interrupted_root_cards(
            recovered_projects,
            auto_resume_project_ids=set(recovery_jobs),
        )
    reconciled_owner_assessments = await _reconcile_owner_assessment_cards()
    if reconciled_owner_assessments:
        logger.info(
            "Reconciled %s stale Project Owner assessment card(s)",
            reconciled_owner_assessments,
        )
    repaired_interaction_projections = (
        await _reconcile_closed_interaction_projections()
    )
    if repaired_interaction_projections:
        logger.info(
            "Reconciled %s closed question projection(s)",
            repaired_interaction_projections,
        )
    if recovered_external_agents:
        logger.warning(
            "Recovered %s external provider execution(s) interrupted by service restart",
            len(recovered_external_agents),
        )
    settings = get_settings()
    logger.info("Using Codex executable: %s", settings.codex_bin)
    recovery_dispatches: dict[str, dict[str, Any]] = {}
    for project_id, (
        _thread_id,
        user_request,
        _request_origin,
        parent_decision_id,
    ) in recovery_jobs.items():
        with Session(recovery_bind) as context_session:
            service = ProjectService(context_session)
            project = service.require_project(project_id)
            human_context = latest_project_human_context(
                service,
                project_id,
                parent_decision_id=parent_decision_id,
            )
        recovery_dispatches[project_id] = {
            "project_id": project_id,
            "bot_account_id": project.bot_account_id,
            "trigger": build_restart_recovery_trigger(
                user_request,
                human_context,
            ),
            "visible_request": user_request,
            "parent_decision_id": parent_decision_id,
        }
    ws_clients: dict[str, FeishuWebSocketClient] = {}
    delegated_recovery_projects: set[str] = set()
    delegated_owner_reviews: set[str] = set()
    if (
        settings.feishu_transport == "websocket"
    ):
        for account in get_feishu_bot_accounts(settings).values():
            account_recovery_turns = tuple(
                job
                for job in recovery_dispatches.values()
                if job["bot_account_id"] == account.id
            )
            delegated_recovery_projects.update(
                str(job["project_id"]) for job in account_recovery_turns
            )
            account_owner_reviews = tuple(
                review_id
                for review_id, _project_id, bot_account_id in owner_review_jobs
                if bot_account_id == account.id
            )
            delegated_owner_reviews.update(account_owner_reviews)
            client = FeishuWebSocketClient(
                account,
                startup_root_turns=account_recovery_turns,
                startup_owner_reviews=account_owner_reviews,
            )
            client.start()
            ws_clients[account.id] = client
    default_account_id = settings.feishu_default_bot_account_id
    ws_client = ws_clients.get(default_account_id) or next(
        iter(ws_clients.values()), None
    )
    app.state.ws_clients = ws_clients
    # Compatibility for local test injection and older callers.
    app.state.ws_client = ws_client
    # The runtime registry is cheap to construct and is also needed by the
    # durable Owner wake-up loop after startup recovery has finished.
    runtime = build_agent_runtime(settings, get_agent_config())
    notification_task = asyncio.create_task(
        _drain_notification_outbox(recovery_bind, runtime, settings, ws_clients),
        name="notification-outbox",
    )
    if recovery_dispatches:
        for project_id, job in recovery_dispatches.items():
            if project_id in delegated_recovery_projects:
                continue
            if settings.feishu_transport == "websocket":
                logger.error(
                    "Could not dispatch Root recovery %s: account process unavailable",
                    project_id,
                )
                continue
            schedule_root_turn(
                recovery_bind,
                runtime,
                settings,
                project_id,
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
            "Dispatched %s interrupted native Codex turn(s) for safe continuation",
            len(recovery_dispatches),
        )
    if owner_review_jobs:
        for review_id, _project_id, _bot_account_id in owner_review_jobs:
            if review_id in delegated_owner_reviews:
                continue
            if settings.feishu_transport == "websocket":
                logger.error(
                    "Could not dispatch Project Owner review %s: account process unavailable",
                    review_id,
                )
                continue
            schedule_project_owner_review(
                recovery_bind,
                runtime,
                settings,
                review_id,
            )
        logger.warning(
            "Scheduled %s interrupted Project Owner review(s) for continuation",
            len(owner_review_jobs),
        )
    try:
        yield
    finally:
        notification_task.cancel()
        await asyncio.gather(
            notification_task, return_exceptions=True
        )
        await close_root_threads()
        register_root_service_loop(None)
        # Each account can need up to 50s to stop its native execution. Stop
        # independent workers concurrently within launchd's shutdown grace.
        stop_results = await asyncio.gather(
            *(asyncio.to_thread(client.stop) for client in ws_clients.values()),
            return_exceptions=True,
        )
        for account_id, result in zip(ws_clients, stop_results):
            if isinstance(result, BaseException):
                logger.error("Account worker shutdown failed account=%s: %s", account_id, result)
        app.state.ws_clients = {}
        if ws_client is not None:
            app.state.ws_client = None


app = FastAPI(title="MAC System API", lifespan=lifespan)
app.include_router(feishu_routes.router)
app.include_router(admin_routes.router)


@app.get("/health")
def health() -> dict[str, Any]:
    from mac_system.feishu import ws_status

    return {"status": "ok", "feishu_ws": ws_status.list_statuses()}


@app.get("/files/{project_id}/{file_path:path}", include_in_schema=False)
def get_project_file(
    project_id: str,
    file_path: str,
    sig: str = Query(default=""),
    service: ProjectService = Depends(get_project_service),
) -> FileResponse:
    """Serve one signed file link previously shown in a project card."""

    project = service.get_project(project_id)
    resolved = resolve_project_file(project, file_path) if project is not None else None
    if project is None or resolved is None:
        raise HTTPException(status_code=404, detail="FILE_NOT_FOUND")
    path, relative_path = resolved
    if not verify_project_file_signature(
        project.id, relative_path, sig, service.settings
    ):
        raise HTTPException(status_code=403, detail="INVALID_FILE_LINK")
    media_type = _PROJECT_TEXT_MEDIA_TYPES.get(path.suffix.lower())
    media_type = media_type or mimetypes.guess_type(path.name)[0]
    media_type = media_type or "application/octet-stream"
    if media_type in _UTF8_APPLICATION_MEDIA_TYPES:
        media_type = f"{media_type}; charset=utf-8"
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
            bot_account_id=req.bot_account_id,
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
    clients = getattr(app.state, "ws_clients", {})
    client = clients.get(project.bot_account_id)
    settings = service.settings
    account_owned = (
        settings.feishu_transport == "websocket"
        and project.bot_account_id in get_feishu_bot_accounts(settings)
    )
    if client is not None:
        try:
            await client.submit_project_message(project.id, req.content)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="PROJECT_MESSAGE_ACKNOWLEDGEMENT_UNKNOWN"
            ) from exc
        except (RuntimeError, OSError, EOFError) as exc:
            raise HTTPException(
                status_code=503, detail="PROJECT_ACCOUNT_UNAVAILABLE"
            ) from exc
    elif account_owned:
        raise HTTPException(status_code=503, detail="PROJECT_ACCOUNT_UNAVAILABLE")
    else:
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
