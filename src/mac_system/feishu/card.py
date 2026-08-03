import re
from typing import Any

from mac_system.utils.formatting import format_card_markdown


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
    """Build a Card 2.0 display card for long markdown content.

    Uses the Feishu ``markdown`` element (full CommonMark support) instead of
    the limited ``lark_md`` text element, and normalizes the Markdown so
    headings/lists render correctly.
    """
    # Defensive coercion: callers may accidentally pass None for page/total.
    if not isinstance(page, int):
        page = 1
    if not isinstance(total, int):
        total = 1
    header_title = title if total <= 1 else f"{title}（{page}/{total}）"
    body_elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": format_card_markdown(content)},
    ]
    # Feishu's message-history API does not return Card 2.0 body JSON; without
    # a summary it exposes only the generic "upgrade your client" fallback.
    # A meaningful preview also keeps chat-list/history output observable for
    # users and black-box checks without introducing a parallel text message.
    summary_text = re.sub(r"[`*_#>\[\]]", "", f"{header_title}\n{content}")
    summary_text = re.sub(r"\s+", " ", summary_text).strip()[:500]
    # Card 2.0 does not support the legacy ``note`` element. Pagination is
    # already visible in the header, so adding a note makes every multi-page
    # report fail provider-side while single-page reports keep working.
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "summary": {"content": summary_text},
        },
        "header": _header(header_title, color),
        "body": {"elements": body_elements},
    }


def build_conversation_progress_card(
    *,
    stage: str = "working",
    detail: str = "正在分析你的问题",
) -> dict[str, Any]:
    """Build the single in-place card used while the Coordinator is working."""
    if stage == "failed":
        return {
            "config": {"wide_screen_mode": True},
            "header": _header("处理失败", "red"),
            "elements": [
                {"tag": "note", "elements": [_plain_text(f"失败原因：{detail}")]},
            ],
        }
    if stage == "retrying":
        return {
            "config": {"wide_screen_mode": True},
            "header": _header("调用失败，正在重试", "orange"),
            "elements": [
                {"tag": "note", "elements": [_plain_text(f"第 1 次失败：{detail}")]},
            ],
        }
    stages = [
        ("理解问题", stage != "received"),
        ("调研与分析", stage in {"composing", "completed"}),
        ("整理结果", stage == "completed"),
    ]
    lines = []
    for label, completed in stages:
        if completed:
            marker = "✅"
        elif not lines or all(done for _, done in stages[: len(lines)]):
            marker = "⏳"
        else:
            marker = "○"
        lines.append(f"{marker} {label}")
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(
            "处理完成" if stage == "completed" else "正在处理",
            "green" if stage == "completed" else "blue",
        ),
        "elements": [
            {"tag": "div", "text": _lark_md("\n".join(lines))},
            {"tag": "note", "elements": [_plain_text(detail)]},
        ],
    }


