"""Feishu cards used by the thin Codex host."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from mac_system.utils.formatting import format_card_markdown
from mac_system.utils.usage import canonical_usage, token_usage_summary


def _plain_text(content: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": content}


def _lark_md(content: str) -> dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _header(title: str, color: str = "blue") -> dict[str, Any]:
    return {"title": _plain_text(title), "template": color}


def _compact_label(value: str, limit: int = 16) -> str:
    """Keep secondary card metadata readable on a phone-sized surface."""

    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}…"


def build_content_card(
    title: str,
    content: str,
    page: int = 1,
    total: int = 1,
    color: str = "blue",
    *,
    footer: list[str] | None = None,
    details: list[tuple[str, list[str], bool]] | None = None,
    owner_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render one report/progress/result card."""

    page = page if isinstance(page, int) else 1
    total = total if isinstance(total, int) else 1
    header_title = title if total <= 1 else f"{title}（{page}/{total}）"
    summary = re.sub(r"[`*_#>\[\]]", "", f"{header_title}\n{content}")
    summary = re.sub(r"\s+", " ", summary).strip()[:500]
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": format_card_markdown(content)}
    ]
    if owner_review:
        _append_owner_review(elements, owner_review)
    _append_secondary_sections(
        elements,
        footer=[line for line in (footer or []) if line.strip()],
        details=[
            (panel_title, rows, expanded)
            for panel_title, rows, expanded in (details or [])
            if panel_title.strip() and any(row.strip() for row in rows)
        ],
    )
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "summary": {"content": summary},
        },
        "header": _header(header_title, color),
        "body": {"elements": elements},
    }


def _append_owner_review(
    elements: list[dict[str, Any]], owner_review: dict[str, Any]
) -> None:
    """Keep the Owner conclusion on the result it evaluated.

    The result card is the timeline event.  The Owner conclusion is one small
    section of that event; rationale, evidence and runtime metadata belong in
    the shared collapsed details panels assembled by the caller.
    """

    outcome = str(owner_review.get("outcome") or "no_action")
    decision = str(owner_review.get("decision") or "").strip()
    if outcome == "no_action" and not decision:
        return
    label = {
        "decide": "Owner 决定",
        "advise": "Owner 建议",
        "require_user": "需要你决定",
        "resolved_by_user": "你的选择",
        "adjustment_requested": "你的调整",
    }.get(outcome, "Owner 结论")
    if decision:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**{label}**：{format_card_markdown(decision)}",
                },
            ]
        )

    decision_id = str(owner_review.get("decision_id") or "").strip()
    chat_id = owner_review.get("chat_id")
    chat_type = str(owner_review.get("chat_type") or "p2p")
    visible_options = [
        {
            "label": str(item.get("label") or "").strip()[:80],
            "description": str(item.get("description") or "").strip()[:240],
        }
        for item in (owner_review.get("options") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    if outcome in {"advise", "require_user"} and decision_id and visible_options:
        for index, option in enumerate(visible_options):
            if option["description"]:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": (
                            f"**{format_card_markdown(option['label'])}**："
                            f"{format_card_markdown(option['description'])}"
                        ),
                    }
                )
            elements.append(
                {
                    "tag": "button",
                    "text": _plain_text(option["label"]),
                    "type": "primary_filled" if index == 0 else "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "approval_id": decision_id,
                                "decision_id": decision_id,
                                "action": "select_owner_option",
                                "option_key": option["label"],
                                "chat_id": chat_id,
                                "chat_type": chat_type,
                            },
                        }
                    ],
                }
            )
    if (
        outcome == "decide"
        and decision_id
        and bool(owner_review.get("reversible"))
    ):
        elements.append(
            {
                "tag": "button",
                "text": _plain_text("调整决定"),
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "approval_id": decision_id,
                            "decision_id": decision_id,
                            "action": "adjust_project_decision",
                            "chat_id": chat_id,
                            "chat_type": chat_type,
                        },
                    }
                ],
            }
        )


