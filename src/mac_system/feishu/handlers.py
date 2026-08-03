"""Shared Feishu event handlers used by both Webhook and WebSocket transports."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from mac_system.config import get_allowed_bind_roots
from mac_system.core.intent import route_demand, suggest_name
from mac_system.core.project_service import (
    ApprovalNotFoundError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.core.notification_service import deliver_card, is_retryable_delivery_error
from mac_system.core.project_paths import project_mac_dir
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.card import (
    build_content_card,
    build_conversation_clarification_card,
    build_conversation_progress_card,
    build_confirmation_card,
    build_feedback_card,
    build_input_card,
    build_question_batch_card,
    build_rejection_input_card,
    build_rejection_summary_card,
    build_status_card,
    build_transition_card,
)
from mac_system.utils.redaction import redact_secrets, redact_text
from mac_system.utils.formatting import format_tool_preview, split_markdown_sections
from mac_system.feishu.menu import (
    build_bind_dir_for_create_card,
    build_create_prompt_card,
    build_directory_candidate_card,
    build_intent_confirm_card,
    build_main_menu_card,
    build_pending_action_card,
    build_project_control_card,
    build_project_list_card,
    build_restart_type_card,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import (
    Approval,
    ConversationSession,
    FeedbackPacket,
    PendingConversationAction,
    Project,
    ProjectUser,
    RouteDecision,
    Task,
)
from mac_system.state_machine import StateMachine

logger = logging.getLogger(__name__)

_COORDINATOR_MAX_TOOL_CALLS = 8
_COORDINATOR_MAX_DISTINCT_READ_FILES = 8

_CUSTOM_ANSWER_OPTION_KEY = "provide_custom"
_GRANTABLE_RUNTIME_CAPABILITIES = frozenset({"HostAutomation"})


def _acceptance_retry_needs_executor_rerun(
    executor_output: dict[str, Any],
    evaluator_output: dict[str, Any],
    *,
    evaluator_ran_after_executor: bool,
) -> bool:
    """Decide which acceptance stage owns a retry from structured evidence.

    A fully passing executor result is expensive, fresh black-box evidence. It
    remains valid when the later zero-tool evaluator alone fails; all other
    combinations conservatively rerun the executor.
    """
    criteria = executor_output.get("criteria") or []
    executor_fully_passed = (
        executor_output.get("result") == "pass"
        and bool(criteria)
        and all(
            isinstance(item, dict) and item.get("result") == "pass"
            for item in criteria
        )
    )
    evaluator_only_failure = (
        evaluator_ran_after_executor
        and bool(evaluator_output)
        and evaluator_output.get("result") != "pass"
    )
    return not (executor_fully_passed and evaluator_only_failure)


def _option_triggers_free_text(question: Any, option_key: str | None) -> bool:
    """Return True if selecting ``option_key`` on ``question`` should collect
    free-text input instead of submitting the option as the final answer.

    This is a protocol decision, not a natural-language inference. The option
    must carry the explicit structural flag defined by the question schema.
    Guessing from option-key synonyms or labels overfits individual model
    outputs and makes semantically equivalent questions behave differently.
    """
    if not option_key:
        return False
    if isinstance(question, dict):
        allow_free_text = bool(question.get("allow_free_text"))
        options = question.get("options") or []
    else:
        allow_free_text = bool(getattr(question, "allow_free_text", False))
        options = getattr(question, "options", None) or []
    if not allow_free_text:
        return False
    selected: dict[str, Any] | None = None
    for opt in options:
        if isinstance(opt, dict) and opt.get("key") == option_key:
            selected = opt
            break
    return bool(selected and selected.get("triggers_free_text") is True)


def _apply_option_runtime_grants(
    project: Project,
    interaction: Any,
    question: Any,
    option_key: str | None,
) -> bool:
    """Persist an explicit runtime grant carried by a submitted option.

    Permission is protocol data.  Never infer it from an option key, label,
    question wording, or a model's recommendation.  Grants are scoped to the
    originating Agent and resume state, and only capabilities in the fixed
    system allowlist are accepted.
    """
    if not option_key:
        return False
    options = (
        question.get("options", [])
        if isinstance(question, dict)
        else (getattr(question, "options", None) or [])
    )
    selected = next(
        (
            option
            for option in options
            if isinstance(option, dict) and option.get("key") == option_key
        ),
        None,
    )
    requested = selected.get("grants_runtime_capabilities", []) if selected else []
    capabilities = sorted(
        {
            str(capability)
            for capability in requested
            if str(capability) in _GRANTABLE_RUNTIME_CAPABILITIES
        }
    )
    if not capabilities:
        return False

    context = dict(project.context or {})
    grants = list(context.get("runtime_capability_grants") or [])
    grant = {
        "agent": str(getattr(interaction, "agent", "") or ""),
        "state": str(getattr(interaction, "resume_state", "") or ""),
        "question_id": str(
            question.get("question_id", "")
            if isinstance(question, dict)
            else (getattr(question, "question_id", "") or "")
        ),
        "capabilities": capabilities,
    }
    grants = [
        existing
        for existing in grants
        if not (
            isinstance(existing, dict)
            and existing.get("agent") == grant["agent"]
            and existing.get("state") == grant["state"]
            and existing.get("question_id") == grant["question_id"]
        )
    ]
    grants.append(grant)
    context["runtime_capability_grants"] = grants[-20:]
    project.context = context
    return True


def _keeps_external_verification_pending(
    interaction: Any, question: Any, option_key: str | None
) -> bool:
    """A user's ``not_verified`` is evidence, not a completed decision.

    Keep the same durable question/card open so a later real-world observation
    can replace the answer without rerunning the Acceptance Evaluator or
    creating another card.
    """
    return bool(
        option_key == "not_verified"
        and getattr(interaction, "stage", None) == "acceptance"
        and list(getattr(question, "affected_trace_ids", None) or [])
    )


# Intent values that mean "the user is correcting the agent's direction" and
# should be recorded as a blocking correction. ``provide_feedback`` is a
# synonym: when the coordinator labels user input as feedback mid-execution,
# the agent must treat it as corrective, not as a non-blocking question.
_CORRECTION_INTENTS = frozenset({"correct", "provide_feedback"})
# Intent values that mean "the user wants to change the scope of the work".
_SCOPE_CHANGE_INTENTS = frozenset({"change_scope"})


def _intent_to_pending_input_kind(intent: str | None) -> str:
    """Map a coordinator ``intent`` enum value to a ``PendingUserInput.kind``.

    Only ``correction`` and ``scope_change`` change downstream agent behavior
    (they mark the input as blocking and persist a requirement record). All
    other intents fall through to ``question``, which is non-blocking and is
    the correct default for conversational inputs that don't redirect the
    agent.
    """
    if intent in _CORRECTION_INTENTS:
        return "correction"
    if intent in _SCOPE_CHANGE_INTENTS:
        return "scope_change"
    return "question"


@dataclass
class FeishuHandlerContext:
    """Runtime context for processing a single Feishu event."""

    bridge: FeishuBridge
    service: ProjectService
    orchestrator: Any  # AgentOrchestrator, deliberately loosely typed to avoid cycles
    chat_id: str | None = None
    chat_type: str = "p2p"


def _reply_target(ctx: FeishuHandlerContext, open_id: str) -> tuple[str, str]:
    """Return (receive_id, receive_id_type). In groups reply to the group; in p2p reply to the user."""
    if ctx.chat_type == "group" and ctx.chat_id:
        return ctx.chat_id, "chat_id"
    return open_id, "open_id"


async def _send_card_message(
    ctx: FeishuHandlerContext,
    receive_id: str,
    text: str,
    receive_id_type: str = "open_id",
    *,
    title: str = "MAC 消息",
) -> str:
    """Compatibility boundary that renders every legacy text output as cards.

    Call sites can migrate to richer purpose-built cards incrementally without
    leaving plain-text holes in the product. Long content follows the same
    ordered pagination contract as Agent reports and every page is persisted
    through NotificationOutbox.
    """
    chunks = split_markdown_sections(str(text), max_chars=1600) or [str(text)]
    delivery_id = hashlib.sha256(
        f"{ctx.chat_id or receive_id}:{time.time_ns()}:{text}".encode("utf-8")
    ).hexdigest()[:24]
    conversation = (
        ctx.service.get_or_create_conversation_session(ctx.chat_id)
        if ctx.chat_id
        else None
    )
    last_message_id = ""
    for page, chunk in enumerate(chunks, start=1):
        last_message_id = (
            await deliver_card(
                ctx.service,
                ctx.bridge,
                dedupe_key=f"notice:{delivery_id}:{page}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="notice",
                card=build_content_card(
                    title,
                    chunk,
                    page=page,
                    total=len(chunks),
                ),
                conversation_id=conversation.id if conversation else None,
            )
            or last_message_id
        )
    return last_message_id


async def _send_interactive_card(
    ctx: FeishuHandlerContext,
    receive_id: str,
    card: dict[str, Any],
    receive_id_type: str = "open_id",
) -> str:
    """Persist and send a legacy interactive card through the reliable outbox."""
    serialized = json.dumps(card, ensure_ascii=False, sort_keys=True)
    delivery_id = hashlib.sha256(
        f"{ctx.chat_id or receive_id}:{time.time_ns()}:{serialized}".encode("utf-8")
    ).hexdigest()[:24]
    conversation = (
        ctx.service.get_or_create_conversation_session(ctx.chat_id)
        if ctx.chat_id
        else None
    )
    return (
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"interactive:{delivery_id}",
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="decision",
            card=card,
            conversation_id=conversation.id if conversation else None,
        )
        or ""
    )


async def _update_interactive_card(
    ctx: FeishuHandlerContext,
    message_id: str,
    card: dict[str, Any],
) -> str:
    """Reliably update a card and advance its durable provider projection."""
    serialized = json.dumps(card, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    conversation = (
        ctx.service.get_or_create_conversation_session(ctx.chat_id)
        if ctx.chat_id
        else None
    )
    return (
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"card-update:{message_id}:{digest}",
            recipient_id=ctx.chat_id or "provider-message",
            recipient_type="chat_id" if ctx.chat_id else "open_id",
            intent_type="progress",
            card=card,
            conversation_id=conversation.id if conversation else None,
            update_message_id=message_id,
            projection_key=f"provider:{message_id}",
            subject_type="provider_message",
            subject_id=message_id,
            lifecycle_status="published",
        )
        or message_id
    )


def _encode_create_state(task_type: str, description: str) -> str:
    """Encode (task_type, description) into a JSON string for PendingIntent.

    Used by the `/create` flow when we pause to ask the user to pick a bind
    directory. We can't add a `task_type` column without a schema migration,
    so we stash it in the existing `description` field as JSON.
    """
    return json.dumps(
        {"task_type": task_type, "description": description},
        ensure_ascii=False,
    )


def _decode_create_state(raw: str | None) -> tuple[str | None, str | None]:
    """Decode a JSON-encoded create state from PendingIntent.description.

    Returns (task_type, description). If `raw` is not JSON, returns (None, raw)
    so callers can fall back to treating it as a plain description.
    """
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "task_type" in data and "description" in data:
            return data["task_type"], data["description"]
    except (json.JSONDecodeError, ValueError):
        pass
    return None, raw


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


_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _is_protocol_blob(blob: str) -> bool:
    """A JSON blob that parses as coordinator output is protocol, not prose."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "reply" in data and (
        "success" in data or "proposed_action" in data or "needs_clarification" in data
    )


def _strip_protocol_echo(text: str) -> str:
    """Remove self-echoes of the coordinator's output JSON from user-facing text.

    The model sometimes appends a copy of its own machine output (the whole
    {"success": ..., "reply": ...} object) as a fenced block inside its reply.
    That is protocol data — showing it to users is always a bug. Legitimate
    JSON snippets (artifacts, examples) do not match _is_protocol_blob and are
    preserved.
    """
    if text and _is_protocol_blob(text):
        return ""
    cleaned = _FENCED_BLOCK_RE.sub(
        lambda m: "" if _is_protocol_blob(m.group(1).strip()) else m.group(0),
        text,
    )
    return cleaned.strip()


def _workflow_stage_index(state_machine: StateMachine, state: str) -> int | None:
    """Return the ordered delivery-stage index represented by a concrete state."""
    stages = state_machine.profile["stages"]
    for index, stage in enumerate(stages):
        stage_states = {
            stage["execution_state"],
            stage["confirmation"]["state"],
            f"{stage['execution_state']}_RECOVERY_PENDING",
        }
        stage_states.update(item["state"] for item in stage.get("inputs", []))
        if state in stage_states:
            return index
    if state in {"REWORK", "ACCEPTANCE_PENDING", "ACCEPTANCE_INPUT_PENDING", "MERGING"}:
        return len(stages)
    return None


def _is_valid_rollback_target(
    state_machine: StateMachine, current_state: str, target_state: str
) -> bool:
    """Allow rollback only to a strictly earlier delivery stage."""
    current_index = _workflow_stage_index(state_machine, current_state)
    target_index = _workflow_stage_index(state_machine, target_state)
    return (
        current_index is not None
        and target_index is not None
        and target_index < current_index
    )


