"""Thin Feishu adapter for native Codex project threads.

Feishu owns presentation and project selection only.  It does not classify
natural language, plan work, choose Agents, retry stages, or implement a
workflow state machine.  Every ordinary message is delivered unchanged to the
selected Codex root thread; messages received during a turn use Codex's native
``turn/steer`` request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mac_system.config import get_allowed_bind_roots
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService, generate_id
from mac_system.core.root_sessions import RootSessionStore
from mac_system.core.root_agent import ProjectResetError, _DEFAULT_LEAD_MODEL
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.card import (
    build_content_card,
    build_project_decision_card,
    build_question_batch_card,
    build_status_card,
)
from mac_system.feishu.menu import (
    build_create_prompt_card,
    build_directory_candidate_card,
    build_main_menu_card,
    build_project_list_card,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import Approval, Project, ProjectDecision
from mac_system.utils.redaction import redact_text


logger = logging.getLogger(__name__)

_PROJECT_LIST_LIMIT = 10

_EXECUTION_STATUS_LABELS = {
    "idle": "空闲",
    "running": "执行中",
    "owner_reviewing": "Owner 审阅中",
    "waiting_user": "等待你决定",
    "waiting_external": "等待外部条件",
}
_HISTORY_STATUS_LABELS = {
    "pending": "进行中",
    "completed": "已送达",
    "failed": "失败已送达",
    "interrupted": "已中断",
    "declined": "已拒绝",
    "delivery_pending": "等待送达",
    "missing_report": "历史缺少汇报",
}
_DECISION_EXECUTION_LABELS = {
    "active": "未完成·推进中",
    "blocked": "未完成·受阻",
    "needs_revision": "需要调整",
    "completed": "已完成",
    "superseded": "已被后续决定替代",
    "not_applicable": "已记录",
}


_COMMAND_ALIASES = {
    "/h": "/help",
    "/m": "/menu",
    "/c": "/create",
    "/ls": "/projects",
    "/proj": "/projects",
    "/s": "/switch",
    "/use": "/switch",
    "/i": "/current",
    "/info": "/current",
    "/a": "/agents",
    "/ow": "/owner",
    "/n": "/newthread",
    "/new": "/newthread",
    "/o": "/bind",
    "/open": "/bind",
    "/b": "/bind",
    "/close": "/unbind",
    "/u": "/unbind",
    "/p": "/pause",
    "/r": "/resume",
    "/x": "/cancel",
    "/stop": "/cancel",
}
_KNOWN_COMMANDS = {
    "/help",
    "/menu",
    "/create",
    "/projects",
    "/list",
    "/switch",
    "/current",
    "/agents",
    "/agent",
    "/owner",
    "/newthread",
    "/model",
    "/bind",
    "/unbind",
    "/pause",
    "/resume",
    "/cancel",
    "/reset",
}


def _canonical_command(text: str) -> str:
    command = text.partition(" ")[0].lower()
    return _COMMAND_ALIASES.get(command, command)


def _is_known_command(text: str) -> bool:
    return _canonical_command(text) in _KNOWN_COMMANDS


def _is_valid_model_name(model: str) -> bool:
    # ASCII-only slug so an invalid name fails here, not at thread/start.
    return (
        1 <= len(model) <= 64
        and model[0].isascii()
        and model[0].isalnum()
        and all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in model)
    )


def _compact_owner_text(value: Any, *, limit: int = 180) -> str:
    text = " ".join(redact_text(str(value or "")).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _card_reply_context(
    ctx: "FeishuHandlerContext",
    project: Project,
    projection: Any,
    *,
    fallback_summary: str,
) -> dict[str, str]:
    """Describe the public request/result behind a replied-to card."""

    context = {
        "kind": str(projection.card_kind or "card"),
        "subject_type": str(projection.subject_type or "notification"),
        "subject_ref": str(projection.subject_id or ""),
        "summary": fallback_summary,
    }
    if projection.subject_type != "root_turn":
        return context
    history = ctx.service.get_project_history_round(
        project.id,
        native_turn_id=str(projection.subject_id or ""),
    )
    if history is None:
        return context
    requests = [
        _compact_owner_text(item.get("text"), limit=500)
        for item in (history.user_instructions or [])[-3:]
        if isinstance(item, dict) and _compact_owner_text(item.get("text"), limit=500)
    ]
    result = _compact_owner_text(
        history.report_content or history.report_title,
        limit=500,
    )
    if requests:
        context["request"] = "；".join(requests)
    if result:
        context["result"] = result
    return context


@dataclass
class FeishuHandlerContext:
    bridge: FeishuBridge
    service: ProjectService
    orchestrator: Any
    bot_account_id: str = "default"
    chat_id: str | None = None
    chat_type: str = "p2p"


def _chat_id(ctx: FeishuHandlerContext, open_id: str) -> str:
    return ctx.chat_id or open_id


def _recipient(ctx: FeishuHandlerContext, open_id: str) -> tuple[str, str]:
    if ctx.chat_id:
        return ctx.chat_id, "chat_id"
    return open_id, "open_id"


async def _send_card(
    ctx: FeishuHandlerContext,
    open_id: str,
    title: str,
    content: str,
    *,
    color: str = "blue",
) -> str:
    receive_id, receive_id_type = _recipient(ctx, open_id)
    try:
        message_id = await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=generate_id("feishu-card"),
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="report",
            card=build_content_card(title, content, color=color),
            bot_account_id=ctx.bot_account_id,
        )
    except Exception:
        logger.warning("Feishu card delivery failed; intent remains persisted", exc_info=True)
        return ""
    return message_id or ""


async def _show_model_picker(ctx: FeishuHandlerContext, open_id: str, project: Project, page: int = 0) -> None:
    try:
        models = await ctx.orchestrator.list_root_models(project.id)
    except Exception as exc:
        await ctx.orchestrator.rotate_root_progress_for_timeline_event(project.id)
        await _send_card(ctx, open_id, "无法获取模型选项",
                         f"{redact_text(str(exc)) or '模型目录请求超时'}\n请稍后重新发送 `/model`。", color="orange")
        return
    # A slow catalog fetch can outlive the text message's initial rotation:
    # freeze any progress emitted during that await before appending the card.
    await ctx.orchestrator.rotate_root_progress_for_timeline_event(project.id)
    models = [item for item in models if _is_valid_model_name(str(item.get("model", "")))]
    if not models:
        await _send_card(ctx, open_id, "暂无模型选项", "当前助手没有返回可选模型；请检查登录状态后重试 `/model`。", color="orange")
        return
    ctx.service.session.refresh(project)
    current = (project.context or {}).get("lead_model") or _DEFAULT_LEAD_MODEL
    catalog_names = {item["model"] for item in models}
    page = max(0, min(page, (len(models) - 1) // 10))
    card = build_content_card("选择当前项目模型", (
        f"**项目**：{project.name or project.id}\n**当前默认模型**：`{current}`\n\n"
        "点击下方模型即可切换。切换会在下一条消息开启新的 Codex 对话，"
        "不携带旧对话上下文；项目文件不变，Owner 模型不变。运行中不能切换。\n"
        f"选项来自当前 Codex 模型目录（第 {page + 1}/{(len(models) - 1) // 10 + 1} 页）。"
    ))
    elements = card["body"]["elements"]
    if current not in catalog_names:
        elements.append({"tag": "markdown", "content": "当前默认模型未出现在本次目录中；下方展示的是助手实际返回的选项。"})

    def button(label: str, action: str, **extra: Any) -> dict[str, Any]:
        return {"tag": "button", "text": {"tag": "plain_text", "content": label},
                "type": "default", "behaviors": [{"type": "callback", "value": {
                    "action": action, "project_id": project.id, "requester": open_id,
                    "option_key": extra.get("model") or f"page:{extra.get('page', 0)}",
                    "current_model": current, "chat_id": _chat_id(ctx, open_id),
                    "chat_type": ctx.chat_type, **extra,
                }}]}

    for item in models[page * 10:(page + 1) * 10]:
        model = item["model"]
        label = str(item.get("displayName") or model)
        if label != model:
            label = f"{label} · {model}"
        if model == current:
            label += "（当前）"
        if item.get("description"):
            elements.append({"tag": "markdown", "content": str(item["description"])[:500]})
        elements.append(button(label, "select_lead_model", model=model))
    default_label = f"恢复 MAC 默认（{_DEFAULT_LEAD_MODEL}）"
    if _DEFAULT_LEAD_MODEL not in catalog_names:
        default_label += " · 本次目录未列出"
    elements.append(button(default_label, "select_lead_model", model="default"))
    if page:
        elements.append(button("上一页", "list_lead_models", page=page - 1))
    if (page + 1) * 10 < len(models):
        elements.append(button("下一页", "list_lead_models", page=page + 1))
    await _send_interactive(ctx, open_id, card)


async def _send_interactive(
    ctx: FeishuHandlerContext,
    open_id: str,
    card: dict[str, Any],
) -> str:
    receive_id, receive_id_type = _recipient(ctx, open_id)
    try:
        message_id = await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=generate_id("feishu-card"),
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="interaction",
            card=card,
            bot_account_id=ctx.bot_account_id,
        )
    except Exception:
        logger.warning("Feishu card delivery failed; intent remains persisted", exc_info=True)
        return ""
    return message_id or ""


async def _close_historical_interaction(
    ctx: FeishuHandlerContext,
    message_id: str | None,
) -> None:
    """Close an old interactive request without rewriting later facts into it.

    Feishu cards sit in the chat timeline.  Once a later user/Owner event has
    resolved a request, the clean projection is to remove that obsolete form
    and append the result at the current time.  If the provider refuses a
    recall (for example because the recall window elapsed), leave the original
    historical card untouched; its callbacks are already rejected by the
    durable lifecycle state.
    """

    if not message_id:
        return
    delete_message = getattr(ctx.bridge, "delete_message", None)
    if not callable(delete_message):
        return
    recalled = False
    try:
        await delete_message(message_id)
        recalled = True
    except Exception:
        logger.warning(
            "Could not remove resolved historical interaction card",
            exc_info=True,
        )
    projection = ctx.service.get_card_projection_by_message_id(message_id)
    if projection is not None:
        ctx.service.upsert_card_projection(
            projection_key=projection.projection_key,
            project_id=projection.project_id,
            conversation_id=projection.conversation_id,
            subject_type=projection.subject_type,
            subject_id=projection.subject_id,
            card_kind=projection.card_kind,
            provider_message_id=projection.provider_message_id,
            lifecycle_status="recalled" if recalled else "closed_unrecalled",
            selected_action=projection.selected_action,
            resolved_by=projection.resolved_by,
            resulting_state=projection.resulting_state,
        )


async def _close_pending_adjustment_prompt(
    ctx: FeishuHandlerContext, decision: ProjectDecision
) -> None:
    """Recall only a waiting adjustment prompt, never an older decision card."""

    projection = ctx.service.get_card_projection(
        f"decision:{decision.id}:adjustment-prompt"
    )
    if projection is not None and projection.lifecycle_status == "waiting_user":
        await _close_historical_interaction(ctx, projection.provider_message_id)


async def _supersede_pending_alignment_for_new_request(
    ctx: FeishuHandlerContext,
    project: Project,
) -> None:
    """Let a new independent human request replace unfinished preflight.

    This applies only to Owner's pre-execution alignment. Native Codex
    ``request_user_input`` remains attached to its active turn and follows the
    separate steer rules below.
    """

    for decision in ctx.service.get_project_decisions(project.id, limit=100):
        if (
            decision.decision_type != "alignment"
            or decision.status != "requires_user"
        ):
            continue
        updated = ctx.service.update_project_decision(
            decision.id,
            status="superseded",
        )
        await _close_historical_interaction(ctx, updated.card_message_id)


async def _resolve_menu_card(
    ctx: FeishuHandlerContext,
    action: ButtonAction,
    title: str,
    content: str,
    *,
    color: str = "green",
) -> None:
    """Close the old menu and append the action result at the newest point."""

    if not action.message_id:
        return
    receive_id, receive_id_type = _recipient(ctx, action.open_id)
    try:
        await _close_historical_interaction(ctx, action.message_id)
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=generate_id("feishu-menu-resolution"),
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="interaction",
            card=build_content_card(title, content, color=color),
            projection_key=f"menu:{action.message_id}:resolution",
            subject_type="menu_action_resolution",
            subject_id=action.message_id,
            lifecycle_status="resolved",
            selected_action=action.option_key,
            resolved_by=action.open_id,
            resulting_state="resolved",
            bot_account_id=ctx.bot_account_id,
        )
    except Exception:
        logger.warning("Failed to resolve Feishu menu card", exc_info=True)


def _active_project(ctx: FeishuHandlerContext, open_id: str) -> Project | None:
    chat_id = _chat_id(ctx, open_id)
    # The explicit chat/user selection is the only routing fact. Falling back
    # to a user's most recently updated Project can cross chats and makes
    # directory/project switches appear to succeed while messages still enter
    # an unrelated thread.
    return ctx.service.get_chat_user_active_project(chat_id, open_id)


def _project_name(description: str) -> str:
    compact = " ".join(description.split())
    return compact[:24] or "新项目"


async def _create_project(
    ctx: FeishuHandlerContext,
    open_id: str,
    description: str,
) -> Project:
    chat_id = _chat_id(ctx, open_id)
    project = await ctx.service.create_project(
        name=_project_name(description),
        task_type="feature",
        description=description,
        bot_account_id=ctx.bot_account_id,
        creator_open_id=open_id,
        feishu_chat_id=chat_id,
        feishu_chat_type=ctx.chat_type,
        initial_context={"orchestration_mode": "root_agent"},
    )
    await _send_card(
        ctx,
        open_id,
        "项目已创建",
        (
            f"**项目**：{project.name}\n\n"
            f"**工作目录**：`{project.workspace_path}`\n\n"
            "已开始处理；需要你决定的方向会先显示选择卡，否则 Codex 直接执行。"
        ),
        color="green",
    )
    ctx.orchestrator.schedule_root_agent(project.id, description)
    return project


async def _deliver_to_root(
    ctx: FeishuHandlerContext,
    project: Project,
    text: str,
    *,
    actor_open_id: str | None = None,
    visible_request: str | None = None,
    request_origin: str = "human",
    parent_decision_id: str | None = None,
    reply_context: dict[str, Any] | None = None,
) -> None:
    if (project.context or {}).get("runtime_reset_required"):
        await _send_card(ctx, actor_open_id or ctx.chat_id or "",
                         "项目尚未恢复", "正在重置或上次清理未确认完成；请 /reset 成功后再发送任务。", color="orange")
        return
    try:
        if project.status == "paused":
            await ctx.service.resume_project(project.id, triggered_by="feishu")
        if project.execution_status in {"running", "owner_reviewing", "waiting_user"}:
            if await ctx.orchestrator.steer_root_agent(
                project.id,
                text,
                actor_open_id=actor_open_id,
                visible_request=visible_request,
                reply_context=reply_context,
            ):
                return
        ctx.orchestrator.schedule_root_agent(
            project.id,
            text,
            actor_open_id=actor_open_id,
            visible_request=visible_request,
            request_origin=request_origin,
            parent_decision_id=parent_decision_id,
            reply_context=reply_context,
        )
    except ProjectResetError:
        await _send_card(ctx, actor_open_id or ctx.chat_id or "",
                         "消息未执行", "项目正在重置或清理尚未确认；请 /reset 成功后重新发送这条消息。", color="orange")


async def _acknowledge_message(
    ctx: FeishuHandlerContext, message_id: str | None
) -> None:
    """Acknowledge transport receipt on the original Feishu message."""
    if not message_id:
        return
    try:
        await ctx.bridge.add_reaction(message_id, "OnIt")
    except Exception:
        # A reaction is presentation-only. Delivery to Root must still proceed.
        logger.warning("Failed to acknowledge Feishu message", exc_info=True)


async def _update_project_decision_card(
    ctx: FeishuHandlerContext,
    project: Project,
    decision: ProjectDecision,
    *,
    resolved_by: str | None = None,
    details_expanded: bool | None = None,
    view_action_key: str | None = None,
) -> None:
    if not project.feishu_chat_id or not decision.card_message_id:
        return
    embedded_report = bool((decision.answer_payload or {}).get("embedded_report"))
    if embedded_report:
        report_card = dict((decision.answer_payload or {}).get("report_card") or {})
        owner_outcome = (
            "resolved_by_user"
            if decision.status == "resolved_by_user"
            else "adjustment_requested"
            if decision.status in {"awaiting_adjustment", "adjustment_requested"}
            else "advise"
            if decision.decision_type == "advice"
            else "require_user"
            if decision.status == "requires_user"
            else "decide"
        )
        owner_decision = decision.user_override or decision.decision
        card = build_content_card(
            str(report_card.get("title") or "本轮结果"),
            str(report_card.get("content") or ""),
            page=int(report_card.get("page") or 1),
            total=int(report_card.get("total") or 1),
            color=str(report_card.get("color") or "blue"),
            footer=[
                (
                    "Project Owner · 已按你的选择继续"
                    if decision.status == "resolved_by_user"
                    else "Project Owner · 等待你的调整"
                    if decision.status == "awaiting_adjustment"
                    else "Project Owner · 已决定"
                )
            ],
            details=[
                (
                    "Owner 判断依据",
                    [
                        line
                        for line in (
                            f"判断依据：{decision.rationale}" if decision.rationale else "",
                            f"结果影响：{decision.impact}" if decision.impact else "",
                            (
                                "依据来源：" + "；".join(decision.authority_refs)
                                if decision.authority_refs
                                else ""
                            ),
                        )
                        if line
                    ],
                    False,
                )
            ],
            owner_review={
                "outcome": owner_outcome,
                "decision": owner_decision,
                "decision_id": decision.id,
                "options": (
                    list(decision.options or [])
                    if decision.status == "requires_user"
                    else []
                ),
                "reversible": (
                    decision.reversible and decision.status == "applied"
                ),
                "chat_id": project.feishu_chat_id,
                "chat_type": project.feishu_chat_type,
            },
        )
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=(
                f"decision-view:{decision.id}:{view_action_key}"
                if view_action_key
                else f"decision-update:{decision.id}:{decision.updated_at.isoformat()}"
            ),
            recipient_id=project.feishu_chat_id,
            recipient_type="chat_id",
            intent_type="report",
            card=card,
            project_id=project.id,
            update_message_id=decision.card_message_id,
            projection_key=str(report_card.get("projection_key") or f"decision:{decision.id}"),
            subject_type="root_turn",
            subject_id=str(report_card.get("subject_id") or decision.native_turn_id or decision.id),
            lifecycle_status=decision.status,
            resolved_by=resolved_by,
            resulting_state=project.execution_status,
        )
        return
    projection = ctx.service.get_card_projection(f"decision:{decision.id}")
    if details_expanded is None:
        details_expanded = bool(
            projection is not None
            and projection.view_state == "details_expanded"
        )
        if decision.status in {
            "awaiting_adjustment",
            "adjustment_requested",
            "application_failed",
        }:
            details_expanded = True
    card = build_project_decision_card(
        decision.id,
        project_name=project.name or project.id,
        question=decision.question,
        decision=decision.decision,
        rationale=decision.rationale,
        impact=decision.impact,
        reversible=decision.reversible,
        options=list(decision.options or []),
        status=decision.status,
        user_override=decision.user_override,
        actor_model=decision.actor_model,
        actor_usage=decision.actor_usage,
        actor_duration_seconds=decision.actor_duration_seconds,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
        details_expanded=details_expanded,
        execution_status=decision.execution_status,
        execution_rounds=decision.execution_rounds,
    )
    await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=(
            f"decision-view:{decision.id}:{view_action_key}"
            if view_action_key
            else f"decision-update:{decision.id}:{decision.updated_at.isoformat()}"
        ),
        recipient_id=project.feishu_chat_id,
        recipient_type="chat_id",
        intent_type="decision",
        card=card,
        project_id=project.id,
        update_message_id=decision.card_message_id,
        projection_key=f"decision:{decision.id}",
        subject_type="project_decision",
        subject_id=decision.id,
        lifecycle_status=decision.status,
        view_state=(
            "details_expanded" if details_expanded else "details_collapsed"
        ),
        resolved_by=resolved_by,
        resulting_state=project.execution_status,
    )


async def _deliver_decision_adjustment(
    ctx: FeishuHandlerContext,
    decision: ProjectDecision,
    text: str,
    open_id: str,
) -> None:
    project = ctx.service.get_project(decision.project_id)
    if project is None:
        await _send_card(ctx, open_id, "项目不存在", "这项决定所属项目已经不可用。")
        return
    updated = ctx.service.update_project_decision(
        decision.id,
        status="adjustment_requested",
        user_override=text,
        user_choice_actor=open_id,
    )
    await _close_pending_adjustment_prompt(ctx, decision)
    if bool((decision.answer_payload or {}).get("embedded_report")):
        ctx.service.set_chat_user_context(_chat_id(ctx, open_id), open_id, project.id)
        await _deliver_to_root(
            ctx,
            project,
            (
                "[用户正在调整一项 AI Project Owner 决策。"
                f"原决定：{decision.decision}]\n{text}"
            ),
            actor_open_id=open_id,
            visible_request=text,
        )
        return
    adjustment_card = build_project_decision_card(
        updated.id,
        project_name=project.name or project.id,
        question=updated.question,
        decision=updated.decision,
        rationale=updated.rationale,
        impact=updated.impact,
        reversible=updated.reversible,
        status="adjustment_requested",
        user_override=text,
        actor_model=updated.actor_model,
        actor_usage=updated.actor_usage,
        actor_duration_seconds=updated.actor_duration_seconds,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
    )
    try:
        message_id = await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=(
                f"project-owner-adjustment:{decision.id}:"
                f"{updated.updated_at.isoformat()}"
            ),
            recipient_id=project.feishu_chat_id or open_id,
            recipient_type=("chat_id" if project.feishu_chat_id else "open_id"),
            intent_type="decision",
            card=adjustment_card,
            project_id=project.id,
            projection_key=(
                f"decision:{decision.id}:adjustment:"
                f"{updated.updated_at.isoformat()}"
            ),
            subject_type="project_decision_adjustment",
            subject_id=decision.id,
            lifecycle_status="adjustment_requested",
            resolved_by=open_id,
            resulting_state=project.execution_status,
        )
    except Exception:
        # The user's visible message and durable adjustment still exist; never
        # rewrite an older card as a fallback for a failed newest-event card.
        logger.warning("Could not append Project Owner adjustment card", exc_info=True)
    else:
        if message_id:
            ctx.service.update_project_decision(
                updated.id,
                status="adjustment_requested",
                card_message_id=message_id,
            )
    ctx.service.set_chat_user_context(_chat_id(ctx, open_id), open_id, project.id)
    await _deliver_to_root(
        ctx,
        project,
        (
            "[用户正在调整一项 AI Project Owner 决策。"
            f"原决定：{decision.decision}]\n{text}"
        ),
        actor_open_id=open_id,
        visible_request=text,
    )


async def handle_text_message(
    ctx: FeishuHandlerContext, msg: TextMessage
) -> None:
    """Route one Feishu message without semantic classification."""

    ctx.chat_id = msg.chat_id or ctx.chat_id
    ctx.chat_type = msg.chat_type or ctx.chat_type
    text = msg.text.strip()
    if not text:
        return
    # Emergency control must not wait for reactions, a stuck progress-card
    # update, reply handling, or the normal Agent steering path.
    if _canonical_command(text) == "/reset":
        await _handle_command(ctx, msg, text)
        return
    await _acknowledge_message(ctx, msg.message_id)

    chat_id = _chat_id(ctx, msg.open_id)
    reply_projection = (
        ctx.service.get_card_projection_by_message_id(msg.parent_id)
        if msg.parent_id
        else None
    )
    reply_project = None
    if reply_projection is not None and reply_projection.project_id:
        candidate = ctx.service.get_project(reply_projection.project_id)
        if candidate is not None and (
            candidate.feishu_chat_id == chat_id
            or ctx.service.user_can_access_project(msg.open_id, candidate.id)
        ):
            reply_project = candidate
            # Replying to a card is an explicit project selection. Subsequent
            # unquoted messages should continue in the same project.
            ctx.service.set_chat_user_context(chat_id, msg.open_id, candidate.id)
        else:
            await _send_card(
                ctx,
                msg.open_id,
                "原卡片所属项目不可用",
                "这条回复没有发送给其他项目。请先选择一个可用项目，再重新发送。",
                color="orange",
            )
            return

    if (
        reply_projection is not None
        and reply_projection.subject_type == "question_interaction"
        and not (text.startswith("/") and _is_known_command(text))
    ):
        interaction = ctx.service.get_question_interaction(
            reply_projection.subject_id
        )
        if interaction is not None and interaction.status == "pending":
            pending_questions = [
                question
                for question in ctx.service.get_questions_for_interaction(
                    interaction.id
                )
                if question.status == "pending"
            ]
            if len(pending_questions) == 1:
                question = pending_questions[0]
                await _handle_question_action(
                    ctx,
                    ButtonAction(
                        open_id=msg.open_id,
                        approval_id=interaction.id,
                        action="submit_inline_free_text",
                        state="ROOT_AGENT_WAITING_USER",
                        decision_id=None,
                        option_key=None,
                        extras={
                            "interaction_id": interaction.id,
                            "question_id": question.question_id,
                            "inline_free_text_value": text,
                        },
                        chat_id=chat_id,
                        chat_type=ctx.chat_type,
                        message_id=reply_projection.provider_message_id,
                    ),
                )
                return
            if len(pending_questions) > 1:
                await _send_card(
                    ctx,
                    msg.open_id,
                    "请在原卡片中回答",
                    "这张卡片包含多个问题，请逐项选择或填写后提交，避免把答案对应错。",
                    color="orange",
                )
            return

    # A human message is a new chronological event even when Codex accepts it
    # through native turn/steer. Freeze the older progress projection now so
    # no later activity can be patched above this message in the timeline.
    timeline_project = reply_project or _active_project(ctx, msg.open_id)
    if timeline_project is not None and timeline_project.execution_status in {
        "running",
        "owner_reviewing",
        "waiting_user",
    }:
        rotate = getattr(
            ctx.orchestrator,
            "rotate_root_progress_for_timeline_event",
            None,
        )
        if rotate is not None:
            await rotate(timeline_project.id)

    if (
        reply_projection is not None
        and reply_projection.subject_type == "project_decision"
        and reply_project is not None
        and not (text.startswith("/") and _is_known_command(text))
    ):
        decision = ctx.service.get_project_decision(reply_projection.subject_id)
        if decision is not None:
            await _deliver_decision_adjustment(ctx, decision, text, msg.open_id)
            return

    pending = ctx.service.get_pending_intent(msg.open_id, chat_id)
    pending_in_chat = pending is not None

    # A recognized project command supersedes a card prompt. Other leading
    # slashes may be legitimate prompt answers, most notably Unix paths.
    if text.startswith("/") and (not pending_in_chat or _is_known_command(text)):
        await _handle_command(ctx, msg, text)
        return

    if pending_in_chat:
        if pending.stage == "awaiting_description":
            ctx.service.delete_pending_intent(msg.open_id, chat_id)
            await _create_project(ctx, msg.open_id, text)
            return
        if pending.stage == "awaiting_bind_dir":
            ctx.service.delete_pending_intent(msg.open_id, chat_id)
            await _bind_directory(ctx, msg.open_id, text)
            return
        if pending.stage == "awaiting_decision_adjustment":
            decision = ctx.service.get_project_decision(pending.description or "")
            ctx.service.delete_pending_intent(msg.open_id, chat_id)
            if decision is None:
                await _send_card(
                    ctx,
                    msg.open_id,
                    "决定记录不可用",
                    "没有找到要调整的决定，请直接说明新的要求。",
                    color="orange",
                )
                return
            await _deliver_decision_adjustment(ctx, decision, text, msg.open_id)
            return

    if text.startswith("/"):
        await _handle_command(ctx, msg, text)
        return

    project = reply_project or _active_project(ctx, msg.open_id)
    if project is None:
        # A Feishu chat is the outer session.  Its first ordinary message gets
        # one persistent Codex project thread; there is no separate chat Agent.
        await _create_project(ctx, msg.open_id, text)
        return

    visible_request = text
    reply_context = None
    if reply_projection is not None and reply_project is not None:
        labels = {
            "progress": "进度卡",
            "report": "结果卡",
            "question": "问题卡",
            "interaction": "操作卡",
            "decision": "AI 决策卡",
        }
        card_label = labels.get(reply_projection.card_kind, "卡片")
        summary = ctx.service.get_card_visible_summary(
            reply_projection.provider_message_id or ""
        )
        reference = f"用户正在回复一张{card_label}。"
        if summary:
            reference += f"卡片可见摘要：{summary}"
        reply_context = _card_reply_context(
            ctx,
            reply_project,
            reply_projection,
            fallback_summary=summary or card_label,
        )
        text = f"[{reference}]\n{text}"
    await _supersede_pending_alignment_for_new_request(ctx, project)
    await _deliver_to_root(
        ctx,
        project,
        text,
        actor_open_id=msg.open_id,
        visible_request=visible_request,
        reply_context=reply_context,
    )


async def _handle_command(
    ctx: FeishuHandlerContext,
    msg: TextMessage,
    text: str,
    *,
    expected_project_id: str | None = None,
) -> None:
    _, _, argument = text.partition(" ")
    command = _canonical_command(text)
    argument = argument.strip()
    open_id = msg.open_id
    if expected_project_id is not None:
        expected_project = _active_project(ctx, open_id)
        if expected_project is None or expected_project.id != expected_project_id:
            await _send_card(ctx, open_id, "模型选项已失效", "当前项目已变化，请重新发送 `/model`。", color="orange")
            return

    # An explicit command supersedes a prior menu prompt in this chat. Without
    # this, tapping “返回” and later typing ordinary text can unexpectedly
    # create a Project or interpret the message as a filesystem path.
    chat_id = _chat_id(ctx, open_id)
    pending = ctx.service.get_pending_intent(open_id, chat_id)
    if pending is not None:
        ctx.service.delete_pending_intent(open_id, chat_id)

    if command in {"/help", "/menu"}:
        if command == "/menu":
            await _send_interactive(
                ctx,
                open_id,
                build_main_menu_card(ctx.chat_id, ctx.chat_type),
            )
        else:
            await _send_card(
                ctx,
                open_id,
                "MAC 使用帮助",
                (
                    "直接发送需求、问题或修改意见即可。\n\n"
                    "- `/create` 或 `/c` 加需求：新建项目\n"
                    "- `/proj` 或 `/ls`：查看项目\n"
                    "- `/use` 或 `/s` 加项目名 / ID 前缀：切换项目\n"
                    "- `/info` 或 `/i`：当前项目、目录和状态\n"
                    "- `/agents` 或 `/a`：查看可用 Coding-Agent\n"
                    "- `/agent`：查看项目执行助手；`/agent codex`、`/agent kimi` 切换后续消息\n"
                    "- `/owner` 或 `/ow`：查看 Owner；加 `on` / `off` 可启用或关闭\n"
                    "- `/new` 或 `/n`：清空当前项目对话（目录不变）\n"
                    "- `/model`：显示可点击的模型选项；也可 `/model 模型名`，`default` 恢复默认\n"
                    "- `/open` 或 `/o` 加目录：打开已有 Git 项目\n"
                    "- `/bind` 或 `/b`：兼容的打开目录命令\n"
                    "- `/close`、`/unbind` 或 `/u`：关闭当前项目（不会删除文件）\n"
                    "- `/pause` 或 `/p`：暂停\n"
                    "- `/resume` 或 `/r`：恢复\n"
                    "- `/stop` 或 `/x`：取消\n"
                    "- `/reset`：强制清理当前项目卡住的执行，保留文件和会话\n"
                    "- `/menu` 或 `/m`：打开操作菜单\n"
                    "- `/help` 或 `/h`：显示本帮助"
                ),
            )
        return

    if command == "/create":
        if argument:
            await _create_project(ctx, open_id, argument)
        else:
            ctx.service.upsert_pending_intent(
                open_id,
                None,
                "awaiting_description",
                chat_id=_chat_id(ctx, open_id),
                chat_type=ctx.chat_type,
            )
            await _send_interactive(
                ctx,
                open_id,
                build_create_prompt_card(ctx.chat_id, ctx.chat_type),
            )
        return

    if command in {"/projects", "/list"}:
        await _show_projects(ctx, open_id)
        return

    if command == "/switch":
        if argument:
            await _switch_project(ctx, open_id, argument)
        else:
            await _show_switch_card(ctx, open_id)
        return

    if command == "/current":
        await _show_current(ctx, open_id)
        return

    if command == "/agents":
        await _show_agents(ctx, open_id)
        return

    if command == "/agent":
        project = _active_project(ctx, open_id)
        if project is None:
            await _send_card(
                ctx, open_id, "无法切换助手", "请先创建或切换项目。", color="orange"
            )
            return
        provider_names = {"codex": "Codex", "kimi_code": "Kimi", "claude_code": "Claude"}
        requested = argument.casefold()
        if not requested or requested in {"status", "list"}:
            current = RootSessionStore(ctx.service.session).ensure(project.id).provider
            await _send_card(
                ctx, open_id, "当前项目执行助手",
                f"**项目**：{project.name or project.id}\n\n"
                f"**当前**：{provider_names.get(current, current)}\n\n"
                "发送 `/agent codex` 或 `/agent kimi` 切换后续消息。"
                "Claude 尚未接入项目主会话；`/agents` 查看外部 Coding-Agent。",
            )
            return
        provider = {"codex": "codex", "kimi": "kimi_code",
                    "kimi_code": "kimi_code", "claude": "claude_code",
                    "claude_code": "claude_code"}.get(requested)
        if provider is None:
            await _send_card(
                ctx, open_id, "助手名称不支持",
                "可用命令：`/agent codex`、`/agent kimi`；"
                "Claude 主会话尚未接入。", color="orange",
            )
            return
        try:
            previous, current = await ctx.orchestrator.set_root_provider(
                project.id, provider
            )
        except RuntimeError as exc:
            await _send_card(ctx, open_id, "暂时无法切换助手", str(exc), color="orange")
            return
        if previous == current:
            await _send_card(
                ctx, open_id, "助手未变化",
                f"当前项目已使用 {provider_names.get(current, current)}；原会话保持不变。",
                color="green",
            )
            return
        await _send_card(
            ctx, open_id, "执行助手已切换",
            f"**项目**：{project.name or project.id}\n\n"
            f"**助手**：{provider_names.get(previous, previous)} → "
            f"{provider_names.get(current, current)}\n\n"
            "当前任务不会重跑；下一条普通消息进入新助手的项目会话。"
            "旧助手会话保留，但对话历史不会自动迁移；项目目录保持不变。",
            color="green",
        )
        return

    if command == "/owner":
        project = _active_project(ctx, open_id)
        if project is None:
            await _send_card(
                ctx, open_id, "Project Owner", "请先创建或切换项目。", color="orange"
            )
            return
        owner_action = argument.casefold()
        if owner_action in {"on", "off"}:
            enabled = owner_action == "on"
            ctx.service.set_project_owner_enabled(project.id, enabled)
            await _send_card(
                ctx,
                open_id,
                "Project Owner 已启用" if enabled else "Project Owner 已关闭",
                (
                    f"**项目**：{project.name or project.id}\n\n"
                    + (
                        "后续汇报、问题和审批会恢复由 Owner 审阅。"
                        if enabled
                        else "后续汇报不再触发 Owner；问题和审批将直接交给你。"
                    )
                    + "\n\n可随时使用 `"
                    + ("/owner off" if enabled else "/owner on")
                    + "` 修改。"
                ),
                color="green" if enabled else "orange",
            )
            return
        if owner_action not in {"", "status"}:
            await _send_card(
                ctx,
                open_id,
                "Project Owner 命令",
                "可用命令：`/owner`、`/owner on`、`/owner off`。",
                color="orange",
            )
            return
        memories = ctx.service.get_effective_owner_memories(
            open_id, project_id=project.id
        )
        project_policies = ctx.service.get_project_policies(project.id)
        open_commitments = ctx.service.get_open_project_commitments(
            project.id, limit=100
        )
        decisions = ctx.service.get_project_decisions(project.id, limit=100)
        pending_decisions = []
        pending_direction_texts = set()
        for item in decisions:
            pending_text = _compact_owner_text(item.question, limit=500)
            if (
                item.status != "requires_user"
                or item.decision_type not in {"alignment", "governance"}
                or not pending_text
                or pending_text.casefold() in pending_direction_texts
            ):
                continue
            pending_direction_texts.add(pending_text.casefold())
            pending_decisions.append(item)
        recent_history = ctx.service.get_project_history_rounds(
            project.id, limit=3, newest_first=True
        )
        global_memories = [item for item in memories if item.scope == "global"]
        project_memories = [item for item in memories if item.scope == "project"]
        owner_enabled = ctx.service.is_project_owner_enabled(project.id)
        lines = [
            f"**项目**：{project.name or project.id}",
            f"**Owner**：{'已启用' if owner_enabled else '已关闭'}",
            f"**当前执行**：{_EXECUTION_STATUS_LABELS.get(project.execution_status, project.execution_status)}",
        ]

        project_rule_lines = [
            f"- [{'硬规则' if item.strength == 'hard' else '默认规则'}] {_compact_owner_text(item.content, limit=500)}"
            for item in project_policies
        ]
        project_rule_lines.extend(
            f"- [项目偏好] {_compact_owner_text(item.content, limit=500)}"
            for item in project_memories
        )
        lines.append(
            "**当前项目的用户规则**：\n"
            + (
                "\n".join(project_rule_lines)
                if project_rule_lines
                else "- 暂无单独记录；仍以当前真人指令为最高权威"
            )
        )

        direction_lines = []
        for item in pending_decisions[:10]:
            direction_lines.append(
                f"- [待你决定] {_compact_owner_text(item.question, limit=500)}"
            )
        for item in open_commitments:
            decision_text = _compact_owner_text(
                item.user_override or item.decision or item.question,
                limit=500,
            )
            direction_lines.append(
                f"- [{_DECISION_EXECUTION_LABELS.get(item.execution_status, item.execution_status)}] {decision_text}"
            )
        lines.append(
            "**当前方向与未完成承诺**：\n"
            + ("\n".join(direction_lines) if direction_lines else "- 暂无")
        )

        history_lines = []
        for item in recent_history:
            human_instructions = [
                instruction
                for instruction in (item.user_instructions or [])
                if isinstance(instruction, dict)
                and (
                    str(instruction.get("origin") or item.request_origin).lower()
                    == "user"
                    or str(
                        instruction.get("origin") or item.request_origin
                    ).lower().startswith("human")
                )
            ]
            instruction_texts = []
            reply_summaries = []
            reply_requests = []
            reply_results = []
            for instruction in human_instructions[-3:]:
                instruction_text = _compact_owner_text(
                    instruction.get("text"), limit=240
                )
                if instruction_text:
                    instruction_texts.append(instruction_text)
                reply_to = instruction.get("reply_to")
                if isinstance(reply_to, dict):
                    reply_request = _compact_owner_text(
                        reply_to.get("request"), limit=240
                    )
                    reply_result = _compact_owner_text(
                        reply_to.get("result"), limit=240
                    )
                    if (
                        (not reply_request or not reply_result)
                        and reply_to.get("subject_type") == "root_turn"
                        and reply_to.get("subject_ref")
                    ):
                        replied_round = ctx.service.get_project_history_round(
                            project.id,
                            native_turn_id=str(reply_to["subject_ref"]),
                        )
                        if replied_round is not None:
                            if not reply_request:
                                replied_requests = [
                                    _compact_owner_text(entry.get("text"), limit=240)
                                    for entry in (replied_round.user_instructions or [])[-3:]
                                    if isinstance(entry, dict)
                                ]
                                reply_request = "；".join(
                                    value for value in replied_requests if value
                                )
                            if not reply_result:
                                reply_result = _compact_owner_text(
                                    replied_round.report_content
                                    or replied_round.report_title,
                                    limit=240,
                                )
                    if reply_request and reply_request not in reply_requests:
                        reply_requests.append(reply_request)
                    if reply_result and reply_result not in reply_results:
                        reply_results.append(reply_result)
                    reply_summary = _compact_owner_text(
                        reply_to.get("summary"), limit=240
                    )
                    if (
                        not reply_request
                        and not reply_result
                        and reply_summary
                        and reply_summary not in reply_summaries
                    ):
                        reply_summaries.append(reply_summary)
            result_summary = _compact_owner_text(
                item.report_content or item.report_title,
                limit=240,
            )
            round_lines = [
                f"- 第 {item.sequence} 轮 · {_HISTORY_STATUS_LABELS.get(item.report_status, item.report_status)}"
            ]
            if instruction_texts:
                round_lines.append(f"  - 用户：{'；'.join(instruction_texts)}")
            if reply_requests:
                round_lines.append(f"  - 回应的请求：{'；'.join(reply_requests)}")
            if reply_results:
                round_lines.append(f"  - 回应时结果：{'；'.join(reply_results)}")
            if reply_summaries:
                round_lines.append(f"  - 回应：{'；'.join(reply_summaries)}")
            if result_summary:
                round_lines.append(f"  - 本轮结果：{result_summary}")
            history_lines.append("\n".join(round_lines))
        lines.append(
            "**最近项目记录**：\n"
            + ("\n".join(history_lines) if history_lines else "- 暂无")
        )

        if global_memories:
            lines.append(
                "**跨项目用户偏好**：\n"
                + "\n".join(
                    f"- [{'硬规则' if item.strength == 'hard' else '默认偏好'}] {_compact_owner_text(item.content, limit=500)}"
                    for item in global_memories
                )
            )
        else:
            lines.append("**跨项目用户偏好**：暂无")
        await _send_card(ctx, open_id, "Project Owner", "\n\n".join(lines))
        return

    if command == "/reset":
        if argument:
            await _send_card(ctx, open_id, "重置未执行", "请单独发送 /reset，不带其他内容。", color="orange")
            return
        project = _active_project(ctx, open_id)
        if project is None:
            await _send_card(ctx, open_id, "未找到项目", "请先创建或切换项目。", color="orange")
            return
        try:
            await ctx.orchestrator.reset_root_runtime(project.id)
        except Exception as exc:
            logger.exception("Project reset could not confirm cleanup: %s", project.id)
            await _send_card(ctx, open_id, "重置未确认成功",
                             f"{redact_text(str(exc))}\n\n未删除项目文件或会话；未确认清理前不会启动新的执行。", color="red")
            return
        await _send_card(ctx, open_id, "当前项目执行已重置",
                         "已确认旧执行及所属进程清理完成，旧排队任务已取消。\n\n"
                         "项目文件、工作目录、Agent 选择和会话历史保留；未自动重跑旧任务。"
                         "请发送下一条消息继续。", color="green")
        return

    if command == "/newthread":
        project = _active_project(ctx, open_id)
        if project is None:
            await _send_card(
                ctx, open_id, "无法新建会话", "请先创建或切换项目。", color="orange"
            )
            return
        try:
            await ctx.orchestrator.start_new_root_thread(project.id)
        except RuntimeError as exc:
            await _send_card(ctx, open_id, "暂时无法新建会话", str(exc), color="orange")
            return
        await _send_card(
            ctx,
            open_id,
            "当前项目已清空对话",
            (
                f"**项目**：{project.name or project.id}\n\n"
                f"**工作目录**：`{project.workspace_path}`\n\n"
                "项目和目录保持不变；下一条普通消息会开始新的 Codex 对话，"
                "不会携带旧对话历史。"
            ),
            color="green",
        )
        return

    if command == "/model":
        project = _active_project(ctx, open_id)
        if project is None:
            await _send_card(
                ctx, open_id, "无法切换模型", "请先创建或切换项目。", color="orange"
            )
            return
        if expected_project_id is not None and project.id != expected_project_id:
            await _send_card(ctx, open_id, "模型选项已失效", "当前项目已变化，请重新发送 `/model`。", color="orange")
            return
        default_model = _DEFAULT_LEAD_MODEL
        if not argument:
            await _show_model_picker(ctx, open_id, project)
            return
        requested = argument.strip()
        if requested.lower() in {"default", "reset"}:
            requested_model: str | None = None
            requested_label = default_model
        else:
            requested_model = requested
            requested_label = requested
            if not _is_valid_model_name(requested_model):
                await _send_card(
                    ctx,
                    open_id,
                    "模型名不合法",
                    "模型名只允许字母、数字、点、下划线和连字符（1–64 个字符）。",
                    color="orange",
                )
                return
        try:
            old_model, _new_model = await ctx.orchestrator.set_root_model(
                project.id, requested_model
            )
        except RuntimeError as exc:
            await _send_card(ctx, open_id, "暂时无法切换模型", str(exc), color="orange")
            return
        if (old_model or default_model) == requested_label:
            await _send_card(
                ctx,
                open_id,
                "模型未变化",
                (
                    f"**项目**：{project.name or project.id}\n\n"
                    f"**模型**：`{requested_label}`（已是当前模型，现有对话保持不变）"
                ),
                color="green",
            )
            return
        await _send_card(
            ctx,
            open_id,
            "模型已切换",
            (
                f"**项目**：{project.name or project.id}\n\n"
                f"**模型**：`{old_model or default_model}` → `{requested_label}`\n\n"
                "下一条消息会以新模型开启新的 Codex 对话（当前旧对话不保留）。"
                "Owner 审阅模型不受影响。"
            ),
            color="green",
        )
        return

    if command == "/bind":
        if argument:
            await _bind_directory(ctx, open_id, argument)
        else:
            await _show_directory_card(ctx, open_id)
        return

    if command == "/unbind":
        await _unbind_directory(ctx, open_id)
        return

    if command in {"/pause", "/resume", "/cancel"}:
        project = (
            ctx.service.get_project(argument)
            if argument
            else _active_project(ctx, open_id)
        )
        chat_id = _chat_id(ctx, open_id)
        if project is not None and not (
            ctx.service.user_can_access_project(open_id, project.id)
            or any(
                item.id == project.id
                for item in ctx.service.list_projects_for_chat(chat_id)
            )
        ):
            project = None
        if project is None:
            await _send_card(ctx, open_id, "未找到项目", "请先创建或切换项目。", color="orange")
            return
        if command == "/pause":
            await ctx.service.pause_project(project.id, triggered_by=open_id)
            message = "项目已暂停；Codex 会话仍保留，可随时恢复。"
        elif command == "/resume":
            await ctx.service.resume_project(project.id, triggered_by=open_id)
            message = "项目已恢复。"
        else:
            try:
                interrupted = await ctx.orchestrator.stop_root_turn(project.id)
                expired_interactions = (
                    ctx.service.expire_pending_question_interactions(
                        project.id,
                        resolution_source="turn_stopped",
                    )
                )
                for interaction in expired_interactions:
                    await _close_historical_interaction(
                        ctx, interaction.card_message_id
                    )
                message = (
                    "已停止当前执行。项目、目录和 Codex 对话仍保留。"
                    if interrupted
                    else "当前没有正在执行的一轮。项目、目录和 Codex 对话仍保留。"
                )
            except Exception:
                logger.exception("Failed to stop native Codex turn for %s", project.id)
                await _send_card(
                    ctx,
                    open_id,
                    "停止失败",
                    "停止请求未能完成，系统已记录错误；项目和目录未被删除。",
                    color="red",
                )
                return
        await _send_card(
            ctx,
            open_id,
            "项目控制",
            f"**项目**：{project.name or project.id}\n\n{message}",
            color="green",
        )
        return

    await _send_card(
        ctx,
        open_id,
        "未知命令",
        "发送 `/h` 查看快捷命令；普通需求无需命令，直接发送即可。",
        color="orange",
    )


async def _show_projects(ctx: FeishuHandlerContext, open_id: str) -> None:
    chat_id = _chat_id(ctx, open_id)
    projects = ctx.service.list_projects_for_chat(chat_id)
    if not projects:
        await _send_card(ctx, open_id, "项目", "还没有项目。")
        return
    selected = ctx.service.get_chat_user_selected_project(chat_id, open_id)
    projects = sorted(
        projects,
        key=lambda item: (bool(selected and selected.id == item.id), item.updated_at),
        reverse=True,
    )
    visible = projects[:_PROJECT_LIST_LIMIT]
    body = "\n".join(
        (
            f"- {'▶ ' if selected and selected.id == project.id else ''}"
            f"**{project.name or project.id}** · {project.execution_status}\n"
            f"  `{project.workspace_path}`\n"
            f"  `{project.id}`"
        )
        for project in visible
    )
    if len(projects) > len(visible):
        body += (
            f"\n\n仅显示当前及最近 {len(visible)} 个项目；"
            "发送 `/use 项目名` 或 `/use ID前缀` 可直接切换。"
        )
    await _send_card(ctx, open_id, "项目", body)


async def _show_switch_card(
    ctx: FeishuHandlerContext, open_id: str, *, mode: str = "switch"
) -> None:
    chat_id = _chat_id(ctx, open_id)
    projects = ctx.service.list_projects_for_chat(chat_id)
    selected = ctx.service.get_chat_user_selected_project(chat_id, open_id)
    projects = sorted(
        projects,
        key=lambda item: (bool(selected and selected.id == item.id), item.updated_at),
        reverse=True,
    )[:_PROJECT_LIST_LIMIT]
    payload = [
        {
            "id": project.id,
            "name": project.name or project.id,
            "execution_status": project.execution_status,
            "is_current": bool(selected and selected.id == project.id),
        }
        for project in projects
    ]
    await _send_interactive(
        ctx,
        open_id,
        build_project_list_card(
            payload,
            mode=mode,
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
        ),
    )


async def _switch_project(
    ctx: FeishuHandlerContext,
    open_id: str,
    project_id: str,
    *,
    announce: bool = True,
) -> None:
    chat_id = _chat_id(ctx, open_id)
    available = ctx.service.list_projects_for_chat(chat_id)
    query = project_id.strip()
    lowered = query.casefold()
    exact = [
        item
        for item in available
        if item.id == query or str(item.name or "").casefold() == lowered
    ]
    candidates = exact or [
        item
        for item in available
        if item.id.startswith(query)
        or str(item.name or "").casefold().startswith(lowered)
    ]
    if len(candidates) != 1:
        if candidates:
            selected = ctx.service.get_chat_user_selected_project(chat_id, open_id)
            await _send_interactive(
                ctx,
                open_id,
                build_project_list_card(
                    [
                        {
                            "id": item.id,
                            "name": item.name or item.id,
                            "execution_status": item.execution_status,
                            "is_current": bool(selected and selected.id == item.id),
                        }
                        for item in candidates[:_PROJECT_LIST_LIMIT]
                    ],
                    mode="switch",
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                ),
            )
        else:
            await _send_card(
                ctx,
                open_id,
                "切换失败",
                "没有找到匹配项目。发送 `/use` 查看最近项目，或输入更完整的项目名。",
                color="orange",
            )
        return
    project = candidates[0]
    ctx.service.bind_project_to_chat(
        project.id,
        chat_id,
        chat_type=ctx.chat_type,
        created_by=open_id,
    )
    ctx.service.set_chat_user_context(chat_id, open_id, project.id)
    if announce:
        await _send_card(
            ctx,
            open_id,
            "已切换项目",
            f"后续消息将进入“{project.name or project.id}”的同一个 Codex 会话。",
            color="green",
        )


async def _show_current(ctx: FeishuHandlerContext, open_id: str) -> None:
    chat_id = _chat_id(ctx, open_id)
    project = ctx.service.get_chat_user_selected_project(chat_id, open_id)
    if project is None:
        await _send_card(
            ctx,
            open_id,
            "当前项目",
            "当前没有打开项目。直接发送需求可创建新项目，或使用 `/open` 打开已有目录。",
            color="orange",
        )
        return
    lines = [
        f"**当前项目**：{project.name or project.id}",
        f"**工作目录**：`{project.workspace_path}`",
        f"**执行状态**：{ {'idle': '空闲', 'running': '执行中', 'owner_reviewing': 'AI 正在判断，你也可以直接选择', 'waiting_user': '等你决定', 'waiting_external': '等待外部结果'}.get(project.execution_status, project.execution_status) }",
    ]
    reset_pending = bool((project.context or {}).get("root_thread_reset_pending"))
    if reset_pending:
        lines.append("**对话**：新对话待开始（下一条普通消息创建）")
    profile = (
        {}
        if reset_pending
        else (ctx.service.get_latest_turn_profile(project.id) or {})
    )
    usage = profile.get("token_usage")
    usage = usage if isinstance(usage, dict) else {}
    total_tokens = usage.get("totalTokens")
    if isinstance(total_tokens, int) and total_tokens > 0:
        usage_parts = []
        input_tokens = usage.get("inputTokens")
        cached_tokens = usage.get("cachedInputTokens")
        output_tokens = usage.get("outputTokens")
        if isinstance(input_tokens, int):
            usage_parts.append(f"输入 {input_tokens:,}")
        if isinstance(cached_tokens, int) and cached_tokens > 0:
            usage_parts.append(f"其中缓存 {cached_tokens:,}")
        if isinstance(output_tokens, int):
            usage_parts.append(f"输出 {output_tokens:,}")
        detail = f"（{'；'.join(usage_parts)}）" if usage_parts else ""
        lines.append(
            f"**最近完成的 Root 轮次 Token**：{total_tokens:,}{detail}"
        )
    reported_cost = project.total_cost_usd
    if isinstance(reported_cost, (int, float)) and reported_cost > 0:
        lines.append(f"**外部 Provider 已报告费用**：${reported_cost:.4f}")
    else:
        lines.append("**费用**：当前没有 Provider 返回可核验费用")
    await _send_card(ctx, open_id, "当前项目", "\n\n".join(lines))


async def _show_agents(ctx: FeishuHandlerContext, open_id: str) -> None:
    display_names = {
        "claude_code": "Claude Code",
        "kimi_code": "Kimi Code",
        "codex": "Codex CLI",
        "pi": "Pi",
        "zcode": "Zcode",
    }
    status_names = {
        "available": "🟢 可用",
        "disabled": "⚪ 未启用",
        "unavailable": "🔴 不可用",
    }
    rows: list[str] = []
    for provider in ctx.orchestrator.provider_catalog():
        name = str(provider.get("provider") or "unknown")
        status = str(provider.get("status") or "unavailable")
        model = str(provider.get("model") or "").strip()
        metrics = provider.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        samples = int(metrics.get("sample_count") or 0)
        detail = []
        if model:
            detail.append(f"模型：{model}")
        if samples:
            completed = int(metrics.get("completed_count") or 0)
            average = metrics.get("average_duration_seconds")
            detail.append(f"近期 {completed}/{samples} 次完成")
            if isinstance(average, (int, float)):
                detail.append(f"平均 {average:g} 秒")
        else:
            detail.append("暂无真实运行样本")
        rows.append(
            f"**{display_names.get(name, name)}** · {status_names.get(status, status)}\n"
            f"{' · '.join(detail)}"
        )
    body = (
        "\n\n".join(rows)
        + "\n\nRoot 会根据任务和近期真实结果决定是否调用；你也可以直接说“这次用 Kimi Code 检查”。"
    )
    await _send_card(ctx, open_id, "Coding-Agent", body)


async def _show_directory_card(ctx: FeishuHandlerContext, open_id: str) -> None:
    candidates = ProjectService.scan_candidate_repositories(get_allowed_bind_roots())
    await _send_interactive(
        ctx,
        open_id,
        build_directory_candidate_card(candidates, ctx.chat_id, ctx.chat_type),
    )


async def _bind_directory(
    ctx: FeishuHandlerContext,
    open_id: str,
    directory: str,
    *,
    announce: bool = True,
) -> Project | None:
    try:
        chat_id = _chat_id(ctx, open_id)
        project = ctx.service.get_user_project_for_directory(
            open_id,
            directory,
            chat_id=chat_id,
        )
        resumed = project is not None
        if project is None:
            workspace_name = Path(directory).expanduser().resolve().name or "已有项目"
            project = await ctx.service.create_project(
                name=workspace_name,
                task_type="existing_project",
                description=f"已有目录 {workspace_name}",
                bot_account_id=ctx.bot_account_id,
                creator_open_id=open_id,
                feishu_chat_id=chat_id,
                feishu_chat_type=ctx.chat_type,
                bound_directory=directory,
                initial_context={"orchestration_mode": "root_agent"},
            )
        else:
            ctx.service.bind_project_to_chat(
                project.id,
                chat_id,
                chat_type=ctx.chat_type,
                created_by=open_id,
            )
            ctx.service.set_chat_user_context(chat_id, open_id, project.id)
        # Remove any legacy future-project binding. Project.workspace_path is
        # now the sole source of truth for the current directory.
        ctx.service.unbind_directory_from_chat(chat_id)
    except (ValueError, OSError) as exc:
        await _send_card(ctx, open_id, "打开失败", str(exc), color="red")
        return None
    if announce:
        await _send_card(
            ctx,
            open_id,
            "项目已打开",
            (
                f"**项目**：{project.name or project.id}\n\n"
                f"**工作目录**：`{project.workspace_path}`\n\n"
                f"{'已恢复这个目录原有的项目和对话。' if resumed else '已为这个目录创建项目。'}"
                "后续消息会直接进入这里。"
            ),
            color="green",
        )
    return project


async def _unbind_directory(ctx: FeishuHandlerContext, open_id: str) -> None:
    chat_id = _chat_id(ctx, open_id)
    project = ctx.service.get_chat_user_selected_project(chat_id, open_id)
    legacy_removed = ctx.service.unbind_directory_from_chat(chat_id)
    if project is not None:
        ctx.service.set_chat_user_context(chat_id, open_id, None)
    await _send_card(
        ctx,
        open_id,
        "当前项目",
        (
            f"已关闭“{project.name or project.id}”；项目和文件都没有删除，可通过 `/use` 返回。"
            if project is not None
            else (
                "已清理旧的目录绑定。" if legacy_removed else "当前没有打开项目。"
            )
        ),
        color="green" if project is not None or legacy_removed else "orange",
    )


async def handle_card_action(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> dict[str, Any] | None:
    """Map Feishu cards to project controls or Codex request_user_input."""

    if action.chat_id:
        ctx.chat_id = action.chat_id
        ctx.chat_type = action.chat_type

    if action.action == "menu":
        await _handle_menu_action(ctx, action)
        return None
    if action.action in {"select_lead_model", "list_lead_models"}:
        project = _active_project(ctx, action.open_id)
        if project is not None:
            await ctx.orchestrator.rotate_root_progress_for_timeline_event(project.id)
        ctx.service.session.expire_all()
        project = _active_project(ctx, action.open_id)
        current = ((project.context or {}).get("lead_model") or _DEFAULT_LEAD_MODEL) if project else None
        if (project is None or action.extras.get("project_id") != project.id
                or action.extras.get("requester") != action.open_id
                or action.extras.get("current_model") != current):
            await _send_card(ctx, action.open_id, "模型选项已失效", "项目或模型已变化，或这不是你的选项卡；请重新发送 `/model`。", color="orange")
            return None
        if action.action == "list_lead_models":
            try:
                page = int(action.extras.get("page", 0))
            except (TypeError, ValueError):
                page = 0
            await _show_model_picker(ctx, action.open_id, project, page)
        else:
            requested = str(action.extras.get("model") or "")
            if not _is_valid_model_name(requested):
                await _send_card(ctx, action.open_id, "模型选项无效", "请重新发送 `/model`。", color="orange")
                return None
            await _handle_command(ctx, TextMessage(open_id=action.open_id,
                                  message_id=action.message_id or "", text=f"/model {requested}",
                                  is_command=True, chat_id=ctx.chat_id, chat_type=ctx.chat_type), f"/model {requested}",
                                  expected_project_id=project.id)
        return None
    if action.action in {
        "select_question_option",
        "answer_question_option",
        "submit_selected_options",
        "answer_all_recommended",
        "submit_inline_free_text",
        "skip_remaining",
    }:
        return await _handle_question_action(ctx, action)
    if action.action == "select_owner_option":
        decision_id = action.decision_id or action.approval_id
        decision = ctx.service.get_project_decision(decision_id)
        if decision is None or decision.status != "requires_user":
            await _send_card(
                ctx,
                action.open_id,
                "选择已处理",
                "这项选择已经提交或不再可用。",
                color="orange",
            )
            return None
        project = ctx.service.get_project(decision.project_id)
        if project is None:
            await _send_card(ctx, action.open_id, "项目不存在", "无法提交这项选择。")
            return None
        selected = str(action.option_key or "").strip()
        selected_option = next(
            (
                item
                for item in (decision.options or [])
                if isinstance(item, dict)
                and str(item.get("label") or "").strip() == selected
            ),
            None,
        )
        if selected_option is None:
            await _send_card(
                ctx,
                action.open_id,
                "选项已失效",
                "请使用最新卡片中的选项。",
                color="orange",
            )
            return None
        updated = ctx.service.update_project_decision(
            decision.id,
            status="resolved_by_user",
            user_override=selected,
            user_choice_actor=action.open_id,
        )
        ctx.service.set_chat_user_context(
            _chat_id(ctx, action.open_id), action.open_id, project.id
        )
        if bool((decision.answer_payload or {}).get("embedded_report")):
            # This is still the unresolved event represented by the current
            # result card, so resolving it in place preserves the timeline and
            # avoids turning a user choice into another AI-decision card.
            await _update_project_decision_card(
                ctx, project, updated, resolved_by=action.open_id
            )
        else:
            await _close_historical_interaction(ctx, decision.card_message_id)
            receipt = build_project_decision_card(
                updated.id,
                project_name=project.name or project.id,
                question=updated.question,
                decision=selected,
                rationale=updated.rationale,
                impact=updated.impact,
                reversible=updated.reversible,
                status="resolved_by_user",
                actor_model=updated.actor_model,
                actor_usage=updated.actor_usage,
                actor_duration_seconds=updated.actor_duration_seconds,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
            )
            receive_id, receive_id_type = _recipient(ctx, action.open_id)
            receipt_message_id = await deliver_card(
                ctx.service,
                ctx.bridge,
                dedupe_key=f"project-owner-choice-receipt:{decision.id}",
                recipient_id=receive_id,
                recipient_type=receive_id_type,
                intent_type="decision",
                card=receipt,
                project_id=project.id,
                projection_key=f"decision:{decision.id}:resolution",
                subject_type="project_decision_resolution",
                subject_id=decision.id,
                lifecycle_status="resolved_by_user",
                selected_action=selected,
                resolved_by=action.open_id,
                resulting_state="running",
            )
            if receipt_message_id:
                ctx.service.update_project_decision(
                    updated.id,
                    status="resolved_by_user",
                    card_message_id=receipt_message_id,
                )
        if (
            decision.decision_type == "advice"
            and str(selected_option.get("action") or "execute") == "record_only"
        ):
            return None
        await _deliver_to_root(
            ctx,
            project,
            (
                "[用户已在 AI Project Owner 的选择卡中作出明确选择。"
                "把它视为新的用户指令，继续当前项目；不要再次询问同一事项。]\n"
                + (
                    f"原请求：{decision.answer_payload.get('source_request')}\n"
                    if decision.decision_type == "alignment"
                    and decision.answer_payload.get("source_request")
                    else ""
                )
                + f"事项：{decision.decision}\n"
                f"用户选择：{selected}"
            ),
            actor_open_id=action.open_id,
            visible_request=(
                str(decision.answer_payload.get("source_request"))
                if decision.decision_type == "alignment"
                and decision.answer_payload.get("source_request")
                else f"我选择：{selected}"
            ),
            request_origin=(
                "human_aligned"
                if decision.decision_type == "alignment"
                else "human"
            ),
            parent_decision_id=decision.id,
            reply_context={
                "kind": "decision",
                "subject_type": "project_decision",
                "subject_ref": decision.id,
                "summary": (
                    f"事项：{decision.question}；Owner 方案：{decision.decision}；"
                    f"用户选择：{selected}"
                ),
            },
        )
        return None
    if action.action == "adjust_project_decision":
        decision_id = action.decision_id or action.approval_id
        decision = ctx.service.get_project_decision(decision_id)
        if decision is None or decision.status != "applied":
            await _send_card(
                ctx,
                action.open_id,
                "决定已处理",
                "这项决定已经在调整中或不再可用。",
                color="orange",
            )
            return None
        project = ctx.service.get_project(decision.project_id)
        if project is None:
            await _send_card(ctx, action.open_id, "项目不存在", "无法调整这项决定。")
            return None
        updated = ctx.service.update_project_decision(
            decision.id, status="awaiting_adjustment"
        )
        ctx.service.set_chat_user_context(
            _chat_id(ctx, action.open_id), action.open_id, project.id
        )
        ctx.service.upsert_pending_intent(
            action.open_id,
            decision.id,
            "awaiting_decision_adjustment",
            chat_id=_chat_id(ctx, action.open_id),
            chat_type=ctx.chat_type,
        )
        prompt = build_content_card(
            "请说明怎么调整",
            f"原决定：{updated.decision}\n\n请直接回复你的调整要求。",
            color="orange",
        )
        prompt_message_id = await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"decision-adjustment-prompt:{decision.id}",
            recipient_id=project.feishu_chat_id or action.open_id,
            recipient_type=("chat_id" if project.feishu_chat_id else "open_id"),
            intent_type="question",
            card=prompt,
            project_id=project.id,
            projection_key=f"decision:{decision.id}:adjustment-prompt",
            subject_type="project_decision_adjustment_prompt",
            subject_id=decision.id,
            lifecycle_status="waiting_user",
            resulting_state=project.execution_status,
        )
        if prompt_message_id:
            ctx.service.update_project_decision(
                updated.id,
                status="awaiting_adjustment",
                card_message_id=prompt_message_id,
            )
        return None
    if action.action == "toggle_project_decision_details":
        decision_id = action.decision_id or action.approval_id
        decision = ctx.service.get_project_decision(decision_id)
        if decision is None:
            await _send_card(
                ctx,
                action.open_id,
                "决定不存在",
                "这项决定已经不可用。",
                color="orange",
            )
            return None
        project = ctx.service.get_project(decision.project_id)
        if project is None:
            await _send_card(ctx, action.open_id, "项目不存在", "无法查看这项决定。")
            return None
        raw_expanded = (action.extras or {}).get("expanded")
        expanded = raw_expanded is True or str(raw_expanded).lower() == "true"
        await _update_project_decision_card(
            ctx,
            project,
            decision,
            details_expanded=expanded,
            view_action_key=generate_id("decision-details"),
        )
        return None
    await _send_card(
        ctx,
        action.open_id,
        "操作已失效",
        "这张卡片当前不可操作，请回到项目继续发送消息。",
        color="orange",
    )
    return None


async def _handle_menu_action(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> None:
    option = action.option_key or "show_menu"
    if option == "show_menu":
        chat_id = _chat_id(ctx, action.open_id)
        pending = ctx.service.get_pending_intent(action.open_id, chat_id)
        if pending is not None:
            ctx.service.delete_pending_intent(action.open_id, chat_id)
        await _resolve_menu_card(ctx, action, "已返回", "已取消上一项输入。")
        await _send_interactive(
            ctx,
            action.open_id,
            build_main_menu_card(ctx.chat_id, ctx.chat_type),
        )
    elif option == "create_prompt":
        await _resolve_menu_card(ctx, action, "已选择", "创建一个新项目。")
        ctx.service.upsert_pending_intent(
            action.open_id,
            None,
            "awaiting_description",
            chat_id=_chat_id(ctx, action.open_id),
            chat_type=ctx.chat_type,
        )
        await _send_interactive(
            ctx,
            action.open_id,
            build_create_prompt_card(ctx.chat_id, ctx.chat_type),
        )
    elif option == "open_directory":
        await _resolve_menu_card(ctx, action, "已选择", "打开已有 Git 项目。")
        await _show_directory_card(ctx, action.open_id)
    elif option == "list_projects_status":
        await _resolve_menu_card(ctx, action, "已选择", "查看项目状态。")
        await _show_switch_card(ctx, action.open_id, mode="status")
    elif option in {"list_projects_switch", "list_projects_bind"}:
        await _resolve_menu_card(ctx, action, "已选择", "切换当前项目。")
        await _show_switch_card(ctx, action.open_id, mode="switch")
    elif option == "show_agents":
        await _resolve_menu_card(ctx, action, "已选择", "查看 Coding-Agent。")
        await _show_agents(ctx, action.open_id)
    elif option.startswith("switch:") or option.startswith("bind:"):
        project_id = option.split(":", 1)[1]
        await _switch_project(ctx, action.open_id, project_id, announce=False)
        project = ctx.service.get_chat_user_selected_project(
            _chat_id(ctx, action.open_id), action.open_id
        )
        if project is not None and project.id == project_id:
            await _resolve_menu_card(
                ctx,
                action,
                "项目已切换",
                (
                    f"**当前项目**：{project.name or project.id}\n\n"
                    f"**工作目录**：`{project.workspace_path}`"
                ),
            )
    elif option.startswith("status:"):
        project = ctx.service.get_project(option.split(":", 1)[1])
        if project is None:
            await _send_card(ctx, action.open_id, "项目不存在", "无法读取项目状态。")
        else:
            card = build_status_card(
                f"[{project.name or project.id}] 项目状态",
                project.id,
                project.execution_status,
                [],
                action_needed=project.waiting_reason,
                workspace=project.workspace_path,
            )
            if action.message_id:
                receive_id, receive_id_type = _recipient(ctx, action.open_id)
                await deliver_card(
                    ctx.service,
                    ctx.bridge,
                    dedupe_key=generate_id("feishu-status-resolution"),
                    recipient_id=receive_id,
                    recipient_type=receive_id_type,
                    intent_type="interaction",
                    card=card,
                    project_id=project.id,
                    update_message_id=action.message_id,
                    projection_key=f"menu:{action.message_id}",
                    subject_type="project_status",
                    subject_id=project.id,
                    lifecycle_status="resolved",
                    selected_action=action.option_key,
                    resolved_by=action.open_id,
                    resulting_state=project.execution_status,
                )
            else:
                await _send_interactive(ctx, action.open_id, card)
    elif option.startswith("bind_dir_select:"):
        project = await _bind_directory(
            ctx,
            action.open_id,
            option.split(":", 1)[1],
            announce=False,
        )
        if project is not None:
            await _resolve_menu_card(
                ctx,
                action,
                "项目已打开",
                (
                    f"**当前项目**：{project.name or project.id}\n\n"
                    f"**工作目录**：`{project.workspace_path}`"
                ),
            )
    elif option == "bind_dir_manual":
        await _resolve_menu_card(ctx, action, "等待输入", "请回复要打开的绝对路径。")
        ctx.service.upsert_pending_intent(
            action.open_id,
            None,
            "awaiting_bind_dir",
            chat_id=_chat_id(ctx, action.open_id),
            chat_type=ctx.chat_type,
        )
        await _send_card(
            ctx,
            action.open_id,
            "输入目录",
            "请直接回复要打开的 Git 仓库绝对路径。",
        )
    elif option == "bind_dir_cancel":
        chat_id = _chat_id(ctx, action.open_id)
        pending = ctx.service.get_pending_intent(action.open_id, chat_id)
        if pending is not None:
            ctx.service.delete_pending_intent(action.open_id, chat_id)
        await _resolve_menu_card(ctx, action, "已取消", "没有切换当前项目。")
        if not action.message_id:
            await _send_card(ctx, action.open_id, "已取消", "没有切换当前项目。")


def _question_payload(
    questions: list[Any],
) -> list[dict[str, Any]]:
    return [question.model_dump() for question in questions]


async def _handle_question_action(
    ctx: FeishuHandlerContext, action: ButtonAction
) -> dict[str, Any] | None:
    interaction_id = str((action.extras or {}).get("interaction_id") or "")
    if not interaction_id:
        interaction_id = action.approval_id
    interaction = ctx.service.get_question_interaction(interaction_id)
    approval = None
    if interaction is None:
        # Compatibility for cards created before QuestionInteraction became
        # the lifecycle source of truth.
        approval = ctx.service.session.get(Approval, action.approval_id)
        if approval is not None:
            interaction_id = str(
                (approval.payload or {}).get("question_interaction_id") or ""
            )
            interaction = ctx.service.get_question_interaction(interaction_id)
    if interaction is None or interaction.status != "pending":
        await _send_card(
            ctx,
            action.open_id,
            "问题已处理",
            "这张卡片已经提交或失效。",
            color="orange",
        )
        return None
    project = ctx.service.get_project(interaction.project_id)
    if project is None:
        return None
    questions = ctx.service.get_questions_for_interaction(interaction.id)
    # SQLAlchemy expires ORM rows on commit.  Keep the exact card projection
    # payload before recording selections/answers so options cannot disappear
    # from the updated Feishu card.
    question_payload = _question_payload(questions)
    pending = [question for question in questions if question.status == "pending"]
    selected = dict(interaction.selected_options or {})
    target_id = str((action.extras or {}).get("question_id") or "")

    if action.action == "select_question_option":
        if target_id and action.option_key:
            selected[target_id] = action.option_key
            interaction = ctx.service.set_question_selections(
                interaction.id, selected
            )
        return build_question_batch_card(
            interaction.id,
            interaction.id,
            question_payload,
            "Codex",
            interaction.stage or "conversation",
            state=action.state or "ROOT_AGENT_WAITING_USER",
            project_name=project.name or project.id,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            selected_options=selected,
        )

    if action.action == "answer_question_option" and target_id and action.option_key:
        selected[target_id] = action.option_key
    elif action.action == "submit_inline_free_text":
        input_name = str((action.extras or {}).get("input_name") or "").strip()
        value = str(
            (action.extras or {}).get(input_name)
            or (action.extras or {}).get("inline_free_text_value")
            or ""
        ).strip()
        if target_id and value:
            selected[target_id] = value
    elif action.action == "answer_all_recommended":
        for question in pending:
            if question.recommended_option:
                selected[question.question_id] = question.recommended_option

    proposed: list[tuple[Any, str | None, str | None, bool]] = []
    submitted: dict[str, str] = {}
    for question in pending:
        value = selected.get(question.question_id)
        if action.action == "skip_remaining" and question.kind not in {"blocking", "decision"}:
            proposed.append((question, None, None, True))
            continue
        if value is None:
            continue
        label = value
        for option in question.options or []:
            if str(option.get("key")) == str(value):
                label = str(option.get("label") or value)
                break
        proposed.append((question, value, label, False))
        submitted[question.question_id] = value

    proposed_ids = {question.id for question, *_rest in proposed}
    required_pending = {
        question.id
        for question in pending
        if question.kind in {"blocking", "decision"}
    }
    if not required_pending.issubset(proposed_ids):
        ctx.service.set_question_selections(interaction.id, selected)
        await _send_card(
            ctx,
            action.open_id,
            "还需要你的选择",
            "请完成卡片中的必答问题后再提交。",
            color="orange",
        )
        return None

    claim_source = f"user:{action.open_id}"
    if not ctx.service.claim_question_interaction(
        interaction.id, resolution_source=claim_source
    ):
        await _send_card(
            ctx,
            action.open_id,
            "问题已处理",
            "这张卡片已经由你或 AI Project Owner 提交。",
            color="orange",
        )
        return None

    try:
        for question, value, label, skipped in proposed:
            await ctx.service.submit_question_answer(
                question.id,
                value,
                label,
                skipped,
                action.open_id,
                commit=False,
            )
        if approval is None and interaction.approval_id:
            approval = ctx.service.session.get(Approval, interaction.approval_id)
        if approval is not None and approval.status == "pending":
            await ctx.service.respond_approval(
                approval.id, "answer", action.open_id, commit=False
            )
        if not ctx.service.complete_question_interaction_claim(
            interaction.id,
            claimed_by=claim_source,
            resolution_source="user",
            commit=False,
        ):
            raise RuntimeError("Question interaction reservation was lost")
        ctx.service.session.commit()
    except Exception:
        ctx.service.session.rollback()
        ctx.service.release_question_interaction(
            interaction.id, resolution_source=claim_source
        )
        raise

    owner_assessment = None
    if interaction.request_item_id:
        owner_assessment = ctx.service.get_project_decision_by_request_item_id(
            project.id, f"assessment:{interaction.request_item_id}"
        )
    # The Owner assessment is a separate card for request_user_input and is
    # merged into the native card for approvals.  Link it by the native item
    # id, not by message id, so both projections close with the same answer.
    if owner_assessment is not None and owner_assessment.status == "requires_user":
        owner_assessment = ctx.service.update_project_decision(
            owner_assessment.id,
            status="resolved_by_user",
            user_override="；".join(submitted.values()),
            user_choice_actor=action.open_id,
        )
        if owner_assessment.card_message_id != interaction.card_message_id:
            await _close_historical_interaction(
                ctx, owner_assessment.card_message_id
            )

    receipt_answers: dict[str, str] = {}
    for question in questions:
        answer = ctx.service.get_question_answer(question.id)
        if answer is not None and not answer.skipped and answer.answer:
            receipt_answers[question.question_id] = str(answer.answer)
    receipt = build_question_batch_card(
        interaction.id,
        interaction.id,
        question_payload,
        "Codex",
        interaction.stage or "conversation",
        state=action.state or "ROOT_AGENT_WAITING_USER",
        project_name=project.name or project.id,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
        submitted_answers=receipt_answers,
    )
    if action.message_id:
        receive_id, receive_id_type = _recipient(ctx, action.open_id)
        await _close_historical_interaction(ctx, action.message_id)
        receipt_message_id = await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"question-receipt:{interaction.id}",
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="question",
            card=receipt,
            project_id=project.id,
            projection_key=f"question:{interaction.id}:resolution",
            subject_type="question_interaction_resolution",
            subject_id=interaction.id,
            lifecycle_status="resolved_by_user",
            resolved_by=action.open_id,
            resulting_state="running",
        )
        if receipt_message_id:
            interaction.card_message_id = receipt_message_id
            ctx.service.session.add(interaction)
            ctx.service.session.commit()

    from mac_system.core.root_agent import resolve_root_user_input

    if not resolve_root_user_input(ctx.service, interaction.id):
        answers = []
        for question in questions:
            answer = ctx.service.get_question_answer(question.id)
            if answer is not None:
                answers.append(
                    {
                        "question_id": question.question_id,
                        "answer": answer.answer_label or answer.answer,
                        "skipped": answer.skipped,
                    }
                )
        ctx.orchestrator.schedule_root_agent(
            project.id,
            "用户回答了上一轮问题：\n" + json.dumps(answers, ensure_ascii=False),
        )
    await _send_card(
        ctx,
        action.open_id,
        "已提交",
        "答案已提交，正在继续处理本轮任务。",
        color="green",
    )
    return receipt if not action.message_id else None
