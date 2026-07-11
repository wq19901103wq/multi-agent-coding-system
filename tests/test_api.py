import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import FakeRuntime
from mac_system.api.dependencies import get_orchestrator, get_project_service
from mac_system.api.main import app
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager


def _create_test_engine():
    db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_file.close()
    engine = create_engine(
        f"sqlite:///{db_file.name}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine, db_file.name


@pytest.fixture
def client():
    engine, db_path = _create_test_engine()

    def override_get_project_service():
        with Session(engine) as session:
            yield ProjectService(session)

    def override_get_orchestrator():
        with Session(engine) as session:
            service = ProjectService(session)
            wm = WorktreeManager(service.settings.projects_dir)
            renderer = PromptRenderer(get_agent_config())
            runtime = FakeRuntime({
                "planner": {"success": True, "next_state_event": "complete"},
                "project_manager": {
                    "success": True,
                    "next_state_event": "complete",
                    "reply": "这是 project_manager 的回复。",
                },
            })
            yield AgentOrchestrator(service, wm, runtime, renderer)

    app.dependency_overrides[get_project_service] = override_get_project_service
    app.dependency_overrides[get_orchestrator] = override_get_orchestrator
    yield TestClient(app)
    app.dependency_overrides.clear()
    os.unlink(db_path)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_project(client):
    response = client.post(
        "/projects",
        json={
            "name": "test-project",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "auto",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"].startswith("proj_")
    assert data["current_state"] == "PLANNING_REQUIREMENTS"


def test_get_project(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "test-project",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["id"] == project_id


def test_get_project_not_found(client):
    response = client.get("/projects/proj_nonexistent")
    assert response.status_code == 404


def test_get_status(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "test-project",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.get(f"/projects/{project_id}/status")
    assert response.status_code == 200
    assert response.json()["project_id"] == project_id


def test_submit_message_returns_agent_reply(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "test-project",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.post(
        f"/projects/{project_id}/messages",
        json={"content": "数据库为什么选 SQLite？"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reply"] != "收到：数据库为什么选 SQLite？"
    assert "project_manager" in data["reply"]
    assert data["state"] == create_resp.json()["current_state"]


def test_inject_card_action_disabled_by_default(client, monkeypatch):
    response = client.post(
        "/test/inject_card_action",
        json={
            "open_id": "ou_1",
            "approval_id": "appr_1",
            "action": "confirm",
        },
    )
    assert response.status_code == 404


def test_inject_card_action_confirm(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "feishu_test_mode", True)

    create_resp = client.post(
        "/projects",
        json={
            "name": "test-project",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "normal",
        },
    )
    project_id = create_resp.json()["id"]

    # Advance to REQUIREMENT_CONFIRM_PENDING and create the pending approval.
    response = client.post(f"/test/tick/{project_id}")
    assert response.status_code == 200
    response = client.post(f"/test/tick/{project_id}")
    assert response.status_code == 200

    status_resp = client.get(f"/projects/{project_id}/status")
    pending = status_resp.json()["pending_approval"]
    assert pending is not None

    response = client.post(
        "/test/inject_card_action",
        json={
            "open_id": "ou_1",
            "approval_id": pending["approval_id"],
            "action": "confirm",
            "state": "REQUIREMENT_CONFIRM_PENDING",
        },
    )
    assert response.status_code == 200

    status_resp = client.get(f"/projects/{project_id}/status")
    assert status_resp.json()["current_state"] == "ARCHITECTURE_CONFIRM_PENDING"


def test_inject_card_action_with_group_chat_context(client, monkeypatch):
    """Injecting a card action with chat_id should make replies target the group."""
    settings = get_settings()
    monkeypatch.setattr(settings, "feishu_test_mode", True)

    create_resp = client.post(
        "/projects",
        json={
            "name": "group-card-test",
            "task_type": "feature",
            "description": "实现 config-validator CLI",
            "approval_mode": "normal",
        },
    )
    project_id = create_resp.json()["id"]

    client.post(f"/test/tick/{project_id}")
    client.post(f"/test/tick/{project_id}")

    status_resp = client.get(f"/projects/{project_id}/status")
    pending = status_resp.json()["pending_approval"]
    assert pending is not None

    response = client.post(
        "/test/inject_card_action",
        json={
            "open_id": "ou_1",
            "approval_id": pending["approval_id"],
            "action": "confirm",
            "state": "REQUIREMENT_CONFIRM_PENDING",
            "chat_id": "oc_group_1",
            "chat_type": "group",
        },
    )
    assert response.status_code == 200

    status_resp = client.get(f"/projects/{project_id}/status")
    assert status_resp.json()["current_state"] == "ARCHITECTURE_CONFIRM_PENDING"


def test_admin_projects_panel_lists_projects(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "admin-test-project",
            "task_type": "feature",
            "description": "实现 admin 面板测试",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.get("/admin/projects")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert project_id in response.text


def test_admin_cancel_project(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "admin-cancel-test",
            "task_type": "feature",
            "description": "实现 admin 终止测试",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.post(f"/admin/projects/{project_id}/cancel", follow_redirects=False)
    assert response.status_code == 303

    status_resp = client.get(f"/projects/{project_id}/status")
    assert status_resp.json()["status"] == "cancelled"


def test_admin_rollback_project(client):
    create_resp = client.post(
        "/projects",
        json={
            "name": "admin-rollback-test",
            "task_type": "feature",
            "description": "实现 admin 回退测试",
            "approval_mode": "auto",
        },
    )
    project_id = create_resp.json()["id"]

    response = client.post(
        f"/admin/projects/{project_id}/rollback",
        data={"target_state": "INIT"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    status_resp = client.get(f"/projects/{project_id}/status")
    assert status_resp.json()["current_state"] == "INIT"


def test_admin_token_blocks_unauthorized(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_token", "secret-token")

    response = client.get("/admin/projects")
    assert response.status_code == 403

    response = client.get("/admin/projects?token=secret-token")
    assert response.status_code == 200
