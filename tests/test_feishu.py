from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import httpx
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.feishu import FeishuBridge
from mac_system.feishu.card import (
    build_confirmation_card,
    build_content_card,
    build_input_card,
    build_status_card,
    build_transition_card,
)
from mac_system.feishu.crypto import verify_signature
from mac_system.feishu.models import ButtonAction, TextMessage


def test_runtime_permission_option_uses_explicit_structural_grant_only():
    from types import SimpleNamespace

    from mac_system.feishu.handlers import _apply_option_runtime_grants

    project = SimpleNamespace(context={})
    interaction = SimpleNamespace(agent="coder", resume_state="CODING")
    question = SimpleNamespace(
        question_id="q_runtime",
        options=[
            {
                "key": "grant",
                "label": "授权",
                "grants_runtime_capabilities": ["HostAutomation"],
            },
            {
                "key": "looks_like_permission",
                "label": "放宽权限但没有结构化授权",
            },
        ],
    )

    assert _apply_option_runtime_grants(
        project, interaction, question, "looks_like_permission"
    ) is False
    assert project.context == {}
    assert _apply_option_runtime_grants(project, interaction, question, "grant") is True
    assert project.context["runtime_capability_grants"] == [{
        "agent": "coder",
        "state": "CODING",
        "question_id": "q_runtime",
        "capabilities": ["HostAutomation"],
    }]


def test_multi_page_content_card_uses_only_card_v2_elements():
    card = build_content_card("Review 结果", "报告正文", page=1, total=2)

    assert card["schema"] == "2.0"
    assert card["config"]["summary"]["content"] == "Review 结果（1/2） 报告正文"
    assert card["header"]["title"]["content"].endswith("（1/2）")
    assert [element["tag"] for element in card["body"]["elements"]] == ["markdown"]


def test_transition_card_never_exposes_quality_state_enum():
    card = build_transition_card(
        project_name="待办网页",
        from_state="TESTING_CONFIRM_PENDING",
        to_state="REVIEW",
        from_state_type="human_confirmation",
        to_state_type="agent_execution",
        from_agent="测试 Agent",
        to_agent="代码评审 Agent",
    )

    rendered = json.dumps(card, ensure_ascii=False)
    assert "TESTING_CONFIRM_PENDING" not in rendered
    assert "测试结果确认 已完成" in rendered
    assert "进入 代码评审" in rendered


def test_confirmation_card_completed_state_exposes_backend_decision():
    card = build_confirmation_card(
        title="确认测试结果",
        summary="测试已完成",
        details={"当前阶段": "等待确认测试结果"},
        options=[
            {"action": "confirm", "label": "通过"},
            {"action": "reject", "label": "打回"},
        ],
        approval_id="appr_hidden",
        state="TESTING_CONFIRM_PENDING",
        show_actions=False,
        selected_action="confirm",
        resolved_by="Coordinator / 系统策略",
        resolved_at="2026-08-01T12:00:00",
        resulting_state="Review 中",
        resolution_reason="全部阻塞验收标准已有通过证据",
    )

    rendered = json.dumps(card, ensure_ascii=False)
    assert "已选择" in rendered
    assert "通过" in rendered
    assert "Coordinator / 系统策略" in rendered
    assert "Review 中" in rendered
    assert "全部阻塞验收标准已有通过证据" in rendered
    assert '"tag": "button"' not in rendered


class FakeBridge:
    def __init__(self):
        self.sent = []
        self.sent_history = []
        self.updated = []
        self.reactions = []
        self._message_seq = 0
        self._message_indexes = {}

    def _next_message_id(self) -> str:
        self._message_seq += 1
        return f"msg_{self._message_seq}"

    async def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> str:
        self.sent.append(("text", open_id, text, receive_id_type))
        return self._next_message_id()

    async def _send_interactive(self, open_id: str, card: dict, receive_id_type: str = "open_id") -> str:
        item = ("interactive", open_id, card, receive_id_type)
        self.sent.append(item)
        self.sent_history.append(item)
        message_id = self._next_message_id()
        self._message_indexes[message_id] = len(self.sent) - 1
        return message_id

    async def send_content_card(
        self, open_id: str, title: str, content: str, color: str = "blue",
        receive_id_type: str = "open_id",
    ) -> str:
        from mac_system.feishu.card import build_content_card

        return await self._send_interactive(
            open_id,
            build_content_card(title, content, color=color),
            receive_id_type=receive_id_type,
        )

    async def update_card(self, message_id: str, card: dict) -> None:
        self.updated.append((message_id, card))
        index = self._message_indexes.get(message_id)
        if index is not None:
            previous = self.sent[index]
            self.sent[index] = (previous[0], previous[1], card, previous[3])
    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str:
        self.reactions.append(("add", message_id, emoji_type))
        return "reaction_1"

    async def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        self.reactions.append(("remove", message_id, reaction_id))


def _sent_output_text(feishu_ctx) -> str:
    """Return user-visible content regardless of the historical envelope."""
    return " ".join(
        item[2] if item[0] == "text" else json.dumps(item[2], ensure_ascii=False)
        for item in feishu_ctx.bridge.sent
        if item[0] in {"text", "interactive"}
    )


class FakeOrchestrator:
    def __init__(self):
        self.ticked = []
        self.cancelled = []

    async def tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    def _schedule_followup_tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        self.cancelled.append((project_id, reason))

    def _agent_label_for_state(self, state: str) -> str:
        return state


@pytest.mark.asyncio
async def test_ws_group_mention_sets_shared_handler_flag(monkeypatch):
    from mac_system.feishu import ws_client

    received: list[TextMessage] = []

    async def fake_bot_open_id():
        return "ou_bot"

    async def fake_handle_text_message(ctx, msg):
        received.append(msg)

    class HandlerScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = ws_client.FeishuWebSocketClient()
    monkeypatch.setattr(ws_client, "_get_bot_open_id", fake_bot_open_id)
    monkeypatch.setattr(ws_client, "handle_text_message", fake_handle_text_message)
    monkeypatch.setattr(client, "_handler_context", lambda msg=None: HandlerScope())
    msg = TextMessage(
        open_id="ou_user",
        message_id="om_mention",
        text="@_user_1 帮我分析",
        is_command=False,
        chat_id="oc_group",
        chat_type="group",
        mentions=[{"id": {"open_id": "ou_bot"}}],
    )

    await client._handle_message(msg)

    assert received and received[0].is_at_bot is True
    assert received[0].text == "帮我分析"


@pytest.mark.asyncio
async def test_ws_cancel_bypasses_busy_user_lock(monkeypatch):
    from mac_system.feishu import ws_client

    received: list[str] = []

    async def fake_handle_text_message(ctx, msg):
        received.append(msg.text)

    class HandlerScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = ws_client.FeishuWebSocketClient()
    monkeypatch.setattr(ws_client, "handle_text_message", fake_handle_text_message)
    monkeypatch.setattr(client, "_handler_context", lambda msg=None: HandlerScope())
    busy_lock = client._user_lock("ou_user")
    busy_lock.acquire()
    try:
        await asyncio.wait_for(
            client._handle_message(
                TextMessage(
                    open_id="ou_user",
                    message_id="om_cancel",
                    text="/cancel proj_running",
                    is_command=True,
                )
            ),
            timeout=1,
        )
    finally:
        busy_lock.release()

    assert received == ["/cancel proj_running"]


@pytest.mark.asyncio
async def test_ws_text_message_is_not_blocked_by_long_card_action(monkeypatch):
    """A confirmation may run an Agent, but the private chat stays responsive."""
    from mac_system.feishu import ws_client

    card_started = asyncio.Event()
    release_card = asyncio.Event()
    received: list[str] = []

    async def fake_handle_card_action(ctx, action):
        card_started.set()
        await release_card.wait()

    async def fake_handle_text_message(ctx, msg):
        received.append(msg.text)

    class HandlerScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = ws_client.FeishuWebSocketClient()
    monkeypatch.setattr(ws_client, "handle_card_action", fake_handle_card_action)
    monkeypatch.setattr(ws_client, "handle_text_message", fake_handle_text_message)
    monkeypatch.setattr(client, "_handler_context", lambda msg=None: HandlerScope())
    card_task = asyncio.create_task(
        client._handle_card_action(
            ButtonAction(
                open_id="ou_user",
                approval_id="appr_1",
                action="confirm",
                state="CODING_CONFIRM_PENDING",
                decision_id=None,
                option_key=None,
                extras={},
            )
        )
    )
    await asyncio.wait_for(card_started.wait(), timeout=1)
    try:
        await asyncio.wait_for(
            client._handle_message(
                TextMessage(
                    open_id="ou_user",
                    message_id="om_status",
                    text="项目到哪一步了？",
                    is_command=False,
                )
            ),
            timeout=1,
        )
    finally:
        release_card.set()
        await card_task

    assert received == ["项目到哪一步了？"]


@pytest.mark.asyncio
async def test_ws_text_message_is_not_blocked_by_long_text_action(monkeypatch):
    """A natural-language approval may start an Agent without freezing chat."""
    from mac_system.feishu import ws_client

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    received: list[str] = []

    async def fake_handle_text_message(ctx, msg):
        received.append(msg.text)
        if msg.message_id == "om_retry":
            first_started.set()
            await release_first.wait()

    class HandlerScope:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    client = ws_client.FeishuWebSocketClient()
    monkeypatch.setattr(ws_client, "handle_text_message", fake_handle_text_message)
    monkeypatch.setattr(client, "_handler_context", lambda msg=None: HandlerScope())
    first_task = asyncio.create_task(
        client._handle_message(
            TextMessage(
                open_id="ou_user",
                message_id="om_retry",
                text="直接重新测试",
                is_command=False,
            )
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    try:
        await asyncio.wait_for(
            client._handle_message(
                TextMessage(
                    open_id="ou_user",
                    message_id="om_status",
                    text="项目到哪一步了？",
                    is_command=False,
                )
            ),
            timeout=1,
        )
    finally:
        release_first.set()
        await first_task

    assert received == ["直接重新测试", "项目到哪一步了？"]


@pytest.fixture
def service(tmp_path, monkeypatch):
    from mac_system.config import get_settings
    from mac_system.core.project_service import ProjectService

    settings = get_settings()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "mac_allowed_bind_roots", str(tmp_path))

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield ProjectService(s)


@pytest.fixture
def feishu_ctx(service):
    from mac_system.feishu.handlers import FeishuHandlerContext

    return FeishuHandlerContext(
        bridge=FakeBridge(),
        service=service,
        orchestrator=FakeOrchestrator(),
    )


async def test_group_chat_never_falls_back_to_project_from_another_chat(
    feishu_ctx,
):
    from mac_system.feishu.handlers import _find_active_project_for_user

    project = await feishu_ctx.service.create_project(
        name="private-project",
        task_type="feature",
        description="private",
        creator_open_id="ou_shared",
        feishu_chat_id="p2p_private",
        feishu_chat_type="p2p",
    )
    feishu_ctx.chat_id = "group_quant"
    feishu_ctx.chat_type = "group"

    assert _find_active_project_for_user(feishu_ctx, "ou_shared") is None

    feishu_ctx.chat_type = "p2p"
    assert _find_active_project_for_user(feishu_ctx, "ou_shared").id == project.id


async def test_deliver_card_persists_and_advances_card_projection(feishu_ctx):
    from mac_system.core.notification_service import deliver_card
    from mac_system.models import CardProjection

    message_id = await deliver_card(
        feishu_ctx.service,
        feishu_ctx.bridge,
        dedupe_key="approval:a1:pending",
        recipient_id="ou_1",
        recipient_type="open_id",
        intent_type="decision",
        card=build_content_card("等待选择", "请选择"),
        projection_key="approval:a1",
        subject_type="approval",
        subject_id="a1",
        lifecycle_status="pending",
    )
    await deliver_card(
        feishu_ctx.service,
        feishu_ctx.bridge,
        dedupe_key="approval:a1:resolved",
        recipient_id="ou_1",
        recipient_type="open_id",
        intent_type="decision",
        card=build_content_card("已完成选择", "已选择通过"),
        update_message_id=message_id,
        projection_key="approval:a1",
        subject_type="approval",
        subject_id="a1",
        lifecycle_status="resolved_auto",
        selected_action="confirm",
        resolved_by="system_auto",
        resulting_state="REVIEW",
    )

    projection = feishu_ctx.service.session.exec(
        select(CardProjection).where(CardProjection.projection_key == "approval:a1")
    ).one()
    assert projection.provider_message_id == message_id
    assert projection.projection_version == 2
    assert projection.lifecycle_status == "resolved_auto"
    assert projection.selected_action == "confirm"
    assert projection.resulting_state == "REVIEW"


@pytest.mark.asyncio
async def test_conversation_transient_update_failure_does_not_post_competing_card(
    feishu_ctx,
):
    from mac_system.feishu.handlers import _deliver_conversation_card

    feishu_ctx.chat_id = "oc_transient"

    async def fail_update(message_id, card):
        raise httpx.ConnectError("")

    feishu_ctx.bridge.update_card = fail_update
    with pytest.raises(httpx.ConnectError):
        await _deliver_conversation_card(
            feishu_ctx,
            "oc_transient",
            "chat_id",
            build_content_card("result", "body"),
            message_id="msg_progress",
        )

    assert feishu_ctx.bridge.sent == []


@pytest.mark.asyncio
async def test_conversation_permanent_update_failure_posts_one_replacement(feishu_ctx):
    from mac_system.feishu.handlers import _deliver_conversation_card

    feishu_ctx.chat_id = "oc_terminal"

    async def fail_update(message_id, card):
        request = httpx.Request("PATCH", "https://open.feishu.cn/messages/expired")
        response = httpx.Response(400, request=request)
        raise httpx.HTTPStatusError("expired", request=request, response=response)

    feishu_ctx.bridge.update_card = fail_update
    message_id = await _deliver_conversation_card(
        feishu_ctx,
        "oc_terminal",
        "chat_id",
        build_content_card("result", "body"),
        message_id="msg_expired",
    )

    assert message_id == "msg_1"
    assert len(feishu_ctx.bridge.sent) == 1


async def test_create_prompt_persists_intent(feishu_ctx):
    from mac_system.feishu.handlers import _handle_menu_action

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key="create_prompt",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is not None
    assert intent.stage == "awaiting_description"
    assert feishu_ctx.bridge.sent[-1][0] == "interactive"


async def test_text_message_routes_pending_description(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.service.upsert_pending_intent(
        "ou_1",
        description=None,
        stage="awaiting_description",
    )
    msg = TextMessage(
        open_id="ou_1",
        message_id="m1",
        text="实现一个 config-validator CLI",
        is_command=False,
    )
    await handle_text_message(feishu_ctx, msg)

    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    assert "已创建项目" in _sent_output_text(feishu_ctx)
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_intent_confirm_creates_project(feishu_ctx):
    from mac_system.feishu.handlers import _handle_menu_action

    feishu_ctx.service.upsert_pending_intent(
        "ou_1",
        description="修复 port 校验范围 bug",
        stage="awaiting_type_confirm",
    )
    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key="intent_confirm:bug_fix",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    assert "已创建项目" in _sent_output_text(feishu_ctx)
    assert len(feishu_ctx.orchestrator.ticked) >= 1


def test_verify_signature():
    app_secret = "test-secret"
    body = b'{"test":"payload"}'
    timestamp = "1234567890"
    nonce = "abc123"

    import base64
    import hashlib
    import hmac

    expected = base64.b64encode(
        hmac.new(app_secret.encode(), (timestamp + nonce).encode() + body, hashlib.sha256).digest()
    ).decode()
    assert verify_signature(timestamp, nonce, body, expected, app_secret) is True
    assert verify_signature(timestamp, nonce, body, expected + "x", app_secret) is False


def test_build_confirmation_card():
    card = build_confirmation_card(
        title="确认需求",
        summary="实现一个配置校验器",
        details={"state": "REQUIREMENT_CONFIRM_PENDING"},
        options=[{"action": "confirm", "label": "确认"}],
        approval_id="appr_1",
        state="REQUIREMENT_CONFIRM_PENDING",
    )
    assert card["header"]["title"]["content"] == "确认需求"
    assert card["elements"][-1]["tag"] == "action"


def test_build_input_card():
    card = build_input_card(
        title="需要澄清",
        question="支持 float 吗？",
        context="配置校验器",
        approval_id="appr_2",
        state="HUMAN_INPUT",
    )
    assert card["header"]["template"] == "orange"
    # Blocking questions have no skip button.
    actions = [a for el in card["elements"] if el.get("tag") == "action" for a in el.get("actions", [])]
    assert all(a.get("value", {}).get("action") != "skip_free_text" for a in actions)


def test_build_input_card_non_blocking_has_skip_button():
    from mac_system.feishu.card import build_input_card
    card = build_input_card(
        title="可选",
        question="备注？",
        context="",
        approval_id="appr_3",
        state="HUMAN_INPUT",
        kind="non_blocking",
        question_id="q_note",
        interaction_id="qi_1",
    )
    skip_buttons = [
        a for el in card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
        if a.get("value", {}).get("action") == "skip_free_text"
    ]
    assert len(skip_buttons) == 1


def test_build_input_card_answered_state_has_no_buttons():
    from mac_system.feishu.card import build_input_card
    card = build_input_card(
        title="已回答",
        question="备注？",
        context="",
        approval_id="appr_4",
        state="HUMAN_INPUT",
        kind="non_blocking",
        question_id="q_note",
        interaction_id="qi_1",
        answered=True,
        answer_text="我的备注",
    )
    actions = [el for el in card["elements"] if el.get("tag") == "action"]
    assert actions == []
    assert any("我的备注" in el.get("text", {}).get("content", "") for el in card["elements"])


def test_build_question_batch_card_has_no_answer_all_recommended_button():
    from mac_system.feishu.card import build_question_batch_card
    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[{
            "question_id": "q_1", "kind": "blocking",
            "question": "选哪个？", "options": [
                {"key": "a", "label": "A", "recommended": True},
                {"key": "b", "label": "B"},
            ],
        }],
        agent="planner", stage="requirements",
    )
    action_values = [
        a.get("value", {}).get("action")
        for el in card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
    ]
    assert "answer_all_recommended" not in action_values
    assert "submit_selected_options" in action_values


def test_build_question_batch_card_skip_remaining_only_when_non_blocking():
    from mac_system.feishu.card import build_question_batch_card
    blocking_card = build_question_batch_card(
        approval_id="appr_1", interaction_id="qi_1",
        questions=[{"question_id": "q_1", "kind": "blocking", "question": "Q",
                     "options": [{"key": "a", "label": "A", "recommended": True}]}],
        agent="planner", stage="requirements",
    )
    blocking_actions = [
        a.get("value", {}).get("action")
        for el in blocking_card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
    ]
    assert "skip_remaining" not in blocking_actions

    non_blocking_card = build_question_batch_card(
        approval_id="appr_2", interaction_id="qi_2",
        questions=[{"question_id": "q_2", "kind": "non_blocking", "question": "Q",
                     "options": [{"key": "a", "label": "A", "recommended": True}]}],
        agent="planner", stage="requirements",
    )
    nb_actions = [
        a.get("value", {}).get("action")
        for el in non_blocking_card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
    ]
    assert "skip_remaining" in nb_actions


def test_build_question_batch_card_submitted_state_disables_buttons():
    from mac_system.feishu.card import build_question_batch_card
    card = build_question_batch_card(
        approval_id="appr_1", interaction_id="qi_1",
        questions=[{
            "question_id": "q_1", "kind": "blocking",
            "question": "选哪个？", "options": [
                {"key": "a", "label": "A", "recommended": True},
                {"key": "b", "label": "B"},
            ],
        }],
        agent="planner", stage="requirements",
        submitted_answers={"q_1": "b"},
    )
    # Header should be green and title should say 已提交.
    assert card["header"]["template"] == "green"
    assert "已提交" in card["header"]["title"]["content"]
    # All option buttons should be disabled.
    option_buttons = [
        a for el in card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
        if a.get("value", {}).get("action") == "select_question_option"
    ]
    assert len(option_buttons) == 2
    assert all(b.get("disabled") for b in option_buttons)
    # The submitted option should be primary.
    submitted = next(b for b in option_buttons if b["value"]["option_key"] == "b")
    assert submitted["type"] == "primary"
    # No submit/skip action buttons.
    action_values = [
        a.get("value", {}).get("action")
        for el in card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
    ]
    assert "submit_selected_options" not in action_values
    assert "skip_remaining" not in action_values


def test_build_question_batch_card_waiting_for_custom_text_receipt():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[{
            "question_id": "q_1",
            "kind": "blocking",
            "question": "是否接受默认？",
            "options": [
                {"key": "accept_default", "label": "接受默认", "recommended": True},
                {
                    "key": "provide_custom",
                    "label": "我要修改",
                    "triggers_free_text": True,
                },
            ],
        }],
        agent="planner",
        stage="requirements",
        submitted_answers={"q_1": "provide_custom"},
        awaiting_free_text=True,
    )

    assert "等待补充说明" in card["header"]["title"]["content"]
    assert "接下来的输入卡" in str(card)
    assert "Agent 继续执行" not in str(card)


def test_build_question_batch_card_renders_inline_free_text_form():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[{
            "question_id": "q_1",
            "kind": "blocking",
            "allow_free_text": True,
            "question": "是否接受默认？",
            "options": [
                {"key": "accept_default", "label": "接受默认", "recommended": True},
                {
                    "key": "provide_custom",
                    "label": "我要修改",
                    "triggers_free_text": True,
                },
            ],
        }],
        agent="planner",
        stage="requirements",
        inline_free_text_question_id="q_1",
    )

    # Card 2.0 envelope is required for form/input elements.
    assert card["schema"] == "2.0"
    elements = card["body"]["elements"]
    forms = [el for el in elements if el.get("tag") == "form"]
    assert len(forms) == 1
    form = forms[0]
    assert form["name"] == "inline_free_text_form_q_1"

    # Form contains a multiline text input and a submit button.
    input_el = next((e for e in form["elements"] if e.get("tag") == "input"), None)
    assert input_el is not None
    assert input_el["name"] == "inline_free_text_value"
    assert input_el["input_type"] == "multiline_text"

    submit_btn = next(
        (e for e in form["elements"]
         if e.get("tag") == "button" and e.get("form_action_type") == "submit"),
        None,
    )
    assert submit_btn is not None
    behavior_value = submit_btn["behaviors"][0]["value"]
    assert behavior_value["action"] == "submit_inline_free_text"
    assert behavior_value["question_id"] == "q_1"
    assert behavior_value["approval_id"] == "appr_1"


def test_build_question_batch_card_inline_form_only_on_matching_question():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[
            {
                "question_id": "q_1",
                "kind": "blocking",
                "allow_free_text": True,
                "question": "Q1",
                "options": [{"key": "a", "label": "A", "recommended": True}],
            },
            {
                "question_id": "q_2",
                "kind": "blocking",
                "question": "Q2",
                "options": [{"key": "x", "label": "X", "recommended": True}],
            },
        ],
        agent="planner",
        stage="requirements",
        inline_free_text_question_id="q_1",
    )

    elements = card["body"]["elements"]
    forms = [el for el in elements if el.get("tag") == "form"]
    assert len(forms) == 1
    assert forms[0]["name"] == "inline_free_text_form_q_1"


def test_build_question_batch_card_no_inline_form_when_id_is_none():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[{
            "question_id": "q_1",
            "kind": "blocking",
            "allow_free_text": True,
            "question": "Q1",
            "options": [{"key": "a", "label": "A", "recommended": True}],
        }],
        agent="planner",
        stage="requirements",
    )

    # Legacy 1.0 envelope is preserved when no inline form is requested.
    assert "schema" not in card
    assert "elements" in card
    assert not any(el.get("tag") == "form" for el in card["elements"])


def test_build_question_batch_card_inline_form_hidden_in_submitted_state():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[{
            "question_id": "q_1",
            "kind": "blocking",
            "allow_free_text": True,
            "question": "Q1",
            "options": [{"key": "a", "label": "A", "recommended": True}],
        }],
        agent="planner",
        stage="requirements",
        submitted_answers={"q_1": "custom answer text"},
        inline_free_text_question_id="q_1",
    )

    # In submitted (receipt) state, the inline form is hidden so the user
    # cannot re-submit. Card 1.0 envelope is also restored because schema 2.0
    # is only required when the form is actually rendered.
    elements = card["elements"]
    assert not any(el.get("tag") == "form" for el in elements)


