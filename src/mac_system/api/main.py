import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any

from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import FakeRuntime
from mac_system.config import get_settings
from mac_system.core.project_service import (
    ApprovalNotFoundError,
    InvalidStateTransitionError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.database import init_db, session_scope
from mac_system.feishu import NullFeishuBridge
from mac_system.feishu.handlers import FeishuHandlerContext, handle_card_action, handle_text_message
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.feishu.ws_client import FeishuWebSocketClient
from mac_system.models import ProjectCreate, ProjectOut
from mac_system.state_machine import StateMachine

from mac_system.api import admin_routes, feishu_routes
from mac_system.api.dependencies import (
    _build_orchestrator,
    get_orchestrator,
    get_project_service,
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
    """Restart one interrupted agent state through the normal tick path."""
    with session_scope() as session:
        service = ProjectService(session)
        project = service.get_project(project_id)
        if project is None or project.status != "active":
            return
        state_machine = StateMachine(project.task_type)
        if state_machine.state_type(project.current_state) != "agent_execution":
            return

        orchestrator = _build_orchestrator(session)
        try:
            await orchestrator.tick(project_id)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered_project_ids: set[str] = set()
    with session_scope() as session:
        service = ProjectService(session)
        recovered = service.recover_stale_tasks()
        if recovered:
            logger.warning(
                "Recovered %d stale running tasks as failed", len(recovered)
            )
            recovered_project_ids = {task.project_id for task in recovered}
    settings = get_settings()
    ws_client: FeishuWebSocketClient | None = None
    if (
        settings.feishu_transport == "websocket"
        and settings.feishu_app_id
        and settings.feishu_app_secret
    ):
        ws_client = FeishuWebSocketClient()
        ws_client.start()
    recovery_tasks = {
        asyncio.create_task(
            _resume_recovered_project(project_id),
            name=f"resume-{project_id}",
        )
        for project_id in recovered_project_ids
    }
    try:
        yield
    finally:
        for task in recovery_tasks:
            if not task.done():
                task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        if ws_client is not None:
            ws_client.stop()


app = FastAPI(title="MAC System API", lifespan=lifespan)
app.include_router(feishu_routes.router)
app.include_router(admin_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/projects", response_model=ProjectOut)
async def create_project(
    req: ProjectCreate,
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
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ProjectOut.model_validate(project)


@app.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> ProjectOut:
    project = service.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return ProjectOut.model_validate(project)


@app.get("/projects/{project_id}/status")
def get_status(
    project_id: str,
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


class DecisionResponse(BaseModel):
    project_id: str
    previous_state: str
    current_state: str
    new_project_id: str | None = None


@app.post("/projects/{project_id}/decisions", response_model=DecisionResponse)
async def submit_decision(
    project_id: str,
    req: DecisionRequest,
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

    if req.action == "reject" and req.message:
        source_stage = _confirmation_source_stage(approval.state)
        await service.create_feedback_packet(
            project_id=project_id,
            source_stage=source_stage,
            source_task_id=approval.task_id,
            source_round=approval.payload.get("round_number", 1),
            kind="human_rejection",
            summary=req.message,
            blocking=True,
            failure_class="human_rejection",
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


async def _process_user_message(project_id: str, content: str) -> None:
    """Background task that runs the project_manager agent and applies its decision."""
    with session_scope() as session:
        orchestrator = _build_orchestrator(session)
        try:
            service = orchestrator.project_service
            project = service.require_project(project_id)
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

            suggested_action = output.get("suggested_action")
            if pending_approval and suggested_action:
                valid_actions = {opt["action"] for opt in pending_approval.get("options", [])}
                if suggested_action == "answer" and pending_approval["type"] == "input":
                    answer = output.get("clarification_answer") or content
                    approval = service.get_approval(pending_approval["approval_id"])
                    await service.resolve_approval(
                        approval.id,
                        "answer",
                        responded_by="api_user",
                        payload={"clarification_answer": answer},
                    )
                    await orchestrator.tick(project_id)
                elif suggested_action in valid_actions:
                    approval = service.get_approval(pending_approval["approval_id"])
                    await service.resolve_approval(
                        approval.id, suggested_action, responded_by="api_user"
                    )
                    await orchestrator.tick(project_id)
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
    service: ProjectService = Depends(get_project_service),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> UserMessageResponse:
    try:
        project = service.require_project(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")

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
        suggested_action = output.get("suggested_action")
        if pending_approval and suggested_action:
            valid_actions = {opt["action"] for opt in pending_approval.get("options", [])}
            if suggested_action == "answer" and pending_approval["type"] == "input":
                answer = output.get("clarification_answer") or req.content
                approval = service.get_approval(pending_approval["approval_id"])
                await service.resolve_approval(
                    approval.id,
                    "answer",
                    responded_by="api_user",
                    payload={"clarification_answer": answer},
                )
                await orchestrator.tick(project_id)
            elif suggested_action in valid_actions:
                approval = service.get_approval(pending_approval["approval_id"])
                await service.resolve_approval(
                    approval.id, suggested_action, responded_by="api_user"
                )
                await orchestrator.tick(project_id)
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


async def _run_inject_card_action(action: ButtonAction) -> None:
    """Run an injected card action in a fresh DB session/orchestrator."""
    with session_scope() as session:
        orchestrator = _build_orchestrator(session)
        ctx = FeishuHandlerContext(
            bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
            service=orchestrator.project_service,
            orchestrator=orchestrator,
        )
        await handle_card_action(ctx, action)


@app.post("/test/inject_card_action")
async def inject_card_action(
    req: InjectCardActionRequest,
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

    # Real runtimes: kick off the action in the background and return immediately.
    asyncio.create_task(_run_inject_card_action(action))
    return {"status": "ok"}


class InjectTextMessageRequest(BaseModel):
    open_id: str
    text: str
    chat_id: str | None = None
    chat_type: str = "p2p"


@app.post("/test/inject_text_message")
async def inject_text_message(
    req: InjectTextMessageRequest,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to simulate a Feishu text message."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    ctx = FeishuHandlerContext(
        bridge=orchestrator.feishu_bridge or NullFeishuBridge(),
        service=orchestrator.project_service,
        orchestrator=orchestrator,
        chat_id=req.chat_id,
        chat_type=req.chat_type,
    )
    msg = TextMessage(
        open_id=req.open_id,
        message_id="msg_test",
        text=req.text,
        is_command=req.text.startswith("/"),
        chat_id=req.chat_id,
        chat_type=req.chat_type or "p2p",
    )
    try:
        await handle_text_message(ctx, msg)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("inject_text_message failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"inject_text_message failed: {exc}")
    return {"status": "ok"}


@app.post("/test/tick/{project_id}")
async def test_tick(
    project_id: str,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to manually trigger an orchestrator tick."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    await orchestrator.tick(project_id)
    return {"status": "ok"}


@app.post("/test/clear_pending_intent/{open_id}")
async def clear_pending_intent(
    open_id: str,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Test-only endpoint to clear a user's pending Feishu intent."""
    settings = get_settings()
    if not settings.feishu_test_mode:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    orchestrator.project_service.delete_pending_intent(open_id)
    return {"status": "ok"}
