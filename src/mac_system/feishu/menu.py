"""Project-level Feishu menu cards."""

from __future__ import annotations

from typing import Any


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _lark_md(content: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _header(title: str, color: str = "blue") -> dict[str, Any]:
    return {"title": _plain_text(title), "template": color}


def _button(
    label: str,
    option_key: str,
    button_type: str = "default",
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": _plain_text(label),
        "type": button_type,
        "value": {
            "action": "menu",
            "option_key": option_key,
            "state": "",
            "chat_id": chat_id,
            "chat_type": chat_type,
        },
    }


def build_main_menu_card(
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("MAC", "blue"),
        "elements": [
            {"tag": "div", "text": _lark_md("选择一个项目操作：")},
            {
                "tag": "action",
                "actions": [
                    _button("创建项目", "create_prompt", "primary", chat_id, chat_type),
                    _button("打开目录", "open_directory", chat_id=chat_id, chat_type=chat_type),
                    _button("查看项目", "list_projects_status", chat_id=chat_id, chat_type=chat_type),
                    _button("切换项目", "list_projects_switch", chat_id=chat_id, chat_type=chat_type),
                ],
            },
            {
                "tag": "action",
                "actions": [
                    _button("Coding-Agent", "show_agents", chat_id=chat_id, chat_type=chat_type),
                ],
            },
        ],
    }


def build_project_list_card(
    projects: list[dict[str, Any]],
    mode: str = "status",
    chat_id: str | None = None,
    chat_type: str = "p2p",
    *,
    page: int = 0,
    total_pages: int = 1,
    query: str = "",
) -> dict[str, Any]:
    if not projects:
        return {
            "config": {"wide_screen_mode": True},
            "header": _header("暂无项目", "grey"),
            "elements": [
                {"tag": "div", "text": _lark_md("没有匹配的项目或可打开的 Git 仓库。")},
                {
                    "tag": "action",
                    "actions": [_button("创建项目", "create_prompt", "primary", chat_id, chat_type)],
                },
            ],
        }

    action_prefix = "switch" if mode == "switch" else "status"
    state_labels = {
        "idle": "空闲",
        "running": "执行中",
        "owner_reviewing": "AI 正在判断",
        "waiting_user": "等你决定",
        "paused": "已暂停",
        "failed": "失败",
    }
    rows = []
    for project in projects:
        unopened = "path" in project
        name = str(project.get("name") or project.get("id"))
        state = project.get("execution_status", "idle")
        label = "未打开" if unopened else state_labels.get(state, state)
        path = project.get("path") or project.get("workspace_path")
        detail = f"`{path}`" if path else f"`{project['id']}`"
        if not unopened and path:
            detail += f"\n`{project['id']}`"
        rows.extend([
            {"tag": "div", "text": _lark_md(detail)},
            {
                "tag": "action",
                "actions": [_button(
                    f"{'当前 · ' if project.get('is_current') else ''}{name}（{label}）",
                    (
                        f"bind_dir_select:{project['path']}" if unopened
                        else f"{action_prefix}:{project['id']}"
                    ),
                    chat_id=chat_id,
                    chat_type=chat_type,
                )],
            },
        ])
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(_button(
                "上一页", f"project_list_page:{mode}:{page - 1}:{query}",
                chat_id=chat_id, chat_type=chat_type,
            ))
        if page + 1 < total_pages:
            navigation.append(_button(
                "下一页", f"project_list_page:{mode}:{page + 1}:{query}",
                chat_id=chat_id, chat_type=chat_type,
            ))
        rows.extend([
            {"tag": "div", "text": _lark_md(f"第 {page + 1} / {total_pages} 页")},
            {"tag": "action", "actions": navigation},
        ])
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("选择项目", "blue"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md(
                    (
                        "点击项目查看状态，或打开尚未使用的 Git 仓库："
                        if mode == "status"
                        else "点击项目切换，或打开 Git 仓库；也可发送 `/use 名称` 或 `/use 绝对路径`："
                    )
                ),
            },
            *rows,
            {
                "tag": "action",
                "actions": [_button("返回", "show_menu", chat_id=chat_id, chat_type=chat_type)],
            },
        ],
    }


def build_create_prompt_card(
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("创建项目", "blue"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md("请直接回复你的需求。下一条消息会原样交给 Codex。"),
            },
            {
                "tag": "action",
                "actions": [_button("返回", "show_menu", chat_id=chat_id, chat_type=chat_type)],
            },
        ],
    }


def build_directory_candidate_card(
    candidates: list[dict[str, str]],
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    actions = [
        _button(
            f"{candidate['name']}（{candidate['path']}）",
            f"bind_dir_select:{candidate['path']}",
            chat_id=chat_id,
            chat_type=chat_type,
        )
        for candidate in candidates
    ]
    rows = [
        {"tag": "action", "actions": actions[index : index + 2]}
        for index in range(0, len(actions), 2)
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": _header("打开已有项目", "blue"),
        "elements": [
            {
                "tag": "div",
                "text": _lark_md(
                    "请选择要打开的 Git 仓库。" if candidates else "没有检测到可打开的 Git 仓库。"
                ),
            },
            *rows,
            {
                "tag": "action",
                "actions": [
                    _button("手输绝对路径", "bind_dir_manual", "primary", chat_id, chat_type),
                    _button("取消", "bind_dir_cancel", chat_id=chat_id, chat_type=chat_type),
                ],
            },
        ],
    }