def test_build_question_batch_card_partial_submission_keeps_other_questions_active():
    from mac_system.feishu.card import build_question_batch_card

    card = build_question_batch_card(
        approval_id="appr_1",
        interaction_id="qi_1",
        questions=[
            {
                "question_id": "q_1",
                "kind": "blocking",
                "allow_free_text": True,
                "question": "Q1",
                "options": [{"key": "a", "label": "A", "recommended": True}],
            },
            {
                "question_id": "q_2",
                "kind": "blocking",
                "question": "Q2",
                "options": [{"key": "x", "label": "X", "recommended": True}],
            },
        ],
        agent="planner",
        stage="requirements",
        submitted_answers={"q_1": "custom typed answer"},
        inline_free_text_question_id="q_1",
    )

    elements = card["elements"]
    # q_1 buttons should be disabled; q_2 buttons should remain active.
    q1_buttons = [
        a for el in elements if el.get("tag") == "action"
        for a in el.get("actions", [])
        if a.get("value", {}).get("question_id") == "q_1"
    ]
    q2_buttons = [
        a for el in elements if el.get("tag") == "action"
        for a in el.get("actions", [])
        if a.get("value", {}).get("question_id") == "q_2"
    ]
    assert all(b.get("disabled") for b in q1_buttons)
    assert not any(b.get("disabled") for b in q2_buttons)
    # Action buttons (submit/skip) should remain visible because q_2 is still
    # pending. Title should not say 已提交.
    action_values = [
        a.get("value", {}).get("action")
        for el in elements if el.get("tag") == "action"
        for a in el.get("actions", [])
    ]
    assert "submit_selected_options" in action_values
    assert "已提交" not in card["header"]["title"]["content"]


def test_build_status_card():
    card = build_status_card(
        title="状态",
        project_id="proj_1",
        current_state="CODING",
        display="编码中",
        recent_events=[{"level": "INFO", "message": "开始编码"}],
    )
    assert card["header"]["template"] == "green"


async def test_bound_group_member_is_authorized_to_confirm_architecture(
    feishu_ctx,
):
    from mac_system.feishu.handlers import _is_authorized_for_project, handle_card_action

    project = await feishu_ctx.service.create_project(
        name="group-project",
        task_type="feature",
        description="Test",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_group",
        feishu_chat_type="group",
    )
    project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="ARCHITECTURE_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认方案",
        options=[
            {
                "action": "confirm",
                "label": "确认",
            }
        ],
    )
    feishu_ctx.chat_id = "oc_group"
    feishu_ctx.chat_type = "group"
    assert _is_authorized_for_project(
        feishu_ctx, "ou_other_member", project.id
    ) is True

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_other_member",
            approval_id=approval.id,
            action="confirm",
            state="ARCHITECTURE_CONFIRM_PENDING",
            decision_id=None,
            option_key=None,
            extras={},
            chat_id="oc_group",
            chat_type="group",
        ),
    )
    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "PLANNING_IMPLEMENTATION_SPECS"
    resolved = feishu_ctx.service.get_approval(approval.id)
    assert resolved.response == "confirm"


def test_parse_text_message():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "message": {
                "message_id": "om_1",
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "content": json.dumps({"text": "  /whoami  "}),
            }
        }
    }
    msg = bridge.parse_text_message(payload)
    assert isinstance(msg, TextMessage)
    assert msg.open_id == "ou_1"
    assert msg.text == "/whoami"
    assert msg.is_command is True


def test_parse_text_message_converts_feishu_create_time():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "message": {
                "message_id": "om_time",
                "create_time": "1721376000123",
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "content": json.dumps({"text": "hello"}),
            }
        }
    }

    msg = bridge.parse_text_message(payload)

    assert msg.created_at == datetime(2024, 7, 19, 8, 0, 0, 123000)


def test_parse_text_message_group_with_mention():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "message": {
                "message_id": "om_2",
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "chat_type": "group",
                "chat_id": "oc_group",
                "mentions": [
                    {"key": "@_user_1", "id": {"open_id": "ou_bot"}},
                ],
                "content": json.dumps({"text": "@_user_1 /whoami"}),
            }
        }
    }
    msg = bridge.parse_text_message(payload)
    assert msg.chat_type == "group"
    assert msg.chat_id == "oc_group"
    assert msg.text == "/whoami"
    assert msg.is_command is True
    assert len(msg.mentions) == 1
    assert msg.mentions[0]["id"]["open_id"] == "ou_bot"


def test_parse_text_message_keeps_parent_id():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "message": {
                "message_id": "om_reply",
                "parent_id": "om_question_card",
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "content": json.dumps({"text": "使用严格模式"}),
            }
        }
    }
    msg = bridge.parse_text_message(payload)
    assert msg.parent_id == "om_question_card"


async def test_recent_planning_project_is_not_assumed_rejected(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_current"
    feishu_ctx.chat_type = "group"
    project = await feishu_ctx.service.create_project(
        name="planning",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_current",
        feishu_chat_type="group",
    )
    project.current_state = "PLANNING_REQUIREMENTS"
    feishu_ctx.service.session.commit()

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_note",
            text="再补充一个普通说明",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_current",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.get_open_feedback_packets(project.id) == []
    assert feishu_ctx.service.get_pending_user_inputs(project.id) == []


async def test_rejection_feedback_does_not_cross_chat(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="other-chat",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_other",
        feishu_chat_type="group",
    )
    project.current_state = "PLANNING_REQUIREMENTS"
    project.context = {
        **project.context,
        "rejected_confirmation_state": "REQUIREMENT_CONFIRM_PENDING",
    }
    feishu_ctx.service.session.commit()
    feishu_ctx.chat_id = "oc_current"
    feishu_ctx.chat_type = "group"

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_other_chat",
            text="这不是另一个群的反馈",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_current",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.get_open_feedback_packets(project.id) == []


async def test_confirmation_free_text_is_not_queued_forever(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="confirming",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="confirmation",
        question="确认需求？",
        options=[
            {"action": "confirm", "label": "确认"},
            {"action": "reject", "label": "驳回"},
        ],
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_confirm_text",
            text="我想补充说明",
            is_command=False,
        ),
    )

    assert feishu_ctx.service.get_pending_user_inputs(project.id) == []
    rendered = _sent_output_text(feishu_ctx)
    assert "最新确认卡" in rendered
    assert approval.id not in rendered


async def test_late_input_for_resolved_approval_returns_idempotent_receipt(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="resolved-input",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="范围是什么？",
        options=[],
    )
    await feishu_ctx.service.respond_approval(
        approval.id, "answer", "ou_1"
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_late_input",
            text=f"/input {approval.id} 迟到的回答",
            is_command=True,
        ),
    )

    assert "已处理，无需重复提交" in _sent_output_text(feishu_ctx)


async def test_reply_to_question_card_routes_by_parent_message(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="reply-routing",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_REQUIREMENTS",
        agent="planner",
        stage="requirements",
        batch_key="reply-test",
        questions=[{
            "question_id": "q_mode",
            "kind": "blocking",
            "question": "采用什么模式？",
            "reason": "影响行为",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": [],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="采用什么模式？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "reply_message_ids": ["om_question_card"],
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_reply",
            parent_id="om_question_card",
            text="使用严格模式",
            is_command=False,
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = feishu_ctx.service.get_question_answer(question.id)
    assert answer is not None
    assert answer.answer == "使用严格模式"
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


async def test_reply_to_free_text_card_routes_to_specific_question(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="reply-specific",
        task_type="feature",
        description="Test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_REQUIREMENTS",
        agent="planner",
        stage="requirements",
        batch_key="reply-specific-batch",
        questions=[
            {
                "question_id": "q_goal",
                "kind": "blocking",
                "question": "目标是什么？",
                "reason": "",
                "allow_free_text": True,
                "options": [],
                "affected_trace_ids": [],
            },
            {
                "question_id": "q_scope",
                "kind": "blocking",
                "question": "范围是什么？",
                "reason": "",
                "allow_free_text": True,
                "options": [],
                "affected_trace_ids": [],
            },
        ],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="目标是什么？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "reply_message_ids": ["om_goal_card", "om_scope_card"],
            "reply_message_to_question": {
                "om_goal_card": "q_goal",
                "om_scope_card": "q_scope",
            },
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    # Reply to the second free-text card.
    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_reply_scope",
            parent_id="om_scope_card",
            text="先做核心功能",
            is_command=False,
        ),
    )

    questions = {q.question_id: q for q in feishu_ctx.service.get_questions_for_interaction(interaction.id)}
    assert feishu_ctx.service.get_question_answer(questions["q_goal"].id) is None
    answer = feishu_ctx.service.get_question_answer(questions["q_scope"].id)
    assert answer is not None
    assert answer.answer == "先做核心功能"
    assert feishu_ctx.service.require_project(project.id).current_state == "REQUIREMENT_INPUT_PENDING"


async def test_question_card_action_rejects_unauthorized_user(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="question-auth",
        task_type="feature",
        description="test",
        creator_open_id="ou_owner",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "auth_batch",
        [{
            "question_id": "q_mode",
            "kind": "decision",
            "question": "采用什么模式？",
            "options": [{"key": "strict", "label": "严格", "recommended": True}],
            "recommended_option": "strict",
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="采用什么模式？",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_other",
            approval_id=approval.id,
            action="answer_all_recommended",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert question.status == "pending"
    assert feishu_ctx.service.get_approval(approval.id).status == "pending"
    assert "没有权限" in str(feishu_ctx.bridge.sent[-1][2])


async def test_custom_choice_collects_text_before_resuming_agent(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action, handle_text_message

    project = await feishu_ctx.service.create_project(
        name="custom-answer",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "custom_batch",
        [{
            "question_id": "q_scope",
            "decision_key": "requirements.scope",
            "kind": "decision",
            "question": "是否接受默认范围？",
            "allow_free_text": True,
            "options": [
                {
                    "key": "accept_default",
                    "label": "接受默认范围",
                    "recommended": True,
                },
                {
                    "key": "provide_custom",
                    "label": "我要修改",
                    "triggers_free_text": True,
                },
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="是否接受默认范围？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "question_batch_message_id": "om_batch",
            "selected_options": {"q_scope": "provide_custom"},
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_selected_options",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
            message_id="om_batch",
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert question.status == "pending"
    assert feishu_ctx.service.get_question_answer(question.id) is None
    refreshed_approval = feishu_ctx.service.get_approval(approval.id)
    assert refreshed_approval.payload["custom_answer_question_ids"] == ["q_scope"]
    assert refreshed_approval.payload["reply_message_to_question"]["msg_1"] == "q_scope"
    assert any(
        message_id == "om_batch" and "等待补充说明" in str(card)
        for message_id, card in feishu_ctx.bridge.updated
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_custom_reply",
            parent_id="msg_1",
            text="改为只支持移动端",
            is_command=False,
        ),
    )

    answer = feishu_ctx.service.get_question_answer(question.id)
    assert answer is not None
    assert answer.answer == "改为只支持移动端"
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    final_cards = [card for message_id, card in feishu_ctx.bridge.updated if message_id == "om_batch"]
    assert "已提交" in str(final_cards[-1])
    assert "改为只支持移动端" in str(final_cards[-1])


def test_parse_text_message_bot_sender():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "message": {
                "message_id": "om_3",
                "sender": {
                    "sender_id": {"open_id": "ou_bot"},
                    "sender_type": "app",
                },
                "content": json.dumps({"text": "hello"}),
            }
        }
    }
    msg = bridge.parse_text_message(payload)
    assert msg.sender_type == "app"


def test_parse_button_action_with_chat_context():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "action": {
                "value": {
                    "approval_id": "appr_3",
                    "action": "confirm",
                    "state": "REQUIREMENT_CONFIRM_PENDING",
                    "chat_id": "oc_group",
                    "chat_type": "group",
                }
            },
            "operator": {"operator_id": {"open_id": "ou_2"}},
        }
    }
    action = bridge.parse_button_action(payload)
    assert isinstance(action, ButtonAction)
    assert action.approval_id == "appr_3"
    assert action.open_id == "ou_2"
    assert action.chat_id == "oc_group"
    assert action.chat_type == "group"


def test_parse_button_action():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "action": {
                "value": {
                    "approval_id": "appr_3",
                    "action": "confirm",
                    "state": "REQUIREMENT_CONFIRM_PENDING",
                }
            },
            "operator": {"operator_id": {"open_id": "ou_2"}},
        }
    }
    action = bridge.parse_button_action(payload)
    assert isinstance(action, ButtonAction)
    assert action.approval_id == "appr_3"
    assert action.open_id == "ou_2"
    assert action.chat_id is None
    assert action.chat_type == "p2p"


def test_parse_button_action_converts_feishu_create_time():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "action": {
                "create_time": "1721376000123",
                "value": {"approval_id": "appr_time", "action": "confirm"},
            },
            "operator": {"operator_id": {"open_id": "ou_2"}},
        }
    }

    action = bridge.parse_button_action(payload)

    assert action.created_at == datetime(2024, 7, 19, 8, 0, 0, 123000)


def test_parse_button_action_includes_form_value():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    payload = {
        "event": {
            "action": {
                "value": {
                    "approval_id": "appr_q",
                    "action": "answer_with_values",
                    "state": "REQUIREMENT_INPUT_PENDING",
                    "interaction_id": "qi_1",
                },
                "form_value": {
                    "q_q_type": "web_app",
                    "q_q_scope": "core",
                },
            },
            "operator": {"operator_id": {"open_id": "ou_2"}},
            "context": {"open_message_id": "om_123"},
        }
    }
    action = bridge.parse_button_action(payload)
    assert action.extras["q_q_type"] == "web_app"
    assert action.extras["q_q_scope"] == "core"
    assert action.extras["interaction_id"] == "qi_1"
    assert action.message_id == "om_123"
    assert "approval_id" not in action.extras


async def test_request_retries_transient_errors():
    import time

    import httpx

    bridge = FeishuBridge(app_id="x", app_secret="y")
    bridge._token = "token"
    bridge._token_expire_at = time.time() + 3600

    calls = []
    req = httpx.Request("POST", "https://open.feishu.cn/x")

    async def fake_request(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise httpx.HTTPStatusError(
                "server error",
                request=req,
                response=httpx.Response(500, request=req),
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "m1"}}, request=req)

    bridge._client.request = fake_request
    data = await bridge._request("POST", "/x")
    assert data["data"]["message_id"] == "m1"
    assert len(calls) == 2


async def test_request_does_not_retry_client_errors():
    import time

    import httpx

    bridge = FeishuBridge(app_id="x", app_secret="y")
    bridge._token = "token"
    bridge._token_expire_at = time.time() + 3600

    req = httpx.Request("POST", "https://open.feishu.cn/x")

    async def fake_request(*args, **kwargs):
        return httpx.Response(400, json={"code": 999, "msg": "bad request"}, request=req)

    bridge._client.request = fake_request
    with pytest.raises(httpx.HTTPStatusError):
        await bridge._request("POST", "/x")


async def test_processing_reaction_uses_original_message():
    bridge = FeishuBridge(app_id="x", app_secret="y")
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json_data")))
        return {"code": 0, "data": {"reaction_id": "reaction_1"}}

    bridge._request = fake_request
    reaction_id = await bridge.add_reaction("om_user", "OnIt")
    await bridge.remove_reaction("om_user", reaction_id)

    assert calls == [
        (
            "POST",
            "/open-apis/im/v1/messages/om_user/reactions",
            {"reaction_type": {"emoji_type": "OnIt"}},
        ),
        (
            "DELETE",
            "/open-apis/im/v1/messages/om_user/reactions/reaction_1",
            None,
        ),
    ]


async def test_create_command_without_args_prompts_for_description(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    msg = TextMessage(
        open_id="ou_1",
        message_id="m1",
        text="/create",
        is_command=True,
    )
    await handle_text_message(feishu_ctx, msg)

    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is not None
    assert intent.stage == "awaiting_description"
    assert feishu_ctx.bridge.sent[-1][0] == "interactive"


async def test_freeform_text_no_longer_creates_project(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    msg = TextMessage(
        open_id="ou_1",
        message_id="m1",
        text="帮我实现一个个人网站",
        is_command=False,
    )
    await handle_text_message(feishu_ctx, msg)

    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    assert not any("已创建项目" in str(c[2]) for c in feishu_ctx.bridge.sent)
    assert any("/create" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_cancel_command_cancels_bound_project(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="cancel-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )

    msg = TextMessage(
        open_id="ou_1",
        message_id="m2",
        text=f"/cancel {project.id}",
        is_command=True,
    )
    await handle_text_message(feishu_ctx, msg)

    assert (project.id, "用户通过飞书命令终止") in feishu_ctx.orchestrator.cancelled


async def test_create_then_generic_description_creates_feature_project(feishu_ctx):
    """/create followed by a generic description should default to feature."""
    from mac_system.feishu.handlers import handle_text_message

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/create", is_command=True),
    )
    assert feishu_ctx.service.get_pending_intent("ou_1").stage == "awaiting_description"

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m2",
            text="帮我做一个个人网站",
            is_command=False,
        ),
    )

    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    assert "已创建项目" in _sent_output_text(feishu_ctx)
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_explicit_create_description_bypasses_general_coordinator(feishu_ctx):
    """The second `/create` turn is authorized delivery, not ordinary chat."""
    from mac_system.feishu.handlers import handle_text_message

    coordinator_calls = 0

    async def run_chat_scoped_agent(**kwargs):
        nonlocal coordinator_calls
        coordinator_calls += 1
        return {"action": "answer", "reply": "should not run"}

    feishu_ctx.orchestrator.run_chat_scoped_agent = run_chat_scoped_agent
    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/create", is_command=True),
    )
    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m2",
            text="帮我做一个个人网站",
            is_command=False,
        ),
    )

    assert coordinator_calls == 0
    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    assert any("已创建项目" in str(item[2]) for item in feishu_ctx.bridge.sent)


async def test_bind_dir_command_binds_directory_to_chat(feishu_ctx, tmp_path):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_bind"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "bound-repo"
    target_dir.mkdir()

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=f"/bind {target_dir}", is_command=True),
    )

    binding = feishu_ctx.service.get_chat_directory_binding("oc_bind")
    assert binding is not None
    assert binding.directory == str(target_dir.resolve())
    assert any("已将当前会话绑定到" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_unbind_command_removes_directory_binding(feishu_ctx, tmp_path):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_unbind"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "bound-repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat("oc_unbind", str(target_dir), "ou_1")
    assert feishu_ctx.service.get_chat_directory_binding("oc_unbind") is not None

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/unbind", is_command=True),
    )

    assert feishu_ctx.service.get_chat_directory_binding("oc_unbind") is None
    assert any("已解绑" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_bind_rejected_when_chat_has_active_project(feishu_ctx, tmp_path):
    """`/bind` must be rejected when the chat has an active or paused project."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_block_bind"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "repo"
    target_dir.mkdir()

    project = await feishu_ctx.service.create_project(
        name="blocker",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_block_bind",
        feishu_chat_type="group",
    )
    assert project.status == "active"

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=f"/bind {target_dir}", is_command=True),
    )

    assert feishu_ctx.service.get_chat_directory_binding("oc_block_bind") is None
    assert any("活跃或暂停项目" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_unbind_rejected_when_chat_has_active_project(feishu_ctx, tmp_path):
    """`/unbind` must be rejected when the chat has an active or paused project."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_block_unbind"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat(
        "oc_block_unbind", str(target_dir), "ou_1"
    )

    await feishu_ctx.service.create_project(
        name="blocker",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_block_unbind",
        feishu_chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/unbind", is_command=True),
    )

    # Binding should still exist (reject happened before removal).
    assert feishu_ctx.service.get_chat_directory_binding("oc_block_unbind") is not None
    assert any("活跃或暂停项目" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_bind_allowed_when_chat_only_has_terminal_projects(feishu_ctx, tmp_path):
    """`/bind` is allowed when chat projects are all completed/failed/cancelled."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_terminal"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "repo"
    target_dir.mkdir()

    project = await feishu_ctx.service.create_project(
        name="done",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_terminal",
        feishu_chat_type="group",
    )
    await feishu_ctx.service.cancel_project(project.id, reason="done")

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=f"/bind {target_dir}", is_command=True),
    )

    binding = feishu_ctx.service.get_chat_directory_binding("oc_terminal")
    assert binding is not None
    assert any("已将当前会话绑定到" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_bind_no_args_without_allowed_roots_falls_back(feishu_ctx, monkeypatch):
    """`/bind` (no args) without configured roots tells user to use /bind <path>."""
    from mac_system.feishu.handlers import handle_text_message
    import mac_system.config as cfg_module

    monkeypatch.setattr(cfg_module, "get_allowed_bind_roots", lambda: [])
    # Also patch the imported reference inside handlers.
    monkeypatch.setattr(
        "mac_system.config.get_allowed_bind_roots", lambda: []
    )

    feishu_ctx.chat_id = "oc_bind_noargs"
    feishu_ctx.chat_type = "group"

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    rendered = _sent_output_text(feishu_ctx)
    assert "MAC_ALLOWED_BIND_ROOTS" in rendered
    assert "/bind &lt;绝对路径&gt;" in rendered


async def test_bind_no_args_with_allowed_roots_shows_candidate_card(
    feishu_ctx, tmp_path, monkeypatch
):
    """`/bind` (no args) with configured roots scans and sends a card."""
    from mac_system.feishu.handlers import handle_text_message

    # Set up an allowed root with a git repo inside.
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "my-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_bind_card"
    feishu_ctx.chat_type = "group"

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert "bind_dir_select:" in str(payload)
    assert str(repo) in str(payload)
    assert "bind_dir_manual" in str(payload)
    assert "bind_dir_cancel" in str(payload)


async def test_bind_dir_select_card_action_binds_directory(
    feishu_ctx, tmp_path, monkeypatch
):
    """Clicking `bind_dir_select:<path>` should bind that directory."""
    from mac_system.feishu.handlers import handle_card_action
    from mac_system.feishu.models import ButtonAction

    feishu_ctx.chat_id = "oc_bind_select"
    feishu_ctx.chat_type = "group"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("project", encoding="utf-8")
    (repo / ".git").mkdir()
    # _prepare_bound_directory expects a real git repo with HEAD; initialize one.
    import subprocess
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"],
        cwd=repo, capture_output=True, check=True,
    )

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key=f"bind_dir_select:{repo}",
            extras={},
            chat_id="oc_bind_select",
            chat_type="group",
            message_id="m1",
        ),
    )

    binding = feishu_ctx.service.get_chat_directory_binding("oc_bind_select")
    assert binding is not None
    assert binding.directory == str(repo.resolve())


async def test_bind_dir_manual_action_sets_pending_intent(feishu_ctx, monkeypatch):
    """Clicking `bind_dir_manual` should set a pending intent for path input."""
    from mac_system.feishu.handlers import handle_card_action
    from mac_system.feishu.models import ButtonAction

    feishu_ctx.chat_id = "oc_bind_manual"
    feishu_ctx.chat_type = "group"

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key="bind_dir_manual",
            extras={},
            chat_id="oc_bind_manual",
            chat_type="group",
            message_id="m1",
        ),
    )

    pending = feishu_ctx.service.get_pending_intent("ou_1")
    assert pending is not None
    assert pending.stage == "awaiting_bind_dir"
    assert any("绝对路径" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_bind_manual_input_path_binds_directory(feishu_ctx, tmp_path, monkeypatch):
    """After `bind_dir_manual`, typing a path should bind that directory."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_bind_path"
    feishu_ctx.chat_type = "group"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"],
        cwd=repo, capture_output=True, check=True,
    )

    # Set pending intent as if user had clicked "manual input".
    feishu_ctx.service.upsert_pending_intent(
        "ou_1", description=None, stage="awaiting_bind_dir",
        chat_id="oc_bind_path", chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=str(repo), is_command=False),
    )

    binding = feishu_ctx.service.get_chat_directory_binding("oc_bind_path")
    assert binding is not None
    assert binding.directory == str(repo.resolve())
    assert feishu_ctx.service.get_pending_intent("ou_1") is None


async def test_bind_dir_cancel_action_sends_cancel_message(feishu_ctx, monkeypatch):
    """Clicking `bind_dir_cancel` should send a cancel message and not bind."""
    from mac_system.feishu.handlers import handle_card_action
    from mac_system.feishu.models import ButtonAction

    feishu_ctx.chat_id = "oc_bind_cancel"
    feishu_ctx.chat_type = "group"

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key="bind_dir_cancel",
            extras={},
            chat_id="oc_bind_cancel",
            chat_type="group",
            message_id="m1",
        ),
    )

    assert any("已取消" in str(c[2]) for c in feishu_ctx.bridge.sent)
    assert feishu_ctx.service.get_chat_directory_binding("oc_bind_cancel") is None


async def test_scan_candidate_repositories_finds_git_dirs(tmp_path):
    """The scanner should find directories with `.git` under allowed roots."""
    from mac_system.core.project_service import ProjectService

    root = tmp_path / "root"
    root.mkdir()
    repo1 = root / "repo1"
    repo1.mkdir()
    (repo1 / ".git").mkdir()
    repo2 = root / "subdir" / "repo2"
    repo2.mkdir(parents=True)
    (repo2 / ".git").mkdir()
    non_repo = root / "not-a-repo"
    non_repo.mkdir()

    candidates = ProjectService.scan_candidate_repositories([root], max_depth=3)
    paths = {c["path"] for c in candidates}
    assert str(repo1) in paths
    assert str(repo2) in paths
    assert str(non_repo) not in paths


