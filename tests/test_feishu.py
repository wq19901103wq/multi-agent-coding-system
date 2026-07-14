import json

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

    async def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> str:
        self.sent.append(("text", open_id, text, receive_id_type))
        return "msg_1"

    async def _send_interactive(self, open_id: str, card: dict, receive_id_type: str = "open_id") -> str:
        self.sent.append(("interactive", open_id, card, receive_id_type))
        return "msg_2"

    async def update_card(self, message_id: str, card: dict) -> None:
        self.updated.append((message_id, card))


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


@pytest.fixture
def service(tmp_path, monkeypatch):
    from mac_system.config import get_settings
    from mac_system.core.project_service import ProjectService

    settings = get_settings()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)

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


async def test_bound_group_member_is_authorized_and_option_payload_is_preserved(
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
        question="选择方案",
        options=[
            {
                "action": "select_option",
                "label": "PostgreSQL",
                "decision_id": "DEC-DB-001",
                "option_key": "postgres",
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
            action="select_option",
            state="ARCHITECTURE_CONFIRM_PENDING",
            decision_id="DEC-DB-001",
            option_key="postgres",
            extras={},
            chat_id="oc_group",
            chat_type="group",
        ),
    )
    updated = feishu_ctx.service.require_project(project.id)
    assert updated.current_state == "PLANNING_ARCHITECTURE"
    assert updated.context["resolved_decisions"][-1] == {
        "decision_id": "DEC-DB-001", "option_key": "postgres"
    }


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
        TextMessage(open_id="ou_1", message_id="m1", text=f"/bind-dir {target_dir}", is_command=True),
    )

    binding = feishu_ctx.service.get_chat_directory_binding("oc_bind")
    assert binding is not None
    assert binding.directory == str(target_dir.resolve())
    assert any("已将当前会话绑定到" in str(c[2]) for c in feishu_ctx.bridge.sent)


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