def build_conversation_clarification_card(
    action_id: str,
    question: str,
    options: list[dict[str, Any]],
    *,
    reason: str = "",
    chat_id: str | None = None,
    chat_type: str = "p2p",
    selected_key: str | None = None,
) -> dict[str, Any]:
    """Build a chat-scoped multiple-choice card for the Coordinator.

    When ``selected_key`` is provided, the card is rendered in a read-only
    completed state: options remain visible and the selected one is marked.
    """
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**{question}**")},
    ]
    if reason:
        elements.append({"tag": "div", "text": _lark_md(reason)})
    actions: list[dict[str, Any]] = []
    selected_label = ""
    for option in options[:3]:
        label = str(option.get("label") or option.get("key") or "选择")
        description = str(option.get("description") or "").strip()
        is_selected = str(option.get("key") or "") == str(selected_key or "")
        if is_selected:
            selected_label = label
        marker = " ✅" if is_selected else ""
        name_md = f"**{label}{marker}**" if is_selected else f"{label}{marker}"
        if description:
            elements.append(
                {"tag": "div", "text": _lark_md(f"{name_md}：{description}")}
            )
        else:
            elements.append({"tag": "div", "text": _lark_md(name_md)})
        if selected_key is None:
            actions.append(
                {
                    "tag": "button",
                    "text": _plain_text(
                        f"{label}{'（推荐）' if option.get('recommended') else ''}"
                    ),
                    "type": "primary" if option.get("recommended") else "default",
                    "value": {
                        "approval_id": "",
                        "action": "conversation_clarification",
                        "pending_action_id": action_id,
                        "option_key": str(option.get("key") or ""),
                        "option_label": label,
                        "chat_id": chat_id,
                        "chat_type": chat_type,
                    },
                }
            )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    if selected_key is not None:
        elements.append(
            {"tag": "div", "text": _lark_md(f"---\n✅ 已选择：**{selected_label or selected_key}**")}
        )
    header_title = "请选择一个方向" if selected_key is None else "已完成选择"
    header_color = "blue" if selected_key is None else "green"
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(header_title, header_color),
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
    selected_action: str | None = None,
    resolved_by: str | None = None,
    resolved_at: str | None = None,
    resulting_state: str | None = None,
    resolution_reason: str | None = None,
) -> dict[str, Any]:
    """Build a pending or read-only completed confirmation card.

    A decision card is a projection of its Approval, not a disposable set of
    buttons.  Once ``selected_action`` is present the original options remain
    visible, the selected option is marked, actions are disabled, and the
    resolver/resulting state are shown so Feishu converges with backend state.
    """
    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "div", "text": _lark_md(f"**摘要**：{summary}")})
    for key, value in details.items():
        elements.append({"tag": "div", "text": _lark_md(f"**{key}**：{value}")})

    selected_label = ""
    if selected_action is not None:
        for opt in options:
            if str(opt.get("action")) == str(selected_action):
                selected_label = str(opt.get("label") or selected_action)
                break
        if not selected_label:
            selected_label = str(selected_action)
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": _lark_md(f"✅ 已选择：**{selected_label}**"),
        })
        if resolved_by:
            elements.append({
                "tag": "div", "text": _lark_md(f"**决策者**：{resolved_by}")
            })
        if resolved_at:
            elements.append({
                "tag": "div", "text": _lark_md(f"**处理时间**：{resolved_at}")
            })
        if resolution_reason:
            elements.append({
                "tag": "div", "text": _lark_md(f"**依据**：{resolution_reason}")
            })
        if resulting_state:
            elements.append({
                "tag": "div", "text": _lark_md(f"**项目已进入**：{resulting_state}")
            })

    if show_actions and selected_action is None:
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
        "header": _header(title, "green" if selected_action is not None else "blue"),
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
    kind: str = "blocking",
    question_id: str | None = None,
    interaction_id: str | None = None,
    answered: bool = False,
    answer_text: str | None = None,
    skipped: bool = False,
) -> dict[str, Any]:
    """Build a Feishu interactive input/clarification card for a free-text question.

    When ``answered`` or ``skipped`` is True, render a read-only receipt state
    showing the answer (or a skipped notice) and no action buttons. Otherwise
    show a "跳过" button only for non_blocking questions; blocking/decision
    questions must be answered by replying.
    """
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**问题**：{question}")},
    ]
    if context:
        elements.append({"tag": "div", "text": _lark_md(f"**上下文**：{context}")})
    if answered:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _lark_md(f"**你的回答**：{answer_text or ''}")})
        elements.append({"tag": "div", "text": _lark_md("✅ 已回答")})
    elif skipped:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _lark_md("⏭️ 已跳过")})
    else:
        elements.append({"tag": "div", "text": _lark_md("请直接回复此消息作答。")})
        if kind == "non_blocking":
            skip_value = {
                "approval_id": approval_id,
                "action": "skip_free_text",
                "state": state,
                "chat_id": chat_id,
                "chat_type": chat_type,
            }
            if question_id is not None:
                skip_value["question_id"] = question_id
            if interaction_id is not None:
                skip_value["interaction_id"] = interaction_id
            elements.append({
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": _plain_text("跳过"),
                    "type": "default",
                    "value": skip_value,
                }],
            })

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "orange"),
        "elements": elements,
    }


