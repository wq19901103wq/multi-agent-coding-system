import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.config import Settings
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService
from mac_system.core.wechat_notifications import queue_wechat_reminder, retry_wechat_reminder
from mac_system.models import NotificationOutbox, Project, ProjectDecision, QuestionInteraction


@pytest.fixture
def service(tmp_path):
    (tmp_path / "account.json").write_text(json.dumps({
        "botToken": "private-bot-token", "botId": "bot", "userId": "recipient",
        "baseUrl": "https://ilinkai.weixin.qq.com/",
    }))
    (tmp_path / "state.json").write_text(json.dumps({
        "contextTokens": {"bot:recipient": "private-context"},
    }))
    bind = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(bind)
    with Session(bind) as session:
        value = ProjectService(session)
        value.settings = Settings(_env_file=None, WECHAT_NOTIFICATIONS_ENABLED=True,
                                  WECHAT_CREDENTIALS_DIR=tmp_path)
        session.add(Project(id="p", name="Demo", workspace_path=str(tmp_path),
                            feishu_chat_id="oc-demo"))
        session.add(QuestionInteraction(id="q", project_id="p", batch_key="native",
                                        stage="native_approval", requested_to="project_owner"))
        session.commit()
        yield value


def queue(service, interaction=True):
    return queue_wechat_reminder(service, event_key="interaction:q" if interaction else "failure:turn",
                                 project_id="p", interaction_id="q" if interaction else None)


def user_waiting(service):
    interaction = service.get_question_interaction("q")
    interaction.requested_to = "user"
    service.session.add(interaction)
    service.session.commit()
    return interaction


