from __future__ import annotations

import json
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

    async def tick(self, project_id: str) -> None:
        self.ticked.append(project_id)

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        self.cancelled.append((project_id, reason))

    async def run_chat_scoped_agent(
        self, agent_type, stage, input_data, extra_variables=None, *, worktree=None,
        progress_callback=None,
    ):
        self.agent_calls.append({
            "agent_type": agent_type,
            "stage": stage,
            "input_data": input_data,
            "extra_variables": extra_variables,
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
    assert "lark_md" in str(feishu_ctx.bridge.sent[-1][2])


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
    assert any(
        message_id == "msg_2" and "已完成选择" in str(card)
        for message_id, card in feishu_ctx.bridge.updated
    )
    assert "已按 FastAPI" in str(feishu_ctx.bridge.sent[-1][2])


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


async def test_invalid_coordinator_contract_degrades_to_safe_confirmation(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    feishu_ctx.chat_id = "oc_safe_fallback"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": True,
        "result_text": "我会开始处理。",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_safe_fallback",
            text="按刚才的最小范围开始开发",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_safe_fallback",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.status == "pending"
    assert any("conversation_action" in str(item[2]) for item in feishu_ctx.bridge.sent)


async def test_failed_coordinator_degrades_execution_to_safe_confirmation(feishu_ctx):
    from mac_system.feishu.handlers import handle_text_message
    from mac_system.models import PendingConversationAction, Project

    feishu_ctx.chat_id = "oc_failed_fallback"
    feishu_ctx.chat_type = "group"
    feishu_ctx.orchestrator = _FakeOrchestratorWithBindDir({
        "success": False,
        "error": "No result event received before timeout",
    })

    await handle_text_message(
        feishu_ctx,
        TextMessage(
            open_id="ou_1",
            message_id="om_failed_fallback",
            text="按刚才的最小范围开始开发",
            is_command=False,
            is_at_bot=True,
            chat_id="oc_failed_fallback",
            chat_type="group",
        ),
    )

    assert feishu_ctx.service.session.exec(select(Project)).all() == []
    pending = feishu_ctx.service.session.exec(select(PendingConversationAction)).one()
    assert pending.status == "pending"
    assert any("conversation_action" in str(item[2]) for item in feishu_ctx.bridge.sent)

    # The fallback reply must contain the real failure reason, not the legacy
    # misleading timeout wording.
    final_card_text = str(feishu_ctx.bridge.sent[-1][2])
    assert "No result event received before timeout" in final_card_text
    assert "时限" not in final_card_text
    assert "结果已整理" not in final_card_text

    # The progress card should reflect the failure, not completion.
    failed_update = feishu_ctx.bridge.updated[-1][1]
    assert failed_update["header"]["title"]["content"] == "处理失败"
    assert "No result event received before timeout" in str(failed_update)


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
