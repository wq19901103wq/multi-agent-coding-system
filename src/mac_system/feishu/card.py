from typing import Any


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _lark_md(content: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _header(title: str, color: str = "blue") -> dict[str, Any]:
    return {
        "title": _plain_text(title),
        "template": color,
    }


def build_content_card(
    title: str,
    content: str,
    page: int = 1,
    total: int = 1,
    color: str = "blue",
) -> dict[str, Any]:
    """Build a plain display card for streaming long markdown content.

    Use this for multi-card content streams: each card shows one chunk of
    markdown, and the caller sends the action buttons on the final card.
    """
    header_title = title if total <= 1 else f"{title}（{page}/{total}）"
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(content)},
    ]
    if total > 1:
        elements.append({
            "tag": "note",
            "elements": [_plain_text(f"第 {page}/{total} 页")],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(header_title, color),
        "elements": elements,
    }


def build_confirmation_card(
    title: str,
    summary: str,
    details: dict[str, Any],
    options: list[dict[str, Any]],
    approval_id: str,
    state: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
    show_actions: bool = True,
) -> dict[str, Any]:
    """Build a Feishu interactive confirmation card."""
    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "div", "text": _lark_md(f"**摘要**：{summary}")})
    for key, value in details.items():
        elements.append({"tag": "div", "text": _lark_md(f"**{key}**：{value}")})

    if show_actions:
        actions: list[dict[str, Any]] = []
        for opt in options:
            value = {
                "approval_id": approval_id,
                "action": opt["action"],
                "state": state,
                "chat_id": chat_id,
                "chat_type": chat_type,
            }
            if "decision_id" in opt:
                value["decision_id"] = opt["decision_id"]
            if "option_key" in opt:
                value["option_key"] = opt["option_key"]
            actions.append({
                "tag": "button",
                "text": _plain_text(opt["label"]),
                "type": opt.get("type", "default"),
                "value": value,
            })

        if actions:
            elements.append({"tag": "action", "actions": actions})

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "blue"),
        "elements": elements,
    }


def build_input_card(
    title: str,
    question: str,
    context: str,
    approval_id: str,
    state: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Build a Feishu interactive input/clarification card."""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**问题**：{question}")},
    ]
    if context:
        elements.append({"tag": "div", "text": _lark_md(f"**上下文**：{context}")})
    elements.append({"tag": "div", "text": _lark_md("请直接回复你的答案。")})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": _plain_text("我已回复"),
            "type": "primary",
            "value": {
                "approval_id": approval_id,
                "action": "answered",
                "state": state,
                "chat_id": chat_id,
                "chat_type": chat_type,
            },
        }],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "orange"),
        "elements": elements,
    }


def build_transition_card(
    project_name: str,
    from_display: str,
    to_display: str,
    from_agent: str,
    to_agent: str,
    status_note: str = "",
    from_state_type: str = "",
) -> dict[str, Any]:
    """Build a card for state transitions showing stage change and agent handoff."""
    if from_state_type == "human_confirmation":
        from_line = f"✅ 已确认：{from_display}"
    else:
        from_line = f"✅ {from_display} 已完成"

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**{project_name}**")},
        {"tag": "hr"},
        {"tag": "div", "text": _lark_md(from_line)},
        {"tag": "div", "text": _lark_md(f"🔄 进入 {to_display}")},
        {"tag": "hr"},
    ]
    if from_agent and to_agent and from_agent != to_agent:
        elements.append(
            {"tag": "div", "text": _lark_md(f"{from_agent} → {to_agent}")}
        )
    elif to_agent:
        elements.append(
            {"tag": "div", "text": _lark_md(f"执行者：{to_agent}")}
        )
    if status_note:
        elements.append({"tag": "div", "text": _lark_md(status_note)})

    return {
        "config": {"wide_screen_mode": True},
        "header": _header("状态流转", "blue"),
        "elements": elements,
    }


def build_feedback_card(
    project_name: str,
    selected_label: str,
    is_rejection: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Build a small card confirming the user's card action."""
    content = f"你选择了「{selected_label}」"
    if is_rejection:
        content += "\n\n项目将返回上一阶段，请直接回复修改意见。"
    elif note:
        content += f"\n\n{note}"
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"操作确认 — {project_name}", "green"),
        "elements": [
            {"tag": "div", "text": _lark_md(content)},
        ],
    }


def build_status_card(
    title: str,
    project_id: str,
    current_state: str,
    display: str,
    recent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a Feishu status update card."""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**项目**：{project_id}")},
        {"tag": "div", "text": _lark_md(f"**当前状态**：{display} ({current_state})")},
    ]
    if recent_events:
        lines = "\n".join(f"- {e.get('level', 'INFO')}: {e.get('message', '')}" for e in recent_events)
        elements.append({"tag": "div", "text": _lark_md(f"**最近事件**：\n{lines}")})

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "green"),
        "elements": elements,
    }
