import json

import pytest
from sqlmodel import Session, SQLModel, create_engine

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

    async def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> str:
        self.sent.append(("text", open_id, text, receive_id_type))
        return "msg_1"

    async def _send_interactive(self, open_id: str, card: dict, receive_id_type: str = "open_id") -> str:
        self.sent.append(("interactive", open_id, card, receive_id_type))
        return "msg_2"


class FakeOrchestrator:
    def __init__(self):
        self.ticked = []

    async def tick(self, project_id: str) -> None:
        self.ticked.append(project_id)


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
    assert len(feishu_ctx.orchestrator.ticked) == 1


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
    assert len(feishu_ctx.orchestrator.ticked) == 1


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


def test_build_status_card():
    card = build_status_card(
        title="状态",
        project_id="proj_1",
        current_state="CODING",
        display="编码中",
        recent_events=[{"level": "INFO", "message": "开始编码"}],
    )
    assert card["header"]["template"] == "green"


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
