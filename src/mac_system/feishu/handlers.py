"""Shared Feishu event handlers used by both Webhook and WebSocket transports."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlmodel import select

from mac_system.core.intent import route_demand, suggest_name
from mac_system.core.project_service import (
    ApprovalNotFoundError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.card import (
    build_feedback_card,
    build_input_card,
    build_question_batch_card,
    build_rejection_summary_card,
    build_status_card,
    build_transition_card,
)
from mac_system.feishu.menu import (
    build_create_prompt_card,
    build_intent_confirm_card,
    build_main_menu_card,
    build_project_list_card,
    build_restart_type_card,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import Approval, Project, ProjectUser, RouteDecision
from mac_system.state_machine import StateMachine

logger = logging.getLogger(__name__)


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
    if pending is not None and not text.startswith("/"):
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
        await ctx.bridge.send_text(
            receive_id, f"你的 open_id 是：{open_id}", receive_id_type=receive_id_type
        )
        return

    if text == "/leave-project":
        await _leave_project(ctx, open_id)
        return

    if text.startswith("/bind-dir "):
        await _bind_directory(ctx, open_id, text[len("/bind-dir "):].strip())
        return

    if text == "/unbind-dir":
        await _unbind_directory(ctx, open_id)
        return

    if text == "/current-dir":
        await _show_current_directory(ctx, open_id)
        return

    if text.startswith("/bind "):
        project_id = text[len("/bind "):].strip()
        await _bind_project(ctx, open_id, project_id)
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
                await _create_project(ctx, open_id, task_type, description)
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

    if text.startswith("/use "):
        project_id = text[len("/use "):].strip()
        await _use_project(ctx, open_id, project_id)
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
        kind = _infer_input_kind(text)
        await ctx.service.create_pending_user_input(
            project_id=active_project.id,
            sender_id=open_id,
            observed_state=active_project.current_state,
            content=text,
            kind=kind,
            source_chat_id=ctx.chat_id,
        )
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await ctx.bridge.send_text(
            receive_id,
            "已记录你的输入，当前 Agent 处理完毕后会自动消费。",
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
            await ctx.bridge._send_interactive(
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
        await ctx.bridge.send_text(
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
            await ctx.bridge.update_card(msg.parent_id, answered_card)
        except Exception:
            logger.exception("Failed to update input card to answered state")

    if ctx.service.interaction_is_resolvable(interaction.id):
        await ctx.service.resolve_question_interaction(interaction.id, commit=False)
        await ctx.service.respond_approval(
            approval.id, "answer", msg.open_id, commit=False
        )
        ctx.service.session.commit()
        await ctx.service.transition_state(
            project.id,
            "answer",
            payload={"resume_state": interaction.resume_state},
            triggered_by=msg.open_id,
        )
        await ctx.bridge.send_text(
            receive_id,
            "已收到你的回答，Agent 继续执行。",
            receive_id_type=receive_id_type,
        )
        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        ctx.service.session.commit()
        # Send the next pending free-text question card, if any.
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await ctx.bridge.send_text(
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
    next_q = next(
        (
            q for q in questions
            if q.status == "pending" and q.allow_free_text and not q.options
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
    message_id = await ctx.bridge._send_interactive(
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
        await ctx.bridge.send_text(receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type)
        return None
    if interaction_id is None:
        interaction_id = approval.payload.get("question_interaction_id")
    if not interaction_id or not target_question_id:
        await ctx.bridge.send_text(receive_id, "未找到对应的问题交互", receive_id_type=receive_id_type)
        return None
    interaction = ctx.service.get_question_interaction(interaction_id)
    if interaction is None or interaction.status != "pending":
        await ctx.bridge.send_text(receive_id, "该问题交互已处理或不存在", receive_id_type=receive_id_type)
        return None
    project = ctx.service.get_project(interaction.project_id)
    if project is None:
        await ctx.bridge.send_text(receive_id, "关联项目不存在", receive_id_type=receive_id_type)
        return None
    if not _is_authorized_for_project(ctx, open_id, project.id):
        await ctx.bridge.send_text(
            receive_id,
            f"你没有权限回答项目 {project.id} 的问题。",
            receive_id_type=receive_id_type,
        )
        return None

    questions = ctx.service.get_questions_for_interaction(interaction_id)
    target = next((q for q in questions if q.question_id == target_question_id and q.allow_free_text), None)
    if target is None:
        await ctx.bridge.send_text(receive_id, "未找到对应问题", receive_id_type=receive_id_type)
        return None
    if target.kind != "non_blocking":
        await ctx.bridge.send_text(receive_id, "该问题不能跳过", receive_id_type=receive_id_type)
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
            await ctx.bridge.update_card(action.message_id, skipped_card)
        except Exception:
            logger.exception("Failed to update input card to skipped state")

    if ctx.service.interaction_is_resolvable(interaction_id):
        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
        await ctx.service.respond_approval(approval_id, "answer", open_id, commit=False)
        ctx.service.session.commit()
        await ctx.service.transition_state(
            interaction.project_id, "answer",
            payload={"resume_state": interaction.resume_state},
            triggered_by=open_id,
        )
        await ctx.bridge.send_text(receive_id, "已收到你的回答，Agent 继续执行。", receive_id_type=receive_id_type)
        project = ctx.service.get_project(interaction.project_id)
        if project is not None:
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await ctx.bridge.send_text(
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
    await ctx.bridge._send_interactive(
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
            await ctx.bridge.send_text(
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
            await ctx.bridge.send_text(
                receive_id, "没有可终止的活跃项目。", receive_id_type=receive_id_type
            )
            return
        project_id = latest.id

    if not _is_authorized_for_project(ctx, open_id, project_id):
        await ctx.bridge.send_text(
            receive_id,
            f"你没有权限终止项目 {project_id}。",
            receive_id_type=receive_id_type,
        )
        return

    try:
        await ctx.orchestrator.cancel_project(project_id, reason="用户通过飞书命令终止")
    except ProjectNotFoundError:
        await ctx.bridge.send_text(
            receive_id, f"项目 {project_id} 不存在。", receive_id_type=receive_id_type
        )
        return
    except Exception as exc:
        logger.exception("Failed to cancel project %s from Feishu", project_id)
        await ctx.bridge.send_text(
            receive_id,
            f"终止项目 {project_id} 失败：{exc}",
            receive_id_type=receive_id_type,
        )
        return

    await ctx.bridge.send_text(
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
) -> None:
    """Shared logic for card actions and text commands responding to an approval."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        await ctx.bridge.send_text(
            receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type
        )
        return

    project = ctx.service.get_project(approval.project_id)
    if project is None:
        await ctx.bridge.send_text(
            receive_id, "项目不存在", receive_id_type=receive_id_type
        )
        return

    # Authorization: the user must be bound to the project, unless the project
    # has no bindings yet (e.g. API-created test projects).
    bindings = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.project_id == project.id)
    ).all()
    if bindings and not _is_authorized_for_project(ctx, open_id, project.id):
        await ctx.bridge.send_text(
            receive_id,
            f"你没有权限操作项目 {project.id} 的审批。",
            receive_id_type=receive_id_type,
        )
        return

    # Input cards use a button that just signals the user has typed a reply.
    if chosen == "answered":
        await ctx.bridge.send_text(
            receive_id,
            "请用 /input <approval_id> <你的回答> 提交答案",
            receive_id_type=receive_id_type,
        )
        return

    # Restart with a different task type needs a secondary type-selection card.
    if chosen == "restart_with_type":
        await ctx.bridge._send_interactive(
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

    # Confirm the user's choice with a card before continuing.
    feedback_card = build_feedback_card(
        project_name=project.name or project.id,
        selected_label=selected_label,
        is_rejection=is_rejection,
        note="项目继续执行。" if not is_rejection else "",
    )
    await ctx.bridge._send_interactive(
        receive_id, feedback_card, receive_id_type=receive_id_type
    )

    from_state = project.current_state

    # If feedback is supplied with a rejection, create the feedback packet in
    # the same transaction as the approval response + state transition.
    if is_rejection and feedback_text:
        source_stage = _confirmation_source_stage(from_state)
        await ctx.service.create_feedback_packet(
            project_id=project.id,
            source_stage=source_stage,
            source_task_id=approval.task_id,
            source_round=approval.payload.get("round_number", 1),
            kind="human_rejection",
            summary=feedback_text,
            blocking=True,
            failure_class="human_rejection",
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
            project.context = {
                **project.context,
                "latest_rejection_feedback": feedback_text,
            }
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
            await ctx.bridge._send_interactive(
                receive_id, summary_card, receive_id_type=receive_id_type
            )
            logger.info(
                "Entering _advance_until_confirm for %s after reject → %s",
                project.id, project.current_state,
            )
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
            return

        # No feedback: tell the user we are waiting for their input.
        project = ctx.service.require_project(project.id)
        sm = StateMachine(project.task_type)
        to_display = sm.display_name(from_state)
        await ctx.bridge.send_text(
            receive_id,
            f"已驳回，请直接回复修改意见。",
            receive_id_type=receive_id_type,
        )
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
    await ctx.bridge._send_interactive(
        receive_id, transition_card, receive_id_type=receive_id_type
    )

    logger.info(
        "Entering _advance_until_confirm for %s after confirm → %s",
        project.id, project.current_state,
    )
    # Tick repeatedly so cards are sent for every confirmation stage.
    await _advance_until_confirm(ctx, project, receive_id, receive_id_type)


async def _list_my_projects(ctx: FeishuHandlerContext, open_id: str) -> None:
    """List projects bound to the user."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if not project_ids:
        await ctx.bridge.send_text(
            receive_id, "你还没有任何项目。", receive_id_type=receive_id_type
        )
        return
    projects = ctx.service.session.exec(
        select(Project).where(Project.id.in_(project_ids)).order_by(Project.updated_at.desc())
    ).all()
    lines = [f"你的项目（共 {len(projects)} 个）："]
    for p in projects:
        lines.append(f"- {p.id} {p.name or '(未命名)'} [{p.status}] {p.current_state}")
    await ctx.bridge.send_text(
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
        "/use <项目ID>    — 将某项目设为当前会话默认项目\n"
        "/current         — 查看当前会话默认项目\n"
        "/leave-project   — 清除当前会话默认项目\n"
        "/bind-dir <绝对路径> — 将当前会话绑定到已有代码目录\n"
        "/current-dir     — 查看当前会话绑定目录\n"
        "/unbind-dir      — 解绑目录（不影响现有项目）\n"
        "/status <项目ID> — 查看项目状态\n"
        "/bind <项目ID>   — 绑定到项目\n"
        "/menu            — 打开菜单\n"
        "/help            — 显示本帮助\n\n"
        "直接发消息不会创建项目；如需创建请使用 /create。"
    )
    await ctx.bridge.send_text(receive_id, help_text, receive_id_type=receive_id_type)


async def _bind_directory(
    ctx: FeishuHandlerContext, open_id: str, directory: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await ctx.bridge.send_text(
            receive_id, "当前消息缺少会话标识，无法绑定目录。",
            receive_id_type=receive_id_type,
        )
        return
    try:
        binding = ctx.service.bind_directory_to_chat(
            ctx.chat_id, directory, bound_by=open_id
        )
    except (ValueError, OSError) as exc:
        await ctx.bridge.send_text(
            receive_id, f"绑定目录失败：{exc}", receive_id_type=receive_id_type
        )
        return
    await ctx.bridge.send_text(
        receive_id,
        f"已将当前会话绑定到 `{binding.directory}`。后续新项目会直接修改该仓库；已有项目不受影响。",
        receive_id_type=receive_id_type,
    )


async def _unbind_directory(ctx: FeishuHandlerContext, open_id: str) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await ctx.bridge.send_text(
            receive_id, "当前消息缺少会话标识，无法解绑目录。",
            receive_id_type=receive_id_type,
        )
        return
    removed = ctx.service.unbind_directory_from_chat(ctx.chat_id)
    message = (
        "已解绑当前会话目录；后续新项目将使用系统工作区，已有项目不受影响。"
        if removed else "当前会话没有绑定目录。"
    )
    await ctx.bridge.send_text(
        receive_id, message, receive_id_type=receive_id_type
    )


async def _show_current_directory(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    binding = (
        ctx.service.get_chat_directory_binding(ctx.chat_id)
        if ctx.chat_id else None
    )
    message = (
        f"当前会话绑定目录：`{binding.directory}`"
        if binding else "当前会话没有绑定目录。"
    )
    await ctx.bridge.send_text(
        receive_id, message, receive_id_type=receive_id_type
    )


async def _leave_project(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Clear the active project for this user in the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await ctx.bridge.send_text(
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    ctx.service.set_chat_user_context(ctx.chat_id, open_id, None)
    await ctx.bridge.send_text(
        receive_id,
        "已清除当前会话的默认项目。",
        receive_id_type=receive_id_type,
    )


async def _use_project(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> None:
    """Set the active project for this user in the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project = ctx.service.get_project(project_id)
    if project is None:
        await ctx.bridge.send_text(
            receive_id, f"项目 {project_id} 不存在", receive_id_type=receive_id_type
        )
        return
    if ctx.chat_id is None:
        await ctx.bridge.send_text(
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

    await ctx.bridge.send_text(
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
    """Show the active project for this user in the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await ctx.bridge.send_text(
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    project = ctx.service.get_chat_user_active_project(ctx.chat_id, open_id)
    if project is None:
        await ctx.bridge.send_text(
            receive_id,
            "当前会话没有默认项目。可用 `/use <项目ID>` 或 `/projects` 选择。",
            receive_id_type=receive_id_type,
        )
        return
    await ctx.bridge.send_text(
        receive_id,
        f"当前默认项目：{project.id} {project.name or ''} [{project.status}] {project.current_state}",
        receive_id_type=receive_id_type,
    )


async def _list_chat_projects(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """List projects bound to the current chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await ctx.bridge.send_text(
            receive_id, "当前会话缺少 chat 上下文。", receive_id_type=receive_id_type
        )
        return
    projects = ctx.service.list_projects_for_chat(ctx.chat_id)
    if not projects:
        await ctx.bridge.send_text(
            receive_id, "当前会话还没有绑定任何项目。", receive_id_type=receive_id_type
        )
        return
    lines = [f"当前会话的项目（共 {len(projects)} 个）："]
    for p in projects:
        marker = "* " if p.status == "active" else "  "
        lines.append(
            f"{marker}{p.id} {p.name or '(未命名)'} [{p.status}] {p.current_state}"
        )
    lines.append("\n使用 `/use <项目ID>` 切换默认项目")
    await ctx.bridge.send_text(
        receive_id, "\n".join(lines), receive_id_type=receive_id_type
    )


async def _show_menu(ctx: FeishuHandlerContext, open_id: str) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    await ctx.bridge._send_interactive(
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
        # Only consider recently updated (within 10 min).
        from datetime import timedelta

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


def _infer_input_kind(text: str) -> str:
    """Classify a free-text user message into pending input kind."""
    lowered = text.lower()
    correction_markers = ["改", "不对", "错了", "重新", "修正", "需要修改", "rollback", "reject"]
    scope_markers = ["范围", "增加", "去掉", "补充", "不要", "加上", "scope"]
    if any(m in lowered for m in correction_markers):
        return "correction"
    if any(m in lowered for m in scope_markers):
        return "scope_change"
    return "question"


def _find_pending_input_approval(
    ctx: FeishuHandlerContext, open_id: str
) -> Any | None:
    """Return the user's single pending human_input approval, if unique."""
    user_projects = ctx.service.session.exec(
        select(ProjectUser).where(ProjectUser.open_id == open_id)
    ).all()
    project_ids = {up.project_id for up in user_projects}
    if not project_ids:
        return None
    approvals = ctx.service.session.exec(
        select(Approval)
        .where(Approval.project_id.in_(project_ids))
        .where(Approval.approval_type == "input")
        .where(Approval.status == "pending")
        .order_by(Approval.created_at.desc())
    ).all()
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
    project.context = {**project.context, "latest_rejection_feedback": text}
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
    await ctx.bridge._send_interactive(
        receive_id, summary_card, receive_id_type=receive_id_type
    )
    await _advance_until_confirm(ctx, project, receive_id, receive_id_type)


async def _bind_project(
    ctx: FeishuHandlerContext, open_id: str, project_id: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    project = ctx.service.get_project(project_id)
    if project is None:
        await ctx.bridge.send_text(
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

    await ctx.bridge.send_text(
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
        await ctx.bridge.send_text(
            receive_id, f"项目 {project_id} 不存在", receive_id_type=receive_id_type
        )
        return

    pending = status.get("pending_approval")
    pending_text = ""
    if pending:
        pending_text = f"\n待处理审批：{pending['approval_id']} ({pending['type']})"

    summary = (
        f"项目：{status['project_id']}\n"
        f"状态：{status['display']} ({status['current_state']})\n"
        f"健康度：{status['status']}{pending_text}"
    )
    await ctx.bridge.send_text(receive_id, summary, receive_id_type=receive_id_type)

    card = build_status_card(
        title=f"[{project_id}] 项目状态",
        project_id=project_id,
        current_state=status["current_state"],
        display=status["display"],
        recent_events=[
            {"level": e["level"], "message": e["message"]}
            for e in status.get("recent_events", [])
        ],
    )
    await ctx.bridge._send_interactive(
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
        )
    except FileNotFoundError:
        await ctx.bridge.send_text(
            receive_id, f"未知任务类型：{task_type}", receive_id_type=receive_id_type
        )
        return
    except Exception as exc:
        logger.exception("Failed to create project from Feishu")
        await ctx.bridge.send_text(
            receive_id, f"创建项目失败：{exc}", receive_id_type=receive_id_type
        )
        return

    # Acknowledge immediately so the user/chat sees a project id without
    # waiting for the (potentially long) first agent tick to complete.
    await ctx.bridge.send_text(
        receive_id,
        f"已创建项目 {project.id}（{task_type}），Agent 开始执行，后续状态变化将通过卡片通知。",
        receive_id_type=receive_id_type,
    )
    try:
        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    except Exception as exc:
        logger.exception("Orchestrator tick failed after Feishu project creation")
        await ctx.bridge.send_text(
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
    # If the caller already starts at a human boundary, exactly one tick is
    # enough to create or reuse its approval. A second tick used to expire the
    # just-created approval and generate a duplicate card.
    sm = StateMachine(project.task_type)
    if sm.state_type(project.current_state) in ("human_confirmation", "human_input"):
        await ctx.orchestrator.tick(project.id)
        return

    last_state: str | None = project.current_state
    for _ in range(max_ticks):
        logger.info(
            "  _advance_until_confirm tick %s (state=%s)", project.id, project.current_state,
        )
        await ctx.orchestrator.tick(project.id)
        project = ctx.service.require_project(project.id)
        if project.current_state == last_state:
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
            # orchestrator creates the approval and sends the card.
            logger.info(
                "  _advance_until_confirm reached %s for %s, sending card",
                project.current_state, project.id,
            )
            await ctx.orchestrator.tick(project.id)
            return
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
        await ctx.bridge.send_text(
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
        await ctx.bridge._send_interactive(
            receive_id,
            build_intent_confirm_card(
                decision.task_type,
                chat_id=effective_chat_id,
                chat_type=effective_chat_type,
            ),
            receive_id_type=receive_id_type,
        )
        return

    name = suggest_name(description)
    await _create_project(
        ctx, open_id, decision.task_type, description, name=name,
        chat_id=effective_chat_id, chat_type=effective_chat_type,
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
    ctx: FeishuHandlerContext, open_id: str, rest: str
) -> None:
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    parts = rest.split(None, 1)

    if len(parts) >= 2 and len(parts[0]) >= 12:
        approval_id, answer = parts
    else:
        # Try to find a single pending human_input approval for this user.
        pending = _find_pending_input_approval(ctx, open_id)
        if pending is None:
            await ctx.bridge.send_text(
                receive_id,
                "格式错误。用法：/input <approval_id> <你的回答>\n"
                "或当前没有待输入的审批。",
                receive_id_type=receive_id_type,
            )
            return
        approval_id = pending.id
        answer = rest

    try:
        approval = ctx.service.get_approval(approval_id)
    except ApprovalNotFoundError:
        await ctx.bridge.send_text(
            receive_id,
            f"审批 {approval_id} 不存在或已过期",
            receive_id_type=receive_id_type,
        )
        return

    project = ctx.service.get_project(approval.project_id)
    if project is None:
        await ctx.bridge.send_text(
            receive_id, "关联项目不存在", receive_id_type=receive_id_type
        )
        return

    # REWORK answers are option keys (e.g. "to_coding"); other inputs are free text.
    if approval.state == "REWORK":
        valid_actions = {opt.get("action") for opt in approval.payload.get("options", [])}
        if answer in valid_actions:
            event = answer
            payload = None
        else:
            await ctx.bridge.send_text(
                receive_id,
                f"无效选项：{answer}。可用选项：{', '.join(sorted(valid_actions))}",
                receive_id_type=receive_id_type,
            )
            return
    else:
        # QuestionInteraction: if the approval has a linked interaction, route
        # the answer to the first pending question (free-text answer).
        interaction_id = approval.payload.get("question_interaction_id")
        if interaction_id:
            interaction = ctx.service.get_question_interaction(interaction_id)
            if interaction and interaction.status == "pending":
                pending_qs = [
                    q for q in ctx.service.get_questions_for_interaction(interaction_id)
                    if q.status == "pending"
                ]
                if pending_qs:
                    q = pending_qs[0]
                    await ctx.service.submit_question_answer(q.id, answer, answer, False, open_id, commit=False)
                    if ctx.service.interaction_is_resolvable(interaction_id):
                        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
                        await ctx.service.respond_approval(approval.id, "answer", open_id, commit=False)
                        ctx.service.session.commit()
                        await ctx.service.transition_state(
                            project.id, "answer",
                            payload={"resume_state": interaction.resume_state},
                            triggered_by=open_id,
                        )
                        await ctx.bridge.send_text(receive_id, "已收到你的回答，Agent 继续执行。", receive_id_type=receive_id_type)
                        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
                        return
                    ctx.service.session.commit()
                    await ctx.bridge.send_text(receive_id, "已记录该问题的回答，还有其他问题待回答。", receive_id_type=receive_id_type)
                    return
        event = "answer"
        payload = {"clarification_answer": answer}

    await ctx.service.resolve_approval(
        approval.id, event, responded_by=open_id, payload=payload
    )
    await ctx.orchestrator.tick(project.id)
    await ctx.bridge.send_text(
        receive_id,
        f"已提交，项目 {project.id} 继续执行",
        receive_id_type=receive_id_type,
    )


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

    # Question batch card actions (unified ask_user).
    if action.action in (
        "answer_all_recommended",
        "answer_with_values",
        "skip_remaining",
        "answer_question_option",
        "select_question_option",
        "submit_selected_options",
    ):
        return await _handle_question_batch_action(ctx, action)

    if action.action == "skip_free_text":
        return await _handle_skip_free_text(ctx, action)

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
        )
        return

    chosen = action.action
    if chosen not in ("confirm", "reject"):
        chosen = action.option_key or chosen
    await _respond_to_approval(ctx, open_id, action.approval_id, chosen)


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
        await ctx.bridge.send_text(receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type)
        return
    if interaction_id is None:
        interaction_id = approval.payload.get("question_interaction_id")
    if not interaction_id:
        await ctx.bridge.send_text(receive_id, "未找到对应的问题交互", receive_id_type=receive_id_type)
        return
    interaction = ctx.service.get_question_interaction(interaction_id)
    if interaction is None or interaction.status != "pending":
        await ctx.bridge.send_text(receive_id, "该问题交互已处理或不存在", receive_id_type=receive_id_type)
        return
    project = ctx.service.get_project(interaction.project_id)
    if project is None:
        await ctx.bridge.send_text(receive_id, "关联项目不存在", receive_id_type=receive_id_type)
        return
    if not _is_authorized_for_project(ctx, open_id, project.id):
        await ctx.bridge.send_text(
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
    # Snapshot question data before any commit, because SQLAlchemy expires
    # ORM objects after commit and model_dump() would return empty dicts.
    pending_question_dicts = [q.model_dump() for q in pending_questions]

    if action.action == "select_question_option":
        target_question_id = action.extras.get("question_id") if action.extras else None
        option_key = action.option_key
        target = next((q for q in pending_question_dicts if q.get("question_id") == target_question_id), None)
        if target is None or option_key is None:
            ctx.service.session.commit()
            await ctx.bridge.send_text(receive_id, "未找到对应问题或选项", receive_id_type=receive_id_type)
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
            await ctx.bridge.send_text(receive_id, "未找到对应问题或选项", receive_id_type=receive_id_type)
            return
        answer_label = option_key
        for opt in target.options:
            if opt.get("key") == option_key:
                answer_label = opt.get("label", option_key)
                break
        await ctx.service.submit_question_answer(target.id, option_key, answer_label, False, open_id, commit=False)
    elif action.action == "submit_selected_options":
        selected_options = dict(approval.payload.get("selected_options", {}))
        for q in pending_questions:
            answer = selected_options.get(q.question_id) or q.recommended_option
            if answer is None:
                continue
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            await ctx.service.submit_question_answer(q.id, answer, answer_label, False, open_id, commit=False)
    elif action.action == "answer_all_recommended":
        for q in pending_questions:
            answer = q.recommended_option
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            await ctx.service.submit_question_answer(q.id, answer, answer_label, False, open_id, commit=False)
    elif action.action == "answer_with_values":
        for q in pending_questions:
            field_name = f"q_{q.question_id}"
            answer = (action.extras or {}).get(field_name) if action.extras else None
            if answer is None:
                answer = q.recommended_option
            answer_label = answer
            for opt in q.options:
                if opt.get("key") == answer:
                    answer_label = opt.get("label", answer)
                    break
            await ctx.service.submit_question_answer(q.id, answer, answer_label, False, open_id, commit=False)
    elif action.action == "skip_remaining":
        for q in pending_questions:
            if q.kind in ("blocking", "decision"):
                continue
            await ctx.service.submit_question_answer(q.id, None, None, True, open_id, commit=False)

    # Update the choice card to a read-only "已提交" receipt state showing the
    # recorded answers. This runs for any finalize action regardless of whether
    # the full interaction resolves, so the user sees their choices are locked.
    if action.action in ("submit_selected_options", "skip_remaining", "answer_all_recommended"):
        submitted_answers: dict[str, Any] = {}
        for q in pending_questions:
            answer_record = ctx.service.get_question_answer(q.id)
            if answer_record and not answer_record.skipped and answer_record.answer:
                submitted_answers[q.question_id] = answer_record.answer
        receipt_card = build_question_batch_card(
            approval_id=approval_id,
            interaction_id=interaction_id,
            questions=pending_question_dicts,
            agent=interaction.agent,
            stage=interaction.stage,
            state=approval.state,
            project_name=project.name or project.id,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            submitted_answers=submitted_answers,
        )
        batch_message_id = action.message_id or approval.payload.get("question_batch_message_id")
        if batch_message_id:
            try:
                await ctx.bridge.update_card(batch_message_id, receipt_card)
            except Exception:
                logger.exception("Failed to update question batch card to submitted state")

    # Determine what's still pending after recording answers.
    all_questions = ctx.service.get_questions_for_interaction(interaction_id)
    pending_choice_qs = [q for q in all_questions if q.status == "pending" and q.options]
    pending_free_text_qs = [q for q in all_questions if q.status == "pending" and not q.options]

    if pending_choice_qs:
        # Still have choice questions to answer; don't move on yet.
        ctx.service.session.commit()
        if action.action == "answer_question_option":
            await ctx.bridge.send_text(receive_id, "已记录选择，请继续回答其他问题。", receive_id_type=receive_id_type)
        else:
            await ctx.bridge.send_text(receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)
    elif pending_free_text_qs:
        # All choices answered but free-text questions remain. Send the first
        # free-text card; the interaction resolves after the user answers or
        # skips all of them.
        ctx.service.session.commit()
        sent_next = await _send_next_free_text_card(ctx, approval, interaction, project, receive_id, receive_id_type)
        if not sent_next:
            await ctx.bridge.send_text(receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)
    elif ctx.service.interaction_is_resolvable(interaction_id):
        await ctx.service.resolve_question_interaction(interaction_id, commit=False)
        resume_state = interaction.resume_state
        await ctx.service.respond_approval(approval_id, "answer", open_id, commit=False)
        ctx.service.session.commit()
        await ctx.service.transition_state(
            interaction.project_id, "answer",
            payload={"resume_state": resume_state},
            triggered_by=open_id,
        )
        await ctx.bridge.send_text(receive_id, "已收到你的回答，Agent 继续执行。", receive_id_type=receive_id_type)
        project = ctx.service.get_project(interaction.project_id)
        if project is not None:
            await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    else:
        ctx.service.session.commit()
        await ctx.bridge.send_text(receive_id, "还有未回答的阻塞问题，请继续回答。", receive_id_type=receive_id_type)


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
        await ctx.bridge._send_interactive(
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
        await ctx.bridge._send_interactive(
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
            await ctx.bridge.send_text(
                receive_id,
                f"已选择 {task_type}。请直接回复该项目的具体描述。",
                receive_id_type=receive_id_type,
            )
        return

    if option.startswith("restart_with_type:"):
        _, project_id, target_type = option.split(":", 2)
        project = ctx.service.get_project(project_id)
        if project is None:
            await ctx.bridge.send_text(
                receive_id, "项目不存在", receive_id_type=receive_id_type
            )
            return
        try:
            new_project = await ctx.service.restart_project_with_type(
                project_id, target_type, triggered_by="feishu"
            )
        except ValueError as exc:
            await ctx.bridge.send_text(
                receive_id,
                f"重新归类失败：{exc}",
                receive_id_type=receive_id_type,
            )
            return
        await ctx.orchestrator.tick(new_project.id)
        await ctx.bridge.send_text(
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
