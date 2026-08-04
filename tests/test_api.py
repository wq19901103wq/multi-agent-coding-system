from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from mac_system.config import Settings
from mac_system.core.project_service import ProjectService


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
    assert client.get("/health").json() == {"status": "ok"}


def test_create_always_uses_native_root(api):
    client, service, _ = api
    response = client.post(
        "/projects",
        json={"name": "demo", "task_type": "bug_fix", "description": "fix it"},
    )
    assert response.status_code == 200, response.text
    project = service.require_project(response.json()["id"])
    assert project.current_state == "ACTIVE"
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
    assert status.json()["current_state"] == "ACTIVE"
    assert status.json()["execution_status"] == "idle"


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
    client, _, _ = api
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


def test_test_endpoints_are_disabled_by_default(api):
    client, service, _ = api
    service.settings.feishu_test_mode = False
    response = client.post(
        "/test/inject_text_message",
        json={"open_id": "ou", "text": "hello", "chat_id": "oc"},
    )
    assert response.status_code == 404
