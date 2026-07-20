"""Interactive menu cards for the Feishu bot.

All menu buttons set ``value.action`` to ``"menu"`` and put the actual semantic
in ``value.option_key``. This lets the shared card-action handler distinguish
menu clicks from approval/decision buttons.
"""

from typing import Any


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _lark_md(content: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _header(title: str, color: str = "blue") -> dict[str, Any]:
    return {"title": _plain_text(title), "template": color}


def _menu_button(
    label: str,
    option_key: str,
    button_type: str = "default",
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "action": "menu",
        "option_key": option_key,
        "state": "",
        "chat_id": chat_id,
        "chat_type": chat_type,
    }
    return {
        "tag": "button",
        "text": _plain_text(label),
        "type": button_type,
        "value": value,
    }


def build_main_menu_card(
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Main entry menu."""
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("MAC System 助手", "blue"),
        "elements": [
            {"tag": "div", "text": _lark_md("请选择你要执行的操作：")},
            {
                "tag": "action",
                "actions": [
                    _menu_button("创建项目", "create_prompt", "primary", chat_id, chat_type),
                    _menu_button("查看我的项目", "list_projects_status", chat_id=chat_id, chat_type=chat_type),
                    _menu_button("绑定到项目", "list_projects_bind", chat_id=chat_id, chat_type=chat_type),
                ],
            },
        ],
    }


def build_project_list_card(
    projects: list[dict[str, Any]],
    mode: str = "status",
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """List projects as clickable buttons.

    ``mode`` is one of ``status`` (view status), ``bind`` (bind user),
    or ``switch`` (set as active project in this chat).
    """
    if not projects:
        return {
            "config": {"wide_screen_mode": True},
            "header": _header("暂无项目", "grey"),
            "elements": [
                {"tag": "div", "text": _lark_md("还没有项目，点击“创建项目”开始。")},
                {"tag": "action", "actions": [_menu_button("返回", "show_menu", chat_id=chat_id, chat_type=chat_type)]},
            ],
        }

    elements: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for p in projects:
        project_id = p["id"]
        label = f"{p.get('name') or project_id} ({p['current_state']})"
        if mode == "status":
            actions.append(_menu_button(label, f"status:{project_id}", chat_id=chat_id, chat_type=chat_type))
        elif mode == "switch":
            actions.append(_menu_button(label, f"switch:{project_id}", chat_id=chat_id, chat_type=chat_type))
        else:
            actions.append(_menu_button(label, f"bind:{project_id}", chat_id=chat_id, chat_type=chat_type))

    # Feishu action rows look best with a few buttons; group into rows of 2.
    rows: list[dict[str, Any]] = []
    for i in range(0, len(actions), 2):
        rows.append({"tag": "action", "actions": actions[i : i + 2]})

    if mode == "status":
        title = "选择项目查看状态"
    elif mode == "switch":
        title = "选择项目设为默认"
    else:
        title = "选择项目绑定"
    elements.append({"tag": "div", "text": _lark_md("点击一个项目：")})
    elements.extend(rows)
    elements.append({"tag": "action", "actions": [_menu_button("返回", "show_menu", chat_id=chat_id, chat_type=chat_type)]})

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "blue"),
        "elements": elements,
    }


def build_create_prompt_card(
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Prompt the user to reply with a description."""
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("创建项目", "blue"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md(
                    "请直接回复一句话描述这个项目的需求。\n\n"
                    "例如：实现一个 config-validator CLI"
                ),
            },
            {"tag": "action", "actions": [_menu_button("返回", "show_menu", chat_id=chat_id, chat_type=chat_type)]},
        ],
    }


