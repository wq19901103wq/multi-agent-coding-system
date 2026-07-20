from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.feishu import FeishuBridge
from mac_system.feishu.card import (
    build_confirmation_card,
    build_input_card,
    build_status_card,
)
from mac_system.feishu.crypto import verify_signature
from mac_system.feishu.models import ButtonAction, TextMessage


class FakeBridge:
    def __init__(self):
        self.sent = []
        self.updated = []
        self.reactions = []
        self._message_seq = 0

    def _next_message_id(self) -> str:
        self._message_seq += 1
        return f"msg_{self._message_seq}"

    async def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> str:
        self.sent.append(("text", open_id, text, receive_id_type))
        return self._next_message_id()

    async def _send_interactive(self, open_id: str, card: dict, receive_id_type: str = "open_id") -> str:
        self.sent.append(("interactive", open_id, card, receive_id_type))
        return self._next_message_id()

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

    async def add_reaction(self, message_id: str, emoji_type: str = "OnIt") -> str:
        self.reactions.append(("add", message_id, emoji_type))
        return "reaction_1"

    async def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        self.reactions.append(("remove", message_id, reaction_id))


class FakeOrchestrator:
    def __init__(self):
        self.ticked = []
        self.cancelled = []

    async def tick(self, project_id: str) -> None:
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
    assert any("已创建项目" in c[2] for c in feishu_ctx.bridge.sent if c[0] == "text")
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
    assert any("已创建项目" in c[2] for c in feishu_ctx.bridge.sent if c[0] == "text")
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
                {"key": "provide_custom", "label": "我要修改"},
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
    sent_text = " ".join(
        str(item[2]) for item in feishu_ctx.bridge.sent if item[0] == "text"
    )
    assert f"/confirm {approval.id}" in sent_text


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

    assert any(
        "已处理，无需重复提交" in str(item[2])
        for item in feishu_ctx.bridge.sent
        if item[0] == "text"
    )


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
    assert "没有权限" in feishu_ctx.bridge.sent[-1][2]


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
                {"key": "provide_custom", "label": "我要修改"},
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
    assert any("已创建项目" in str(c[2]) for c in feishu_ctx.bridge.sent if c[0] == "text")
    assert len(feishu_ctx.orchestrator.ticked) >= 1


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

    sent_text = " ".join(str(c[2]) for c in feishu_ctx.bridge.sent if c[0] == "text")
    assert "MAC_ALLOWED_BIND_ROOTS" in sent_text
    assert "/bind <绝对路径>" in sent_text


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

    async def tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        self.cancelled.append((project_id, reason))
        if self.project_service is not None:
            await self.project_service.cancel_project(project_id, reason=reason)

    async def run_chat_scoped_agent(
        self, agent_type, stage, input_data, extra_variables=None, *, worktree=None,
        progress_callback=None,
    ):
        self.agent_calls.append({
            "agent_type": agent_type,
            "stage": stage,
            "input_data": input_data,
            "extra_variables": extra_variables,
            "worktree": worktree,
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
    assert "正在处理" in str(feishu_ctx.bridge.sent[0][2])
    assert "处理完成" in str(feishu_ctx.bridge.updated[-1][1])
    assert "不会开始修改代码" in str(feishu_ctx.bridge.sent[-1][2])
    assert feishu_ctx.bridge.reactions == [
        ("add", "om_discuss", "OnIt"),
        ("remove", "om_discuss", "reaction_1"),
    ]


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


async def test_conversation_resolves_unbound_workspace_from_repo_mention(
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

    monkeypatch.setattr(cfg_module, "get_allowed_bind_roots", lambda: [allowed_root])

    chat_id = "oc_repo_mention"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "p2p"
    fake = _FakeOrchestratorWithBindDir({
        "success": True,
        "reply": "已找到并读取参考项目。",
        "intent": "research",
        "effect": "read_only",
        "commitment": "exploring",
        "needs_clarification": False,
        "proposed_action": {"type": "answer"},
        "updated_summary": "用户参考 mac-rpa 项目",
    })
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

    call = fake.agent_calls[0]
    assert call["worktree"] == repo
    extra = call["extra_variables"]
    assert extra["workspace_path"] == str(repo)
    repos = json.loads(extra["available_repositories"])
    assert any(r["name"] == "wechat-mac-rpa" for r in repos)


def test_match_repository_mention_prefers_exact_and_non_backup():
    from mac_system.feishu.handlers import _match_repository_mention

    candidates = [
        {"name": "wechat-mac-rpa", "path": "/a/wechat-mac-rpa"},
        {"name": "wechat-mac-rpa-backup", "path": "/a/backup"},
        {"name": "other", "path": "/a/other"},
    ]
    assert _match_repository_mention("mac-rpa", candidates)["name"] == "wechat-mac-rpa"
    assert _match_repository_mention("wechat-mac-rpa", candidates)["name"] == "wechat-mac-rpa"
    assert _match_repository_mention("备份", candidates) is None


def test_match_repository_mention_prefers_exact_match_when_possible():
    from mac_system.feishu.handlers import _match_repository_mention

    candidates = [
        {"name": "wechat-mac-rpa", "path": "/a/wechat-mac-rpa"},
        {"name": "mac-rpa", "path": "/a/mac-rpa"},
    ]
    # Exact token wins over substring match.
    assert _match_repository_mention("mac-rpa", candidates)["name"] == "mac-rpa"


def test_match_repository_mention_returns_none_when_ambiguous():
    from mac_system.feishu.handlers import _match_repository_mention

    candidates = [
        {"name": "wechat-mac-rpa", "path": "/a/wechat-mac-rpa"},
        {"name": "project-mac-rpa", "path": "/a/project-mac-rpa"},
    ]
    # Both contain the same token with the same score; should not guess.
    assert _match_repository_mention("mac-rpa", candidates) is None


async def test_conversation_coordinator_gets_worktree_for_any_phrasing_when_bound(
    feishu_ctx, tmp_path
):
    """A bound chat must hand the coordinator its Read worktree regardless of
    how the user phrases the message — no keyword whitelist."""
    from mac_system.feishu.handlers import handle_text_message

    chat_id = "oc_bound_any_phrasing"
    feishu_ctx.chat_id = chat_id
    feishu_ctx.chat_type = "group"
    target_dir = tmp_path / "bound_repo"
    target_dir.mkdir()
    feishu_ctx.service.bind_directory_to_chat(chat_id, str(target_dir), "ou_1")

    phrasings = [
        "测试覆盖率为什么低",
        "这个项目依赖了哪些第三方库",
        "随便聊聊",
        "help me summarize the code structure",
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
        assert fake.agent_calls[0]["worktree"] == target_dir.resolve(), text


async def test_conversation_coordinator_gets_no_worktree_when_unbound(feishu_ctx):
    """Without a binding (and no repo mention) the coordinator runs with no
    worktree — and therefore no filesystem tools."""
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

    assert [item[0] for item in feishu_ctx.bridge.sent] == ["interactive", "interactive"]
    assert "处理完成" in str(feishu_ctx.bridge.updated[-1][1])
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

    assert "本次结果格式不完整" in str(feishu_ctx.bridge.sent[-1][2])
    conversation = feishu_ctx.service.get_or_create_conversation_session(
        "oc_inspect_no_evidence"
    )
    assert "我来查看项目" not in conversation.recent_turns[-1]["content"]


async def test_workspace_inspection_persists_structured_evidence(feishu_ctx):
    """Completed inspection evidence is retained for later execution turns."""
    from mac_system.feishu.handlers import handle_text_message

    feishu_ctx.chat_id = "oc_inspect_evidence"
    feishu_ctx.chat_type = "p2p"
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
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
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
    })

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
        f"已创建项目 {projects[0].id}" in str(item[2])
        for item in feishu_ctx.bridge.sent
    )

    await handle_card_action(feishu_ctx, action)
    assert len(feishu_ctx.service.session.exec(select(Project)).all()) == 1
    assert "已经处理" in feishu_ctx.bridge.sent[-1][2]


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
    assert feishu_ctx.orchestrator.ticked == [project.id]


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
    assert feishu_ctx.orchestrator.ticked == [project.id]


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
    assert project.id in last_text
    refreshed_conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    actual_reply = refreshed_conversation.recent_turns[-1]["content"]
    assert project.id in actual_reply
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

        # The fallback reply must contain the real failure reason, not the
        # legacy misleading timeout wording.
        final_card_text = str(feishu_ctx.bridge.sent[-1])
        assert "No result event received before timeout" in final_card_text, text
        assert "时限" not in final_card_text, text
        assert "结果已整理" not in final_card_text, text

        # The progress card should reflect the failure, not completion.
        failed_update = feishu_ctx.bridge.updated[-1][1]
        assert failed_update["header"]["title"]["content"] == "处理失败", text
        assert "No result event received before timeout" in str(failed_update), text


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


async def test_failed_coordinator_shows_real_reason_no_fake_timeout(feishu_ctx):
    """When the coordinator fails, the user sees the real reason, not 'timeout'."""
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
    assert len(feishu_ctx.orchestrator.agent_calls) == 2  # original + one retry
    final_card_text = str(feishu_ctx.bridge.sent[-1][2])
    assert "upstream socket closed" in final_card_text
    assert "已自动重试 1 次仍无法完成" in final_card_text
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
    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    final_card_text = str(feishu_ctx.bridge.sent[-1][2])
    assert "已帮你取消" in final_card_text

    # Progress card should have shown retrying then completed.
    updated_headers = [
        u[1]["header"]["title"]["content"] for u in feishu_ctx.bridge.updated
    ]
    assert "调用失败，正在重试" in updated_headers
    assert "处理完成" in updated_headers


async def test_bind_no_args_uses_agent_candidates_when_available(
    feishu_ctx, tmp_path, monkeypatch
):
    """When the project_manager agent returns candidates, they appear on the card."""
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

    # Agent was invoked with bind_dir stage.
    assert feishu_ctx.orchestrator.agent_calls
    call = feishu_ctx.orchestrator.agent_calls[0]
    assert call["agent_type"] == "project_manager"
    assert call["stage"] == "bind_dir"
    assert "allowed_bind_roots" in call["extra_variables"]

    cards = [c for c in feishu_ctx.bridge.sent if c[0] == "interactive"]
    assert cards
    payload = cards[-1][2]
    assert str(repo) in str(payload)
    assert "agent-repo" in str(payload)


async def test_bind_no_args_falls_back_when_agent_returns_no_candidates(
    feishu_ctx, tmp_path, monkeypatch
):
    """When the agent returns empty candidates, fall back to deterministic scan."""
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
    """When the agent raises, fall back to deterministic scan."""
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
    """Agent-returned paths outside allowed roots are filtered out."""
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
    assert any(project.id in str(c[2]) for c in feishu_ctx.bridge.sent)


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

    assert any(project.id in str(c[2]) for c in feishu_ctx.bridge.sent)
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
    assert any(project.id in str(c[2]) for c in feishu_ctx.bridge.sent)

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

    sent_text = " ".join(str(c[2]) for c in feishu_ctx.bridge.sent if c[0] == "text")
    assert str(target_dir.resolve()) in sent_text
    assert project.id in sent_text


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
    assert "阻塞问题" in feishu_ctx.bridge.sent[-1][2]


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
    assert any(
        "已绑定到" in str(c[2]) for c in feishu_ctx.bridge.sent if c[0] == "text"
    )


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
    assert any(
        "没有正在等待的创建流程" in str(c[2])
        for c in feishu_ctx.bridge.sent
        if c[0] == "text"
    )


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
    assert any("无法按该方式处理当前审批" in c for c in cards)
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
    conversation = feishu_ctx.service.get_or_create_conversation_session(chat_id)
    assert "已按 select_option 处理" in conversation.recent_turns[-1]["content"]


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
    assert "已按 confirm 处理" in conversation.recent_turns[-1]["content"]
