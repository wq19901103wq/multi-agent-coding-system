import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any, Literal

from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import ClaudeCodeRuntime, FakeRuntime
from mac_system.config import get_settings
from mac_system.core.project_service import (
    ApprovalNotFoundError,
    InvalidStateTransitionError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.core.notification_service import retry_notification
from mac_system.database import init_db, session_scope
from mac_system.feishu import NullFeishuBridge
from mac_system.feishu.handlers import FeishuHandlerContext, handle_card_action, handle_text_message
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.feishu.ws_client import FeishuWebSocketClient
from mac_system.models import (
    Approval,
    FeishuEventLog,
    Project,
    ProjectCreate,
    ProjectOut,
    ProjectUser,
)
from mac_system.state_machine import StateMachine

from mac_system.api import admin_routes, feishu_routes
from mac_system.api.dependencies import (
    _build_orchestrator,
    get_orchestrator,
    get_project_service,
    require_control_api_token,
)

logger = logging.getLogger(__name__)


def _confirmation_source_stage(confirmation_state: str) -> str:
    """Map a human-confirmation state back to the agent stage that produced it."""
    return {
        "REQUIREMENT_CONFIRM_PENDING": "PLANNING_REQUIREMENTS",
        "ARCHITECTURE_CONFIRM_PENDING": "PLANNING_ARCHITECTURE",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "PLANNING_IMPLEMENTATION_SPECS",
        "BUG_FIX_CONFIRM_PENDING": "BUG_ANALYSIS",
        "OUTLINE_CONFIRM_PENDING": "DOC_OUTLINE",
        "CODING_CONFIRM_PENDING": "CODING",
        "TESTING_CONFIRM_PENDING": "TESTING",
        "REVIEW_CONFIRM_PENDING": "REVIEW",
        "ACCEPTANCE_PENDING": "ACCEPTANCE",
    }.get(confirmation_state, confirmation_state)


async def _resume_recovered_project(project_id: str) -> None:
    """Restart an interrupted flow until it reaches a stable boundary."""
    with session_scope() as session:
        service = ProjectService(session)
        project = service.get_project(project_id)
        if project is None or project.status != "active":
            return
        uses_agent_loop = project.context.get("orchestration_mode") == "agent_loop"
        if not uses_agent_loop:
            state_machine = StateMachine(project.task_type)
            if state_machine.is_terminal(project.current_state) or state_machine.is_idle(
                project.current_state
            ):
                return

        orchestrator = _build_orchestrator(session)
        try:
            # Restart recovery is an external resume path and follows the same
            # drive-until-boundary contract as a Coordinator-resolved question.
            drive = getattr(orchestrator, "tick_until_stable", None)
            if drive is not None:
                await drive(project_id, max_ticks=30)
            else:
                # Compatibility for lightweight/test orchestrators that expose
                # only the original single-step tick contract.
                for _ in range(30):
                    current = service.require_project(project_id)
                    before = current.current_state
                    await orchestrator.tick(project_id)
                    session.expire_all()
                    current = service.require_project(project_id)
                    machine = StateMachine(current.task_type)
                    if (
                        current.current_state == before
                        or machine.is_terminal(current.current_state)
                        or machine.is_idle(current.current_state)
                    ):
                        break
            project = service.require_project(project_id)
            await service.log_event(
                project_id=project_id,
                level="INFO",
                message="Interrupted agent task rescheduled after service restart",
                meta={"state": project.current_state},
            )
        except Exception as exc:
            logger.exception("Failed to resume project %s after restart", project_id)
            await service.log_event(
                project_id=project_id,
                level="ERROR",
                message=f"Failed to resume interrupted agent task: {exc}",
                meta={"state": project.current_state},
            )
        finally:
            bridge = orchestrator.feishu_bridge
            if bridge is not None:
                await bridge.close()


async def _drain_notification_outbox() -> None:
    """Continuously deliver persisted user-visible intents that previously failed."""
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
                pending = service.list_retryable_notifications()
                for notification in pending:
                    try:
                        await retry_notification(service, bridge, notification)
                    except Exception as exc:
                        logger.warning(
                            "Notification retry failed id=%s attempt=%d: %s",
                            notification.id,
                            notification.attempt_count + 1,
                            exc,
                        )
    except asyncio.CancelledError:
        raise
    finally:
        await bridge.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from mac_system.core.coordinator_engine import register_coordinator_service_loop

    register_coordinator_service_loop(asyncio.get_running_loop())
    init_db()
    recovered_project_ids: set[str] = set()
    with session_scope() as session:
        service = ProjectService(session)
        migrated_secrets = service.reconcile_secret_answers()
        backfilled_question_keys = service.backfill_question_decision_keys()
        recovered_grants = service.reconcile_runtime_capability_grants()
        resolved_feedback = service.reconcile_feedback_from_passing_tester_evidence()
        if any(
            (migrated_secrets, backfilled_question_keys, recovered_grants, resolved_feedback)
        ):
            logger.info(
                "Reconciled persisted MAC state: secrets=%d question_keys=%d "
                "grant_projects=%d feedback=%d",
                migrated_secrets,
                backfilled_question_keys,
                recovered_grants,
                resolved_feedback,
            )
        recovered_notifications = service.recover_stuck_notifications(
            max_age_seconds=0
        )
        if recovered_notifications:
            logger.warning(
                "Recovered %d interrupted notification deliveries",
                len(recovered_notifications),
            )
        recovered = service.recover_stale_tasks()
        if recovered:
            logger.warning(
                "Recovered %d stale running tasks as failed", len(recovered)
            )
            recovered_project_ids = {task.project_id for task in recovered}
        recovered_project_ids.update(service.reconcile_project_invariants())
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
    recovery_tasks = {
        asyncio.create_task(
            _resume_recovered_project(project_id),
            name=f"resume-{project_id}",
        )
        for project_id in recovered_project_ids
    }
    notification_retry_task = asyncio.create_task(
        _drain_notification_outbox(), name="notification-outbox"
    )
    try:
        yield
    finally:
        from mac_system.core.coordinator_engine import (
            close_coordinator_runtimes,
            register_coordinator_service_loop,
        )

        await close_coordinator_runtimes()
        register_coordinator_service_loop(None)
        cancelled_agent_ids = ClaudeCodeRuntime.cancel_all_active()
        if cancelled_agent_ids:
            logger.info(
                "Terminated %d active Agent process groups during shutdown: %s",
                len(cancelled_agent_ids),
                cancelled_agent_ids,
            )
        for task in recovery_tasks:
            if not task.done():
                task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        notification_retry_task.cancel()
        await asyncio.gather(notification_retry_task, return_exceptions=True)
        # A task can register a process while cancellation is propagating.
        ClaudeCodeRuntime.cancel_all_active()
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
            initial_context=(
                {"confirmed_brief": req.confirmed_brief}
                if req.confirmed_brief is not None
                else None
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")


class DecisionRequest(BaseModel):
    approval_id: str
    action: str
    message: str | None = None
    target_type: str | None = None
    question_id: str | None = None


class DecisionResponse(BaseModel):
    project_id: str
    previous_state: str
    current_state: str
    new_project_id: str | None = None


@app.post("/projects/{project_id}/decisions", response_model=DecisionResponse)
async def submit_decision(
    project_id: str,
    req: DecisionRequest,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> DecisionResponse:
    # Special action: restart the project under a different task type.
    if req.action == "restart_with_type":
        target_type = req.target_type or req.message
        if not target_type:
            raise HTTPException(status_code=400, detail="TARGET_TYPE_REQUIRED")
        try:
            old_project = service.require_project(project_id)
            previous_state = old_project.current_state
            new_project = await service.restart_project_with_type(
                project_id, target_type, triggered_by="user"
            )
        except ProjectNotFoundError:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await orchestrator.tick(new_project.id)
        return DecisionResponse(
            project_id=project_id,
            previous_state=previous_state,
            current_state="CANCELLED",
            new_project_id=new_project.id,
        )

    try:
        approval = service.get_approval(req.approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="APPROVAL_NOT_FOUND")
    if approval.project_id != project_id:
        raise HTTPException(
            status_code=400,
            detail="APPROVAL_PROJECT_MISMATCH",
        )

    # Input approvals created for an Agent QuestionInteraction are not ordinary
    # state-machine approvals: the answer must be persisted on the current
    # Question first.  Otherwise API callers can transition back to the Agent
    # while leaving the interaction pending, and the next card may surface an
    # unrelated, stale ``clarification_answer`` from project context.
    interaction_id = (approval.payload or {}).get("question_interaction_id")
    if (
        getattr(approval, "approval_type", None) == "input"
        and req.action == "answer"
        and interaction_id
    ):
        answer = str(req.message or "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="ANSWER_REQUIRED")
        interaction = service.get_question_interaction(interaction_id)
        if interaction is None or interaction.status != "pending":
            raise HTTPException(status_code=400, detail="QUESTION_INTERACTION_NOT_PENDING")
        questions = service.get_questions_for_interaction(interaction_id)
        pending_questions = [question for question in questions if question.status == "pending"]
        target_question = (
            next(
                (
                    question
                    for question in pending_questions
                    if question.question_id == req.question_id
                ),
                None,
            )
            if req.question_id
            else (pending_questions[0] if len(pending_questions) == 1 else None)
        )
        if target_question is None:
            detail = "QUESTION_NOT_FOUND" if req.question_id else "QUESTION_ID_REQUIRED"
            raise HTTPException(status_code=400, detail=detail)

        keep_external_pending = (
            getattr(interaction, "stage", None) == "acceptance"
            and answer == "not_verified"
            and bool(getattr(target_question, "affected_trace_ids", None))
        )
        await service.submit_question_answer(
            target_question.id,
            answer,
            answer,
            False,
            "api_user",
            commit=False,
            **({"keep_pending": True} if keep_external_pending else {}),
        )
        if keep_external_pending:
            service.set_execution_status(
                project_id,
                "waiting_external",
                reason="等待用户完成外部人工验收",
                commit=False,
            )
        if not service.interaction_is_resolvable(interaction_id):
            service.session.commit()
            project = service.require_project(project_id)
            return DecisionResponse(
                project_id=project_id,
                previous_state=project.previous_state or "",
                current_state=project.current_state,
            )

        await service.resolve_question_interaction(interaction_id, commit=False)
        await service.respond_approval(
            approval.id, "answer", "api_user", commit=False
        )
        service.session.commit()
        project = await service.transition_state(
            project_id,
            "answer",
            payload={"resume_state": interaction.resume_state},
            triggered_by="api_user",
        )
        await orchestrator.tick_until_stable(project_id)
        return DecisionResponse(
            project_id=project_id,
            previous_state=project.previous_state or "",
            current_state=project.current_state,
        )

    is_rejection_feedback = req.action == "reject" and bool(req.message)
    is_rework_feedback = (
        approval.state == "REWORK"
        and req.action.startswith("to_")
        and bool(req.message)
    )
    if is_rejection_feedback or is_rework_feedback:
        source_stage = _confirmation_source_stage(approval.state)
        feedback_kind = (
            "human_rejection" if is_rejection_feedback else "human_rework"
        )
        recommended_target = (
            req.action.removeprefix("to_") if is_rework_feedback else None
        )
        await service.create_feedback_packet(
            project_id=project_id,
            source_stage=source_stage,
            source_task_id=approval.task_id,
            source_round=approval.payload.get("round_number", 1),
            kind=feedback_kind,
            summary=req.message,
            blocking=True,
            failure_class=feedback_kind,
            recommended_rework_target=recommended_target,
            user_comment=req.message,
            commit=False,
        )

    try:
        approval, project = await service.resolve_approval(
            approval_id=req.approval_id,
            event=req.action,
            responded_by="api_user",
            payload={"message": req.message} if req.message else None,
        )
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="APPROVAL_NOT_FOUND")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    except InvalidStateTransitionError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "INVALID_STATE_TRANSITION",
                "message": str(e),
                "current_state": e.current_state,
            },
        )

    # Trigger next tick if the new state is auto-executable
    await orchestrator.tick(project_id)

    return DecisionResponse(
        project_id=project_id,
        previous_state=project.previous_state or "",
        current_state=project.current_state,
    )


class UserMessageRequest(BaseModel):
    content: str


class UserMessageResponse(BaseModel):
    project_id: str
    reply: str
    state: str


_ROLLBACK_TARGETS = {
    "rollback_to_requirements": "requirements",
    "rollback_to_architecture": "architecture",
    "rollback_to_specs": "specs",
    "rollback_to_coding": "coding",
    "rollback_to_analysis": "analysis",
    "rollback_to_outline": "outline",
}


async def _apply_project_manager_action(
    *,
    service: ProjectService,
    orchestrator: AgentOrchestrator,
    project_id: str,
    pending_approval: dict[str, Any] | None,
    suggested_action: str | None,
    content: str,
) -> bool:
    """Apply one project-manager decision using the approval's real events.

    The project-manager schema exposes semantic rollback actions while state
    machine approvals normally expose only ``reject``.  Normalize that mismatch
    here and preserve the user's defect report as structured feedback.
    """
    if not pending_approval or not suggested_action:
        return False

    valid_actions = {
        opt["action"] for opt in pending_approval.get("options", [])
        if opt.get("action")
    }
    approval = service.get_approval(pending_approval["approval_id"])
    action = suggested_action
    if action == "continue" and "confirm" in valid_actions:
        action = "confirm"
    rollback_target = _ROLLBACK_TARGETS.get(action)
    if rollback_target and "reject" in valid_actions:
        action = "reject"
    elif rollback_target and pending_approval["type"] == "input":
        project = service.require_project(project_id)
        machine = StateMachine(project.task_type)
        target_execution = next(
            (
                stage["execution_state"]
                for stage in machine.profile.get("stages", [])
                if stage.get("id") == rollback_target
            ),
            None,
        )
        action = next(
            (
                event
                for event, target in machine.get_state(approval.state)
                .get("next_states", {})
                .items()
                if target_execution is not None and target == target_execution
            ),
            suggested_action,
        )

    if action == "answer" and pending_approval["type"] == "input":
        await service.resolve_approval(
            approval.id,
            "answer",
            responded_by="api_user",
            payload={"clarification_answer": content},
        )
        await orchestrator.tick_until_stable(project_id)
        return True
    if (
        rollback_target
        and pending_approval["type"] == "input"
        and action.startswith("needs_")
    ):
        interaction_id = approval.payload.get("question_interaction_id")
        interaction = (
            service.get_question_interaction(interaction_id)
            if interaction_id
            else None
        )
        source_state = (
            interaction.resume_state if interaction is not None else approval.state
        )
        await service.create_feedback_packet(
            project_id=project_id,
            source_stage=_confirmation_source_stage(source_state),
            source_task_id=(
                interaction.origin_task_id if interaction is not None else approval.task_id
            ),
            source_round=(
                interaction.origin_round
                if interaction is not None
                else approval.payload.get("round_number", 1)
            ),
            kind="human_correction",
            summary=content,
            blocking=True,
            failure_class="invalid_question_premise",
            recommended_rework_target=rollback_target,
            user_comment=content,
            commit=False,
        )
        if interaction_id:
            service.expire_question_interaction_for_correction(
                interaction_id,
                commit=False,
            )
        await service.resolve_approval(
            approval.id,
            action,
            responded_by="api_user",
            payload={"message": content},
        )
        await orchestrator.tick_until_stable(project_id)
        return True
    if action not in valid_actions:
        logger.warning(
            "Ignoring project-manager action %s; approval %s accepts %s",
            suggested_action,
            approval.id,
            sorted(valid_actions),
        )
        return False

    if action == "reject" and content.strip():
        source_stage = _confirmation_source_stage(approval.state)
        await service.create_feedback_packet(
            project_id=project_id,
            source_stage=source_stage,
            source_task_id=approval.task_id,
            source_round=approval.payload.get("round_number", 1),
            kind="human_rejection",
            summary=content,
            blocking=True,
            failure_class="human_rejection",
            recommended_rework_target=rollback_target,
            user_comment=content,
            commit=False,
        )
    await service.resolve_approval(
        approval.id,
        action,
        responded_by="api_user",
        payload={"message": content} if content.strip() else None,
    )
    await orchestrator.tick_until_stable(project_id)
    return True


async def _process_user_message(project_id: str, content: str) -> None:
    """Background task that runs the project_manager agent and applies its decision."""
    with session_scope() as session:
        orchestrator = _build_orchestrator(session)
        try:
            service = orchestrator.project_service
            project = service.require_project(project_id)
            if project.context.get("orchestration_mode") == "agent_loop":
                if not await orchestrator.steer_coordinator(project_id, content):
                    await orchestrator.run_coordinator(
                        project_id, "New user message:\n" + content
                    )
                return
            pending_approval = service.get_project_status(project_id).get("pending_approval")
            input_data = {
                "mode": "user_message",
                "message": content,
                "pending_approval": pending_approval,
            }
            output = await orchestrator._execute_auxiliary_agent(
                project,
                agent_type="project_manager",
                input_data=input_data,
            )
            if output is None:
                return

            # This endpoint carries an explicit user-authored message.  The
            # Project Manager may classify how it should be routed, but must
            # never replace the user's correction with its own preferred
            # answer to the pending question.
            answer = content
            await _apply_project_manager_action(
                service=service,
                orchestrator=orchestrator,
                project_id=project_id,
                pending_approval=pending_approval,
                suggested_action=output.get("suggested_action"),
                content=answer if output.get("suggested_action") == "answer" else content,
            )
        finally:
            if orchestrator.feishu_bridge is not None:
                try:
                    await orchestrator.feishu_bridge.close()
                except Exception:
                    pass


@app.post("/projects/{project_id}/messages", response_model=UserMessageResponse)
async def submit_message(
    project_id: str,
    req: UserMessageRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> UserMessageResponse:
    try:
        project = service.require_project(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")

    if project.context.get("orchestration_mode") == "agent_loop":
        if isinstance(orchestrator.runtime, FakeRuntime):
            if not await orchestrator.steer_coordinator(project_id, req.content):
                await orchestrator.run_coordinator(
                    project_id, "New user message:\n" + req.content
                )
        else:
            background_tasks.add_task(_process_user_message, project_id, req.content)
        return UserMessageResponse(
            project_id=project_id,
            reply="收到，Coordinator 正在处理。",
            state=project.current_state,
        )

    # Fast-running test runtime can stay synchronous so existing unit tests keep
    # asserting on the real agent reply. Production runtimes are executed in the
    # background to avoid blocking the HTTP request for minutes.
    if isinstance(orchestrator.runtime, FakeRuntime):
        pending_approval = service.get_project_status(project_id).get("pending_approval")
        input_data = {
            "mode": "user_message",
            "message": req.content,
            "pending_approval": pending_approval,
        }
        output = await orchestrator._execute_auxiliary_agent(
            project,
            agent_type="project_manager",
            input_data=input_data,
        )
        if output is None:
            return UserMessageResponse(
                project_id=project_id,
                reply="暂时无法处理你的消息，请稍后再试。",
                state=project.current_state,
            )
        reply = output.get("reply", "收到。")
        # Preserve explicit user text verbatim.  ``clarification_answer`` is
        # for autonomous/default decisions, not for rewriting user input.
        answer = req.content
        await _apply_project_manager_action(
            service=service,
            orchestrator=orchestrator,
            project_id=project_id,
            pending_approval=pending_approval,
            suggested_action=output.get("suggested_action"),
            content=answer if output.get("suggested_action") == "answer" else req.content,
        )
        return UserMessageResponse(
            project_id=project_id,
            reply=reply,
            state=project.current_state,
        )

    background_tasks.add_task(_process_user_message, project_id, req.content)
    return UserMessageResponse(
        project_id=project_id,
        reply="收到，正在处理你的消息。",
        state=project.current_state,
    )


class CommandRequest(BaseModel):
    command: str = Field(..., pattern="^(pause|resume|cancel|rollback|status)$")
    target_state: str | None = None


class CommandResponse(BaseModel):
    project_id: str
    command: str
    previous_state: str
    current_state: str
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
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")

    previous_state = project.current_state
    message = ""

    if req.command == "pause":
        project.status = "paused"
        project.previous_state = previous_state
        project.updated_at = datetime.now(timezone.utc)
        message = "项目已暂停"
    elif req.command == "resume":
        if project.status != "paused":
            raise HTTPException(status_code=400, detail="PROJECT_NOT_PAUSED")
        target = project.previous_state or previous_state
        project.status = "active"
        project.current_state = target
        project.previous_state = previous_state
        project.updated_at = datetime.now(timezone.utc)
        message = "项目已恢复"
        await orchestrator.tick(project_id)
    elif req.command == "cancel":
        await orchestrator.cancel_project(project_id, reason="通过 /projects/{id}/commands 取消")
        project = service.require_project(project_id)
        message = "项目已取消"
    elif req.command == "rollback":
        if not req.target_state:
            raise HTTPException(status_code=400, detail="TARGET_STATE_REQUIRED")
        state_machine = StateMachine(project.task_type)
        if req.target_state not in state_machine.states:
            raise HTTPException(status_code=400, detail="TARGET_STATE_UNREACHABLE")
        project.current_state = req.target_state
        project.previous_state = previous_state
        project.updated_at = datetime.now(timezone.utc)
        message = f"项目已回退到 {req.target_state}"
    elif req.command == "status":
        message = "项目状态查询成功"

    service.session.commit()

    return CommandResponse(
        project_id=project_id,
        command=req.command,
        previous_state=previous_state,
        current_state=project.current_state,
        message=message,
    )


def _parse_inject_created_at(raw: str | datetime | None) -> datetime | None:
    """Parse an injected event timestamp from ISO 8601 or epoch (ms/s)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    try:
        # Numeric epoch timestamp (Feishu create_time is in milliseconds).
        if raw.replace(".", "", 1).replace("-", "", 1).isdigit():
            ts = float(raw)
            if ts > 1_000_000_000_000:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


class InjectCardActionRequest(BaseModel):
    open_id: str
    approval_id: str
    action: str
    state: str = ""
    option_key: str | None = None
    decision_id: str | None = None
    chat_id: str | None = None
    chat_type: str = "p2p"
    extras: dict[str, Any] = Field(default_factory=dict)
    created_at: str | datetime | None = None
    event_id: str | None = None


async def _run_inject_card_action(action: ButtonAction) -> None:
    """Run an injected card action in a fresh DB session/orchestrator."""
    try:
        with session_scope() as session:
            orchestrator = _build_orchestrator(session)
            ctx = FeishuHandlerContext(
                bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
                service=orchestrator.project_service,
                orchestrator=orchestrator,
            )
            await handle_card_action(ctx, action)
    except Exception:
        # Background tasks otherwise surface only as "exception was never
        # retrieved", which leaves the E2E driver waiting with no useful cause.
        logging.getLogger(__name__).exception(
            "Background injected card action failed: action=%s approval_id=%s",
            action.action,
            action.approval_id,
        )


@app.post("/test/inject_card_action")
async def inject_card_action(
    req: InjectCardActionRequest,
    request: Request,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to simulate a Feishu card button click.

    This lets an automated end-to-end driver "click" approval/menu cards
    without a real Feishu user interaction.  It is disabled unless
    ``FEISHU_TEST_MODE`` is enabled.

    For production runtimes the card action is executed in the background so
    the HTTP request does not block for minutes while the orchestrator ticks
    through agent executions.  FakeRuntime tests still run synchronously so
    assertions on the immediate state continue to work.
    """
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    action = ButtonAction(
        open_id=req.open_id,
        approval_id=req.approval_id,
        action=req.action,
        state=req.state,
        option_key=req.option_key,
        decision_id=req.decision_id,
        extras=req.extras,
        chat_id=req.chat_id,
        chat_type=req.chat_type,
        created_at=_parse_inject_created_at(req.created_at),
    )

    if isinstance(orchestrator.runtime, FakeRuntime):
        ctx = FeishuHandlerContext(
            bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
            service=orchestrator.project_service,
            orchestrator=orchestrator,
        )
        try:
            await handle_card_action(ctx, action)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("inject_card_action failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"inject_card_action failed: {exc}")
        return {"status": "ok"}

    # Real runtimes: route through the WS client so the injected action follows
    # the same staleness/deduplication/serialization path as live Feishu events.
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        asyncio.create_task(ws_client.inject_card_action(action, req.event_id))
        return {"status": "ok"}

    # Fallback if the WS client is not available.
    asyncio.create_task(_run_inject_card_action(action))
    return {"status": "ok"}


class InjectTextMessageRequest(BaseModel):
    open_id: str
    text: str
    chat_id: str | None = None
    chat_type: str = "p2p"
    message_id: str = "msg_test"
    created_at: str | datetime | None = None
    event_id: str | None = None


class SeedFeishuInteractionRequest(BaseModel):
    kind: Literal[
        "planning_rejection",
        "architecture_confirmation",
        "coding_confirmation",
        "requirement_confirmation",
        "requirement_custom_question",
        "acceptance_question",
        "runtime_recovery",
        "active_task_cancel",
    ]
    open_id: str
    chat_id: str
    chat_type: str = "group"


@app.post("/test/inject_text_message")
async def inject_text_message(
    req: InjectTextMessageRequest,
    request: Request,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to simulate a Feishu text message."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    msg = TextMessage(
        open_id=req.open_id,
        message_id=req.message_id,
        text=req.text,
        is_command=req.text.startswith("/"),
        chat_id=req.chat_id,
        chat_type=req.chat_type or "p2p",
        created_at=_parse_inject_created_at(req.created_at),
    )

    if isinstance(orchestrator.runtime, FakeRuntime):
        ctx = FeishuHandlerContext(
            bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
            service=orchestrator.project_service,
            orchestrator=orchestrator,
            chat_id=req.chat_id,
            chat_type=req.chat_type,
        )
        try:
            await handle_text_message(ctx, msg)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("inject_text_message failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"inject_text_message failed: {exc}")
        return {"status": "ok"}

    # Real runtimes: route through the WS client so the injected message follows
    # the same staleness/deduplication/serialization path as live Feishu events.
    ws_client = getattr(request.app.state, "ws_client", None)
    if ws_client is not None:
        asyncio.create_task(ws_client.inject_text_message(msg, req.event_id))
        return {"status": "ok"}

    # Fallback if the WS client is not available.
    ctx = FeishuHandlerContext(
        bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
        service=orchestrator.project_service,
        orchestrator=orchestrator,
        chat_id=req.chat_id,
        chat_type=req.chat_type,
    )
    try:
        await handle_text_message(ctx, msg)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("inject_text_message failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"inject_text_message failed: {exc}")
    return {"status": "ok"}


@app.post("/test/seed_feishu_interaction")
async def seed_feishu_interaction(
    req: SeedFeishuInteractionRequest,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Seed one deterministic human-interaction state and send its real card."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    service = orchestrator.project_service
    project = await service.create_project(
        name=f"e2e-{req.kind}",
        task_type="feature",
        description=f"飞书交互验证：{req.kind}",
        approval_mode="normal",
        creator_open_id=req.open_id,
        feishu_chat_id=req.chat_id,
        feishu_chat_type=req.chat_type,
    )

    interaction_id = None
    if req.kind in ("planning_rejection", "requirement_confirmation"):
        project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    elif req.kind == "architecture_confirmation":
        project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    elif req.kind == "coding_confirmation":
        project.current_state = "CODING_CONFIRM_PENDING"
    elif req.kind == "runtime_recovery":
        project.current_state = "CODING_RECOVERY_PENDING"
        project.context = {
            **project.context,
            "last_agent_failure": {
                "state": "CODING",
                "error": "E2E seeded runtime timeout",
                "retry_count": 2,
            },
        }
    elif req.kind == "active_task_cancel":
        project.current_state = "PLANNING_REQUIREMENTS"
    else:
        is_requirement_custom = req.kind == "requirement_custom_question"
        project.current_state = (
            "REQUIREMENT_INPUT_PENDING"
            if is_requirement_custom
            else "ACCEPTANCE_INPUT_PENDING"
        )
        interaction = await service.create_question_interaction(
            project_id=project.id,
            origin_task_id=None,
            origin_round=1,
            resume_state=(
                "PLANNING_REQUIREMENTS"
                if is_requirement_custom
                else "ACCEPTANCE_PENDING"
            ),
            agent="requirements_analyst" if is_requirement_custom else "acceptance_evaluator",
            stage="requirements" if is_requirement_custom else "acceptance",
            batch_key=(
                f"e2e_requirement_custom_{project.id}"
                if is_requirement_custom
                else f"e2e_acceptance_{project.id}"
            ),
            questions=[
                {
                    "question_id": (
                        "q_requirement_scope"
                        if is_requirement_custom
                        else "q_acceptance_mode"
                    ),
                    "decision_key": (
                        "requirements.client_scope"
                        if is_requirement_custom
                        else "acceptance.confirmation_mode"
                    ),
                    "kind": "blocking",
                    "question": (
                        "是否接受默认只支持移动端？"
                        if is_requirement_custom
                        else "最终验收采用哪种确认方式？"
                    ),
                    "reason": (
                        "用于验证选择修改后继续收集具体要求"
                        if is_requirement_custom
                        else "用于验证 Acceptance ask_user 卡片和恢复流转"
                    ),
                    "allow_free_text": is_requirement_custom,
                    "options": (
                        [
                            {
                                "key": "accept_default",
                                "label": "接受默认：只支持移动端",
                                "impact": "按默认范围继续",
                                "recommended": True,
                            },
                            {
                                "key": "provide_custom",
                                "label": "我要修改",
                                "impact": "继续填写具体要求",
                                "recommended": False,
                            },
                        ]
                        if is_requirement_custom
                        else [
                            {
                                "key": "strict",
                                "label": "严格验收",
                                "impact": "所有验收证据必须完整",
                                "recommended": True,
                            },
                            {
                                "key": "manual",
                                "label": "人工复核",
                                "impact": "保留人工最终判断",
                                "recommended": False,
                            },
                        ]
                    ),
                    "affected_trace_ids": ["AC-E2E-001"],
                }
            ],
        )
        interaction_id = interaction.id
        project.context = {
            **project.context,
            "_pending_question_interaction_id": interaction.id,
        }
    service.session.commit()

    if req.kind != "active_task_cancel":
        await orchestrator.tick(project.id)
    approval = service.session.exec(
        select(Approval)
        .where(Approval.project_id == project.id, Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    ).first()
    return {
        "project_id": project.id,
        "approval_id": approval.id if approval else None,
        "interaction_id": interaction_id,
        "state": service.require_project(project.id).current_state,
    }


@app.post("/test/tick/{project_id}")
async def test_tick(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to manually trigger an orchestrator tick."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    await orchestrator.tick(project_id)
    return {"status": "ok"}


@app.get("/test/projects/{project_id}/runtime")
def test_project_runtime(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """Expose DB/runtime activity for true cancellation E2E assertions."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    service.require_project(project_id)
    db_task_ids = [task.id for task in service._running_tasks(project_id)]
    active_ids = set(ClaudeCodeRuntime.active_task_ids())
    return {
        "db_running_task_ids": db_task_ids,
        "process_task_ids": [task_id for task_id in db_task_ids if task_id in active_ids],
    }


@app.get("/test/projects/{project_id}/cards")
def test_project_cards(
    project_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """Expose durable card/provider receipts for real-Feishu E2E observation."""
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    service.require_project(project_id)
    return {
        "items": [
            {
                "projection_key": item.projection_key,
                "subject_type": item.subject_type,
                "subject_id": item.subject_id,
                "card_kind": item.card_kind,
                "provider_message_id": item.provider_message_id,
                "lifecycle_status": item.lifecycle_status,
                "selected_action": item.selected_action,
                "resulting_state": item.resulting_state,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in service.get_project_card_projections(project_id)
        ]
    }


@app.get("/test/feishu/events/{message_id}")
def test_feishu_event(
    message_id: str,
    _auth: None = Depends(require_control_api_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """Return the latest durable WS receipt for a real Feishu message."""
    if not get_settings().feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    event = service.session.exec(
        select(FeishuEventLog)
        .where(FeishuEventLog.message_id == message_id)
        .order_by(FeishuEventLog.created_at.desc())
    ).first()
    if event is None:
        return {"found": False}
    pending_intent = service.get_pending_intent(event.open_id)
    if pending_intent is not None and event.chat_id:
        if pending_intent.chat_id != event.chat_id:
            pending_intent = None

    # CardKit 2.0 message-history responses intentionally hide interactive
    # card bodies.  Correlate the inbound Feishu message with the durable
    # project relation so a transport-level E2E test does not parse UI copy.
    candidate = service.session.exec(
        select(Project)
        .join(ProjectUser, ProjectUser.project_id == Project.id)
        .where(ProjectUser.open_id == event.open_id)
        .where(Project.feishu_chat_id == event.chat_id)
        .order_by(Project.created_at.desc())
    ).first()
    correlated_project_id = None
    if candidate is not None:
        source_time = event.feishu_created_at or event.created_at
        if source_time.tzinfo is not None:
            source_time = source_time.astimezone(timezone.utc).replace(tzinfo=None)
        if candidate.created_at >= source_time - timedelta(seconds=5):
            correlated_project_id = candidate.id

    return {
        "found": True,
        "open_id": event.open_id,
        "chat_id": event.chat_id,
        "status": event.status,
        "detail": event.detail,
        "pending_intent_stage": pending_intent.stage if pending_intent else None,
        "project_id": correlated_project_id,
        "created_at": event.created_at.isoformat(),
    }


@app.post("/test/clear_pending_intent/{open_id}")
async def clear_pending_intent(
    open_id: str,
    _auth: None = Depends(require_control_api_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to clear a user's pending Feishu intent."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    orchestrator.project_service.delete_pending_intent(open_id)
    return {"status": "ok"}