def build_project_decision_card(
    decision_id: str,
    *,
    project_name: str,
    question: str = "",
    decision: str,
    rationale: str,
    impact: str,
    reversible: bool,
    options: list[dict[str, Any]] | None = None,
    status: str = "applied",
    user_override: str | None = None,
    actor_model: str | None = None,
    actor_usage: dict[str, int] | None = None,
    actor_duration_seconds: float | None = None,
    chat_id: str | None = None,
    chat_type: str = "p2p",
    details_expanded: bool = False,
    execution_status: str = "not_applicable",
    execution_rounds: int = 0,
) -> dict[str, Any]:
    """Render one durable, non-blocking AI Project Owner decision.

    The decision itself is the mobile-first summary. Evidence uses the same
    native ellipsis panel as execution history, while Agent/model/usage uses
    the same collapsed detail panel as progress cards.
    """

    status_text = {
        "applied": "AI 已替你完成决定",
        "requires_user": "AI 没有擅自决定，等待你的选择",
        "awaiting_adjustment": "等待你说明要怎么调整",
        "adjustment_requested": "已收到你的调整要求",
        "application_failed": "决定未能应用，已改为询问你",
        "resolved_by_user": "已按你的选择继续",
        "expired": "原问题已经失效",
        "superseded": "这项决定已被后续决定替代",
        "advice": "这是一条建议，等待你的选择",
    }.get(status, "AI 决策记录")
    primary_label = (
        "建议"
        if status == "advice"
        else "需要你选择"
        if status == "requires_user"
        else "你的选择"
        if status == "resolved_by_user"
        else "决定"
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{primary_label}**：{format_card_markdown(decision)}",
        }
    ]
    execution_label = {
        "active": "正在执行",
        "completed": "已完成",
        "blocked": "已阻塞",
        "needs_revision": "需要调整方向",
        "superseded": "已被新决定替代",
    }.get(execution_status)
    if execution_label:
        round_text = f" · {execution_rounds} 轮" if execution_rounds else ""
        elements.append(
            {
                "tag": "markdown",
                "content": f"**进展**：{execution_label}{round_text}",
            }
        )
    if user_override:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**你的调整**：{format_card_markdown(user_override)}",
            }
        )
    if status not in {"applied", "requires_user", "advice"}:
        elements.append(
            {"tag": "markdown", "content": format_card_markdown(status_text)}
        )
    evidence_rows: list[str] = []
    if question:
        evidence_rows.append(f"事项：{question}")
    if rationale:
        evidence_rows.append(f"依据：{rationale}")
    if impact:
        evidence_rows.append(f"影响：{impact}")
    if status == "requires_user":
        evidence_rows.extend(["处理方式：需要你决定", status_text])
    elif status == "advice":
        evidence_rows.append(status_text)
    else:
        evidence_rows.append(f"可调整：{'是' if reversible else '否'}")
        if status == "applied":
            evidence_rows.append(status_text)
    # The decision card is a user-facing proxy, not an audit report. Keep the
    # default surface result-first; rationale and runtime evidence remain
    # available only when the user expands the shared ellipsis panel.
    if evidence_rows:
        elements.append(
            _progress_panel("···", evidence_rows, expanded=details_expanded)
        )

    visible_options = [
        {
            "label": str(item.get("label") or "").strip()[:80],
            "description": str(item.get("description") or "").strip()[:240],
        }
        for item in (options or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    if status in {"requires_user", "advice"} and visible_options:
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": "**请选择一项**"}
        )
        for index, option in enumerate(visible_options):
            if option["description"]:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": (
                            f"**{format_card_markdown(option['label'])}**："
                            f"{format_card_markdown(option['description'])}"
                        ),
                    }
                )
            elements.append(
                {
                    "tag": "button",
                    "text": _plain_text(option["label"]),
                    "type": "primary_filled" if index == 0 else "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "approval_id": decision_id,
                                "decision_id": decision_id,
                                "action": "select_owner_option",
                                "option_key": option["label"],
                                "chat_id": chat_id,
                                "chat_type": chat_type,
                            },
                        }
                    ],
                }
            )

    agent_rows = [
        f"Project Owner{f' · {actor_model}' if actor_model else ' · 模型未报告'}"
    ]
    usage_line = token_usage_summary(
        "Project Owner", canonical_usage(actor_usage)
    )
    agent_rows.append(usage_line or "Token · Project Owner：后端未报告")
    duration = None
    if isinstance(actor_duration_seconds, (int, float)):
        duration = max(0, round(actor_duration_seconds))
    footer: list[str] = []
    _append_secondary_sections(
        elements,
        footer=footer,
        details=[("Agent 与用量详情", agent_rows + ([f"用时 {duration} 秒"] if duration is not None else []), False)],
    )
    if status == "applied" and reversible:
        elements.append(
            {
                "tag": "button",
                "text": _plain_text("调整决定"),
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "approval_id": decision_id,
                            "decision_id": decision_id,
                            "action": "adjust_project_decision",
                            "chat_id": chat_id,
                            "chat_type": chat_type,
                        },
                    }
                ],
            }
        )
    summary = re.sub(
        r"\s+",
        " ",
        re.sub(
            r"[`*_#>\[\]]",
            "",
            (
                f"{project_name} "
                f"{'AI 建议' if status == 'advice' else '需要你选择' if status == 'requires_user' else '你的选择' if status == 'resolved_by_user' else 'AI 已替你决定'} "
                f"{decision}"
            ),
        ),
    ).strip()[:500]
    return {
        "schema": "2.0",
        "config": {"width_mode": "default", "summary": {"content": summary}},
        "header": _header(
            (
                "AI 建议"
                if status == "advice"
                else "需要你选择"
                if status == "requires_user"
                else "你的选择"
                if status == "resolved_by_user"
                else "AI 决策记录"
            ),
            {
                "applied": "green",
                "requires_user": "orange",
                "awaiting_adjustment": "orange",
                "adjustment_requested": "blue",
                "application_failed": "red",
                "resolved_by_user": "green",
                "expired": "grey",
                "superseded": "grey",
                "advice": "turquoise",
            }.get(status, "turquoise"),
        ),
        "body": {"elements": elements},
    }