# Display names used in transition cards.  Use noun forms so the wording
# "XX 已完成" makes sense (e.g. "需求分析 已完成", not "需求分析中 已完成").
_TRANSITION_DISPLAY_NAMES: dict[str, str] = {
    "PLANNING_REQUIREMENTS": "需求分析",
    "PLANNING_ARCHITECTURE": "方案设计",
    "PLANNING_IMPLEMENTATION_SPECS": "实现计划",
    "BUG_ANALYSIS": "Bug 分析",
    "DOC_OUTLINE": "文档大纲",
    "CODING": "编码",
    "TESTING": "测试",
    "REVIEW": "代码评审",
    "MERGING": "提交代码",
    "REQUIREMENT_CONFIRM_PENDING": "需求确认",
    "ARCHITECTURE_CONFIRM_PENDING": "方案确认",
    "IMPLEMENTATION_SPEC_CONFIRM_PENDING": "实现计划确认",
    "BUG_FIX_CONFIRM_PENDING": "修复方案确认",
    "OUTLINE_CONFIRM_PENDING": "文档大纲确认",
    "CODING_CONFIRM_PENDING": "编码结果确认",
    "TESTING_CONFIRM_PENDING": "测试结果确认",
    "REVIEW_CONFIRM_PENDING": "代码评审确认",
    "ACCEPTANCE_PENDING": "验收确认",
}


def _transition_display(state: str) -> str:
    return _TRANSITION_DISPLAY_NAMES.get(state, state)


def build_transition_card(
    project_name: str,
    from_state: str,
    to_state: str,
    from_state_type: str,
    to_state_type: str,
    from_agent: str,
    to_agent: str,
    status_note: str = "",
    round_number: int | None = None,
) -> dict[str, Any]:
    """Build a card for state transitions showing stage change and agent handoff.

    Semantics:
    - Agent state whose result needs human confirmation -> result produced, waiting.
    - Human confirmation state after user confirms -> round completed.
    - Agent/system state that does not need confirmation -> completed.
    """
    from_display = _transition_display(from_state)
    to_display = _transition_display(to_state)

    if from_state_type == "human_confirmation":
        if round_number and round_number > 1:
            from_line = f"✅ 第{round_number}轮 {from_display} 已完成"
        else:
            from_line = f"✅ {from_display} 已完成"
    elif from_state_type in ("agent_execution", "system_action"):
        if to_state_type == "human_confirmation":
            from_line = f"✅ {from_display} 结果已产出，等待确认"
        else:
            from_line = f"✅ {from_display} 已完成"
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
        "header": _header(f"[{project_name}] 状态流转", "blue"),
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
        content += f"\n\n{note or '项目将返回上一阶段，请直接回复修改意见。'}"
    elif note:
        content += f"\n\n{note}"
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(f"[{project_name}] 操作确认", "green"),
        "elements": [
            {"tag": "div", "text": _lark_md(content)},
        ],
    }


def build_rejection_summary_card(
    project_name: str,
    state_display: str,
    feedback: str,
    round_number: int,
) -> dict[str, Any]:
    """Build a card summarizing the user's rejection feedback before re-planning."""
    title = f"[{project_name}] 第{round_number}轮 {state_display} 修改意见"
    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "orange"),
        "elements": [
            {"tag": "div", "text": _lark_md(f"**项目**：{project_name}")},
            {"tag": "div", "text": _lark_md(f"**意见**：{feedback}")},
            {"tag": "div", "text": _lark_md("Agent 会根据以上意见重新规划。")},
        ],
    }


def build_rejection_input_card(
    project_name: str,
    approval_id: str,
    state: str,
    chat_id: str | None = None,
    chat_type: str = "p2p",
) -> dict[str, Any]:
    """Collect planning-rejection feedback before changing project state."""
    value = {
        "approval_id": approval_id,
        "action": "submit_rejection_feedback",
        "state": state,
        "chat_id": chat_id,
        "chat_type": chat_type,
    }
    # ``form`` and ``input`` are Card JSON 2.0 components.  Mixing them into
    # the legacy 1.0 ``elements`` envelope makes Feishu reject the complete
    # message with HTTP 400, so this card intentionally uses the 2.0 envelope.
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": _plain_text(f"[{project_name}] 填写修改意见"),
            "template": "orange",
        },
        "body": {"elements": [
            {
                "tag": "markdown",
                "content": "审批尚未流转。填写意见并提交后，Agent 才会开始重做。",
            },
            {
                "tag": "form",
                "name": "rejection_feedback_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "rejection_feedback",
                        "required": True,
                        "input_type": "multiline_text",
                        "width": "fill",
                        "placeholder": _plain_text("请说明哪里需要修改、期望结果是什么"),
                    },
                    {
                        "tag": "button",
                        "text": _plain_text("提交并驳回"),
                        "type": "primary_filled",
                        "form_action_type": "submit",
                        "behaviors": [{"type": "callback", "value": value}],
                    },
                ],
            },
            {
                "tag": "button",
                "text": _plain_text("跳过说明，直接驳回"),
                "type": "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {**value, "action": "reject_without_feedback"},
                    }
                ],
            },
        ]},
    }