class _FakeOrchestratorWithBindDir:
    """Fake orchestrator that returns canned bind_dir agent output."""

    def __init__(self, agent_output=None):
        self.ticked = []
        self.cancelled = []
        self.agent_output = agent_output
        self.agent_calls = []
        self.project_service = None
        self.interrupted = []

    async def tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    def _schedule_followup_tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        self.cancelled.append((project_id, reason))
        if self.project_service is not None:
            await self.project_service.cancel_project(project_id, reason=reason)

    def interrupt_running_agents(self, project_id: str) -> list[str]:
        self.interrupted.append(project_id)
        return ["task_running"]

    async def run_chat_scoped_agent(
        self, agent_type, stage, input_data, extra_variables=None, *, worktree=None,
        readonly_dirs=None, max_tool_calls=None, progress_callback=None,
    ):
        self.agent_calls.append({
            "agent_type": agent_type,
            "stage": stage,
            "input_data": input_data,
            "extra_variables": extra_variables,
            "worktree": worktree,
            "readonly_dirs": readonly_dirs,
            "max_tool_calls": max_tool_calls,
            "progress_callback": progress_callback,
        })
        return self.agent_output

    def _agent_label_for_state(self, state: str) -> str:
        return state


class _FakeOrchestratorWithRetry(_FakeOrchestratorWithBindDir):
    """Fake orchestrator that fails a configurable number of times before succeeding."""

    def __init__(self, outputs: list[dict[str, Any] | Exception]):
        super().__init__()
        self._outputs = outputs
        self._index = 0

    async def run_chat_scoped_agent(self, *args, **kwargs):
        self.agent_calls.append({"args": args, "kwargs": kwargs})
        out = self._outputs[self._index]
        self._index = min(self._index + 1, len(self._outputs) - 1)
        if isinstance(out, Exception):
            raise out
        return out


async def test_conversation_coordinator_answers_without_creating_project(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import Project

    feishu_ctx.chat_id = "oc_conversation"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "当前更适合先讨论架构，不会开始修改代码。",
        "intent": "discuss",
        "effect": "none",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "正在讨论系统架构",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_discuss",
            text="先讨论大方向，再讨论细节",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_conversation",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    conversation = feishu_ctx.service.get_or_create_conversation_session("oc_conversation")
    assert conversation.summary == "正在讨论系统架构"
    assert [turn["role"] for turn in conversation.recent_turns] == ["user", "assistant"]
    assert feishu_ctx.bridge.sent[0][0] == "interactive"
    assert "正在处理" in str(feishu_ctx.bridge.sent_history[0][2])
    assert "不会开始修改代码" in str(feishu_ctx.bridge.updated[-1][1])
    assert "不会开始修改代码" in str(feishu_ctx.bridge.sent[-1][2])
    assert feishu_ctx.bridge.reactions == [
        ("add", "om_discuss", "OnIt"),
        ("remove", "om_discuss", "reaction_1"),
    ]


async def test_conversation_bound_workspace_is_readable_on_first_turn_and_retry(
    feishu_ctx, tmp_path
):
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_retry_without_tools"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("project", encoding="utf-8")
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "group"
    feishu_ctx.service.bind_directory_to_chat(chat_id, str(repo), "ou_1")
    fake = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "正在检查。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(repo.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["tests", "implementation"],
            },
        },
        {
            "success": True,
            "reply": "正在选择证据。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(repo.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["README.md"],
            },
        },
        {"success": False, "error": "non-json tool result"},
        {
            "success": True,
            "reply": "只读检查未形成结构化结论，请指定要看的文件。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "检查降级为安全回答",
        },
    ])
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_retry_read", text="帮我检查代码",
            is_command=False, is_at_bot=True, chat_id=chat_id, chat_type="group",
        ),
    )

    assert fake.agent_calls[0]["kwargs"]["worktree"] is None
    assert fake.agent_calls[1]["kwargs"]["worktree"] == repo.resolve()
    read_roots = fake.agent_calls[0]["kwargs"]["readonly_dirs"]
    assert any(repo.resolve() == root or root in repo.resolve().parents for root in read_roots)
    assert fake.agent_calls[0]["kwargs"]["max_tool_calls"] == 0
    assert fake.agent_calls[1]["kwargs"]["worktree"] == repo.resolve()
    assert json.loads(
        fake.agent_calls[1]["kwargs"]["extra_variables"]["repository_file_manifest"]
    ) == ["README.md"]
    assert all(call["kwargs"]["readonly_dirs"] == read_roots for call in fake.agent_calls)
    assert [call["kwargs"]["max_tool_calls"] for call in fake.agent_calls[:2]] == [0, 8]
    assert fake.agent_calls[2]["kwargs"]["max_tool_calls"] == 0


async def test_conversation_context_request_can_switch_within_configured_read_root(
    feishu_ctx, tmp_path
):
    from mac_system.feishu.handlers import handle_text_message

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    chat_id = "oc_context_scope"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    feishu_ctx.service.bind_directory_to_chat(chat_id, str(allowed), "ou_1")
    fake = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "正在检查。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(outside.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["README"],
            },
        },
        {
            "success": True,
            "reply": "无法从已授权上下文确认。",
            "intent": "explain",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
        },
    ])
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_context_scope", text="核对实现",
            is_command=False, chat_type="p2p",
        ),
    )

    assert any(
        call["kwargs"]["worktree"] == outside.resolve()
        for call in fake.agent_calls
    )
    assert all(
        any(
            outside.resolve() == root or root in outside.resolve().parents
            for root in call["kwargs"]["readonly_dirs"]
        )
        for call in fake.agent_calls
    )


async def test_conversation_context_request_cannot_escape_configured_read_roots(
    feishu_ctx, tmp_path, monkeypatch
):
    from mac_system.feishu.handlers import handle_text_message
    import mac_system.config as cfg_module

    allowed_root = tmp_path / "allowed_root"
    workspace = allowed_root / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    monkeypatch.setattr(cfg_module, "get_allowed_bind_roots", lambda: [allowed_root])

    chat_id = "oc_context_escape"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    feishu_ctx.service.bind_directory_to_chat(chat_id, str(workspace), "ou_1")
    fake = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "正在检查。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(outside.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["README.md"],
            },
        },
        {
            "success": True,
            "reply": "该目录不在配置的只读根范围内。",
            "intent": "explain",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
        },
    ])
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_context_escape",
            text="核对另一个目录",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert outside.resolve() not in {
        call["kwargs"]["worktree"] for call in fake.agent_calls
    }
    assert all(
        call["kwargs"]["readonly_dirs"] == [allowed_root.resolve()]
        for call in fake.agent_calls
    )


def test_repository_file_manifest_is_bounded_and_excludes_generated_or_secret_paths(
    tmp_path,
):
    from mac_system.feishu.handlers import _repository_file_manifest

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.js").write_text("x", encoding="utf-8")

    manifest = _repository_file_manifest(tmp_path, max_files=2)

    assert manifest == [".env.example", "src/app.py"]


async def test_conversation_non_json_prose_repairs_once_then_relays_without_action(
    feishu_ctx,
):
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    feishu_ctx.chat_id = "oc_safe_prose"
    feishu_ctx.chat_type = "p2p"
    fake = _FakeOrchestratorWithBindDir({
        "success": False,
        "error": "Invalid agent output: result is not a JSON object",
        "result_text": "根因是输出协议失败；本轮没有执行修改。",
        "_runtime": {"tool_calls": []},
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_safe_prose", text="解释根因",
            is_command=False, chat_type="p2p",
        ),
    )

    assert len(fake.agent_calls) == 2
    assert "根因是输出协议失败" in str(feishu_ctx.bridge.sent[-1][2])
    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []


async def test_conversation_schema_invalid_json_is_supplied_to_repair_attempt(
    feishu_ctx,
):
    from mac_system.feishu.handlers import handle_text_message

    invalid = {
        "subject_key": "project.paused_resolution",
        "question": "选哪条路？",
        "options": [{"key": "resume", "label": "继续"}],
    }
    valid = {
        "success": True,
        "reply": "刚才只是解释问题，没有执行项目控制。",
        "intent": "explain",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
    }
    fake = _FakeOrchestratorWithRetry([
        {
            "success": False,
            "error": "Invalid agent output: schema violation",
            "result_text": json.dumps(invalid, ensure_ascii=False),
            "_runtime": {"tool_calls": []},
        },
        valid,
    ])
    feishu_ctx.orchestrator = fake
    feishu_ctx.chat_id = "oc_schema_repair"
    feishu_ctx.chat_type = "p2p"

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_schema_repair",
            text="为什么这么多轮？", is_command=False, chat_type="p2p",
        ),
    )

    assert len(fake.agent_calls) == 2
    retry_feedback = fake.agent_calls[1]["kwargs"]["extra_variables"][
        "retry_feedback"
    ]
    assert "project.paused_resolution" in retry_feedback
    assert "没有执行项目控制" in str(feishu_ctx.bridge.sent[-1])


async def test_conversation_ignores_cancelled_project_reference(feishu_ctx):
    """A stale conversation active_project_id pointing to a cancelled project
    must not make the coordinator offer confirm/cancel options again.
    """
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_cancelled_ref"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"

    project = await feishu_ctx.service.create_project(
        name="stale", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    await feishu_ctx.service.cancel_project(project.id)

    # Simulate a conversation session that still references the dead project.
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目仍处于悬空状态",
        active_project_id=project.id,
        update_active_project=True,
    )

    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "当前没有活跃项目，可以重新开始。",
        "intent": "answer",
        "effect": "none",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "用户询问状态，已告知无活跃项目",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_status",
            text="现在是啥情况",
            is_command=False,
            chat_type="p2p",
        ),
    )

    call = fake.agent_calls[0]
    assert json.loads(call["extra_variables"]["active_project"]) is None
    assert json.loads(call["extra_variables"]["pending_actions"]) == []

    refreshed = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert refreshed.active_project_id is None
    assert refreshed.summary != "项目仍处于悬空状态"


async def test_conversation_grants_repo_only_after_structured_context_request(
    feishu_ctx, tmp_path, monkeypatch
):
    """When the chat is unbound and the user mentions a local repo, the
    coordinator should receive that repo as its workspace so it can read it.
    """
    from mac_system.feishu.handlers import handle_text_message
    import mac_system.config as cfg_module

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "wechat-mac-rpa"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".env.example").write_text(
        "DEEPSEEK_API_KEY=example\nOPENAI_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text(
        "DEEPSEEK_API_KEY=never-render-this-secret\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg_module, "get_allowed_bind_roots", lambda: [allowed_root])

    chat_id = "oc_repo_mention"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    fake = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "正在核对。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(repo.resolve()),
                "context_capabilities": [
                    "workspace_read", "capability_metadata"
                ],
                "read_targets": ["README", ".env.example"],
            },
        },
        {
            "success": True,
            "reply": "正在选择证据。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(repo.resolve()),
                "context_capabilities": [
                    "workspace_read", "capability_metadata"
                ],
                "read_targets": [".env.example"],
            },
        },
        {
            "success": True,
            "reply": "已找到并读取参考项目。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "用户参考 mac-rpa 项目",
        },
    ])
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_repo",
            text="参考mac-rpa项目的个人agent实现",
            is_command=False,
            chat_type="p2p",
        ),
    )

    first = fake.agent_calls[0]["kwargs"]
    assert first["worktree"] is None
    assert first["readonly_dirs"] == [allowed_root.resolve()]
    assert json.loads(first["extra_variables"]["repository_capability_metadata"]) == {}
    call = fake.agent_calls[1]["kwargs"]
    assert call["worktree"] == repo.resolve()
    assert call["readonly_dirs"] == [allowed_root.resolve()]
    assert call["max_tool_calls"] == 8
    extra = call["extra_variables"]
    assert extra["workspace_path"] == "未绑定工作区"
    capability_metadata = json.loads(extra["repository_capability_metadata"])
    assert capability_metadata[str(repo.resolve())] == [
        "DEEPSEEK_API_KEY", "OPENAI_BASE_URL"
    ]
    repos = json.loads(extra["available_repositories"])
    assert any(r["name"] == "wechat-mac-rpa" for r in repos)

    assert "never-render-this-secret" not in json.dumps(extra, ensure_ascii=False)
    final = fake.agent_calls[2]["kwargs"]
    assert final["worktree"] == repo.resolve()
    assert final["readonly_dirs"] == [allowed_root.resolve()]


def test_full_reference_delivery_does_not_require_chat_preflight_inventory():
    from mac_system.feishu.handlers import _conversation_contract_error

    decisions = [{
        "key": "reference.constraints_exclusions",
        "status": "confirmed",
        "details": {"user_confirmed_exclusions": []},
    }, {
        "key": "reference.behavioral_probes",
        "status": "confirmed",
        "details": {"probes": []},
    }]
    for decision in decisions:
        decision.setdefault("question", decision["key"])
        decision.setdefault("answer", "已盘点")
    output = {
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "full",
            "description": "完整复刻参考项目的能力和行为",
            "acceptance_criteria": ["页面可访问"],
            "out_of_scope": ["source-tool 自动化"],
            "decisions": decisions,
        }
    }

    # Capability discovery is performed by the persistent project Coordinator
    # after creation. The chat router must not duplicate that audit or delay an
    # already-authorized delivery before a project exists.
    assert _conversation_contract_error(output) is None


def test_iteration_contract_requires_bounded_scope_and_acceptance_criteria():
    from mac_system.feishu.handlers import _conversation_contract_error

    output = {
        "proposed_action": {
            "type": "start_delivery",
            "task_type": "iteration",
            "reference_mode": "none",
            "scope": [],
            "acceptance_criteria": [],
            "decisions": [],
        }
    }
    assert "scope" in (_conversation_contract_error(output) or "")

    output["proposed_action"]["scope"] = ["bounded change"]
    assert "acceptance criteria" in (_conversation_contract_error(output) or "")

    output["proposed_action"]["acceptance_criteria"] = ["observable result"]
    assert _conversation_contract_error(output) is None

    output["proposed_action"]["reference_mode"] = "full"
    assert "full reference" in (_conversation_contract_error(output) or "")


def test_conversation_retry_feedback_inherits_reads_and_keeps_read_tools():
    from mac_system.feishu.handlers import _conversation_retry_feedback

    feedback = _conversation_retry_feedback(
        "identity quote mismatch",
        verified_read_paths={"/repo/prompts/persona.md"},
    )

    assert "identity quote mismatch" in feedback
    assert "/repo/prompts/persona.md" in feedback
    assert "继续提供同一组只读工具" in feedback
    assert "只有确需补证时才再次调用 Read" in feedback


def test_conversation_retry_feedback_carries_semantic_candidate():
    from mac_system.feishu.handlers import _conversation_retry_feedback

    feedback = _conversation_retry_feedback(
        "files_read was not read in this run",
        verified_read_paths={"/repo/persona.md"},
        previous_candidate={
            "success": True,
            "proposed_action": {"type": "propose_start_delivery"},
            "_runtime": {"tool_calls": [{"name": "Read"}]},
        },
    )

    assert '"type":"propose_start_delivery"' in feedback
    assert "只修正上面列出的错误" in feedback
    assert '"_runtime"' not in feedback


def test_acceptance_criterion_reference_matches_stable_ac_id():
    from mac_system.feishu.handlers import _acceptance_criterion_reference_matches

    criteria = {"AC-FEATURE-001 从真实入口保持源行为"}
    assert _acceptance_criterion_reference_matches("AC-FEATURE-001", criteria)
    assert _acceptance_criterion_reference_matches(next(iter(criteria)), criteria)
    assert not _acceptance_criterion_reference_matches("AC-OTHER-001", criteria)


def test_complete_reference_contract_rejects_probe_for_unknown_capability(tmp_path):
    from mac_system.feishu.handlers import _complete_reference_contract_error

    source = tmp_path / "behavior.py"
    source.write_text("def respond(): return 'stable behavior'\n", encoding="utf-8")
    criterion = "AC-BEHAVIOR-001 真实入口保持源行为"
    decisions = [{
        "key": "reference.source_repo", "status": "confirmed",
        "details": {"source_path": str(tmp_path)},
    }, {
        "key": "reference.capability_inventory", "status": "confirmed",
        "details": {"capabilities": [{
            "id": "cap-core", "name": "核心行为", "category": "domain",
            "observable_behavior": "返回稳定结果", "user_visible": True,
            "reuse_policy": "adapt", "source_files": ["behavior.py"],
        }]},
    }, {
        "key": "reference.behavioral_probes", "status": "confirmed",
        "details": {"probes": [{
            "capability_id": "cap-unknown", "input": "运行",
            "expected_output": "stable behavior", "acceptance_criterion": criterion,
            "source_file": "behavior.py", "source_quote": "stable behavior",
        }]},
    }, {
        "key": "reference.constraints_exclusions", "status": "confirmed",
        "details": {"user_confirmed_exclusions": []},
    }]
    for decision in decisions:
        decision.setdefault("question", decision["key"])
        decision.setdefault("answer", "已盘点")
    output = {
        "_runtime": {"tool_calls": [{
            "name": "Read", "input": {"file_path": str(source)},
        }]},
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "full",
            "description": "完整复刻参考项目",
            "acceptance_criteria": [criterion],
            "out_of_scope": [],
            "decisions": decisions,
        },
    }

    error = _complete_reference_contract_error(
        output["proposed_action"], runtime_evidence=output["_runtime"]
    )
    assert "capability_id is not in capability_inventory" in str(error)
    assert "missing user-visible capabilities: cap-core" in str(error)


def test_complete_reference_contract_accepts_generic_capability_inventory(
    tmp_path,
):
    from mac_system.feishu.handlers import _complete_reference_contract_error

    engine = tmp_path / "engine.py"
    engine.write_text(
        "def convert(value): return value.upper()\n"
        "def batch(values): return [convert(v) for v in values]\n",
        encoding="utf-8",
    )
    criteria = [
        "AC-CONVERT-001 真实入口转换输入",
        "AC-BATCH-001 真实入口批量转换输入",
    ]
    capabilities = [{
        "id": "cap-convert", "name": "格式转换", "category": "transformation",
        "observable_behavior": "输入被转换为大写", "user_visible": True,
        "reuse_policy": "copy", "source_files": ["engine.py"],
    }, {
        "id": "cap-batch", "name": "批量处理", "category": "workflow",
        "observable_behavior": "一次转换多个输入", "user_visible": True,
        "reuse_policy": "adapt", "source_files": ["engine.py"],
    }]
    probes = [{
        "capability_id": "cap-convert", "input": "convert abc",
        "expected_output": "ABC", "acceptance_criterion": criteria[0],
        "source_file": "engine.py", "source_quote": "value.upper()",
    }, {
        "capability_id": "cap-batch", "input": "batch a,b",
        "expected_output": "A,B", "acceptance_criterion": criteria[1],
        "source_file": "engine.py", "source_quote": "def batch",
    }]
    decisions = [{
        "key": "reference.source_repo",
        "status": "confirmed",
        "details": {"source_path": str(tmp_path)},
    }, {
        "key": "reference.capability_inventory", "status": "confirmed",
        "details": {"capabilities": capabilities},
    }, {
        "key": "reference.behavioral_probes", "status": "confirmed",
        "details": {"probes": probes},
    }, {
        "key": "reference.constraints_exclusions", "status": "confirmed",
        "details": {"user_confirmed_exclusions": ["legacy dashboard"]},
    }]
    for decision in decisions:
        decision.setdefault("question", decision["key"])
        decision.setdefault("answer", "已盘点")
    output = {
        "_runtime": {"tool_calls": [{
            "name": "Read", "input": {"file_path": str(engine)},
        }]},
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "full",
            "description": "完整复刻参考项目的能力和行为",
            "acceptance_criteria": criteria,
            "out_of_scope": ["legacy dashboard"],
            "decisions": decisions,
        }
    }

    def validate(candidate, *, verified_read_paths=None):
        return _complete_reference_contract_error(
            candidate["proposed_action"],
            runtime_evidence=candidate.get("_runtime"),
            verified_read_paths=verified_read_paths,
        )

    assert validate(output) is None
    decisions[2]["details"]["probes"] = probes[:1]
    assert "missing user-visible capabilities: cap-batch" in str(
        validate(output)
    )
    decisions[2]["details"]["probes"] = probes
    output_without_runtime = dict(output)
    output_without_runtime.pop("_runtime")
    assert validate(
        output_without_runtime,
        verified_read_paths={str(engine)},
    ) is None
    capabilities[1]["source_files"] = ["missing.py"]
    assert "does not exist" in str(validate(output))


def test_complete_reference_contract_reports_independent_errors_together(tmp_path):
    from mac_system.feishu.handlers import _complete_reference_contract_error

    evidence = tmp_path / "evidence.md"
    evidence.write_text("observable source behavior", encoding="utf-8")
    decisions = [{
        "key": "reference.source_repo", "question": "源仓库", "answer": "错误路径",
        "status": "confirmed", "details": {"source_path": "/not/a/real/repo"},
    }, {
        "key": "reference.capability_inventory", "status": "confirmed",
        "details": {"capabilities": [{
            "id": "cap-a", "name": "能力 A", "category": "domain",
            "observable_behavior": "可见行为", "user_visible": True,
            "reuse_policy": "copy", "source_files": ["missing.py"],
        }]},
    }, {
        "key": "reference.behavioral_probes", "status": "confirmed",
        "details": {"probes": [{
            "capability_id": "cap-a", "input": "run",
            "expected_output": "observable", "acceptance_criterion": "AC-UNKNOWN-001",
            "source_file": str(evidence), "source_quote": "observable source behavior",
        }]},
    }, {
        "key": "reference.constraints_exclusions", "status": "confirmed",
        "details": {"user_confirmed_exclusions": []},
    }]
    for decision in decisions:
        decision.setdefault("question", decision["key"])
        decision.setdefault("answer", "已盘点")
    output = {"_runtime": {"tool_calls": [{
        "name": "Read", "input": {"file_path": str(evidence)},
    }]}, "proposed_action": {
        "type": "propose_start_delivery", "reference_mode": "full",
        "description": "完整复刻参考项目",
        "acceptance_criteria": ["AC-OTHER-001 其他标准"], "out_of_scope": [],
        "decisions": decisions,
    }}

    error = str(_complete_reference_contract_error(
        output["proposed_action"], runtime_evidence=output["_runtime"]
    ))
    assert "acceptance_criterion" in error
    assert "source_path" in error
    assert "does not exist" in error


