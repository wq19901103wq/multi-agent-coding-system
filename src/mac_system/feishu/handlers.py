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
from mac_system.feishu.bridge import FeishuBridge
from mac_system.feishu.card import (
    build_content_card,
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
from mac_system.models import Approval, Project


logger = logging.getLogger(__name__)


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
    "/newthread",
    "/bind",
    "/unbind",
    "/pause",
    "/resume",
    "/cancel",
}


def _canonical_command(text: str) -> str:
    command = text.partition(" ")[0].lower()
    return _COMMAND_ALIASES.get(command, command)


def _is_known_command(text: str) -> bool:
    return _canonical_command(text) in _KNOWN_COMMANDS


@dataclass
class FeishuHandlerContext:
    bridge: FeishuBridge
    service: ProjectService
    orchestrator: Any
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
        )
    except Exception:
        logger.warning("Feishu card delivery failed; intent remains persisted", exc_info=True)
        return ""
    return message_id or ""


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
        )
    except Exception:
        logger.warning("Feishu card delivery failed; intent remains persisted", exc_info=True)
        return ""
    return message_id or ""


async def _resolve_menu_card(
    ctx: FeishuHandlerContext,
    action: ButtonAction,
    title: str,
    content: str,
    *,
    color: str = "green",
) -> None:
    """Persist and display the result on the card the user acted on."""

    if not action.message_id:
        return
    receive_id, receive_id_type = _recipient(ctx, action.open_id)
    try:
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=generate_id("feishu-menu-resolution"),
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="interaction",
            card=build_content_card(title, content, color=color),
            update_message_id=action.message_id,
            projection_key=f"menu:{action.message_id}",
            subject_type="menu_action",
            subject_id=action.message_id,
            lifecycle_status="resolved",
            selected_action=action.option_key,
            resolved_by=action.open_id,
            resulting_state="resolved",
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
            "Codex 已开始处理；进度、问题和本轮结果都会继续显示在当前项目。"
        ),
        color="green",
    )
    ctx.orchestrator.schedule_root_agent(project.id, description)
    return project


async def _deliver_to_root(
    ctx: FeishuHandlerContext,
    project: Project,
    text: str,
) -> None:
    if project.status == "paused":
        await ctx.service.resume_project(project.id, triggered_by="feishu")
    if project.execution_status in {"running", "waiting_user"}:
        if await ctx.orchestrator.steer_root_agent(project.id, text):
            return
    ctx.orchestrator.schedule_root_agent(project.id, text)


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


async def handle_text_message(
    ctx: FeishuHandlerContext, msg: TextMessage
) -> None:
    """Route one Feishu message without semantic classification."""

    ctx.chat_id = msg.chat_id or ctx.chat_id
    ctx.chat_type = msg.chat_type or ctx.chat_type
    text = msg.text.strip()
    if not text:
        return
    await _acknowledge_message(ctx, msg.message_id)

    chat_id = _chat_id(ctx, msg.open_id)
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

    if text.startswith("/"):
        await _handle_command(ctx, msg, text)
        return

    project = _active_project(ctx, msg.open_id)
    if project is None:
        # A Feishu chat is the outer session.  Its first ordinary message gets
        # one persistent Codex project thread; there is no separate chat Agent.
        await _create_project(ctx, msg.open_id, text)
        return
    await _deliver_to_root(ctx, project, text)