def _progress_panel(
    title: str,
    rows: list[str],
    *,
    expanded: bool,
) -> dict[str, Any]:
    """Build a compact cc-connect-style history panel for mobile."""

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "background_color": "grey",
        "header": {
            "title": _plain_text(title),
        },
        "border": {"color": "grey"},
        "vertical_spacing": "8px",
        "padding": "4px 8px",
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": row,
                    "text_size": "notation",
                },
            }
            for row in rows
            if row.strip()
        ],
    }


def _progress_rows(rows: list[str]) -> list[dict[str, Any]]:
    """Render visible timeline rows without adding another collapsible layer."""

    return [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": row,
                "text_size": "notation",
            },
        }
        for row in rows
        if row.strip()
    ]


def _append_secondary_sections(
    elements: list[dict[str, Any]],
    *,
    footer: list[str],
    details: list[tuple[str, list[str], bool]],
) -> None:
    """Append shared mobile metadata hierarchy to any execution-related card."""

    if footer:
        elements.append({"tag": "hr"})
        elements.extend(
            {
                "tag": "markdown",
                "content": format_card_markdown(line),
                "text_size": "notation",
            }
            for line in footer
            if line.strip()
        )
    for panel_title, rows, expanded in details:
        if panel_title.strip() and any(row.strip() for row in rows):
            elements.append(_progress_panel(panel_title, rows, expanded=expanded))


