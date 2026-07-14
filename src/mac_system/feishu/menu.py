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
                f"根据你的需求描述，检测到可能匹配的代码目录：\n\n> {preview}\n\n"
                "选择一个目录绑定后再创建项目，或跳过使用系统工作区。"
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