async def _handle_command(
    ctx: FeishuHandlerContext,
    msg: TextMessage,
    text: str,
) -> None:
    _, _, argument = text.partition(" ")
    command = _canonical_command(text)
    argument = argument.strip()
    open_id = msg.open_id

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
                    "- `/use` 或 `/s` 加项目 ID：切换项目\n"
                    "- `/info` 或 `/i`：当前项目、目录和状态\n"
                    "- `/new` 或 `/n`：在当前项目中开始新对话（目录不变）\n"
                    "- `/open` 或 `/o` 加目录：打开已有 Git 项目\n"
                    "- `/bind` 或 `/b`：兼容的打开目录命令\n"
                    "- `/close`、`/unbind` 或 `/u`：关闭当前项目（不会删除文件）\n"
                    "- `/pause` 或 `/p`：暂停\n"
                    "- `/resume` 或 `/r`：恢复\n"
                    "- `/stop` 或 `/x`：取消\n"
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
            "当前项目已开始新对话",
            (
                f"**项目**：{project.name or project.id}\n\n"
                f"**工作目录**：`{project.workspace_path}`\n\n"
                "项目和目录保持不变；后续消息不会携带旧对话历史。"
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
            interrupted = await ctx.orchestrator.stop_root_turn(project.id)
            message = (
                "已请求停止当前一轮；项目、目录和 Codex 对话仍保留，可以继续发送新消息。"
                if interrupted
                else "当前没有正在执行的一轮；项目、目录和 Codex 对话仍保留。"
            )
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
    body = "\n".join(
        (
            f"- {'▶ ' if selected and selected.id == project.id else ''}"
            f"**{project.name or project.id}** · {project.execution_status}\n"
            f"  `{project.workspace_path}`\n"
            f"  `{project.id}`"
        )
        for project in projects
    )
    await _send_card(ctx, open_id, "项目", body)


async def _show_switch_card(
    ctx: FeishuHandlerContext, open_id: str, *, mode: str = "switch"
) -> None:
    chat_id = _chat_id(ctx, open_id)
    projects = ctx.service.list_projects_for_chat(chat_id)
    selected = ctx.service.get_chat_user_selected_project(chat_id, open_id)
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
    project = ctx.service.get_project(project_id)
    if project is None or project.status == "cancelled":
        await _send_card(ctx, open_id, "切换失败", "项目不存在或已取消。", color="orange")
        return
    chat_id = _chat_id(ctx, open_id)
    if not (
        ctx.service.user_can_access_project(open_id, project.id)
        or any(item.id == project.id for item in ctx.service.list_projects_for_chat(chat_id))
    ):
        await _send_card(ctx, open_id, "切换失败", "当前用户无权访问该项目。", color="orange")
        return
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
        f"**执行状态**：{project.execution_status}",
    ]
    await _send_card(ctx, open_id, "当前项目", "\n\n".join(lines))


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
        project = ctx.service.get_user_project_for_directory(open_id, directory)
        resumed = project is not None
        if project is None:
            workspace_name = Path(directory).expanduser().resolve().name or "已有项目"
            project = await ctx.service.create_project(
                name=workspace_name,
                task_type="existing_project",
                description=f"继续迭代已有目录 {workspace_name}",
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
    if action.action in {
        "select_question_option",
        "answer_question_option",
        "submit_selected_options",
        "answer_all_recommended",
        "submit_inline_free_text",
        "skip_remaining",
    }:
        return await _handle_question_action(ctx, action)
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
            "conversation",
            state=action.state or "ROOT_AGENT_WAITING_USER",
            project_name=project.name or project.id,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            selected_options=selected,
        )

    if action.action == "answer_question_option" and target_id and action.option_key:
        selected[target_id] = action.option_key
    elif action.action == "submit_inline_free_text":
        value = str((action.extras or {}).get("inline_free_text_value") or "").strip()
        if target_id and value:
            selected[target_id] = value
    elif action.action == "answer_all_recommended":
        for question in pending:
            if question.recommended_option:
                selected[question.question_id] = question.recommended_option

    submitted: dict[str, str] = {}
    for question in pending:
        value = selected.get(question.question_id)
        if action.action == "skip_remaining" and question.kind not in {"blocking", "decision"}:
            await ctx.service.submit_question_answer(
                question.id, None, None, True, action.open_id, commit=False
            )
            continue
        if value is None:
            continue
        label = value
        for option in question.options or []:
            if str(option.get("key")) == str(value):
                label = str(option.get("label") or value)
                break
        await ctx.service.submit_question_answer(
            question.id,
            value,
            label,
            False,
            action.open_id,
            commit=False,
        )
        submitted[question.question_id] = value

    if not ctx.service.interaction_is_resolvable(interaction.id):
        ctx.service.set_question_selections(interaction.id, selected)
        await _send_card(
            ctx,
            action.open_id,
            "还需要你的选择",
            "请完成卡片中的必答问题后再提交。",
            color="orange",
        )
        return None

    await ctx.service.resolve_question_interaction(interaction.id, commit=False)
    if approval is None and interaction.approval_id:
        approval = ctx.service.session.get(Approval, interaction.approval_id)
    if approval is not None and approval.status == "pending":
        await ctx.service.respond_approval(
            approval.id, "answer", action.open_id, commit=False
        )
    ctx.service.session.commit()

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
        "conversation",
        state=action.state or "ROOT_AGENT_WAITING_USER",
        project_name=project.name or project.id,
        chat_id=project.feishu_chat_id,
        chat_type=project.feishu_chat_type,
        submitted_answers=receipt_answers,
    )
    if action.message_id:
        receive_id, receive_id_type = _recipient(ctx, action.open_id)
        await deliver_card(
            ctx.service,
            ctx.bridge,
            dedupe_key=f"question-receipt:{interaction.id}",
            recipient_id=receive_id,
            recipient_type=receive_id_type,
            intent_type="question",
            card=receipt,
            project_id=project.id,
            update_message_id=action.message_id,
            projection_key=f"question:{interaction.id}",
            subject_type="question_interaction",
            subject_id=interaction.id,
            lifecycle_status="resolved_by_user",
            resolved_by=action.open_id,
            resulting_state="running",
        )

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
        "答案已返回同一个 Codex turn，Agent 正在继续。",
        color="green",
    )
    return receipt if not action.message_id else None