def build_progress_card(
    title: str,
    current: str,
    *,
    steps: list[str] | None = None,
    activities: list[str] | None = None,
    hidden_steps: list[str] | None = None,
    hidden_activities: list[str] | None = None,
    details: list[tuple[str, list[str], bool]] | None = None,
    footer: list[str] | None = None,
    color: str = "blue",
) -> dict[str, Any]:
    """Render one in-place progress card with content-first hierarchy.

    Current work is primary. Ordered history stays in compact panels. Runtime
    metadata is deliberately rendered as notation-sized footer text, matching
    the hierarchy used by cc-connect's Feishu rich cards.
    """

    def bounded_rows(
        rows: list[str] | None,
        limit: int,
        description: str,
    ) -> list[str]:
        cleaned = [row for row in (rows or []) if row.strip()]
        if len(cleaned) <= limit:
            return cleaned
        retained = max(1, limit - 1)
        omitted = len(cleaned) - retained
        return [f"… 较早 {omitted} 条{description}已归档", *cleaned[-retained:]]

    # This is the final Feishu rendering boundary.  Keep the card's nested
    # element count bounded even if a caller accidentally supplies an
    # unbounded execution history.
    steps = bounded_rows(steps, 8, "步骤")
    activities = bounded_rows(activities, 7, "活动")
    hidden_steps = bounded_rows(hidden_steps, 4, "步骤")
    hidden_activities = bounded_rows(hidden_activities, 3, "活动")
    details = [
        (panel_title, bounded_rows(rows, 9, "详情"), expanded)
        for panel_title, rows, expanded in (details or [])
        if panel_title.strip() and any(row.strip() for row in rows)
    ]
    footer = [line for line in (footer or []) if line.strip()]
    elements: list[dict[str, Any]] = []
    if current.strip():
        elements.append(
            {"tag": "markdown", "content": format_card_markdown(current)}
        )
    if hidden_steps or hidden_activities:
        # Overflow is part of the execution timeline, so the ellipsis must
        # occupy the position of the rows it replaces. Keeping all hidden
        # rows in one trailing panel made old history appear newer than the
        # current step.
        elements.append(
            {
                "tag": "markdown",
                "content": format_card_markdown("**执行步骤**"),
            }
        )
        if hidden_steps:
            elements.append(_progress_panel("···", hidden_steps, expanded=False))
        elements.extend(_progress_rows(steps))
        if activities:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "当前步骤详情",
                        "text_size": "notation",
                    },
                }
            )
            if hidden_activities:
                elements.append(
                    _progress_panel("···", hidden_activities, expanded=False)
                )
            elements.extend(_progress_rows(activities))
    else:
        execution_rows = list(steps)
        if activities:
            if execution_rows:
                execution_rows.append("当前步骤详情")
            execution_rows.extend(activities)
        if execution_rows:
            elements.append(
                _progress_panel("执行步骤", execution_rows, expanded=True)
            )
    _append_secondary_sections(elements, footer=footer, details=details)
    summary = re.sub(r"[`*_#>\[\]]", "", f"{title}\n{current}")
    summary = re.sub(r"\s+", " ", summary).strip()[:500]
    return {
        "schema": "2.0",
        "config": {
            "width_mode": "default",
            "summary": {"content": summary},
        },
        "header": _header(title, color),
        "body": {"elements": elements},
    }