@pytest.mark.asyncio
async def test_submit_input_returns_real_failure_instead_of_false_success(feishu_ctx):
    from mac_system.feishu.handlers import _submit_input

    project = await feishu_ctx.service.create_project(
        name="input-result",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "ARCHITECTURE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_ARCHITECTURE",
        agent="planner",
        stage="architecture",
        batch_key="input-result",
        questions=[{
            "question_id": "q_real",
            "kind": "blocking",
            "question": "Where should this run?",
            "allow_free_text": True,
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="Please answer",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    result = await _submit_input(
        feishu_ctx,
        "ou_1",
        f"{approval.id} an answer",
        question_id="q_missing",
    )

    assert result == "问题 q_missing 不属于当前输入批次。"
    assert feishu_ctx.service.get_approval(approval.id).status == "pending"
    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert feishu_ctx.service.get_question_answer(question.id) is None


@pytest.mark.asyncio
async def test_submit_input_with_question_id_preserves_full_natural_answer(feishu_ctx):
    from mac_system.feishu.handlers import _submit_input

    project = await feishu_ctx.service.create_project(
        name="question-id-answer",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "ACCEPTANCE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="manual-acceptance",
        questions=[{
            "question_id": "q_manual_acceptance_verify",
            "kind": "blocking",
            "question": "请反馈浏览器验收结果",
            "allow_free_text": True,
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="请回答",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    answer = "q_manual_acceptance_verify 的答案：三项尚未执行，不是失败"
    result = await _submit_input(
        feishu_ctx,
        "ou_1",
        answer,
        question_id="q_manual_acceptance_verify",
    )

    assert result == "已收到你的回答，Agent 继续执行。"
    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert feishu_ctx.service.get_question_answer(question.id).answer == answer
    assert feishu_ctx.service.get_approval(approval.id).status == "responded"
    assert feishu_ctx.service.require_project(project.id).current_state == "ACCEPTANCE_PENDING"


@pytest.mark.asyncio
async def test_coordinator_input_is_scoped_to_active_project_and_records_option(
    feishu_ctx,
):
    """Historical pending inputs must not block a resolved active-project answer."""
    from mac_system.feishu.handlers import _submit_input

    old_project = await feishu_ctx.service.create_project(
        name="historical-input",
        task_type="feature",
        description="old",
        creator_open_id="ou_1",
    )
    old_project.current_state = "ACCEPTANCE_INPUT_PENDING"
    old_interaction = await feishu_ctx.service.create_question_interaction(
        project_id=old_project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="old-manual",
        questions=[{
            "question_id": "q_old",
            "kind": "blocking",
            "question": "旧项目问题",
            "allow_free_text": True,
        }],
    )
    old_approval = await feishu_ctx.service.create_approval(
        project_id=old_project.id,
        task_id=None,
        state=old_project.current_state,
        approval_type="input",
        question="旧问题",
        options=[],
        payload={"question_interaction_id": old_interaction.id},
    )
    old_interaction.approval_id = old_approval.id

    project = await feishu_ctx.service.create_project(
        name="current-input",
        task_type="feature",
        description="current",
        creator_open_id="ou_1",
    )
    project.current_state = "ACCEPTANCE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="current-manual",
        questions=[{
            "question_id": "q_manual_failure",
            "kind": "blocking",
            "question": "停止服务后错误提示是否符合预期？",
            "allow_free_text": True,
            "options": [
                {"key": "verified_pass", "label": "完整通过"},
                {"key": "verified_fail", "label": "验证失败"},
                {"key": "not_verified", "label": "尚未验证"},
            ],
            "affected_trace_ids": ["AC-ERROR"],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="当前问题",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    evidence_text = "停止服务后只显示 Failed to fetch，不符合中文错误提示要求"
    result = await _submit_input(
        feishu_ctx,
        "ou_1",
        evidence_text,
        question_id="q_manual_failure",
        project_id=project.id,
        answer_key="verified_fail",
        rework_target="coding",
    )

    assert result == "已收到你的回答，Agent 继续执行。"
    assert feishu_ctx.service.get_approval(approval.id).status == "responded"
    assert feishu_ctx.service.get_approval(old_approval.id).status == "pending"
    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = feishu_ctx.service.get_question_answer(question.id)
    assert answer.answer == "verified_fail"
    assert answer.answer_label == evidence_text
    active_evidence = feishu_ctx.service.get_active_acceptance_evidence(project.id)
    assert active_evidence["AC-ERROR"].verdict == "fail"
    feedback = feishu_ctx.service.get_open_feedback_packets(project.id)
    assert len(feedback) == 1
    assert feedback[0].kind == "acceptance_rejection"
    assert feedback[0].recommended_rework_target == "coding"
    assert feedback[0].user_comment == evidence_text


@pytest.mark.asyncio
async def test_submit_input_does_not_treat_long_first_word_as_approval_id(feishu_ctx):
    from mac_system.feishu.handlers import _submit_input

    project = await feishu_ctx.service.create_project(
        name="long-natural-answer",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_REQUIREMENTS",
        agent="planner",
        stage="requirements",
        batch_key="long-answer",
        questions=[{
            "question_id": "q_environment",
            "kind": "blocking",
            "question": "部署环境是什么？",
            "allow_free_text": True,
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="请回答",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    answer = "physical-device-browser 作为最终验收入口"
    result = await _submit_input(feishu_ctx, "ou_1", answer)

    assert result == "已收到你的回答，Agent 继续执行。"
    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert feishu_ctx.service.get_question_answer(question.id).answer == answer


async def test_conversation_coordinator_capabilities_do_not_depend_on_wording(
    feishu_ctx, tmp_path
):
    """Every wording starts with the same no-tools capability boundary."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_bound_any_phrasing"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "bound_repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat(chat_id, str(target_dir), "ou_1")

    phrasings = [
        "帮我看看测试覆盖率为什么低",
        "检查这个项目依赖了哪些第三方库",
        "随便聊聊",
        "为什么不配置真实api",
        "help me summarize the code structure",
        "用任何未曾出现在测试里的表达方式聊这个项目",
    ]
    for i, text in enumerate(phrasings):
        fake = _FakeOrchestratorWithBindDir({
            "success": True,
            "reply": "好的。",
            "intent": "discuss",
            "effect": "none",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "闲聊",
        })
        feishu_ctx.orchestrator = fake
        await handle_text_message(
            feishu_ctx,
            TextMessage(
                open_id="ou_1",
                message_id=f"om_any_{i}",
                text=text,
                is_command=False,
                is_at_bot=True,
                chat_id=chat_id,
                chat_type="group",
            ),
        )
        assert fake.agent_calls, f"coordinator not invoked for: {text}"
        assert fake.agent_calls[0]["worktree"] is None, text
        assert any(
            target_dir.resolve() == root or root in target_dir.resolve().parents
            for root in fake.agent_calls[0]["readonly_dirs"]
        ), text
        assert fake.agent_calls[0]["max_tool_calls"] == 0, text


async def test_conversation_coordinator_gets_host_read_roots_when_unbound(feishu_ctx):
    """Association is optional; configured host roots remain readable."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_unbound_plain"
    feishu_ctx.chat_type = "group"
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "你好，有什么可以帮你？",
        "intent": "discuss",
        "effect": "none",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "打招呼",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_unbound_1",
            text="你好，聊聊",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_unbound_plain",
            chat_type="group",
        ),
    )

    assert fake.agent_calls
    assert fake.agent_calls[0]["worktree"] is None
    assert fake.agent_calls[0]["readonly_dirs"]



async def test_conversation_coordinator_updates_progress_to_markdown_card(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_rich_reply"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "## 调研结论\n\n- 方案 A\n- 方案 B\n\n**推荐：A**",
        "intent": "research",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "完成方案调研",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_rich", text="调研两个方案",
            is_command=False, is_at_bot=True,
            chat_id="oc_rich_reply", chat_type="group",
        ),
    )

    assert [item[0] for item in feishu_ctx.bridge.sent] == ["interactive"]
    assert "调研结论" in str(feishu_ctx.bridge.updated[-1][1])
    assert "调研结论" in str(feishu_ctx.bridge.sent[-1][2])
    assert feishu_ctx.bridge.sent[-1][2].get("schema") == "2.0"
    assert any(
        el.get("tag") == "markdown" and "调研结论" in (el.get("content") or "")
        for el in feishu_ctx.bridge.sent[-1][2].get("body", {}).get("elements", [])
    )


async def test_workspace_inspection_requires_completed_evidence(feishu_ctx):
    """An inspect label without evidence must not leak an action preview."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_inspect_no_evidence"
    feishu_ctx.chat_type = "p2p"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我来查看项目，稍后告诉你结果。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "inspect_workspace"},
        "_runtime": {"tool_calls": []},
        "updated_summary": "准备查看项目",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_inspect_no_evidence",
            text="看下当前项目", is_command=False, chat_type="p2p",
        ),
    )

    visible = str(feishu_ctx.bridge.sent[-1][2])
    assert "没有执行任何操作" in visible
    assert "inspect_workspace requires inspection evidence" not in visible
    conversation = feishu_ctx.service.get_or_create_conversation_session(
        "oc_inspect_no_evidence"
    )
    assert "我来查看项目" not in conversation.recent_turns[-1]["content"]


async def test_workspace_inspection_semantic_failure_retries_as_plain_answer(
    feishu_ctx,
):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_inspect_retry"
    feishu_ctx.chat_type = "p2p"
    feishu_ctx.orchestrator = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "我已经检查完成。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "inspect_workspace"},
            "inspection": {
                "files_read": ["README.md"],
                "summary": "声称读过",
                "findings": [{"title": "结论", "evidence": "README.md"}],
            },
            "_runtime": {"tool_calls": []},
        },
        {
            "success": True,
            "reply": "当前没有可验证的读取证据，因此不声称已经检查文件。",
            "intent": "provide_information",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "未取得读取证据",
        },
    ])

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_inspect_retry",
            text="看下当前项目",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert len(feishu_ctx.orchestrator.agent_calls) == 2
    assert "inspect_workspace did not perform a Read" in str(
        feishu_ctx.orchestrator.agent_calls[1]["kwargs"]["extra_variables"][
            "retry_feedback"
        ]
    )
    assert "不声称已经检查文件" in str(feishu_ctx.bridge.sent[-1][2])


def test_read_path_relevance_is_scoped_to_selected_workspace(tmp_path):
    from mac_system.feishu.handlers import _read_path_within_workspace

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("project", encoding="utf-8")
    outside = tmp_path / "mac-system" / "specs" / "runtime.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("platform", encoding="utf-8")

    assert _read_path_within_workspace("README.md", workspace)
    assert _read_path_within_workspace(str(workspace / "README.md"), workspace)
    assert not _read_path_within_workspace(str(outside), workspace)
    assert not _read_path_within_workspace("../mac-system/specs/runtime.md", workspace)


async def test_coordinator_retries_when_project_answer_uses_mac_system_evidence(
    feishu_ctx, tmp_path, monkeypatch,
):
    from mac_system.feishu.handlers import handle_text_message
    import mac_system.config as cfg_module

    allowed_root = tmp_path / "allowed"
    project_repo = allowed_root / "personal-ai-site"
    project_repo.mkdir(parents=True)
    (project_repo / ".git").mkdir()
    project_readme = project_repo / "README.md"
    project_readme.write_text("run project", encoding="utf-8")
    mac_spec = tmp_path / "multi-agent-coding-system" / "specs" / "runtime.md"
    mac_spec.parent.mkdir(parents=True)
    mac_spec.write_text("MAC runtime", encoding="utf-8")
    monkeypatch.setattr(cfg_module, "get_allowed_bind_roots", lambda: [allowed_root])

    fake = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "正在核对启动信息。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(project_repo.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["README", "dependency manifest"],
            },
        },
        {
            "success": True,
            "reply": "正在选择启动证据。",
            "intent": "inspect",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {
                "type": "request_context",
                "repository_path": str(project_repo.resolve()),
                "context_capabilities": ["workspace_read"],
                "read_targets": ["README.md"],
            },
        },
        {
            "success": True,
            "reply": "只能确认项目 README 中的启动信息；MAC System spec 不是该项目事实。",
            "intent": "provide_information",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "仅使用目标项目证据回答",
            "_runtime": {"tool_calls": []},
        },
    ])
    feishu_ctx.chat_id = "oc_project_evidence_scope"
    feishu_ctx.chat_type = "p2p"
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_project_evidence_scope",
            text="personal-ai-site 项目怎么启动？",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert len(fake.agent_calls) == 3
    assert fake.agent_calls[0]["kwargs"]["worktree"] is None
    assert fake.agent_calls[0]["kwargs"]["readonly_dirs"] == [
        allowed_root.resolve()
    ]
    assert fake.agent_calls[1]["kwargs"]["worktree"] == project_repo.resolve()
    assert fake.agent_calls[1]["kwargs"]["readonly_dirs"] == [
        allowed_root.resolve()
    ]
    assert fake.agent_calls[1]["kwargs"]["max_tool_calls"] == 8
    bundle = json.loads(
        fake.agent_calls[2]["kwargs"]["extra_variables"]["repository_context_bundle"]
    )
    # Structured context negotiation can still supply an exact, redacted
    # content bundle after selecting any directory under the host read root.
    assert [item["path"] for item in bundle] == ["README.md"]
    assert bundle[0]["content"] == "run project"
    assert str(mac_spec) not in json.dumps(bundle, ensure_ascii=False)
    assert "MAC System spec 不是该项目事实" in str(feishu_ctx.bridge.sent[-1][2])


def test_workspace_inspection_retry_accepts_inherited_verified_read_paths(tmp_path):
    from mac_system.feishu.handlers import _conversation_contract_error

    evidence = tmp_path / "README.md"
    evidence.write_text("verified", encoding="utf-8")
    output = {
        "success": True,
        "reply": "已根据上一尝试的读取证据修正结论。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "inspect_workspace"},
        "inspection": {
            "files_read": [str(evidence)],
            "summary": "已修正",
            "findings": [{"title": "结论", "evidence": str(evidence)}],
        },
        "_runtime": {"tool_calls": []},
    }

    assert _conversation_contract_error(
        output, verified_read_paths={str(evidence)}
    ) is None


async def test_coordinator_receives_latest_test_and_capability_facts(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_project_snapshot"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="snapshot",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "TESTING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    task = await feishu_ctx.service.create_task(
        project.id, "tester", "TESTING", {}
    )
    await feishu_ctx.service.complete_task(
        task.id,
        {
            "passed": True,
            "report": "只运行了单元测试",
            "test_evidence": [
                {
                    "id": "TE-1",
                    "type": "unit",
                    "result": "pass",
                    "acceptance_criterion_ids": ["AC-1"],
                }
            ],
            "uncovered_acceptance_criterion_ids": ["AC-E2E"],
            "notes": ["真实 API 尚未验证"],
        },
        cost_info={
            "input_tokens": 100,
            "output_tokens": 25,
            "total_tokens": 125,
            "estimated_cost_usd": 0.2,
        },
    )
    feishu_ctx.service.create_context_record(
        project.id,
        record_type="capability",
        record_key="runtime.env.openai",
        value={
            "source_file": "/allowed/source/.env",
            "env_bindings": {"OPENAI_API_KEY": "OPENAI_API_KEY"},
            "applies_to": ["coder", "tester"],
        },
        source_type="user",
        sensitivity="secret_ref",
    )
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "真实 API 尚未验证。",
        "intent": "provide_information",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "说明测试证据",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_snapshot",
            text="这次测了真实 API 吗？",
            is_command=False,
            chat_type="p2p",
        ),
    )

    snapshot = json.loads(
        feishu_ctx.orchestrator.agent_calls[0]["extra_variables"][
            "active_project"
        ]
    )
    tester = next(
        item for item in snapshot["latest_tasks"] if item["agent"] == "tester"
    )
    assert tester["output"]["uncovered_acceptance_criterion_ids"] == ["AC-E2E"]
    assert tester["output"]["notes"] == ["真实 API 尚未验证"]
    assert tester["profiling"]["usage"]["total_tokens"] == 125
    assert tester["cost_usd"] == pytest.approx(0.2)
    assert any(
        fact["key"] == "runtime.env.openai"
        for fact in snapshot["active_facts"]
    )


@pytest.mark.asyncio
async def test_coordinator_snapshot_omits_failure_superseded_by_success(feishu_ctx):
    from mac_system.feishu.handlers import _coordinator_project_snapshot

    project = await feishu_ctx.service.create_project(
        name="retry-snapshot",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id="oc_retry_snapshot",
    )
    failed = await feishu_ctx.service.create_task(
        project.id, "coder", "CODING", {}
    )
    await feishu_ctx.service.fail_task(
        failed.id,
        "Agent contract violation: invalid first attempt",
    )
    succeeded = await feishu_ctx.service.create_task(
        project.id, "coder", "CODING", {}
    )
    await feishu_ctx.service.complete_task(
        succeeded.id,
        {
            "success": True,
            "next_state_event": "complete",
            "summary": "retry succeeded",
        },
    )

    snapshot = _coordinator_project_snapshot(feishu_ctx, project)
    coder_tasks = [
        item for item in snapshot["latest_tasks"] if item["agent"] == "coder"
    ]

    assert len(coder_tasks) == 1
    assert coder_tasks[0]["id"] == succeeded.id
    assert coder_tasks[0]["status"] == "completed"
    assert "invalid first attempt" not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["total_cost_usd"] == 0.0
    assert "provider-reported" in snapshot["cost_basis"]


@pytest.mark.asyncio
async def test_coordinator_snapshot_exposes_iteration_lineage_costs(feishu_ctx):
    from mac_system.feishu.handlers import _coordinator_project_snapshot

    base = await feishu_ctx.service.create_project(
        name="base-run", task_type="bug_fix", description="base"
    )
    base.total_cost_usd = 8.178246
    base.status = "completed"
    iteration = await feishu_ctx.service.create_project(
        name="follow-up", task_type="feature", description="iterate"
    )
    iteration.context = {**iteration.context, "base_project_id": base.id}
    feishu_ctx.service.session.commit()

    snapshot = _coordinator_project_snapshot(feishu_ctx, iteration)

    assert snapshot["lineage"][0]["id"] == base.id
    assert snapshot["lineage"][0]["total_cost_usd"] == pytest.approx(8.178246)


async def test_input_question_answer_is_not_misrouted_as_approval_confirmation(
    feishu_ctx,
):
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_input_answer_retry"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="input-answer",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "ARCHITECTURE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_ARCHITECTURE",
        agent="planner",
        stage="architecture",
        batch_key="deployment-location",
        questions=[{
            "question_id": "q_deployment",
            "kind": "blocking",
            "question": "系统部署在哪里？",
            "reason": "决定部署约束",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": [],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="请回答架构问题",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    feishu_ctx.orchestrator = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "已确认。",
            "intent": "approve",
            "effect": "workflow_control",
            "commitment": "authorized",
            "needs_clarification": False,
            "target": {"kind": "approval", "id": approval.id},
            "proposed_action": {
                "type": "propose_approval_resolution",
                "approval_decision": "confirm",
            },
            "updated_summary": "确认审批",
        },
        {
            "success": True,
            "reply": "已记录部署位置。",
            "intent": "provide_information",
            "effect": "workflow_control",
            "commitment": "authorized",
            "needs_clarification": False,
            "target": {"kind": "project", "id": project.id},
            "proposed_action": {
                "type": "route_project_input",
                "target_stage": "architecture",
            },
            "updated_summary": "系统部署在本机个人电脑",
        },
    ])

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_deployment_answer",
            text="在本机个人电脑上就可以",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert len(feishu_ctx.orchestrator.agent_calls) == 2
    first_snapshot = json.loads(
        feishu_ctx.orchestrator.agent_calls[0]["kwargs"]["extra_variables"][
            "pending_approvals"
        ]
    )
    assert first_snapshot[0]["input_questions"][0]["question"] == "系统部署在哪里？"
    retry_feedback = feishu_ctx.orchestrator.agent_calls[1]["kwargs"][
        "extra_variables"
    ]["retry_feedback"]
    assert "input approval cannot be confirmed" in retry_feedback
    feishu_ctx.service.session.refresh(approval)
    assert approval.status == "responded"
    assert project.current_state == "PLANNING_ARCHITECTURE"


async def test_coordinator_targets_exact_question_and_can_correct_prior_answer(
    feishu_ctx,
    tmp_path,
    monkeypatch,
):
    """Natural-language answers must not be assigned by question order."""
    from mac_system.feishu import handlers as handlers_module
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import ProjectContextRecord

    source_env = tmp_path / ".env"
    source_env.write_text("SOURCE_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(handlers_module, "get_allowed_bind_roots", lambda: [])

    chat_id = "oc_exact_input_question"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="exact-input",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "ARCHITECTURE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="PLANNING_ARCHITECTURE",
        agent="planner",
        stage="architecture",
        batch_key="multi-runtime",
        questions=[
            {
                "question_id": "q_llm",
                "kind": "blocking",
                "question": "LLM 配置是什么？",
                "allow_free_text": True,
                "options": [{"key": "custom", "label": "自定义"}],
            },
            {
                "question_id": "q_vector",
                "kind": "blocking",
                "question": "向量扩展如何加载？",
                "allow_free_text": True,
                "options": [],
            },
        ],
    )
    questions = {
        item.question_id: item
        for item in feishu_ctx.service.get_questions_for_interaction(interaction.id)
    }
    await feishu_ctx.service.submit_question_answer(
        questions["q_llm"].id,
        "误写成了向量扩展答案",
        "误写成了向量扩展答案",
        False,
        "ou_1",
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="input",
        question="请回答运行时问题",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已更正 LLM 配置答案。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "project", "id": project.id},
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "architecture",
            "question_id": "q_llm",
            "capability_refs": [{
                "source_file": str(source_env),
                "env_bindings": {"LLM_API_KEY": "SOURCE_API_KEY"},
            }],
        },
        "updated_summary": "更正 LLM 配置",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_correct_llm",
            text="刚才 LLM 的答案错了，应该复用现有配置",
            is_command=False,
            chat_type="p2p",
        ),
    )

    # Answering a waiting Agent is a snapshot-only routing turn.  The
    # Coordinator must not scan the target workspace or a referenced secret
    # source before emitting the structured capability reference.
    assert feishu_ctx.orchestrator.agent_calls[0]["worktree"] is None
    assert feishu_ctx.orchestrator.agent_calls[0]["max_tool_calls"] == 0
    assert feishu_ctx.orchestrator.agent_calls[0]["max_tool_calls"] == 0
    corrected = feishu_ctx.service.get_question_answer(questions["q_llm"].id)
    assert corrected.answer == "刚才 LLM 的答案错了，应该复用现有配置"
    assert questions["q_vector"].status == "pending"
    assert project.current_state == "ARCHITECTURE_INPUT_PENDING"
    capability = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(
            ProjectContextRecord.project_id == project.id,
            ProjectContextRecord.record_type == "capability",
        )
    ).one()
    assert capability.value["env_bindings"] == {
        "LLM_API_KEY": "SOURCE_API_KEY"
    }


def test_multiple_input_questions_require_explicit_question_id():
    from mac_system.feishu.handlers import _conversation_state_contract_error

    error = _conversation_state_contract_error(
        {
            "intent": "provide_information",
            "proposed_action": {"type": "route_project_input"},
        },
        approvals=[{
            "id": "appr_1",
            "type": "input",
            "input_questions": [
                {"question_id": "q_llm", "status": "pending"},
                {"question_id": "q_vector", "status": "pending"},
            ],
        }],
        pending_actions=[],
    )

    assert "semantically matched question_id" in error


def test_correction_cannot_be_routed_as_approval_resolution():
    from mac_system.feishu.handlers import _conversation_state_contract_error

    error = _conversation_state_contract_error(
        {
            "intent": "change_scope",
            "commitment": "authorized",
            "target": {"kind": "approval", "id": "appr_old"},
            "proposed_action": {
                "type": "propose_approval_resolution",
                "approval_decision": "reject",
            },
        },
        approvals=[{
            "id": "appr_old",
            "type": "confirmation",
            "options": [{"action": "reject"}],
        }],
        pending_actions=[],
    )

    assert "cannot resolve, confirm, reject" in error


def test_route_project_input_option_must_belong_to_target_question():
    from mac_system.feishu.handlers import _conversation_state_contract_error

    approvals = [{
        "id": "appr_input",
        "type": "input",
        "input_questions": [{
            "question_id": "q_manual_failure",
            "status": "pending",
            "options": [
                {"key": "verified_pass", "label": "完整通过"},
                {"key": "verified_fail", "label": "验证失败"},
            ],
        }],
    }]
    error = _conversation_state_contract_error(
        {
            "intent": "correct",
            "proposed_action": {
                "type": "route_project_input",
                "question_id": "q_manual_failure",
                "option_key": "invented_failure",
            },
        },
        approvals=approvals,
        pending_actions=[],
    )
    valid = _conversation_state_contract_error(
        {
            "intent": "correct",
            "proposed_action": {
                "type": "route_project_input",
                "question_id": "q_manual_failure",
                "option_key": "verified_fail",
            },
        },
        approvals=approvals,
        pending_actions=[],
    )

    assert "option_key must match" in error
    assert valid is None


async def test_invalid_capability_reference_retries_before_project_mutation(
    feishu_ctx,
):
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import FeedbackPacket, ProjectContextRecord

    chat_id = "oc_invalid_capability_retry"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="invalid-capability",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    missing_source = "/definitely/missing/provider.env"
    feishu_ctx.orchestrator = _FakeOrchestratorWithRetry([
        {
            "success": True,
            "reply": "已经配置并继续编码。",
            "intent": "correct",
            "effect": "workflow_control",
            "commitment": "authorized",
            "needs_clarification": False,
            "target": {"kind": "project", "id": project.id},
            "proposed_action": {
                "type": "route_project_input",
                "target_stage": "coding",
                "capability_refs": [{
                    "source_file": missing_source,
                    "env_bindings": {"API_KEY": "API_KEY"},
                }],
            },
            "updated_summary": "已配置",
        },
        {
            "success": True,
            "reply": "找不到可用的凭证来源，请提供有效位置。",
            "intent": "provide_information",
            "effect": "read_only",
            "commitment": "exploring",
            "needs_clarification": False,
            "proposed_action": {"type": "answer"},
            "updated_summary": "凭证来源不可用",
        },
    ])

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_invalid_capability",
            text="把那份 API key 给 coder 用",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert len(feishu_ctx.orchestrator.agent_calls) == 2
    assert "source_file is unavailable" in feishu_ctx.orchestrator.agent_calls[
        1
    ]["kwargs"]["extra_variables"]["retry_feedback"]
    assert feishu_ctx.service.require_project(project.id).current_state == (
        "CODING_CONFIRM_PENDING"
    )
    assert feishu_ctx.service.session.exec(
        select(FeedbackPacket).where(FeedbackPacket.project_id == project.id)
    ).all() == []
    assert feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(
            ProjectContextRecord.project_id == project.id
        )
    ).all() == []


async def test_workspace_inspection_persists_structured_evidence(feishu_ctx, tmp_path):
    """Completed inspection evidence is retained for later execution turns."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_inspect_evidence"
    feishu_ctx.chat_type = "p2p"
    workspace = tmp_path / "coordinator-workspace"
    source = workspace / "src" / "mac_system" / "feishu" / "handlers.py"
    source.parent.mkdir(parents=True)
    source.write_text("# evidence", encoding="utf-8")
    feishu_ctx.service.bind_directory_to_chat(
        feishu_ctx.chat_id, str(workspace), "ou_1"
    )
    evidence = {
        "files_read": ["src/mac_system/feishu/handlers.py"],
        "summary": "会话历史保存了协调器预测回复。",
        "findings": [{
            "title": "记忆与执行结果可能不一致",
            "evidence": "动作结束后直接保存 reply 字段。",
            "severity": "high",
            "recommendation": "保存实际发送内容。",
        }],
    }
    feishu_ctx.orchestrator = _FakeOrchestratorWithRetry([{
        "success": True,
        "reply": "正在检查。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {
            "type": "request_context",
            "repository_path": str(workspace.resolve()),
            "context_capabilities": ["workspace_read"],
            "read_targets": ["src/mac_system/feishu/handlers.py"],
        },
    }, {
        "success": True,
        "reply": "正在选择证据。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {
            "type": "request_context",
            "repository_path": str(workspace.resolve()),
            "context_capabilities": ["workspace_read"],
            "read_targets": ["src/mac_system/feishu/handlers.py"],
        },
    }, {
        "success": True,
        "reply": "检查完成：会话记忆可能与实际执行结果不一致。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "inspect_workspace"},
        "inspection": evidence,
        "_runtime": {
            "tool_calls": [{
                "name": "Read",
                "input": {"file_path": "src/mac_system/feishu/handlers.py"},
            }]
        },
        "updated_summary": "完成协调器记忆检查",
    }])

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_inspect_evidence",
            text="检查协调器记忆", is_command=False, chat_type="p2p",
        ),
    )

    conversation = feishu_ctx.service.get_or_create_conversation_session(
        "oc_inspect_evidence"
    )
    assert "检查完成" in conversation.recent_turns[-1]["content"]
    assert conversation.summary == evidence["summary"]
    assert conversation.latest_artifact["evidence"] == evidence


async def test_conversation_clarification_uses_choice_card_and_resumes(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    feishu_ctx.chat_id = "oc_clarification"
    feishu_ctx.chat_type = "group"
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "请选择技术路线",
        "intent": "compare",
        "effect": "none",
        "commitment": "considering",
        "needs_clarification": True,
        "clarification": {
            "question": "后端使用哪种技术？",
            "reason": "影响部署方式",
            "options": [
                {"key": "fastapi", "label": "FastAPI", "recommended": True},
                {"key": "node", "label": "Node.js", "recommended": False},
            ],
            "allow_free_text": True,
        },
        "proposed_action": {"type": "request_clarification"},
        "updated_summary": "等待选择后端技术",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_choose", text="帮我选后端",
            is_command=False, is_at_bot=True,
            chat_id="oc_clarification", chat_type="group",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.action_type == "clarification"
    assert any("conversation_clarification" in str(item[2]) for item in feishu_ctx.bridge.sent)

    fake.agent_output = {
        "success": True,
        "reply": "已按 FastAPI 继续细化。",
        "intent": "provide_information",
        "effect": "none",
        "commitment": "preferred",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "已选择 FastAPI",
    }
    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1", approval_id="", action="conversation_clarification",
            state="", option_key="fastapi", decision_id=None,
            extras={"pending_action_id": pending.id},
            chat_id="oc_clarification", chat_type="group",
            message_id="msg_2",
        ),
    )

    feishu_ctx.service.session.refresh(pending)
    assert pending.status == "resolved"
    conversation = feishu_ctx.service.get_or_create_conversation_session("oc_clarification")
    assert any("FastAPI" in turn["content"] for turn in conversation.recent_turns)
    assert not any(item[0] == "text" for item in feishu_ctx.bridge.sent)
    updated_card = next(
        card for message_id, card in feishu_ctx.bridge.updated if message_id == "msg_2"
    )
    assert "已完成选择" in str(updated_card)
    assert "FastAPI" in str(updated_card)
    assert "Node.js" in str(updated_card)
    assert "✅" in str(updated_card)
    assert "已按 FastAPI" in str(feishu_ctx.bridge.sent[-1][2])


async def test_request_clarification_action_renders_choice_card_even_without_flag(
    feishu_ctx,
):
    """The coordinator may set proposed_action.type=request_clarification without
    needs_clarification=true; the handler should still render a choice card.
    """
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction

    feishu_ctx.chat_id = "oc_clarification2"
    feishu_ctx.chat_type = "group"
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "请确认方案",
        "intent": "compare",
        "effect": "none",
        "commitment": "considering",
        "needs_clarification": False,
        "clarification": {
            "question": "是否采用该方案？",
            "reason": "需要你的确认",
            "options": [
                {"key": "yes", "label": "确认", "recommended": True},
                {"key": "no", "label": "再改改", "recommended": False},
            ],
            "allow_free_text": True,
        },
        "proposed_action": {"type": "request_clarification"},
        "updated_summary": "等待用户确认方案",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_choose2", text="这个方案可以吗",
            is_command=False, is_at_bot=True,
            chat_id="oc_clarification2", chat_type="group",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.action_type == "clarification"
    assert any("conversation_clarification" in str(item[2]) for item in feishu_ctx.bridge.sent)
    assert not any(item[0] == "text" for item in feishu_ctx.bridge.sent)


async def test_conversation_coordinator_limits_recent_turn_context(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    for index in range(12):
        feishu_ctx.service.append_conversation_turn(
            "oc_context_limit", role="user", content=f"历史消息 {index}"
        )
    fake = _FakeOrchestratorWithBindDir({
        "success": True, "reply": "收到", "intent": "discuss", "effect": "none",
        "commitment": "exploring", "needs_clarification": False,
        "proposed_action": {"type": "answer"}, "updated_summary": "继续讨论",
    })
    feishu_ctx.orchestrator = fake
    feishu_ctx.chat_id = "oc_context_limit"

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_latest", text="继续",
            is_command=False, chat_type="p2p",
        ),
    )

    recent = json.loads(fake.agent_calls[0]["extra_variables"]["recent_turns"])
    assert len(recent) == 8
    assert recent[-1]["content"] == "继续"


async def test_completed_project_followup_starts_new_run_in_same_workspace(feishu_ctx):
    """A terminal project remains a follow-up target, not a rollback target."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    chat_id = "oc_terminal_followup"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    original = await feishu_ctx.service.create_project(
        name="todo",
        task_type="feature",
        description="build todo",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    original.status = "completed"
    original.current_state = "COMPLETED"
    original.context = {
        **original.context,
        "orchestration_mode": "agent_loop",
        "runtime_capability_grants": [{
            "agent": "coder",
            "state": "CODING",
            "question_id": "q_runtime",
            "capabilities": ["HostAutomation"],
        }],
    }
    feishu_ctx.service.session.add(original)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="待办项目已完成",
        active_project_id=original.id,
        update_active_project=True,
    )

    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已开始修复并重新验收。",
        "intent": "correct",
        "effect": "workspace_write",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "start_delivery",
            "base_project_id": original.id,
            "reference_mode": "none",
            "task_type": "bug_fix",
            "description": "修复真实界面验收发现的两个缺陷",
            "scope": ["修复交互", "修复移动布局", "重新黑盒验收"],
            "out_of_scope": [],
            "permissions": ["workspace_write", "local_diagnostics"],
            "acceptance_criteria": ["真实入口按 Enter 可新增", "移动视口正常"],
            "decisions": [],
        },
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_followup",
            text="请在刚完成的项目上修复这两个问题",
            is_command=False,
            chat_id=chat_id,
            chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.session.exec(
        select(PendingConversationAction)
    ).all() == []
    projects = list(feishu_ctx.service.session.exec(select(Project)).all())
    assert len(projects) == 2
    followup = next(project for project in projects if project.id != original.id)
    assert followup.task_type == "bug_fix"
    assert followup.workspace_path == original.workspace_path
    assert followup.bound_directory == original.workspace_path
    assert followup.context["base_project_id"] == original.id
    assert followup.context["runtime_capability_grants"] == [
        {
            "agent": "coder",
            "state": "CODING",
            "question_id": "q_runtime",
            "capabilities": ["HostAutomation"],
        }
    ]
    assert fake.ticked == [followup.id]
    prompt_vars = fake.agent_calls[0]["extra_variables"]
    assert json.loads(prompt_vars["active_project"]) is None
    assert json.loads(prompt_vars["recent_project"])["id"] == original.id
    refreshed = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert refreshed.active_project_id == followup.id
    assert any("原项目目录" in str(item[2]) for item in feishu_ctx.bridge.sent)


async def test_conversation_start_delivery_requires_card_and_is_idempotent(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction, Project

    feishu_ctx.chat_id = "oc_delivery"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我整理了执行范围，请确认后开始。",
        "intent": "authorize_execution",
        "effect": "workspace_write",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "none",
            "task_type": "bug_fix",
            "description": "修复审批重复点击",
            "scope": ["重复点击返回已处理", "补充回归测试"],
            "out_of_scope": ["重构所有卡片"],
            "permissions": ["workspace_write", "local_diagnostics"],
            "acceptance_criteria": ["重复点击只推进一次"],
            "decisions": [{
                "key": "approval.duplicate_click",
                "question": "重复点击如何处理？",
                "answer": "只推进一次并返回已处理",
                "status": "confirmed",
            }],
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_fix",
            text="OK，你帮忙修复",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_delivery",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    pending = feishu_ctx.service.session.exec(
        select(PendingConversationAction)
    ).one()
    assert pending.status == "pending"
    assert any(item[0] == "interactive" for item in feishu_ctx.bridge.sent)

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "confirm"},
        chat_id="oc_delivery",
        chat_type="group",
    )
    await handle_card_action(feishu_ctx, action)

    projects = feishu_ctx.service.session.exec(select(Project)).all()
    assert len(projects) == 1
    feishu_ctx.service.session.refresh(pending)
    assert pending.status == "resolved"
    assert pending.target_project_id == projects[0].id
    confirmed_brief = projects[0].context["confirmed_brief"]
    assert confirmed_brief["scope"] == ["重复点击返回已处理", "补充回归测试"]
    assert confirmed_brief["out_of_scope"] == ["重构所有卡片"]
    assert confirmed_brief["acceptance_criteria"] == ["重复点击只推进一次"]
    assert confirmed_brief["decisions"][0]["key"] == "approval.duplicate_click"
    assert confirmed_brief["source_session_id"] == pending.session_id
    assert confirmed_brief["recent_turns"]
    assert any(
        f"已创建项目“{projects[0].name}”" in str(item[2])
        for item in feishu_ctx.bridge.sent
    )

    await handle_card_action(feishu_ctx, action)
    assert len(feishu_ctx.service.session.exec(select(Project)).all()) == 1
    assert "已经处理" in str(feishu_ctx.bridge.sent[-1][2])


async def test_resolve_pending_action_executes_before_recording_result(feishu_ctx):
    """A coordinator success prediction must not be sent or stored before execution."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_resolve_actual_result"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    pending = feishu_ctx.service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="unsupported_for_regression",
        payload={},
        source_message_ids=["om_source"],
        idempotency_key="resolve-actual-result",
        created_by="ou_1",
    )
    false_success = "已确认并成功开始执行。"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": false_success,
        "intent": "authorize_execution",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "pending_action", "id": pending.id},
        "proposed_action": {"type": "resolve_pending_action"},
        "updated_summary": false_success,
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_confirm_pending",
            text="确认", is_command=False, chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(pending)
    assert pending.status == "pending"
    assert not any(false_success in str(item) for item in feishu_ctx.bridge.sent)
    refreshed = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert false_success not in refreshed.recent_turns[-1]["content"]
    assert "实际处理状态：pending" in refreshed.recent_turns[-1]["content"]
    assert refreshed.summary == refreshed.recent_turns[-1]["content"]


