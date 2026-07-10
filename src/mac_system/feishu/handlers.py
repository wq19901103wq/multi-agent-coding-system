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
from mac_system.models import Project, ProjectUser, RouteDecision
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


async def handle_text_message(ctx: FeishuHandlerContext, msg: TextMessage) -> None:
    """Dispatch a text message from Feishu."""
    text = msg.text
    open_id = msg.open_id

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

    if text == "/whoami":
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await ctx.bridge.send_text(
            receive_id, f"你的 open_id 是：{open_id}", receive_id_type=receive_id_type
        )
        return

    if text.startswith("/bind "):
        project_id = text[len("/bind "):].strip()
        await _bind_project(ctx, open_id, project_id)
        return

    if text.startswith("/status "):
        project_id = text[len("/status "):].strip()
        await _show_status(ctx, open_id, project_id)
        return

    if text.startswith("/create "):
        rest = text[len("/create "):].strip()
        task_type, description = _parse_create_command(rest)
        if description:
            if task_type:
                await _create_project(ctx, open_id, task_type, description)
            else:
                await _route_and_create(ctx, open_id, description)
        else:
            receive_id, receive_id_type = _reply_target(ctx, open_id)
            await ctx.bridge.send_text(
                receive_id,
                "格式错误。用法：/create <描述> 或 /create --type=<task_type> <描述>",
                receive_id_type=receive_id_type,
            )
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

    # Default: treat free-form text as a project creation request.
    await _route_and_create(ctx, open_id, text)


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
        "PLANNING_REQUIREMENTS", "PLANNING_ARCHITECTURE", "PLANNING_SPECS",
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


async def _handle_rejection_feedback(
    ctx: FeishuHandlerContext,
    open_id: str,
    project: Project,
    text: str,
) -> None:
    """Store the user's rejection feedback and trigger the planner."""
    receive_id, receive_id_type = _reply_target(ctx, open_id)

    project.context = {**project.context, "latest_rejection_feedback": text}
    ctx.service.session.commit()

    # Summarize the feedback in a card before re-planning.
    sm = StateMachine(project.task_type)
    rejected_state = project.context.get("rejected_confirmation_state")
    if rejected_state:
        state_display = sm.display_name(rejected_state)
        rounds = project.context.get("confirmation_rounds", {})
        # The round we are about to re-do is the next attempt.
        round_number = rounds.get(rejected_state, 0) + 1
    else:
        state_display = sm.display_name(project.current_state)
        round_number = 1
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
        await ctx.bridge.send_text(
            receive_id, f"你已经绑定到项目 {project_id}", receive_id_type=receive_id_type
        )
        return

    await ctx.bridge.send_text(
        receive_id, f"已绑定到项目 {project_id}", receive_id_type=receive_id_type
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
        title=f"项目状态：{project_id}",
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
    last_state: str | None = None
    for _ in range(max_ticks):
        await ctx.orchestrator.tick(project.id)
        project = ctx.service.require_project(project.id)
        if project.current_state == last_state:
            return  # Tick did not change state; avoid spinning forever.
        last_state = project.current_state
        sm = StateMachine(project.task_type)
        st = sm.state_type(project.current_state)
        if st in ("human_confirmation", "human_input"):
            # We just arrived at a confirmation state; tick once more so the
            # orchestrator creates the approval and sends the card.
            await ctx.orchestrator.tick(project.id)
            return
        if st in ("terminal", "idle"):
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

    if decision.task_type is None or decision.confidence < ctx.service.settings.intent_low_threshold:
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
    if len(parts) < 2:
        await ctx.bridge.send_text(
            receive_id,
            "格式错误。用法：/input <approval_id> <你的回答>",
            receive_id_type=receive_id_type,
        )
        return

    approval_id, answer = parts
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

    await ctx.service.respond_approval(
        approval.id, "answer", responded_by=open_id
    )
    await ctx.service.transition_state(
        project.id,
        "answer",
        payload={"clarification_answer": answer},
        triggered_by="feishu",
    )
    await ctx.orchestrator.tick(project.id)
    await ctx.bridge.send_text(
        receive_id,
        f"已提交，项目 {project.id} 继续执行",
        receive_id_type=receive_id_type,
    )


async def handle_card_action(ctx: FeishuHandlerContext, action: ButtonAction) -> None:
    """Process an interactive card button click."""
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

    # Otherwise treat as an approval/decision button.
    try:
        approval = ctx.service.get_approval(action.approval_id)
    except ApprovalNotFoundError:
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await ctx.bridge.send_text(
            receive_id, "该审批已过期或不存在", receive_id_type=receive_id_type
        )
        return

    project = ctx.service.get_project(approval.project_id)
    if project is None:
        receive_id, receive_id_type = _reply_target(ctx, open_id)
        await ctx.bridge.send_text(
            receive_id, "项目不存在", receive_id_type=receive_id_type
        )
        return

    chosen = action.action
    if chosen not in ("confirm", "reject"):
        chosen = action.option_key or chosen

    receive_id, receive_id_type = _reply_target(ctx, open_id)

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
        if opt.get("action") == chosen:
            if chosen == "select_option" and action.option_key:
                if opt.get("option_key") != action.option_key:
                    continue
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

    await ctx.service.respond_approval(
        approval.id, chosen, responded_by=open_id
    )

    from_state = project.current_state
    await ctx.service.transition_state(
        project.id, chosen, triggered_by="feishu"
    )

    if is_rejection:
        # Remember which confirmation state was rejected so the feedback summary
        # card can show the correct round and name.
        project.context = {**project.context, "rejected_confirmation_state": from_state}
        ctx.service.session.commit()
        # For rejection we wait for the user's free-form feedback text before
        # re-running the agent.  Do not advance here.
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

    # Tick repeatedly so cards are sent for every confirmation stage.
    await _advance_until_confirm(ctx, project, receive_id, receive_id_type)



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