def build_status_card(
    title: str,
    project_id: str,
    display: str,
    recent_events: list[dict[str, Any]],
    action_needed: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Render the durable project posture without a synthetic workflow."""

    del project_id
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
    digest = hashlib.sha256(question_id.encode("utf-8")).hexdigest()[:8]
    safe_id = f"{safe_id[:12]}_{digest}"
    input_name = f"inline_free_text_value_{safe_id}"
    callback_value = {
        "approval_id": approval_id,
        "interaction_id": interaction_id,
        "action": "submit_inline_free_text",
        "question_id": question_id,
        "state": state,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "input_name": input_name,
    }
    return {
        "tag": "form",
        "name": f"form_{safe_id}",
        "elements": [
            {
                "tag": "input",
                "element_id": f"input_{safe_id}",
                "name": input_name,
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
    resolution_note: str | None = None,
    pending_notice: str | None = None,
    followup_adjustment: str | None = None,
    awaiting_free_text: bool = False,
    inline_free_text_question_id: str | None = None,
    owner_decision: dict[str, Any] | None = None,
    owner_reviewing: bool = False,
) -> dict[str, Any]:
    """Render Codex `request_user_input` as a state-synchronized card."""

    selected_options = selected_options or {}
    submitted_answers = submitted_answers or {}
    is_native_approval = stage == "native_approval"
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

    if all_submitted and is_native_approval:
        # A resolved approval is historical context, not an active form. Keep
        # the chosen outcome immediately visible and put the long command,
        # reason and other request details behind the same ellipsis pattern
        # used by progress cards. Disabled buttons made old approvals look
        # actionable and consumed most of a mobile screen.
        answer_lines = [
            str(submitted_answers.get(
                str(question.get("question_id") or question.get("db_id") or ""),
                "",
            )).strip()
            for question in questions
        ]
        answer_lines = [answer for answer in answer_lines if answer]
        if answer_lines:
            elements.append(
                {
                    "tag": "markdown",
                    "content": format_card_markdown(
                        "**已选择**：" + "；".join(answer_lines)
                    ),
                }
            )
        if resolution_note:
            elements.append(
                {
                    "tag": "markdown",
                    "content": format_card_markdown(resolution_note),
                }
            )
        if owner_decision:
            decision_text = str(owner_decision.get("decision") or "").strip()
            if decision_text:
                elements.append(
                    {
                        "tag": "markdown",
                        "content": format_card_markdown(
                            f"**Project Owner**：{decision_text}"
                        ),
                    }
                )
        history_rows: list[str] = []
        for question in questions:
            prompt = str(question.get("question") or "").strip()
            reason = str(question.get("reason") or "").strip()
            if prompt:
                history_rows.append(prompt)
            if reason:
                history_rows.append(reason)
        if owner_decision:
            rationale = str(owner_decision.get("rationale") or "").strip()
            impact_text = str(owner_decision.get("impact") or "").strip()
            if rationale:
                history_rows.append(f"决策依据：{rationale}")
            if impact_text:
                history_rows.append(f"影响：{impact_text}")
        if history_rows:
            elements.append(_progress_panel("···", history_rows, expanded=False))
        if followup_adjustment:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        "**用户后续调整**："
                        f"{format_card_markdown(followup_adjustment)}"
                    ),
                }
            )
        decision_id = str((owner_decision or {}).get("id") or "").strip()
        decision_status = str(
            (owner_decision or {}).get("status") or "applied"
        )
        if decision_id and decision_status == "applied":
            elements.append(
                {
                    "tag": "button",
                    "text": _plain_text("调整决定"),
                    "type": "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "approval_id": decision_id,
                                "decision_id": decision_id,
                                "action": "adjust_project_decision",
                                "chat_id": chat_id,
                                "chat_type": chat_type,
                            },
                        }
                    ],
                }
            )
        if project_name:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"项目：{_compact_label(project_name)} · 执行：Codex"
                    ),
                    "text_size": "notation",
                }
            )
        del agent
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": _header("已处理", "green"),
            "body": {"elements": elements},
        }

    if is_native_approval and owner_decision:
        assessment = str(owner_decision.get("decision") or "").strip()
        if assessment:
            elements.append(
                {
                    "tag": "markdown",
                    "content": format_card_markdown(
                        f"**Project Owner 评估**：{assessment}"
                    ),
                }
            )
        assessment_rows = []
        rationale = str(owner_decision.get("rationale") or "").strip()
        impact_text = str(owner_decision.get("impact") or "").strip()
        if rationale:
            assessment_rows.append(f"原因：{rationale}")
        if impact_text:
            assessment_rows.append(f"影响：{impact_text}")
        if assessment_rows:
            elements.append(
                _progress_panel("···", assessment_rows, expanded=False)
            )

    if impact:
        elements.append(
            {"tag": "markdown", "content": f"**影响说明**：{impact}"}
        )
    if not is_submitted:
        if owner_reviewing:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        "**AI Project Owner 正在审阅**，通常无需你操作。"
                        "只有 Owner 无法可靠代答时，才会请你确认。"
                    ),
                }
            )
        elif pending_notice:
            elements.append(
                {
                    "tag": "markdown",
                    "content": format_card_markdown(pending_notice),
                }
            )
        if not owner_reviewing:
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

        for option in options if not owner_reviewing else []:
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
            else (resolution_note or "✅ 已提交，正在继续处理")
        )
        elements.append({"tag": "markdown", "content": status})
        if followup_adjustment:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        "**用户后续调整**："
                        f"{format_card_markdown(followup_adjustment)}"
                    ),
                }
            )
    elif not owner_reviewing:
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

    # ``agent`` and ``stage`` are durable routing/audit facts.  They are not
    # user-facing concepts and must never leak labels such as ``conversation``
    # or ``root_agent`` into the card header.
    del agent, stage
    title = (
        "已提交"
        if all_submitted
        else (
            "Owner 审阅中"
            if owner_reviewing
            else f"需要确认 {len(questions)} 个问题"
        )
    )
    if project_name:
        elements.append(
            {
                "tag": "markdown",
                "content": f"项目：{_compact_label(project_name)} · 执行：Codex",
                "text_size": "notation",
            }
        )
    card = {
        "config": {"wide_screen_mode": True},
        "header": _header(
            title,
            "green" if all_submitted else ("blue" if owner_reviewing else "orange"),
        ),
    }
    return {"schema": "2.0", **card, "body": {"elements": elements}}
