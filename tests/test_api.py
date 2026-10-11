from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from mac_system.api.dependencies import (
    get_orchestrator,
    get_project_service,
    require_control_api_token,
)
from mac_system.api.main import app
import mac_system.api.main as api_main
from mac_system.config import Settings
from mac_system.core.project_service import ProjectService
from mac_system.core.project_files import build_project_file_url
from mac_system.models import EventLog, ProjectOwnerReview


class Orchestrator:
    def __init__(self, service):
        self.project_service = service
        self.runtime = SimpleNamespace()
        self.feishu_bridge = None
        self.cancelled = []

    async def cancel_project(self, project_id, reason=""):
        self.cancelled.append(project_id)
        await self.project_service.cancel_project(project_id, reason)

    async def tick(self, project_id):
        return None


@pytest.fixture
def api(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    service = ProjectService(session)
    service.settings = Settings(
        PROJECTS_DIR=tmp_path / "projects",
        GIT_USER_NAME="MAC Test",
        GIT_USER_EMAIL="mac-test@example.com",
    )
    service.settings.projects_dir.mkdir()
    orchestrator = Orchestrator(service)
    app.dependency_overrides[get_project_service] = lambda: service
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[require_control_api_token] = lambda: None
    client = TestClient(app)
    yield client, service, orchestrator
    app.dependency_overrides.clear()
    session.close()


def test_health(api):
    client, _, _ = api
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["feishu_ws"], dict)


def test_signed_project_file_link_opens_without_control_api_token(api):
    client, service, _ = api
    service.settings = Settings(
        _env_file=None,
        PROJECTS_DIR=service.settings.projects_dir,
        GIT_USER_NAME="MAC Test",
        GIT_USER_EMAIL="mac-test@example.com",
        MAC_PUBLIC_BASE_URL="https://mac.example.test",
        MAC_FILE_LINK_SECRET="test-file-secret",
    )
    response = client.post(
        "/projects",
        json={"name": "files", "task_type": "feature", "description": "files"},
    )
    project = service.require_project(response.json()["id"])
    artifact = service.settings.projects_dir / project.id / "README.md"
    artifact.write_text("手机可以打开", encoding="utf-8")
    public_url = build_project_file_url(project, artifact, service.settings)
    assert public_url is not None
    parsed = urlparse(public_url)

    file_response = client.get(f"{parsed.path}?{parsed.query}")

    assert file_response.status_code == 200
    assert file_response.text == "手机可以打开"
    assert file_response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert file_response.headers["content-disposition"].startswith("inline;")
    assert client.get(f"{parsed.path}?sig=wrong").status_code == 403


def test_owner_has_no_goal_driven_wakeup_scheduler():
    assert not hasattr(api_main, "_claim_due_project_owner_review_ids")
    assert not hasattr(api_main, "_drain_project_owner_wakeups")