def _is_authorized_for_project(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> bool:
    """Allow direct project members or members of the bound group chat."""
    from mac_system.models import ProjectChat

    direct_binding = ctx.service.session.exec(
        select(ProjectUser).where(
            ProjectUser.project_id == project_id,
            ProjectUser.open_id == open_id,
        )
    ).first()
    if direct_binding is not None:
        return True

    if ctx.chat_type == "group" and ctx.chat_id:
        chat_binding = ctx.service.session.exec(
            select(ProjectChat).where(
                ProjectChat.project_id == project_id,
                ProjectChat.chat_id == ctx.chat_id,
            )
        ).first()
        return chat_binding is not None

    return False


async def handle_text_message(ctx: FeishuHandlerContext, msg: TextMessage) -> None:
    """Dispatch a text message from Feishu."""
    text = msg.text
    open_id = msg.open_id

    # A reply to an Agent question card is routed by the card message itself,
    # not by whichever project is currently active in this chat.
    if msg.parent_id and await _handle_question_card_reply(ctx, msg):
        return

    # If the user is in the middle of creating a project, treat the reply as
    # the project description instead of a command.
    pending = ctx.service.get_pending_intent(open_id)
    if pending is not None:
        if pending.stage == "awaiting_bind_dir":
            # User is replying with an absolute path for /bind manual input.
            # Don't require it to not start with "/" - absolute paths do.
            ctx.service.delete_pending_intent(open_id)
            await _bind_directory(ctx, open_id, text.strip())
            return
        # `/create` is already an explicit request to start delivery. Its next
        # free-text turn is the description, even when the general conversation
        # Coordinator is available; routing it through ordinary chat adds a
        # second confirmation/clarification before the Requirements stage.
        if (
            not text.startswith("/")
            and pending.stage in {"awaiting_description", "awaiting_type_confirm"}
        ):
            ctx.service.delete_pending_intent(open_id)
            if pending.stage == "awaiting_type_confirm" and pending.description:
                # User typed again while we were waiting for type confirmation;
                # treat the new text as a fresh description.
                await _route_and_create(
                    ctx, open_id, text,
                    chat_id=pending.chat_id, chat_type=pending.chat_type,
                )
            else:
                await _route_and_create(
                    ctx, open_id, text,
                    chat_id=pending.chat_id, chat_type=pending.chat_type,
                )
            return

    # In group chats, only respond to slash commands or explicit @bot mentions.
    if ctx.chat_type == "group" and not text.startswith("/") and not getattr(msg, "is_at_bot", False):
        return

    if text == "/whoami":
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await _send_card_message(ctx, 
            receive_id, f"你的 open_id 是：{open_id}", receive_id_type=receive_id_type
        )
        return

    if text == "/leave-project":
        await _leave_project(ctx, open_id)
        return

    if text.startswith("/bind "):
        directory = text[len("/bind "):].strip()
        await _bind_directory(ctx, open_id, directory)
        return

    if text == "/bind":
        await _show_bind_candidate_card(ctx, open_id)
        return

    if text == "/unbind":
        await _unbind_directory(ctx, open_id)
        return

    if text.startswith("/status "):
        project_id = text[len("/status "):].strip()
        await _show_status(ctx, open_id, project_id)
        return

    if text == "/create" or text.startswith("/create "):
        rest = text[len("/create "):].strip() if text.startswith("/create ") else ""
        task_type, description = _parse_create_command(rest)
        if description:
            if task_type:
                # Explicit type: skip intent routing but still try bind_dir
                # recommendation so the user can pick a matching repo.
                if await _maybe_pause_for_bind_dir_recommendation(
                    ctx,
                    open_id,
                    task_type=task_type,
                    description=description,
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                ):
                    return
                name = suggest_name(description)
                await _create_project(
                    ctx, open_id, task_type, description, name=name,
                )
            else:
                await _route_and_create(ctx, open_id, description)
        else:
            await _prompt_for_create_description(ctx, open_id)
        return

    if text.startswith("/cancel"):
        rest = text[len("/cancel"):].strip()
        await _cancel_project_command(ctx, open_id, rest)
        return

    if text.startswith("/confirm "):
        rest = text[len("/confirm "):].strip()
        parts = rest.split(None, 1)
        if len(parts) >= 1:
            await _confirm_approval_command(
                ctx, open_id, parts[0], option_key=parts[1] if len(parts) > 1 else None
            )
        else:
            await _show_help(ctx, open_id)
        return

    if text.startswith("/reject "):
        rest = text[len("/reject "):].strip()
        parts = rest.split(None, 1)
        if len(parts) >= 1:
            await _reject_approval_command(
                ctx, open_id, parts[0], feedback=parts[1] if len(parts) > 1 else None
            )
        else:
            await _show_help(ctx, open_id)
        return

    if text == "/list":
        await _list_my_projects(ctx, open_id)
        return

    if text.startswith("/switch "):
        project_id = text[len("/switch "):].strip()
        await _switch_project(ctx, open_id, project_id)
        return

    if text == "/switch":
        await _show_project_switch_card(ctx, open_id)
        return

    if text == "/current":
        await _show_current_project(ctx, open_id)
        return

    if text == "/projects":
        await _list_chat_projects(ctx, open_id)
        return

    if text == "/help":
        await _show_help(ctx, open_id)
        return

    if text.startswith("/input "):
        rest = text[len("/input "):].strip()
        await _submit_input(ctx, open_id, rest)
        return

    if text == "/menu":
        await _show_menu(ctx, open_id)
        return

    active_agent_loop = _find_active_project_for_user(ctx, open_id)
    if (
        active_agent_loop is not None
        and active_agent_loop.context.get("orchestration_mode") == "agent_loop"
    ):
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await _send_card_message(
            ctx,
            receive_id,
            "已收到，Coordinator 会结合当前项目上下文处理。",
            receive_id_type=receive_id_type,
        )
        steered = False
        steer = getattr(ctx.orchestrator, "steer_coordinator", None)
        if steer is not None:
            steered = await steer(active_agent_loop.id, text)
        if not steered:
            runner = getattr(ctx.orchestrator, "run_coordinator", None)
            if runner is not None:
                await runner(
                    active_agent_loop.id,
                    "New user message in this project conversation:\n" + text,
                )
            else:
                await ctx.orchestrator.tick(active_agent_loop.id)
        return

    # Natural-language conversation is handled before legacy project-input
    # fallbacks.  If the coordinator is unavailable, return False and retain
    # the existing deterministic behavior as a safe compatibility path.
    if await _handle_coordinated_free_text(ctx, msg):
        return

    # Before treating free-form text as a new project, check whether the user
    # has an active project that was recently rejected and needs feedback.
    active_project = _find_rejected_project_for_feedback(ctx, open_id)
    if active_project is not None:
        await _handle_rejection_feedback(ctx, open_id, active_project, text)
        return

    # Otherwise, if the user already has an active project, record the message
    # as a pending user input so it can be consumed when the running agent
    # returns.  This prevents free-text from being lost or mis-routed.
    # Skip for projects in terminal/failed/cancelled states — recording
    # input to a dead project is confusing and never gets consumed.
    active_project = _find_active_project_for_user(ctx, open_id)
    if active_project is not None:
        dead_states = {"FAILED", "COMPLETED", "CANCELLED", "CANCELED", "MERGING"}
        if active_project.status == "cancelled" or active_project.current_state in dead_states:
            await _show_help(ctx, open_id)
            return
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        state_machine = StateMachine(active_project.task_type)
        running_state_tasks = [
            task
            for task in ctx.service._running_tasks(active_project.id)
            if task.state == active_project.current_state
        ]
        if state_machine.is_agent_execution(active_project.current_state) and running_state_tasks:
            # Free text during execution is always recorded as a non-blocking
            # question. Keyword-based kind inference both over-interrupted on
            # casual remarks and missed real corrections; explicit corrections
            # go through the coordinator paths (rollback / route_project_input).
            kind = "question"
            await ctx.service.create_pending_user_input(
                project_id=active_project.id,
                sender_id=open_id,
                observed_state=active_project.current_state,
                content=text,
                kind=kind,
                source_chat_id=ctx.chat_id,
            )
            await _send_card_message(ctx, 
                receive_id,
                "已记录你的输入，当前 Agent 处理完毕后会自动消费。",
                receive_id_type=receive_id_type,
            )
            return

        pending_approval = ctx.service.session.exec(
            select(Approval)
            .where(
                Approval.project_id == active_project.id,
                Approval.status == "pending",
            )
            .order_by(Approval.created_at.desc())
        ).first()
        if pending_approval and pending_approval.approval_type == "input":
            await _submit_input(ctx, open_id, text)
        elif pending_approval:
            await _send_card_message(ctx, 
                receive_id,
                "项目正在等待确认。请在最新确认卡中选择；也可以直接用自然语言告诉我"
                "确认、驳回或修改意见。命令仅作为兼容快捷方式。",
                receive_id_type=receive_id_type,
            )
        else:
            await _send_card_message(ctx, 
                receive_id,
                "当前项目没有正在运行的 Agent，也没有待处理输入；请使用 `/status "
                f"{active_project.id}` 查看状态。",
                receive_id_type=receive_id_type,
            )
        return

    # No active project: check whether the user has multiple candidate
    # projects.  If so, create a ConversationInteraction and show a selection
    # card (deterministic, no LLM guessing).
    from mac_system.feishu.menu import build_project_list_card
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if project_ids:
        candidates = ctx.service.session.exec(
            select(Project)
            .where(Project.id.in_(project_ids))
            .where(Project.status == "active")
            .order_by(Project.updated_at.desc())
        ).all()
        if len(candidates) > 1:
            await ctx.service.create_conversation_interaction(
                chat_id=ctx.chat_id or open_id,
                open_id=open_id,
                source_message_id=None,
                source_text=text,
                candidates=[
                    {"id": p.id, "name": p.name, "current_state": p.current_state}
                    for p in candidates
                ],
            )
            receive_id, receive_id_type = _reply_target(ctx, open_id)
            await _send_interactive_card(ctx, 
                receive_id,
                build_project_list_card(
                    [
                        {"id": p.id, "name": p.name, "current_state": p.current_state}
                        for p in candidates
                    ],
                    mode="select",
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                ),
                receive_id_type=receive_id_type,
            )
            return

    # Default: do not auto-create projects from free-form text. Show help instead.
    await _show_help(ctx, open_id)


async def _handle_coordinated_free_text(
    ctx: FeishuHandlerContext,
    msg: TextMessage,
    *,
    progress_message_id: str | None = None,
) -> bool:
    """Run the bounded conversation coordinator for one free-text turn."""
    runner = getattr(ctx.orchestrator, "run_chat_scoped_agent", None)
    if runner is None:
        return False

    open_id = msg.open_id
    chat_key = ctx.chat_id or msg.chat_id or f"p2p:{open_id}"
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    reaction_id: str | None = None
    if msg.message_id and not msg.message_id.startswith("card:"):
        try:
            reaction_id = await ctx.bridge.add_reaction(msg.message_id, "OnIt")
        except Exception:
            logger.warning("Failed to add processing reaction", exc_info=True)

    progress_card = build_conversation_progress_card(
        stage="working", detail="正在结合上下文调研与分析"
    )
    try:
        if progress_message_id:
            await _update_interactive_card(ctx, progress_message_id, progress_card)
        else:
            progress_message_id = await _send_interactive_card(ctx, 
                receive_id, progress_card, receive_id_type=receive_id_type
            )
    except Exception:
        logger.warning("Failed to send conversation progress card", exc_info=True)
        progress_message_id = None
    progress_refresh_task = (
        asyncio.create_task(
            _refresh_conversation_progress(ctx, progress_message_id)
        )
        if progress_message_id
        else None
    )
    conversation = ctx.service.append_conversation_turn(
        chat_key,
        role="user",
        content=msg.text,
        open_id=open_id,
        message_id=msg.message_id,
    )
    active_project = _find_active_project_for_user(ctx, open_id)
    recent_project: Project | None = None
    if ctx.chat_id:
        selected_project = ctx.service.get_chat_user_selected_project(
            ctx.chat_id, open_id
        )
        if (
            selected_project is not None
            and selected_project.status not in {"active", "paused"}
        ):
            recent_project = selected_project
    if (
        active_project is None
        and conversation.active_project_id
        and ctx.chat_type != "group"
    ):
        candidate = ctx.service.get_project(conversation.active_project_id)
        if candidate is not None and candidate.status in {"active", "paused"}:
            active_project = candidate
        elif candidate is not None:
            # A terminal project is not controllable, but it remains the
            # conversation's follow-up target.  Keeping this separate from the
            # active project lets a later bug fix/iteration create a fresh run
            # against the same workspace instead of misusing rollback or
            # silently starting in an unrelated directory.
            recent_project = candidate

    def _action_project_is_active(action: PendingConversationAction) -> bool:
        if not action.target_project_id:
            return True
        proj = ctx.service.get_project(action.target_project_id)
        return proj is not None and proj.status in {"active", "paused"}

    approvals: list[dict[str, Any]] = []
    if active_project is not None:
        pending_approval_rows = list(
            ctx.service.session.exec(
                select(Approval)
                .where(
                    Approval.project_id == active_project.id,
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at.desc())
            ).all()
        )
        approvals = [
            _coordinator_approval_snapshot(ctx, approval)
            for approval in pending_approval_rows
        ]
    pending_actions = [
        {
            "id": action.id,
            "type": action.action_type,
            "payload": action.payload,
        }
        for action in ctx.service.get_session_pending_actions(conversation.id)
        if _action_project_is_active(action)
    ]
    pending_intent = ctx.service.get_pending_intent(open_id)

    binding = ctx.service.get_chat_directory_binding(ctx.chat_id) if ctx.chat_id else None
    workspace = Path(binding.directory) if binding else None
    if workspace is None and active_project is not None:
        workspace = Path(active_project.workspace_path)
    if workspace is None and recent_project is not None:
        workspace = Path(recent_project.workspace_path)

    available_repositories: list[dict[str, str]] = _scan_available_repositories(ctx)
    # Project association decides where delivery writes and where follow-up
    # state belongs.  It is not the Coordinator's read boundary.  A
    # Coordinator may inspect any relevant directory under the operator's
    # configured host roots, while project Agents continue to receive only
    # task-scoped context.  Keeping this capability root-based also avoids
    # repository-name/keyword routing and works for ordinary non-git folders.
    from mac_system.config import get_allowed_bind_roots as _configured_read_roots

    authorized_read_roots: list[Path] = [
        root.resolve()
        for root in _configured_read_roots()
        if root.is_absolute() and root.is_dir()
    ]
    if workspace is not None and workspace.is_absolute() and workspace.is_dir():
        resolved_workspace = workspace.resolve()
        if not any(
            resolved_workspace == root or root in resolved_workspace.parents
            for root in authorized_read_roots
        ):
            authorized_read_roots.append(resolved_workspace)

    active_project_data = (
        _coordinator_project_snapshot(ctx, active_project)
        if active_project is not None
        else None
    )
    extra_variables = {
        "user_message": msg.text,
        "open_id": open_id,
        "chat_id": chat_key,
        "conversation_summary": conversation.summary or "无",
        # The rolling summary carries older context. Keeping only the latest
        # eight turns bounds prompt size and reduces chat latency.
        "recent_turns": json.dumps(conversation.recent_turns[-8:], ensure_ascii=False),
        "active_project": json.dumps(active_project_data, ensure_ascii=False),
        "recent_project": json.dumps(
            _coordinator_project_snapshot(ctx, recent_project)
            if recent_project is not None else None,
            ensure_ascii=False,
        ),
        "pending_approvals": json.dumps(approvals, ensure_ascii=False),
        "pending_actions": json.dumps(pending_actions, ensure_ascii=False),
        "pending_intent": json.dumps(
            {
                "stage": pending_intent.stage,
                "description": pending_intent.description,
            }
            if pending_intent is not None
            else None,
            ensure_ascii=False,
        ),
        "latest_artifact": json.dumps(conversation.latest_artifact, ensure_ascii=False),
        "workspace_path": str(workspace) if workspace else "未绑定工作区",
        "available_repositories": json.dumps(available_repositories, ensure_ascii=False),
        "context_access": json.dumps({"status": "unavailable"}, ensure_ascii=False),
        "repository_capability_metadata": "{}",
        "repository_file_manifest": "[]",
        "repository_file_metadata": "{}",
        "repository_context_bundle": "[]",
    }
    coordinator_started = time.monotonic()

    last_progress_update = 0.0

    async def _coordinator_progress(event_type: str, content: str) -> None:
        """Coalesce Coordinator activity into the existing progress card."""
        nonlocal last_progress_update
        if not progress_message_id:
            return
        now = time.monotonic()
        if now - last_progress_update < 2.0:
            return
        try:
            if event_type == "thinking":
                # Chain-of-thought may contain unverified intermediate claims
                # or values seen while inspecting files. Never stream it to
                # the user; expose only a stable phase label.
                detail = "正在结合当前项目、历史决策和可用事实进行分析"
            elif event_type == "tool":
                data = json.loads(content)
                tool_name = data.get("name") or "工具"
                detail = f"正在进行只读检查：{tool_name}"
            else:
                return
            await _update_interactive_card(ctx, 
                progress_message_id,
                build_conversation_progress_card(stage="working", detail=detail),
            )
            last_progress_update = now
        except Exception:
            logger.warning("Failed to send Coordinator progress event", exc_info=True)

    # The chat router starts without a mounted workspace. Authorized roots are
    # advertised as metadata and one root is mounted only after a semantic
    # request_context action. No filename or keyword branch grants access.
    output: dict[str, Any] | None = None
    failure_reason = "未返回有效结果"
    # Start every chat turn with the smallest useful context. Repository access
    # is negotiated only after the first semantic decision asks for it. This
    # mirrors Codex's dynamic tool/context loading and prevents an executable
    # request from becoming a multi-minute pre-delivery repository audit.
    max_attempts = 4
    verified_read_paths: set[str] = set()
    last_candidate: dict[str, Any] | None = None
    raw_prose_repair_attempted = False
    read_budget_failures = 0
    # While an Agent is waiting for user input, the authoritative question,
    # options, and project snapshot already contain everything needed to route
    # the answer.  Start that Coordinator turn without repository tools.  This
    # is especially important for capability references: the Coordinator must
    # copy the user-authorized path/bindings into a structured action, not
    # inspect neighbouring .env templates.  If the message is actually an
    # unrelated repository question, it can still request one read root via
    # the normal context protocol on the next attempt.
    answering_agent_input = any(
        item.get("type") == "input" for item in approvals
    )
    selected_context_root: Path | None = None
    context_bundle: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        semantic_failed = False
        try:
            attempt_worktree = selected_context_root
            attempt_variables = {
                **extra_variables,
                "context_access": json.dumps(
                    {
                        "status": "supplied" if context_bundle else "direct_read",
                        "working_directory": (
                            str(selected_context_root)
                            if selected_context_root is not None
                            else None
                        ),
                        "read_roots": [str(root) for root in authorized_read_roots],
                    }
                    if authorized_read_roots
                    else {"status": "unavailable", "read_roots": []},
                    ensure_ascii=False,
                ),
                "repository_capability_metadata": json.dumps(
                    {
                        str(selected_context_root): _repository_env_key_names(
                            selected_context_root
                        )
                    }
                    if selected_context_root is not None
                    else {},
                    ensure_ascii=False,
                ),
                "repository_file_manifest": json.dumps(
                    _repository_file_manifest(selected_context_root)
                    if selected_context_root is not None
                    else [],
                    ensure_ascii=False,
                ),
                "repository_file_metadata": json.dumps(
                    _repository_file_metadata(selected_context_root)
                    if selected_context_root is not None
                    else {},
                    ensure_ascii=False,
                ),
                "repository_context_bundle": json.dumps(
                    context_bundle, ensure_ascii=False
                ),
                "retry_feedback": (
                    _conversation_retry_feedback(
                        failure_reason,
                        verified_read_paths=verified_read_paths,
                        previous_candidate=last_candidate,
                    )
                    if attempt > 1
                    else ""
                ),
            }
            output = await runner(
                agent_type="conversation_coordinator",
                stage=None,
                input_data={"message": msg.text},
                extra_variables=attempt_variables,
                worktree=attempt_worktree,
                readonly_dirs=authorized_read_roots,
                max_tool_calls=(
                    0
                    if attempt > 1 and verified_read_paths
                    else (
                        _COORDINATOR_MAX_TOOL_CALLS
                        if selected_context_root is not None
                        else 0
                    )
                ),
                progress_callback=_coordinator_progress,
            )
            runtime_evidence = output.get("_runtime") if output else None
            if isinstance(runtime_evidence, dict):
                read_paths = {
                    str((call.get("input") or {}).get("file_path") or "")
                    for call in runtime_evidence.get("tool_calls") or []
                    if isinstance(call, dict) and call.get("name") == "Read"
                }
                read_paths.discard("")
                in_scope_reads = {
                    path
                    for path in read_paths
                    if _read_path_within_roots(
                        path,
                        working_directory=selected_context_root,
                        roots=authorized_read_roots,
                    )
                }
                out_of_scope_reads = read_paths - in_scope_reads
                verified_read_paths.update(in_scope_reads)
                if len(in_scope_reads) > _COORDINATOR_MAX_DISTINCT_READ_FILES:
                    failure_reason = (
                        "Read evidence used more than "
                        f"{_COORDINATOR_MAX_DISTINCT_READ_FILES} distinct files."
                    )
                    read_budget_failures += 1
                    logger.warning(
                        "Conversation coordinator exceeded distinct-file budget "
                        "chat=%s message=%s attempt=%s count=%s",
                        chat_key,
                        msg.message_id,
                        attempt,
                        len(in_scope_reads),
                    )
                    last_candidate = None
                    output = None
                    semantic_failed = True
                    if read_budget_failures >= 2:
                        break
                elif out_of_scope_reads:
                    failure_reason = (
                        "Read evidence is outside every authorized repository root."
                    )
                    logger.warning(
                        "Conversation coordinator used out-of-scope Read evidence "
                        "chat=%s message=%s attempt=%s paths=%s",
                        chat_key,
                        msg.message_id,
                        attempt,
                        sorted(out_of_scope_reads),
                    )
                    last_candidate = None
                    output = None
                    semantic_failed = True
            if output and not output.get("success"):
                raw_answer = str(output.get("result_text") or "").strip()
                if raw_answer and not raw_answer.startswith(("```", "{")):
                    # The Coordinator is a read-only boundary. Preserve useful
                    # prose when only its JSON envelope failed, without ever
                    # turning that prose into an executable action.
                    output = {"success": True, "result_text": raw_answer}
                elif raw_answer.startswith("{"):
                    # Keep a schema-invalid JSON object as repair evidence. It
                    # must never be executed or relayed directly, but showing
                    # the exact adjacent/wrapper shape to the next bounded
                    # attempt lets the Coordinator repair its envelope instead
                    # of guessing from a validation error alone.
                    try:
                        invalid_candidate = json.loads(raw_answer)
                    except json.JSONDecodeError:
                        invalid_candidate = None
                    if isinstance(invalid_candidate, dict):
                        last_candidate = invalid_candidate
            if output and output.get("success"):
                result_text = str(output.get("result_text") or "").strip()
                if (
                    result_text
                    and not result_text.startswith(("```", "{"))
                    and not {
                        "reply", "intent", "effect", "commitment",
                        "needs_clarification", "proposed_action",
                    }.issubset(output)
                ):
                    # The prose itself is safe and read-only, but first give
                    # the Coordinator one bounded chance to wrap the same
                    # answer in its declared schema. This keeps follow-up
                    # state and machine actions reliable without discarding a
                    # useful answer if the repair also fails.
                    if not raw_prose_repair_attempted and attempt < max_attempts:
                        raw_prose_repair_attempted = True
                        last_candidate = {"result_text": result_text}
                        failure_reason = (
                            "The answer is useful but the required JSON envelope is "
                            "missing. Return the same conclusion as a complete "
                            "Conversation Coordinator schema object; do not add or "
                            "execute an action."
                        )
                        output = None
                        continue
                    # Preserve the safe relay after one failed repair. It can
                    # answer the user but can never fabricate an action.
                    break
                action = output.get("proposed_action")
                action_type = action.get("type") if isinstance(action, dict) else None
                if action_type == "request_context":
                    request_error = _conversation_contract_error(output)
                    requested_path = str(action.get("repository_path") or "")
                    try:
                        requested_candidate = Path(requested_path).expanduser().resolve()
                    except (OSError, RuntimeError):
                        requested_candidate = None
                    requested_root = (
                        requested_candidate
                        if requested_candidate is not None
                        and requested_candidate.is_dir()
                        and any(
                            requested_candidate == root
                            or root in requested_candidate.parents
                            for root in authorized_read_roots
                        )
                        else None
                    )
                    if request_error:
                        failure_reason = request_error
                        last_candidate = {
                            key: value
                            for key, value in output.items()
                            if key != "_runtime"
                        }
                        output = None
                        semantic_failed = True
                    elif requested_root is None:
                        failure_reason = "requested repository is not authorized"
                        last_candidate = {
                            key: value
                            for key, value in output.items()
                            if key != "_runtime"
                        }
                        output = None
                        semantic_failed = True
                    elif (
                        selected_context_root is None
                        or requested_root != selected_context_root
                    ):
                        selected_context_root = requested_root
                        last_candidate = None
                        output = None
                        # Context switching is internal and read-only.  It does
                        # not rebind the chat or change the delivery target.
                        continue
                    elif context_bundle:
                        failure_reason = "context was already supplied for this turn"
                        last_candidate = {
                            key: value
                            for key, value in output.items()
                            if key != "_runtime"
                        }
                        output = None
                        semantic_failed = True
                    else:
                        requested_files = [
                            str(item) for item in action.get("read_targets") or []
                        ]
                        manifest = set(
                            _repository_file_manifest(selected_context_root)
                        )
                        invalid_files = [
                            path for path in requested_files if path not in manifest
                        ]
                        if invalid_files:
                            failure_reason = (
                                "requested files are not present in the repository manifest"
                            )
                            last_candidate = {
                                key: value
                                for key, value in output.items()
                                if key != "_runtime"
                            }
                            output = None
                            semantic_failed = True
                        else:
                            context_bundle = _repository_context_bundle(
                                selected_context_root, requested_files
                            )
                            if not context_bundle:
                                failure_reason = "selected files produced no readable context"
                                output = None
                                semantic_failed = True
                            else:
                                logger.info(
                                    "Conversation coordinator context supplied "
                                    "chat=%s message=%s repository=%s files=%s",
                                    chat_key,
                                    msg.message_id,
                                    selected_context_root,
                                    [item["path"] for item in context_bundle],
                                )
                                verified_read_paths.update(
                                    str(selected_context_root / item["path"])
                                    for item in context_bundle
                                )
                                verified_read_paths.update(
                                    str(item["path"]) for item in context_bundle
                                )
                                last_candidate = None
                                output = None
                                # The next no-tools pass receives redacted file
                                # content and must produce the final action.
                                continue
                if semantic_failed:
                    continue
                semantic_error = _conversation_contract_error(
                    output,
                    verified_read_paths=verified_read_paths,
                )
                if semantic_error is None:
                    semantic_error = _conversation_state_contract_error(
                        output,
                        approvals=approvals,
                        pending_actions=pending_actions,
                    )
                if semantic_error is None:
                    semantic_error = _capability_refs_contract_error(output)
                if semantic_error is None:
                    break
                failure_reason = semantic_error
                logger.warning(
                    "Conversation coordinator semantic validation failed "
                    "chat=%s message=%s attempt=%s reason=%s",
                    chat_key,
                    msg.message_id,
                    attempt,
                    semantic_error,
                )
                last_candidate = {
                    key: value for key, value in output.items() if key != "_runtime"
                }
                output = None
                semantic_failed = True
            if output:
                failure_reason = output.get("error", "协调器返回失败")
                if "Tool-call budget exceeded" in str(failure_reason):
                    read_budget_failures += 1
                logger.warning(
                    "Conversation coordinator returned success=false chat=%s message=%s attempt=%s output=%s",
                    chat_key,
                    msg.message_id,
                    attempt,
                    json.dumps(output, ensure_ascii=False),
                )
                if read_budget_failures >= 2:
                    break
            elif not semantic_failed:
                failure_reason = "调用未返回结果"
            logger.warning(
                "Conversation coordinator failed chat=%s message=%s attempt=%s reason=%s",
                chat_key,
                msg.message_id,
                attempt,
                failure_reason,
            )
        except Exception as exc:
            logger.exception("Conversation coordinator crashed")
            failure_reason = f"协调器异常：{exc}"
            output = None
        # Three model attempts are enough for ordinary no-tools answers.  A
        # fourth slot exists only because a valid context negotiation consumes
        # the first pass before the bounded read-and-answer pass begins.
        if selected_context_root is None and attempt >= 3:
            break
        if attempt < max_attempts and progress_message_id:
            try:
                await _update_interactive_card(ctx, 
                    progress_message_id,
                    build_conversation_progress_card(
                        stage="retrying",
                        detail="本次结果不完整，正在自动重试",
                    ),
                )
            except Exception:
                logger.warning("Failed to update retry progress card", exc_info=True)
    if progress_refresh_task is not None:
        progress_refresh_task.cancel()
        await asyncio.gather(progress_refresh_task, return_exceptions=True)
    coordinator_success = bool(output and output.get("success"))
    coordinator_elapsed = time.monotonic() - coordinator_started
    logger.info(
        "Conversation coordinator finished chat=%s message=%s elapsed=%.2fs success=%s attempts=%s",
        chat_key,
        msg.message_id,
        coordinator_elapsed,
        coordinator_success,
        attempt,
    )
    if active_project is not None:
        await ctx.service.log_event(
            project_id=active_project.id,
            level="INFO",
            message="Conversation coordinator profiled",
            meta={
                "profile_type": "conversation_coordinator",
                "elapsed_seconds": round(coordinator_elapsed, 3),
                "attempts": attempt,
                "success": coordinator_success,
                "message_id": msg.message_id,
            },
        )
    if progress_message_id and not coordinator_success:
        try:
            await _update_interactive_card(ctx, 
                progress_message_id,
                build_conversation_progress_card(
                    stage="failed",
                    detail="本次未能生成可靠答复，未执行任何操作",
                ),
            )
        except Exception:
            logger.warning("Failed to advance conversation progress card", exc_info=True)
    if not coordinator_success:
        # Never fabricate an execution proposal from a failed parse — failure
        # fallbacks must stay in the safe direction for every phrasing.
        output = {
            "success": True,
            "reply": "这次没能整理出可靠答复，且没有执行任何操作。请直接重试一次。",
            "intent": "unknown",
            "effect": "none",
            "commitment": "ambiguous",
            "needs_clarification": False,
            "updated_summary": msg.text[:300],
            "proposed_action": {"type": "answer"},
        }

    output = redact_secrets(output)
    required_fields = {
        "reply", "intent", "effect", "commitment", "needs_clarification",
        "proposed_action",
    }
    contract_error = _conversation_contract_error(
        output,
        verified_read_paths=verified_read_paths,
    )
    if not required_fields.issubset(output) or contract_error:
        result_text = str(output.get("result_text") or "").strip()
        # Raw model output that still looks like a JSON/fenced blob is not
        # user-facing prose — never leak it into the reply.
        if result_text and not result_text.startswith(("```", "{")):
            output = {
                "success": True,
                "reply": result_text,
                "intent": "explain",
                "effect": "read_only",
                "commitment": "exploring",
                "needs_clarification": False,
                "updated_summary": result_text[:300],
                "proposed_action": {"type": "answer"},
            }
        else:
            logger.warning(
                "Conversation coordinator returned an invalid contract: %s",
                contract_error or "missing required fields",
            )
            output = {
                "success": True,
                "reply": "本次结果格式不完整，请重试一次。",
                "intent": "unknown",
                "effect": "none",
                "commitment": "ambiguous",
                "needs_clarification": False,
                "updated_summary": msg.text[:300],
                "proposed_action": {"type": "answer"},
            }

    try:
        await _apply_conversation_result(
            ctx,
            msg,
            conversation,
            output,
            active_project=active_project,
            recent_project=recent_project,
            # Replace the temporary progress card with the actual answer or
            # decision.  A second generic "处理完成" card adds no information
            # and obscures the execution chain in a busy private chat.
            progress_message_id=progress_message_id,
        )
    finally:
        if reaction_id:
            try:
                await ctx.bridge.remove_reaction(msg.message_id, reaction_id)
            except Exception:
                logger.warning("Failed to remove processing reaction", exc_info=True)
    return True


def _read_path_within_workspace(path: str, workspace: Path) -> bool:
    """Whether a Read result is relevant evidence for the selected workspace.

    This is an evidence-provenance rule rather than a permission boundary: an
    authorized agent may observe other files, but those files cannot support
    conclusions about the current project.
    """
    if not path:
        return False
    try:
        root = workspace.expanduser().resolve()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == root or root in candidate.parents


def _read_path_within_roots(
    path: str,
    *,
    working_directory: Path | None,
    roots: list[Path],
) -> bool:
    """Validate read evidence against every configured Coordinator root.

    Relative paths are resolved from the runtime working directory. Absolute
    paths may point anywhere below an allowed host root. This is deliberately
    independent from the chat's associated project directory.
    """
    if not path or not roots:
        return False
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            if working_directory is None:
                return False
            candidate = working_directory.expanduser().resolve() / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    return any(
        candidate == root.resolve() or root.resolve() in candidate.parents
        for root in roots
    )


def _conversation_retry_feedback(
    failure_reason: str,
    *,
    verified_read_paths: set[str],
    previous_candidate: dict[str, Any] | None = None,
) -> str:
    """Build retry guidance while preserving the authorized capabilities."""
    parts = [failure_reason]
    if verified_read_paths:
        paths = "\n".join(
            f"- {path}" for path in sorted(verified_read_paths)[:20]
        )
        parts.append(
            "上一尝试已经通过真实 Read 验证了以下文件，本次技术重试会继承这些"
            "证据，并继续提供同一组只读工具：\n"
            f"{paths}\n"
            "可以复用这些证据；只有确需补证时才再次调用 Read。不要把工具调用"
            "写成普通文本。"
        )
    if previous_candidate:
        safe_candidate = {
            key: value
            for key, value in previous_candidate.items()
            if key != "_runtime"
        }
        candidate_text = json.dumps(
            safe_candidate, ensure_ascii=False, separators=(",", ":")
        )
        if len(candidate_text) > 60_000:
            candidate_text = candidate_text[:60_000]
        parts.append(
            "下面是上一尝试已经解析成功、仅语义校验未通过的候选合同。以它为"
            "基础只修正上面列出的错误；保留其余已正确字段，并返回完整 Schema "
            "对象，不要返回片段：\n"
            f"```json\n{candidate_text}\n```"
        )
    parts.append("按完整输出 Schema 只返回修正后的 JSON 合同。")
    return "\n\n".join(parts)


def _conversation_contract_error(
    output: dict[str, Any],
    *,
    verified_read_paths: set[str] | None = None,
) -> str | None:
    """Validate action-specific invariants even when a test/runtime bypasses JSON Schema."""
    action = output.get("proposed_action")
    if not isinstance(action, dict):
        return "proposed_action must be an object"
    action_type = action.get("type")
    if not isinstance(action_type, str):
        return "proposed_action.type is required"

    if output.get("needs_clarification") or action_type == "request_clarification":
        clarification = output.get("clarification")
        options = clarification.get("options") if isinstance(clarification, dict) else None
        if not isinstance(options, list) or not options:
            return "clarification options are required"

    if action_type == "request_context":
        if not str(action.get("repository_path") or "").strip():
            return "request_context requires repository_path"
        capabilities = action.get("context_capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            return "request_context requires context_capabilities"
        if any(
            capability not in {"workspace_read", "capability_metadata"}
            for capability in capabilities
        ):
            return "request_context contains an unsupported capability"
        read_targets = action.get("read_targets")
        if (
            not isinstance(read_targets, list)
            or not read_targets
            or len(read_targets) > 8
        ):
            return "request_context read_targets must contain 1 to 8 items"

    if action_type == "inspect_workspace":
        inspection = output.get("inspection")
        if not isinstance(inspection, dict):
            return "inspect_workspace requires inspection evidence"
        files_read = inspection.get("files_read")
        findings = inspection.get("findings")
        summary = inspection.get("summary")
        if not isinstance(files_read, list) or not files_read:
            return "inspection.files_read must not be empty"
        if not isinstance(findings, list) or not findings:
            return "inspection.findings must not be empty"
        if not isinstance(summary, str) or not summary.strip():
            return "inspection.summary is required"
        for finding in findings:
            if not isinstance(finding, dict):
                return "inspection.findings entries must be objects"
            if not str(finding.get("title") or "").strip():
                return "inspection finding title is required"
            if not str(finding.get("evidence") or "").strip():
                return "inspection finding evidence is required"
        runtime_evidence = output.get("_runtime")
        if isinstance(runtime_evidence, dict):
            tool_calls = runtime_evidence.get("tool_calls") or []
            files_read_set = {str(path) for path in files_read if str(path)}
            inherited_reads = verified_read_paths or set()
            performed_readonly_inspection = any(
                call.get("name") == "Read"
                or (
                    runtime_evidence.get("provider") == "codex"
                    and call.get("name") == "command_execution"
                )
                for call in tool_calls
                if isinstance(call, dict)
            )
            if not performed_readonly_inspection and not (
                files_read_set and files_read_set.issubset(inherited_reads)
            ):
                return "inspect_workspace did not perform a Read"
    if action_type == "research":
        runtime_evidence = output.get("_runtime")
        tool_calls = (
            runtime_evidence.get("tool_calls") or []
            if isinstance(runtime_evidence, dict)
            else []
        )
        if not any(
            call.get("name") in {"WebSearch", "WebFetch"}
            for call in tool_calls
        ):
            return "research did not perform an external research tool call"

    required_action_fields = {
        "propose_project_control": ("control_action",),
        "propose_approval_resolution": ("approval_decision",),
        "view_artifact": ("artifact_kind",),
    }
    missing = [
        field
        for field in required_action_fields.get(action_type, ())
        if not action.get(field)
    ]
    if missing:
        return f"{action_type} missing fields: {', '.join(missing)}"
    if action_type in {"resolve_pending_action", "propose_approval_resolution"}:
        target = output.get("target")
        if not isinstance(target, dict) or not target.get("id"):
            return f"{action_type} requires an explicit target id"
    if action_type in {"propose_start_delivery", "start_delivery"}:
        task_type = str(action.get("task_type") or "feature")
        if task_type == "iteration":
            if action.get("reference_mode") == "full":
                return "iteration cannot be used for a full reference contract"
            if not action.get("scope"):
                return "iteration requires a non-empty confirmed scope"
            if not action.get("acceptance_criteria"):
                return "iteration requires explicit acceptance criteria"
        decisions = action.get("decisions", [])
        if not isinstance(decisions, list):
            return "propose_start_delivery decisions must be an array"
        required_decision_fields = {"key", "question", "answer", "status"}
        allowed_decision_fields = required_decision_fields | {"details"}
        for index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                return f"propose_start_delivery decisions[{index}] must be an object"
            missing = sorted(required_decision_fields - set(decision))
            unexpected = sorted(set(decision) - allowed_decision_fields)
            if missing:
                return (
                    f"propose_start_delivery decisions[{index}] missing fields: "
                    + ", ".join(missing)
                )
            if unexpected:
                return (
                    f"propose_start_delivery decisions[{index}] unexpected fields: "
                    + ", ".join(unexpected)
                )
            if decision.get("status") != "confirmed":
                return f"propose_start_delivery decisions[{index}].status must be confirmed"
        # Reference discovery belongs to the project's persistent Coordinator
        # and its bounded agents. Requiring a complete source audit in this
        # chat-routing turn duplicated the delivery workflow, inflated context,
        # and delayed an already-authorized request before a project existed.
    return None


_REFERENCE_CONTRACT_KEYS = frozenset({
    "reference.source_repo",
    "reference.capability_inventory",
    "reference.constraints_exclusions",
    "reference.behavioral_probes",
})


_AC_ID_RE = re.compile(r"\bAC-[A-Z0-9][A-Z0-9-]*\b", re.IGNORECASE)


def _acceptance_criterion_reference_matches(
    reference: str, acceptance_criteria: set[str]
) -> bool:
    """Match a probe to an AC by exact text or by its stable AC identifier."""
    if reference in acceptance_criteria:
        return True
    reference_ids = {item.upper() for item in _AC_ID_RE.findall(reference)}
    if not reference_ids:
        return False
    criterion_ids = {
        item.upper()
        for criterion in acceptance_criteria
        for item in _AC_ID_RE.findall(criterion)
    }
    return bool(reference_ids & criterion_ids)


def _contract_errors(errors: list[str]) -> str | None:
    """Return every independently actionable violation in one retry message."""
    return "; ".join(dict.fromkeys(errors)) if errors else None


def _complete_reference_contract_error(
    action: dict[str, Any],
    *,
    runtime_evidence: Any = None,
    verified_read_paths: set[str] | None = None,
) -> str | None:
    """Reject incomplete or silently narrowed complete-reference proposals.

    Prompt guidance alone is not a sufficient boundary for a phrase such as
    "complete reference": the resulting confirmation card is the executable
    project contract.  This validator therefore requires an explicit,
    auditable capability inventory, behavior probes, and exclusions before
    that card can reach the user.  The
    checks are source-agnostic and do not encode any product category,
    repository, identity, framework, or tool name.
    """
    if action.get("reference_mode") != "full":
        return None

    decisions = action.get("decisions") or []

    by_key = {
        str(decision.get("key") or ""): decision
        for decision in decisions
        if isinstance(decision, dict)
    }
    missing_contracts = sorted(_REFERENCE_CONTRACT_KEYS - set(by_key))
    if missing_contracts:
        return (
            "complete-reference proposal missing contracts: "
            + ", ".join(missing_contracts)
        )

    errors: list[str] = []
    read_paths = {
        str((call.get("input") or {}).get("file_path") or "")
        for call in (
            runtime_evidence.get("tool_calls") or []
            if isinstance(runtime_evidence, dict)
            else []
        )
        if isinstance(call, dict) and call.get("name") == "Read"
    }
    read_paths.update(verified_read_paths or set())
    provider = (
        str(runtime_evidence.get("provider") or "")
        if isinstance(runtime_evidence, dict)
        else ""
    )
    # Codex exposes read-only shell inspection as command_execution rather
    # than provider-specific Read events. File existence and quoted source
    # anchors below remain the deterministic evidence check.
    has_readonly_command_evidence = provider == "codex" and any(
        isinstance(call, dict) and call.get("name") == "command_execution"
        for call in (
            runtime_evidence.get("tool_calls") or []
            if isinstance(runtime_evidence, dict)
            else []
        )
    )
    source_repo = by_key.get("reference.source_repo")
    source_repo_details = (
        source_repo.get("details") if isinstance(source_repo, dict) else None
    )
    source_path_text = (
        str(source_repo_details.get("source_path") or "").strip()
        if isinstance(source_repo_details, dict)
        else ""
    )
    source_path = Path(source_path_text)
    source_path_valid = source_path.is_absolute() and source_path.is_dir()
    if not source_path_valid:
        errors.append(
            "complete-reference source_repo.details.source_path must be an "
            "existing absolute directory"
        )
    source_root = source_path.resolve() if source_path_valid else None

    inventory_details = by_key["reference.capability_inventory"].get("details")
    capabilities = (
        inventory_details.get("capabilities")
        if isinstance(inventory_details, dict)
        else None
    )
    if not isinstance(capabilities, list) or not capabilities:
        errors.append(
            "complete-reference capability_inventory.details.capabilities must "
            "be a non-empty array"
        )
        capabilities = []
    capability_ids: set[str] = set()
    user_visible_ids: set[str] = set()
    for index, capability in enumerate(capabilities):
        if not isinstance(capability, dict):
            errors.append(f"complete-reference capabilities[{index}] must be an object")
            continue
        missing = [
            field
            for field in ("id", "name", "category", "observable_behavior", "reuse_policy")
            if not str(capability.get(field) or "").strip()
        ]
        if missing:
            errors.append(
                f"complete-reference capabilities[{index}] missing fields: "
                + ", ".join(missing)
            )
            continue
        capability_id = str(capability["id"]).strip()
        if capability_id in capability_ids:
            errors.append(f"complete-reference duplicate capability id: {capability_id}")
        capability_ids.add(capability_id)
        if capability.get("user_visible") is not False:
            user_visible_ids.add(capability_id)
        source_files = capability.get("source_files")
        if not isinstance(source_files, list) or not source_files:
            errors.append(
                f"complete-reference capabilities[{index}].source_files must not be empty"
            )
            continue
        for file_index, item in enumerate(source_files):
            evidence_path = Path(str(item or ""))
            if source_root is not None and not evidence_path.is_absolute():
                evidence_path = source_root / evidence_path
            try:
                resolved = evidence_path.resolve()
                if source_root is not None:
                    resolved.relative_to(source_root)
            except (OSError, ValueError):
                errors.append(
                    f"complete-reference capabilities[{index}].source_files"
                    f"[{file_index}] is outside source repository"
                )
                continue
            if not resolved.is_file():
                errors.append(
                    f"complete-reference capabilities[{index}].source_files"
                    f"[{file_index}] does not exist"
                )
            elif str(resolved) not in read_paths and not has_readonly_command_evidence:
                errors.append(
                    f"complete-reference capabilities[{index}].source_files"
                    f"[{file_index}] was not read in this run"
                )

    probes = by_key["reference.behavioral_probes"].get("details")
    probes = probes.get("probes") if isinstance(probes, dict) else None
    if not isinstance(probes, list) or not probes:
        errors.append(
            "complete-reference behavioral_probes.details.probes must not be empty"
        )
        probes = []
    acceptance_criteria = {
        str(item).strip() for item in (action.get("acceptance_criteria") or [])
        if str(item).strip()
    }
    covered_capability_ids: set[str] = set()
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            errors.append(f"complete-reference probes[{index}] must be an object")
            continue
        missing_probe_fields = [
            field for field in (
                "capability_id",
                "input",
                "expected_output",
                "acceptance_criterion",
                "source_file",
                "source_quote",
            )
            if not str(probe.get(field) or "").strip()
        ]
        if missing_probe_fields:
            errors.append(
                f"complete-reference probes[{index}] missing fields: "
                + ", ".join(missing_probe_fields)
            )
            continue
        capability_id = str(probe["capability_id"]).strip()
        covered_capability_ids.add(capability_id)
        if capability_id not in capability_ids:
            errors.append(
                f"complete-reference probes[{index}].capability_id is not in "
                "capability_inventory"
            )
        criterion = str(probe["acceptance_criterion"]).strip()
        if not _acceptance_criterion_reference_matches(
            criterion, acceptance_criteria
        ):
            errors.append(
                f"complete-reference probes[{index}].acceptance_criterion "
                "must match an acceptance_criteria item exactly or reference its AC-* id"
            )
        probe_source_path = Path(str(probe["source_file"]).strip())
        if source_root is not None and not probe_source_path.is_absolute():
            probe_source_path = source_root / probe_source_path
        try:
            resolved_probe_source = probe_source_path.resolve()
            if source_root is not None:
                resolved_probe_source.relative_to(source_root)
        except (OSError, ValueError):
            errors.append(
                f"complete-reference probes[{index}].source_file is outside source repository"
            )
            resolved_probe_source = None
        if resolved_probe_source is not None:
            if not resolved_probe_source.is_file():
                errors.append(
                    f"complete-reference probes[{index}].source_file does not exist"
                )
            else:
                try:
                    probe_source_text = resolved_probe_source.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    probe_source_text = ""
                    errors.append(
                        f"complete-reference probes[{index}].source_file could not be verified"
                    )
                if (
                    probe_source_text
                    and str(probe["source_quote"]).strip() not in probe_source_text
                ):
                    errors.append(
                        f"complete-reference probes[{index}].source_quote is absent from source_file"
                    )
                if (
                    str(resolved_probe_source) not in read_paths
                    and not has_readonly_command_evidence
                ):
                    errors.append(
                        f"complete-reference probes[{index}].source_file was not read in this run"
                    )
    missing_probe_capabilities = sorted(user_visible_ids - covered_capability_ids)
    if missing_probe_capabilities:
        errors.append(
            "complete-reference behavioral probes missing user-visible capabilities: "
            + ", ".join(missing_probe_capabilities)
        )
    for key, decision in by_key.items():
        if not key.startswith("reference.") or not isinstance(decision, dict):
            continue
        details = decision.get("details")
        files_read = details.get("files_read") if isinstance(details, dict) else None
        if files_read is None:
            continue
        if not isinstance(files_read, list):
            errors.append(
                f"complete-reference {key}.details.files_read must be an array"
            )
            continue
        if source_root is None:
            continue
        for index, item in enumerate(files_read):
            evidence_path = Path(str(item or ""))
            if not evidence_path.is_absolute():
                evidence_path = source_root / evidence_path
            try:
                resolved_evidence = evidence_path.resolve()
                resolved_evidence.relative_to(source_root)
            except (OSError, ValueError):
                errors.append(
                    f"complete-reference {key}.details.files_read[{index}] "
                    "is outside source repository"
                )
                continue
            if not resolved_evidence.is_file():
                errors.append(
                    f"complete-reference {key}.details.files_read[{index}] "
                    "does not exist"
                )
                continue
            if (
                str(resolved_evidence) not in read_paths
                and not has_readonly_command_evidence
            ):
                errors.append(
                    f"complete-reference {key}.details.files_read[{index}] "
                    "was not read in this run"
                )

    exclusion_details = by_key["reference.constraints_exclusions"].get("details")
    confirmed_exclusions = (
        exclusion_details.get("user_confirmed_exclusions")
        if isinstance(exclusion_details, dict)
        else None
    )
    if not isinstance(confirmed_exclusions, list):
        errors.append(
            "complete-reference constraints_exclusions.details."
            "user_confirmed_exclusions must be an array"
        )
        confirmed_exclusions = []
    confirmed = {str(item).strip() for item in confirmed_exclusions if str(item).strip()}
    for item in action.get("out_of_scope") or []:
        item_text = str(item).strip()
        if not item_text or item_text in confirmed:
            continue
        errors.append(
            "complete-reference out_of_scope requires explicit user confirmation: "
            f"{item_text}"
        )
    return _contract_errors(errors)


def _conversation_state_contract_error(
    output: dict[str, Any],
    *,
    approvals: list[dict[str, Any]],
    pending_actions: list[dict[str, Any]],
) -> str | None:
    """Validate Coordinator actions against the live state snapshot.

    JSON Schema validates the model-authored shape. This guard validates that
    the requested action can actually be applied to the concrete pending
    object before the result leaves the retry loop.
    """
    action = output.get("proposed_action")
    if not isinstance(action, dict):
        return "proposed_action must be an object"
    action_type = action.get("type")
    target_id = str((output.get("target") or {}).get("id") or "")
    intent = str(output.get("intent") or "")

    # A correction/scope change updates the workflow premise; it is never an
    # answer to an existing confirmation card.  Enforce this at the handler
    # boundary because several stale approvals may coexist briefly while an
    # Agent finishes and a new stage starts.  Letting the Coordinator select
    # one of those cards loses the user's change request or applies it to the
    # wrong revision.
    if (
        intent in {"correct", "change_scope"}
        and action_type in {
            "propose_approval_resolution",
            "resolve_pending_action",
            "resend_approval",
        }
    ):
        return (
            "correct and change_scope intents cannot resolve, confirm, reject, "
            "or resend a pending approval; route active-project input, start a "
            "new terminal-project iteration, or propose an explicit project "
            "control action"
        )

    if action_type == "propose_approval_resolution":
        matches = [item for item in approvals if item.get("id") == target_id]
        if len(matches) != 1:
            return "approval resolution target is not a unique pending approval"
        approval = matches[0]
        if approval.get("type") == "input":
            return (
                "input approval cannot be confirmed or rejected; answer its "
                "question through route_project_input"
            )
        options = approval.get("options") or []
        decision = action.get("approval_decision")
        valid_keys = {
            item.get("option_key")
            for item in options
            if isinstance(item, dict) and item.get("option_key")
        }
        if valid_keys:
            option_key = action.get("option_key")
            if decision != "select_option" or option_key not in valid_keys:
                return (
                    "option approval requires select_option with one listed "
                    "option_key"
                )
        elif decision not in {"confirm", "reject"}:
            return "confirmation approval requires confirm or reject"

    if action_type == "resolve_pending_action":
        matches = [item for item in pending_actions if item.get("id") == target_id]
        if len(matches) != 1:
            return "pending action target is not unique"
        pending = matches[0]
        if pending.get("type") == "clarification":
            options = (pending.get("payload") or {}).get("options") or []
            valid_keys = {
                item.get("key")
                for item in options
                if isinstance(item, dict) and item.get("key")
            }
            option_key = action.get("option_key")
            if valid_keys and option_key not in valid_keys:
                return (
                    "clarification resolution requires one listed option_key; "
                    "do not close the question without recording its answer"
                )
    if action_type == "route_project_input":
        input_approvals = [item for item in approvals if item.get("type") == "input"]
        is_workflow_correction = (
            output.get("intent") in {"correct", "change_scope"}
            and bool(str(action.get("target_stage") or "").strip())
        )
        if input_approvals and not is_workflow_correction:
            questions = input_approvals[0].get("input_questions") or []
            question_id = str(action.get("question_id") or "")
            pending_questions = [
                item for item in questions if item.get("status") == "pending"
            ]
            if len(questions) > 1 and not question_id:
                return (
                    "route_project_input must include the semantically matched "
                    "question_id when an input interaction contains multiple questions"
                )
            target = None
            if question_id:
                target = next(
                    (item for item in questions if item.get("question_id") == question_id),
                    None,
                )
                if target is None:
                    return "route_project_input question_id is not part of the pending input approval"
                if target.get("status") != "pending" and output.get("intent") != "correct":
                    return "an answered input question may only be targeted by an explicit correction"
            elif len(pending_questions) != 1:
                return "route_project_input could not resolve a unique pending input question"
            else:
                target = pending_questions[0]
            option_key = str(action.get("option_key") or "")
            if option_key and target is not None:
                valid_keys = {
                    str(option.get("key") or option.get("option_key") or "")
                    for option in (target.get("options") or [])
                    if isinstance(option, dict)
                }
                valid_keys.discard("")
                if option_key not in valid_keys:
                    return (
                        "route_project_input option_key must match one option "
                        "from the targeted input question"
                    )
        pending_action_id = str(action.get("pending_action_id") or "")
        pending_option_key = str(action.get("pending_option_key") or "")
        if bool(pending_action_id) != bool(pending_option_key):
            return (
                "route_project_input must include both pending_action_id and "
                "pending_option_key for an atomic clarification resolution"
            )
        if pending_action_id:
            matches = [
                item for item in pending_actions
                if item.get("id") == pending_action_id
                and item.get("type") == "clarification"
            ]
            if len(matches) != 1:
                return "route_project_input pending_action_id is not a pending clarification"
            valid_pending_keys = {
                str(item.get("key") or "")
                for item in ((matches[0].get("payload") or {}).get("options") or [])
                if isinstance(item, dict)
            }
            valid_pending_keys.discard("")
            if pending_option_key not in valid_pending_keys:
                return "route_project_input pending_option_key is not listed by the clarification"
    return None


def _capability_refs_contract_error(output: dict[str, Any]) -> str | None:
    """Validate every proposed secret reference before any project mutation."""
    action = output.get("proposed_action")
    if not isinstance(action, dict) or action.get("type") != "route_project_input":
        return None
    capability_refs = action.get("capability_refs") or []
    if not capability_refs:
        return None
    allowed_roots = get_allowed_bind_roots()
    for index, capability in enumerate(capability_refs):
        if not isinstance(capability, dict):
            return f"capability_refs[{index}] must be an object"
        source_file = Path(str(capability.get("source_file") or ""))
        bindings = capability.get("env_bindings")
        if not source_file.is_absolute() or not source_file.is_file():
            return f"capability_refs[{index}] source_file is unavailable"
        if not isinstance(bindings, dict) or not bindings:
            return f"capability_refs[{index}] env_bindings is empty"
        if allowed_roots and not _path_within_allowed_roots(
            source_file, allowed_roots
        ):
            return f"capability_refs[{index}] source_file is outside allowed roots"
    return None


def _compact_coordinator_task(task: Task) -> dict[str, Any]:
    """Return bounded, decision-useful task facts for the Coordinator."""
    output = task.output_data if isinstance(task.output_data, dict) else {}
    common = {
        "id": task.id,
        "agent": task.agent_type,
        "state": task.state,
        "round": task.round_number,
        "status": task.status,
        "cost_usd": task.cost_usd,
        "profiling": redact_secrets(output.get("_profiling") or {}),
        "error": redact_text(str(task.error_message or ""))[:800] or None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
    allowed_by_agent = {
        "coder": (
            "summary", "modified_files", "addressed_feedback", "notes",
            "deferred_obligations",
        ),
        "tester": (
            "passed", "report", "test_evidence", "failures",
            "uncovered_acceptance_criterion_ids", "coverage_status", "coverage", "notes",
        ),
        "reviewer": (
            "result", "summary", "issues", "requirement_coverage",
            "stale_artifact_ids", "notes",
        ),
        "acceptance_agent": (
            "result", "summary", "criteria", "user_input_requests", "environment",
        ),
        "acceptance_evaluator": (
            "result", "summary", "recommendation", "acceptance_matrix",
            "blocking_conditions",
        ),
    }
    keys = allowed_by_agent.get(task.agent_type, ("summary", "notes"))
    selected = {key: output[key] for key in keys if key in output}
    # Keep the snapshot bounded. Full outputs remain in Task/.mac and can be
    # shown by deterministic handlers when explicitly requested.
    serialized = json.dumps(redact_secrets(selected), ensure_ascii=False)
    if len(serialized) > 12000:
        selected = {
            "summary": str(
                output.get("report")
                or output.get("summary")
                or "任务输出过长，已省略明细"
            )[:3000],
            "evidence_count": len(output.get("test_evidence") or []),
            "issue_count": len(output.get("issues") or []),
            "uncovered_acceptance_criterion_ids": output.get(
                "uncovered_acceptance_criterion_ids", []
            ),
            "notes": (output.get("notes") or [])[:8],
        }
    return {**common, "output": redact_secrets(selected)}


def _coordinator_project_snapshot(
    ctx: FeishuHandlerContext,
    project: Project,
) -> dict[str, Any]:
    """Build an authoritative read-only project snapshot for conversation turns."""
    tasks = list(
        ctx.service.session.exec(
            select(Task)
            .where(Task.project_id == project.id)
            .order_by(Task.updated_at.desc())
        ).all()
    )
    latest_by_agent: dict[str, Task] = {}
    for task in tasks:
        if task.agent_type not in latest_by_agent:
            latest_by_agent[task.agent_type] = task
    task_snapshot = [
        _compact_coordinator_task(task)
        for task in latest_by_agent.values()
        if task.agent_type in {
            "planner", "requirements_analyst", "architect", "spec_writer",
            "bug_analyst", "documentation_planner", "coder", "tester",
            "reviewer", "acceptance_agent", "acceptance_evaluator"
        }
    ]
    # Do not append an older failed attempt after a newer attempt from the
    # same Agent has completed. That failure is audit history, not the current
    # project truth, and previously caused the Coordinator to repeatedly tell
    # users that coding/testing had failed after an automatic retry succeeded.
    # An exhausted failure remains visible because it is itself the latest
    # task for that Agent and the project is in *_RECOVERY_PENDING.

    facts = []
    for record in ctx.service.get_active_context_records(project.id):
        facts.append(
            {
                "fact_id": record.id,
                "key": record.record_key,
                "type": record.record_type,
                "value": redact_secrets(record.value),
                "confidence": record.confidence,
                "sensitivity": record.sensitivity,
                "updated_at": (
                    record.updated_at.isoformat()
                    if getattr(record, "updated_at", None)
                    else None
                ),
            }
        )

    feedback = [
        {
            "id": item.id,
            "source_stage": item.source_stage,
            "kind": item.kind,
            "summary": redact_text(item.summary),
            "blocking": item.blocking,
            "status": item.status,
            "recommended_rework_target": item.recommended_rework_target,
        }
        for item in ctx.service.session.exec(
            select(FeedbackPacket)
            .where(
                FeedbackPacket.project_id == project.id,
                FeedbackPacket.status.in_(["open", "disputed"]),
            )
            .order_by(FeedbackPacket.created_at.desc())
        ).all()
    ]
    lineage: list[dict[str, Any]] = []
    seen_lineage_ids: set[str] = set()
    base_project_id = str(project.context.get("base_project_id") or "").strip()
    while base_project_id and base_project_id not in seen_lineage_ids and len(lineage) < 6:
        seen_lineage_ids.add(base_project_id)
        base_project = ctx.service.get_project(base_project_id)
        if base_project is None:
            break
        lineage.append({
            "id": base_project.id,
            "name": base_project.name,
            "task_type": base_project.task_type,
            "status": base_project.status,
            "current_state": base_project.current_state,
            "total_cost_usd": base_project.total_cost_usd or 0.0,
            "updated_at": base_project.updated_at.isoformat(),
        })
        base_project_id = str(
            base_project.context.get("base_project_id") or ""
        ).strip()

    return {
        "id": project.id,
        "name": project.name,
        "task_type": project.task_type,
        "status": project.status,
        "current_state": project.current_state,
        "context_version": project.context_version,
        "total_cost_usd": project.total_cost_usd or 0.0,
        "cost_basis": (
            "sum of provider-reported task costs recorded by MAC; providers "
            "that do not report a dollar estimate may contribute 0"
        ),
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "lineage": lineage,
        "latest_tasks": task_snapshot,
        "active_facts": facts[-60:],
        "open_feedback": feedback[:30],
        "artifact_versions": project.context.get("artifact_versions", {}),
    }


def _coordinator_approval_snapshot(
    ctx: FeishuHandlerContext,
    approval: Approval,
) -> dict[str, Any]:
    """Serialize a pending approval with the question facts needed for routing."""
    payload = approval.payload or {}
    snapshot: dict[str, Any] = {
        "id": approval.id,
        "type": approval.approval_type,
        "state": approval.state,
        "question": approval.question,
        "options": [
            {
                # Recovery approvals historically expose only ``action``.
                # Normalize non-binary actions so the Coordinator can select
                # them through the same option contract as REWORK cards.
                "option_key": (
                    opt.get("option_key")
                    or (
                        opt.get("action")
                        if opt.get("action") not in {"confirm", "reject"}
                        else None
                    )
                ),
                "label": opt.get("label"),
            }
            for opt in payload.get("options", [])
            if isinstance(opt, dict)
        ],
    }
    interaction_id = payload.get("question_interaction_id")
    if approval.approval_type == "input" and interaction_id:
        snapshot["question_interaction_id"] = interaction_id
        snapshot["input_questions"] = [
            {
                "question_id": question.question_id,
                "question": question.question,
                "reason": question.reason,
                "kind": question.kind,
                "allow_free_text": question.allow_free_text,
                "options": question.options,
                "status": question.status,
                "current_answer": (
                    answer.answer
                    if (answer := ctx.service.get_question_answer(question.id))
                    else None
                ),
            }
            for question in ctx.service.get_questions_for_interaction(
                interaction_id
            )
        ]
    return redact_secrets(snapshot)


async def _refresh_conversation_progress(
    ctx: FeishuHandlerContext, message_id: str
) -> None:
    """Keep a long Coordinator turn visibly alive without sending new messages."""
    started = time.monotonic()
    while True:
        await asyncio.sleep(10)
        elapsed = int(time.monotonic() - started)
        try:
            await _update_interactive_card(ctx, 
                message_id,
                build_conversation_progress_card(
                    stage="working",
                    detail=f"已处理 {elapsed} 秒，仍在调研与分析",
                ),
            )
        except Exception:
            logger.warning("Failed to refresh conversation progress card", exc_info=True)


async def _apply_conversation_result(
    ctx: FeishuHandlerContext,
    msg: TextMessage,
    conversation: Any,
    output: dict[str, Any],
    *,
    active_project: Project | None,
    recent_project: Project | None,
    progress_message_id: str | None = None,
) -> None:
    """Apply one schema-validated coordinator result through safe handlers."""
    open_id = msg.open_id
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    raw_reply = str(output.get("reply") or "").strip()
    reply = _strip_protocol_echo(raw_reply)
    if len(reply) < len(raw_reply):
        logger.warning(
            "Stripped protocol JSON echo from coordinator reply message=%s",
            msg.message_id,
        )
    if not reply:
        reply = "本次回复格式异常，请重试。"
    action = output.get("proposed_action") or {"type": "none"}
    action_type = action.get("type", "none")
    sent_message_id: str | None = None
    effective_reply = ""

    async def send_effective_reply(
        text: str, *, title: str = "答复"
    ) -> str:
        """Send and remember the text that actually reached the user."""
        nonlocal effective_reply
        effective_reply = text
        return await _send_conversation_reply(
            ctx,
            receive_id,
            receive_id_type,
            text,
            title=title,
            message_id=progress_message_id,
        )

    clarification = output.get("clarification") or {}
    clarification_options = clarification.get("options") or []
    is_clarification = bool(
        output.get("needs_clarification") or action_type == "request_clarification"
    )
    if action_type == "route_project_input" and action.get("capability_refs"):
        unresolved_capability_choices = [
            item
            for item in ctx.service.get_session_pending_actions(conversation.id)
            if item.action_type == "clarification"
            and str((item.payload or {}).get("subject_key") or "").startswith(
                ("runtime.", "capability.", "env_config.")
            )
        ]
        resolved_choice = None
        pending_action_id = str(action.get("pending_action_id") or "")
        pending_option_key = str(action.get("pending_option_key") or "")
        if len(unresolved_capability_choices) == 1 and pending_action_id:
            candidate = unresolved_capability_choices[0]
            options = (candidate.payload or {}).get("options") or []
            selected = next(
                (
                    item for item in options
                    if isinstance(item, dict)
                    and str(item.get("key") or "") == pending_option_key
                ),
                None,
            )
            if candidate.id == pending_action_id and selected is not None:
                ctx.service.resolve_pending_conversation_action(
                    candidate.id,
                    decision="confirm",
                    resolved_by=open_id,
                )
                resolved_choice = candidate
                card_message_id = (candidate.payload or {}).get("card_message_id")
                if card_message_id:
                    await _update_interactive_card(ctx, 
                        card_message_id,
                        build_conversation_clarification_card(
                            candidate.id,
                            str((candidate.payload or {}).get("question") or "当前问题"),
                            options,
                            reason=str((candidate.payload or {}).get("reason") or ""),
                            chat_id=ctx.chat_id,
                            chat_type=ctx.chat_type,
                            selected_key=pending_option_key,
                        ),
                    )
        if unresolved_capability_choices and resolved_choice is None:
            # A capability cannot be both ambiguous and applied. The card click
            # resolves the pending choice first and starts a new Coordinator
            # turn, after which one concrete reference may pass this gate.
            action_type = "answer"
            action = {"type": "answer"}
            is_clarification = False
            reply = "源配置包含多个不兼容方案，请先从最新配置方案卡中选择；当前尚未写入任何运行时能力。"
    if is_clarification and clarification_options:
        payload = {
            "subject_key": (
                clarification.get("subject_key")
                or "conversation:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "target": output.get("target") or {},
                            "option_keys": sorted(
                                str(item.get("key"))
                                for item in clarification_options
                                if isinstance(item, dict) and item.get("key")
                            ),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            "question": clarification.get("question") or reply,
            "reason": clarification.get("reason") or "",
            "options": clarification_options[:3],
            "allow_free_text": bool(clarification.get("allow_free_text")),
        }
        digest_source = json.dumps(
            {
                "session": conversation.id,
                "message_id": msg.message_id,
                "clarification": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        pending = next(
            (
                item
                for item in ctx.service.get_session_pending_actions(conversation.id)
                if item.action_type == "clarification"
                and (item.payload or {}).get("subject_key") == payload["subject_key"]
            ),
            None,
        )
        existing_card_id = None
        if pending is None:
            pending = ctx.service.create_pending_conversation_action(
                session_id=conversation.id,
                action_type="clarification",
                payload=payload,
                source_message_ids=[msg.message_id] if msg.message_id else [],
                idempotency_key=hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
                created_by=open_id,
                expire_existing=False,
            )
        else:
            existing_card_id = (pending.payload or {}).get("card_message_id")
            pending.payload = {**pending.payload, **payload}
            if msg.message_id and msg.message_id not in pending.source_message_ids:
                pending.source_message_ids = [*pending.source_message_ids, msg.message_id]
            ctx.service.session.add(pending)
            ctx.service.session.commit()
        clarification_card = build_conversation_clarification_card(
                pending.id,
                payload["question"],
                payload["options"],
                reason=payload["reason"],
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
        )
        sent_message_id = await _deliver_conversation_card(
            ctx, receive_id, receive_id_type, clarification_card,
            message_id=existing_card_id or progress_message_id,
        )
        pending.payload = {**pending.payload, "card_message_id": sent_message_id}
        ctx.service.session.add(pending)
        ctx.service.session.commit()
        effective_reply = f"等待用户选择：{payload['question']}"
    elif action_type == "resolve_pending_action":
        pending = ctx.service.get_session_pending_actions(conversation.id)
        target_id = (output.get("target") or {}).get("id")
        if target_id:
            pending = [item for item in pending if item.id == target_id]
        if len(pending) != 1 or output.get("commitment") != "authorized":
            sent_message_id = await send_effective_reply(
                "当前无法唯一确定要确认的执行动作，请从最新决策卡片中选择。"
            )
        else:
            target_action = pending[0]
            if target_action.action_type == "clarification":
                option_key = str(action.get("option_key") or "")
                options = (target_action.payload or {}).get("options") or []
                selected = next(
                    (
                        item
                        for item in options
                        if isinstance(item, dict)
                        and str(item.get("key") or "") == option_key
                    ),
                    None,
                )
                if not options:
                    ctx.service.resolve_pending_conversation_action(
                        target_action.id,
                        decision="confirm",
                        resolved_by=open_id,
                    )
                    await send_effective_reply(
                        reply or "已记录这条澄清答案，正在应用到后续处理。"
                    )
                    question = str(
                        (target_action.payload or {}).get("question")
                        or "当前问题"
                    )
                    await _handle_coordinated_free_text(
                        ctx,
                        TextMessage(
                            open_id=open_id,
                            message_id=f"text:{target_action.id}:free_text",
                            text=f"关于“{question}”，用户回答：{msg.text}",
                            is_command=False,
                            chat_id=ctx.chat_id,
                            chat_type=ctx.chat_type,
                        ),
                        progress_message_id=None,
                    )
                    return
                if selected is None:
                    sent_message_id = await send_effective_reply(
                        "没有找到对应的澄清选项，请从最新选择卡片中选择。"
                    )
                    # The semantic retry guard should prevent this branch, but
                    # keep the deterministic handler fail-closed.
                    selected = None
                if selected is None:
                    target_action = None
                else:
                    ctx.service.resolve_pending_conversation_action(
                        target_action.id,
                        decision="confirm",
                        resolved_by=open_id,
                    )
                    card_message_id = (target_action.payload or {}).get(
                        "card_message_id"
                    )
                    if card_message_id:
                        await _update_interactive_card(ctx, 
                            card_message_id,
                            build_conversation_clarification_card(
                                target_action.id,
                                str(
                                    (target_action.payload or {}).get("question")
                                    or "当前问题"
                                ),
                                options,
                                reason=str(
                                    (target_action.payload or {}).get("reason")
                                    or ""
                                ),
                                chat_id=ctx.chat_id,
                                chat_type=ctx.chat_type,
                                selected_key=option_key,
                            ),
                        )
                    label = str(selected.get("label") or option_key)
                    question = str(
                        (target_action.payload or {}).get("question")
                        or "当前问题"
                    )
                    await _handle_coordinated_free_text(
                        ctx,
                        TextMessage(
                            open_id=open_id,
                            message_id=f"text:{target_action.id}:{option_key}",
                            text=(
                                f"关于“{question}”，我选择「{label}」"
                                f"（{option_key}）。"
                            ),
                            is_command=False,
                            chat_id=ctx.chat_id,
                            chat_type=ctx.chat_type,
                        ),
                        progress_message_id=None,
                    )
                    return
            if target_action is None:
                pass
            elif target_action.action_type == "clarification":
                # Handled above, including starting the follow-up Coordinator
                # turn that applies the selected answer.
                return
            else:
                await _execute_pending_conversation_action(
                    ctx, open_id, target_action.id, decision="confirm"
                )
                ctx.service.session.refresh(target_action)
                effective_reply = (
                    f"待确认动作 {target_action.id} 的实际处理状态："
                    f"{target_action.status}。"
                )
    elif action_type == "propose_approval_resolution":
        target_id = (output.get("target") or {}).get("id")
        pending_approvals = []
        if active_project is not None:
            pending_approvals = list(
                ctx.service.session.exec(
                    select(Approval).where(
                        Approval.project_id == active_project.id,
                        Approval.status == "pending",
                    )
                ).all()
            )
        if target_id:
            pending_approvals = [a for a in pending_approvals if a.id == target_id]
        decision = action.get("approval_decision")
        if (
            len(pending_approvals) != 1
            or output.get("commitment") != "authorized"
            or decision not in {"confirm", "reject", "select_option"}
        ):
            sent_message_id = await send_effective_reply(
                "当前无法唯一确定待处理审批，请从最新审批卡片中选择。"
            )
        else:
            approval = pending_approvals[0]
            approval_options = (approval.payload or {}).get("options", [])
            option_key = action.get("option_key")
            # Translate the coordinator's decision into a concrete approval
            # event and validate it against the approval's own options BEFORE
            # anything is sent — the reply must describe what actually
            # happened, not what the coordinator hoped would happen.
            if decision == "select_option":
                chosen = next(
                    (
                        opt.get("action")
                        for opt in approval_options
                        if (opt.get("option_key") or opt.get("action")) == option_key
                    ),
                    None,
                )
            elif decision in {opt.get("action") for opt in approval_options}:
                chosen = decision
            else:
                chosen = None
            if chosen is None:
                logger.warning(
                    "Coordinator proposed invalid decision %s (option_key=%s) for approval %s",
                    decision, option_key, approval.id,
                )
                sent_message_id = await send_effective_reply(
                    "无法按该方式处理当前审批，请从最新审批卡片中选择。"
                )
            else:
                try:
                    extra = {"option_key": option_key} if option_key else None
                    feedback_text = action.get("feedback")
                    if approval.state == "REWORK" and not feedback_text:
                        selected_option = next(
                            (
                                opt
                                for opt in approval_options
                                if opt.get("action") == chosen
                            ),
                            {},
                        )
                        bare_selections = {
                            str(value).strip()
                            for value in (
                                chosen,
                                option_key,
                                selected_option.get("label"),
                            )
                            if value
                        }
                        user_text = msg.text.strip()
                        # A REWORK choice often carries the actual defect report
                        # in the same natural-language turn.  Preserve that text
                        # when the Coordinator omitted ``feedback``; otherwise
                        # the state transition succeeds but the downstream Agent
                        # only sees the older quality failure.
                        if user_text and user_text not in bare_selections:
                            feedback_text = user_text
                    await _respond_to_approval(
                        ctx,
                        open_id,
                        approval.id,
                        chosen,
                        feedback_text=feedback_text,
                        extra=extra,
                    )
                except Exception:
                    logger.exception(
                        "Failed to resolve approval %s via coordinator", approval.id
                    )
                    sent_message_id = await send_effective_reply(
                        "处理审批时出错，审批仍待处理。请从最新审批卡片重试。"
                    )
                else:
                    ctx.service.session.refresh(approval)
                    if approval.status == "pending":
                        effective_reply = f"审批 {approval.id} 仍待处理。"
                    else:
                        refreshed_project = ctx.service.require_project(
                            approval.project_id
                        )
                        action_label = next(
                            (
                                str(opt.get("label") or chosen)
                                for opt in approval_options
                                if opt.get("action") == chosen
                            ),
                            str(chosen),
                        )
                        if refreshed_project.current_state == "REWORK":
                            feedback_note = (
                                f" 已记录你的反馈：{action.get('feedback')}。"
                                if action.get("feedback")
                                else ""
                            )
                            effective_reply = (
                                f"已{action_label}。项目已进入返工选择，"
                                "我已发送返工路径卡；请选择回到需求、方案、"
                                f"实现计划、编码或测试阶段。{feedback_note}"
                            )
                        else:
                            state_label = StateMachine(
                                refreshed_project.task_type
                            ).display_name(refreshed_project.current_state)
                            effective_reply = (
                                f"已{action_label}。项目已进入{state_label}，"
                                "并按实际结果继续推进。"
                            )
                        sent_message_id = await send_effective_reply(
                            effective_reply
                        )
    elif action_type in {"propose_start_delivery", "start_delivery"}:
        # Guard: never spawn a new project while one is already active.
        # The coordinator is instructed to route new-requirement text into the
        # current project (route_project_input / propose_project_control), but
        # if it slips through we must not silently create a parallel project.
        if active_project is not None and active_project.status in {"active", "paused"}:
            # Always override the coordinator's reply here — if the guard fires
            # the coordinator is confused and its reply would be misleading.
            sent_message_id = await send_effective_reply(
                (
                    f"当前会话已有进行中的项目“{active_project.name or '未命名'}”。"
                    "如果要改需求请直接说明改动点，"
                    "或先取消当前项目再开始新项目。"
                )
            )
        else:
            base_project_id = str(action.get("base_project_id") or "").strip()
            base_project = (
                ctx.service.get_project(base_project_id) if base_project_id else None
            )
            if base_project_id and (
                base_project is None
                or base_project.status in {"active", "paused"}
                or recent_project is None
                or base_project.id != recent_project.id
            ):
                await send_effective_reply(
                    "无法确认要继续修改的已结束项目，请重新选择项目后再试。"
                )
                return
            if ctx.service.get_pending_intent(open_id) is not None:
                ctx.service.delete_pending_intent(open_id)
            payload = {
                "task_type": action.get("task_type", "feature"),
                "objective": action.get("description") or msg.text,
                "description": action.get("description") or msg.text,
                "scope": action.get("scope") or [],
                "out_of_scope": action.get("out_of_scope") or [],
                "permissions": action.get("permissions") or ["workspace_write"],
                "acceptance_criteria": action.get("acceptance_criteria") or [],
                "delivery_policy": action.get("delivery_policy") or {},
                "reference_mode": action.get("reference_mode") or "none",
                "decisions": action.get("decisions") or [],
                "base_project_id": base_project_id or None,
            }
            if action_type == "start_delivery":
                if output.get("commitment") != "authorized":
                    await send_effective_reply(
                        "当前请求尚未形成明确执行授权，未开始修改。"
                    )
                    return
                try:
                    project = await _start_delivery_from_payload(
                        ctx,
                        open_id,
                        payload,
                        source_session_id=conversation.id,
                        source_message_ids=[msg.message_id] if msg.message_id else [],
                    )
                except Exception as exc:
                    logger.exception("Failed to start authorized delivery")
                    await send_effective_reply(f"未能开始实施：{exc}")
                    return
                effective_reply = (
                    f"已在原项目目录启动“{project.name or '后续修改'}”，"
                    "正在按新的交付流程修复并重新验收。"
                    if base_project_id else
                    f"已启动项目“{project.name or '未命名'}”，正在准备第一个交付阶段。"
                )
                if progress_message_id:
                    sent_message_id = await _send_conversation_reply(
                        ctx, receive_id, receive_id_type, effective_reply,
                        message_id=progress_message_id,
                    )
                else:
                    sent_message_id = await _send_card_message(ctx, 
                        receive_id, effective_reply,
                        receive_id_type=receive_id_type,
                    )
                ctx.service.update_conversation_session(
                    conversation,
                    summary=effective_reply,
                    active_project_id=project.id,
                    update_active_project=True,
                )
                ctx.service.append_conversation_turn(
                    conversation.chat_id,
                    role="assistant",
                    content=effective_reply,
                    message_id=sent_message_id,
                )
                # Do not bind a multi-minute Agent run to the Feishu event
                # callback.  The durable project exists and the orchestrator's
                # level-triggered scheduler owns all subsequent work.
                ctx.orchestrator._schedule_followup_tick(project.id)
                return
            digest_source = json.dumps(
                {
                    "session": conversation.id,
                    "message_id": msg.message_id,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            idem_key = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            pending = ctx.service.create_pending_conversation_action(
                session_id=conversation.id,
                action_type="start_delivery",
                payload=payload,
                source_message_ids=[msg.message_id] if msg.message_id else [],
                idempotency_key=idem_key,
                created_by=open_id,
            )
            sent_message_id = await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_pending_action_card(
                    pending.id,
                    pending.payload,
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                ),
                message_id=progress_message_id,
            )
            effective_reply = "已生成待确认的执行范围。"
    elif action_type == "propose_project_control":
        control_action = str(action.get("control_action") or "").strip()
        target_stage = str(action.get("target_stage") or "").strip() or None
        valid_actions = {"pause", "resume", "cancel", "rollback"}
        if control_action not in valid_actions:
            sent_message_id = await send_effective_reply(
                "暂不支持该项目操作。"
            )
        elif active_project is None:
            sent_message_id = await send_effective_reply(
                "当前没有可操作的活跃项目。"
            )
        elif control_action == "rollback":
            sm = StateMachine(active_project.task_type)
            rollback_targets = {
                item["action"].removeprefix("to_"): item["next_state"]
                for item in sm.rework_options()
            }
            if not target_stage or target_stage not in rollback_targets:
                sent_message_id = await send_effective_reply(
                    "请告诉我要回滚到哪个阶段（需求 / 方案 / 实现计划 / 编码 / 测试）。"
                )
            elif rollback_targets[target_stage] == active_project.current_state:
                sent_message_id = await send_effective_reply(
                    "项目已经处于该阶段，无需回滚。"
                )
            elif not _is_valid_rollback_target(
                sm, active_project.current_state, rollback_targets[target_stage]
            ):
                sent_message_id = await send_effective_reply(
                    "只能回滚到当前阶段之前的需求、方案、实现计划、编码或测试阶段。"
                )
            else:
                payload = {
                    "control_action": control_action,
                    "target_project_id": active_project.id,
                    "target_project_name": active_project.name or "",
                    "target_stage": target_stage,
                }
                digest_source = json.dumps(
                    {
                        "session": conversation.id,
                        "message_id": msg.message_id,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                idem_key = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
                pending = ctx.service.create_pending_conversation_action(
                    session_id=conversation.id,
                    action_type="project_control",
                    target_project_id=active_project.id,
                    payload=payload,
                    source_message_ids=[msg.message_id] if msg.message_id else [],
                    idempotency_key=idem_key,
                    created_by=open_id,
                )
                sent_message_id = await _deliver_conversation_card(
                    ctx, receive_id, receive_id_type,
                    build_project_control_card(
                        pending.id,
                        control_action,
                        payload["target_project_name"] or active_project.id,
                        active_project.id,
                        chat_id=ctx.chat_id,
                        chat_type=ctx.chat_type,
                        target_stage=target_stage,
                    ),
                    message_id=progress_message_id,
                )
                effective_reply = "已生成项目回滚确认卡。"
        else:
            payload = {
                "control_action": control_action,
                "target_project_id": active_project.id,
                "target_project_name": active_project.name or "",
            }
            digest_source = json.dumps(
                {
                    "session": conversation.id,
                    "message_id": msg.message_id,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            idem_key = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            pending = ctx.service.create_pending_conversation_action(
                session_id=conversation.id,
                action_type="project_control",
                target_project_id=active_project.id,
                payload=payload,
                source_message_ids=[msg.message_id] if msg.message_id else [],
                idempotency_key=idem_key,
                created_by=open_id,
            )
            sent_message_id = await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_project_control_card(
                    pending.id,
                    control_action,
                    payload["target_project_name"] or active_project.id,
                    active_project.id,
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                ),
                message_id=progress_message_id,
            )
            effective_reply = f"已生成项目{control_action}确认卡。"
    elif action_type == "view_artifact":
        artifact_kind = str(action.get("artifact_kind") or "").strip()
        filename_by_kind = {
            "requirements": "REQUIREMENTS.json",
            "architecture": "ARCHITECTURE.json",
            "specs": "SPECS.json",
        }
        filename = filename_by_kind.get(artifact_kind)
        if active_project is None:
            sent_message_id = await send_effective_reply(
                "当前没有可查看的活跃项目。"
            )
        elif not filename:
            sent_message_id = await send_effective_reply(
                "暂不支持查看该类型的产物。"
            )
        else:
            artifact_path = project_mac_dir(active_project) / filename
            if not artifact_path.exists():
                sent_message_id = await send_effective_reply(
                    f"当前项目还没有产出 `{filename}`；对应阶段可能还未完成。"
                )
            else:
                try:
                    artifact_text = artifact_path.read_text(encoding="utf-8")
                    try:
                        parsed = json.loads(artifact_text)
                        artifact_text = json.dumps(
                            parsed, ensure_ascii=False, indent=2
                        )
                    except json.JSONDecodeError:
                        pass
                    max_len = 4000
                    if len(artifact_text) > max_len:
                        artifact_text = (
                            artifact_text[:max_len]
                            + f"\n\n…（已截断，共 {len(artifact_text)} 字符）"
                        )
                    label = {
                        "requirements": "需求文档",
                        "architecture": "技术方案",
                        "specs": "实现计划",
                    }[artifact_kind]
                    sent_message_id = await send_effective_reply(
                        f"**{label}**（`{filename}`）：\n\n```json\n{artifact_text}\n```"
                    )
                except OSError as exc:
                    logger.warning("Failed to read artifact %s: %s", artifact_path, exc)
                    sent_message_id = await send_effective_reply(
                        f"读取 `{filename}` 失败：{exc}"
                    )
    elif action_type == "query_status" and active_project is not None:
        await _show_status(ctx, open_id, active_project.id)
        state_label = StateMachine(active_project.task_type).display_name(
            active_project.current_state
        )
        effective_reply = (
            f"已展示“{active_project.name or '当前项目'}”的实际状态："
            f"{state_label}。"
        )
    elif action_type == "resend_approval" and active_project is not None:
        approval = ctx.service.session.exec(
            select(Approval)
            .where(
                Approval.project_id == active_project.id,
                Approval.status == "pending",
            )
            .order_by(Approval.created_at.desc())
        ).first()
        if approval is None:
            sent_message_id = await send_effective_reply("当前没有待处理的审批。")
        else:
            question, options = ctx.orchestrator._build_confirmation_question(
                active_project, active_project.current_state
            )
            sent_message_id = await ctx.orchestrator._send_confirmation_cards(
                active_project,
                approval,
                question,
                options,
                delivery_key_suffix=f":resend:{msg.message_id or int(time.time())}",
            )
            effective_reply = f"已重新发送审批 {approval.id} 的汇报卡和操作卡。"
    elif action_type == "route_project_input" and active_project is not None:
        state_machine = StateMachine(active_project.task_type)
        running_state_tasks = [
            task
            for task in ctx.service._running_tasks(active_project.id)
            if task.state == active_project.current_state
        ]
        if state_machine.is_agent_execution(active_project.current_state) and running_state_tasks:
            intent = output.get("intent")
            kind = _intent_to_pending_input_kind(intent)
            await ctx.service.create_pending_user_input(
                project_id=active_project.id,
                sender_id=open_id,
                observed_state=active_project.current_state,
                content=msg.text,
                kind=kind,
                source_chat_id=ctx.chat_id,
                recommended_rework_target=(
                    str(action.get("target_stage") or "").strip() or None
                ),
            )
            interrupted: list[str] = []
            if kind in {"scope_change", "correction"}:
                interrupt = getattr(ctx.orchestrator, "interrupt_running_agents", None)
                if callable(interrupt):
                    interrupted = interrupt(active_project.id)
            sent_message_id = await send_effective_reply(
                (
                    "已记录这项更正，并中断正在使用旧上下文的 Agent；"
                    "系统会基于新信息重新执行。"
                    if interrupted
                    else "已记录这项对当前执行有影响的输入，Agent 本轮结束后会处理。"
                )
            )
        elif state_machine.is_human_confirmation(active_project.current_state):
            source_state = _confirmation_source_stage(active_project.current_state)
            target_by_confirmation = {
                "REQUIREMENT_CONFIRM_PENDING": "PLANNING_REQUIREMENTS",
                "ARCHITECTURE_CONFIRM_PENDING": "PLANNING_ARCHITECTURE",
                "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "PLANNING_IMPLEMENTATION_SPECS",
                "BUG_FIX_CONFIRM_PENDING": "BUG_ANALYSIS",
                "OUTLINE_CONFIRM_PENDING": "DOC_OUTLINE",
                "CODING_CONFIRM_PENDING": "CODING",
                "TESTING_CONFIRM_PENDING": "TESTING",
                "REVIEW_CONFIRM_PENDING": "REVIEW",
                "ACCEPTANCE_PENDING": "CODING",
            }
            explicit_stage = str(action.get("target_stage") or "").strip()
            if (
                active_project.current_state == "ACCEPTANCE_PENDING"
                and explicit_stage == "acceptance"
            ):
                # A correction to the evaluator's report/evidence is not an
                # implementation defect. Keep the delivery artifacts intact,
                # expire only the stale acceptance approval, persist the new
                # acceptance fact, and run the evaluator again in place.
                fact_key = hashlib.sha256(
                    f"{conversation.id}:{msg.message_id or msg.text}".encode("utf-8")
                ).hexdigest()[:16]
                ctx.service.create_context_record(
                    active_project.id,
                    record_type="decision",
                    record_key=f"acceptance.evidence_update.{fact_key}",
                    value={
                        "value": msg.text,
                        "applies_to": ["acceptance_evaluator", "ACCEPTANCE_PENDING"],
                    },
                    source_type="user",
                    source_ref=msg.message_id,
                    commit=False,
                )
                ctx.service.session.commit()
                await ctx.service.expire_pending_approvals_for_state(
                    active_project.id, "ACCEPTANCE_PENDING"
                )
                await ctx.orchestrator.tick(active_project.id)
                sent_message_id = await send_effective_reply(
                    "已记录验收证据纠正并重新运行验收评估；不会回退到编码。"
                )
                effective_reply = (
                    "已记录验收证据纠正并重新运行验收评估；不会回退到编码。"
                )
                # This branch intentionally bypasses the generic confirmation
                # correction path below, which creates implementation feedback
                # and rolls the project back to an execution stage.
            else:
                execution_by_stage = {
                    stage.get("id"): stage.get("execution_state")
                    for stage in state_machine.profile.get("stages", [])
                }
                target_state = (
                    execution_by_stage.get(explicit_stage)
                    or target_by_confirmation.get(active_project.current_state)
                    or source_state
                )
                await ctx.service.create_feedback_packet(
                    project_id=active_project.id,
                    source_stage=source_state,
                    source_task_id=None,
                    source_round=1,
                    kind=(
                        "scope_change"
                        if output.get("intent") == "change_scope"
                        else "correction"
                    ),
                    summary=msg.text,
                    blocking=True,
                    recommended_rework_target=explicit_stage or None,
                    user_comment=msg.text,
                    commit=False,
                )
                fact_key = hashlib.sha256(
                    f"{conversation.id}:{msg.message_id or msg.text}".encode("utf-8")
                ).hexdigest()[:16]
                ctx.service.create_context_record(
                    active_project.id,
                    record_type="requirement",
                    record_key=f"user.change.{fact_key}",
                    value={"value": msg.text, "applies_to": [target_state]},
                    source_type="user",
                    source_ref=msg.message_id,
                    commit=False,
                )
                allowed_roots = get_allowed_bind_roots()
                for capability in action.get("capability_refs") or []:
                    if not isinstance(capability, dict):
                        continue
                    source_file = Path(str(capability.get("source_file") or ""))
                    bindings = capability.get("env_bindings")
                    if (
                        not source_file.is_absolute()
                        or not source_file.is_file()
                        or not isinstance(bindings, dict)
                        or not bindings
                        or (
                            allowed_roots
                            and not _path_within_allowed_roots(source_file, allowed_roots)
                        )
                    ):
                        continue
                    normalized_bindings = {
                        str(target): str(source)
                        for target, source in bindings.items()
                    }
                    ctx.service.create_context_record(
                        active_project.id,
                        record_type="capability",
                        record_key=f"runtime.env.{fact_key}.{len(bindings)}",
                        value={
                            "source_file": str(source_file.resolve()),
                            "env_bindings": normalized_bindings,
                            "applies_to": ["coder", "tester"],
                            "affected_trace_ids": [
                                str(value)
                                for value in capability.get("affected_trace_ids") or []
                                if value
                            ],
                        },
                        source_type="user",
                        source_ref=msg.message_id,
                        sensitivity="secret_ref",
                        replace_active=False,
                        commit=False,
                    )
                ctx.service.session.commit()
                await ctx.service.rollback_project(
                    active_project.id,
                    target_state,
                    triggered_by="conversation_coordinator",
                )
                await ctx.orchestrator.tick(active_project.id)
                target_label = state_machine.display_name(target_state)
                sent_message_id = await send_effective_reply(
                    f"已记录你的修改意见并回到{target_label}，项目将继续执行。"
                )
        elif state_machine.is_human_input(active_project.current_state):
            is_workflow_correction = (
                output.get("intent") in {"correct", "change_scope"}
                and bool(str(action.get("target_stage") or "").strip())
                # An exact question target means the user is correcting that
                # answer, not invalidating the whole planning stage.  The
                # stage is routing context only in this case.
                and not bool(str(action.get("question_id") or "").strip())
            )
            if is_workflow_correction:
                explicit_stage = str(action.get("target_stage") or "").strip()
                execution_by_stage = {
                    stage.get("id"): stage.get("execution_state")
                    for stage in state_machine.profile.get("stages", [])
                }
                target_state = execution_by_stage.get(explicit_stage)
                if target_state is None and explicit_stage == "acceptance":
                    target_state = "ACCEPTANCE_PENDING"
                if target_state is None:
                    sent_message_id = await send_effective_reply(
                        "无法把这项纠正路由到有效执行阶段，项目状态未改变。"
                    )
                else:
                    await ctx.service.create_feedback_packet(
                        project_id=active_project.id,
                        source_stage=active_project.current_state,
                        source_task_id=None,
                        source_round=1,
                        kind=(
                            "scope_change"
                            if output.get("intent") == "change_scope"
                            else "correction"
                        ),
                        summary=msg.text,
                        blocking=True,
                        recommended_rework_target=explicit_stage,
                        user_comment=msg.text,
                        commit=False,
                    )
                    fact_key = hashlib.sha256(
                        f"{conversation.id}:{msg.message_id or msg.text}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:16]
                    ctx.service.create_context_record(
                        active_project.id,
                        record_type="requirement",
                        record_key=f"user.change.{fact_key}",
                        value={"value": msg.text, "applies_to": [target_state]},
                        source_type="user",
                        source_ref=msg.message_id,
                        commit=False,
                    )
                    ctx.service.session.commit()
                    # The correction changes the evidence contract. Existing
                    # input questions are stale; expire them without turning
                    # the correction text into an answer to an unrelated card.
                    ctx.service.expire_open_question_interactions(active_project.id)
                    await ctx.service.rollback_project(
                        active_project.id,
                        target_state,
                        triggered_by="conversation_coordinator",
                    )
                    await ctx.orchestrator.tick(active_project.id)
                    target_label = state_machine.display_name(target_state)
                    effective_reply = (
                        f"已记录这项纠正并回到{target_label}继续执行；"
                        "原输入卡已作废，纠正内容没有被当作其答案。"
                    )
                    sent_message_id = await send_effective_reply(effective_reply)
            else:
                allowed_roots = get_allowed_bind_roots()
                fact_key = hashlib.sha256(
                    f"{conversation.id}:{msg.message_id or msg.text}".encode("utf-8")
                ).hexdigest()[:16]
                for capability in action.get("capability_refs") or []:
                    if not isinstance(capability, dict):
                        continue
                    source_file = Path(str(capability.get("source_file") or ""))
                    bindings = capability.get("env_bindings")
                    if (
                        not source_file.is_absolute()
                        or not source_file.is_file()
                        or not isinstance(bindings, dict)
                        or not bindings
                        or (
                            allowed_roots
                            and not _path_within_allowed_roots(source_file, allowed_roots)
                        )
                    ):
                        continue
                    normalized_bindings = {
                        str(target): str(source)
                        for target, source in bindings.items()
                    }
                    ctx.service.create_context_record(
                        active_project.id,
                        record_type="capability",
                        record_key=f"runtime.env.{fact_key}.{len(bindings)}",
                        value={
                            "source_file": str(source_file.resolve()),
                            "env_bindings": normalized_bindings,
                            "applies_to": ["coder", "tester"],
                            "affected_trace_ids": [
                                str(value)
                                for value in capability.get("affected_trace_ids") or []
                                if value
                            ],
                        },
                        source_type="user",
                        source_ref=msg.message_id,
                        sensitivity="secret_ref",
                        replace_active=False,
                        commit=False,
                    )
                effective_reply = await _submit_input(
                    ctx,
                    open_id,
                    msg.text,
                    question_id=str(action.get("question_id") or "") or None,
                    project_id=active_project.id,
                    answer_key=str(action.get("option_key") or "") or None,
                    rework_target=str(action.get("target_stage") or "") or None,
                )
        else:
            sent_message_id = await send_effective_reply(
                "当前没有正在运行的 Agent；我暂未修改项目状态。"
            )
    else:
        sent_message_id = await send_effective_reply(
            reply or "我已经理解你的消息，但暂时没有需要执行的动作。"
        )

    ctx.service.append_conversation_turn(
        conversation.chat_id,
        role="assistant",
        content=effective_reply or f"[{action_type}]",
        message_id=sent_message_id,
    )
    updated_summary = output.get("updated_summary")
    handler_controlled_actions = {
        "query_status",
        "request_clarification",
        "resolve_pending_action",
        "propose_start_delivery",
        "start_delivery",
        "propose_project_control",
        "propose_approval_resolution",
        "resend_approval",
        "route_project_input",
        "view_artifact",
    }
    if action_type in handler_controlled_actions:
        # The deterministic handler result is authoritative. Never let a
        # coordinator-authored prediction overwrite memory after a guard,
        # failed execution, or state-dependent response.
        summary_to_store = effective_reply[:1000]
    elif action_type == "inspect_workspace":
        inspection = output.get("inspection") or {}
        summary_to_store = str(inspection.get("summary") or effective_reply)[:1000]
    else:
        summary_to_store = (
            updated_summary
            if isinstance(updated_summary, str) and updated_summary.strip()
            else effective_reply[:1000]
        )
    latest_artifact = None
    if action_type in {"inspect_workspace", "create_draft", "research"}:
        latest_artifact = {
            "kind": action_type,
            "summary": summary_to_store,
            "message_id": sent_message_id,
        }
        if action_type == "inspect_workspace":
            latest_artifact["evidence"] = output.get("inspection") or {}
    ctx.service.update_conversation_session(
        conversation,
        summary=summary_to_store,
        latest_artifact=latest_artifact,
        active_project_id=active_project.id if active_project else None,
        update_active_project=(
            active_project is not None
            or (
                recent_project is not None
                and recent_project.status == "cancelled"
            )
        ),
    )


def _conversation_reply_needs_card(text: str) -> bool:
    """Return whether a Coordinator reply benefits from Markdown rendering."""
    markdown_markers = ("\n#", "\n- ", "\n1. ", "```", "**", "|---", "\n>")
    return len(text) > 240 or any(marker in text for marker in markdown_markers)


async def _send_conversation_reply(
    ctx: FeishuHandlerContext,
    receive_id: str,
    receive_id_type: str,
    text: str,
    *,
    title: str = "答复",
    message_id: str | None = None,
) -> str:
    """Render a Coordinator reply as one or more content cards.

    Long replies are automatically split into multiple cards so Feishu does
    not truncate them.  When ``message_id`` is the in-progress card, replace
    it with the first result page instead of leaving a generic milestone above
    the answer.
    """
    text = redact_text(text)
    if message_id:
        chunks = split_markdown_sections(text)
        total = len(chunks)
        delivered = await _deliver_conversation_card(
            ctx,
            receive_id,
            receive_id_type,
            build_content_card(title, chunks[0], page=1, total=total),
            message_id=message_id,
        )
        for page, chunk in enumerate(chunks[1:], start=2):
            delivered = await _send_interactive_card(ctx, 
                receive_id,
                build_content_card(title, chunk, page=page, total=total),
                receive_id_type=receive_id_type,
            )
        return delivered
    chunks = split_markdown_sections(text)
    delivered = ""
    for page, chunk in enumerate(chunks, start=1):
        delivered = await _send_interactive_card(
            ctx,
            receive_id,
            build_content_card(title, chunk, page=page, total=len(chunks)),
            receive_id_type=receive_id_type,
        )
    return delivered


async def _deliver_conversation_card(
    ctx: FeishuHandlerContext,
    receive_id: str,
    receive_id_type: str,
    card: dict[str, Any],
    *,
    message_id: str | None = None,
) -> str:
    """Persist and deliver a Coordinator card with content-level deduplication."""
    serialized = json.dumps(card, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(
        f"{ctx.chat_id or receive_id}:{message_id or 'new'}:{serialized}".encode("utf-8")
    ).hexdigest()
    conversation = (
        ctx.service.get_or_create_conversation_session(ctx.chat_id)
        if ctx.chat_id
        else None
    )
    try:
        return await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"conversation-card:{digest}",
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="progress" if message_id else "decision",
            card=card,
            conversation_id=conversation.id if conversation else None,
            update_message_id=message_id,
        )
    except Exception as exc:
        if not message_id:
            raise
        # A transient failure has a durable retryable update intent. Posting a
        # replacement at the same time creates two competing deliveries and
        # can duplicate the card after recovery. Only a permanent update
        # rejection (for example an expired/deleted message) degrades to POST.
        if is_retryable_delivery_error(exc):
            raise
        logger.warning("Failed to update conversation card; posting replacement", exc_info=True)
        return await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"conversation-card:{digest}:replacement",
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="progress",
            card=card,
            conversation_id=conversation.id if conversation else None,
        )


async def _start_delivery_from_payload(
    ctx: FeishuHandlerContext,
    open_id: str,
    payload: dict[str, Any],
    *,
    source_session_id: str,
    source_message_ids: list[str],
) -> Project:
    """Create one delivery run, optionally reusing a terminal project's repo.

    A completed project is immutable workflow history.  Follow-up work gets a
    new Project/run while its workspace is deliberately reused.  This keeps
    approvals, rounds and feedback auditable without copying the repository or
    trying to roll a terminal state machine backwards.
    """
    source_conversation = ctx.service.session.get(
        ConversationSession, source_session_id
    )
    base_project_id = str(payload.get("base_project_id") or "").strip()
    base_project: Project | None = None
    bound_directory: str | None = None
    if base_project_id:
        base_project = ctx.service.get_project(base_project_id)
        membership = ctx.service.session.exec(
            select(ProjectUser).where(
                ProjectUser.project_id == base_project_id,
                ProjectUser.open_id == open_id,
            )
        ).first()
        if (
            base_project is None
            or membership is None
            or base_project.status in {"active", "paused"}
        ):
            raise ValueError("无法复用该项目工作区")
        bound_directory = base_project.workspace_path

    inherited_runtime_grants: list[dict[str, Any]] = []
    if base_project is not None:
        inherited_runtime_grants = [
            dict(grant)
            for grant in (base_project.context or {}).get(
                "runtime_capability_grants", []
            )
            if isinstance(grant, dict)
            and grant.get("capabilities")
            and set(grant.get("capabilities") or []).issubset({"HostAutomation"})
        ]

    confirmed_brief = {
        "objective": payload.get("objective") or payload.get("description") or "",
        "scope": payload.get("scope") or [],
        "out_of_scope": payload.get("out_of_scope") or [],
        "permissions": payload.get("permissions") or [],
        "acceptance_criteria": payload.get("acceptance_criteria") or [],
        "delivery_policy": payload.get("delivery_policy") or {},
        "reference_mode": payload.get("reference_mode") or "none",
        "decisions": payload.get("decisions") or [],
        "source_session_id": source_session_id,
        "source_message_ids": source_message_ids,
        "base_project_id": base_project_id or None,
        "conversation_summary": source_conversation.summary if source_conversation else "",
        "recent_turns": list(source_conversation.recent_turns or [])
        if source_conversation else [],
    }
    return await ctx.service.create_project(
        name=suggest_name(
            payload.get("description") or payload.get("objective") or "新项目"
        ),
        task_type=payload.get("task_type", "feature"),
        description=payload.get("description") or payload.get("objective") or "",
        creator_open_id=open_id,
        feishu_chat_id=ctx.chat_id,
        feishu_chat_type=ctx.chat_type,
        bound_directory=bound_directory,
        initial_context={
            "orchestration_mode": "agent_loop",
            "confirmed_brief": confirmed_brief,
            "base_project_id": base_project_id or None,
            **(
                {"runtime_capability_grants": inherited_runtime_grants}
                if inherited_runtime_grants
                else {}
            ),
        },
    )


async def _execute_pending_conversation_action(
    ctx: FeishuHandlerContext,
    open_id: str,
    action_id: str,
    *,
    decision: str,
) -> None:
    """Confirm/reject a chat-scoped action with idempotent side effects."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    action = ctx.service.session.get(PendingConversationAction, action_id)
    if action is None:
        await _send_card_message(ctx, 
            receive_id, "该执行请求不存在或已经清理。", receive_id_type=receive_id_type
        )
        return
    if action.status in {"resolved", "rejected", "expired"}:
        await _send_card_message(ctx, 
            receive_id, "该操作已经处理，无需重复点击。", receive_id_type=receive_id_type
        )
        return
    if action.created_by and action.created_by != open_id:
        await _send_card_message(ctx, 
            receive_id, "只有发起该执行请求的用户可以确认。", receive_id_type=receive_id_type
        )
        return
    if decision != "confirm":
        ctx.service.resolve_pending_conversation_action(
            action_id, decision="reject", resolved_by=open_id
        )
        await _send_card_message(ctx, 
            receive_id, "已保留当前讨论，不会开始修改。", receive_id_type=receive_id_type
        )
        return

    claimed = ctx.service.claim_pending_conversation_action(
        action_id, claimed_by=open_id
    )
    if claimed is None:
        await _send_card_message(ctx, 
            receive_id, "该操作正在处理或已经处理，请勿重复点击。", receive_id_type=receive_id_type
        )
        return
    if claimed.action_type == "project_control":
        payload = dict(claimed.payload)
        control_action = str(payload.get("control_action") or "").strip()
        target_project_id = str(payload.get("target_project_id") or "").strip()
        target_project_name = str(
            payload.get("target_project_name") or target_project_id
        )
        project = (
            ctx.service.session.get(Project, target_project_id)
            if target_project_id
            else None
        )
        if project is None:
            ctx.service.finish_pending_conversation_action(action_id, status="pending")
            await _send_card_message(ctx, 
                receive_id,
                "目标项目不存在或已被删除。",
                receive_id_type=receive_id_type,
            )
            return
        should_advance = False
        try:
            if control_action == "pause":
                if project.status != "active":
                    raise ValueError("项目当前不是运行状态，无法暂停")
                await ctx.service.pause_project(project.id, triggered_by=open_id)
                result_text = f"已暂停项目“{target_project_name}”。"
            elif control_action == "resume":
                if project.status != "paused":
                    raise ValueError("项目当前不是暂停状态，无法恢复")
                await ctx.service.resume_project(project.id, triggered_by=open_id)
                result_text = f"已恢复项目“{target_project_name}”。"
                should_advance = True
            elif control_action == "cancel":
                if project.status not in {"active", "paused"}:
                    raise ValueError("项目已经结束，无法取消")
                await ctx.orchestrator.cancel_project(
                    project.id, reason="用户通过对话确认卡取消"
                )
                result_text = f"已取消项目“{target_project_name}”。"
            elif control_action == "rollback":
                if project.status != "active":
                    raise ValueError("只有运行中的项目可以回滚")
                target_stage = str(payload.get("target_stage") or "").strip()
                sm = StateMachine(project.task_type)
                rollback_targets = {
                    item["action"].removeprefix("to_"): item["next_state"]
                    for item in sm.rework_options()
                }
                target_state = rollback_targets.get(target_stage)
                if not target_state:
                    ctx.service.finish_pending_conversation_action(
                        action_id, status="pending"
                    )
                    await _send_card_message(ctx, 
                        receive_id,
                        "未知的回滚目标阶段。",
                        receive_id_type=receive_id_type,
                    )
                    return
                if not _is_valid_rollback_target(sm, project.current_state, target_state):
                    ctx.service.finish_pending_conversation_action(
                        action_id, status="pending"
                    )
                    await _send_card_message(ctx, 
                        receive_id,
                        "回滚目标必须早于项目当前阶段，请重新发起操作。",
                        receive_id_type=receive_id_type,
                    )
                    return
                await ctx.service.rollback_project(
                    project.id, target_state, triggered_by=open_id
                )
                result_text = (
                    f"已将项目“{target_project_name}”回滚到"
                    f"{sm.display_name(target_state)}。"
                )
                should_advance = True
            else:
                ctx.service.finish_pending_conversation_action(
                    action_id, status="pending"
                )
                await _send_card_message(ctx, 
                    receive_id,
                    "暂不支持该项目操作。",
                    receive_id_type=receive_id_type,
                )
                return
        except Exception as exc:
            ctx.service.finish_pending_conversation_action(action_id, status="pending")
            logger.exception("Failed to execute project control action")
            await _send_card_message(ctx, 
                receive_id,
                f"操作失败：{exc}",
                receive_id_type=receive_id_type,
            )
            return
        ctx.service.finish_pending_conversation_action(
            action_id,
            status="resolved",
            # Cancellation deliberately clears the conversation's active
            # project. Do not reattach the just-cancelled project while
            # finalizing its confirmation action.
            target_project_id=None if control_action == "cancel" else project.id,
        )
        await _send_card_message(ctx, 
            receive_id, result_text, receive_id_type=receive_id_type
        )
        if should_advance:
            try:
                await _advance_until_confirm(
                    ctx, project, receive_id, receive_id_type
                )
            except Exception as exc:
                logger.exception(
                    "Failed to advance project %s after %s", project.id, control_action
                )
                await _send_card_message(ctx, 
                    receive_id,
                    f"项目状态已更新，但自动继续失败：{exc}",
                    receive_id_type=receive_id_type,
                )
        return

    if claimed.action_type != "start_delivery":
        ctx.service.finish_pending_conversation_action(action_id, status="pending")
        await _send_card_message(ctx, 
            receive_id, "暂不支持该操作类型。", receive_id_type=receive_id_type
        )
        return

    payload = dict(claimed.payload)
    try:
        project = await _start_delivery_from_payload(
            ctx,
            open_id,
            payload,
            source_session_id=claimed.session_id,
            source_message_ids=claimed.source_message_ids or [],
        )
    except Exception as exc:
        ctx.service.finish_pending_conversation_action(action_id, status="pending")
        logger.exception("Failed to create project from pending conversation action")
        await _send_card_message(ctx, 
            receive_id, f"未能开始实施：{exc}", receive_id_type=receive_id_type
        )
        return

    payload["created_project_id"] = project.id
    ctx.service.finish_pending_conversation_action(
        action_id,
        status="resolved",
        target_project_id=project.id,
        payload=payload,
    )
    await _send_card_message(ctx, 
        receive_id,
        f"已创建项目“{project.name or '未命名'}”，当前正在准备第一个交付阶段。",
        receive_id_type=receive_id_type,
    )
    ctx.orchestrator._schedule_followup_tick(project.id)


async def _resume_after_question_interaction(
    ctx: FeishuHandlerContext,
    project: Project,
    interaction,
    responded_by: str,
) -> None:
    """Resume the owning execution model after a durable question answer."""
    if project.context.get("orchestration_mode") != "agent_loop":
        await ctx.service.transition_state(
            project.id,
            "answer",
            payload={"resume_state": interaction.resume_state},
            triggered_by=responded_by,
        )
        return

    answers: list[dict[str, Any]] = []
    for question in ctx.service.get_questions_for_interaction(interaction.id):
        answer = ctx.service.get_question_answer(question.id)
        if answer is None:
            continue
        answers.append(
            {
                "question_id": question.question_id,
                "question": question.question,
                "answer": answer.answer,
                "answer_label": answer.answer_label,
                "skipped": answer.skipped,
            }
        )
    ctx.service.set_execution_status(project.id, "coordinating")
    trigger = (
        "The user answered your pending questions. Continue from these durable answers:\n"
        + json.dumps(answers, ensure_ascii=False)
    )
    if hasattr(ctx.orchestrator, "run_coordinator"):
        await ctx.orchestrator.run_coordinator(project.id, trigger)
    else:
        await ctx.orchestrator.tick(project.id)


async def _handle_question_card_reply(
    ctx: FeishuHandlerContext, msg: TextMessage
) -> bool:
    """Consume a free-text reply to a pending question-batch card."""
    approvals = ctx.service.session.exec(
        select(Approval).where(Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    ).all()
    approval = next(
        (
            item for item in approvals
            if msg.parent_id in item.payload.get("reply_message_ids", [])
        ),
        None,
    )
    if approval is None:
        return False

    interaction_id = approval.payload.get("question_interaction_id")
    interaction = (
        ctx.service.get_question_interaction(interaction_id)
        if interaction_id else None
    )
    project = ctx.service.get_project(approval.project_id)
    if interaction is None or interaction.status != "pending" or project is None:
        return False

    bindings = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.project_id == project.id)
    ).all()
    if bindings and not _is_authorized_for_project(ctx, msg.open_id, project.id):
        receive_id, receive_id_type = _reply_target(ctx, msg.open_id)
        await _send_card_message(ctx, 
            receive_id,
            f"你没有权限回答项目 {project.id} 的问题。",
            receive_id_type=receive_id_type,
        )
        return True

    question_id = approval.payload.get("reply_message_to_question", {}).get(msg.parent_id)
    if question_id:
        question = next(
            (
                q for q in ctx.service.get_questions_for_interaction(interaction.id)
                if q.question_id == question_id and q.status == "pending" and q.allow_free_text
            ),
            None,
        )
    else:
        # Legacy fallback: reply to the batch card itself -> first pending free-text question.
        question = next(
            (
                q for q in ctx.service.get_questions_for_interaction(interaction.id)
                if q.status == "pending" and q.allow_free_text
            ),
            None,
        )
    if question is None:
        return False

    await ctx.service.submit_question_answer(
        question.id,
        msg.text,
        msg.text,
        False,
        msg.open_id,
        commit=False,
        keep_pending=False,
    )
    receive_id, receive_id_type = _reply_target(ctx, msg.open_id)

    # Update the replied input card to a read-only "已回答" receipt state.
    if msg.parent_id:
        answered_card = build_input_card(
            title=f"[{project.name or project.id}] [{interaction.stage or interaction.agent}] {question.question}",
            question=question.question,
            context=question.reason or "",
            approval_id=approval.id,
            state=approval.state,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            kind=question.kind,
            question_id=question.question_id,
            interaction_id=interaction.id,
            answered=True,
            answer_text=msg.text,
        )
        try:
            await _update_interactive_card(ctx, msg.parent_id, answered_card)
        except Exception:
            logger.exception("Failed to update input card to answered state")

    if ctx.service.interaction_is_resolvable(interaction.id):
        await ctx.service.resolve_question_interaction(interaction.id, commit=False)
        await ctx.service.respond_approval(
            approval.id, "answer", msg.open_id, commit=False
        )
        ctx.service.session.commit()
        await _update_completed_question_choice_card(
            ctx, approval, interaction, project
        )
        await _resume_after_question_interaction(ctx, project, interaction, msg.open_id)
        await _send_card_message(ctx, 
            receive_id,
            "已收到你的回答，Agent 继续执行。",
            receive_id_type=receive_id_type,
        )
        if project.context.get("orchestration_mode") != "agent_loop":
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        ctx.service.session.commit()
        # Send the next pending free-text question card, if any.
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await _send_card_message(ctx, 
                receive_id,
                "已记录回答，还有其他阻塞问题待回答。",
                receive_id_type=receive_id_type,
            )
            await ctx.orchestrator.tick(project.id)
    return True


async def _send_next_free_text_card(
    ctx: FeishuHandlerContext, approval: Approval, interaction, project: Project,
    receive_id: str, receive_id_type: str,
) -> bool:
    """Send the next pending free-text question card for an interaction.

    Returns True if a card was sent, False if no pending free-text question
    remains.
    """
    questions = ctx.service.get_questions_for_interaction(interaction.id)
    custom_question_ids = set(
        approval.payload.get("custom_answer_question_ids", [])
    )
    next_q = next(
        (
            q for q in questions
            if q.status == "pending"
            and q.allow_free_text
            and (not q.options or q.question_id in custom_question_ids)
        ),
        None,
    )
    if next_q is None:
        return False

    title = f"[{project.name or project.id}] [{interaction.stage or interaction.agent}] {next_q.question}"
    card = build_input_card(
        title=title,
        question=next_q.question,
        context=next_q.reason or "",
        approval_id=approval.id,
        state=approval.state,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
        kind=next_q.kind,
        question_id=next_q.question_id,
        interaction_id=interaction.id,
    )
    message_id = await _send_interactive_card(ctx, 
        receive_id, card, receive_id_type=receive_id_type,
    )
    if message_id:
        payload = dict(approval.payload)
        reply_message_ids = list(payload.get("reply_message_ids", []))
        reply_message_to_question = dict(payload.get("reply_message_to_question", {}))
        reply_message_ids.append(message_id)
        reply_message_to_question[message_id] = next_q.question_id
        approval.payload = {
            **payload,
            "reply_message_ids": reply_message_ids[-20:],
            "reply_message_to_question": reply_message_to_question,
        }
        ctx.service.session.commit()
    return True


async def _update_completed_question_choice_card(
    ctx: FeishuHandlerContext,
    approval: Approval,
    interaction,
    project: Project,
) -> None:
    """Turn the original choice card into a final read-only receipt."""
    message_id = approval.payload.get("question_batch_message_id")
    if not message_id:
        return
    choice_questions = [
        q for q in ctx.service.get_questions_for_interaction(interaction.id)
        if q.options
    ]
    if not choice_questions:
        return
    submitted_answers: dict[str, Any] = {}
    for question in choice_questions:
        answer_record = ctx.service.get_question_answer(question.id)
        if answer_record and not answer_record.skipped and answer_record.answer:
            submitted_answers[question.question_id] = answer_record.answer
    if not submitted_answers:
        return
    card = build_question_batch_card(
        approval_id=approval.id,
        interaction_id=interaction.id,
        questions=[q.model_dump() for q in choice_questions],
        agent=interaction.agent,
        stage=interaction.stage,
        state=approval.state,
        project_name=project.name or project.id,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
        submitted_answers=submitted_answers,
    )
    try:
        await _update_interactive_card(ctx, message_id, card)
    except Exception:
        logger.exception("Failed to finalize question batch receipt card")


async def _handle_skip_free_text(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> dict[str, Any] | None:
    """Skip a non_blocking free-text question and send the next one."""
    open_id = action.open_id
    approval_id = action.approval_id
    interaction_id = action.extras.get("interaction_id") if action.extras else None
    target_question_id = action.extras.get("question_id") if action.extras else None
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        await _send_card_message(ctx, receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type)
        return None
    if interaction_id is None:
        interaction_id = approval.payload.get("question_interaction_id")
    if not interaction_id or not target_question_id:
        await _send_card_message(ctx, receive_id, "未找到对应的问题交互", receive_id_type=receive_id_type)
        return None
    interaction = ctx.service.get_question_interaction(interaction_id)
    if interaction is None or interaction.status != "pending":
        await _send_card_message(ctx, receive_id, "该问题交互已处理或不存在", receive_id_type=receive_id_type)
        return None
    project = ctx.service.get_project(interaction.project_id)
    if project is None:
        await _send_card_message(ctx, receive_id, "关联项目不存在", receive_id_type=receive_id_type)
        return None
    if not _is_authorized_for_project(ctx, open_id, project.id):
        await _send_card_message(ctx, 
            receive_id,
            f"你没有权限回答项目 {project.id} 的问题。",
            receive_id_type=receive_id_type,
        )
        return None

    questions = ctx.service.get_questions_for_interaction(interaction_id)
    target = next((q for q in questions if q.question_id == target_question_id and q.allow_free_text), None)
    if target is None:
        await _send_card_message(ctx, receive_id, "未找到对应问题", receive_id_type=receive_id_type)
        return None
    if target.kind != "non_blocking":
        await _send_card_message(ctx, receive_id, "该问题不能跳过", receive_id_type=receive_id_type)
        return None

    await ctx.service.submit_question_answer(target.id, None, None, True, open_id, commit=False)
    ctx.service.session.commit()

    # Update the skipped card to a read-only "已跳过" receipt state.
    if action.message_id:
        skipped_card = build_input_card(
            title=f"[{project.name or project.id}] [{interaction.stage or interaction.agent}] {target.question}",
            question=target.question,
            context=target.reason or "",
            approval_id=approval.id,
            state=approval.state,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            kind=target.kind,
            question_id=target.question_id,
            interaction_id=interaction.id,
            skipped=True,
        )
        try:
            await _update_interactive_card(ctx, action.message_id, skipped_card)
        except Exception:
            logger.exception("Failed to update input card to skipped state")

    if ctx.service.interaction_is_resolvable(interaction_id):
        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
        await ctx.service.respond_approval(approval_id, "answer", open_id, commit=False)
        ctx.service.session.commit()
        await _resume_after_question_interaction(ctx, project, interaction, open_id)
        await _send_card_message(ctx, receive_id, "已收到你的回答，Agent 继续执行。", receive_id_type=receive_id_type)
        project = ctx.service.get_project(interaction.project_id)
        if project is not None and project.context.get("orchestration_mode") != "agent_loop":
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await _send_card_message(ctx, 
                receive_id,
                "已跳过，还有其他阻塞问题待回答。",
                receive_id_type=receive_id_type,
            )
            await ctx.orchestrator.tick(project.id)
    return None


async def _prompt_for_create_description(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Enter the interactive project-creation flow and ask for a description."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    ctx.service.upsert_pending_intent(
        open_id,
        description=None,
        stage="awaiting_description",
        chat_id=ctx.chat_id,
        chat_type=ctx.chat_type,
    )
    await _send_interactive_card(ctx, 
        receive_id,
        build_create_prompt_card(chat_id=ctx.chat_id, chat_type=ctx.chat_type),
        receive_id_type=receive_id_type,
    )


async def _cancel_project_command(
    ctx: FeishuHandlerContext, open_id: str, rest: str
) -> None:
    """Handle /cancel [project_id]. If no id is given, cancel the user's latest active project."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project_id = rest.strip()

    if not project_id:
        # Find the most recent active project bound to this user.
        user_projects = ctx.service.session.exec(
            select(ProjectUser).where(ProjectUser.open_id == open_id)
        ).all()
        project_ids = {up.project_id for up in user_projects}
        if not project_ids:
            await _send_card_message(ctx, 
                receive_id, "你没有参与任何项目。", receive_id_type=receive_id_type
            )
            return
        latest = ctx.service.session.exec(
            select(Project)
            .where(Project.id.in_(project_ids))
            .where(Project.status == "active")
            .order_by(Project.updated_at.desc())
        ).first()
        if latest is None:
            await _send_card_message(ctx, 
                receive_id, "没有可终止的活跃项目。", receive_id_type=receive_id_type
            )
            return
        project_id = latest.id

    if not _is_authorized_for_project(ctx, open_id, project_id):
        await _send_card_message(ctx, 
            receive_id,
            f"你没有权限终止项目 {project_id}。",
            receive_id_type=receive_id_type,
        )
        return

    try:
        await ctx.orchestrator.cancel_project(project_id, reason="用户通过飞书命令终止")
    except ProjectNotFoundError:
        await _send_card_message(ctx, 
            receive_id, f"项目 {project_id} 不存在。", receive_id_type=receive_id_type
        )
        return
    except Exception as exc:
        logger.exception("Failed to cancel project %s from Feishu", project_id)
        await _send_card_message(ctx, 
            receive_id,
            f"终止项目 {project_id} 失败：{exc}",
            receive_id_type=receive_id_type,
        )
        return

    await _send_card_message(ctx, 
        receive_id,
        f"项目 {project_id} 已终止。",
        receive_id_type=receive_id_type,
    )


async def _confirm_approval_command(
    ctx: FeishuHandlerContext,
    open_id: str,
    approval_id: str,
    option_key: str | None,
) -> None:
    """Handle /confirm <approval_id> [option_key]."""
    chosen = option_key if option_key else "confirm"
    await _respond_to_approval(ctx, open_id, approval_id, chosen)


async def _reject_approval_command(
    ctx: FeishuHandlerContext,
    open_id: str,
    approval_id: str,
    feedback: str | None,
) -> None:
    """Handle /reject <approval_id> [feedback]."""
    await _respond_to_approval(ctx, open_id, approval_id, "reject", feedback_text=feedback)


async def _respond_to_approval(
    ctx: FeishuHandlerContext,
    open_id: str,
    approval_id: str,
    chosen: str,
    feedback_text: str | None = None,
    extra: dict[str, Any] | None = None,
    skip_rejection_feedback: bool = False,
    source_message_id: str | None = None,
) -> None:
    """Shared logic for card actions and text commands responding to an approval."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        await _send_card_message(ctx, 
            receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type
        )
        return

    if approval.status != "pending":
        await _send_card_message(ctx, 
            receive_id,
            "该审批已经处理或过期，无需重复点击。",
            receive_id_type=receive_id_type,
        )
        return
    project = ctx.service.get_project(approval.project_id)
    if project is None:
        await _send_card_message(ctx, 
            receive_id, "项目不存在", receive_id_type=receive_id_type
        )
        return

    # Authorization: the user must be bound to the project, unless the project
    # has no bindings yet (e.g. API-created test projects).
    bindings = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.project_id == project.id)
    ).all()
    if bindings and not _is_authorized_for_project(ctx, open_id, project.id):
        await _send_card_message(ctx, 
            receive_id,
            f"你没有权限操作项目 {project.id} 的审批。",
            receive_id_type=receive_id_type,
        )
        return

    valid_options = [
        option
        for option in approval.payload.get("options", [])
        if isinstance(option, dict) and option.get("action")
    ]
    valid_actions = {str(option["action"]) for option in valid_options}
    if approval.approval_type == "input":
        valid_actions.update({"answer", "answered"})
    if chosen not in valid_actions:
        labels = "、".join(
            f"{option.get('label') or option['action']}（{option['action']}）"
            for option in valid_options
        )
        await _send_card_message(ctx, 
            receive_id,
            f"这个操作不适用于当前卡片。可选操作：{labels or '请使用当前卡片上的按钮'}。",
            receive_id_type=receive_id_type,
        )
        return

    if (
        chosen == "reject"
        and not feedback_text
        and not skip_rejection_feedback
        and any(
            stage.get("kind") == "planning"
            and stage.get("confirmation", {}).get("state") == project.current_state
            for stage in StateMachine(project.task_type).profile.get("stages", [])
        )
    ):
        await _send_interactive_card(ctx, 
            receive_id,
            build_rejection_input_card(
                project_name=project.name or project.id,
                approval_id=approval.id,
                state=project.current_state,
                chat_id=project.feishu_chat_id or ctx.chat_id,
                chat_type=project.feishu_chat_type or ctx.chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    # Input cards use a button that just signals the user has typed a reply.
    if chosen == "answered":
        await _send_card_message(ctx, 
            receive_id,
            "请直接回复这张问题卡，或在输入卡中填写答案。无需输入审批 ID。",
            receive_id_type=receive_id_type,
        )
        return

    # Restart with a different task type needs a secondary type-selection card.
    if chosen == "restart_with_type":
        await _send_interactive_card(ctx, 
            receive_id,
            build_restart_type_card(
                project.id,
                project.description or "",
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    # Map the chosen action back to its human-readable label for feedback.
    selected_label = chosen
    for opt in approval.payload.get("options", []):
        if extra and opt.get("option_key") == extra.get("option_key"):
            selected_label = opt.get("label", chosen)
            break
        if opt.get("action") == chosen or opt.get("option_key") == chosen:
            selected_label = opt.get("label", chosen)
            break

    is_rejection = chosen == "reject"
    action_note = {
        "retry": "已重新排队执行。",
        "choose_rework": "接下来请选择要回退的阶段。",
        "cancel": "项目已终止。",
    }.get(chosen, "项目继续执行。")
    if is_rejection and feedback_text:
        action_note = "修改意见已记录，Agent 将据此重做。"
    elif is_rejection and skip_rejection_feedback:
        action_note = "已跳过修改说明，项目将按原有上下文回退。"

    # Confirm the user's choice with a card before continuing.
    feedback_card = build_feedback_card(
        project_name=project.name or project.id,
        selected_label=selected_label,
        is_rejection=is_rejection,
        note=action_note,
    )
    await _send_interactive_card(ctx, 
        receive_id, feedback_card, receive_id_type=receive_id_type
    )

    from_state = project.current_state

    # Preserve feedback both for a direct rejection and for an option-based
    # REWORK choice.  The latter is how the Coordinator routes a natural-
    # language defect report to a concrete upstream stage; dropping the text
    # here leaves Coder/Tester with only the old failure packet.
    is_rework_feedback = approval.state == "REWORK" and bool(feedback_text)
    if (is_rejection or is_rework_feedback) and feedback_text:
        source_stage = _confirmation_source_stage(from_state)
        feedback_kind = "human_rejection" if is_rejection else "human_rework"
        await ctx.service.create_feedback_packet(
            project_id=project.id,
            source_stage=source_stage,
            source_task_id=approval.task_id,
            source_round=approval.payload.get("round_number", 1),
            kind=feedback_kind,
            summary=feedback_text,
            blocking=True,
            failure_class=feedback_kind,
            user_comment=feedback_text,
            commit=False,
        )

    transition_payload = None
    if chosen == "select_option":
        decision = {
            key: (extra or {}).get(key)
            for key in ("decision_id", "option_key")
            if (extra or {}).get(key)
        }
        transition_payload = {"resolved_decisions": [decision]} if decision else None

    await ctx.service.resolve_approval(
        approval.id,
        chosen,
        responded_by=open_id,
        payload=transition_payload,
    )
    ctx.service.session.refresh(approval)
    project = ctx.service.require_project(project.id)
    await _update_resolved_approval_card(
        ctx,
        approval=approval,
        project=project,
        source_state=from_state,
        chosen=chosen,
        selected_label=selected_label,
        resolved_by=open_id,
        source_message_id=source_message_id,
        resolution_reason=(
            feedback_text
            or ("用户选择跳过修改说明" if skip_rejection_feedback else "用户已通过飞书完成选择")
        ),
    )

    # Retry the expensive black-box executor only when its own structured
    # evidence was incomplete/failed. If every executor criterion passed and
    # only the later zero-tool evaluator rejected the evidence chain, preserve
    # that fresh browser run and rerun the evaluator. This distinction is based
    # on stage ownership and structured verdicts, never failure-message text.
    if chosen == "retry" and from_state == "ACCEPTANCE_PENDING":
        project = ctx.service.require_project(project.id)
        latest_executor = ctx.service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_agent",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        latest_evaluator = ctx.service.session.exec(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.agent_type == "acceptance_evaluator",
                Task.status == "completed",
            )
            .order_by(Task.updated_at.desc())
        ).first()
        executor_output = (
            latest_executor.output_data
            if latest_executor is not None and isinstance(latest_executor.output_data, dict)
            else {}
        )
        evaluator_output = (
            latest_evaluator.output_data
            if latest_evaluator is not None and isinstance(latest_evaluator.output_data, dict)
            else {}
        )
        evaluator_ran_after_executor = bool(
            latest_evaluator is not None
            and latest_executor is not None
            and latest_evaluator.updated_at >= latest_executor.updated_at
        )
        if _acceptance_retry_needs_executor_rerun(
            executor_output,
            evaluator_output,
            evaluator_ran_after_executor=evaluator_ran_after_executor,
        ):
            project.context = {
                **project.context,
                "acceptance_retry_generation": int(
                    project.context.get("acceptance_retry_generation", 0) or 0
                ) + 1,
            }
            ctx.service.session.commit()
        else:
            logger.info(
                "Acceptance retry for %s reuses fully passing executor %s and reruns evaluator only",
                project.id,
                latest_executor.id,
            )

    # Log the decision so we can trace "卡住" vs "agent 正在跑".
    project = ctx.service.require_project(project.id)
    logger.info(
        "Approval %s → %s (chosen=%s) — project %s now at %s",
        approval_id, approval.state, chosen, project.id, project.current_state,
    )

    if is_rejection:
        # Remember which confirmation state was rejected.
        project = ctx.service.require_project(project.id)
        project.context = {
            **project.context,
            "rejected_confirmation_state": from_state,
        }
        ctx.service.session.commit()

        if feedback_text:
            updated_context = dict(project.context)
            updated_context["latest_rejection_feedback"] = feedback_text
            updated_context.pop("rejected_confirmation_state", None)
            project.context = updated_context
            ctx.service.session.commit()
            sm = StateMachine(project.task_type)
            state_display = sm.display_name(from_state)
            rounds = project.context.get("confirmation_rounds", {})
            round_number = rounds.get(from_state, 0) + 1
            summary_card = build_rejection_summary_card(
                project_name=project.name or project.id,
                state_display=state_display,
                feedback=feedback_text,
                round_number=round_number,
            )
            await _send_interactive_card(ctx, 
                receive_id, summary_card, receive_id_type=receive_id_type
            )
            logger.info(
                "Entering _advance_until_confirm for %s after reject → %s",
                project.id, project.current_state,
            )
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
            return

        # No feedback: advance the state machine so the agent reruns.
        # Without this, resolve_approval already transitioned to an
        # agent_execution state but no tick is scheduled, leaving the
        # project stuck until manual intervention.
        project = ctx.service.require_project(project.id)
        await _send_card_message(ctx, 
            receive_id,
            "已驳回，未提供修改意见。Agent 将根据已有上下文重做；如需补充意见请立即回复。",
            receive_id_type=receive_id_type,
        )
        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
        return

    # For confirmation, show a transition card before continuing to the next stage.
    project = ctx.service.require_project(project.id)
    sm = StateMachine(project.task_type)
    round_number = approval.payload.get("round_number", 1)
    transition_card = build_transition_card(
        project_name=project.name or project.id,
        from_state=from_state,
        to_state=project.current_state,
        from_state_type="human_confirmation",
        to_state_type=sm.state_type(project.current_state),
        from_agent="",
        to_agent=ctx.orchestrator._agent_label_for_state(project.current_state),
        round_number=round_number,
    )
    await _send_interactive_card(ctx, 
        receive_id, transition_card, receive_id_type=receive_id_type
    )

    logger.info(
        "Entering _advance_until_confirm for %s after confirm → %s",
        project.id, project.current_state,
    )
    # Tick repeatedly so cards are sent for every confirmation stage.
    await _advance_until_confirm(ctx, project, receive_id, receive_id_type)


async def _update_resolved_approval_card(
    ctx: FeishuHandlerContext,
    *,
    approval: Approval,
    project: Project,
    source_state: str,
    chosen: str,
    selected_label: str,
    resolved_by: str,
    source_message_id: str | None,
    resolution_reason: str,
) -> str | None:
    """Make the original decision card converge to the resolved Approval."""
    notification = dict((approval.payload or {}).get("notification") or {})
    message_id = (
        notification.get("confirmation_card_message_id") or source_message_id
    )
    state_machine = StateMachine(project.task_type)
    options = [
        item
        for item in (approval.payload or {}).get("options", [])
        if isinstance(item, dict) and item.get("action")
    ]
    if not any(str(item.get("action")) == str(chosen) for item in options):
        options.append({"action": chosen, "label": selected_label})
    card = build_confirmation_card(
        title=f"[{project.name or project.id}] {approval.question or '决策结果'}",
        summary="该决策已经完成，卡片状态已与后台同步",
        details={"决策阶段": state_machine.display_name(source_state)},
        options=options,
        approval_id=approval.id,
        state=source_state,
        chat_id=project.feishu_chat_id or ctx.chat_id,
        chat_type=project.feishu_chat_type or ctx.chat_type,
        show_actions=False,
        selected_action=chosen,
        resolved_by="用户" if resolved_by and resolved_by != "system_auto" else "Coordinator / 系统策略",
        resolved_at=(
            approval.responded_at.isoformat(timespec="seconds")
            if approval.responded_at
            else datetime.utcnow().isoformat(timespec="seconds")
        ),
        resulting_state=state_machine.display_name(project.current_state),
        resolution_reason=resolution_reason,
    )
    receive_id, receive_id_type = _reply_target(ctx, resolved_by)
    updated_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=(
            f"project:{project.id}:approval:{approval.id}:resolved:"
            f"{chosen}:{project.context_version}"
        ),
        recipient_id=receive_id,
        recipient_type=receive_id_type,
        intent_type="decision",
        card=card,
        project_id=project.id,
        update_message_id=message_id,
        projection_key=f"approval:{approval.id}",
        subject_type="approval",
        subject_id=approval.id,
        lifecycle_status="resolved_by_user",
        selected_action=chosen,
        resolved_by=resolved_by,
        resulting_state=project.current_state,
    )
    notification.update({
        "confirmation_card_message_id": updated_id or message_id,
        "card_status": "resolved",
        "selected_action": chosen,
        "resolved_by": resolved_by,
        "resulting_state": project.current_state,
    })
    approval.payload = {
        **(approval.payload or {}),
        "notification": notification,
    }
    ctx.service.session.add(approval)
    ctx.service.session.commit()
    return updated_id


async def _list_my_projects(ctx: FeishuHandlerContext, open_id: str) -> None:
    """List projects bound to the user."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if not project_ids:
        await _send_card_message(ctx, 
            receive_id, "你还没有任何项目。", receive_id_type=receive_id_type
        )
        return
    projects = ctx.service.session.exec(
        select(Project).where(Project.id.in_(project_ids)).order_by(Project.updated_at.desc())
    ).all()
    lines = [f"你的项目（共 {len(projects)} 个）："]
    for p in projects:
        lines.append(f"- {p.id} {p.name or '(未命名)'} [{p.status}] {p.current_state}")
    await _send_card_message(ctx, 
        receive_id, "\n".join(lines), receive_id_type=receive_id_type
    )


async def _show_help(ctx: FeishuHandlerContext, open_id: str) -> None:
    """Send a concise command help message."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    help_text = (
        "可用命令：\n"
        "/create <描述>  — 创建新项目\n"
        "/create          — 进入交互式创建\n"
        "/confirm <审批ID> [选项key] — 确认审批（等同于点击确认/选择选项）\n"
        "/reject <审批ID> [反馈]    — 驳回审批并可选附带反馈\n"
        "/input <审批ID> <回答>     — 提交澄清/输入类审批的答案\n"
        "/cancel [项目ID] — 终止项目（省略 ID 则终止最近活跃项目）\n"
        "/list            — 列出我的项目\n"
        "/projects        — 列出当前会话绑定的项目\n"
        "/switch <项目ID> - 切换当前会话默认项目\n"
        "/switch          - 推项目选择卡\n"
        "/current         - 查看当前会话绑定目录与默认项目\n"
        "/leave-project   - 清除当前会话默认项目\n"
        "/bind <绝对路径> - 绑定当前会话到代码目录（仅在没有活跃/暂停项目时可切换）\n"
        "/bind            - 推目录候选卡\n"
        "/unbind          - 解绑目录（仅在没有活跃/暂停项目时可解绑）\n"
        "/status <项目ID> - 查看项目状态\n"
        "/menu            — 打开菜单\n"
        "/help            — 显示本帮助\n\n"
        "你也可以直接用自然语言描述新项目、已有项目修改或缺陷；"
        "以上命令只是快捷方式，不是必需操作。"
    )
    await _send_card_message(ctx, receive_id, help_text, receive_id_type=receive_id_type)


def _blocking_projects_for_directory_change(
    ctx: FeishuHandlerContext, chat_id: str
) -> list[Project]:
    """Return chat-bound projects that block a directory bind/unbind.

    Only `active` and `paused` projects block. `completed`/`failed`/`cancelled`
    projects do not block because their `bound_directory` snapshot is frozen.
    """
    projects = ctx.service.list_projects_for_chat(chat_id)
    return [p for p in projects if p.status in ("active", "paused")]


async def _bind_directory(
    ctx: FeishuHandlerContext, open_id: str, directory: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await _send_card_message(ctx, 
            receive_id, "当前消息缺少会话标识，无法绑定目录。",
            receive_id_type=receive_id_type,
        )
        return
    blockers = _blocking_projects_for_directory_change(ctx, ctx.chat_id)
    if blockers:
        lines = ["当前会话存在活跃或暂停项目，不能切换目录："]
        for p in blockers:
            lines.append(f"- {p.id} {p.name or ''} [{p.status}] {p.current_state}")
        lines.append("\n请先 `/cancel` 这些项目或等其进入终态后再切换目录。")
        await _send_card_message(ctx, 
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
        )
        return
    try:
        binding = ctx.service.bind_directory_to_chat(
            ctx.chat_id, directory, bound_by=open_id
        )
    except (ValueError, OSError) as exc:
        await _send_card_message(ctx, 
            receive_id, f"绑定目录失败：{exc}", receive_id_type=receive_id_type
        )
        return
    await _send_card_message(ctx, 
        receive_id,
        f"已将当前会话绑定到 `{binding.directory}`。后续新项目会直接修改该仓库；已有项目不受影响。",
        receive_id_type=receive_id_type,
    )


async def _unbind_directory(ctx: FeishuHandlerContext, open_id: str) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await _send_card_message(ctx, 
            receive_id, "当前消息缺少会话标识，无法解绑目录。",
            receive_id_type=receive_id_type,
        )
        return
    blockers = _blocking_projects_for_directory_change(ctx, ctx.chat_id)
    if blockers:
        lines = ["当前会话存在活跃或暂停项目，不能解绑目录："]
        for p in blockers:
            lines.append(f"- {p.id} {p.name or ''} [{p.status}] {p.current_state}")
        lines.append("\n请先 `/cancel` 这些项目或等其进入终态后再解绑目录。")
        await _send_card_message(ctx, 
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
        )
        return
    removed = ctx.service.unbind_directory_from_chat(ctx.chat_id)
    message = (
        "已解绑当前会话目录；后续新项目将使用系统工作区，已有项目不受影响。"
        if removed else "当前会话没有绑定目录。"
    )
    await _send_card_message(ctx, 
        receive_id, message, receive_id_type=receive_id_type
    )


async def _show_bind_candidate_card(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Push a candidate-directory selection card for `/bind` (no args).

    Uses the deterministic scanner for repository discovery. `/bind` does not
    require semantic judgement, and putting an LLM in this control path causes
    avoidable latency while granting it broader filesystem visibility than the
    task needs. Handler path validation still runs before binding.
    """
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await _send_card_message(ctx, 
            receive_id, "当前消息缺少会话标识，无法绑定目录。",
            receive_id_type=receive_id_type,
        )
        return
    blockers = _blocking_projects_for_directory_change(ctx, ctx.chat_id)
    if blockers:
        lines = ["当前会话存在活跃或暂停项目，不能切换目录："]
        for p in blockers:
            lines.append(f"- {p.id} {p.name or ''} [{p.status}] {p.current_state}")
        lines.append("\n请先 `/cancel` 这些项目或等其进入终态后再切换目录。")
        await _send_card_message(ctx, 
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
        )
        return

    from mac_system.config import get_allowed_bind_roots

    allowed_roots = get_allowed_bind_roots()
    if not allowed_roots:
        await _send_card_message(ctx, 
            receive_id,
            "未配置 `MAC_ALLOWED_BIND_ROOTS`，请使用 `/bind <绝对路径>` 显式指定目录。",
            receive_id_type=receive_id_type,
        )
        return

    candidates = await _collect_bind_dir_candidates(ctx, allowed_roots)
    await _send_interactive_card(ctx, 
        receive_id,
        build_directory_candidate_card(
            candidates, chat_id=ctx.chat_id, chat_type=ctx.chat_type
        ),
        receive_id_type=receive_id_type,
    )


async def _collect_bind_dir_candidates(
    ctx: FeishuHandlerContext, allowed_roots: list[Path]
) -> list[dict[str, str]]:
    """Return validated repositories without invoking an Agent."""
    return ProjectService.scan_candidate_repositories(allowed_roots)


def _path_within_allowed_roots(path: Path, allowed_roots: list[Path]) -> bool:
    """Return True if `path` is equal to or nested inside an allowed root."""
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in allowed_roots:
        try:
            resolved_root = root.resolve()
        except Exception:
            continue
        if resolved == resolved_root:
            return True
        try:
            resolved.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


def _valid_create_dir_candidate(path: Path, allowed_roots: list[Path]) -> bool:
    """Validate an agent/card supplied directory before creating or binding it."""
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return False
    direct_child = False
    for root in allowed_roots:
        try:
            if resolved.parent == root.expanduser().resolve():
                direct_child = True
                break
        except Exception:
            continue
    if not direct_child or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", resolved.name):
        return False
    if not resolved.exists():
        return True
    if not resolved.is_dir():
        return False
    if (resolved / ".git").exists():
        return True
    try:
        return next(resolved.iterdir(), None) is None
    except OSError:
        return False


async def _leave_project(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Clear the active project for this user in the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await _send_card_message(ctx, 
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    ctx.service.set_chat_user_context(ctx.chat_id, open_id, None)
    await _send_card_message(ctx, 
        receive_id,
        "已清除当前会话的默认项目。",
        receive_id_type=receive_id_type,
    )


async def _switch_project(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> None:
    """Set the active project for this user in the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project = ctx.service.get_project(project_id)
    if project is None:
        await _send_card_message(ctx, 
            receive_id, f"项目 {project_id} 不存在", receive_id_type=receive_id_type
        )
        return
    if ctx.chat_id is None:
        await _send_card_message(ctx, 
            receive_id, "当前会话缺少 chat 上下文，无法设置默认项目。",
            receive_id_type=receive_id_type
        )
        return
    ctx.service.set_chat_user_context(ctx.chat_id, open_id, project_id)

    # If there was a pending ConversationInteraction (the user was asked to
    # pick a project), resolve it and re-dispatch the original message.
    pending_ci = ctx.service.get_pending_conversation_interaction(ctx.chat_id, open_id)
    redispatch_text: str | None = None
    if pending_ci is not None:
        await ctx.service.resolve_conversation_interaction(pending_ci.id, project_id)
        redispatch_text = pending_ci.source_text

    await _send_card_message(ctx, 
        receive_id,
        f"已将项目 {project_id} 设为当前会话的默认项目。",
        receive_id_type=receive_id_type,
    )

    # Re-dispatch the original free-text message that triggered the
    # disambiguation, now that the active project is set.
    if redispatch_text:
        from mac_system.feishu.models import TextMessage
        msg = TextMessage(
            open_id=open_id, message_id="", text=redispatch_text,
            is_command=redispatch_text.startswith("/"),
            chat_id=ctx.chat_id, chat_type=ctx.chat_type,
        )
        await handle_text_message(ctx, msg)


async def _show_current_project(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Show the bound directory and active project for this user in this chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await _send_card_message(ctx, 
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    binding = ctx.service.get_chat_directory_binding(ctx.chat_id)
    directory_line = (
        f"当前会话绑定目录：`{binding.directory}`"
        if binding else "当前会话没有绑定目录。"
    )
    project = ctx.service.get_chat_user_active_project(ctx.chat_id, open_id)
    if project is None:
        project = ctx.service.get_chat_user_selected_project(ctx.chat_id, open_id)
    if project is None:
        await _send_card_message(ctx, 
            receive_id,
            f"{directory_line}\n当前会话没有默认项目。可用 `/switch <项目ID>` 或 `/switch` 选择。",
            receive_id_type=receive_id_type,
        )
        return
    await _send_card_message(ctx, 
        receive_id,
        f"{directory_line}\n当前默认项目：{project.name or '未命名'}；"
        f"{StateMachine(project.task_type).display_name(project.current_state)}。",
        receive_id_type=receive_id_type,
    )


async def _show_project_switch_card(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Push a project selection card for `/switch` (no args).

    Lists projects the user can see in this chat and lets them pick one to
    become the active project. Clicking a button is equivalent to
    `/switch <project_id>`.
    """
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await _send_card_message(ctx, 
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    projects = ctx.service.list_projects_for_chat(ctx.chat_id)
    if not projects:
        await _send_card_message(ctx, 
            receive_id, "当前会话还没有绑定任何项目。", receive_id_type=receive_id_type
        )
        return
    payload = [
        {"id": p.id, "name": p.name or p.id, "current_state": p.current_state}
        for p in projects
    ]
    await _send_interactive_card(ctx, 
        receive_id,
        build_project_list_card(
            payload,
            mode="switch",
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
        ),
        receive_id_type=receive_id_type,
    )


async def _list_chat_projects(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """List projects bound to the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await _send_card_message(ctx, 
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    projects = ctx.service.list_projects_for_chat(ctx.chat_id)
    if not projects:
        await _send_card_message(ctx, 
            receive_id, "当前会话还没有绑定任何项目。", receive_id_type=receive_id_type
        )
        return
    lines = [f"当前会话的项目（共 {len(projects)} 个）："]
    for p in projects:
        marker = "* " if p.status == "active" else "  "
        lines.append(
            f"{marker}{p.id} {p.name or '(未命名)'} [{p.status}] {p.current_state}"
        )
    lines.append("\n使用 `/switch <项目ID>` 切换默认项目")
    await _send_card_message(ctx, 
        receive_id, "\n".join(lines), receive_id_type=receive_id_type
    )


async def _show_menu(ctx: FeishuHandlerContext, open_id: str) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    await _send_interactive_card(ctx, 
        receive_id,
        build_main_menu_card(chat_id=ctx.chat_id, chat_type=ctx.chat_type),
        receive_id_type=receive_id_type,
    )


def _find_rejected_project_for_feedback(
    ctx: FeishuHandlerContext, open_id: str
) -> Project | None:
    """Return the user's most recent active project that was just in a planning state."""
    from sqlmodel import select

    from mac_system.models import ProjectUser

    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if ctx.chat_id:
        chat_project_ids = {
            project.id for project in ctx.service.list_projects_for_chat(ctx.chat_id)
        }
        project_ids &= chat_project_ids
    if not project_ids:
        return None

    # Find the most recently updated active project that is in a planning state
    # (meaning it was just rolled back from a confirmation rejection).
    planning_states = {
        "PLANNING_REQUIREMENTS", "PLANNING_ARCHITECTURE", "PLANNING_IMPLEMENTATION_SPECS",
        "BUG_ANALYSIS", "DOC_OUTLINE",
    }
    projects = ctx.service.session.exec(
        select(Project)
        .where(Project.id.in_(project_ids))
        .where(Project.status == "active")
        .where(Project.current_state.in_(planning_states))
        .order_by(Project.updated_at.desc())
    ).all()

    for project in projects:
        if not project.context.get("rejected_confirmation_state"):
            continue
        # Only consider recently updated (within 10 min).
        age = (datetime.utcnow() - project.updated_at.replace(tzinfo=None)).total_seconds()
        if age < 600:
            return project

    return None


def _find_active_project_for_user(
    ctx: FeishuHandlerContext, open_id: str
) -> Project | None:
    """Return the active project for this user in the current chat, if any."""
    from sqlmodel import select

    from mac_system.models import ProjectUser

    # Prefer per-chat user context so group chats do not leak across projects.
    if ctx.chat_id:
        project = ctx.service.get_chat_user_active_project(ctx.chat_id, open_id)
        if project is not None:
            return project

    # A group chat is an independent project namespace. Falling back to the
    # user's globally most recent project leaks work from a DM or another
    # group into this conversation. Group projects must be selected through
    # the chat-scoped context (or created from this chat) explicitly.
    if ctx.chat_type == "group":
        return None

    # Fallback: most recent active project bound to the user (legacy p2p / no chat context).
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if not project_ids:
        return None
    return ctx.service.session.exec(
        select(Project)
        .where(Project.id.in_(project_ids))
        .where(Project.status == "active")
        .order_by(Project.updated_at.desc())
    ).first()


def _scan_available_repositories(ctx: FeishuHandlerContext) -> list[dict[str, str]]:
    """Return candidate git repositories under allowed bind roots.

    The bot is only allowed to look inside ``MAC_ALLOWED_BIND_ROOTS``; this
    keeps the coordinator's reach no wider than the explicit allow-list.
    """
    from mac_system.config import get_allowed_bind_roots

    allowed_roots = get_allowed_bind_roots()
    if not allowed_roots:
        return []
    return ProjectService.scan_candidate_repositories(allowed_roots)


def _repository_env_key_names(repository: Path) -> list[str]:
    """Read dotenv variable names only; never place their values in prompts."""
    if not repository.is_absolute() or not repository.is_dir():
        return []
    names: set[str] = set()
    for filename in (".env.example", ".env"):
        source = repository / filename
        if not source.is_file():
            continue
        try:
            for raw_line in source.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name = line.split("=", 1)[0].strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    names.add(name)
        except OSError:
            continue
    return sorted(names)


def _repository_file_manifest(
    repository: Path, *, max_files: int = 400, max_directories: int = 200
) -> list[str]:
    """Return a bounded, deterministic list of existing relative file paths.

    This is capability metadata, not semantic routing. It prevents a read-only
    Agent from spending its tool budget guessing entry points or traversing
    dependency/build directories after one repository has been authorized.
    """
    if not repository.is_absolute() or not repository.is_dir():
        return []
    excluded_directories = {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
        "dist", "build", "coverage", "projects",
    }
    pending: list[Path] = [repository]
    files: list[str] = []
    visited_directories = 0
    while pending and len(files) < max_files and visited_directories < max_directories:
        directory = pending.pop(0)
        visited_directories += 1
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        child_directories: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    if entry.name not in excluded_directories:
                        child_directories.append(entry)
                elif entry.is_file():
                    if (
                        entry.name == ".env"
                        or entry.name.startswith(".env.")
                        and entry.name != ".env.example"
                    ):
                        continue
                    files.append(str(entry.relative_to(repository)))
                    if len(files) >= max_files:
                        break
            except OSError:
                continue
        pending.extend(child_directories)
    return files


def _repository_context_bundle(
    repository: Path,
    relative_paths: list[str],
    *,
    max_total_chars: int = 80_000,
    max_chars_per_file: int = 20_000,
) -> list[dict[str, Any]]:
    """Read an Agent-selected, bounded set of files into a redacted bundle."""
    manifest = set(_repository_file_manifest(repository))
    bundle: list[dict[str, Any]] = []
    remaining = max_total_chars
    for relative_path in relative_paths[:8]:
        if relative_path not in manifest or remaining <= 0:
            continue
        source = repository / relative_path
        try:
            raw = source.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        limit = min(max_chars_per_file, remaining)
        excerpt = redact_text(text[:limit])
        try:
            modified_at = datetime.utcfromtimestamp(
                source.stat().st_mtime
            ).isoformat() + "Z"
        except OSError:
            modified_at = "unknown"
        bundle.append({
            "path": relative_path,
            "content": excerpt,
            "truncated": len(text) > limit,
            "modified_at": modified_at,
        })
        remaining -= len(excerpt)
    return bundle


def _repository_file_metadata(repository: Path) -> dict[str, dict[str, Any]]:
    """Return freshness metadata for the already bounded repository manifest."""
    metadata: dict[str, dict[str, Any]] = {}
    for relative_path in _repository_file_manifest(repository):
        source = repository / relative_path
        try:
            stat = source.stat()
        except OSError:
            continue
        metadata[relative_path] = {
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
            "size_bytes": stat.st_size,
        }
    return metadata


def _find_pending_input_approval(
    ctx: FeishuHandlerContext,
    open_id: str,
    *,
    project_id: str | None = None,
) -> Any | None:
    """Return one pending human-input approval in the resolved project scope.

    Explicit Coordinator routing already resolves the active project.  In that
    path, approvals from unrelated historical projects must not make the
    current input ambiguous.  Text ``/input`` commands without project context
    retain the conservative user-wide uniqueness rule.
    """
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if not project_ids:
        return None
    statement = (
        select(Approval)
        .where(Approval.project_id.in_(project_ids))
        .where(Approval.approval_type == "input")
        .where(Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    )
    if project_id is not None:
        if project_id not in project_ids:
            return None
        statement = statement.where(Approval.project_id == project_id)
    approvals = ctx.service.session.exec(statement).all()
    return approvals[0] if len(approvals) == 1 else None


async def _handle_rejection_feedback(
    ctx: FeishuHandlerContext,
    open_id: str,
    project: Project,
    text: str,
) -> None:
    """Store the user's rejection feedback as a structured packet and continue."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    rejected_state = project.context.get("rejected_confirmation_state", project.current_state)
    source_stage = _confirmation_source_stage(rejected_state)
    rounds = project.context.get("confirmation_rounds", {})
    round_number = rounds.get(rejected_state, 0) + 1
    await ctx.service.create_feedback_packet(
        project_id=project.id,
        source_stage=source_stage,
        source_task_id=None,
        source_round=round_number,
        kind="human_rejection",
        summary=text,
        blocking=True,
        failure_class="human_rejection",
        user_comment=text,
    )
    new_context = dict(project.context)
    new_context["latest_rejection_feedback"] = text
    new_context.pop("rejected_confirmation_state", None)
    project.context = new_context
    ctx.service.session.commit()

    # Summarize the feedback in a card before re-planning.
    sm = StateMachine(project.task_type)
    state_display = sm.display_name(rejected_state)
    summary_card = build_rejection_summary_card(
        project_name=project.name or project.id,
        state_display=state_display,
        feedback=text,
        round_number=round_number,
    )
    await _send_interactive_card(ctx, 
        receive_id, summary_card, receive_id_type=receive_id_type
    )
    await _advance_until_confirm(ctx, project, receive_id, receive_id_type)


async def _bind_project(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project = ctx.service.get_project(project_id)
    if project is None:
        await _send_card_message(ctx, 
            receive_id, f"项目 {project_id} 不存在", receive_id_type=receive_id_type
        )
        return

    ctx.service.session.add(ProjectUser(project_id=project_id, open_id=open_id))
    try:
        ctx.service.session.commit()
    except Exception:
        ctx.service.session.rollback()

    if ctx.chat_id:
        ctx.service.bind_project_to_chat(
            project_id, ctx.chat_id, chat_type=ctx.chat_type
        )
        ctx.service.set_chat_user_context(
            ctx.chat_id, open_id, project_id
        )

    await _send_card_message(ctx, 
        receive_id, f"已绑定到项目 {project_id}，并设为当前会话的默认项目。",
        receive_id_type=receive_id_type
    )


async def _show_status(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    try:
        status = ctx.service.get_project_status(project_id)
    except ProjectNotFoundError:
        await _send_card_message(ctx, 
            receive_id, f"项目 {project_id} 不存在", receive_id_type=receive_id_type
        )
        return

    project = ctx.service.require_project(project_id)
    pending = status.get("pending_approval")
    if project.context.get("orchestration_mode") == "agent_loop":
        agent_status_labels = {
            "pending": "等待执行",
            "running": "正在执行",
            "completed": "已完成",
            "blocked": "等待处理",
            "failed": "执行失败",
            "cancelled": "已取消",
        }
        agents = list(status.get("agents") or [])
        workflow_steps = [
            {
                "label": (
                    f"{agent.get('name') or 'Agent'}（{agent.get('provider') or 'unknown'}）"
                    f"· {agent_status_labels.get(agent.get('status'), agent.get('status'))}"
                ),
                "current": agent.get("status") in {"pending", "running"},
            }
            for agent in agents[-6:]
        ] or [{"label": "Coordinator", "current": project.status == "active"}]
        if project.status == "completed":
            workflow_steps.append({"label": "交付完成", "current": True})
        completion = (project.context or {}).get("completion") or {}
        recent_events = []
        if project.status == "completed":
            recent_events.append({
                "level": "INFO",
                "message": str(completion.get("summary") or "项目已完成并通过独立验收"),
            })
        recent_events.extend(
            {
                "level": "INFO",
                "message": (
                    f"{agent.get('name') or 'Agent'}："
                    f"{agent_status_labels.get(agent.get('status'), agent.get('status'))}"
                ),
            }
            for agent in reversed(agents[-3:])
        )
    else:
        state_machine = StateMachine(project.task_type)
        current_stage = state_machine.stage_for(status["current_state"])
        workflow_steps = [
            {
                "label": stage.get("display") or stage.get("id"),
                "current": stage.get("id") == current_stage,
            }
            for stage in state_machine.profile.get("stages", [])
        ]
        workflow_steps.extend([
            {"label": "主动验收", "current": status["current_state"] == "ACCEPTANCE_PENDING"},
            {"label": "交付", "current": status["current_state"] in {"MERGING", "COMPLETED"}},
        ])
        recent_events = [
            {"level": e["level"], "message": e["message"]}
            for e in status.get("recent_events", [])
        ]
    card = build_status_card(
        title=f"[{project.name or '当前项目'}] 项目状态",
        project_id=project_id,
        current_state=status["current_state"],
        display=status["display"],
        recent_events=recent_events,
        workflow_steps=workflow_steps,
        action_needed=(pending or {}).get("question"),
        workspace=(
            f"{status['workspace_path']}（老目录增量迭代）"
            if status.get("bound_directory")
            else status.get("workspace_path")
        ),
    )
    await _send_interactive_card(ctx, 
        receive_id, card, receive_id_type=receive_id_type
    )


async def _create_project(
    ctx: FeishuHandlerContext,
    open_id: str,
    task_type: str,
    description: str,
    name: str | None = None,
    chat_id: str | None = None,
    chat_type: str | None = None,
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project_chat_id = chat_id if chat_id is not None else ctx.chat_id
    project_chat_type = chat_type if chat_type is not None else ctx.chat_type
    if not name:
        name = suggest_name(description)
    try:
        project = await ctx.service.create_project(
            name=name,
            task_type=task_type,
            description=description,
            creator_open_id=open_id,
            feishu_chat_id=project_chat_id,
            feishu_chat_type=project_chat_type,
            initial_context={"orchestration_mode": "agent_loop"},
        )
    except FileNotFoundError:
        await _send_card_message(ctx, 
            receive_id, f"未知任务类型：{task_type}", receive_id_type=receive_id_type
        )
        return
    except Exception as exc:
        logger.exception("Failed to create project from Feishu")
        await _send_card_message(ctx, 
            receive_id, f"创建项目失败：{exc}", receive_id_type=receive_id_type
        )
        return

    # Acknowledge immediately so the user/chat sees a project id without
    # waiting for the (potentially long) first agent tick to complete.
    await _send_card_message(ctx, 
        receive_id,
        (
            f"已创建项目 {project.id}（{task_type}），开始执行。\n"
            f"工作目录：{project.workspace_path}"
            + ("（在已有仓库上增量迭代）" if project.bound_directory else "（独立项目目录）")
            + "\n系统开始处理，并会在需要你确认时发送卡片。"
        ),
        receive_id_type=receive_id_type,
    )
    try:
        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    except Exception as exc:
        logger.exception("Orchestrator tick failed after Feishu project creation")
        await _send_card_message(ctx, 
            receive_id, f"项目启动失败：{exc}", receive_id_type=receive_id_type
        )


async def _advance_until_confirm(
    ctx: FeishuHandlerContext,
    project: Project,
    receive_id: str,
    receive_id_type: str,
    max_ticks: int = 10,
) -> None:
    """Tick repeatedly until the project needs human input AND the card is sent.

    Human_confirmation states need an explicit tick to create the approval card.
    This helper keeps ticking through agent_execution states, then ticks the
    human_confirmation state so the card is actually delivered, then returns.
    """
    if project.context.get("orchestration_mode") == "agent_loop":
        await ctx.orchestrator.tick(project.id)
        return

    # If the caller starts at a human boundary, one tick normally creates or
    # reuses its approval.  That tick may also consume newly submitted evidence
    # and route into an Agent stage (for example acceptance failure -> coding).
    # Re-read the actual state before returning so the newly selected stage is
    # not left marked ``running`` without a task.
    sm = StateMachine(project.task_type)
    if sm.state_type(project.current_state) in ("human_confirmation", "human_input"):
        boundary_state = project.current_state
        await ctx.orchestrator.tick(project.id)
        project = ctx.service.require_project(project.id)
        next_type = StateMachine(project.task_type).state_type(
            project.current_state
        )
        if project.current_state == boundary_state or next_type in (
            "human_confirmation",
            "human_input",
        ):
            return

    last_state: str | None = project.current_state
    retried_idle_agent_states: set[str] = set()
    for _ in range(max_ticks):
        logger.info(
            "  _advance_until_confirm tick %s (state=%s)", project.id, project.current_state,
        )
        await ctx.orchestrator.tick(project.id)
        project = ctx.service.require_project(project.id)
        if project.current_state == last_state:
            sm = StateMachine(project.task_type)
            if sm.state_type(project.current_state) == "agent_execution":
                agent_type = sm.agent_for(project.current_state)
                running_task = ctx.service.session.exec(
                    select(Task)
                    .where(
                        Task.project_id == project.id,
                        Task.state == project.current_state,
                        Task.agent_type == agent_type,
                        Task.status == "running",
                    )
                    .limit(1)
                ).first()
                if (
                    running_task is None
                    and project.current_state not in retried_idle_agent_states
                ):
                    # A boundary tick can consume a just-completed result or
                    # perform recovery bookkeeping without starting the next
                    # Agent. Give the durable agent state one more tick instead
                    # of leaving it marked running with no task.
                    logger.warning(
                        "  _advance_until_confirm retrying idle agent state %s "
                        "for %s",
                        project.current_state,
                        project.id,
                    )
                    retried_idle_agent_states.add(project.current_state)
                    continue
            logger.info(
                "  _advance_until_confirm done for %s — state unchanged (%s)",
                project.id, project.current_state,
            )
            return  # Tick did not change state; avoid spinning forever.
        last_state = project.current_state
        sm = StateMachine(project.task_type)
        st = sm.state_type(project.current_state)
        if st in ("human_confirmation", "human_input"):
            # We just arrived at a confirmation state; tick once more so the
            # orchestrator creates the approval and sends the card.  That tick
            # may also auto-resolve the boundary (for example REWORK selecting
            # its recommended coding target).  Re-read the state and continue
            # into the selected Agent stage instead of leaving the project
            # marked running with no task.
            boundary_state = project.current_state
            logger.info(
                "  _advance_until_confirm reached %s for %s, sending card",
                project.current_state, project.id,
            )
            await ctx.orchestrator.tick(project.id)
            project = ctx.service.require_project(project.id)
            routed_type = StateMachine(project.task_type).state_type(
                project.current_state
            )
            if project.current_state == boundary_state or routed_type in (
                "human_confirmation",
                "human_input",
            ):
                return
            if routed_type in ("terminal", "idle"):
                return
            last_state = project.current_state
            continue
        if st in ("terminal", "idle"):
            logger.info(
                "  _advance_until_confirm terminal for %s at %s",
                project.id, project.current_state,
            )
            return


async def _route_and_create(
    ctx: FeishuHandlerContext,
    open_id: str,
    description: str,
    chat_id: str | None = None,
    chat_type: str | None = None,
) -> None:
    """Classify the description and create the project or ask for confirmation."""
    decision = route_demand(description)
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    # Prefer chat context explicitly provided (e.g. from a pending intent) over
    # the current message context, so a project is created in the chat where the
    # user started the flow.
    effective_chat_id = chat_id if chat_id is not None else ctx.chat_id
    effective_chat_type = chat_type if chat_type is not None else ctx.chat_type

    # If the user explicitly invoked /create, we default to a feature project
    # when no strong bug/doc signal is detected, so that generic descriptions
    # like "帮我做一个个人网站" still result in a project.
    if decision.task_type is None:
        from mac_system.core.intent import CONFIDENCE_EXPLICIT
        decision = RouteDecision(
            task_type="feature",
            initial_state=StateMachine("feature").initial_state,
            confidence=CONFIDENCE_EXPLICIT,
            needs_disambiguation=False,
            reason="未识别明确类型，默认按功能需求创建",
        )

    if decision.confidence < ctx.service.settings.intent_low_threshold:
        await _send_card_message(ctx, 
            receive_id,
            "这个需求我不太确定怎么归类，能再描述一下吗？\n"
            "或者用 `/create --type=feature <描述>` 强制指定类型。",
            receive_id_type=receive_id_type,
        )
        return

    if decision.needs_disambiguation:
        ctx.service.upsert_pending_intent(
            open_id,
            description=description,
            stage="awaiting_type_confirm",
            chat_id=effective_chat_id,
            chat_type=effective_chat_type,
        )
        await _send_interactive_card(ctx, 
            receive_id,
            build_intent_confirm_card(
                decision.task_type,
                chat_id=effective_chat_id,
                chat_type=effective_chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    # If the chat has no directory binding and allowed roots are configured,
    # try the project_manager bind_dir agent to recommend a matching repo
    # based on the description. If candidates are found, pause the create
    # flow and show a pick card. Otherwise proceed with system workspace.
    if await _maybe_pause_for_bind_dir_recommendation(
        ctx,
        open_id,
        task_type=decision.task_type,
        description=description,
        chat_id=effective_chat_id,
        chat_type=effective_chat_type,
    ):
        return

    name = suggest_name(description)
    await _create_project(
        ctx, open_id, decision.task_type, description, name=name,
        chat_id=effective_chat_id, chat_type=effective_chat_type,
    )


async def _maybe_pause_for_bind_dir_recommendation(
    ctx: FeishuHandlerContext,
    open_id: str,
    *,
    task_type: str,
    description: str,
    chat_id: str | None,
    chat_type: str | None,
) -> bool:
    """Try to recommend a bind dir based on description; pause create if found.

    Returns True if the create flow should pause (card was shown), False if
    the create flow should proceed normally.
    """
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not chat_id:
        return False
    # Already bound - no need to recommend.
    if ctx.service.get_chat_directory_binding(chat_id) is not None:
        return False
    from mac_system.config import get_allowed_bind_roots

    allowed_roots = get_allowed_bind_roots()
    if not allowed_roots:
        return False

    # Don't recommend if there are active/paused projects (same constraint as
    # /bind - the user would have to cancel them first).
    blockers = _blocking_projects_for_directory_change(ctx, chat_id)
    if blockers:
        return False

    await _send_card_message(ctx, 
        receive_id,
        "正在根据需求描述生成候选目录名…",
        receive_id_type=receive_id_type,
    )

    allowed_roots_str = "\n".join(str(r) for r in allowed_roots)
    try:
        output = await asyncio.wait_for(
            ctx.orchestrator.run_chat_scoped_agent(
                agent_type="project_manager",
                stage="create_dir",
                input_data={
                    "allowed_bind_roots": [str(r) for r in allowed_roots],
                    "description": description,
                },
                extra_variables={
                    "allowed_bind_roots": allowed_roots_str,
                    "description": description,
                },
            ),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        logger.warning("project_manager create_dir timed out; continuing without recommendation")
        return False
    except Exception as exc:
        logger.warning("project_manager create_dir agent crashed in /create: %s", exc)
        return False

    if not output or not output.get("success") or not output.get("candidates"):
        return False

    valid: list[dict[str, str]] = []
    seen: set[Path] = set()
    for c in output["candidates"][:8]:
        path_str = c.get("path", "")
        if not path_str:
            continue
        path = Path(path_str).expanduser()
        try:
            path = path.resolve()
        except Exception:
            logger.warning("bind_dir candidate path resolve failed: %s", path_str)
            continue
        if not _valid_create_dir_candidate(path, allowed_roots):
            logger.warning(
                "project_manager returned unsafe create-dir candidate: %s", path
            )
            continue
        if path in seen:
            continue
        seen.add(path)
        valid.append({"path": str(path), "name": c.get("name") or path.name})
    if not valid:
        return False

    ctx.service.upsert_pending_intent(
        open_id,
        description=_encode_create_state(task_type, description),
        stage="awaiting_bind_dir_for_create",
        chat_id=chat_id,
        chat_type=chat_type or "p2p",
    )
    await _send_interactive_card(ctx, 
        receive_id,
        build_bind_dir_for_create_card(
            valid,
            description=description,
            task_type=task_type,
            chat_id=chat_id,
            chat_type=chat_type or "p2p",
        ),
        receive_id_type=receive_id_type,
    )
    return True


async def _resume_create_with_bind_dir(
    ctx: FeishuHandlerContext,
    open_id: str,
    *,
    directory: str | None,
) -> None:
    """Resume the `/create` flow after the user picked (or skipped) a bind dir.

    Reads the JSON-encoded (task_type, description) from the user's pending
    intent, optionally binds the directory, then proceeds with project
    creation. If binding fails, we still create the project in the system
    workspace so the user's intent isn't lost.
    """
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    pending = ctx.service.get_pending_intent(open_id)
    if pending is None or pending.stage != "awaiting_bind_dir_for_create":
        await _send_card_message(ctx, 
            receive_id,
            "没有正在等待的创建流程，请重新使用 `/create <描述>`。",
            receive_id_type=receive_id_type,
        )
        return
    task_type, description = _decode_create_state(pending.description)
    if not task_type or not description:
        ctx.service.delete_pending_intent(open_id)
        await _send_card_message(ctx, 
            receive_id,
            "创建流程状态已损坏，请重新使用 `/create <描述>`。",
            receive_id_type=receive_id_type,
        )
        return
    chat_id = pending.chat_id or ctx.chat_id
    chat_type = pending.chat_type or ctx.chat_type

    if directory:
        from mac_system.config import get_allowed_bind_roots

        if not _valid_create_dir_candidate(
            Path(directory), get_allowed_bind_roots()
        ):
            await _send_card_message(ctx, 
                receive_id,
                "该目录选项已失效或不符合安全规则，请使用最新候选卡片重新选择。",
                receive_id_type=receive_id_type,
            )
            return
    ctx.service.delete_pending_intent(open_id)

    if directory:
        try:
            ctx.service.bind_directory_to_chat(
                chat_id, directory, bound_by=open_id
            )
        except (ValueError, OSError) as exc:
            await _send_card_message(ctx, 
                receive_id,
                f"绑定目录失败：{exc}\n将使用系统工作区继续创建项目。",
                receive_id_type=receive_id_type,
            )
        else:
            await _send_card_message(ctx, 
                receive_id,
                f"已绑定到 `{directory}`，开始创建项目…",
                receive_id_type=receive_id_type,
            )

    name = suggest_name(description)
    await _create_project(
        ctx, open_id, task_type, description, name=name,
        chat_id=chat_id, chat_type=chat_type,
    )


def _parse_create_command(rest: str) -> tuple[str | None, str]:
    """Parse `/create [--type=<task_type>] <description>`.

    Returns (task_type, description). If ``--type=`` is omitted,
    task_type is ``None`` and the description should be routed.
    """
    rest = rest.strip()
    task_type: str | None = None
    if rest.startswith("--type="):
        prefix, _, remainder = rest.partition(" ")
        task_type = prefix[len("--type="):].strip() or None
        rest = remainder.strip()
    return task_type, rest


async def _submit_input(
    ctx: FeishuHandlerContext,
    open_id: str,
    rest: str,
    *,
    question_id: str | None = None,
    project_id: str | None = None,
    answer_key: str | None = None,
    rework_target: str | None = None,
) -> str:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    parts = rest.split(None, 1)

    # Only the explicit ``/input <approval_id> <answer>`` form may select an
    # approval from the first token.  Natural-language answers commonly start
    # with a long token (for example a question_id); treating every token with
    # length >= 12 as an approval id misroutes valid answers to the
    # "approval not found" path.  When the Coordinator already supplied a
    # question_id, the current pending input approval is authoritative and the
    # complete text must remain the answer.
    if question_id is None and len(parts) >= 2 and parts[0].startswith("appr_"):
        approval_id, answer = parts
    else:
        # Try to find a single pending human_input approval for this user.
        pending = _find_pending_input_approval(
            ctx, open_id, project_id=project_id
        )
        if pending is None:
            message = (
                "格式错误。用法：/input <approval_id> <你的回答>\n"
                "或当前没有待输入的审批。"
            )
            await _send_card_message(ctx, 
                receive_id, message, receive_id_type=receive_id_type
            )
            return message
        approval_id = pending.id
        answer = rest

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        message = f"审批 {approval_id} 不存在或已过期"
        await _send_card_message(ctx, 
            receive_id, message, receive_id_type=receive_id_type
        )
        return message
    if approval.status != "pending":
        message = f"审批 {approval_id} 已处理，无需重复提交。"
        await _send_card_message(ctx, 
            receive_id, message, receive_id_type=receive_id_type
        )
        return message

    project = ctx.service.get_project(approval.project_id)
    if project is None:
        message = "关联项目不存在"
        await _send_card_message(ctx, 
            receive_id, message, receive_id_type=receive_id_type
        )
        return message

    # REWORK answers are option keys (e.g. "to_coding"); other inputs are free text.
    if approval.state == "REWORK":
        valid_actions = {opt.get("action") for opt in approval.payload.get("options", [])}
        if answer in valid_actions:
            event = answer
            payload = None
        else:
            message = f"无效选项：{answer}。可用选项：{', '.join(sorted(valid_actions))}"
            await _send_card_message(ctx, 
                receive_id, message, receive_id_type=receive_id_type
            )
            return message
    else:
        # QuestionInteraction: if the approval has a linked interaction, route
        # the answer to the first pending question (free-text answer).
        interaction_id = approval.payload.get("question_interaction_id")
        if interaction_id:
            interaction = ctx.service.get_question_interaction(interaction_id)
            if interaction and interaction.status == "pending":
                questions = ctx.service.get_questions_for_interaction(interaction_id)
                pending_qs = [q for q in questions if q.status == "pending"]
                target_q = (
                    next((q for q in questions if q.question_id == question_id), None)
                    if question_id
                    else (pending_qs[0] if pending_qs else None)
                )
                if question_id and target_q is None:
                    message = f"问题 {question_id} 不属于当前输入批次。"
                    await _send_card_message(ctx, 
                        receive_id, message, receive_id_type=receive_id_type
                    )
                    return message
                if target_q is not None:
                    q = target_q
                    trace_ids = [
                        str(value)
                        for value in (q.affected_trace_ids or [])
                        if value
                    ]
                    if (
                        answer_key == "verified_fail"
                        and interaction.stage == "acceptance"
                        and len(trace_ids) == 1
                    ):
                        summary = (
                            "用户已明确报告人工验收失败：" + trace_ids[0]
                        )
                        existing_failure = next(
                            (
                                item
                                for item in ctx.service.get_open_feedback_packets(
                                    project.id
                                )
                                if item.kind == "acceptance_rejection"
                                and item.summary == summary
                            ),
                            None,
                        )
                        if existing_failure is None:
                            await ctx.service.create_feedback_packet(
                                project_id=project.id,
                                source_stage="ACCEPTANCE",
                                source_task_id=None,
                                source_round=1,
                                kind="acceptance_rejection",
                                failure_class="observed_behavior_failure",
                                summary=summary,
                                blocking=True,
                                recommended_rework_target=(
                                    rework_target
                                    if rework_target
                                    in {
                                        "requirements",
                                        "architecture",
                                        "specs",
                                        "coding",
                                        "testing",
                                    }
                                    else None
                                ),
                                evidence=[{
                                    "acceptance_criterion_id": trace_ids[0],
                                    "source_question_id": q.question_id,
                                    "user_observation": answer,
                                }],
                                user_comment=answer,
                                commit=False,
                            )
                    await ctx.service.submit_question_answer(
                        q.id,
                        answer_key or answer,
                        answer,
                        False,
                        open_id,
                        commit=False,
                    )
                    if ctx.service.interaction_is_resolvable(interaction_id):
                        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
                        await ctx.service.respond_approval(approval.id, "answer", open_id, commit=False)
                        ctx.service.session.commit()
                        await _resume_after_question_interaction(
                            ctx, project, interaction, open_id
                        )
                        message = "已收到你的回答，Agent 继续执行。"
                        await _send_card_message(ctx, 
                            receive_id, message, receive_id_type=receive_id_type
                        )
                        if project.context.get("orchestration_mode") != "agent_loop":
                            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
                        return message
                    ctx.service.session.commit()
                    message = "已记录该问题的回答，还有其他问题待回答。"
                    await _send_card_message(ctx, 
                        receive_id, message, receive_id_type=receive_id_type
                    )
                    return message
        event = "answer"
        payload = {"clarification_answer": answer}

    await ctx.service.resolve_approval(
        approval.id, event, responded_by=open_id, payload=payload
    )
    await ctx.orchestrator.tick(project.id)
    message = f"已提交，项目 {project.id} 继续执行"
    await _send_card_message(ctx, 
        receive_id, message, receive_id_type=receive_id_type
    )
    return message


async def handle_card_action(ctx: FeishuHandlerContext, action: ButtonAction) -> dict[str, Any] | None:
    """Process an interactive card button click.

    Returns a card dict when the action should update the card in-place via the
    Feishu response (e.g. selecting an option). Returns None for actions that
    only need a toast or text reply.
    """
    open_id = action.open_id

    # Card actions may carry the chat context of the chat where the card was
    # originally sent (e.g. a group). Prefer it over the receiving ctx so
    # replies go back to the right conversation.
    if action.chat_id:
        ctx.chat_id = action.chat_id
        ctx.chat_type = action.chat_type

    # Menu buttons are marked with action == "menu".
    if action.action == "menu":
        await _handle_menu_action(ctx, action)
        return

    if action.action == "conversation_clarification":
        pending_action_id = action.extras.get("pending_action_id")
        pending = (
            ctx.service.session.get(PendingConversationAction, pending_action_id)
            if pending_action_id
            else None
        )
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        if pending is None or pending.action_type != "clarification":
            await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_content_card("选择已失效", "该选择已经失效，请查看最新卡片。"),
                message_id=action.message_id,
            )
            return
        if pending.status != "pending":
            await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_content_card("已经处理", "该选择已经处理，无需重复点击。"),
                message_id=action.message_id,
            )
            return
        if pending.created_by and pending.created_by != open_id:
            await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_content_card("无法操作", "只有发起问题的用户可以选择。"),
                message_id=action.message_id,
            )
            return
        option_key = action.option_key or ""
        options = pending.payload.get("options") or []
        selected = next(
            (item for item in options if str(item.get("key")) == option_key), None
        )
        if selected is None:
            await _deliver_conversation_card(
                ctx, receive_id, receive_id_type,
                build_content_card("选项无效", "未找到这个选项，请查看最新卡片。"),
                message_id=action.message_id,
            )
            return
        ctx.service.resolve_pending_conversation_action(
            pending.id, decision="confirm", resolved_by=open_id
        )
        label = str(selected.get("label") or option_key)
        progress_message_id = action.message_id
        if progress_message_id:
            await _update_interactive_card(ctx, 
                progress_message_id,
                build_conversation_clarification_card(
                    pending.id,
                    str(pending.payload.get("question") or "当前问题"),
                    options,
                    reason=str(pending.payload.get("reason") or ""),
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                    selected_key=option_key,
                ),
            )
        question = str(pending.payload.get("question") or "当前问题")
        await _handle_coordinated_free_text(
            ctx,
            TextMessage(
                open_id=open_id,
                message_id=f"card:{pending.id}:{option_key}",
                text=f"关于“{question}”，我选择「{label}」（{option_key}）。",
                is_command=False,
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
            ),
            # Preserve the selected decision card and start a new milestone.
            progress_message_id=None,
        )
        return

    if action.action == "conversation_action":
        pending_action_id = action.extras.get("pending_action_id")
        decision = action.extras.get("decision", "confirm")
        if not pending_action_id:
            receive_id, receive_id_type = _reply_target(ctx, open_id)
            await _send_card_message(ctx, 
                receive_id,
                "卡片缺少执行请求标识，请查看最新卡片。",
                receive_id_type=receive_id_type,
            )
            return
        await _execute_pending_conversation_action(
            ctx,
            open_id,
            pending_action_id,
            decision=decision,
        )
        return

    # Question batch card actions (unified ask_user).
    if action.action in (
        "answer_all_recommended",
        "answer_with_values",
        "skip_remaining",
        "answer_question_option",
        "select_question_option",
        "submit_selected_options",
        "submit_inline_free_text",
    ):
        return await _handle_question_batch_action(ctx, action)

    if action.action == "skip_free_text":
        return await _handle_skip_free_text(ctx, action)

    if action.action == "submit_rejection_feedback":
        feedback = str((action.extras or {}).get("rejection_feedback", "")).strip()
        if not feedback:
            receive_id, receive_id_type = _reply_target(ctx, open_id)
            await _send_card_message(ctx, 
                receive_id,
                "请先填写修改意见，再提交驳回。",
                receive_id_type=receive_id_type,
            )
            return
        await _respond_to_approval(
            ctx,
            open_id,
            action.approval_id,
            "reject",
            feedback_text=feedback,
            source_message_id=action.message_id,
        )
        return

    if action.action == "reject_without_feedback":
        await _respond_to_approval(
            ctx,
            open_id,
            action.approval_id,
            "reject",
            skip_rejection_feedback=True,
            source_message_id=action.message_id,
        )
        return

    # Otherwise treat as an approval/decision button.
    if action.action == "select_option":
        await _respond_to_approval(
            ctx,
            open_id,
            action.approval_id,
            "select_option",
            extra={
                "decision_id": action.decision_id,
                "option_key": action.option_key,
            },
            source_message_id=action.message_id,
        )
        return

    chosen = action.action
    if chosen not in ("confirm", "reject"):
        chosen = action.option_key or chosen
    await _respond_to_approval(
        ctx,
        open_id,
        action.approval_id,
        chosen,
        source_message_id=action.message_id,
    )


async def _handle_question_batch_action(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> dict[str, Any] | None:
    """Process answers from a question batch card.

    Returns the updated card dict for ``select_question_option`` so the caller
    can update the card via the Feishu response without a toast.
    """
    open_id = action.open_id
    interaction_id = action.extras.get("interaction_id") if action.extras else None
    approval_id = action.approval_id
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        await _send_card_message(ctx, receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type)
        return
    if interaction_id is None:
        interaction_id = approval.payload.get("question_interaction_id")
    if not interaction_id:
        await _send_card_message(ctx, receive_id, "未找到对应的问题交互", receive_id_type=receive_id_type)
        return
    interaction = ctx.service.get_question_interaction(interaction_id)
    if interaction is None or interaction.status != "pending":
        await _send_card_message(ctx, receive_id, "该问题交互已处理或不存在", receive_id_type=receive_id_type)
        return
    project = ctx.service.get_project(interaction.project_id)
    if project is None:
        await _send_card_message(ctx, receive_id, "关联项目不存在", receive_id_type=receive_id_type)
        return
    if not _is_authorized_for_project(ctx, open_id, project.id):
        await _send_card_message(ctx, 
            receive_id,
            f"你没有权限回答项目 {project.id} 的问题。",
            receive_id_type=receive_id_type,
        )
        return

    questions = ctx.service.get_questions_for_interaction(interaction_id)
    # The question batch card only contains choice questions. Free-text
    # questions are sent as separate input cards and answered by replying
    # to those cards, so they should not be touched here.
    pending_questions = [q for q in questions if q.status == "pending" and q.options]
    all_question_dicts = [q.model_dump() for q in questions if q.options]
    # Snapshot question data before any commit, because SQLAlchemy expires
    # ORM objects after commit and model_dump() would return empty dicts.
    pending_question_dicts = [q.model_dump() for q in pending_questions]
    custom_answer_question_ids = set(
        approval.payload.get("custom_answer_question_ids", [])
    )

    if action.action == "select_question_option":
        target_question_id = action.extras.get("question_id") if action.extras else None
        option_key = action.option_key
        target = next((q for q in pending_question_dicts if q.get("question_id") == target_question_id), None)
        if target is None or option_key is None:
            ctx.service.session.commit()
            await _send_card_message(ctx, receive_id, "未找到对应问题或选项", receive_id_type=receive_id_type)
            return
        selected_options = dict(approval.payload.get("selected_options", {}))
        selected_options[target_question_id] = option_key
        approval.payload = {**approval.payload, "selected_options": selected_options}
        ctx.service.session.commit()

        # Re-render the card with the new selection so the user sees the change.
        card = build_question_batch_card(
            approval_id=approval.id,
            interaction_id=interaction_id,
            questions=pending_question_dicts,
            agent=interaction.agent,
            stage=interaction.stage,
            state=approval.state,
            project_name=project.name or project.id,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            selected_options=selected_options,
            inline_free_text_question_id=approval.payload.get("inline_free_text_question_id"),
        )
        # The caller (WebSocket) will return this card in the action response,
        # which updates the card in-place without a separate API call.
        return card

    if action.action == "answer_question_option":
        target_question_id = action.extras.get("question_id") if action.extras else None
        option_key = action.option_key
        target = next((q for q in pending_questions if q.question_id == target_question_id), None)
        if target is None or option_key is None:
            ctx.service.session.commit()
            await _send_card_message(ctx, receive_id, "未找到对应问题或选项", receive_id_type=receive_id_type)
            return
        answer_label = option_key
        for opt in target.options:
            if opt.get("key") == option_key:
                answer_label = opt.get("label", option_key)
                break
        if _option_triggers_free_text(target, option_key):
            custom_answer_question_ids.add(target.question_id)
        else:
            _apply_option_runtime_grants(project, interaction, target, option_key)
            await ctx.service.submit_question_answer(
                target.id,
                option_key,
                answer_label,
                False,
                open_id,
                commit=False,
                keep_pending=_keeps_external_verification_pending(
                    interaction, target, option_key
                ),
            )
    elif action.action == "submit_selected_options":
        selected_options = dict(approval.payload.get("selected_options", {}))
        for q in pending_questions:
            answer = selected_options.get(q.question_id) or q.recommended_option
            if answer is None:
                continue
            if _option_triggers_free_text(q, answer):
                custom_answer_question_ids.add(q.question_id)
                continue
            _apply_option_runtime_grants(project, interaction, q, answer)
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            _apply_option_runtime_grants(project, interaction, q, answer)
            await ctx.service.submit_question_answer(
                q.id,
                answer,
                answer_label,
                False,
                open_id,
                commit=False,
                keep_pending=_keeps_external_verification_pending(
                    interaction, q, answer
                ),
            )
    elif action.action == "answer_all_recommended":
        custom_answer_question_ids.clear()
        for q in pending_questions:
            answer = q.recommended_option
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            await ctx.service.submit_question_answer(
                q.id,
                answer,
                answer_label,
                False,
                open_id,
                commit=False,
                keep_pending=_keeps_external_verification_pending(
                    interaction, q, answer
                ),
            )
    elif action.action == "answer_with_values":
        for q in pending_questions:
            field_name = f"q_{q.question_id}"
            answer = (action.extras or {}).get(field_name) if action.extras else None
            if answer is None:
                answer = q.recommended_option
            if _option_triggers_free_text(q, answer):
                custom_answer_question_ids.add(q.question_id)
                continue
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            await ctx.service.submit_question_answer(
                q.id,
                answer,
                answer_label,
                False,
                open_id,
                commit=False,
                keep_pending=_keeps_external_verification_pending(
                    interaction, q, answer
                ),
            )
    elif action.action == "skip_remaining":
        for q in pending_questions:
            if q.kind in ("blocking", "decision"):
                continue
            await ctx.service.submit_question_answer(q.id, None, None, True, open_id, commit=False)
    elif action.action == "submit_inline_free_text":
        # Inline free-text submission: user typed a custom answer directly in
        # the question card's inline form, bypassing the 2-step "provide_custom
        # -> reply to input card" flow. Submit the typed text as the answer for
        # the targeted question only; other pending questions remain open.
        target_question_id = action.extras.get("question_id") if action.extras else None
        typed_text = str((action.extras or {}).get("inline_free_text_value", "")).strip()
        target = next((q for q in pending_questions if q.question_id == target_question_id), None)
        if target is None:
            ctx.service.session.commit()
            await _send_card_message(ctx, receive_id, "未找到对应问题。", receive_id_type=receive_id_type)
            return
        if not typed_text:
            ctx.service.session.commit()
            await _send_card_message(ctx, receive_id, "请先输入自定义答案再提交。", receive_id_type=receive_id_type)
            return
        if not target.allow_free_text:
            ctx.service.session.commit()
            await _send_card_message(ctx, receive_id, "该问题不支持自定义答案。", receive_id_type=receive_id_type)
            return
        # Discard the question from custom_answer_question_ids (if it was
        # pre-marked) so the 2-step fallback path does not fire after we have
        # already recorded a direct free-text answer.
        custom_answer_question_ids.discard(target.question_id)
        await ctx.service.submit_question_answer(
            target.id, typed_text, typed_text, False, open_id, commit=False
        )

    approval.payload = {
        **approval.payload,
        "custom_answer_question_ids": sorted(custom_answer_question_ids),
    }

    # Update the choice card to a read-only "已提交" receipt state showing the
    # recorded answers. This runs for any finalize action regardless of whether
    # the full interaction resolves, so the user sees their choices are locked.
    if action.action in (
        "submit_selected_options",
        "skip_remaining",
        "answer_all_recommended",
        "submit_inline_free_text",
    ):
        submitted_answers: dict[str, Any] = {}
        receipt_selected_options = dict(
            approval.payload.get("selected_options", {})
        )
        # Include already-completed siblings in the receipt. A deferred
        # `not_verified` answer deliberately keeps one acceptance question
        # pending; otherwise completed siblings become clickable again when
        # that card is refreshed.
        for q in questions:
            if not q.options:
                continue
            answer_record = ctx.service.get_question_answer(q.id)
            if answer_record and not answer_record.skipped and answer_record.answer:
                if q.status == "pending" and answer_record.answer == "not_verified":
                    receipt_selected_options[q.question_id] = answer_record.answer
                else:
                    submitted_answers[q.question_id] = answer_record.answer
            elif q.question_id in custom_answer_question_ids:
                submitted_answers[q.question_id] = _CUSTOM_ANSWER_OPTION_KEY
        receipt_card = build_question_batch_card(
            approval_id=approval_id,
            interaction_id=interaction_id,
            questions=all_question_dicts,
            agent=interaction.agent,
            stage=interaction.stage,
            state=approval.state,
            project_name=project.name or project.id,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            submitted_answers=submitted_answers,
            selected_options=receipt_selected_options,
            awaiting_free_text=bool(custom_answer_question_ids),
            inline_free_text_question_id=approval.payload.get("inline_free_text_question_id"),
        )
        batch_message_id = action.message_id or approval.payload.get("question_batch_message_id")
        if batch_message_id:
            try:
                await _update_interactive_card(ctx, batch_message_id, receipt_card)
            except Exception:
                logger.exception("Failed to update question batch card to submitted state")

    # Determine what's still pending after recording answers.
    all_questions = ctx.service.get_questions_for_interaction(interaction_id)
    pending_choice_qs = [q for q in all_questions if q.status == "pending" and q.options]
    pending_free_text_qs = [q for q in all_questions if q.status == "pending" and not q.options]
    pending_custom_qs = [
        q for q in pending_choice_qs
        if q.question_id in custom_answer_question_ids and q.allow_free_text
    ]
    pending_external_qs = [
        q
        for q in pending_choice_qs
        if (
            (latest_answer := ctx.service.get_question_answer(q.id)) is not None
            and latest_answer.answer == "not_verified"
            and interaction.stage == "acceptance"
        )
    ]

    if pending_custom_qs:
        ctx.service.session.commit()
        sent_next = await _send_next_free_text_card(
            ctx, approval, interaction, project, receive_id, receive_id_type
        )
        if not sent_next:
            await _send_card_message(ctx, 
                receive_id,
                "请选择回复输入卡，补充你的具体要求。",
                receive_id_type=receive_id_type,
            )
    elif pending_choice_qs and len(pending_external_qs) == len(pending_choice_qs):
        ctx.service.set_execution_status(
            project.id,
            "waiting_external",
            reason="等待用户完成外部人工验收",
            commit=False,
        )
        ctx.service.session.commit()
        await _send_card_message(ctx, 
            receive_id,
            "已记录“尚未验证”。项目会保持等待，不会重复运行 Agent 或重新提问；完成外部验证后，可直接在原卡片更新结果。",
            receive_id_type=receive_id_type,
        )
    elif pending_choice_qs:
        # Still have choice questions to answer; don't move on yet.
        ctx.service.session.commit()
        if action.action == "answer_question_option":
            await _send_card_message(ctx, receive_id, "已记录选择，请继续回答其他问题。", receive_id_type=receive_id_type)
        else:
            await _send_card_message(ctx, receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)
    elif pending_free_text_qs:
        # All choices answered but free-text questions remain. Send the first
        # free-text card; the interaction resolves after the user answers or
        # skips all of them.
        ctx.service.session.commit()
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await _send_card_message(ctx, receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)
    elif ctx.service.interaction_is_resolvable(interaction_id):
        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
        await ctx.service.respond_approval(approval_id, "answer", open_id, commit=False)
        ctx.service.session.commit()
        project = ctx.service.get_project(interaction.project_id)
        if project is not None:
            await _resume_after_question_interaction(ctx, project, interaction, open_id)
        await _send_card_message(ctx, receive_id, "已收到你的回答，Agent 继续执行。", receive_id_type=receive_id_type)
        if project is not None and project.context.get("orchestration_mode") != "agent_loop":
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        ctx.service.session.commit()
        await _send_card_message(ctx, receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)


async def _handle_menu_action(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> None:
    open_id = action.open_id
    option = action.option_key or "show_menu"
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    if option == "show_menu":
        await _show_menu(ctx, open_id)
        return

    if option == "list_projects_status":
        projects = _list_projects_for_user(ctx, open_id)
        await _send_interactive_card(ctx, 
            receive_id,
            build_project_list_card(
                projects,
                mode="status",
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    if option == "list_projects_bind":
        projects = _list_all_projects(ctx)
        await _send_interactive_card(ctx, 
            receive_id,
            build_project_list_card(
                projects,
                mode="bind",
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    if option == "create_prompt":
        await _prompt_for_create_description(ctx, open_id)
        return

    if option.startswith("intent_confirm:"):
        task_type = option.split(":", 1)[1]
        pending = ctx.service.get_pending_intent(open_id)
        # Reply in the chat where the user started the create flow.
        if pending is not None and pending.chat_id:
            ctx.chat_id = pending.chat_id
            ctx.chat_type = pending.chat_type
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        description = pending.description if pending else None
        if description:
            ctx.service.delete_pending_intent(open_id)
            name = suggest_name(description)
            await _create_project(
                ctx, open_id, task_type, description, name=name,
                chat_id=pending.chat_id, chat_type=pending.chat_type,
            )
        else:
            ctx.service.upsert_pending_intent(
                open_id,
                description=None,
                stage="awaiting_description",
                chat_id=ctx.chat_id,
                chat_type=ctx.chat_type,
            )
            await _send_card_message(ctx, 
                receive_id,
                f"已选择 {task_type}。请直接回复该项目的具体描述。",
                receive_id_type=receive_id_type,
            )
        return

    if option.startswith("restart_with_type:"):
        _, project_id, target_type = option.split(":", 2)
        project = ctx.service.get_project(project_id)
        if project is None:
            await _send_card_message(ctx, 
                receive_id, "项目不存在", receive_id_type=receive_id_type
            )
            return
        try:
            new_project = await ctx.service.restart_project_with_type(
                project_id, target_type, triggered_by="feishu"
            )
        except ValueError as exc:
            await _send_card_message(ctx, 
                receive_id,
                f"重新归类失败：{exc}",
                receive_id_type=receive_id_type,
            )
            return
        await ctx.orchestrator.tick(new_project.id)
        await _send_card_message(ctx, 
            receive_id,
            f"已取消原项目 {project_id}，用 {target_type} 重建为新项目 {new_project.id}",
            receive_id_type=receive_id_type,
        )
        return

    if option.startswith("status:"):
        project_id = option.split(":", 1)[1]
        await _show_status(ctx, open_id, project_id)
        return

    if option.startswith("bind:"):
        project_id = option.split(":", 1)[1]
        await _bind_project(ctx, open_id, project_id)
        return

    if option.startswith("switch:"):
        project_id = option.split(":", 1)[1]
        await _switch_project(ctx, open_id, project_id)
        return

    if option.startswith("bind_dir_select:"):
        directory = option.split(":", 1)[1]
        await _bind_directory(ctx, open_id, directory)
        return

    if option == "bind_dir_manual":
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        if not ctx.chat_id:
            await _send_card_message(ctx, 
                receive_id, "当前消息缺少会话标识，无法绑定目录。",
                receive_id_type=receive_id_type,
            )
            return
        ctx.service.upsert_pending_intent(
            open_id,
            description=None,
            stage="awaiting_bind_dir",
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
        )
        await _send_card_message(ctx, 
            receive_id,
            "请回复要绑定的目录绝对路径（例如 `/Users/you/projects/my-repo`）。",
            receive_id_type=receive_id_type,
        )
        return

    if option == "bind_dir_cancel":
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await _send_card_message(ctx, 
            receive_id, "已取消目录绑定。", receive_id_type=receive_id_type
        )
        return

    if option.startswith("bind_dir_for_create_select:"):
        directory = option.split(":", 1)[1]
        await _resume_create_with_bind_dir(ctx, open_id, directory=directory)
        return

    if option == "bind_dir_for_create_skip":
        await _resume_create_with_bind_dir(ctx, open_id, directory=None)
        return

    await _show_menu(ctx, open_id)


def _list_projects_for_user(
    ctx: FeishuHandlerContext, open_id: str
) -> list[dict[str, Any]]:
    """Return projects the user is bound to."""
    rows = ctx.service.session.exec(
        select(ProjectUser.project_id).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = [r for r in rows]
    if not project_ids:
        return []
    projects = ctx.service.session.exec(
        select(Project).where(Project.id.in_(project_ids))
    ).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "current_state": p.current_state,
            "status": p.status,
        }
        for p in projects
    ]


def _list_all_projects(ctx: FeishuHandlerContext) -> list[dict[str, Any]]:
    """Return all projects."""
    projects = ctx.service.session.exec(select(Project)).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "current_state": p.current_state,
            "status": p.status,
        }
        for p in projects
    ]
