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

from mac_system.core.intent import route_demand, suggest_name
from mac_system.core.project_service import (
    ApprovalNotFoundError,
    ProjectNotFoundError,
    ProjectService,
)
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.card import (
    build_content_card,
    build_conversation_clarification_card,
    build_conversation_progress_card,
    build_feedback_card,
    build_input_card,
    build_question_batch_card,
    build_rejection_input_card,
    build_rejection_summary_card,
    build_status_card,
    build_transition_card,
)
from mac_system.utils.formatting import format_tool_preview
from mac_system.feishu.menu import (
    build_bind_dir_for_create_card,
    build_create_prompt_card,
    build_directory_candidate_card,
    build_intent_confirm_card,
    build_main_menu_card,
    build_pending_action_card,
    build_project_list_card,
    build_restart_type_card,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import (
    Approval,
    ConversationSession,
    PendingConversationAction,
    Project,
    ProjectUser,
    RouteDecision,
)
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
        # The real coordinator gets first refusal so a user can ask a question
        # or pause instead of having every sentence consumed as a description.
        # Test/legacy orchestrators without the coordinator keep the old path.
        if not text.startswith("/") and not callable(
            getattr(ctx.orchestrator, "run_chat_scoped_agent", None)
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
        await ctx.bridge.send_text(
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
            kind = _infer_input_kind(text)
            await ctx.service.create_pending_user_input(
                project_id=active_project.id,
                sender_id=open_id,
                observed_state=active_project.current_state,
                content=text,
                kind=kind,
                source_chat_id=ctx.chat_id,
            )
            await ctx.bridge.send_text(
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
            await ctx.bridge.send_text(
                receive_id,
                f"项目正在等待确认，请使用 `/confirm {pending_approval.id}` 或 "
                f"`/reject {pending_approval.id} <修改意见>`。",
                receive_id_type=receive_id_type,
            )
        else:
            await ctx.bridge.send_text(
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
            await ctx.bridge.update_card(progress_message_id, progress_card)
        else:
            progress_message_id = await ctx.bridge._send_interactive(
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
    if active_project is None and conversation.active_project_id:
        active_project = ctx.service.get_project(conversation.active_project_id)

    approvals: list[dict[str, Any]] = []
    if active_project is not None:
        approvals = [
            {
                "id": approval.id,
                "type": approval.approval_type,
                "state": approval.state,
                "question": approval.question,
            }
            for approval in ctx.service.session.exec(
                select(Approval)
                .where(
                    Approval.project_id == active_project.id,
                    Approval.status == "pending",
                )
                .order_by(Approval.created_at.desc())
            ).all()
        ]
    pending_actions = [
        {
            "id": action.id,
            "type": action.action_type,
            "payload": action.payload,
        }
        for action in ctx.service.get_session_pending_actions(conversation.id)
    ]
    pending_intent = ctx.service.get_pending_intent(open_id)

    binding = ctx.service.get_chat_directory_binding(ctx.chat_id) if ctx.chat_id else None
    workspace = Path(binding.directory) if binding else None
    if workspace is None and active_project is not None:
        workspace = Path(active_project.workspace_path)

    active_project_data = (
        {
            "id": active_project.id,
            "name": active_project.name,
            "task_type": active_project.task_type,
            "status": active_project.status,
            "current_state": active_project.current_state,
        }
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
    }
    inspect_markers = (
        "看下最新", "看看最新", "检查代码", "检查文档", "查看代码",
        "查看文档", "review code", "inspect workspace",
    )
    coordinator_worktree = (
        workspace
        if workspace and workspace.exists() and any(marker in msg.text.lower() for marker in inspect_markers)
        else None
    )
    execution_markers = (
        "开始开发", "开始实现", "开始修复", "按刚才", "按这个做",
        "着手开发", "执行这个方案",
    )
    wants_execution = any(marker in msg.text for marker in execution_markers)
    coordinator_started = time.monotonic()

    async def _coordinator_progress(event_type: str, content: str) -> None:
        """Preserve the same real thinking/tool history used by project Agents."""
        try:
            if event_type == "thinking":
                title = "对话协调 · 思考中"
                body = content
                color = "blue"
            elif event_type == "tool":
                data = json.loads(content)
                tool_name = data.get("name") or "工具"
                tool_input = data.get("input") or {}
                body = f"⚙️ {format_tool_preview(tool_name, tool_input)}"
                title = "对话协调 · 正在执行"
                color = "purple"
            else:
                return
            await ctx.bridge.send_content_card(
                open_id=receive_id,
                title=title,
                content=body,
                color=color,
                receive_id_type=receive_id_type,
            )
        except Exception:
            logger.warning("Failed to send Coordinator progress event", exc_info=True)

    # Run the coordinator with one automatic retry so transient API errors
    # do not immediately surface as failures to the user.
    output: dict[str, Any] | None = None
    failure_reason = "未返回有效结果"
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            output = await runner(
                agent_type="conversation_coordinator",
                stage=None,
                input_data={"message": msg.text},
                extra_variables=extra_variables,
                worktree=coordinator_worktree,
                progress_callback=_coordinator_progress,
            )
            if output and output.get("success"):
                break
            if output:
                failure_reason = output.get("error", "协调器返回失败")
                logger.warning(
                    "Conversation coordinator returned success=false chat=%s message=%s attempt=%s output=%s",
                    chat_key,
                    msg.message_id,
                    attempt,
                    json.dumps(output, ensure_ascii=False),
                )
            else:
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
        if attempt < max_attempts and progress_message_id:
            try:
                await ctx.bridge.update_card(
                    progress_message_id,
                    build_conversation_progress_card(
                        stage="retrying",
                        detail=failure_reason,
                    ),
                )
            except Exception:
                logger.warning("Failed to update retry progress card", exc_info=True)
    if progress_refresh_task is not None:
        progress_refresh_task.cancel()
        await asyncio.gather(progress_refresh_task, return_exceptions=True)
    coordinator_success = bool(output and output.get("success"))
    logger.info(
        "Conversation coordinator finished chat=%s message=%s elapsed=%.2fs success=%s attempts=%s",
        chat_key,
        msg.message_id,
        time.monotonic() - coordinator_started,
        coordinator_success,
        attempt,
    )
    if progress_message_id:
        try:
            if coordinator_success:
                await ctx.bridge.update_card(
                    progress_message_id,
                    build_conversation_progress_card(
                        stage="completed", detail="调研与分析完成，结果已整理"
                    ),
                )
            else:
                await ctx.bridge.update_card(
                    progress_message_id,
                    build_conversation_progress_card(
                        stage="failed", detail=failure_reason
                    ),
                )
        except Exception:
            logger.warning("Failed to advance conversation progress card", exc_info=True)
    if not coordinator_success:
        if not wants_execution:
            output = {
                "success": True,
                "reply": f"处理失败（{failure_reason}），已自动重试 1 次仍无法完成。请重试一次，或把问题拆成更小的一步。",
                "intent": "unknown",
                "effect": "none",
                "commitment": "ambiguous",
                "needs_clarification": False,
                "updated_summary": conversation.summary or msg.text[:300],
                "proposed_action": {"type": "answer"},
            }
        else:
            output = {
                "success": True,
                "reply": f"处理失败（{failure_reason}），已自动重试 1 次仍无法完成。我已按你的原话整理为待确认执行，不会直接修改。",
                "intent": "start_delivery",
                "effect": "propose",
                "commitment": "proposed",
                "needs_clarification": False,
                "updated_summary": conversation.summary or msg.text[:300],
                "proposed_action": {
                    "type": "propose_start_delivery",
                    "task_type": "feature",
                    "description": f"处理失败（{failure_reason}），已自动重试 1 次仍无法完成。原始请求：{msg.text}",
                    "scope": [],
                    "out_of_scope": [],
                    "permissions": ["workspace_write"],
                    "acceptance_criteria": [],
                },
            }

    required_fields = {
        "reply", "intent", "effect", "commitment", "needs_clarification",
        "proposed_action",
    }
    if not required_fields.issubset(output):
        result_text = str(output.get("result_text") or "").strip()
        if wants_execution:
            output = {
                "success": True,
                "reply": "模型没有返回完整动作结构。我已按最小安全范围整理为待确认执行，不会直接修改。",
                "intent": "start_delivery",
                "effect": "propose",
                "commitment": "proposed",
                "needs_clarification": False,
                "updated_summary": conversation.summary or msg.text[:300],
                "proposed_action": {
                    "type": "propose_start_delivery",
                    "task_type": "feature",
                    "description": msg.text,
                    "scope": [],
                    "out_of_scope": [],
                    "permissions": ["workspace_write"],
                    "acceptance_criteria": [],
                },
            }
        elif result_text:
            output = {
                "success": True,
                "reply": result_text,
                "intent": "consult",
                "effect": "read_only",
                "commitment": "none",
                "needs_clarification": False,
                "updated_summary": conversation.summary or result_text[:300],
                "proposed_action": {"type": "answer"},
            }
        else:
            logger.warning("Conversation coordinator returned an invalid contract")
            output = {
                "success": True,
                "reply": "本次结果格式不完整，请重试一次。",
                "intent": "unknown",
                "effect": "none",
                "commitment": "ambiguous",
                "needs_clarification": False,
                "updated_summary": conversation.summary or msg.text[:300],
                "proposed_action": {"type": "answer"},
            }

    try:
        await _apply_conversation_result(
            ctx,
            msg,
            conversation,
            output,
            active_project=active_project,
            # Completed milestones stay visible. The result/decision is a new
            # card so users retain the execution history in the chat.
            progress_message_id=None,
        )
    finally:
        if reaction_id:
            try:
                await ctx.bridge.remove_reaction(msg.message_id, reaction_id)
            except Exception:
                logger.warning("Failed to remove processing reaction", exc_info=True)
    return True


async def _refresh_conversation_progress(
    ctx: FeishuHandlerContext, message_id: str
) -> None:
    """Keep a long Coordinator turn visibly alive without sending new messages."""
    started = time.monotonic()
    while True:
        await asyncio.sleep(10)
        elapsed = int(time.monotonic() - started)
        try:
            await ctx.bridge.update_card(
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
    progress_message_id: str | None = None,
) -> None:
    """Apply one schema-validated coordinator result through safe handlers."""
    open_id = msg.open_id
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    reply = str(output.get("reply") or "").strip()
    action = output.get("proposed_action") or {"type": "none"}
    action_type = action.get("type", "none")
    sent_message_id: str | None = None

    clarification = output.get("clarification") or {}
    clarification_options = clarification.get("options") or []
    if output.get("needs_clarification") and clarification_options:
        payload = {
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
        pending = ctx.service.create_pending_conversation_action(
            session_id=conversation.id,
            action_type="clarification",
            payload=payload,
            source_message_ids=[msg.message_id] if msg.message_id else [],
            idempotency_key=hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
            created_by=open_id,
        )
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
            message_id=progress_message_id,
        )
    elif action_type == "resolve_pending_action":
        pending = ctx.service.get_session_pending_actions(conversation.id)
        target_id = (output.get("target") or {}).get("id")
        if target_id:
            pending = [item for item in pending if item.id == target_id]
        if len(pending) != 1 or output.get("commitment") != "authorized":
            sent_message_id = await _send_conversation_reply(
                ctx, receive_id, receive_id_type,
                reply or "当前无法唯一确定要确认的执行动作，请从最新决策卡片中选择。",
                message_id=progress_message_id,
            )
        else:
            if reply:
                sent_message_id = await _send_conversation_reply(
                    ctx, receive_id, receive_id_type, reply,
                    message_id=progress_message_id,
                )
            await _execute_pending_conversation_action(
                ctx, open_id, pending[0].id, decision="confirm"
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
            sent_message_id = await _send_conversation_reply(
                ctx, receive_id, receive_id_type,
                reply or "当前无法唯一确定待处理审批，请从最新审批卡片中选择。",
                message_id=progress_message_id,
            )
        else:
            if reply:
                sent_message_id = await _send_conversation_reply(
                    ctx, receive_id, receive_id_type, reply,
                    message_id=progress_message_id,
                )
            extra = {"option_key": action.get("option_key")} if action.get("option_key") else None
            await _respond_to_approval(
                ctx,
                open_id,
                pending_approvals[0].id,
                decision,
                feedback_text=action.get("feedback"),
                extra=extra,
            )
    elif action_type == "propose_start_delivery":
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
            "decisions": action.get("decisions") or [],
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
    elif action_type == "query_status" and active_project is not None:
        if reply:
            sent_message_id = await _send_conversation_reply(
                ctx, receive_id, receive_id_type, reply, title="项目状态",
                message_id=progress_message_id,
            )
        await _show_status(ctx, open_id, active_project.id)
    elif action_type == "route_project_input" and active_project is not None:
        state_machine = StateMachine(active_project.task_type)
        running_state_tasks = [
            task
            for task in ctx.service._running_tasks(active_project.id)
            if task.state == active_project.current_state
        ]
        if state_machine.is_agent_execution(active_project.current_state) and running_state_tasks:
            intent = output.get("intent")
            kind = (
                "correction" if intent == "correct"
                else "scope_change" if intent == "change_scope"
                else "question"
            )
            await ctx.service.create_pending_user_input(
                project_id=active_project.id,
                sender_id=open_id,
                observed_state=active_project.current_state,
                content=msg.text,
                kind=kind,
                source_chat_id=ctx.chat_id,
            )
            sent_message_id = await _send_conversation_reply(
                ctx, receive_id, receive_id_type,
                reply or "已记录这项对当前执行有影响的输入，Agent 本轮结束后会处理。",
                message_id=progress_message_id,
            )
        else:
            sent_message_id = await _send_conversation_reply(
                ctx, receive_id, receive_id_type,
                reply or "当前没有正在运行的 Agent；我暂未修改项目状态。",
                message_id=progress_message_id,
            )
    else:
        sent_message_id = await _send_conversation_reply(
            ctx,
            receive_id,
            receive_id_type,
            reply or "我已经理解你的消息，但暂时没有需要执行的动作。",
            message_id=progress_message_id,
        )

    ctx.service.append_conversation_turn(
        conversation.chat_id,
        role="assistant",
        content=reply or f"[{action_type}]",
        message_id=sent_message_id,
    )
    updated_summary = output.get("updated_summary")
    latest_artifact = None
    if action_type in {"inspect_workspace", "create_draft", "research"}:
        latest_artifact = {
            "kind": action_type,
            "summary": updated_summary or reply[:500],
            "message_id": sent_message_id,
        }
    ctx.service.update_conversation_session(
        conversation,
        summary=updated_summary if isinstance(updated_summary, str) else None,
        latest_artifact=latest_artifact,
        active_project_id=active_project.id if active_project else None,
        update_active_project=active_project is not None,
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
    not truncate them.  The optional ``message_id`` is the in-progress card;
    we leave it as-is and post the real reply below it to avoid Card 1.0/2.0
    schema mismatch.
    """
    message_ids = await ctx.bridge.send_content_card(
        open_id=receive_id,
        title=title,
        content=text,
        receive_id_type=receive_id_type,
    )
    return message_ids[-1] if message_ids else ""


async def _deliver_conversation_card(
    ctx: FeishuHandlerContext,
    receive_id: str,
    receive_id_type: str,
    card: dict[str, Any],
    *,
    message_id: str | None = None,
) -> str:
    """Update the active Coordinator card, falling back to a new card."""
    if message_id:
        try:
            await ctx.bridge.update_card(message_id, card)
            return message_id
        except Exception:
            logger.warning("Failed to update conversation card", exc_info=True)
    return await ctx.bridge._send_interactive(
        receive_id, card, receive_id_type=receive_id_type
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
        await ctx.bridge.send_text(
            receive_id, "该执行请求不存在或已经清理。", receive_id_type=receive_id_type
        )
        return
    if action.status in {"resolved", "rejected", "expired"}:
        await ctx.bridge.send_text(
            receive_id, "该操作已经处理，无需重复点击。", receive_id_type=receive_id_type
        )
        return
    if action.created_by and action.created_by != open_id:
        await ctx.bridge.send_text(
            receive_id, "只有发起该执行请求的用户可以确认。", receive_id_type=receive_id_type
        )
        return
    if decision != "confirm":
        ctx.service.resolve_pending_conversation_action(
            action_id, decision="reject", resolved_by=open_id
        )
        await ctx.bridge.send_text(
            receive_id, "已保留当前讨论，不会开始修改。", receive_id_type=receive_id_type
        )
        return

    claimed = ctx.service.claim_pending_conversation_action(
        action_id, claimed_by=open_id
    )
    if claimed is None:
        await ctx.bridge.send_text(
            receive_id, "该操作正在处理或已经完成，请勿重复点击。", receive_id_type=receive_id_type
        )
        return
    if claimed.action_type != "start_delivery":
        ctx.service.finish_pending_conversation_action(action_id, status="pending")
        await ctx.bridge.send_text(
            receive_id, "暂不支持该操作类型。", receive_id_type=receive_id_type
        )
        return

    payload = dict(claimed.payload)
    source_conversation = ctx.service.session.get(
        ConversationSession, claimed.session_id
    )
    confirmed_brief = {
        "objective": payload.get("objective") or payload.get("description") or "",
        "scope": payload.get("scope") or [],
        "out_of_scope": payload.get("out_of_scope") or [],
        "permissions": payload.get("permissions") or [],
        "acceptance_criteria": payload.get("acceptance_criteria") or [],
        "decisions": payload.get("decisions") or [],
        "source_session_id": claimed.session_id,
        "source_message_ids": claimed.source_message_ids or [],
        # Structured decisions are authoritative. The bounded conversation
        # snapshot is retained as a fallback for details the coordinator did
        # not normalize, so the delivery Planner still sees confirmed context.
        "conversation_summary": source_conversation.summary if source_conversation else "",
        "recent_turns": list(source_conversation.recent_turns or [])
        if source_conversation
        else [],
    }
    try:
        project = await ctx.service.create_project(
            name=suggest_name(payload.get("description") or payload.get("objective") or "新项目"),
            task_type=payload.get("task_type", "feature"),
            description=payload.get("description") or payload.get("objective") or "",
            creator_open_id=open_id,
            feishu_chat_id=ctx.chat_id,
            feishu_chat_type=ctx.chat_type,
            initial_context={"confirmed_brief": confirmed_brief},
        )
    except Exception as exc:
        ctx.service.finish_pending_conversation_action(action_id, status="pending")
        logger.exception("Failed to create project from pending conversation action")
        await ctx.bridge.send_text(
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
    await ctx.bridge.send_text(
        receive_id,
        f"已创建项目 {project.id}（{project.name or '未命名'}），当前正在准备第一个交付阶段。",
        receive_id_type=receive_id_type,
    )
    try:
        await _advance_until_confirm(ctx, project, receive_id, receive_id_type)
    except Exception as exc:
        logger.exception("Orchestrator tick failed after conversation action")
        await ctx.bridge.send_text(
            receive_id, f"项目已创建，但启动失败：{exc}", receive_id_type=receive_id_type
        )


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
    skip_rejection_feedback: bool = False,
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

    if approval.status != "pending":
        await ctx.bridge.send_text(
            receive_id,
            "该审批已经处理或过期，无需重复点击。",
            receive_id_type=receive_id_type,
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
        await ctx.bridge._send_interactive(
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
        await ctx.bridge.send_text(
            receive_id,
            "已驳回，请直接回复修改意见。",
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
        "直接发消息不会创建项目；如需创建请使用 /create。"
    )
    await ctx.bridge.send_text(receive_id, help_text, receive_id_type=receive_id_type)


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
        await ctx.bridge.send_text(
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
        await ctx.bridge.send_text(
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
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
    blockers = _blocking_projects_for_directory_change(ctx, ctx.chat_id)
    if blockers:
        lines = ["当前会话存在活跃或暂停项目，不能解绑目录："]
        for p in blockers:
            lines.append(f"- {p.id} {p.name or ''} [{p.status}] {p.current_state}")
        lines.append("\n请先 `/cancel` 这些项目或等其进入终态后再解绑目录。")
        await ctx.bridge.send_text(
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
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


async def _show_bind_candidate_card(
    ctx: FeishuHandlerContext, open_id: str
) -> None:
    """Push a candidate-directory selection card for `/bind` (no args).

    Tries the `project_manager` agent (stage=`bind_dir`) first to scan
    `MAC_ALLOWED_BIND_ROOTS` via Bash and produce candidate recommendations.
    Falls back to the deterministic Python scanner if the agent is unavailable,
    returns no candidates, or any error occurs. The agent never writes to the
    database - Handler runs path validation before binding.
    """
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if not ctx.chat_id:
        await ctx.bridge.send_text(
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
        await ctx.bridge.send_text(
            receive_id, "\n".join(lines), receive_id_type=receive_id_type
        )
        return

    from mac_system.config import get_allowed_bind_roots

    allowed_roots = get_allowed_bind_roots()
    if not allowed_roots:
        await ctx.bridge.send_text(
            receive_id,
            "未配置 `MAC_ALLOWED_BIND_ROOTS`，请使用 `/bind <绝对路径>` 显式指定目录。",
            receive_id_type=receive_id_type,
        )
        return

    candidates = await _collect_bind_dir_candidates(ctx, allowed_roots)
    await ctx.bridge._send_interactive(
        receive_id,
        build_directory_candidate_card(
            candidates, chat_id=ctx.chat_id, chat_type=ctx.chat_type
        ),
        receive_id_type=receive_id_type,
    )


async def _collect_bind_dir_candidates(
    ctx: FeishuHandlerContext, allowed_roots: list[Path]
) -> list[dict[str, str]]:
    """Try the project_manager agent first; fall back to deterministic scan.

    The agent is asked (via `bind_dir` stage prompt) to scan allowed roots
    with Bash and return candidate repos. If the agent fails or returns an
    empty list, we fall back to the pure-Python scanner so the user always
    gets a usable card.
    """
    allowed_roots_str = "\n".join(str(r) for r in allowed_roots)
    try:
        output = await ctx.orchestrator.run_chat_scoped_agent(
            agent_type="project_manager",
            stage="bind_dir",
            input_data={"allowed_bind_roots": [str(r) for r in allowed_roots]},
            extra_variables={"allowed_bind_roots": allowed_roots_str},
        )
    except Exception as exc:
        logger.warning("project_manager bind_dir agent crashed: %s", exc)
        output = None

    if output and output.get("success") and output.get("candidates"):
        # Filter candidates to those that actually exist and have .git, as a
        # safety net against the agent returning paths outside allowed roots.
        valid: list[dict[str, str]] = []
        for c in output["candidates"][:12]:
            path_str = c.get("path", "")
            if not path_str:
                continue
            path = Path(path_str).expanduser()
            try:
                path = path.resolve()
            except Exception:
                continue
            if not _path_within_allowed_roots(path, allowed_roots):
                logger.warning(
                    "project_manager returned path outside allowed roots: %s",
                    path,
                )
                continue
            if not (path / ".git").exists():
                continue
            valid.append({"path": str(path), "name": c.get("name") or path.name})
        if valid:
            return valid

    # Fallback: deterministic Python scan.
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


async def _switch_project(
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
    """Show the bound directory and active project for this user in this chat."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)
    if ctx.chat_id is None:
        await ctx.bridge.send_text(
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
        await ctx.bridge.send_text(
            receive_id,
            f"{directory_line}\n当前会话没有默认项目。可用 `/switch <项目ID>` 或 `/switch` 选择。",
            receive_id_type=receive_id_type,
        )
        return
    await ctx.bridge.send_text(
        receive_id,
        f"{directory_line}\n当前默认项目：{project.id} {project.name or ''} [{project.status}] {project.current_state}",
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
    payload = [
        {"id": p.id, "name": p.name or p.id, "current_state": p.current_state}
        for p in projects
    ]
    await ctx.bridge._send_interactive(
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
    lines.append("\n使用 `/switch <项目ID>` 切换默认项目")
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

    await ctx.bridge.send_text(
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
    await ctx.bridge._send_interactive(
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
        await ctx.bridge.send_text(
            receive_id,
            "没有正在等待的创建流程，请重新使用 `/create <描述>`。",
            receive_id_type=receive_id_type,
        )
        return
    task_type, description = _decode_create_state(pending.description)
    if not task_type or not description:
        ctx.service.delete_pending_intent(open_id)
        await ctx.bridge.send_text(
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
            await ctx.bridge.send_text(
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
            await ctx.bridge.send_text(
                receive_id,
                f"绑定目录失败：{exc}\n将使用系统工作区继续创建项目。",
                receive_id_type=receive_id_type,
            )
        else:
            await ctx.bridge.send_text(
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
            await ctx.bridge.update_card(
                progress_message_id,
                build_content_card("已完成选择", f"✅ 已选择：**{label}**", color="green"),
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
            await ctx.bridge.send_text(
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
    ):
        return await _handle_question_batch_action(ctx, action)

    if action.action == "skip_free_text":
        return await _handle_skip_free_text(ctx, action)

    if action.action == "submit_rejection_feedback":
        feedback = str((action.extras or {}).get("rejection_feedback", "")).strip()
        if not feedback:
            receive_id, receive_id_type = _reply_target(ctx, open_id)
            await ctx.bridge.send_text(
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
        )
        return

    if action.action == "reject_without_feedback":
        await _respond_to_approval(
            ctx,
            open_id,
            action.approval_id,
            "reject",
            skip_rejection_feedback=True,
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
            await ctx.bridge.send_text(
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
        await ctx.bridge.send_text(
            receive_id,
            "请回复要绑定的目录绝对路径（例如 `/Users/you/projects/my-repo`）。",
            receive_id_type=receive_id_type,
        )
        return

    if option == "bind_dir_cancel":
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await ctx.bridge.send_text(
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