def build_pending_action_card(
    action_id: str,
    payload: dict[str, Any],
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Ask the user to authorize one frozen, chat-scoped action."""
    objective = str(payload.get("objective") or payload.get("description") or "开始执行")
    scope = [str(item) for item in payload.get("scope", [])]
    out_of_scope = [str(item) for item in payload.get("out_of_scope", [])]
    acceptance = [str(item) for item in payload.get("acceptance_criteria", [])]

    sections = [f"**目标**\n{objective}"]
    if scope:
        sections.append("**包含**\n" + "\n".join(f"- {item}" for item in scope[:6]))
    if out_of_scope:
        sections.append("**不包含**\n" + "\n".join(f"- {item}" for item in out_of_scope[:4]))
    if acceptance:
        sections.append("**完成标准**\n" + "\n".join(f"- {item}" for item in acceptance[:4]))

    def action_button(label: str, decision: str, button_type: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": _plain_text(label),
            "type": button_type,
            "value": {
                "action": "conversation_action",
                "decision": decision,
                "pending_action_id": action_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
            },
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": _header("确认执行范围", "orange"),
        "elements": [
            {"tag": "div", "text": _lark_md("\n\n".join(sections))},
            {"tag": "div", "text": _lark_md(f"执行请求ID：{action_id}")},
            {
                "tag": "action",
                "actions": [
                    action_button("按此范围开始", "confirm", "primary"),
                    action_button("继续讨论", "reject"),
                ],
            },
        ],
    }


_CONTROL_ACTION_TITLES = {
    "pause": "确认暂停项目",
    "resume": "确认恢复项目",
    "cancel": "确认取消项目",
    "rollback": "确认回滚项目",
}

_CONTROL_ACTION_DESCRIPTIONS = {
    "pause": "暂停后项目将停止推进，正在运行的 Agent 会在当前步骤结束后挂起；可随时恢复。",
    "resume": "恢复后项目将继续从上次的阶段推进。",
    "cancel": "取消后项目将终止，相关 Approval 与 PendingInput 会被清理；此操作不可恢复。",
    "rollback": "回滚后项目将回到目标阶段重新执行；当前阶段未完成的工作与待确认的审批会被清理。",
}

_ROLLBACK_TARGET_LABELS = {
    "requirements": "需求分析",
    "architecture": "方案设计",
    "specs": "实现计划",
    "coding": "编码",
    "testing": "测试",
}


def build_project_control_card(
    action_id: str,
    control_action: str,
    project_name: str,
    project_id: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
    target_stage: str | None = None,
) -> dict[str, Any]:
    """Ask the user to authorize a pause/resume/cancel/rollback on a project."""
    title = _CONTROL_ACTION_TITLES.get(control_action, "确认项目操作")
    description = _CONTROL_ACTION_DESCRIPTIONS.get(control_action, "")
    if control_action == "rollback" and target_stage:
        stage_label = _ROLLBACK_TARGET_LABELS.get(target_stage, target_stage)
        title = f"确认回滚到{stage_label}"
        description = (
            f"回滚后项目将回到 **{stage_label}** 阶段重新执行；"
            "当前阶段未完成的工作与待确认的审批会被清理。"
        )
    is_destructive = control_action in {"cancel", "rollback"}
    header_color = "red" if is_destructive else "orange"

    def action_button(label: str, decision: str, button_type: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": _plain_text(label),
            "type": button_type,
            "value": {
                "action": "conversation_action",
                "decision": decision,
                "pending_action_id": action_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
            },
        }

    body = [
        f"**目标项目**：{project_name}（`{project_id}`）",
        description,
        f"执行请求ID：{action_id}",
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, header_color),
        "elements": [
            {"tag": "div", "text": _lark_md("\n\n".join(part for part in body if part))},
            {
                "tag": "action",
                "actions": [
                    action_button(
                        "确认执行",
                        "confirm",
                        "danger" if is_destructive else "primary",
                    ),
                    action_button("再想想", "reject"),
                ],
            },
        ],
    }


def build_intent_confirm_card(
    task_type: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Ask the user to confirm or override the inferred task type."""
    all_types = ["feature", "bug_fix", "documentation"]
    actions: list[dict[str, Any]] = [
        _menu_button(
            f"是，{task_type}",
            f"intent_confirm:{task_type}",
            "primary",
            chat_id,
            chat_type,
        ),
    ]
    for other in all_types:
        if other != task_type:
            actions.append(
                _menu_button(
                    f"否，是 {other}",
                    f"intent_confirm:{other}",
                    chat_id=chat_id,
                    chat_type=chat_type,
                )
            )
    actions.append(_menu_button("重新描述", "create_prompt", chat_id=chat_id, chat_type=chat_type))
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("确认任务类型", "orange"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md(
                    f"这看起来是一个 **{task_type}** 任务，对吗？"
                ),
            },
            {"tag": "action", "actions": actions},
        ],
    }