async def test_coordinator_can_resolve_specific_natural_language_clarification(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_resolve_clarification"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    pending = feishu_ctx.service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="clarification",
        payload={"question": "使用 SQLite 还是 PostgreSQL？"},
        source_message_ids=["om_question"],
        idempotency_key="resolve-clarification",
        created_by="ou_1",
        expire_existing=False,
    )
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已记录选择 SQLite。",
        "intent": "provide_information",
        "effect": "conversation_write",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "pending_action", "id": pending.id},
        "proposed_action": {"type": "resolve_pending_action"},
        "updated_summary": "用户选择 SQLite",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_answer",
            text="第一个问题选 SQLite",
            is_command=False,
            chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(pending)
    assert pending.status == "resolved"
    assert any("已记录选择 SQLite" in str(item) for item in feishu_ctx.bridge.sent)


async def test_conversation_propose_project_control_pause_creates_pending_action(feishu_ctx):
    """Coordinator-proposed pause must route through a card-confirmed
    PendingConversationAction; confirming flips project.status to 'paused'.
    """
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_pause"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我会先暂停这个项目。",
        "intent": "control",
        "effect": "none",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {"type": "propose_project_control", "control_action": "pause"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_pause",
            text="先暂停一下",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pendings = feishu_ctx.service.session.exec(select(PendingConversationAction)).all()
    assert len(pendings) == 1
    pending = pendings[0]
    assert pending.action_type == "project_control"
    assert pending.payload["control_action"] == "pause"
    assert pending.payload["target_project_id"] == project.id
    assert any(item[0] == "interactive" for item in feishu_ctx.bridge.sent)

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "confirm"},
        chat_id=chat_id,
        chat_type="p2p",
    )
    await handle_card_action(feishu_ctx, action)

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.status == "paused"
    assert pending.status == "resolved"
    assert pending.target_project_id == project.id


async def test_conversation_propose_project_control_resume_continues_project(feishu_ctx):
    """A paused project remains visible to the coordinator and resumes with a tick."""
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_resume"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.commit()
    await feishu_ctx.service.pause_project(project.id, triggered_by="test")
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目已暂停",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "恢复项目并继续执行。",
        "intent": "control",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_project_control",
            "control_action": "resume",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_resume", text="继续项目",
            is_command=False, chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    active_context = json.loads(
        feishu_ctx.orchestrator.agent_calls[0]["extra_variables"]["active_project"]
    )
    assert active_context["status"] == "paused"

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1", approval_id="", action="conversation_action", state="",
            decision_id=None, option_key=None,
            extras={"pending_action_id": pending.id, "decision": "confirm"},
            chat_id=chat_id, chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.status == "active"
    assert pending.status == "resolved"
    assert feishu_ctx.orchestrator.ticked == [project.id, project.id]


async def test_conversation_propose_project_control_cancel_cancels_project(feishu_ctx):
    """Coordinator-proposed cancel must terminate the project once confirmed."""
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_cancel"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我来取消这个项目。",
        "intent": "control",
        "effect": "none",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {"type": "propose_project_control", "control_action": "cancel"},
    })
    feishu_ctx.orchestrator.project_service = feishu_ctx.service

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_cancel",
            text="取消这个项目",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.action_type == "project_control"
    assert pending.payload["control_action"] == "cancel"

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "confirm"},
        chat_id=chat_id,
        chat_type="p2p",
    )
    await handle_card_action(feishu_ctx, action)

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.status == "cancelled"
    assert pending.status == "resolved"
    assert feishu_ctx.orchestrator.cancelled == [
        (project.id, "用户通过对话确认卡取消")
    ]
    refreshed_conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert refreshed_conversation.active_project_id is None


async def test_conversation_propose_project_control_reject_keeps_project_active(feishu_ctx):
    """Rejecting the control card must leave the project untouched."""
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_reject"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我来暂停这个项目。",
        "intent": "control",
        "effect": "none",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {"type": "propose_project_control", "control_action": "pause"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_reject",
            text="暂停一下",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "reject"},
        chat_id=chat_id,
        chat_type="p2p",
    )
    await handle_card_action(feishu_ctx, action)

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.status == "active"
    assert pending.status == "rejected"


async def test_conversation_propose_project_control_without_active_project_falls_back(feishu_ctx):
    """If the session has no active project, propose_project_control must not
    create a PendingConversationAction and must fall back to a plain reply.
    """
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    chat_id = "oc_control_no_active"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "当前没有可操作的项目。",
        "intent": "control",
        "effect": "none",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "propose_project_control", "control_action": "pause"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_no_active",
            text="暂停项目",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []
    assert feishu_ctx.service.session.exec(select(Project)).all() == []


async def test_conversation_propose_project_control_rollback_to_requirements(feishu_ctx):
    """Coordinator-proposed rollback must call service.rollback_project and
    move the project back to the target stage's execution state.
    """
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_rollback"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    # Simulate a project already in CODING so rollback target is valid.
    project.current_state = "CODING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目编码中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我来把项目回滚到需求阶段。",
        "intent": "change_scope",
        "effect": "workflow_control",
        "commitment": "proposed",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_project_control",
            "control_action": "rollback",
            "target_stage": "requirements",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_rollback",
            text="改一下需求",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.action_type == "project_control"
    assert pending.payload["control_action"] == "rollback"
    assert pending.payload["target_stage"] == "requirements"

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "confirm"},
        chat_id=chat_id,
        chat_type="p2p",
    )
    await handle_card_action(feishu_ctx, action)

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.current_state == "PLANNING_REQUIREMENTS"
    assert project.previous_state == "CODING"
    assert pending.status == "resolved"
    assert feishu_ctx.orchestrator.ticked == [project.id, project.id]


async def test_conversation_propose_project_control_rollback_missing_target(feishu_ctx):
    """If the coordinator proposes rollback without target_stage, the handler
    must NOT create a PendingConversationAction; it should ask the user to
    specify which stage.
    """
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_rollback_missing"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目编码中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我需要更多信息。",
        "intent": "change_scope",
        "effect": "workflow_control",
        "commitment": "ambiguous",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_project_control",
            "control_action": "rollback",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_rollback_amb",
            text="我觉得这个不太对",
            is_command=False,
            chat_type="p2p",
        ),
    )

    # No pending action should be created; we should just have sent a reply.
    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []
    feishu_ctx.service.session.refresh(project)
    assert project.current_state == "CODING"


async def test_conversation_project_control_rejects_forward_rollback(feishu_ctx):
    """A rollback proposal cannot skip forward over uncompleted stages."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_forward_rollback"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "PLANNING_REQUIREMENTS"
    feishu_ctx.service.session.commit()

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "回滚到测试阶段。",
        "intent": "change_scope",
        "effect": "workflow_control",
        "commitment": "proposed",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_project_control",
            "control_action": "rollback",
            "target_stage": "testing",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_forward_rollback",
            text="直接去测试", is_command=False, chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    assert any("只能回滚到当前阶段之前" in str(item[2]) for item in feishu_ctx.bridge.sent)


async def test_conversation_propose_project_control_rollback_reject_keeps_state(feishu_ctx):
    """Rejecting the rollback card must leave project.current_state unchanged."""
    from mac_system.feishu.handlers import handle_card_action, handle_text_message
    from mac_system.models import PendingConversationAction

    chat_id = "oc_control_rollback_reject"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目编码中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我来把项目回滚到需求阶段。",
        "intent": "change_scope",
        "effect": "workflow_control",
        "commitment": "proposed",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_project_control",
            "control_action": "rollback",
            "target_stage": "requirements",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_rollback_rej",
            text="改一下需求",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="conversation_action",
        state="",
        decision_id=None,
        option_key=None,
        extras={"pending_action_id": pending.id, "decision": "reject"},
        chat_id=chat_id,
        chat_type="p2p",
    )
    await handle_card_action(feishu_ctx, action)

    feishu_ctx.service.session.refresh(project)
    feishu_ctx.service.session.refresh(pending)
    assert project.current_state == "CODING"
    assert pending.status == "rejected"


async def test_conversation_view_artifact_requirements_returns_content(feishu_ctx):
    """When the user asks to see the requirements doc, the handler must read
    REQUIREMENTS.json from the project's .mac/ directory and return its content.
    """
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_view_requirements"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    artifact = {
        "version": 1,
        "round": 1,
        "status": "confirmed",
        "objective": "实现一个 config-validator CLI",
        "scope": ["validate 子命令", "yaml 解析"],
        "acceptance_criteria": ["validate 能返回非零退出码"],
    }
    artifact_path = (
        Path(project.workspace_path) / ".mac" / "REQUIREMENTS.json"
    )
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False), encoding="utf-8"
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "这是当前的需求文档。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {
            "type": "view_artifact",
            "artifact_kind": "requirements",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_view_req",
            text="看一下需求",
            is_command=False,
            chat_type="p2p",
        ),
    )

    # The reply is sent via send_content_card → logged as "interactive".
    interactives = [item for item in feishu_ctx.bridge.sent if item[0] == "interactive"]
    assert interactives, "expected at least one interactive reply"
    last_card_text = str(interactives[-1][2])
    assert "config-validator CLI" in last_card_text
    assert "REQUIREMENTS.json" in last_card_text
    assert "validate 子命令" in last_card_text


async def test_conversation_view_artifact_missing_file_reports_not_produced(feishu_ctx):
    """When the artifact file does not exist, reply 'not yet produced' rather
    than crashing or making the coordinator guess paths.
    """
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_view_missing"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我看一下。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {
            "type": "view_artifact",
            "artifact_kind": "architecture",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_view_missing",
            text="看一下方案",
            is_command=False,
            chat_type="p2p",
        ),
    )

    replies = [item for item in feishu_ctx.bridge.sent if item[0] == "interactive"]
    assert replies, "expected at least one interactive reply"
    last_text = str(replies[-1][2])
    assert "ARCHITECTURE.json" in last_text
    assert "还未完成" in last_text or "没有产出" in last_text


async def test_conversation_view_artifact_without_active_project_falls_back(feishu_ctx):
    """No active project + view_artifact → no file read, plain fallback reply."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_view_no_project"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "当前没有项目。",
        "intent": "inspect",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {
            "type": "view_artifact",
            "artifact_kind": "requirements",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_view_no_proj",
            text="看一下需求",
            is_command=False,
            chat_type="p2p",
        ),
    )

    replies = [item for item in feishu_ctx.bridge.sent if item[0] == "interactive"]
    assert replies, "expected at least one interactive reply"
    last_text = str(replies[-1][2])
    assert "当前没有可查看的活跃项目" in last_text or "没有项目" in last_text


async def test_robustness_propose_start_delivery_blocked_with_active_project(feishu_ctx):
    """A'5: coordinator must not spawn a parallel project while one is active.
    Even if it tries (prompt slip), the handler must reject the action and
    leave the existing project untouched.
    """
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    chat_id = "oc_robust_active_block"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="existing", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目运行中",
        active_project_id=project.id,
        update_active_project=True,
    )

    # Coordinator tries to start a new delivery despite active project.
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "这是整理后的最小范围方案。",
        "intent": "authorize_execution",
        "effect": "workspace_write",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "propose_start_delivery",
            "reference_mode": "none",
            "task_type": "feature",
            "description": "一个完全不同的新项目",
            "scope": ["新功能 X"],
            "out_of_scope": [],
            "permissions": ["workspace_write"],
            "acceptance_criteria": ["X 工作"],
            "decisions": [],
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_new_req",
            text="再帮我做一个别的功能",
            is_command=False,
            chat_type="p2p",
        ),
    )

    # No PendingConversationAction for start_delivery should exist.
    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []
    # Only the original project exists.
    projects = feishu_ctx.service.session.exec(select(Project)).all()
    assert len(projects) == 1
    assert projects[0].id == project.id
    # Reply should tell the user there's an active project.
    interactives = [item for item in feishu_ctx.bridge.sent if item[0] == "interactive"]
    assert interactives
    last_text = str(interactives[-1][2])
    assert project.name in last_text
    refreshed_conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    actual_reply = refreshed_conversation.recent_turns[-1]["content"]
    assert project.name in actual_reply
    assert "这是整理后的最小范围方案" not in actual_reply
    assert refreshed_conversation.summary == actual_reply


async def test_robustness_free_text_during_approval_does_not_auto_resolve(feishu_ctx):
    """A'1: while a confirmation approval is pending, an ambiguous free-text
    message must NOT auto-resolve the approval. The approval stays pending
    and the user is asked to pick an option.
    """
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_robust_pending_approval"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="等待方案确认",
        active_project_id=project.id,
        update_active_project=True,
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="ARCHITECTURE_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认方案",
        options=[{"action": "confirm", "label": "确认"}],
    )

    # Coordinator responds with an ambiguous answer (NOT approval_resolution).
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我需要更多信息才能判断。",
        "intent": "discuss",
        "effect": "none",
        "commitment": "ambiguous",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_during_approval",
            text="这个方案你觉得怎么样",
            is_command=False,
            chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(approval)
    feishu_ctx.service.session.refresh(project)
    # Approval must still be pending — no auto-resolution.
    assert approval.status == "pending"
    # Project state unchanged.
    assert project.current_state == "ARCHITECTURE_CONFIRM_PENDING"