@pytest.mark.asyncio
@pytest.mark.parametrize("account_available", [True, False])
async def test_restart_keeps_root_and_owner_in_account_process(api, monkeypatch, account_available):
    import asyncio
    import threading
    from unittest.mock import Mock
    import mac_system.core.root_agent as root_agent
    import mac_system.agents.runtime as runtime_module
    from mac_system.core.root_sessions import RootSessionStore
    from mac_system.feishu.accounts import FeishuBotAccount
    from mac_system.models import Project

    _, service, _ = api
    bind = service.session.get_bind()
    project = Project(id="restart-owner", name="restart", task_type="feature",
                      description="resume", workspace_path="/tmp/restart-owner",
                      bot_account_id="bot-a")
    service.session.add(project)
    service.session.commit()
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-restart")
    store.begin_turn(project.id, user_request="继续当前工作")
    service.session.add(ProjectOwnerReview(
        id="restart-review", project_id=project.id, request_item_id="report:previous",
        status="running", outcome="pending",
    ))
    service.session.commit()

    @contextmanager
    def scope():
        with Session(bind) as session:
            yield session

    async def idle_drain(*args):
        await asyncio.Event().wait()

    account = FeishuBotAccount("bot-a", "app", "secret")
    other_account = FeishuBotAccount("bot-b", "other-app", "secret")
    stop_barrier = threading.Barrier(2)
    stopped = []
    clients = []
    def client_factory(account, **kwargs):
        def stop():
            # Independent account shutdown must overlap, not multiply the
            # native interrupt timeout by the number of bot accounts.
            stop_barrier.wait(timeout=5)
            stopped.append(account.id)
        client = SimpleNamespace(account=account, options=kwargs, start=Mock(), stop=Mock(side_effect=stop))
        clients.append(client)
        return client

    settings = service.settings.model_copy(update={"feishu_transport": "websocket"})
    monkeypatch.setattr(api_main, "init_db", lambda: None)
    monkeypatch.setattr(api_main, "session_scope", scope)
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(api_main, "get_agent_config", lambda: {})
    monkeypatch.setattr(api_main, "get_feishu_bot_accounts", lambda _: {"bot-a": account, "bot-b": other_account} if account_available else {})
    monkeypatch.setattr(api_main, "FeishuWebSocketClient", client_factory)
    monkeypatch.setattr(api_main, "_drain_notification_outbox", idle_drain)
    for name in ("_reconcile_interrupted_root_cards", "_reconcile_owner_assessment_cards", "_reconcile_closed_interaction_projections"):
        monkeypatch.setattr(api_main, name, AsyncMock(return_value=0))
    monkeypatch.setattr(runtime_module, "build_agent_runtime", lambda *args: object())
    monkeypatch.setattr(root_agent, "register_root_service_loop", Mock())
    monkeypatch.setattr(root_agent, "close_root_threads", AsyncMock())
    local_root, local_owner = Mock(), Mock()
    monkeypatch.setattr(root_agent, "schedule_root_turn", local_root)
    monkeypatch.setattr(root_agent, "schedule_project_owner_review", local_owner)
    async with api_main.lifespan(app):
        local_root.assert_not_called()
        local_owner.assert_not_called()
        if account_available:
            assert clients[0].options["startup_owner_reviews"] == ("restart-review",)
            assert clients[0].options["startup_root_turns"][0]["project_id"] == project.id
        else:
            assert not clients
            service.session.expire_all()
            assert store.get_restart_recovery(project.id) is not None
    assert sorted(stopped) == (["bot-a", "bot-b"] if account_available else [])


