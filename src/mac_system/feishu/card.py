"""Feishu cards used by the thin Codex host."""

from __future__ import annotations

import re
from typing import Any

from mac_system.utils.formatting import format_card_markdown


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _lark_md(content: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _header(title: str, color: str = "blue") -> dict[str, Any]:
    return {"title": _plain_text(title), "template": color}


def build_content_card(
    title: str,
    content: str,
    page: int = 1,
    total: int = 1,
    color: str = "blue",
) -> dict[str, Any]:
    """Render one report/progress/result card."""

    page = page if isinstance(page, int) else 1
    total = total if isinstance(total, int) else 1
    header_title = title if total <= 1 else f"{title}（{page}/{total}）"
    summary = re.sub(r"[`*_#>\[\]]", "", f"{header_title}\n{content}")
    summary = re.sub(r"\s+", " ", summary).strip()[:500]
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "summary": {"content": summary},
        },
        "header": _header(header_title, color),
        "body": {
            "elements": [
                {"tag": "markdown", "content": format_card_markdown(content)}
            ]
        },
    }


def build_status_card(
    title: str,
    project_id: str,
    current_state: str,
    display: str,
    recent_events: list[dict[str, Any]],
    action_needed: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Render the durable project posture without a synthetic workflow."""

    del project_id, current_state
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**当前状态**：{display}")}
    ]
    if workspace:
        elements.append(
            {"tag": "div", "text": _lark_md(f"**工作目录**：{workspace}")}
        )
    if action_needed:
        elements.append(
            {
                "tag": "div",
                "text": _lark_md(f"**当前需要你处理**：{action_needed}"),
            }
        )
    if recent_events:
        lines = "\n".join(
            f"- {event.get('message', '')}" for event in recent_events[:4]
        )
        elements.append(
            {"tag": "div", "text": _lark_md(f"**关键进展**：\n{lines}")}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "green"),
        "elements": elements,
    }


def _inline_free_text_form(
    approval_id: str,
    interaction_id: str,
    question_id: str,
    state: str,
    chat_id: str | None,
    chat_type: str,
) -> dict[str, Any]:
    safe_id = re.sub(r"[^A-Za-z0-9_]", "_", question_id)
    if not safe_id or not safe_id[0].isalpha():
        safe_id = f"q_{safe_id}"
    safe_id = safe_id[:12]
    callback_value = {
        "approval_id": approval_id,
        "interaction_id": interaction_id,
        "action": "submit_inline_free_text",
        "question_id": question_id,
        "state": state,
        "chat_id": chat_id,
        "chat_type": chat_type,
    }
    return {
        "tag": "form",
        "name": f"form_{safe_id}",
        "elements": [
            {
                "tag": "input",
                "element_id": f"input_{safe_id}",
                "name": "inline_free_text_value",
                "required": True,
                "input_type": "multiline_text",
                "width": "fill",
                "placeholder": _plain_text("或直接在此输入你的答案..."),
            },
            {
                "tag": "button",
                "text": _plain_text("提交自定义答案"),
                "type": "primary_filled",
                "name": f"submit_{safe_id}",
                "form_action_type": "submit",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": callback_value,
                    }
                ],
            },
        ],
    }


def build_question_batch_card(
    approval_id: str,
    interaction_id: str,
    questions: list[dict[str, Any]],
    agent: str,
    stage: str,
    impact: str | None = None,
    state: str = "",
    project_name: str = "",
    chat_id: str | None = None,
    chat_type: str = "p2p",
    selected_options: dict[str, str] | None = None,
    submitted_answers: dict[str, str] | None = None,
    awaiting_free_text: bool = False,
    inline_free_text_question_id: str | None = None,
) -> dict[str, Any]:
    """Render Codex `request_user_input` as a state-synchronized card."""

    selected_options = selected_options or {}
    submitted_answers = submitted_answers or {}
    is_submitted = bool(submitted_answers)
    all_submitted = is_submitted and all(
        (question.get("question_id") or question.get("db_id") or "")
        in submitted_answers
        for question in questions
    )
    use_inline_form = not is_submitted and any(
        question.get("allow_free_text")
        or (
            inline_free_text_question_id
            and (question.get("question_id") or question.get("db_id"))
            == inline_free_text_question_id
        )
        for question in questions
    )
    elements: list[dict[str, Any]] = []
    if impact:
        elements.append(
            {"tag": "markdown", "content": f"**影响说明**：{impact}"}
        )
    if not is_submitted:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"共 {len(questions)} 个问题。点击选项只会暂存，"
                    "全部选好后请点 **提交答案**。"
                ),
            }
        )

    for question in questions:
        question_id = str(
            question.get("question_id") or question.get("db_id") or ""
        )
        options = question.get("options") or []
        selected = selected_options.get(question_id)
        final_answer = submitted_answers.get(question_id)
        is_question_submitted = question_id in submitted_answers
        text = f"**{question.get('question', '')}**"
        reason = str(question.get("reason") or "")
        if reason:
            text += f"\n{reason}"
        if is_question_submitted:
            text += f"\n已提交：{final_answer}"
        elif selected:
            text += f"\n已选：{selected}"
        elements.extend([{"tag": "hr"}, {"tag": "markdown", "content": text}])

        for option in options:
            label = str(option.get("label") or option.get("key") or "")
            key = str(option.get("key") or label)
            button: dict[str, Any] = {
                "tag": "button",
                "text": _plain_text(label),
                "type": (
                    "primary_filled"
                    if key in {selected, final_answer}
                    else "default"
                ),
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "approval_id": approval_id,
                            "interaction_id": interaction_id,
                            "action": "select_question_option",
                            "question_id": question_id,
                            "option_key": key,
                            "state": state,
                            "chat_id": chat_id,
                            "chat_type": chat_type,
                        },
                    }
                ],
            }
            if is_question_submitted:
                button["disabled"] = True
            elements.append(button)
        if use_inline_form and (
            question.get("allow_free_text")
            or question_id == inline_free_text_question_id
        ):
            elements.append(
                _inline_free_text_form(
                    approval_id,
                    interaction_id,
                    question_id,
                    state,
                    chat_id,
                    chat_type,
                )
            )

    elements.append({"tag": "hr"})
    if all_submitted:
        status = (
            "等待补充说明"
            if awaiting_free_text
            else "✅ 已提交，Codex 正在继续"
        )
        elements.append({"tag": "markdown", "content": status})
    else:
        actions: list[dict[str, Any]] = [
            {
                "tag": "button",
                "text": _plain_text("提交答案"),
                "type": "primary_filled",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "approval_id": approval_id,
                            "interaction_id": interaction_id,
                            "action": "submit_selected_options",
                            "state": state,
                            "chat_id": chat_id,
                            "chat_type": chat_type,
                        },
                    }
                ],
            }
        ]
        if any(question.get("kind") == "non_blocking" for question in questions):
            actions.append(
                {
                    "tag": "button",
                    "text": _plain_text("跳过未答"),
                    "type": "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "approval_id": approval_id,
                                "interaction_id": interaction_id,
                                "action": "skip_remaining",
                                "state": state,
                                "chat_id": chat_id,
                                "chat_type": chat_type,
                            },
                        }
                    ],
                }
            )
        elements.extend(actions)

    stage_label = stage or agent
    title = "已提交" if all_submitted else f"需要确认 {len(questions)} 个问题"
    prefix = f"[{project_name}] " if project_name else ""
    card = {
        "config": {"wide_screen_mode": True},
        "header": _header(
            f"{prefix}[{stage_label}] {title}",
            "green" if all_submitted else "orange",
        ),
    }
    return {"schema": "2.0", **card, "body": {"elements": elements}}