async def test_robustness_rework_suibian_does_not_auto_decide(feishu_ctx):
    """A'3: '随便' during REWORK must not trigger any approval resolution.
    The approval stays pending and the project stays in REWORK.
    """
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_robust_rework_suibian"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    project.current_state = "REWORK"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="等待 rework 选择",
        active_project_id=project.id,
        update_active_project=True,
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REWORK",
        approval_type="confirmation",
        question="本轮未通过，请选择如何处理",
        options=[
            {"action": "to_coding", "label": "重新编码"},
            {"action": "to_requirements", "label": "重新明确需求"},
        ],
    )

    # Coordinator (correctly) treats '随便' as ambiguous and asks for clarification.
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "请告诉我要回到哪个阶段。",
        "intent": "discuss",
        "effect": "none",
        "commitment": "ambiguous",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_suibian",
            text="随便",
            is_command=False,
            chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(approval)
    feishu_ctx.service.session.refresh(project)
    # Approval NOT auto-resolved.
    assert approval.status == "pending"
    # Project still in REWORK.
    assert project.current_state == "REWORK"


async def test_robustness_gibberish_free_text_is_safe_no_op(feishu_ctx):
    """A'6: gibberish free text with active project must not corrupt state.
    Coordinator responds with answer; handler just relays; nothing else changes.
    """
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import Approval, PendingConversationAction

    chat_id = "oc_robust_gibberish"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d", feishu_chat_id=chat_id
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目编码中",
        active_project_id=project.id,
        update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我不太理解这句话，能换种方式描述吗？",
        "intent": "unknown",
        "effect": "none",
        "commitment": "ambiguous",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_gibberish",
            text="asdkfjqwieorzxcv",
            is_command=False,
            chat_type="p2p",
        ),
    )

    feishu_ctx.service.session.refresh(project)
    assert project.current_state == "CODING"
    assert project.status == "active"
    # No pending action / approval created.
    assert feishu_ctx.service.session.exec(select(PendingConversationAction)).all() == []
    assert feishu_ctx.service.session.exec(select(Approval)).all() == []


async def test_invalid_coordinator_contract_never_fabricates_action(feishu_ctx):
    """Malformed coordinator output must degrade identically for every
    phrasing: relay result_text as a plain answer, never fabricate a
    pending execution action."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    phrasings = [
        "按刚才的最小范围开始开发",  # legacy "execution keyword" phrasing
        "可以动手了吗",  # execution intent without any keyword
        "随便聊聊",
    ]
    for i, text in enumerate(phrasings):
        chat_id = f"oc_safe_fallback_{i}"
        feishu_ctx.chat_id = chat_id
        feishu_ctx.chat_type = "group"
        feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
            "success": True,
            "result_text": "我会开始处理。",
        })

        await handle_text_message(
            feishu_ctx,
            TextMessage(
                open_id="ou_1",
                message_id=f"om_safe_fallback_{i}",
                text=text,
                is_command=False,
                is_at_bot=True,
                chat_id=chat_id,
                chat_type="group",
            ),
        )

        assert feishu_ctx.service.session.exec(select(Project)).all() == [], text
        assert (
            feishu_ctx.service.session.exec(select(PendingConversationAction)).all()
            == []
        ), text
        assert "我会开始处理。" in str(feishu_ctx.bridge.sent[-1]), text


async def test_failed_coordinator_safe_fallback_for_any_phrasing(feishu_ctx):
    """Coordinator failure must degrade to the same safe retry message for
    every phrasing — never fabricate a pending execution action from a
    failed parse."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    phrasings = [
        "按刚才的最小范围开始开发",  # legacy "execution keyword" phrasing
        "可以动手了吗",  # execution intent without any keyword
        "随便聊聊",
    ]
    for i, text in enumerate(phrasings):
        chat_id = f"oc_failed_fallback_{i}"
        feishu_ctx.chat_id = chat_id
        feishu_ctx.chat_type = "group"
        feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
            "success": False,
            "error": "No result event received before timeout",
        })

        await handle_text_message(
            feishu_ctx,
            TextMessage(
                open_id="ou_1",
                message_id=f"om_failed_fallback_{i}",
                text=text,
                is_command=False,
                is_at_bot=True,
                chat_id=chat_id,
                chat_type="group",
            ),
        )

        assert feishu_ctx.service.session.exec(select(Project)).all() == [], text
        assert (
            feishu_ctx.service.session.exec(select(PendingConversationAction)).all()
            == []
        ), text

        # Technical provider/schema details remain in logs and never leak to
        # user-facing cards.
        final_card_text = str(feishu_ctx.bridge.sent[-1])
        assert "No result event received before timeout" not in final_card_text, text
        assert "没有执行任何操作" in final_card_text, text
        assert "结果已整理" not in final_card_text, text

        # The progress card should reflect the failure before the safe fallback
        # replaces it with the actual user-facing result.
        updated_titles = [
            item[1]["header"]["title"]["content"]
            for item in feishu_ctx.bridge.updated
        ]
        assert "处理失败" in updated_titles, text
        assert "No result event received before timeout" not in str(
            feishu_ctx.bridge.updated
        ), text


async def test_free_text_during_running_agent_always_nonblocking_question(feishu_ctx):
    """Free text while an agent is running is always recorded as a
    non-blocking question — no keyword-based kind inference."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingUserInput

    chat_id = "oc_running_kind"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        summary="项目编码中",
        active_project_id=project.id,
        update_active_project=True,
    )
    task = await feishu_ctx.service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await feishu_ctx.service.start_task(task.id)

    phrasings = [
        "改天再看吧",  # legacy classifier misfired: 改 -> "correction" (blocking)
        "这个不要急",  # legacy classifier misfired: 不要 -> "scope_change" (blocking)
        "我觉得这里设计有问题",  # real correction content, zero keyword overlap
    ]
    for i, text in enumerate(phrasings):
        await handle_text_message(
            feishu_ctx,
            TextMessage(
                open_id="ou_1",
                message_id=f"om_kind_{i}",
                text=text,
                is_command=False,
                chat_type="p2p",
            ),
        )

    inputs = feishu_ctx.service.session.exec(
        select(PendingUserInput).where(PendingUserInput.project_id == project.id)
    ).all()
    assert len(inputs) == len(phrasings)
    assert {inp.kind for inp in inputs} == {"question"}


async def test_correction_during_running_agent_interrupts_stale_runtime(feishu_ctx):
    """A coordinator-classified correction should not wait for stale work."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingUserInput

    chat_id = "oc_running_correction"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    task = await feishu_ctx.service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await feishu_ctx.service.start_task(task.id)
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已记录更正。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "project", "id": project.id},
        "proposed_action": {"type": "route_project_input", "target_stage": "coding"},
        "updated_summary": "修正实现策略",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_running_correction",
            text="当前实现会留下无法取消的后台线程，请改成可终止方案",
            is_command=False,
            chat_type="p2p",
        ),
    )

    pending = feishu_ctx.service.session.exec(
        select(PendingUserInput).where(PendingUserInput.project_id == project.id)
    ).one()
    assert pending.kind == "correction"
    assert pending.recommended_rework_target == "coding"
    assert fake.interrupted == [project.id]
    assert "中断正在使用旧上下文" in str(feishu_ctx.bridge.sent[-1])


async def test_coordinator_correction_at_confirmation_restarts_stage(feishu_ctx):
    """A confirmed correction is a ChangeRequest, not inert chat text."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import FeedbackPacket, ProjectContextRecord

    chat_id = "oc_confirmation_change"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="confirmation",
        question="确认代码？",
        options=[{"action": "confirm", "label": "确认"}],
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "我会把这个问题交给 coder 修改。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {"type": "route_project_input", "target_stage": "coding"},
        "updated_summary": "用户指出代码问题",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_change_code",
            text="这里的代码有问题，需要按刚才说的方式修改",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "CODING"
    assert fake.ticked == [project.id]
    feedback = feishu_ctx.service.session.exec(
        select(FeedbackPacket).where(FeedbackPacket.project_id == project.id)
    ).all()
    assert len(feedback) == 1 and feedback[0].blocking is True
    facts = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(ProjectContextRecord.project_id == project.id)
    ).all()
    assert any(item.record_key.startswith("user.change.") for item in facts)


async def test_acceptance_evidence_correction_reruns_evaluator_without_coder(
    feishu_ctx,
):
    """Acceptance-report corrections stay at acceptance instead of changing code."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import FeedbackPacket, ProjectContextRecord

    chat_id = "oc_acceptance_evidence_change"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "ACCEPTANCE_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state=project.current_state,
        approval_type="confirmation",
        question="确认验收？",
        options=[{"action": "confirm", "label": "确认"}],
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "重新核对验收证据。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "acceptance",
        },
        "updated_summary": "验收证据需要纠正",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_acceptance_evidence",
            text="服务端通过，但没有实体手机，不能判手机验收通过",
            is_command=False,
            chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "ACCEPTANCE_PENDING"
    assert feishu_ctx.service.get_approval(approval.id).status == "expired"
    assert fake.ticked == [project.id]
    feedback = feishu_ctx.service.session.exec(
        select(FeedbackPacket).where(FeedbackPacket.project_id == project.id)
    ).all()
    assert feedback == []
    facts = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(ProjectContextRecord.project_id == project.id)
    ).all()
    assert any(
        item.record_key.startswith("acceptance.evidence_update.")
        and item.value.get("applies_to") == ["acceptance_evaluator", "ACCEPTANCE_PENDING"]
        for item in facts
    )


def test_acceptance_retry_reuses_fully_passing_executor_for_evaluator_failure():
    from mac_system.feishu.handlers import _acceptance_retry_needs_executor_rerun

    assert _acceptance_retry_needs_executor_rerun(
        {
            "result": "pass",
            "criteria": [
                {"id": "AC-001", "result": "pass"},
                {"id": "AC-002", "result": "pass"},
            ],
        },
        {"result": "fail", "blocking_conditions": ["missing embedded evidence"]},
        evaluator_ran_after_executor=True,
    ) is False


@pytest.mark.parametrize(
    ("executor_output", "evaluator_output", "evaluator_ran_after_executor"),
    [
        ({"result": "fail", "criteria": []}, {"result": "fail"}, True),
        ({"result": "pass", "criteria": []}, {"result": "fail"}, True),
        (
            {"result": "pass", "criteria": [{"id": "AC-001", "result": "fail"}]},
            {"result": "fail"},
            True,
        ),
        (
            {"result": "pass", "criteria": [{"id": "AC-001", "result": "pass"}]},
            {},
            True,
        ),
        (
            {"result": "pass", "criteria": [{"id": "AC-001", "result": "pass"}]},
            {"result": "fail"},
            False,
        ),
    ],
)
def test_acceptance_retry_reruns_executor_without_evaluator_only_failure(
    executor_output,
    evaluator_output,
    evaluator_ran_after_executor,
):
    from mac_system.feishu.handlers import _acceptance_retry_needs_executor_rerun

    assert _acceptance_retry_needs_executor_rerun(
        executor_output,
        evaluator_output,
        evaluator_ran_after_executor=evaluator_ran_after_executor,
    ) is True


async def test_pending_capability_choice_blocks_capability_side_effect(feishu_ctx, tmp_path):
    """An unresolved provider card and an applied capability cannot coexist."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import ProjectContextRecord

    chat_id = "oc_capability_choice_gate"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, active_project_id=project.id, update_active_project=True,
    )
    feishu_ctx.service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="clarification",
        payload={
            "subject_key": "runtime.provider_choice",
            "question": "Which provider?",
            "options": [{"key": "deepseek", "label": "适配 DeepSeek"}],
        },
        source_message_ids=["om_choice"],
        idempotency_key="capability-choice",
        created_by="ou_1",
    )
    source_env = tmp_path / ".env"
    source_env.write_text("TOKEN=placeholder\n", encoding="utf-8")
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已配置。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "coding",
            "capability_refs": [{
                "source_file": str(source_env),
                "env_bindings": {"TOKEN": "TOKEN"},
            }],
        },
        "updated_summary": "configure provider",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_ambiguous_capability",
            text="复制这份配置", is_command=False, chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "CODING_CONFIRM_PENDING"
    assert fake.ticked == []
    facts = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(ProjectContextRecord.project_id == project.id)
    ).all()
    assert not any(item.record_type == "capability" for item in facts)
    assert "先从最新配置方案卡中选择" in str(feishu_ctx.bridge.sent[-1][2])


async def test_correction_during_multi_question_input_routes_without_answering_card(
    feishu_ctx,
):
    """A new correction is not an implicit answer to an unrelated pending card."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import FeedbackPacket

    chat_id = "oc_input_correction"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="existing-site",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
        feishu_chat_id=chat_id,
    )
    project.current_state = "ACCEPTANCE_INPUT_PENDING"
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACCEPTANCE_PENDING",
        "acceptance_evaluator",
        "acceptance",
        "two-external-facts",
        [
            {
                "question_id": "q_phone",
                "kind": "blocking",
                "question": "Phone passed?",
                "allow_free_text": False,
                "options": [{"key": "verified_pass", "label": "Passed"}],
            },
            {
                "question_id": "q_preference",
                "kind": "blocking",
                "question": "Preferred color?",
                "allow_free_text": True,
                "options": [],
            },
        ],
    )
    approval = await feishu_ctx.service.create_approval(
        project.id,
        None,
        "ACCEPTANCE_INPUT_PENDING",
        "input",
        "Please answer",
        [],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, active_project_id=project.id, update_active_project=True
    )
    feishu_ctx.service.session.commit()
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "Route the observed failure to coding.",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "project", "id": project.id},
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "coding",
            "question_id": None,
            "option_key": None,
        },
        "updated_summary": "service lifetime correction",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_lifetime_failure",
            text="Agent 退出后服务也退出，请回到编码修复。",
            is_command=False,
            chat_type="p2p",
        ),
    )

    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "CODING"
    assert feishu_ctx.service.get_question_interaction(interaction.id).status == "expired"
    for question in feishu_ctx.service.get_questions_for_interaction(interaction.id):
        assert feishu_ctx.service.get_question_answer(question.id) is None
    assert feishu_ctx.service.get_approval(approval.id).status == "expired"
    assert fake.ticked == [project.id]
    feedback = feishu_ctx.service.session.exec(
        select(FeedbackPacket).where(FeedbackPacket.project_id == project.id)
    ).all()
    assert len(feedback) == 1
    assert feedback[0].recommended_rework_target == "coding"


async def test_capability_choice_and_details_apply_atomically(feishu_ctx, tmp_path):
    """One natural-language answer may select a capability card and supply its ref."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import ProjectContextRecord

    chat_id = "oc_atomic_capability_choice"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, active_project_id=project.id, update_active_project=True,
    )
    pending = feishu_ctx.service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="clarification",
        payload={
            "subject_key": "runtime.provider_choice",
            "question": "Which provider?",
            "options": [
                {"key": "reference_project", "label": "Reference a local project"},
                {"key": "provide_key", "label": "Provide a key"},
            ],
        },
        source_message_ids=["om_choice"],
        idempotency_key="atomic-capability-choice",
        created_by="ou_1",
    )
    source_env = tmp_path / ".env"
    source_env.write_text("SOURCE_TOKEN=placeholder\n", encoding="utf-8")
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已选择本地项目并登记凭证引用。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "project", "id": project.id},
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "coding",
            "pending_action_id": pending.id,
            "pending_option_key": "reference_project",
            "capability_refs": [{
                "source_file": str(source_env),
                "env_bindings": {"TARGET_TOKEN": "SOURCE_TOKEN"},
            }],
        },
        "updated_summary": "referenced local runtime capability",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_atomic_capability",
            text=f"选择本地项目，引用 {source_env}",
            is_command=False, chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.session.get(type(pending), pending.id).status == "resolved"
    facts = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(
            ProjectContextRecord.project_id == project.id,
            ProjectContextRecord.record_type == "capability",
        )
    ).all()
    assert len(facts) == 1
    assert facts[0].value["env_bindings"] == {"TARGET_TOKEN": "SOURCE_TOKEN"}
    assert fake.ticked == [project.id]


async def test_capability_binding_direction_comes_from_structured_contract(
    feishu_ctx, tmp_path
):
    """Handler never reparses a human-facing label to change a binding."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import ProjectContextRecord

    chat_id = "oc_capability_binding_direction"
    feishu_ctx.chat_id = chat_id
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, active_project_id=project.id, update_active_project=True,
    )
    source_env = tmp_path / ".env"
    source_env.write_text(
        "DEEPSEEK_API_KEY=placeholder\nOPENAI_BASE_URL=https://example.invalid\n",
        encoding="utf-8",
    )
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已配置。",
        "intent": "correct",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "proposed_action": {
            "type": "route_project_input",
            "target_stage": "coding",
            "capability_refs": [{
                "source_file": str(source_env),
                "env_bindings": {
                    "OPENAI_API_KEY": "DEEPSEEK_API_KEY",
                    "OPENAI_BASE_URL": "OPENAI_BASE_URL",
                },
                "affected_trace_ids": ["AC-PROVIDER-001"],
            }],
        },
        "updated_summary": "configure provider",
    })
    feishu_ctx.orchestrator = fake

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="card:provider:deepseek",
            # Deliberately omit any arrow notation. The structured action is
            # authoritative and equivalent natural-language labels cannot
            # reverse its meaning.
            text="我选择刚才推荐的 DeepSeek 方案。",
            is_command=False, chat_type="p2p",
        ),
    )

    facts = feishu_ctx.service.session.exec(
        select(ProjectContextRecord).where(
            ProjectContextRecord.project_id == project.id,
            ProjectContextRecord.record_type == "capability",
        )
    ).all()
    assert len(facts) == 1
    assert facts[0].value["env_bindings"] == {
        "OPENAI_API_KEY": "DEEPSEEK_API_KEY",
        "OPENAI_BASE_URL": "OPENAI_BASE_URL",
    }
    assert facts[0].value["affected_trace_ids"] == ["AC-PROVIDER-001"]


async def test_failed_coordinator_hides_internal_reason_and_fails_safe(feishu_ctx):
    """Provider details stay in logs while the UI reports a safe failure."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import Project

    feishu_ctx.chat_id = "oc_real_reason"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": False,
        "error": "upstream socket closed",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_real_reason",
            text="取消这个项目",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_real_reason",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    assert len(feishu_ctx.orchestrator.agent_calls) == 3  # original + two retries
    final_card_text = str(feishu_ctx.bridge.sent[-1][2])
    assert "upstream socket closed" not in final_card_text
    assert "没有执行任何操作" in final_card_text
    assert "时限" not in final_card_text
    assert "结果已整理" not in final_card_text


async def test_coordinator_retries_once_then_succeeds(feishu_ctx):
    """A transient failure is retried once; success on the second attempt wins."""
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import Project

    feishu_ctx.chat_id = "oc_retry_then_ok"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithRetry([
        {"success": False, "error": "socket reset"},
        {
            "success": True,
            "reply": "已帮你取消",
            "intent": "cancel_project",
            "effect": "cancel",
            "commitment": "committed",
            "needs_clarification": False,
            "updated_summary": "用户取消项目",
            "proposed_action": {"type": "cancel_project"},
        },
    ])

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_retry_then_ok",
            text="取消这个项目",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_retry_then_ok",
            chat_type="group",
        ),
    )

    assert len(feishu_ctx.orchestrator.agent_calls) == 2
    first_call, second_call = feishu_ctx.orchestrator.agent_calls
    assert first_call["kwargs"]["extra_variables"]["retry_feedback"] == ""
    retry_feedback = second_call["kwargs"]["extra_variables"]["retry_feedback"]
    assert retry_feedback.startswith("socket reset")
    assert "只返回修正后的 JSON 合同" in retry_feedback
    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    final_card_text = str(feishu_ctx.bridge.sent[-1][2])
    assert "已帮你取消" in final_card_text

    # Progress card shows the retry, then is replaced by the actual result.
    updated_headers = [
        u[1]["header"]["title"]["content"] for u in feishu_ctx.bridge.updated
    ]
    assert "调用失败，正在重试" in updated_headers
    assert "答复" in updated_headers


async def test_bind_no_args_uses_deterministic_candidates_without_agent(
    feishu_ctx, tmp_path, monkeypatch
):
    """Repository discovery stays out of the LLM control path."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "agent-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_bind_agent"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={
            "success": True,
            "candidates": [
                {"path": str(repo), "name": "agent-repo", "description": "from agent"}
            ],
            "notes": [],
        }
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    assert feishu_ctx.orchestrator.agent_calls == []

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert str(repo) in str(payload)
    assert "agent-repo" in str(payload)


async def test_bind_no_args_falls_back_when_agent_returns_no_candidates(
    feishu_ctx, tmp_path, monkeypatch
):
    """Deterministic scan is independent of an Agent's empty output."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "scanned-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_bind_fallback"
    feishu_ctx.chat_type = "group"
    # Agent returns success but empty candidate list.
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={"success": True, "candidates": [], "notes": []}
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    # Deterministic scan should have found the repo.
    assert str(repo) in str(payload)
    assert "scanned-repo" in str(payload)


async def test_bind_no_args_falls_back_when_agent_crashes(
    feishu_ctx, tmp_path, monkeypatch
):
    """Deterministic scan never calls a crashing Agent."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "fallback-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_bind_crash"
    feishu_ctx.chat_type = "group"

    class _CrashingOrchestrator(_FakeOrchestratorWithBindDir):
        async def run_chat_scoped_agent(self, *args, **kwargs):
            raise RuntimeError("agent crashed")

    feishu_ctx.orchestrator = _CrashingOrchestrator()

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert str(repo) in str(payload)


async def test_bind_no_args_filters_agent_candidates_outside_allowed_roots(
    feishu_ctx, tmp_path, monkeypatch
):
    """The deterministic scan never includes repos outside allowed roots."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inside_repo = allowed_root / "inside-repo"
    inside_repo.mkdir()
    (inside_repo / ".git").mkdir()

    # A path outside allowed_root that the agent should not have returned.
    outside_repo = tmp_path / "outside" / "outside-repo"
    outside_repo.mkdir(parents=True)
    (outside_repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_bind_filter"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={
            "success": True,
            "candidates": [
                {"path": str(inside_repo), "name": "inside-repo"},
                {"path": str(outside_repo), "name": "outside-repo"},
            ],
            "notes": [],
        }
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/bind", is_command=True),
    )

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    # Inside repo appears, outside is filtered.
    assert str(inside_repo) in str(payload)
    assert str(outside_repo) not in str(payload)


async def test_create_in_bound_directory_uses_binding(feishu_ctx, tmp_path):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_bound_create"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat("oc_bound_create", str(target_dir), "ou_1")

    from mac_system.models import Project

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    projects = feishu_ctx.service.session.exec(
        select(Project).where(Project.bound_directory == str(target_dir.resolve()))
    ).all()
    assert len(projects) == 1
    assert projects[0].bound_directory == str(target_dir.resolve())


async def test_status_command_sends_status_card(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="status-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=f"/status {project.id}", is_command=True),
    )

    assert any(c[0] == "interactive" for c in feishu_ctx.bridge.sent)
    assert any(project.name in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_input_command_submits_answer(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="input-test", task_type="feature", description="test", creator_open_id="ou_1"
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="有什么问题？",
        options=[{"action": "answer", "label": "提交回答"}],
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text=f"/input {approval.id} 使用严格模式",
            is_command=True,
        ),
    )

    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS"
    assert updated.context.get("clarification_answer") == "使用严格模式"


async def test_confirm_text_command_confirms_approval(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="confirm-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        approval_mode="normal",
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认需求？",
        options=[{"action": "confirm", "label": "确认"}, {"action": "reject", "label": "驳回"}],
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text=f"/confirm {approval.id}", is_command=True),
    )

    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "PLANNING_ARCHITECTURE"
    assert feishu_ctx.service.get_approval(approval.id).status == "responded"


async def test_reject_text_command_creates_feedback_and_rolls_back(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="reject-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        approval_mode="normal",
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认需求？",
        options=[{"action": "confirm", "label": "确认"}, {"action": "reject", "label": "驳回"}],
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text=f"/reject {approval.id} 范围太大，先只做核心",
            is_command=True,
        ),
    )

    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "PLANNING_REQUIREMENTS"
    feedback = feishu_ctx.service.get_open_feedback_packets(project.id)
    assert any(fb.kind == "human_rejection" and "范围太大" in fb.summary for fb in feedback)


async def test_planning_reject_card_collects_feedback_before_transition(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="reject-card-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        approval_mode="normal",
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认需求？",
        options=[{"action": "confirm", "label": "确认"}, {"action": "reject", "label": "驳回"}],
    )

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="reject",
            state="REQUIREMENT_CONFIRM_PENDING",
            decision_id=None,
            option_key=None,
            extras={},
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert feishu_ctx.service.get_approval(approval.id).status == "pending"
    assert any("submit_rejection_feedback" in str(item[2]) for item in feishu_ctx.bridge.sent)
    rejection_card = next(
        item[2] for item in feishu_ctx.bridge.sent
        if "submit_rejection_feedback" in str(item[2])
    )
    assert rejection_card["schema"] == "2.0"
    assert rejection_card["body"]["elements"][1]["tag"] == "form"

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_rejection_feedback",
            state="REQUIREMENT_CONFIRM_PENDING",
            decision_id=None,
            option_key=None,
            extras={"rejection_feedback": "范围太大，先做核心"},
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    feedback = feishu_ctx.service.get_open_feedback_packets(project.id)
    assert any("范围太大" in item.summary for item in feedback)


async def test_list_command_shows_user_projects(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    project = await feishu_ctx.service.create_project(
        name="list-test", task_type="feature", description="test", creator_open_id="ou_1"
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/list", is_command=True),
    )

    assert any(project.name in str(c[2]) for c in feishu_ctx.bridge.sent)
    assert any("list-test" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_current_and_leave_project_commands(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_current"
    feishu_ctx.chat_type = "group"
    project = await feishu_ctx.service.create_project(
        name="current-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_current",
        feishu_chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/current", is_command=True),
    )
    assert any(project.name in str(c[2]) for c in feishu_ctx.bridge.sent)

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m2", text="/leave-project", is_command=True),
    )
    assert any("已清除" in str(c[2]) or "离开" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_current_shows_bound_directory_and_active_project(feishu_ctx, tmp_path):
    """`/current` should show both the chat's bound directory and active project."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_current_dir"
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat(
        "oc_current_dir", str(target_dir), "ou_1"
    )
    project = await feishu_ctx.service.create_project(
        name="with-dir",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_current_dir",
        feishu_chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/current", is_command=True),
    )

    rendered = _sent_output_text(feishu_ctx)
    assert str(target_dir.resolve()) in rendered
    assert project.name in rendered


