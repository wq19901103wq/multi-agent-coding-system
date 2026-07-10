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


def build_confirmation_card(
    title: str,
    summary: str,
    details: dict[str, Any],
    options: list[dict[str, Any]],
    approval_id: str,
    state: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Build a Feishu interactive confirmation card."""
    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "div", "text": _lark_md(f"**摘要**：{summary}")})
    for key, value in details.items():
        elements.append({"tag": "div", "text": _lark_md(f"**{key}**：{value}")})

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