def build_status_card(
    title: str,
    project_id: str,
    current_state: str,
    display: str,
    recent_events: list[dict[str, Any]],
    workflow_steps: list[dict[str, Any]] | None = None,
    action_needed: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Build a Feishu status update card."""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": _lark_md(f"**当前状态**：{display}")},
    ]
    if workflow_steps:
        chain = " → ".join(
            (
                f"**{item.get('label', '')}**"
                if item.get("current")
                else str(item.get("label", ""))
            )
            for item in workflow_steps
            if item.get("label")
        )
        elements.append({"tag": "div", "text": _lark_md(f"**执行链路**：{chain}")})
    if workspace:
        elements.append({"tag": "div", "text": _lark_md(f"**工作目录**：{workspace}")})
    if action_needed:
        elements.append({
            "tag": "div",
            "text": _lark_md(f"**当前需要你处理**：{action_needed}"),
        })
    if recent_events:
        lines = "\n".join(f"- {e.get('message', '')}" for e in recent_events[:4])
        elements.append({"tag": "div", "text": _lark_md(f"**关键进展**：\n{lines}")})

    return {
        "config": {"wide_screen_mode": True},
        "header": _header(title, "green"),
        "elements": elements,
    }


def _inline_free_text_form(
    approval_id, interaction_id, question_id, state, chat_id, chat_type,
) -> dict[str, Any]:
    """Build a Card 2.0 form element for inline free-text submission.

    The form contains a multiline text input and a submit button. On submit,
    Feishu delivers ``form_value`` keyed by the input ``name`` alongside the
    button ``value`` payload, so the handler can read the typed text from
    ``action.extras["inline_free_text_value"]``.
    """
    return {
        "tag": "form",
        "name": f"inline_free_text_form_{question_id}",
        "elements": [
            {
                "tag": "input",
                "name": "inline_free_text_value",
                "required": True,
                "input_type": "multiline_text",
                "width": "fill",
                "placeholder": _plain_text("或直接在此输入你的方案..."),
            },
            {
                "tag": "button",
                "text": _plain_text("提交自定义答案"),
                "type": "primary_filled",
                "form_action_type": "submit",
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "approval_id": approval_id,
                        "interaction_id": interaction_id,
                        "action": "submit_inline_free_text",
                        "question_id": question_id,
                        "state": state,
                        "chat_id": chat_id,
                        "chat_type": chat_type,
                    },
                }],
            },
        ],
    }


def build_question_batch_card(
    approval_id, interaction_id, questions, agent, stage, impact=None,
    state="", project_name="", chat_id=None, chat_type="p2p",
    selected_options=None, submitted_answers=None, awaiting_free_text=False,
    inline_free_text_question_id=None,
):
    """Build a Feishu card that presents a batch of choice questions as buttons.

    Individual option clicks only change the selection state on the card. The
    user must click the "提交答案" button to submit all selected answers.

    When ``submitted_answers`` is provided, render a read-only receipt
    state: option buttons are disabled, final answers are highlighted, action
    buttons are hidden, and a status line is appended.

    When ``inline_free_text_question_id`` is set (and the matching question is
    in ``questions``), an inline form is rendered below that question's option
    buttons so the user can type a custom answer without going through the
    2-step "provide_custom -> reply to input card" flow. The card switches to
    the Feishu Card 2.0 envelope because ``form``/``input`` elements require
    schema 2.0; legacy 1.0 elements (div/hr/action) remain compatible.
    """
    selected_options = selected_options or {}
    submitted_answers = submitted_answers or {}
    is_submitted = bool(submitted_answers)
    all_submitted = is_submitted and all(
        (q.get("question_id") or q.get("db_id") or "") in submitted_answers
        for q in questions
    )
    use_inline_form = bool(inline_free_text_question_id) and not is_submitted
    elements: list[dict[str, Any]] = []
    if impact:
        elements.append({"tag": "div", "text": _lark_md(f"**影响说明**：{impact}")})
    if not is_submitted:
        elements.append({"tag": "div", "text": _lark_md(
            f"共 {len(questions)} 道独立单选题，每题只能选 1 项。"
            "点击选项只会暂存选择，不会立即提交；全部选好后点 **提交答案**。"
        )})
    for q in questions:
        qid = q.get("question_id") or q.get("db_id") or ""
        question_text = q.get("question", "")
        kind = q.get("kind", "blocking")
        reason = q.get("reason", "")
        options = q.get("options") or []
        recommended = q.get("recommended_option")
        selected = selected_options.get(qid)
        final_answer = submitted_answers.get(qid)
        is_q_submitted = bool(qid) and qid in submitted_answers
        kind_badge = {"blocking": "阻塞", "decision": "决策", "non_blocking": "可选"}.get(kind, kind)
        header_line = f"**{question_text}** （{kind_badge}）"
        if reason:
            header_line += f"\n原因：{reason}"
        if is_q_submitted:
            if final_answer:
                final_label = next((opt.get("label", opt.get("key", "")) for opt in options if opt.get("key") == final_answer), final_answer)
                header_line += f"\n已提交：{final_label}"
        else:
            if recommended:
                rec_label = next((opt.get("label", opt.get("key", "")) for opt in options if opt.get("key") == recommended), recommended)
                header_line += f"\n推荐：{rec_label}"
            if selected:
                sel_label = next((opt.get("label", opt.get("key", "")) for opt in options if opt.get("key") == selected), selected)
                header_line += f"\n已选：{sel_label}"
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _lark_md(header_line)})
        # Feishu allows up to 4 buttons per action row.
        option_buttons: list[dict[str, Any]] = []
        for opt in options:
            label = opt.get("label", opt.get("key", ""))
            key = opt.get("key", label)
            is_selected = key == selected
            is_final = key == final_answer
            is_recommended = key == recommended
            if is_q_submitted:
                btn_type = "primary" if is_final else "default"
            else:
                btn_type = "primary" if is_selected else ("primary" if is_recommended and selected is None else "default")
            button: dict[str, Any] = {
                "tag": "button",
                "text": _plain_text(label),
                "type": btn_type,
                "value": {
                    "approval_id": approval_id,
                    "interaction_id": interaction_id,
                    "action": "select_question_option",
                    "question_id": qid,
                    "option_key": key,
                    "state": state,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                },
            }
            if is_q_submitted:
                button["disabled"] = True
            option_buttons.append(button)
        for i in range(0, len(option_buttons), 4):
            elements.append({"tag": "action", "actions": option_buttons[i:i + 4]})
        # Inline free-text form: render below the option buttons so the user
        # can type a custom answer without going through the 2-step flow.
        if use_inline_form and qid == inline_free_text_question_id:
            elements.append(_inline_free_text_form(
                approval_id=approval_id,
                interaction_id=interaction_id,
                question_id=qid,
                state=state,
                chat_id=chat_id,
                chat_type=chat_type,
            ))
    elements.append({"tag": "hr"})
    if all_submitted:
        status = (
            "✏️ 选择已提交，请在接下来的输入卡补充具体要求"
            if awaiting_free_text
            else "✅ 已提交，Agent 继续执行"
        )
        elements.append({"tag": "div", "text": _lark_md(status)})
    else:
        has_non_blocking = any(q.get("kind") == "non_blocking" for q in questions)
        actions = [
            {"tag": "button", "text": _plain_text("提交答案"), "type": "primary",
             "value": {"approval_id": approval_id, "interaction_id": interaction_id,
                       "action": "submit_selected_options", "state": state,
                       "chat_id": chat_id, "chat_type": chat_type}},
        ]
        if has_non_blocking:
            actions.append(
                {"tag": "button", "text": _plain_text("跳过未答"), "type": "default",
                 "value": {"approval_id": approval_id, "interaction_id": interaction_id,
                           "action": "skip_remaining", "state": state,
                           "chat_id": chat_id, "chat_type": chat_type}},
            )
        elements.append({"tag": "action", "actions": actions})
    stage_label = stage or agent
    title = (
        "等待补充说明"
        if is_submitted and awaiting_free_text
        else ("已提交" if all_submitted else f"需要确认 {len(questions)} 个问题")
    )
    prefix = f"[{project_name}] " if project_name else ""
    header_color = "green" if all_submitted else "orange"
    if use_inline_form:
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": _header(f"{prefix}[{stage_label}] {title}", header_color),
            "body": {"elements": elements},
        }
    return {"config": {"wide_screen_mode": True},
            "header": _header(f"{prefix}[{stage_label}] {title}", header_color),
            "elements": elements}