async def test_switch_command_sets_active_project(feishu_ctx):
    """`/switch <project_id>` should set the active project for this user."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_switch"
    feishu_ctx.chat_type = "group"
    project = await feishu_ctx.service.create_project(
        name="switch-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_switch",
        feishu_chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="m1",
            text=f"/switch {project.id}", is_command=True,
        ),
    )

    active = feishu_ctx.service.get_chat_user_active_project("oc_switch", "ou_1")
    assert active is not None
    assert active.id == project.id
    assert any("默认项目" in str(c[2]) for c in feishu_ctx.bridge.sent)


async def test_switch_no_args_pushes_project_card(feishu_ctx):
    """`/switch` (no args) should push a project selection card."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_switch_card"
    feishu_ctx.chat_type = "group"
    project = await feishu_ctx.service.create_project(
        name="switch-card-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_switch_card",
        feishu_chat_type="group",
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(open_id="ou_1", message_id="m1", text="/switch", is_command=True),
    )

    # An interactive card should have been sent.
    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert "switch" in str(payload)
    assert project.id in str(payload)


async def test_switch_card_button_sets_active_project(feishu_ctx):
    """Clicking a `switch:<project_id>` button should set the active project."""
    from mac_system.feishu.handlers import handle_card_action
    from mac_system.feishu.models import ButtonAction

    feishu_ctx.chat_id = "oc_switch_btn"
    feishu_ctx.chat_type = "group"
    project = await feishu_ctx.service.create_project(
        name="switch-btn-test",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        feishu_chat_id="oc_switch_btn",
        feishu_chat_type="group",
    )

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key=f"switch:{project.id}",
            extras={},
            chat_id="oc_switch_btn",
            chat_type="group",
            message_id="m1",
        ),
    )

    active = feishu_ctx.service.get_chat_user_active_project("oc_switch_btn", "ou_1")
    assert active is not None
    assert active.id == project.id


async def test_question_batch_answer_all_recommended_resolves(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-confirm",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_1",
        [{
            "question_id": "q_mode",
            "kind": "blocking",
            "question": "采用什么模式？",
            "options": [{"key": "strict", "label": "严格", "recommended": True}],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="采用什么模式？",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="answer_all_recommended",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert question.status == "answered"
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


async def test_question_batch_answer_with_values_resolves(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-values",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_2",
        [{
            "question_id": "q_scope",
            "kind": "blocking",
            "question": "范围？",
            "options": [
                {"key": "full", "label": "全部", "recommended": True},
                {"key": "core", "label": "核心", "recommended": False},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="范围？",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="answer_with_values",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id, "q_q_scope": "core"},
        ),
    )

    answer = feishu_ctx.service.get_question_answer(
        feishu_ctx.service.get_questions_for_interaction(interaction.id)[0].id
    )
    assert answer.answer == "core"
    assert answer.answer_label == "核心"
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


async def test_question_batch_select_question_option_updates_card(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-select",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_select",
        [{
            "question_id": "q_scope",
            "kind": "blocking",
            "question": "范围？",
            "options": [
                {"key": "full", "label": "全部", "recommended": True},
                {"key": "core", "label": "核心", "recommended": False},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="范围？",
        options=[],
        payload={"question_interaction_id": interaction.id, "question_batch_message_id": "om_card"},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    card = await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="select_question_option",
            state="REQUIREMENT_INPUT_PENDING",
            option_key="core",
            decision_id=None,
            extras={"interaction_id": interaction.id, "question_id": "q_scope"},
            message_id="om_card",
        ),
    )

    approval = feishu_ctx.service.get_approval(approval.id)
    assert approval.payload.get("selected_options") == {"q_scope": "core"}
    assert card is not None
    # The WebSocket response updates the card in-place; the handler returns it.
    assert len(feishu_ctx.bridge.updated) == 0
    selected_buttons = [
        a for element in card["elements"]
        for a in element.get("actions", [])
        if a.get("value", {}).get("action") == "select_question_option"
        and a.get("value", {}).get("option_key") == "core"
    ]
    assert len(selected_buttons) == 1
    assert selected_buttons[0]["type"] == "primary"
    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert question.status == "pending"


async def test_question_batch_submit_selected_options_resolves(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-submit",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_submit",
        [{
            "question_id": "q_scope",
            "kind": "blocking",
            "question": "范围？",
            "options": [
                {"key": "full", "label": "全部", "recommended": True},
                {"key": "core", "label": "核心", "recommended": False},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="范围？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "selected_options": {"q_scope": "core"},
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_selected_options",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = feishu_ctx.service.get_question_answer(question.id)
    assert answer.answer == "core"
    assert answer.answer_label == "核心"
    assert question.status == "answered"
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


async def test_question_batch_submit_updates_card_to_submitted_state(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-submit-receipt",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_submit_receipt",
        [{
            "question_id": "q_scope",
            "kind": "blocking",
            "question": "范围？",
            "options": [
                {"key": "full", "label": "全部", "recommended": True},
                {"key": "core", "label": "核心", "recommended": False},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="范围？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "selected_options": {"q_scope": "core"},
            "question_batch_message_id": "om_batch_card",
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_selected_options",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
            message_id="om_batch_card",
        ),
    )

    # The choice card should have been updated to a read-only submitted state.
    assert len(feishu_ctx.bridge.updated) >= 1
    receipt_card = feishu_ctx.bridge.updated[-1][1]
    assert receipt_card["header"]["template"] == "green"
    option_buttons = [
        a for el in receipt_card["elements"] if el.get("tag") == "action"
        for a in el.get("actions", [])
        if a.get("value", {}).get("action") == "select_question_option"
    ]
    assert all(b.get("disabled") for b in option_buttons)
    submitted = next(b for b in option_buttons if b["value"]["option_key"] == "core")
    assert submitted["type"] == "primary"


async def test_question_batch_submit_inline_free_text_resolves_interaction(feishu_ctx):
    """Option B: typing a custom answer in the inline form submits it directly
    and resolves the interaction without going through the 2-step flow."""
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-inline",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_inline",
        [{
            "question_id": "q_default",
            "kind": "blocking",
            "allow_free_text": True,
            "question": "是否接受默认？",
            "options": [
                {"key": "accept_default", "label": "接受默认", "recommended": True},
                {
                    "key": "provide_custom", "label": "我要修改",
                    "triggers_free_text": True,
                },
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="是否接受默认？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "inline_free_text_question_id": "q_default",
            "question_batch_message_id": "om_batch_card",
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_inline_free_text",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={
                "interaction_id": interaction.id,
                "question_id": "q_default",
                "inline_free_text_value": "我要用 PostgreSQL 而不是 SQLite",
            },
            message_id="om_batch_card",
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = feishu_ctx.service.get_question_answer(question.id)
    assert answer.answer == "我要用 PostgreSQL 而不是 SQLite"
    assert answer.answer_label == "我要用 PostgreSQL 而不是 SQLite"
    assert question.status == "answered"
    # Interaction should resolve and project should advance.
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"
    # Receipt card should have been pushed updating the original batch card.
    assert len(feishu_ctx.bridge.updated) >= 1


async def test_question_batch_submit_inline_free_text_empty_value_rejected(feishu_ctx):
    """Empty inline free-text submission should not record an answer."""
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-inline-empty",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_inline_empty",
        [{
            "question_id": "q_default",
            "kind": "blocking",
            "allow_free_text": True,
            "question": "是否接受默认？",
            "options": [
                {"key": "accept_default", "label": "接受默认", "recommended": True},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="是否接受默认？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "inline_free_text_question_id": "q_default",
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_inline_free_text",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={
                "interaction_id": interaction.id,
                "question_id": "q_default",
                "inline_free_text_value": "   ",
            },
        ),
    )

    question = feishu_ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert question.status == "pending"
    assert feishu_ctx.service.get_question_answer(question.id) is None


async def test_question_batch_submit_inline_free_text_keeps_other_questions_pending(feishu_ctx):
    """When there are multiple questions and the user submits inline free-text
    for one, the other questions remain pending and the interaction does not
    resolve."""
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-inline-multi",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_inline_multi",
        [
            {
                "question_id": "q_default",
                "kind": "blocking",
                "allow_free_text": True,
                "question": "是否接受默认？",
                "options": [
                    {"key": "accept_default", "label": "接受默认", "recommended": True},
                    {
                        "key": "provide_custom", "label": "我要修改",
                        "triggers_free_text": True,
                    },
                ],
            },
            {
                "question_id": "q_scope",
                "kind": "blocking",
                "question": "范围？",
                "options": [
                    {"key": "full", "label": "全部", "recommended": True},
                    {"key": "core", "label": "核心"},
                ],
            },
        ],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="是否接受默认？",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "inline_free_text_question_id": "q_default",
            "question_batch_message_id": "om_batch_card",
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_inline_free_text",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={
                "interaction_id": interaction.id,
                "question_id": "q_default",
                "inline_free_text_value": "我要用 PostgreSQL",
            },
            message_id="om_batch_card",
        ),
    )

    questions = {q.question_id: q for q in feishu_ctx.service.get_questions_for_interaction(interaction.id)}
    # q_default should be answered with the typed text.
    assert questions["q_default"].status == "answered"
    default_answer = feishu_ctx.service.get_question_answer(questions["q_default"].id)
    assert default_answer.answer == "我要用 PostgreSQL"
    # q_scope should remain pending.
    assert questions["q_scope"].status == "pending"
    # Project should NOT have advanced because q_scope is still pending.
    assert feishu_ctx.service.require_project(project.id).current_state == "REQUIREMENT_INPUT_PENDING"


async def test_question_batch_skip_remaining_does_not_skip_blocking(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-skip-blocking",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_4",
        [
            {
                "question_id": "q_required",
                "kind": "blocking",
                "question": "必须回答的问题？",
                "options": [{"key": "yes", "label": "是", "recommended": True}],
            },
            {
                "question_id": "q_optional",
                "kind": "non_blocking",
                "question": "可选说明？",
                "allow_free_text": True,
                "options": [],
            },
        ],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="问题批次",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="skip_remaining",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
        ),
    )

    questions = feishu_ctx.service.get_questions_for_interaction(interaction.id)
    blocking = next(q for q in questions if q.question_id == "q_required")
    optional = next(q for q in questions if q.question_id == "q_optional")
    assert blocking.status == "pending"
    # Free-text questions are on separate input cards; the batch card skip
    # button only applies to choice questions presented on that card.
    assert optional.status == "pending"
    assert feishu_ctx.service.require_project(project.id).current_state == "REQUIREMENT_INPUT_PENDING"
    assert "阻塞问题" in str(feishu_ctx.bridge.sent[-1][2])


async def test_question_batch_skip_remaining_resolves(feishu_ctx):
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-skip",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_3",
        [{
            "question_id": "q_optional",
            "kind": "non_blocking",
            "question": "补充说明？",
            "options": [
                {"key": "none", "label": "无补充", "recommended": True},
                {"key": "details", "label": "有补充"},
            ],
        }],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="补充说明？",
        options=[],
        payload={"question_interaction_id": interaction.id},
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="skip_remaining",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
        ),
    )

    answer = feishu_ctx.service.get_question_answer(
        feishu_ctx.service.get_questions_for_interaction(interaction.id)[0].id
    )
    assert answer.skipped is True
    assert feishu_ctx.service.require_project(project.id).current_state == "PLANNING_REQUIREMENTS"


async def test_question_batch_submit_choice_then_free_text_card_appears(feishu_ctx):
    """When a batch has a blocking choice + non_blocking free-text, submitting
    the choice must send the free-text input card instead of resolving early.

    Regression: previously `interaction_is_resolvable` returned True because
    non_blocking questions don't block resolution, so the interaction resolved
    immediately and the free-text card was never sent.
    """
    from mac_system.feishu.handlers import handle_card_action

    project = await feishu_ctx.service.create_project(
        name="qa-choice-then-freetext",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
    )
    project.current_state = "REQUIREMENT_INPUT_PENDING"
    feishu_ctx.service.session.commit()
    interaction = await feishu_ctx.service.create_question_interaction(
        project.id,
        "task_origin",
        1,
        "PLANNING_REQUIREMENTS",
        "planner",
        "requirements",
        "batch_choice_then_freetext",
        [
            {
                "question_id": "q_scope",
                "kind": "blocking",
                "question": "范围？",
                "options": [
                    {"key": "full", "label": "全部", "recommended": True},
                    {"key": "core", "label": "核心", "recommended": False},
                ],
            },
            {
                "question_id": "q_note",
                "kind": "non_blocking",
                "question": "补充说明？",
                "allow_free_text": True,
                "options": [],
            },
        ],
    )
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_INPUT_PENDING",
        approval_type="input",
        question="问题批次",
        options=[],
        payload={
            "question_interaction_id": interaction.id,
            "selected_options": {"q_scope": "core"},
            "question_batch_message_id": "om_batch_card",
        },
    )
    interaction.approval_id = approval.id
    feishu_ctx.service.session.commit()

    interactive_cards_before = sum(
        1 for entry in feishu_ctx.bridge.sent if entry[0] == "interactive"
    )
    await handle_card_action(
        feishu_ctx,
        ButtonAction(
            open_id="ou_1",
            approval_id=approval.id,
            action="submit_selected_options",
            state="REQUIREMENT_INPUT_PENDING",
            option_key=None,
            decision_id=None,
            extras={"interaction_id": interaction.id},
            message_id="om_batch_card",
        ),
    )

    # The choice card should be updated to a submitted receipt state.
    assert len(feishu_ctx.bridge.updated) >= 1
    # A new free-text input card should have been sent.
    interactive_cards_after = sum(
        1 for entry in feishu_ctx.bridge.sent if entry[0] == "interactive"
    )
    assert interactive_cards_after == interactive_cards_before + 1
    # The interaction should still be pending (not resolved).
    refreshed = feishu_ctx.service.get_question_interaction(interaction.id)
    assert refreshed.status == "pending"
    # The project should still be in REQUIREMENT_INPUT_PENDING (not advanced).
    assert (
        feishu_ctx.service.require_project(project.id).current_state
        == "REQUIREMENT_INPUT_PENDING"
    )
    # The free-text question should still be pending; the choice should be answered.
    questions = feishu_ctx.service.get_questions_for_interaction(interaction.id)
    choice_q = next(q for q in questions if q.question_id == "q_scope")
    free_text_q = next(q for q in questions if q.question_id == "q_note")
    assert choice_q.status == "answered"
    assert free_text_q.status == "pending"
    # The approval payload should now route replies from the new card to q_note.
    approval_after = feishu_ctx.service.get_approval(approval.id)
    reply_map = approval_after.payload.get("reply_message_to_question", {})
    assert any(v == "q_note" for v in reply_map.values())


# ---------------------------------------------------------------------------
# /create <description> flow: create_dir agent recommendation integration
# ---------------------------------------------------------------------------


async def test_create_with_description_calls_create_dir_agent_when_no_binding(
    feishu_ctx, tmp_path, monkeypatch
):
    """When chat has no binding and allowed roots are configured, /create calls the create_dir agent."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    # Candidate path does NOT exist and has no .git - this is the new /create semantics.
    new_dir = allowed_root / "config-validator"

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_agent"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={
            "success": True,
            "candidates": [
                {"path": str(new_dir), "name": "config-validator", "description": "matches"}
            ],
            "notes": [],
        }
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    # Agent was invoked with create_dir stage and the description was passed.
    assert feishu_ctx.orchestrator.agent_calls
    call = feishu_ctx.orchestrator.agent_calls[0]
    assert call["agent_type"] == "project_manager"
    assert call["stage"] == "create_dir"
    assert "config-validator" in call["extra_variables"]["description"]

    # A pick card was shown with the non-existent path (no .git filter anymore).
    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert str(new_dir) in str(payload)
    assert "config-validator" in str(payload)

    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is not None
    assert intent.stage == "awaiting_bind_dir_for_create"

    # No project should have been created yet.
    assert not feishu_ctx.orchestrator.ticked


async def test_create_with_create_dir_accepts_nonexistent_path_and_auto_creates(
    feishu_ctx, tmp_path, monkeypatch
):
    """Clicking a non-existent candidate path auto-creates the dir + git init."""
    from mac_system.feishu.handlers import _handle_menu_action

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    new_dir = allowed_root / "brand-new-project"
    assert not new_dir.exists()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_auto"
    feishu_ctx.chat_type = "group"

    # Simulate the paused state after _maybe_pause_for_bind_dir_recommendation.
    import json as _json
    feishu_ctx.service.upsert_pending_intent(
        "ou_1",
        description=_json.dumps(
            {"task_type": "feature", "description": "brand new project"},
            ensure_ascii=False,
        ),
        stage="awaiting_bind_dir_for_create",
        chat_id="oc_create_auto",
        chat_type="group",
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir()

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key=f"bind_dir_for_create_select:{new_dir}",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    # Directory was auto-created and git-initialized.
    assert new_dir.exists()
    assert (new_dir / ".git").exists()
    # Chat is now bound to that directory.
    binding = feishu_ctx.service.get_chat_directory_binding("oc_create_auto")
    assert binding is not None
    assert binding.directory == str(new_dir.resolve())
    # Project was created (tick was called).
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_create_with_description_skips_agent_when_chat_already_bound(
    feishu_ctx, tmp_path, monkeypatch
):
    """When chat already has a directory binding, /create does not call the agent."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_bound"
    feishu_ctx.chat_type = "group"

    bound_dir = allowed_root / "already-bound"
    bound_dir.mkdir()
    (bound_dir / ".git").mkdir()
    feishu_ctx.service.bind_directory_to_chat(
        "oc_create_bound", str(bound_dir), bound_by="ou_1"
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={"success": True, "candidates": [], "notes": []}
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    # Agent was NOT invoked because the chat is already bound.
    assert not feishu_ctx.orchestrator.agent_calls
    # Project was created directly.
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_create_with_description_skips_agent_when_no_allowed_roots(
    feishu_ctx, tmp_path, monkeypatch
):
    """When no allowed roots are configured, /create does not call the agent."""
    from mac_system.feishu.handlers import handle_text_message

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: []
    )

    feishu_ctx.chat_id = "oc_create_no_roots"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={"success": True, "candidates": [], "notes": []}
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    assert not feishu_ctx.orchestrator.agent_calls
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_create_with_description_skips_agent_when_chat_has_active_project(
    feishu_ctx, tmp_path, monkeypatch
):
    """When chat has an active/paused project, /create does not call the agent."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_blocked"
    feishu_ctx.chat_type = "group"

    # Create a project in this chat; it defaults to active status.
    await feishu_ctx.service.create_project(
        name="blocked-project",
        task_type="feature",
        description="desc",
        creator_open_id="ou_1",
        feishu_chat_id="oc_create_blocked",
        feishu_chat_type="group",
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={"success": True, "candidates": [], "notes": []}
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 另一个项目",
            is_command=True,
        ),
    )

    # Agent was NOT invoked because chat has an active project.
    assert not feishu_ctx.orchestrator.agent_calls
    # Project was still created (the blocker only blocks /bind, not /create).
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_create_with_description_falls_back_when_agent_returns_no_candidates(
    feishu_ctx, tmp_path, monkeypatch
):
    """When the agent returns no valid candidates, /create proceeds with system workspace."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_empty"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={"success": True, "candidates": [], "notes": []}
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    # Agent was invoked but returned no candidates, so project was created directly.
    assert feishu_ctx.orchestrator.agent_calls
    assert len(feishu_ctx.orchestrator.ticked) >= 1
    # No pending intent left.
    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is None


async def test_create_with_description_falls_back_when_agent_crashes(
    feishu_ctx, tmp_path, monkeypatch
):
    """When the agent raises, /create proceeds with system workspace."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_crash"
    feishu_ctx.chat_type = "group"

    class _CrashingOrchestrator(_FakeOrchestratorWithBindDir):
        async def run_chat_scoped_agent(self, *args, **kwargs):
            raise RuntimeError("agent crashed")

    feishu_ctx.orchestrator = _CrashingOrchestrator()

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    assert len(feishu_ctx.orchestrator.ticked) >= 1
    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is None


async def test_create_with_description_filters_agent_candidates_outside_allowed_roots(
    feishu_ctx, tmp_path, monkeypatch
):
    """Agent-returned paths outside allowed roots are filtered; if all are out, no card."""
    from mac_system.feishu.handlers import handle_text_message

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_repo = tmp_path / "outside" / "outside-repo"
    outside_repo.mkdir(parents=True)
    (outside_repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_filter"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir(
        agent_output={
            "success": True,
            "candidates": [
                {"path": str(outside_repo), "name": "outside-repo"},
            ],
            "notes": [],
        }
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m1",
            text="/create --type=feature 实现 config-validator CLI",
            is_command=True,
        ),
    )

    # All candidates were filtered out, so no pick card and project was created directly.
    assert len(feishu_ctx.orchestrator.ticked) >= 1
    intent = feishu_ctx.service.get_pending_intent("ou_1")
    assert intent is None


async def test_create_bind_dir_select_binds_directory_and_creates_project(
    feishu_ctx, tmp_path, monkeypatch
):
    """Clicking a candidate in the /create pick card binds the dir and creates the project."""
    from mac_system.feishu.handlers import _handle_menu_action

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    repo = allowed_root / "matched-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_pick"
    feishu_ctx.chat_type = "group"

    # Simulate the state after _maybe_pause_for_bind_dir_recommendation paused.
    import json as _json
    feishu_ctx.service.upsert_pending_intent(
        "ou_1",
        description=_json.dumps(
            {"task_type": "feature", "description": "实现 config-validator CLI"},
            ensure_ascii=False,
        ),
        stage="awaiting_bind_dir_for_create",
        chat_id="oc_create_pick",
        chat_type="group",
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir()

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key=f"bind_dir_for_create_select:{repo}",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    # Pending intent was cleared.
    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    # Directory was bound.
    binding = feishu_ctx.service.get_chat_directory_binding("oc_create_pick")
    assert binding is not None
    assert binding.directory == str(repo.resolve())
    # Project was created and ticked.
    assert len(feishu_ctx.orchestrator.ticked) >= 1
    assert "已绑定到" in _sent_output_text(feishu_ctx)


async def test_create_bind_dir_skip_creates_project_with_system_workspace(
    feishu_ctx, tmp_path, monkeypatch
):
    """Clicking 'skip' creates the project in the system workspace without binding."""
    from mac_system.feishu.handlers import _handle_menu_action

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_skip"
    feishu_ctx.chat_type = "group"

    import json as _json
    feishu_ctx.service.upsert_pending_intent(
        "ou_1",
        description=_json.dumps(
            {"task_type": "feature", "description": "实现 config-validator CLI"},
            ensure_ascii=False,
        ),
        stage="awaiting_bind_dir_for_create",
        chat_id="oc_create_skip",
        chat_type="group",
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir()

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key="bind_dir_for_create_skip",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    # Pending intent was cleared.
    assert feishu_ctx.service.get_pending_intent("ou_1") is None
    # No directory binding.
    assert feishu_ctx.service.get_chat_directory_binding("oc_create_skip") is None
    # Project was still created.
    assert len(feishu_ctx.orchestrator.ticked) >= 1


async def test_create_bind_dir_action_without_pending_intent_recovers(
    feishu_ctx, tmp_path, monkeypatch
):
    """If the user clicks a pick card button without a pending intent, send an error."""
    from mac_system.feishu.handlers import _handle_menu_action

    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    import mac_system.config as cfg_module
    monkeypatch.setattr(
        cfg_module, "get_allowed_bind_roots", lambda: [allowed_root]
    )

    feishu_ctx.chat_id = "oc_create_stale"
    feishu_ctx.chat_type = "group"

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir()

    action = ButtonAction(
        open_id="ou_1",
        approval_id="",
        action="menu",
        state="",
        decision_id=None,
        option_key="bind_dir_for_create_skip",
        extras={},
    )
    await _handle_menu_action(feishu_ctx, action)

    # No project created; user sees an error message.
    assert not feishu_ctx.orchestrator.ticked
    assert "没有正在等待的创建流程" in _sent_output_text(feishu_ctx)


class _FakeWatermarkService:
    """In-memory service stub for testing FeishuWebSocketClient staleness logic."""

    def __init__(self):
        self._watermarks: dict[tuple[str, str], datetime] = {}

    def get_last_event_at(self, chat_id: str, open_id: str) -> datetime | None:
        return self._watermarks.get((chat_id, open_id))

    def update_last_event_at(self, chat_id: str, open_id: str, event_at: datetime) -> None:
        key = (chat_id, open_id)
        current = self._watermarks.get(key)
        if current is None or event_at > current:
            self._watermarks[key] = event_at


def test_ws_client_staleness_uses_global_user_watermark():
    """A newer event in one chat should block older events in another chat."""
    from mac_system.feishu.ws_client import FeishuWebSocketClient

    client = FeishuWebSocketClient()
    service = _FakeWatermarkService()
    open_id = "ou_global"
    group_chat = "oc_group"
    p2p_chat = open_id

    now = datetime.utcnow()
    # Newer message handled in group chat.
    service.update_last_event_at(group_chat, open_id, now)
    service.update_last_event_at(p2p_chat, open_id, now)

    # Older p2p message is stale because of the global watermark.
    old_event = now - timedelta(hours=1)
    assert client._is_stale_event(service, p2p_chat, open_id, old_event)


def test_ws_client_staleness_allows_fresh_event():
    """An event newer than the watermark is not stale."""
    from mac_system.feishu.ws_client import FeishuWebSocketClient

    client = FeishuWebSocketClient()
    service = _FakeWatermarkService()
    open_id = "ou_fresh"
    chat_id = open_id

    now = datetime.utcnow()
    service.update_last_event_at(chat_id, open_id, now - timedelta(seconds=1))
    assert not client._is_stale_event(service, chat_id, open_id, now)


def test_ws_client_staleness_rejects_pre_startup_events():
    """Events created before the WS client started are dropped."""
    from mac_system.feishu.ws_client import FeishuWebSocketClient

    client = FeishuWebSocketClient()
    service = _FakeWatermarkService()
    open_id = "ou_startup"

    # Event from long before the client started should be stale even with no watermark.
    ancient = datetime.utcnow() - timedelta(days=1)
    assert client._is_stale_event(service, open_id, open_id, ancient)


def test_ws_client_staleness_rejects_pre_disconnect_events():
    """Events created before the last disconnect are dropped on reconnect."""
    from mac_system.feishu.ws_client import FeishuWebSocketClient

    client = FeishuWebSocketClient()
    service = _FakeWatermarkService()
    open_id = "ou_reconnect"

    # Simulate a disconnect 5 minutes ago.
    client._last_disconnect_at = datetime.utcnow() - timedelta(minutes=5)
    pre_disconnect = datetime.utcnow() - timedelta(minutes=6)
    assert client._is_stale_event(service, open_id, open_id, pre_disconnect)


async def _setup_rework_approval(feishu_ctx, chat_id: str):
    """Project parked in REWORK with an option-based pending approval."""
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "REWORK"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REWORK",
        approval_type="confirmation",
        question="本轮未通过，请选择如何处理",
        options=[
            {"action": "to_requirements", "label": "重新明确需求",
             "next_state": "PLANNING_REQUIREMENTS", "option_key": "to_requirements"},
            {"action": "to_testing", "label": "直接重新测试",
             "next_state": "TESTING", "option_key": "to_testing"},
        ],
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, summary="等待 rework 选择",
        active_project_id=project.id, update_active_project=True,
    )
    return project, approval


def _sent_card_texts(feishu_ctx) -> list[str]:
    return [
        json.dumps(item[2], ensure_ascii=False)
        for item in feishu_ctx.bridge.sent
        if item[0] == "interactive"
    ]


@pytest.mark.asyncio
async def test_rework_approval_invalid_reject_decision_reports_failure(feishu_ctx):
    """A 'reject' decision on an option-based (REWORK) approval must not crash,
    must not send the coordinator's false success reply, and must leave the
    approval pending with a user-visible failure notice."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_rework_reject"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project, approval = await _setup_rework_approval(feishu_ctx, chat_id)

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已收到，驳回当前测试结果，Agent 将配置 LLM 后重新运行集成测试。",
        "intent": "reject",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "approval", "id": approval.id},
        "proposed_action": {
            "type": "propose_approval_resolution",
            "approval_decision": "reject",
            "option_key": None,
            "feedback": "配置 LLM 后重新测试",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_rework_reject",
            text="驳回，配置 LLM 后重新测试", is_command=False, chat_type="p2p",
        ),
    )

    refreshed = feishu_ctx.service.get_approval(approval.id)
    assert refreshed.status == "pending"
    assert feishu_ctx.service.require_project(project.id).current_state == "REWORK"
    cards = _sent_card_texts(feishu_ctx)
    assert any("没有执行任何操作" in c for c in cards)
    assert not any("option approval requires select_option" in c for c in cards)
    assert not any("已收到，驳回当前测试结果" in c for c in cards)

    # The coordinator must see the approval's options so it can emit a valid
    # select_option next time.
    call = feishu_ctx.orchestrator.agent_calls[0]
    ctx_approvals = json.loads(call["extra_variables"]["pending_approvals"])
    assert ctx_approvals[0]["options"] == [
        {"option_key": "to_requirements", "label": "重新明确需求"},
        {"option_key": "to_testing", "label": "直接重新测试"},
    ]


@pytest.mark.asyncio
async def test_rework_approval_select_option_resolves(feishu_ctx):
    """select_option with a valid option_key maps to the option's action and
    resolves the REWORK approval, then the deterministic result card is sent."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_rework_select"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project, approval = await _setup_rework_approval(feishu_ctx, chat_id)

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已选择直接重新测试，项目将回到 TESTING 阶段重跑。",
        "intent": "select_option",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "approval", "id": approval.id},
        "proposed_action": {
            "type": "propose_approval_resolution",
            "approval_decision": "select_option",
            "option_key": "to_testing",
            "feedback": "配置 LLM 后重新运行集成测试",
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_rework_select",
            text="直接重新测试", is_command=False, chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "TESTING"
    resolved = feishu_ctx.service.get_approval(approval.id)
    assert resolved.status == "responded"
    assert resolved.response == "to_testing"
    cards = _sent_card_texts(feishu_ctx)
    assert any("你选择了「直接重新测试」" in c for c in cards)
    feedback = feishu_ctx.service.get_open_feedback_packets(project.id)
    assert any(
        item.kind == "human_rework"
        and "配置 LLM 后重新运行集成测试" in item.summary
        for item in feedback
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert "项目已进入测试中" in conversation.recent_turns[-1]["content"]


@pytest.mark.asyncio
async def test_rework_approval_preserves_detailed_user_text_when_feedback_omitted(
    feishu_ctx,
):
    """A Coordinator omission must not discard the defect report attached to
    an option-based REWORK choice."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_rework_feedback_fallback"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project, approval = await _setup_rework_approval(feishu_ctx, chat_id)
    defect = "真实浏览器发现身份回答前后矛盾，请回编码阶段修复根因"

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已回退编码阶段处理身份问题。",
        "intent": "select_option",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "approval", "id": approval.id},
        "proposed_action": {
            "type": "propose_approval_resolution",
            "approval_decision": "select_option",
            "option_key": "to_requirements",
            "feedback": None,
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_rework_feedback_fallback",
            text=defect,
            is_command=False,
            chat_type="p2p",
        ),
    )

    feedback = feishu_ctx.service.get_open_feedback_packets(project.id)
    assert any(item.kind == "human_rework" and item.summary == defect for item in feedback)


@pytest.mark.asyncio
async def test_confirmation_approval_confirm_via_coordinator_resolves(feishu_ctx):
    """Plain confirm on a confirmation approval keeps working through the
    coordinator path (no options starvation regression)."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_confirm_coord"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="ARCHITECTURE_CONFIRM_PENDING",
        approval_type="confirmation",
        question="确认方案",
        options=[
            {"action": "confirm", "label": "确认"},
            {"action": "reject", "label": "驳回"},
        ],
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, summary="等待方案确认",
        active_project_id=project.id, update_active_project=True,
    )

    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已确认当前技术方案，继续推进。",
        "intent": "approve",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "approval", "id": approval.id},
        "proposed_action": {
            "type": "propose_approval_resolution",
            "approval_decision": "confirm",
            "option_key": None,
            "feedback": None,
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_confirm_coord",
            text="确认", is_command=False, chat_type="p2p",
        ),
    )

    assert (
        feishu_ctx.service.require_project(project.id).current_state
        == "PLANNING_IMPLEMENTATION_SPECS"
    )
    resolved = feishu_ctx.service.get_approval(approval.id)
    assert resolved.status == "responded"
    assert resolved.response == "confirm"
    cards = _sent_card_texts(feishu_ctx)
    assert any("你选择了「确认」" in c for c in cards)
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert (
            "项目已进入实现计划编制中"
        in conversation.recent_turns[-1]["content"]
    )


@pytest.mark.asyncio
async def test_recovery_approval_action_only_option_resolves_via_coordinator(
    feishu_ctx,
):
    """Recovery cards use action-only options; natural language selection must
    normalize the action into option_key and resume the failed stage."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_recovery_coord"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    project = await feishu_ctx.service.create_project(
        name="demo", task_type="feature", description="d",
        creator_open_id="ou_1", feishu_chat_id=chat_id,
    )
    project.current_state = "CODING_RECOVERY_PENDING"
    feishu_ctx.service.session.add(project)
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="CODING_RECOVERY_PENDING",
        approval_type="confirmation",
        question="编码 Agent 已连续失败，请选择恢复方式",
        options=[
            {"action": "retry", "label": "再次重试", "next_state": "CODING"},
            {"action": "cancel", "label": "终止项目", "next_state": "CANCELLED"},
        ],
    )
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    feishu_ctx.service.update_conversation_session(
        conversation, summary="等待恢复选择",
        active_project_id=project.id, update_active_project=True,
    )
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已选择再次重试编码阶段。",
        "intent": "select_option",
        "effect": "workflow_control",
        "commitment": "authorized",
        "needs_clarification": False,
        "target": {"kind": "approval", "id": approval.id},
        "proposed_action": {
            "type": "propose_approval_resolution",
            "approval_decision": "select_option",
            "option_key": "retry",
            "feedback": None,
        },
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1", message_id="om_recovery_coord",
            text="再次重试", is_command=False, chat_type="p2p",
        ),
    )

    assert feishu_ctx.service.require_project(project.id).current_state == "CODING"
    resolved = feishu_ctx.service.get_approval(approval.id)
    assert resolved.status == "responded"
    assert resolved.response == "retry"
    call = feishu_ctx.orchestrator.agent_calls[0]
    ctx_approvals = json.loads(call["extra_variables"]["pending_approvals"])
    assert ctx_approvals[0]["options"][0]["option_key"] == "retry"


