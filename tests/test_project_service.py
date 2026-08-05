from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService
from mac_system.models import Project


class Bridge:
    def __init__(self):
        self.cards = []

    async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
        self.cards.append((recipient, receive_id_type, card))
        return f"message-{len(self.cards)}"

    async def update_card(self, message_id, card):
        self.cards.append((message_id, "update", card))


@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        value = ProjectService(session)
        value.settings.projects_dir = tmp_path / "projects"
        value.settings.projects_dir.mkdir()
        value.settings.git_user_name = "MAC Test"
        value.settings.git_user_email = "mac-test@example.com"
        yield value


@pytest.mark.asyncio
async def test_new_project_is_native_root_and_idle(service):
    project = await service.create_project(
        "demo",
        "feature",
        "build it",
        initial_context={"anything": True},
    )
    assert project.current_state == "ACTIVE"
    assert project.execution_status == "idle"
    assert project.context == {"anything": True, "orchestration_mode": "root_agent"}
    assert (Path(project.workspace_path) / ".git").exists()
    assert (Path(project.workspace_path) / ".gitignore").read_text() == ".mac/\n"


@pytest.mark.asyncio
async def test_chat_project_is_selected_for_creator(service):
    project = await service.create_project(
        "demo",
        "feature",
        "build it",
        creator_open_id="ou-1",
        feishu_chat_id="oc-1",
    )
    assert service.get_chat_user_active_project("oc-1", "ou-1").id == project.id
    assert [item.id for item in service.list_projects_for_chat("oc-1")] == [project.id]


@pytest.mark.asyncio
async def test_pause_resume_cancel_are_lifecycle_only(service):
    project = await service.create_project("demo", "feature", "build")
    await service.pause_project(project.id)
    assert service.require_project(project.id).status == "paused"
    await service.resume_project(project.id)
    assert service.require_project(project.id).status == "active"
    await service.cancel_project(project.id)
    assert service.require_project(project.id).status == "cancelled"
    assert service.require_project(project.id).current_state == "ACTIVE"


@pytest.mark.asyncio
async def test_question_batch_round_trip(service):
    project = await service.create_project("demo", "feature", "build")
    approval = await service.create_approval(
        project.id, None, "ROOT_AGENT_WAITING_USER", "input", "style?", []
    )
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "batch-1",
        [
            {
                "question_id": "style",
                "kind": "decision",
                "question": "style?",
                "options": [
                    {"key": "simple", "label": "Simple"},
                    {"key": "bold", "label": "Bold"},
                ],
            }
        ],
        approval_id=approval.id,
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    assert question.recommended_option == "simple"
    assert not service.interaction_is_resolvable(interaction.id)
    await service.submit_question_answer(
        question.id, "bold", "Bold", False, "ou-1"
    )
    assert service.interaction_is_resolvable(interaction.id)
    await service.resolve_question_interaction(interaction.id)
    await service.respond_approval(approval.id, "answer", "ou-1")
    assert service.get_question_interaction(interaction.id).status == "answered"
    assert service.get_approval(approval.id).status == "responded"


@pytest.mark.asyncio
async def test_notification_dedupes_provider_post(service):
    project = await service.create_project("demo", "feature", "build")
    bridge = Bridge()
    kwargs = dict(
        service=service,
        bridge=bridge,
        dedupe_key="same-card",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        card={"schema": "2.0"},
        project_id=project.id,
    )
    first = await deliver_card(**kwargs)
    second = await deliver_card(**kwargs)
    assert first == second == "message-1"
    assert len(bridge.cards) == 1


def test_scan_candidate_repositories_is_bounded(service, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    too_deep = root / "a" / "b" / "c"
    too_deep.mkdir(parents=True)
    (too_deep / ".git").mkdir()
    assert ProjectService.scan_candidate_repositories([root], max_depth=2) == [
        {"path": str(repo), "name": "repo"}
    ]


def test_project_status_has_no_workflow_projection(service):
    project = Project(
        id="p",
        name="p",
        task_type="feature",
        description="x",
        workspace_path="/tmp/p",
        current_state="ACTIVE",
    )
    service.session.add(project)
    service.session.commit()
    status = service.get_project_status("p")
    assert status["current_state"] == "ACTIVE"
    assert "workflow_steps" not in status


def test_restart_recovery_supersedes_only_stale_projection_updates(service):
    stale = service.enqueue_notification(
        dedupe_key="stale-progress",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        payload={"projection_key": "root-agent:p:activity"},
        project_id=None,
    )
    unrelated = service.enqueue_notification(
        dedupe_key="unrelated-report",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        payload={"projection_key": "root-result:turn-1"},
        project_id=None,
    )

    assert service.supersede_projection_notifications(
        {"root-agent:p:activity"}, reason="restart recovery"
    ) == 1
    service.session.refresh(stale)
    service.session.refresh(unrelated)
    assert stale.status == "terminal_failed"
    assert stale.last_error == "restart recovery"
    assert unrelated.status == "pending"


def test_processed_event_claim_commits_before_async_handler_boundary(service):
    service.mark_event_processed("evt-1", "message")

    assert not service.session.in_transaction()
    assert service.is_event_processed("evt-1") is True