@pytest.mark.asyncio
async def test_outbox_retries_owner_dispatch_without_resending_report(api, monkeypatch):
    import asyncio
    from unittest.mock import Mock
    import mac_system.core.root_agent as root_agent
    from mac_system.models import Project, NotificationOutbox

    _, service, _ = api
    bind = service.session.get_bind()
    service.session.add(Project(id="pending-owner", name="pending", task_type="feature",
                                description="report delivered", workspace_path="/tmp/pending-owner",
                                bot_account_id="bot-a"))
    service.session.commit()
    service.session.add(ProjectOwnerReview(
        id="pending-review", project_id="pending-owner", request_item_id="report:done", status="pending", outcome="pending",
    ))
    notification = service.enqueue_notification(
        dedupe_key="already-sent-report", recipient_id="chat", recipient_type="chat_id",
        intent_type="report", project_id="pending-owner", payload={},
    )
    service.finish_notification(notification.id, provider_message_id="message-done")
    service.session.commit()

    @contextmanager
    def scope():
        with Session(bind) as session:
            yield session

    iterations = 0
    async def tick(_delay):
        nonlocal iterations
        iterations += 1
        if iterations > 2:
            raise asyncio.CancelledError

    submissions = []
    async def dispatch(project_id, review_id):
        submissions.append((project_id, review_id))
        if len(submissions) == 1:
            raise TimeoutError("acknowledgement lost")
        with Session(bind) as session:
            ProjectService(session).mark_project_owner_review_running(review_id)

    bridge = SimpleNamespace(close=AsyncMock())
    retry_report, local_owner = AsyncMock(), Mock()
    monkeypatch.setattr(api_main.asyncio, "sleep", tick)
    monkeypatch.setattr(api_main, "session_scope", scope)
    monkeypatch.setattr(api_main, "get_feishu_bot_accounts", lambda _: {"bot-a": object()})
    monkeypatch.setattr(api_main, "create_feishu_bridge", lambda *args: bridge)
    monkeypatch.setattr(api_main, "retry_notification", retry_report)
    monkeypatch.setattr(root_agent, "schedule_project_owner_review", local_owner)
    with pytest.raises(asyncio.CancelledError):
        await api_main._drain_notification_outbox(
            bind, object(), SimpleNamespace(feishu_transport="websocket"),
            {"bot-a": SimpleNamespace(submit_owner_review=dispatch)},
        )
    assert submissions == [("pending-owner", "pending-review")] * 2
    retry_report.assert_not_awaited()
    local_owner.assert_not_called()
    service.session.expire_all()
    assert service.session.get(NotificationOutbox, notification.id).status == "sent"
    assert service.session.get(ProjectOwnerReview, "pending-review").status == "running"


def test_create_always_uses_native_root(api):
    client, service, _ = api
    response = client.post(
        "/projects",
        json={"name": "demo", "task_type": "bug_fix", "description": "fix it"},
    )
    assert response.status_code == 200, response.text
    project = service.require_project(response.json()["id"])
    assert "current_state" not in response.json()
    assert "task_type" not in response.json()
    assert project.context["orchestration_mode"] == "root_agent"


def test_project_status_is_execution_posture(api):
    client, service, _ = api
    response = client.post(
        "/projects",
        json={"name": "demo", "task_type": "feature", "description": "build"},
    )
    project_id = response.json()["id"]
    status = client.get(f"/projects/{project_id}/status")
    assert status.status_code == 200
    assert "current_state" not in status.json()
    assert status.json()["execution_status"] == "idle"


def test_admin_profile_exposes_recent_turn_timing(api):
    client, service, _ = api
    project_id = client.post(
        "/projects",
        json={"name": "demo", "task_type": "feature", "description": "build"},
    ).json()["id"]
    timing = {
        "profile_type": "root_turn_timing",
        "turn_id": "turn-1",
        "status": "completed",
        "milestones_ms": {"total": 1200},
    }
    service.session.add(
        EventLog(
            project_id=project_id,
            level="INFO",
            message="Root turn performance",
            meta=timing,
        )
    )
    service.session.commit()

    response = client.get(f"/admin/projects/{project_id}/profile")

    assert response.status_code == 200
    assert response.json()["recent_turn_timings"] == [timing]


def test_pause_resume_cancel(api):
    client, _, orchestrator = api
    project_id = client.post(
        "/projects",
        json={"name": "demo", "task_type": "feature", "description": "build"},
    ).json()["id"]
    assert client.post(
        f"/projects/{project_id}/commands", json={"command": "pause"}
    ).json()["status"] == "paused"
    assert client.post(
        f"/projects/{project_id}/commands", json={"command": "resume"}
    ).json()["status"] == "active"
    assert client.post(
        f"/projects/{project_id}/commands", json={"command": "cancel"}
    ).json()["status"] == "cancelled"
    assert orchestrator.cancelled == [project_id]


def test_rollback_command_no_longer_exists(api):
    client, _, _ = api
    project_id = client.post(
        "/projects",
        json={"name": "demo", "task_type": "feature", "description": "build"},
    ).json()["id"]
    response = client.post(
        f"/projects/{project_id}/commands", json={"command": "rollback"}
    )
    assert response.status_code == 422