def test_strip_protocol_echo_removes_self_echo_block():
    from mac_system.feishu.handlers import _strip_protocol_echo

    echo = (
        "这是给你的答案。\n\n现在你有三条路可以选：\n\n"
        '```json\n{"success": true, "reply": "这是给你的答案。", '
        '"proposed_action": {"type": "answer"}, "needs_clarification": false}\n```'
    )
    cleaned = _strip_protocol_echo(echo)
    assert "这是给你的答案" in cleaned
    assert "```" not in cleaned
    assert "proposed_action" not in cleaned


def test_strip_protocol_echo_preserves_legit_json_block():
    from mac_system.feishu.handlers import _strip_protocol_echo

    text = '需求文档片段如下：\n\n```json\n{"requirements": [{"id": "REQ-1"}]}\n```'
    assert _strip_protocol_echo(text) == text


def test_strip_protocol_echo_whole_reply_is_blob_returns_empty():
    from mac_system.feishu.handlers import _strip_protocol_echo

    blob = '{"success": true, "reply": "x", "proposed_action": {"type": "answer"}}'
    assert _strip_protocol_echo(blob) == ""


async def test_reject_without_feedback_advances_state_machine(feishu_ctx, monkeypatch):
    """Bug #3: reject without feedback on a non-planning confirmation state
    (e.g. CODING_CONFIRM_PENDING) must call _advance_until_confirm.

    For planning stages, the handler shows a feedback input card and returns
    early (handlers.py:2467-2488).  For non-planning stages (quality gates
    like CODING_CONFIRM_PENDING), no feedback form is shown, and without
    _advance_until_confirm the project is left stuck in an agent_execution
    state with no tick scheduled.
    """
    from mac_system.feishu import handlers as feishu_handlers
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.feishu.bridge import TextMessage

    advance_calls = []

    async def fake_advance(ctx, project, receive_id, receive_id_type, **kwargs):
        advance_calls.append(project.id)

    monkeypatch.setattr(feishu_handlers, "_advance_until_confirm", fake_advance)

    project = await feishu_ctx.service.create_project(
        name="reject-no-feedback",
        task_type="feature",
        description="test",
        creator_open_id="ou_1",
        approval_mode="normal",
    )
    # Use CODING_CONFIRM_PENDING (quality gate, not planning) so the feedback
    # form branch at handlers.py:2467-2488 is skipped.
    project.current_state = "CODING_CONFIRM_PENDING"
    feishu_ctx.service.session.commit()
    approval = await feishu_ctx.service.create_approval(
        project_id=project.id,
        task_id=None,
        state="CODING_CONFIRM_PENDING",
        approval_type="confirmation",
        question="代码确认？",
        options=[{"action": "confirm", "label": "确认"}, {"action": "reject", "label": "驳回"}],
    )

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="m_reject_no_feedback",
            text=f"/reject {approval.id}",
            is_command=True,
        ),
    )

    assert len(advance_calls) == 1, (
        "_advance_until_confirm must be called on /reject without feedback text "
        "so the project is not left stuck in an agent_execution state"
    )


@pytest.mark.asyncio
async def test_advance_continues_when_boundary_tick_routes_to_agent_state(
    feishu_ctx,
):
    from mac_system.feishu.handlers import _advance_until_confirm

    project = await feishu_ctx.service.create_project(
        name="boundary-route",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "ACCEPTANCE_INPUT_PENDING"
    feishu_ctx.service.session.commit()

    class BoundaryRoutingOrchestrator:
        def __init__(self):
            self.calls = 0

        async def tick(self, project_id: str) -> None:
            self.calls += 1
            if self.calls == 1:
                routed = feishu_ctx.service.require_project(project_id)
                routed.current_state = "TESTING"
                feishu_ctx.service.session.add(routed)
                feishu_ctx.service.session.commit()

    fake = BoundaryRoutingOrchestrator()
    feishu_ctx.orchestrator = fake

    await _advance_until_confirm(
        feishu_ctx,
        project,
        "ou_1",
        "open_id",
    )

    assert fake.calls == 3


@pytest.mark.asyncio
async def test_advance_continues_when_reached_boundary_auto_routes_to_agent_state(
    feishu_ctx,
):
    """A boundary reached inside the loop may auto-resolve in the same tick."""
    from mac_system.feishu.handlers import _advance_until_confirm

    project = await feishu_ctx.service.create_project(
        name="reached-boundary-route",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "CODING"
    feishu_ctx.service.session.commit()

    class ReachedBoundaryRoutingOrchestrator:
        def __init__(self):
            self.calls = 0

        async def tick(self, project_id: str) -> None:
            self.calls += 1
            routed = feishu_ctx.service.require_project(project_id)
            if self.calls == 1:
                routed.current_state = "REWORK"
            elif self.calls == 2:
                routed.current_state = "CODING"
            feishu_ctx.service.session.add(routed)
            feishu_ctx.service.session.commit()

    fake = ReachedBoundaryRoutingOrchestrator()
    feishu_ctx.orchestrator = fake

    await _advance_until_confirm(
        feishu_ctx,
        project,
        "ou_1",
        "open_id",
    )

    assert fake.calls == 4


@pytest.mark.asyncio
async def test_advance_retries_unchanged_agent_state_without_running_task(
    feishu_ctx,
):
    """Recovery bookkeeping must not leave an idle agent state unscheduled."""
    from mac_system.feishu.handlers import _advance_until_confirm

    project = await feishu_ctx.service.create_project(
        name="idle-agent-retry",
        task_type="feature",
        description="d",
        creator_open_id="ou_1",
    )
    project.current_state = "TESTING"
    feishu_ctx.service.session.commit()

    class IdleThenAdvanceOrchestrator:
        def __init__(self):
            self.calls = 0

        async def tick(self, project_id: str) -> None:
            self.calls += 1
            if self.calls == 2:
                routed = feishu_ctx.service.require_project(project_id)
                routed.current_state = "TESTING_CONFIRM_PENDING"
                feishu_ctx.service.session.add(routed)
                feishu_ctx.service.session.commit()

    fake = IdleThenAdvanceOrchestrator()
    feishu_ctx.orchestrator = fake

    await _advance_until_confirm(
        feishu_ctx,
        project,
        "ou_1",
        "open_id",
    )

    assert fake.calls == 3


# ---------------------------------------------------------------------------
# Free-text trigger + intent recognition
#
# Bug: clicking the "I'll describe my own plan" option did not collect
# free-text input because the handler only recognized the exact option key
# "provide_custom". LLMs frequently emit other keys ("custom", "describe",
# "i_will_describe", ...). The handler now uses three signals: an explicit
# ``triggers_free_text: true`` marker on the option, a synonym set, and the
# ``allow_free_text + non-recommended`` heuristic.
# ---------------------------------------------------------------------------


class _FakeQuestion:
    """Minimal stand-in for the Question ORM model used by the handler."""

    def __init__(self, *, allow_free_text: bool, options: list[dict]):
        self.allow_free_text = allow_free_text
        self.options = options


def test_option_triggers_free_text_explicit_marker():
    from mac_system.feishu.handlers import _option_triggers_free_text

    question = _FakeQuestion(
        allow_free_text=True,
        options=[
            {"key": "accept_default", "label": "接受默认", "recommended": True},
            {"key": "anything", "label": "我要修改", "recommended": False, "triggers_free_text": True},
        ],
    )
    assert _option_triggers_free_text(question, "anything") is True


def test_option_triggers_free_text_does_not_guess_from_key_synonym():
    from mac_system.feishu.handlers import _option_triggers_free_text

    # LLM emitted "custom" instead of "provide_custom" - no explicit marker.
    question = _FakeQuestion(
        allow_free_text=True,
        options=[
            {"key": "accept_default", "label": "接受默认", "recommended": True},
            {"key": "custom", "label": "我要修改", "recommended": False},
        ],
    )
    assert _option_triggers_free_text(question, "custom") is False


def test_option_triggers_free_text_does_not_guess_from_recommendation():
    from mac_system.feishu.handlers import _option_triggers_free_text

    # LLM emitted a totally non-conventional key, but the question has
    # allow_free_text=True and the selected option is the non-recommended one.
    question = _FakeQuestion(
        allow_free_text=True,
        options=[
            {"key": "yes", "label": "接受", "recommended": True},
            {"key": "no_thanks", "label": "我自己说", "recommended": False},
        ],
    )
    assert _option_triggers_free_text(question, "no_thanks") is False


def test_option_triggers_free_text_does_not_capture_multi_option_verdict():
    from mac_system.feishu.handlers import _option_triggers_free_text

    question = _FakeQuestion(
        allow_free_text=True,
        options=[
            {"key": "verified_pass", "recommended": False},
            {"key": "verified_fail", "recommended": False},
            {"key": "not_verified", "recommended": True},
            {
                "key": "provide_details",
                "recommended": False,
                "triggers_free_text": True,
            },
        ],
    )
    assert _option_triggers_free_text(question, "verified_pass") is False
    assert _option_triggers_free_text(question, "verified_fail") is False
    assert _option_triggers_free_text(question, "provide_details") is True


def test_not_verified_keeps_only_external_acceptance_question_pending():
    from mac_system.feishu.handlers import _keeps_external_verification_pending

    interaction = type("Interaction", (), {"stage": "acceptance"})()
    question = type(
        "Question", (), {"affected_trace_ids": ["AC-NET-001"]}
    )()
    assert _keeps_external_verification_pending(
        interaction, question, "not_verified"
    ) is True
    assert _keeps_external_verification_pending(
        interaction, question, "verified_pass"
    ) is False
    non_acceptance = type("Interaction", (), {"stage": "testing"})()
    assert _keeps_external_verification_pending(
        non_acceptance, question, "not_verified"
    ) is False


def test_option_triggers_free_text_recommended_option_does_not_trigger():
    from mac_system.feishu.handlers import _option_triggers_free_text

    question = _FakeQuestion(
        allow_free_text=True,
        options=[
            {"key": "accept_default", "label": "接受默认", "recommended": True},
            {"key": "provide_custom", "label": "我要修改", "recommended": False, "triggers_free_text": True},
        ],
    )
    assert _option_triggers_free_text(question, "accept_default") is False


def test_option_triggers_free_text_disabled_when_allow_free_text_false():
    from mac_system.feishu.handlers import _option_triggers_free_text

    question = _FakeQuestion(
        allow_free_text=False,
        options=[
            {"key": "opt_a", "label": "A", "recommended": True},
            {"key": "opt_b", "label": "B", "recommended": False, "triggers_free_text": True},
        ],
    )
    # Even if the option is marked triggers_free_text, the question must
    # also have allow_free_text=True for free-text collection to make sense.
    assert _option_triggers_free_text(question, "opt_b") is False


def test_option_triggers_free_text_works_with_dict_input():
    from mac_system.feishu.handlers import _option_triggers_free_text

    # Handler also passes plain dicts (from model_dump()). Verify the helper
    # handles both ORM objects and dicts.
    question_dict = {
        "allow_free_text": True,
        "options": [
            {"key": "accept_default", "recommended": True},
            {"key": "describe", "recommended": False, "triggers_free_text": True},
        ],
    }
    assert _option_triggers_free_text(question_dict, "describe") is True


def test_option_triggers_free_text_empty_option_key():
    from mac_system.feishu.handlers import _option_triggers_free_text

    question = _FakeQuestion(allow_free_text=True, options=[])
    assert _option_triggers_free_text(question, None) is False
    assert _option_triggers_free_text(question, "") is False


# ---------------------------------------------------------------------------
# Intent -> PendingUserInput.kind mapping
#
# Bug: handler only recognized intent=="correct" as a correction. The schema
# defines 25+ intent values; ``provide_feedback`` is semantically equivalent
# to ``correct`` when the user is sending mid-execution feedback, and should
# also be recorded as a blocking correction.
# ---------------------------------------------------------------------------


def test_intent_to_kind_correct_maps_to_correction():
    from mac_system.feishu.handlers import _intent_to_pending_input_kind

    assert _intent_to_pending_input_kind("correct") == "correction"


def test_intent_to_kind_provide_feedback_maps_to_correction():
    from mac_system.feishu.handlers import _intent_to_pending_input_kind

    assert _intent_to_pending_input_kind("provide_feedback") == "correction"


def test_intent_to_kind_change_scope_maps_to_scope_change():
    from mac_system.feishu.handlers import _intent_to_pending_input_kind

    assert _intent_to_pending_input_kind("change_scope") == "scope_change"


def test_intent_to_kind_conversational_intents_map_to_question():
    from mac_system.feishu.handlers import _intent_to_pending_input_kind

    # These are conversational and should NOT block the agent.
    for intent in ("inspect", "discuss", "explain", "status_query", "provide_information", "unknown"):
        assert _intent_to_pending_input_kind(intent) == "question"


def test_intent_to_kind_none_maps_to_question():
    from mac_system.feishu.handlers import _intent_to_pending_input_kind

    assert _intent_to_pending_input_kind(None) == "question"