def build_restart_type_card(
    project_id: str,
    description: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Ask the user to pick a new task type when restarting a project."""
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("重新归类创建", "orange"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md(
                    f"项目 **{project_id}** 当前要重新归类。\n\n"
                    f"原始描述：{description[:200]}"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    _menu_button("Feature", f"restart_with_type:{project_id}:feature", "primary", chat_id, chat_type),
                    _menu_button("Bug Fix", f"restart_with_type:{project_id}:bug_fix", chat_id=chat_id, chat_type=chat_type),
                    _menu_button("Documentation", f"restart_with_type:{project_id}:documentation", chat_id=chat_id, chat_type=chat_type),
                ],
            },
        ],
    }


def build_directory_candidate_card(
    candidates: list[dict[str, str]],
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Show detected repositories and let the user pick one to bind.

    ``candidates`` is a list of ``{"path": <abs path>, "name": <dir name>}``.
    Always appended at the end: a "manual input" button and a "cancel" button.
    """
    elements: list[dict[str, Any]] = []
    if candidates:
        elements.append({"tag": "div", "text": _lark_md("检测到以下 Git 仓库，点击绑定：")})
        actions: list[dict[str, Any]] = []
        for c in candidates:
            label = f"{c['name']} ({c['path']})"
            actions.append(
                _menu_button(label, f"bind_dir_select:{c['path']}", chat_id=chat_id, chat_type=chat_type)
            )
        # Group into rows of 2 for readability.
        rows: list[dict[str, Any]] = []
        for i in range(0, len(actions), 2):
            rows.append({"tag": "action", "actions": actions[i : i + 2]})
        elements.extend(rows)
    else:
        elements.append(
            {"tag": "div", "text": _lark_md("未在 `MAC_ALLOWED_BIND_ROOTS` 下检测到 Git 仓库。")}
        )

    elements.append(
        {
            "tag": "div",
            "text": _lark_md("也可以手输绝对路径，或取消绑定。"),
        }
    )
    elements.append(
        {
            "tag": "action",
            "actions": [
                _menu_button("手输绝对路径", "bind_dir_manual", "primary", chat_id, chat_type),
                _menu_button("取消", "bind_dir_cancel", chat_id=chat_id, chat_type=chat_type),
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": _header("选择要绑定的目录", "blue"),
        "elements": elements,
    }


def build_bind_dir_for_create_card(
    candidates: list[dict[str, str]],
    description: str,
    task_type: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Show agent-recommended repos during `/create <description>` flow.

    User picks a candidate -> bind that dir then create the project.
    User clicks "skip" -> create the project with the system workspace.
    """
    elements: list[dict[str, Any]] = []
    preview = description if len(description) <= 200 else description[:200] + "…"
    elements.append(
        {
            "tag": "div",
            "text": _lark_md(
                f"根据你的需求描述，推荐以下目录（点击后将**自动创建并初始化 Git**）：\n\n> {preview}\n\n"
                "选择一个目录绑定后创建项目，或跳过使用系统工作区。"
            ),
        }
    )
    actions: list[dict[str, Any]] = []
    for c in candidates:
        label = f"{c['name']} ({c['path']})"
        actions.append(
            _menu_button(
                label, f"bind_dir_for_create_select:{c['path']}",
                chat_id=chat_id, chat_type=chat_type,
            )
        )
    rows: list[dict[str, Any]] = []
    for i in range(0, len(actions), 2):
        rows.append({"tag": "action", "actions": actions[i : i + 2]})
    elements.extend(rows)
    elements.append(
        {
            "tag": "action",
            "actions": [
                _menu_button(
                    "跳过，使用系统工作区", "bind_dir_for_create_skip",
                    chat_id=chat_id, chat_type=chat_type,
                ),
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"为 {task_type} 项目选择目录", "blue"),
        "elements": elements,
    }