def test_missing_project_returns_404(api):
    client, _, _ = api
    assert client.get("/projects/missing").status_code == 404
    assert client.get("/projects/missing/status").status_code == 404


def test_message_ack_is_short_and_project_scoped(api, monkeypatch):
    client, service, _ = api
    service.settings.feishu_transport = "webhook"
    run = AsyncMock()
    monkeypatch.setattr("mac_system.api.main._run_project_message", run)
    project_id = client.post(
        "/projects",
        json={"name": "demo", "task_type": "feature", "description": "build"},
    ).json()["id"]
    response = client.post(
        f"/projects/{project_id}/messages", json={"content": "continue"}
    )
    assert response.status_code == 200
    assert response.json()["project_id"] == project_id
    assert response.json()["reply"] == "收到，正在处理。"
    run.assert_awaited_once_with(project_id, "continue")


@pytest.mark.parametrize("status", ["running", "waiting_user", "idle"])
def test_message_is_delivered_by_owning_account_without_local_resume(api, monkeypatch, status):
    client, service, _ = api
    local_run = AsyncMock()
    owner = SimpleNamespace(submit_project_message=AsyncMock())
    other = SimpleNamespace(submit_project_message=AsyncMock())
    monkeypatch.setattr(api_main, "_run_project_message", local_run)
    monkeypatch.setattr(app.state, "ws_clients", {"bot-a": owner, "bot-b": other}, raising=False)
    project_id = client.post("/projects", json={"name": "owned", "description": "test"}).json()["id"]
    project = service.require_project(project_id)
    project.bot_account_id = "bot-a"
    project.execution_status = status
    service.session.add(project)
    service.session.commit()

    response = client.post(f"/projects/{project_id}/messages", json={"content": "核对当前结果"})

    assert response.status_code == 200
    owner.submit_project_message.assert_awaited_once_with(project_id, "核对当前结果")
    other.submit_project_message.assert_not_awaited()
    local_run.assert_not_awaited()
    service.session.refresh(project)
    assert project.execution_status == status


@pytest.mark.parametrize("failure,code", [(RuntimeError("unavailable"), 503), (TimeoutError(), 504)])
def test_failed_account_handoff_never_starts_another_writer(api, monkeypatch, failure, code):
    client, service, _ = api
    local_run = AsyncMock()
    monkeypatch.setattr(api_main, "_run_project_message", local_run)
    project_id = client.post("/projects", json={"name": "owned", "description": "test"}).json()["id"]
    project = service.require_project(project_id)
    owner = SimpleNamespace(submit_project_message=AsyncMock(side_effect=failure))
    monkeypatch.setattr(app.state, "ws_clients", {project.bot_account_id: owner}, raising=False)
    response = client.post(f"/projects/{project_id}/messages", json={"content": "continue"})
    assert response.status_code == code
    local_run.assert_not_awaited()


def test_configured_account_missing_process_does_not_fall_back(api, monkeypatch):
    client, service, _ = api
    local_run = AsyncMock()
    monkeypatch.setattr(api_main, "_run_project_message", local_run)
    monkeypatch.setattr(app.state, "ws_clients", {}, raising=False)
    project_id = client.post("/projects", json={"name": "owned", "description": "test"}).json()["id"]
    project = service.require_project(project_id)
    service.settings.feishu_transport = "websocket"
    monkeypatch.setattr(api_main, "get_feishu_bot_accounts", lambda _settings: {project.bot_account_id: object()})
    response = client.post(f"/projects/{project_id}/messages", json={"content": "continue"})
    assert response.status_code == 503
    local_run.assert_not_awaited()


def test_test_endpoints_are_disabled_by_default(api):
    client, service, _ = api
    service.settings.feishu_test_mode = False
    response = client.post(
        "/test/inject_text_message",
        json={"open_id": "ou", "text": "hello", "chat_id": "oc"},
    )
    assert response.status_code == 404