def fake_api(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr("mac_system.core.wechat_notifications.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def test_reminders_require_user_waiting_and_dedupe_without_secrets(service):
    assert queue(service) is None
    user_waiting(service)
    row = queue(service)
    assert queue(service).id == row.id
    assert row.channel == "wechat"
    assert row.recipient_id == "recipient"
    assert "审批" in row.payload["text"]
    assert "openChatId=oc-demo" in row.payload["text"]
    assert "private" not in json.dumps(row.payload)
    service.settings.wechat_notifications_enabled = False
    assert queue(service, False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response_body", [{"ret": 0}, {"message_id": "wechat-message-id"},
                                          {"message_id": 12345678901234567890}])
async def test_delivery_uses_existing_context_and_sends_once(service, monkeypatch, response_body):
    user_waiting(service)
    row = queue(service)
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["msg"]["context_token"] == "private-context"
        assert body["msg"]["to_user_id"] == "recipient"
        assert request.headers["Authorization"] == "Bearer private-bot-token"
        return httpx.Response(200, json=response_body)

    fake_api(monkeypatch, respond)
    await retry_wechat_reminder(service, row)
    await retry_wechat_reminder(service, row)
    assert len(requests) == 1
    assert row.status == "sent"
    assert row.attempt_count == 1
    if "message_id" in response_body:
        assert row.provider_message_id == str(response_body["message_id"])


@pytest.mark.asyncio
async def test_resolved_interaction_cancels_queued_reminder(service, monkeypatch):
    interaction = user_waiting(service)
    row = queue(service)
    interaction.status = "answered"
    service.session.add(interaction)
    service.session.commit()
    fake_api(monkeypatch, lambda _: pytest.fail("resolved interaction must not send"))
    await retry_wechat_reminder(service, row)
    assert row.status == "terminal_failed"
    assert row.attempt_count == 0


@pytest.mark.parametrize("failure,expected", [
    ("business", "terminal_failed"), ("connect", "failed"),
    ("read_timeout", "terminal_failed"), ("rate_limit", "failed"),
    ("unconfirmed", "terminal_failed"), ("conflicting_ack", "terminal_failed"),
])
@pytest.mark.asyncio
async def test_delivery_failure_semantics(service, monkeypatch, failure, expected):
    row = queue(service, False)

    def respond(request):
        if failure == "connect":
            raise httpx.ConnectError("offline", request=request)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("unknown delivery", request=request)
        if failure == "rate_limit":
            return httpx.Response(429)
        if failure == "unconfirmed":
            return httpx.Response(200, json={})
        if failure == "conflicting_ack":
            return httpx.Response(200, json={"ret": -2, "message_id": "not-accepted"})
        return httpx.Response(200, json={"ret": -14, "errmsg": "private provider response"})

    fake_api(monkeypatch, respond)
    await retry_wechat_reminder(service, row)
    assert row.status == expected
    assert "private" not in row.last_error


@pytest.mark.asyncio
async def test_failure_reports_queue_even_if_feishu_fails_and_dedupe(service):
    bridge = type("Bridge", (), {"_send_interactive": AsyncMock(side_effect=httpx.ConnectError("offline"))})()
    for intent, lifecycle in [("progress", "failed"), ("report", "published")]:
        with pytest.raises(httpx.ConnectError):
            await deliver_card(service, bridge, dedupe_key=f"{intent}:{lifecycle}",
                               recipient_id="chat", recipient_type="chat_id", intent_type=intent,
                               card={}, project_id="p", lifecycle_status=lifecycle)
    assert service.session.exec(select(NotificationOutbox).where(NotificationOutbox.channel == "wechat")).all() == []
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await deliver_card(service, bridge, dedupe_key="failed-turn", recipient_id="chat",
                               recipient_type="chat_id", intent_type="report", card={},
                               project_id="p", lifecycle_status="failed")
    assert len(service.session.exec(select(NotificationOutbox).where(NotificationOutbox.channel == "wechat")).all()) == 1


@pytest.mark.asyncio
async def test_outbox_dispatches_wechat_without_feishu_accounts(service, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from mac_system.api import main

    row = queue(service, False)
    row.updated_at = datetime.utcnow() - timedelta(seconds=10)
    service.session.add(row)
    service.session.commit()

    @contextmanager
    def scope():
        yield service.session

    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    send = AsyncMock()
    monkeypatch.setattr(main, "get_feishu_bot_accounts", lambda _: {})
    monkeypatch.setattr(main, "session_scope", scope)
    monkeypatch.setattr(main, "ProjectService", lambda _: service)
    monkeypatch.setattr(main.asyncio, "sleep", tick)
    monkeypatch.setattr(main, "retry_wechat_reminder", send)
    monkeypatch.setattr(main, "retry_notification", AsyncMock(side_effect=AssertionError("wrong channel")))
    with pytest.raises(asyncio.CancelledError):
        await main._drain_notification_outbox(service.session.get_bind(), None, service.settings)
    send.assert_awaited_once_with(service, row)


def owner_choice(service, *, status="requires_user", project_id="p", continue_execution=False):
    decision = ProjectDecision(
        id="choice", project_id=project_id, question="private question",
        decision="private recommendation", rationale="private rationale",
        options=[{"id": "go", "label": "private option"}], status=status,
        decision_type="advice",
        answer_payload={"continue_execution": continue_execution},
    )
    service.session.add(decision)
    service.session.commit()
    return decision


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["advice", "decision"])
async def test_owner_choice_cards_notify_once_across_card_publications(service, intent):
    owner_choice(service)
    bridge = type("Bridge", (), {"_send_interactive": AsyncMock(return_value="card")})()
    for publication in ("initial", "retry", "replacement"):
        await deliver_card(
            service, bridge, dedupe_key=f"{intent}:{publication}",
            recipient_id="chat", recipient_type="chat_id", intent_type=intent,
            card={}, project_id="p", subject_type="project_decision",
            subject_id="choice", lifecycle_status="requires_user",
        )
    rows = service.session.exec(select(NotificationOutbox).where(
        NotificationOutbox.channel == "wechat")).all()
    assert len(rows) == 1
    assert rows[0].dedupe_key == "wechat:decision:choice"
    assert rows[0].payload["decision_id"] == "choice"
    assert "选择" in rows[0].payload["text"]
    assert "Demo" in rows[0].payload["text"]
    assert "openChatId=oc-demo" in rows[0].payload["text"]
    assert "private" not in json.dumps(rows[0].payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["applied", "resolved_by_user"])
async def test_owner_handled_or_answered_choice_does_not_notify(service, status):
    owner_choice(service, status=status)
    bridge = type("Bridge", (), {"_send_interactive": AsyncMock(return_value="card")})()
    await deliver_card(
        service, bridge, dedupe_key="stale-choice-card", recipient_id="chat",
        recipient_type="chat_id", intent_type="advice", card={}, project_id="p",
        subject_type="project_decision", subject_id="choice", lifecycle_status="requires_user",
    )
    assert service.session.exec(select(NotificationOutbox).where(
        NotificationOutbox.channel == "wechat")).all() == []


def test_owner_choice_cannot_notify_another_project(service):
    owner_choice(service, project_id="another-project")
    assert queue_wechat_reminder(
        service, event_key="decision:choice", project_id="p", decision_id="choice",
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["advice", "decision"])
async def test_optional_choice_while_owner_continues_does_not_notify(service, intent):
    owner_choice(service, continue_execution=True)
    bridge = type("Bridge", (), {"_send_interactive": AsyncMock(return_value="card")})()
    await deliver_card(
        service, bridge, dedupe_key="parallel-options", recipient_id="chat",
        recipient_type="chat_id", intent_type=intent, card={}, project_id="p",
        subject_type="project_decision", subject_id="choice", lifecycle_status="requires_user",
    )
    assert service.session.exec(select(NotificationOutbox).where(
        NotificationOutbox.channel == "wechat")).all() == []
    bridge._send_interactive.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded_by", ["answered", "continuing"])
async def test_owner_choice_superseded_before_delivery_is_not_sent(service, monkeypatch, superseded_by):
    decision = owner_choice(service)
    row = queue_wechat_reminder(
        service, event_key="decision:choice", project_id="p", decision_id=decision.id,
    )
    if superseded_by == "answered":
        decision.status = "resolved_by_user"
    else:
        decision.answer_payload = {"continue_execution": True}
    service.session.add(decision)
    service.session.commit()
    fake_api(monkeypatch, lambda _: pytest.fail("answered decision must not be sent"))
    await retry_wechat_reminder(service, row)
    assert row.status == "terminal_failed"
    assert row.attempt_count == 0
    assert "resolved" in row.last_error
