import json

import pytest
import httpx
from contextlib import contextmanager
from datetime import datetime, timedelta
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.core.project_service import (
    InvalidStateTransitionError,
    ProjectNotFoundError,
    ProjectService,
    StaleApprovalStateError,
)
from mac_system.models import (
    Approval,
    ConversationSession,
    FeedbackPacket,
    NotificationOutbox,
    PendingConversationAction,
    QuestionInteraction,
    Task,
)
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_paths import project_mac_dir


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def service(session, monkeypatch):
    from mac_system.config import get_settings

    monkeypatch.setattr(get_settings(), "mac_allowed_bind_roots", "/")
    return ProjectService(session)


async def test_create_project(service):
    project = await service.create_project(
        name="test-project",
        task_type="feature",
        description="Test project",
    )
    assert project.id.startswith("proj_")
    assert project.name == "test-project"
    assert project.task_type == "feature"
    assert project.current_state == "PLANNING_REQUIREMENTS"
    assert project.status == "active"
    assert project.workspace_path


@pytest.mark.asyncio
async def test_iteration_project_starts_at_coding_with_durable_brief(
    service, tmp_path
):
    repo = tmp_path / "existing"
    repo.mkdir()
    service._initialize_repository(repo)

    project = await service.create_project(
        name="small follow-up",
        task_type="iteration",
        description="Add a bounded status field",
        bound_directory=str(repo),
        initial_context={
            "confirmed_brief": {
                "objective": "Add a bounded status field",
                "scope": ["status field"],
                "out_of_scope": ["storage migration"],
                "acceptance_criteria": ["status is visible from the public API"],
            }
        },
    )

    assert project.current_state == "CODING"
    brief = json.loads(
        (project_mac_dir(project) / "ITERATION_BRIEF.json").read_text(
            encoding="utf-8"
        )
    )
    assert brief["acceptance_criteria"] == [
        {
            "id": "AC-ITER-001",
            "requirement_ids": ["REQ-ITER-001"],
            "statement": "status is visible from the public API",
            "verification_method": "automated_test",
            "evidence_source": "system_observable",
            "evidence_required": "从自动化测试和真实用户入口观察结果",
        }
    ]
    specs = json.loads(
        (project_mac_dir(project) / "SPECS.json").read_text(encoding="utf-8")
    )
    assert specs["status"] == "confirmed"
    project_scope = project.id.rsplit("_", 1)[-1][:8].upper()
    assert specs["artifact_id"] == "specs"
    assert specs["specs"][0]["id"] == f"SPEC-ITER-{project_scope}-001"
    assert specs["specs"][0]["requirement_ids"] == ["REQ-ITER-001"]
    assert specs["specs"][0]["acceptance_criterion_ids"] == ["AC-ITER-001"]
    assert specs["specs"][0]["test_cases"][0]["id"] == f"TC-ITER-{project_scope}-001"


@pytest.mark.asyncio
async def test_iteration_specs_append_to_existing_contract_idempotently(
    service, tmp_path
):
    repo = tmp_path / "existing-with-specs"
    repo.mkdir()
    service._initialize_repository(repo)
    original_spec = {
        "id": "SPEC-BASE-001",
        "title": "Existing behavior",
        "description": "Existing behavior",
        "requirement_ids": ["REQ-BASE-001"],
        "acceptance_criterion_ids": ["AC-BASE-001"],
        "decision_ids": [],
        "acceptance_criteria": ["Existing behavior remains"],
        "dependencies": [],
        "owners": {
            "implementation": "coder",
            "verification": "tester",
            "review": "reviewer",
        },
        "test_cases": [{
            "id": "TC-BASE-001",
            "type": "unit",
            "scenario": "Existing behavior",
            "expected": "Existing behavior remains",
            "acceptance_criterion_ids": ["AC-BASE-001"],
        }],
        "definition_of_done": ["Existing behavior remains"],
    }
    project = await service.create_project(
        name="small follow-up",
        task_type="iteration",
        description="Change one heading",
        bound_directory=str(repo),
        initial_context={
            "confirmed_brief": {
                "objective": "Change one heading",
                "acceptance_criteria": ["Only the heading changes"],
            }
        },
    )
    artifact_path = project_mac_dir(project) / "SPECS.json"
    artifact_path.write_text(
        json.dumps({
            "artifact_id": "specs",
            "version": 2,
            "round": 2,
            "status": "confirmed",
            "specs": [original_spec],
        }),
        encoding="utf-8",
    )
    service.ensure_iteration_specs(project)
    service.ensure_iteration_specs(project)

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    project_scope = project.id.rsplit("_", 1)[-1][:8].upper()
    ids = [item["id"] for item in artifact["specs"]]
    assert ids == ["SPEC-BASE-001", f"SPEC-ITER-{project_scope}-001"]
    assert artifact["version"] == 3
    assert artifact["round"] == 3


@pytest.mark.asyncio
async def test_iteration_project_rejects_missing_repo_or_acceptance_contract(
    service, tmp_path
):
    with pytest.raises(ValueError, match="已绑定"):
        await service.create_project(
            name="not an iteration",
            task_type="iteration",
            description="d",
            initial_context={
                "confirmed_brief": {"acceptance_criteria": ["observable"]}
            },
        )

    repo = tmp_path / "existing-empty-contract"
    repo.mkdir()
    service._initialize_repository(repo)
    with pytest.raises(ValueError, match="验收标准"):
        await service.create_project(
            name="missing contract",
            task_type="iteration",
            description="d",
            bound_directory=str(repo),
            initial_context={"confirmed_brief": {"acceptance_criteria": []}},
        )


@pytest.mark.asyncio
async def test_successful_agent_transition_resets_attempt_history(service):
    project = await service.create_project(
        name="retry-reset",
        task_type="feature",
        description="d",
    )
    project.context = {
        **project.context,
        "retry_counts": {"PLANNING_REQUIREMENTS": 2},
        "agent_retry_feedback": {
            "PLANNING_REQUIREMENTS": "old contract failure",
            "CODING": "keep for another active sequence",
        },
    }
    service.session.commit()

    await service.transition_state(
        project.id,
        "complete",
        triggered_by="agent",
    )

    updated = service.require_project(project.id)
    assert updated.context["retry_counts"]["PLANNING_REQUIREMENTS"] == 0
    assert "PLANNING_REQUIREMENTS" not in updated.context["agent_retry_feedback"]
    assert updated.context["agent_retry_feedback"]["CODING"] == "keep for another active sequence"


@pytest.mark.asyncio
async def test_notification_outbox_deduplicates_sent_card(service):
    class Bridge:
        def __init__(self):
            self.sent = 0

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.sent += 1
            return "msg_1"

    bridge = Bridge()
    kwargs = {
        "dedupe_key": "test:decision:1",
        "recipient_id": "ou_1",
        "recipient_type": "open_id",
        "intent_type": "decision",
        "card": {"header": {"title": "确认"}},
    }
    first = await deliver_card(service, bridge, **kwargs)
    second = await deliver_card(service, bridge, **kwargs)

    assert first == second == "msg_1"
    assert bridge.sent == 1


@pytest.mark.asyncio
async def test_notification_delivery_retries_one_rejected_provider_request(service):
    class Bridge:
        def __init__(self):
            self.sent = 0

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.sent += 1
            if self.sent == 1:
                request = httpx.Request("POST", "https://open.feishu.cn/messages")
                response = httpx.Response(400, request=request)
                raise httpx.HTTPStatusError(
                    "provider rejected card", request=request, response=response
                )
            return "msg_retry"

    bridge = Bridge()
    message_id = await deliver_card(
        service,
        bridge,
        dedupe_key="test:report:retry",
        recipient_id="oc_1",
        recipient_type="chat_id",
        intent_type="report",
        card={"header": {"title": "报告"}},
    )

    assert message_id == "msg_retry"
    assert bridge.sent == 2


def test_notification_outbox_recovers_interrupted_claim(service):
    item = service.enqueue_notification(
        dedupe_key="test:progress:1",
        recipient_id="ou_1",
        recipient_type="open_id",
        intent_type="progress",
        payload={"card": {}},
    )
    service.claim_notification(item.id)

    recovered = service.recover_stuck_notifications(max_age_seconds=0)

    assert [row.id for row in recovered] == [item.id]
    assert service.session.get(type(item), item.id).status == "failed"


def test_notification_outbox_recovers_sent_row_without_provider_ack(service):
    item = service.enqueue_notification(
        dedupe_key="test:invalid-sent:1",
        recipient_id="ou_1",
        recipient_type="open_id",
        intent_type="progress",
        payload={"card": {}},
    )
    item.status = "sent"
    item.provider_message_id = None
    item.last_error = ""
    service.session.add(item)
    service.session.commit()

    recovered = service.recover_stuck_notifications(max_age_seconds=300)

    assert item.id in {row.id for row in recovered}
    refreshed = service.session.get(type(item), item.id)
    assert refreshed.status == "failed"
    assert refreshed.last_error == "delivery interrupted before provider acknowledgement"


def test_notification_retry_does_not_resurrect_stale_user_interactions(service):
    from datetime import datetime, timedelta

    item = service.enqueue_notification(
        dedupe_key="test:stale-question:1",
        recipient_id="ou_1",
        recipient_type="open_id",
        intent_type="question",
        payload={"card": {}},
    )
    item.status = "failed"
    item.updated_at = datetime.utcnow() - timedelta(hours=2)
    service.session.add(item)
    service.session.commit()

    retryable = service.list_retryable_notifications(
        min_age_seconds=0, max_age_seconds=3600
    )

    assert item.id not in {row.id for row in retryable}


@pytest.mark.asyncio
async def test_failed_notification_is_replayed_from_durable_payload(service):
    from mac_system.core.notification_service import retry_notification

    class Bridge:
        def __init__(self):
            self.sent = 0

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.sent += 1
            if self.sent == 1:
                # Some transport errors stringify to an empty string.  They
                # must still be persisted as failures rather than false sends.
                raise httpx.ConnectError("")
            return "msg_recovered"

    bridge = Bridge()
    with pytest.raises(httpx.ConnectError):
        await deliver_card(
            service,
            bridge,
            dedupe_key="test:durable-retry:1",
            recipient_id="oc_1",
            recipient_type="chat_id",
            intent_type="report",
            card={"header": {"title": "最终报告"}},
            projection_key="project:1:report",
            subject_type="project",
            subject_id="project_1",
        )

    item = service.list_retryable_notifications(min_age_seconds=0)[0]
    assert item.status == "failed"
    message_id = await retry_notification(service, bridge, item)

    assert message_id == "msg_recovered"
    refreshed = service.session.get(type(item), item.id)
    assert refreshed.status == "sent"
    assert refreshed.attempt_count == 2
    assert refreshed.provider_message_id == "msg_recovered"


@pytest.mark.asyncio
async def test_permanent_provider_rejection_is_not_retryable(service):
    class Bridge:
        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            request = httpx.Request("POST", "https://open.feishu.cn/messages")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "permanent rejection", request=request, response=response
            )

    with pytest.raises(httpx.HTTPStatusError):
        await deliver_card(
            service,
            Bridge(),
            dedupe_key="test:terminal-delivery:1",
            recipient_id="oc_1",
            recipient_type="chat_id",
            intent_type="report",
            card={"header": {"title": "report"}},
        )

    item = service.session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.dedupe_key == "test:terminal-delivery:1"
        )
    ).one()
    assert item.status == "terminal_failed"
    assert service.list_retryable_notifications(min_age_seconds=0) == []


async def test_create_project_with_creator(service):
    project = await service.create_project(
        name="test-project",
        task_type="feature",
        description="Test project",
        creator_open_id="ou_123",
    )
    from mac_system.models import ProjectUser

    users = service.session.exec(
        select(ProjectUser).where(ProjectUser.project_id == project.id)
    ).all()
    assert len(users) == 1
    assert users[0].open_id == "ou_123"


async def test_reconcile_project_invariants_repairs_terminal_and_missing_approval(service):
    completed = await service.create_project(
        name="completed", task_type="feature", description="done"
    )
    completed.current_state = "COMPLETED"
    completed.status = "active"
    awaiting = await service.create_project(
        name="awaiting", task_type="feature", description="confirm"
    )
    awaiting.current_state = "REQUIREMENT_CONFIRM_PENDING"
    merging = await service.create_project(
        name="merging", task_type="feature", description="finish delivery"
    )
    merging.current_state = "MERGING"
    service.session.commit()

    needing_tick = service.reconcile_project_invariants()

    assert service.require_project(completed.id).status == "completed"
    assert awaiting.id in needing_tick
    assert merging.id in needing_tick


async def test_reconcile_resumes_transitioned_agent_state_without_task(service):
    project = await service.create_project(
        name="transition-gap", task_type="feature", description="resume me"
    )
    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    service.session.commit()

    await service.transition_state(project.id, "confirm", triggered_by="test")

    assert service.require_project(project.id).current_state == "PLANNING_ARCHITECTURE"
    assert project.id in service.reconcile_project_invariants()


async def test_reconcile_resumes_agent_state_after_task_already_failed(service):
    """A failure persisted before shutdown must not strand an active flow."""
    project = await service.create_project(
        name="failed-task-gap", task_type="feature", description="resume me"
    )
    project.current_state = "CODING"
    project.execution_status = "running"
    service.session.commit()

    task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await service.fail_task(task.id, "process exited before follow-up scheduling")

    assert project.id in service.reconcile_project_invariants()


async def test_reconcile_does_not_resume_agent_waiting_for_external_evidence(service):
    project = await service.create_project(
        name="external-wait", task_type="feature", description="wait for user"
    )
    project.current_state = "TESTING"
    project.execution_status = "waiting_external"
    project.waiting_reason = "需要用户在外部设备完成观察"
    service.session.commit()

    assert project.id not in service.reconcile_project_invariants()


async def test_chat_directory_binding_is_inherited_and_project_metadata_is_isolated(
    service, tmp_path
):
    repo = tmp_path / "existing-repo"
    binding = service.bind_directory_to_chat("oc_bound", str(repo), "ou_owner")
    assert binding.directory == str(repo.resolve())

    first = await service.create_project(
        name="first",
        task_type="feature",
        description="one",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_bound",
        feishu_chat_type="group",
    )
    second = await service.create_project(
        name="second",
        task_type="feature",
        description="two",
        creator_open_id="ou_owner",
        feishu_chat_id="oc_bound",
        feishu_chat_type="group",
    )
    assert first.workspace_path == second.workspace_path == str(repo.resolve())
    assert first.bound_directory == str(repo.resolve())
    assert (repo / ".mac" / "projects" / first.id / "STATE.json").exists()
    assert (repo / ".mac" / "projects" / second.id / "STATE.json").exists()

    service.unbind_directory_from_chat("oc_bound")
    assert service.get_chat_directory_binding("oc_bound") is None
    assert first.bound_directory == str(repo.resolve())


def test_directory_binding_rejects_path_outside_allowlist(service, tmp_path, monkeypatch):
    from mac_system.config import get_settings

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(get_settings(), "mac_allowed_bind_roots", str(allowed))

    with pytest.raises(ValueError, match="MAC_ALLOWED_BIND_ROOTS"):
        service.bind_directory_to_chat("oc_outside", str(outside), "ou_owner")


async def test_get_project(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    fetched = service.get_project(project.id)
    assert fetched is not None
    assert fetched.id == project.id


def test_require_project_not_found(service):
    with pytest.raises(ProjectNotFoundError):
        service.require_project("proj_nonexistent")


async def test_transition_state(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    # From PLANNING_REQUIREMENTS we can go to REQUIREMENT_CONFIRM_PENDING via complete
    updated = await service.transition_state(project.id, "complete")
    assert updated.current_state == "REQUIREMENT_CONFIRM_PENDING"
    assert updated.previous_state == "PLANNING_REQUIREMENTS"


async def test_invalid_transition(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    with pytest.raises(InvalidStateTransitionError):
        await service.transition_state(project.id, "confirm")


async def test_artifact_versions_propagate_staleness_and_confirm(service):
    project = await service.create_project(
        name="versions", task_type="feature", description="Test"
    )
    service.update_artifact_version(project.id, "requirements", 1, 1, "confirmed")
    service.update_artifact_version(project.id, "architecture", 1, 1, "confirmed")
    service.update_artifact_version(project.id, "specs", 1, 1, "confirmed")

    service.mark_downstream_artifacts_stale(project.id, "requirements")
    assert service.get_artifact_version(project.id, "architecture")["status"] == "stale"
    assert service.get_artifact_version(project.id, "specs")["status"] == "stale"
    assert {item["artifact_id"] for item in service.get_stale_artifacts(project.id)} == {
        "architecture", "specs"
    }

    project.current_state = "REQUIREMENT_CONFIRM_PENDING"
    service.update_artifact_version(
        project.id, "requirements", 2, 2, "pending_confirmation"
    )
    requirements_path = project_mac_dir(project) / "REQUIREMENTS.json"
    requirements_path.parent.mkdir(parents=True, exist_ok=True)
    requirements_path.write_text(
        json.dumps({
            "artifact_id": "requirements",
            "version": 2,
            "round": 2,
            "status": "pending_confirmation",
        }),
        encoding="utf-8",
    )
    await service.transition_state(project.id, "confirm")
    assert service.get_artifact_version(project.id, "requirements")["status"] == "confirmed"
    assert json.loads(requirements_path.read_text(encoding="utf-8"))["status"] == "confirmed"


async def test_planning_confirmation_keeps_delivery_corrections_open(service):
    project = await service.create_project(
        name="feedback lifecycle", task_type="feature", description="Test"
    )
    artifact_rejection = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_REQUIREMENTS",
        source_task_id=None,
        source_round=1,
        kind="human_rejection",
        summary="requirements wording needs revision",
        recommended_rework_target="requirements",
    )
    delivery_correction = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_REQUIREMENTS",
        source_task_id=None,
        source_round=1,
        kind="correction",
        summary="the public behavior is still wrong",
        recommended_rework_target="requirements",
    )

    service._resolve_confirmed_stage_feedback(
        project.id, "REQUIREMENT_CONFIRM_PENDING"
    )
    service.session.commit()

    assert service.session.get(FeedbackPacket, artifact_rejection.id).status == "resolved"
    assert service.session.get(FeedbackPacket, delivery_correction.id).status == "open"


async def test_create_and_complete_task(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={"description": "plan"},
    )
    assert task.id.startswith("task_")
    assert task.attempt_id.startswith("attempt_")
    assert task.attempt_number == 1
    assert task.context_version == project.context_version
    contract = task.input_data["work_item_contract"]
    assert contract == {
        "contract_version": 2,
        "objective": "plan",
        "owner": "planner",
        "stage": "PLANNING_REQUIREMENTS",
        "stage_kind": "planning",
        "relevant_facts": [],
        "artifact_refs": {},
        "artifact_contract_key": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "artifact_revision_changed": False,
        "constraints": [],
        "acceptance_criteria": [],
        "regression_acceptance_criteria": [],
        "in_scope_acceptance_criterion_ids": [],
        "required_capabilities": [],
        "allowed_deferred_obligations": [],
        "runtime_contract": {},
        "reference": {},
        "feedback_refs": [],
        "feedback_routes": [],
        "completion_conditions": [
            "artifact_schema_valid",
            "no_unresolved_blockers",
            "acceptance_traceability_preserved",
        ],
        "performance_budget": {
            "no_progress_retry_limit": 3,
            "prefer_incremental_verification": True,
        },
        "context_version": project.context_version,
    }
    assert task.status == "pending"

    await service.start_task(task.id, "/tmp/wt")
    started = service.session.get(Task, task.id)
    assert started.status == "running"
    assert started.worktree_path == "/tmp/wt"

    completed = await service.complete_task(
        task.id,
        output_data={"success": True, "next_state_event": "complete"},
        cost_info={"estimated_cost_usd": 0.01},
    )
    assert completed.status == "completed"
    assert completed.cost_usd == 0.01


async def test_delivery_task_expands_scope_when_artifact_versions_change(service):
    project = await service.create_project(
        name="revision scope", task_type="feature", description="Test"
    )
    service.update_artifact_version(project.id, "requirements", 1, 1, "confirmed")
    criteria = [{"id": "AC-1"}, {"id": "AC-2"}]
    feedback = [{
        "feedback_id": "fb-old",
        "evidence": [{"affected_acceptance_criterion_ids": ["AC-1"]}],
    }]

    first = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={
            "description": "implement",
            "open_feedback": feedback,
            "work_item_contract": {"acceptance_criteria": criteria},
        },
    )
    assert first.input_data["work_item_contract"]["artifact_revision_changed"] is True
    assert first.input_data["work_item_contract"][
        "in_scope_acceptance_criterion_ids"
    ] == ["AC-1", "AC-2"]
    await service.complete_task(
        first.id, {"success": True, "next_state_event": "complete"}
    )

    unchanged = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={
            "description": "repair",
            "open_feedback": feedback,
            "work_item_contract": {"acceptance_criteria": criteria},
        },
    )
    assert unchanged.input_data["work_item_contract"][
        "artifact_revision_changed"
    ] is False
    assert unchanged.input_data["work_item_contract"][
        "in_scope_acceptance_criterion_ids"
    ] == ["AC-1"]
    await service.complete_task(
        unchanged.id, {"success": True, "next_state_event": "complete"}
    )

    service.update_artifact_version(project.id, "requirements", 2, 2, "confirmed")
    revised = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={
            "description": "reconcile",
            "open_feedback": feedback,
            "work_item_contract": {"acceptance_criteria": criteria},
        },
    )
    assert revised.input_data["work_item_contract"]["artifact_revision_changed"] is True
    assert revised.input_data["work_item_contract"][
        "in_scope_acceptance_criterion_ids"
    ] == ["AC-1", "AC-2"]


async def test_project_profile_separates_agent_execution_and_card_wait(service):
    base = datetime(2026, 7, 29, 8, 0, 0)
    project = await service.create_project(
        name="profiled", task_type="feature", description="Measure the flow"
    )
    project.created_at = base
    project.updated_at = base + timedelta(seconds=100)
    project.status = "completed"

    task = await service.create_task(
        project.id, "coder", "CODING", {"description": "implement"}
    )
    task.created_at = base + timedelta(seconds=5)
    task.updated_at = base + timedelta(seconds=35)
    task.status = "completed"
    task.cost_usd = 0.25
    task.input_data = {
        **task.input_data,
        "runtime_provider": "codex",
        "_profiling": {
            "execution_started_at": (base + timedelta(seconds=10)).isoformat(),
            "rendered_contract_chars": 12000,
            "system_prompt_chars": 15000,
            "runtime_user_prompt_chars": 64,
            "tool_call_count": 7,
        },
    }
    approval = await service.create_approval(
        project.id,
        task.id,
        "CODING_CONFIRM_PENDING",
        "confirmation",
        "通过吗？",
        [],
        enforce_current_state=False,
    )
    approval.created_at = base + timedelta(seconds=40)
    approval.responded_at = base + timedelta(seconds=70)
    approval.status = "responded"
    await service.log_event(
        project.id,
        "INFO",
        "Conversation coordinator profiled",
        {
            "profile_type": "conversation_coordinator",
            "elapsed_seconds": 12.5,
            "attempts": 2,
            "success": True,
            "message_id": "om_1",
        },
    )
    service.session.add(project)
    service.session.add(task)
    service.session.add(approval)
    service.session.commit()

    profile = service.get_project_profile(project.id, now=base + timedelta(seconds=999))

    assert profile["total_seconds"] == 100.0
    assert profile["agent_execution_seconds"] == 25.0
    assert profile["decision_wait_seconds"] == 30.0
    assert profile["system_unattributed_seconds"] == 45.0
    assert profile["tasks"][0]["provider"] == "codex"
    assert profile["tasks"][0]["timing_source"] == "runtime"
    assert profile["tasks"][0]["rendered_contract_chars"] == 12000
    assert profile["tasks"][0]["runtime_user_prompt_chars"] == 64
    assert profile["tasks"][0]["tool_call_count"] == 7
    assert profile["tasks"][0]["reported_cost_usd"] == 0.25
    assert profile["reported_cost_usd"] == 0.25
    assert profile["cost_is_lower_bound"] is False
    assert profile["agent_type_summary"] == [
        {
            "agent_type": "coder",
            "task_count": 1,
            "status_counts": {"completed": 1},
            "elapsed_seconds": 25.0,
            "reported_cost_usd": 0.25,
            "cost_reporting_task_count": 1,
            "tool_call_count": 7,
            "average_seconds": 25.0,
            "cost_is_lower_bound": False,
        }
    ]
    assert profile["conversation_coordinator_seconds"] == 12.5
    assert profile["conversation_coordinator_runs"] == [
        {
            "event_id": profile["conversation_coordinator_runs"][0]["event_id"],
            "finished_at": profile["conversation_coordinator_runs"][0]["finished_at"],
            "elapsed_seconds": 12.5,
            "attempts": 2,
            "success": True,
            "message_id": "om_1",
        }
    ]


async def test_project_profile_does_not_double_count_agent_and_card_overlap(service):
    base = datetime(2026, 7, 29, 8, 0, 0)
    project = await service.create_project(
        name="overlap-profile", task_type="feature", description="Measure overlap"
    )
    project.created_at = base
    project.updated_at = base + timedelta(seconds=100)
    project.status = "completed"
    task = await service.create_task(project.id, "coder", "CODING", {})
    task.created_at = base + timedelta(seconds=20)
    task.updated_at = base + timedelta(seconds=60)
    task.status = "completed"
    approval = await service.create_approval(
        project.id,
        task.id,
        "CODING",
        "input",
        "Need a decision",
        [],
        enforce_current_state=False,
    )
    approval.created_at = base + timedelta(seconds=10)
    approval.responded_at = base + timedelta(seconds=40)
    approval.status = "responded"
    service.session.add_all([project, task, approval])
    service.session.commit()

    profile = service.get_project_profile(project.id)

    assert profile["agent_execution_seconds"] == 40.0
    assert profile["raw_decision_wait_seconds"] == 30.0
    assert profile["decision_wait_seconds"] == 10.0
    assert profile["system_unattributed_seconds"] == 50.0
    assert (
        profile["agent_execution_seconds"]
        + profile["decision_wait_seconds"]
        + profile["system_unattributed_seconds"]
    ) == profile["total_seconds"]


async def test_task_completion_persists_runtime_profile(service):
    project = await service.create_project(
        name="profile-output", task_type="feature", description="Persist runtime"
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    started_at = datetime.utcnow() - timedelta(seconds=2)
    task.input_data = {
        **task.input_data,
        "_profiling": {"execution_started_at": started_at.isoformat()},
    }
    service.session.add(task)
    service.session.commit()

    completed = await service.complete_task(
        task.id,
        {"next_state_event": "pass"},
        cost_info={
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "estimated_cost_usd": 0.01,
            "model_usage": {"provider_specific": "not persisted"},
            "runtime_attempts": [
                {
                    "provider": "codex",
                    "success": False,
                    "failure_reason": "tool_call_budget_exceeded",
                    "input_tokens": 80,
                    "output_tokens": 10,
                    "total_tokens": 90,
                },
                {
                    "provider": "claude_code",
                    "success": True,
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                    "estimated_cost_usd": 0.01,
                },
            ],
        },
    )

    timing = completed.output_data["_profiling"]
    assert timing["execution_seconds"] >= 1.9
    assert timing["execution_started_at"] == started_at.isoformat()
    assert timing["usage"] == {
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "total_tokens": 150,
        "estimated_cost_usd": 0.01,
    }
    assert timing["runtime_attempts"][0]["failure_reason"] == (
        "tool_call_budget_exceeded"
    )
    assert timing["runtime_attempts"][1]["provider"] == "claude_code"


async def test_new_task_attempt_supersedes_previous_attempt(service):
    project = await service.create_project(
        name="attempts", task_type="feature", description="Test"
    )
    first = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"description": "first"},
    )
    await service.fail_task(first.id, "transient failure")
    second = await service.create_task(
        project.id,
        "coder",
        "CODING",
        {"description": "retry"},
    )

    service.session.refresh(first)
    assert first.attempt_number == 1
    assert first.superseded_by_task_id == second.id
    assert second.attempt_number == 2


async def test_context_records_preserve_history_and_advance_project_version(service):
    project = await service.create_project(
        name="context", task_type="feature", description="Test"
    )
    assert project.context_version == 1

    first = service.create_context_record(
        project.id,
        record_type="requirement",
        record_key="database.engine",
        value={"name": "sqlite"},
        source_type="user",
        source_ref="om_1",
    )
    second = service.create_context_record(
        project.id,
        record_type="requirement",
        record_key="database.engine",
        value={"name": "postgresql"},
        source_type="user",
        source_ref="om_2",
    )

    history = service.get_context_record_history(project.id, "database.engine")
    assert [(item.version, item.status) for item in history] == [
        (1, "superseded"),
        (2, "active"),
    ]
    assert first.valid_until is not None
    assert second.supersedes_id == first.id
    assert service.require_project(project.id).context_version == 3
    assert service.get_active_context_records(project.id) == [second]


async def test_confirmed_brief_is_normalized_and_selected_for_work_item(service):
    project = await service.create_project(
        name="brief-context",
        task_type="feature",
        description="Test",
        initial_context={
            "confirmed_brief": {
                "objective": "实现导出功能",
                "scope": ["CSV"],
                "acceptance_criteria": ["可下载"],
                "decisions": [
                    {
                        "key": "export.format",
                        "answer": "CSV",
                        "status": "confirmed",
                    }
                ],
                "source_session_id": "conv_1",
            }
        },
    )
    active = service.get_active_context_records(project.id)
    assert {record.record_key for record in active} == {
        "project.objective",
        "project.scope",
        "project.acceptance_criteria",
        "export.format",
    }
    assert all(record.source_ref == "conv_1" for record in active)

    task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={"description": "implement"},
    )
    facts = task.input_data["work_item_contract"]["relevant_facts"]
    assert {fact["key"] for fact in facts} == {
        "project.objective",
        "project.scope",
        "project.acceptance_criteria",
        "export.format",
    }
    assert task.context_version == service.require_project(project.id).context_version


async def test_legacy_credential_fact_uses_origin_interaction_scope(service):
    project = await service.create_project(
        name="credential-scope", task_type="feature", description="Test"
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="CODING",
        agent="coder",
        stage="coding",
        batch_key="credential",
        questions=[{
            "question_id": "q_key",
            "kind": "blocking",
            "question": "credential?",
            "reason": "runtime dependency",
            "allow_free_text": True,
            "options": [],
            "affected_trace_ids": ["AC-001"],
            "sensitivity": "secret",
            "secret_env_var": "EXAMPLE_API_KEY",
        }],
    )
    service.create_context_record(
        project.id,
        record_type="capability",
        record_key="runtime.example_credential",
        value={
            "available": True,
            "secret_ref": "env:EXAMPLE_API_KEY",
            "question_id": "q_key",
        },
        source_type="user",
        source_ref=interaction.id,
    )
    service.create_context_record(
        project.id,
        record_type="capability",
        record_key="runtime.env.example",
        value={
            "source_file": "/allowed/source/.env",
            "env_bindings": {"EXAMPLE_API_KEY": "SOURCE_API_KEY"},
            "applies_to": ["coder", "tester"],
        },
        source_type="user",
        source_ref="om_binding",
    )

    coder = service.build_relevant_context(
        project.id, agent_type="coder", state="CODING"
    )
    specs = service.build_relevant_context(
        project.id,
        agent_type="spec_writer",
        state="PLANNING_IMPLEMENTATION_SPECS",
    )

    assert {fact["key"] for fact in coder} == {
        "runtime.example_credential",
        "runtime.env.example",
    }
    assert all(fact["affected_trace_ids"] == ["AC-001"] for fact in coder)
    assert specs == []


async def test_task_freshness_detects_newer_context_without_changing_task(service):
    project = await service.create_project(
        name="freshness", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state=project.current_state,
        input_data={
            "description": "implement",
            "work_item_contract": {
                "relevant_facts": ["database.engine@1"],
                "acceptance_criteria": ["tests pass"],
            },
        },
    )
    assert service.assess_task_freshness(task.id)["fresh"] is True

    service.create_context_record(
        project.id,
        record_type="decision",
        record_key="database.engine",
        value={"name": "sqlite"},
        source_type="user",
    )
    freshness = service.assess_task_freshness(task.id)
    assert freshness["fresh"] is False
    assert freshness["task_context_version"] == 1
    assert freshness["project_context_version"] == 2
    assert "context_version changed" in freshness["reasons"][0]
    assert service.session.get(Task, task.id).status == "pending"


async def test_blocking_user_input_advances_context_but_question_does_not(service):
    project = await service.create_project(
        name="input-version", task_type="feature", description="Test"
    )
    original_version = project.context_version
    await service.create_pending_user_input(
        project.id,
        sender_id="ou_1",
        observed_state=project.current_state,
        content="现在进展如何？",
        kind="question",
    )
    assert service.require_project(project.id).context_version == original_version

    correction = await service.create_pending_user_input(
        project.id,
        sender_id="ou_1",
        observed_state=project.current_state,
        content="数据库改成 PostgreSQL",
        kind="correction",
    )
    updated = service.require_project(project.id)
    assert updated.context_version == original_version + 1
    record = next(
        item
        for item in service.get_active_context_records(project.id)
        if item.record_key == f"user_input.{correction.id}"
    )
    assert record.value["content"] == "数据库改成 PostgreSQL"


async def test_create_approval(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    await service.transition_state(project.id, "complete")
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        options=[
            {"action": "confirm", "label": "Confirm", "next_state": "PLANNING_ARCHITECTURE"},
            {"action": "reject", "label": "Reject", "next_state": "PLANNING_REQUIREMENTS"},
        ],
    )
    assert approval.id.startswith("appr_")
    assert approval.status == "pending"

    responded = await service.respond_approval(approval.id, "confirm", "ou_123")
    assert responded.status == "responded"
    assert responded.response == "confirm"
    assert responded.responded_by == "ou_123"


async def test_create_approval_rejects_state_left_by_concurrent_tick(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )

    with pytest.raises(StaleApprovalStateError):
        await service.create_approval(
            project_id=project.id,
            task_id=None,
            state="ARCHITECTURE_CONFIRM_PENDING",
            approval_type="confirmation",
            question="Stale?",
            options=[],
        )

    assert service.get_project_status(project.id)["pending_approval"] is None


async def test_transition_expires_duplicate_pending_approval_for_state_left(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    await service.transition_state(project.id, "complete")
    selected = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        options=[{"action": "confirm", "label": "Confirm"}],
    )
    duplicate = Approval(
        id="appr_concurrent_duplicate",
        project_id=project.id,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        payload={"options": [{"action": "confirm", "label": "Confirm"}]},
        status="pending",
    )
    service.session.add(duplicate)
    service.session.commit()

    _, updated = await service.resolve_approval(
        selected.id, "confirm", responded_by="ou_123"
    )

    service.session.refresh(duplicate)
    assert updated.current_state == "PLANNING_ARCHITECTURE"
    assert duplicate.status == "expired"
    assert service.get_project_status(project.id)["pending_approval"] is None


async def test_complete_task_accumulates_cost(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state="PLANNING_REQUIREMENTS",
        input_data={"description": "plan"},
    )
    await service.start_task(task.id, "/tmp/wt")
    await service.complete_task(
        task.id,
        output_data={"success": True},
        cost_info={"estimated_cost_usd": 0.42},
    )
    updated_project = service.get_project(project.id)
    assert updated_project.total_cost_usd == 0.42

    task2 = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    await service.start_task(task2.id, "/tmp/wt")
    await service.complete_task(
        task2.id,
        output_data={"success": True},
        cost_info={"estimated_cost_usd": 0.18},
    )
    updated_project = service.get_project(project.id)
    assert updated_project.total_cost_usd == pytest.approx(0.60)


def test_recover_stale_tasks(service):
    import asyncio

    project = asyncio.run(
        service.create_project(name="test", task_type="feature", description="Test")
    )
    task = asyncio.run(
        service.create_task(
            project_id=project.id,
            agent_type="planner",
            state="PLANNING_REQUIREMENTS",
            input_data={},
        )
    )
    asyncio.run(service.start_task(task.id, "/tmp/wt"))

    recovered = service.recover_stale_tasks()
    assert len(recovered) == 1
    assert recovered[0].id == task.id
    assert recovered[0].status == "failed"
    assert "restarted" in recovered[0].error_message


def test_recover_stale_tasks_empty(service):
    assert service.recover_stale_tasks() == []


async def test_restart_recovery_reschedules_active_agent_project(
    service, session, monkeypatch
):
    from mac_system.api import main as api_main

    project = await service.create_project(
        name="restart-recovery", task_type="feature", description="Test"
    )
    task = await service.create_task(
        project_id=project.id,
        agent_type="planner",
        state=project.current_state,
        input_data={},
    )
    await service.start_task(task.id, "/tmp/wt")
    service.recover_stale_tasks()

    @contextmanager
    def same_session_scope():
        yield session

    ticked: list[str] = []

    class StubOrchestrator:
        feishu_bridge = None

        async def tick(self, project_id: str) -> None:
            ticked.append(project_id)

    monkeypatch.setattr(api_main, "session_scope", same_session_scope)
    monkeypatch.setattr(api_main, "_build_orchestrator", lambda _session: StubOrchestrator())

    await api_main._resume_recovered_project(project.id)

    assert ticked == [project.id]


async def test_full_crash_restart_lifecycle_marks_task_failed_and_resumes(
    service, session, monkeypatch, tmp_path
):
    """B7: simulate the full crash → restart → resume path end-to-end.

    Sequence:
      1. Project is mid-execution (task running).
      2. Server process is killed (simulated by recover_stale_tasks).
      3. On startup, reconcile_project_invariants runs.
      4. _resume_recovered_project schedules a tick for each recovered project.

    Asserts:
      - Running task transitioned to "failed" with a restart error message.
      - Project remains in its pre-crash state (state machine intact).
      - Reconcile identifies the project as needing a tick.
      - Resume triggers orchestrator.tick for the recovered project.
    """
    from mac_system.api import main as api_main

    project = await service.create_project(
        name="crash-lifecycle", task_type="feature", description="Test"
    )
    # Force the project into an agent-execution state so the invariant checker
    # treats it as resumable.
    project.current_state = "CODING"
    service.session.add(project)
    service.session.commit()

    running_task = await service.create_task(
        project_id=project.id,
        agent_type="coder",
        state="CODING",
        input_data={},
    )
    worktree = tmp_path / "coder-worktree"
    worktree.mkdir()
    (worktree / "partial.py").write_text("partial = True\n")
    await service.start_task(running_task.id, str(worktree))

    # Step 1: server killed → on next startup, recover_stale_tasks runs first.
    recovered = service.recover_stale_tasks()
    assert [t.id for t in recovered] == [running_task.id]

    service.session.refresh(running_task)
    assert running_task.status == "failed"
    assert "restarted" in (running_task.error_message or "")

    # Step 2: project state is unchanged (crash did not corrupt state machine).
    service.session.refresh(project)
    assert project.current_state == "CODING"
    assert project.status == "active"
    assert project.context["_resumable_agent_worktree"] == {
        "task_id": running_task.id,
        "agent_type": "coder",
        "state": "CODING",
        "worktree_path": str(worktree),
        "mode": "service_restart",
    }
    assert (worktree / "partial.py").read_text() == "partial = True\n"

    # Step 3: the project invariant is independently sufficient to resume.
    # This also covers a shutdown that happens after the task was marked
    # failed but before its project id was retained in process memory.
    needing_tick = service.reconcile_project_invariants()
    assert project.id in needing_tick
    recovered_project_ids = {t.project_id for t in recovered}
    assert project.id in recovered_project_ids

    # Step 4: _resume_recovered_project triggers an orchestrator tick.
    @contextmanager
    def same_session_scope():
        yield session

    ticked: list[str] = []

    class StubOrchestrator:
        feishu_bridge = None

        async def tick(self, project_id: str) -> None:
            ticked.append(project_id)

    monkeypatch.setattr(api_main, "session_scope", same_session_scope)
    monkeypatch.setattr(api_main, "_build_orchestrator", lambda _session: StubOrchestrator())

    await api_main._resume_recovered_project(project.id)
    assert ticked == [project.id]


async def test_restart_recovery_continues_across_changed_system_state(
    service, session, monkeypatch
):
    """Cached auto-acceptance must not strand the project in MERGING."""
    from mac_system.api import main as api_main

    project = await service.create_project(
        name="restart-auto-acceptance", task_type="feature", description="Test"
    )
    project.current_state = "ACCEPTANCE_PENDING"
    service.session.add(project)
    service.session.commit()

    @contextmanager
    def same_session_scope():
        yield session

    ticked_states: list[str] = []

    class StubOrchestrator:
        feishu_bridge = None

        async def tick(self, project_id: str) -> None:
            current = service.require_project(project_id)
            ticked_states.append(current.current_state)
            current.current_state = (
                "MERGING"
                if current.current_state == "ACCEPTANCE_PENDING"
                else "COMPLETED"
            )
            if current.current_state == "COMPLETED":
                current.status = "completed"
            service.session.add(current)
            service.session.commit()

    monkeypatch.setattr(api_main, "session_scope", same_session_scope)
    monkeypatch.setattr(
        api_main, "_build_orchestrator", lambda _session: StubOrchestrator()
    )

    await api_main._resume_recovered_project(project.id)

    assert ticked_states == ["ACCEPTANCE_PENDING", "MERGING"]
    assert service.require_project(project.id).current_state == "COMPLETED"


async def test_pending_intent_crud(service):
    intent = service.upsert_pending_intent(
        open_id="ou_123",
        description="实现一个 config-validator CLI",
        stage="awaiting_type_confirm",
    )
    assert intent.open_id == "ou_123"
    assert intent.stage == "awaiting_type_confirm"

    fetched = service.get_pending_intent("ou_123")
    assert fetched is not None
    assert fetched.description == "实现一个 config-validator CLI"

    service.delete_pending_intent("ou_123")
    assert service.get_pending_intent("ou_123") is None


async def test_pending_intent_survives_new_service(session):
    """Simulate a service restart by creating a fresh ProjectService on the same DB."""
    service1 = ProjectService(session)
    service1.upsert_pending_intent(
        open_id="ou_456",
        description="修复 port 校验范围 bug",
        stage="awaiting_type_confirm",
    )

    service2 = ProjectService(session)
    intent = service2.get_pending_intent("ou_456")
    assert intent is not None
    assert intent.description == "修复 port 校验范围 bug"


async def test_pending_intent_expires_after_one_hour(service):
    from datetime import timedelta

    intent = service.upsert_pending_intent(
        open_id="ou_789",
        description="写一份 README",
        stage="awaiting_description",
    )
    # Manually age the record beyond the TTL.
    intent.created_at = intent.created_at - timedelta(hours=2)
    service.session.commit()

    assert service.get_pending_intent("ou_789") is None


async def test_night_mode_inside_window_uses_night_config(service, monkeypatch):
    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: True)
    )
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )
    # Night config auto-resolves clarifications/conflicts and only requires
    # REQUIREMENT_CONFIRM_PENDING and ACCEPTANCE_PENDING.
    assert project.approval_config.get("auto_resolve_clarification") is True
    assert "REQUIREMENT_CONFIRM_PENDING" in project.approval_config.get(
        "require_confirmation_for", []
    )


async def test_night_mode_outside_window_falls_back_to_normal(service, monkeypatch):
    monkeypatch.setattr(
        ProjectService, "_in_night_window", staticmethod(lambda window: False)
    )
    project = await service.create_project(
        name="test",
        task_type="bug_fix",
        description="修复 port 校验范围 bug",
        approval_mode="night",
    )
    # Outside the night window, night mode behaves like normal.
    assert project.approval_config.get("auto_resolve_clarification") is True
    assert project.approval_config.get("auto_resolve_strategy") == "conversation_coordinator"
    assert project.approval_config.get("require_confirmation_for_types") == []


async def test_reconcile_migrates_active_normal_approval_policy(service):
    project = await service.create_project(
        name="legacy-normal",
        task_type="feature",
        description="继续已有项目",
        approval_mode="normal",
    )
    project.current_state = "TESTING_CONFIRM_PENDING"
    project.approval_config = {
        "require_confirmation_for_types": ["human_confirmation"],
        "auto_resolve_strategy": "none",
    }
    service.session.commit()

    needing_tick = service.reconcile_project_invariants()

    refreshed = service.require_project(project.id)
    assert refreshed.approval_config["auto_resolve_strategy"] == "conversation_coordinator"
    assert refreshed.approval_config["require_confirmation_for_types"] == []
    assert project.id in needing_tick


def test_night_window_wrap_around_midnight():
    """A 23:00-08:00 window should be active at midnight and inactive at noon."""
    window = {"start_time": "23:00", "end_time": "08:00", "timezone": "Asia/Shanghai"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(window.get("timezone", "Asia/Shanghai"))

    midnight = datetime(2026, 1, 1, 1, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=midnight) is True

    noon = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=noon) is False

    edge_start = datetime(2026, 1, 1, 23, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=edge_start) is True

    edge_end = datetime(2026, 1, 1, 8, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=edge_end) is False


def test_night_window_non_wrapping():
    """A 09:00-18:00 window should be active during the day."""
    window = {"start_time": "09:00", "end_time": "18:00", "timezone": "Asia/Shanghai"}
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(window.get("timezone", "Asia/Shanghai"))
    during = datetime(2026, 1, 1, 12, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=during) is True

    before = datetime(2026, 1, 1, 8, 0, tzinfo=tz)
    assert ProjectService._in_night_window(window, now=before) is False


def test_event_dedup(service):
    assert service.is_event_processed("evt_1") is False
    service.mark_event_processed("evt_1", "message")
    service.session.commit()
    assert service.is_event_processed("evt_1") is True


async def test_create_project_binds_chat_and_context(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
        creator_open_id="ou_123",
        feishu_chat_id="chat_abc",
        feishu_chat_type="group",
    )
    from mac_system.models import ProjectChat, ChatUserContext

    chats = service.session.exec(
        select(ProjectChat).where(ProjectChat.project_id == project.id)
    ).all()
    assert len(chats) == 1
    assert chats[0].chat_id == "chat_abc"

    ctx = service.session.exec(
        select(ChatUserContext).where(
            ChatUserContext.chat_id == "chat_abc",
            ChatUserContext.open_id == "ou_123",
        )
    ).first()
    assert ctx is not None
    assert ctx.active_project_id == project.id


async def test_chat_user_context_routing(service):
    project = await service.create_project(
        name="test",
        task_type="feature",
        description="Test",
        creator_open_id="ou_123",
        feishu_chat_id="chat_abc",
        feishu_chat_type="group",
    )
    active = service.get_chat_user_active_project("chat_abc", "ou_123")
    assert active is not None
    assert active.id == project.id

    service.set_chat_user_context("chat_abc", "ou_123", "proj_nonexistent")
    assert service.get_chat_user_active_project("chat_abc", "ou_123") is None


async def test_chat_user_context_without_project_returns_none_without_lookup(service):
    from mac_system.models import ChatUserContext

    service.session.add(ChatUserContext(
        chat_id="chat_empty", open_id="ou_empty", active_project_id=None
    ))
    service.session.commit()

    assert service.get_chat_user_active_project("chat_empty", "ou_empty") is None


async def test_chat_user_active_project_excludes_terminal_selected_history(service):
    project = await service.create_project(
        name="finished",
        task_type="feature",
        description="done",
        creator_open_id="ou_terminal",
        feishu_chat_id="chat_terminal",
    )
    project.status = "completed"
    project.current_state = "COMPLETED"
    service.session.add(project)
    service.session.commit()

    assert (
        service.get_chat_user_active_project("chat_terminal", "ou_terminal")
        is None
    )
    selected = service.get_chat_user_selected_project(
        "chat_terminal", "ou_terminal"
    )
    assert selected is not None
    assert selected.id == project.id


async def test_bind_directory_clears_stale_chat_project_selection(
    service, tmp_path,
):
    from mac_system.models import ChatUserContext, ConversationSession

    project = await service.create_project(
        name="old-context",
        task_type="feature",
        description="old",
        creator_open_id="ou_bind",
    )
    conversation = service.get_or_create_conversation_session("chat_bind")
    service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    service.set_chat_user_context("chat_bind", "ou_bind", project.id)
    repository = tmp_path / "new-repository"

    service.bind_directory_to_chat("chat_bind", str(repository), "ou_bind")

    refreshed = service.get_or_create_conversation_session("chat_bind")
    user_context = service.session.get(
        ChatUserContext, ("chat_bind", "ou_bind")
    )
    assert refreshed.active_project_id is None
    assert user_context is not None
    assert user_context.active_project_id is None


async def test_list_projects_for_chat(service):
    p1 = await service.create_project(
        name="p1", task_type="feature", description="p1", feishu_chat_id="chat_x"
    )
    p2 = await service.create_project(
        name="p2", task_type="feature", description="p2", feishu_chat_id="chat_x"
    )
    await service.cancel_project(p2.id)
    projects = service.list_projects_for_chat("chat_x")
    assert [p.id for p in projects] == [p1.id]


async def test_cancelled_project_cannot_transition_from_late_agent_result(service):
    project = await service.create_project(
        name="cancel-race", task_type="feature", description="Test"
    )
    await service.cancel_project(project.id)

    with pytest.raises(InvalidStateTransitionError):
        await service.transition_state(project.id, "complete")

    refreshed = service.get_project(project.id)
    assert refreshed.status == "cancelled"
    assert refreshed.current_state == "PLANNING_REQUIREMENTS"
    assert refreshed.execution_status == "idle"
    assert refreshed.waiting_reason is None


async def test_cancel_project_clears_conversation_session_and_pending_actions(service):
    project = await service.create_project(
        name="cancel-cleanup", task_type="feature", description="Test"
    )
    chat_id = "chat_cleanup"
    conversation = service.get_or_create_conversation_session(chat_id)
    service.update_conversation_session(
        conversation,
        active_project_id=project.id,
        update_active_project=True,
    )
    action = service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="confirm_plan",
        payload={"key": "value"},
        source_message_ids=["m1"],
        idempotency_key="key1",
        created_by="ou_1",
        target_project_id=project.id,
    )

    await service.cancel_project(project.id)

    refreshed = service.session.get(ConversationSession, conversation.id)
    assert refreshed.active_project_id is None
    assert refreshed.pending_action_id is None

    refreshed_action = service.session.get(PendingConversationAction, action.id)
    assert refreshed_action.status == "expired"


def test_multiple_conversation_clarifications_can_remain_pending(service):
    conversation = service.get_or_create_conversation_session("chat_questions")
    first = service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="clarification",
        payload={"question": "第一个问题"},
        source_message_ids=["m1"],
        idempotency_key="clarification-1",
        created_by="ou_1",
        expire_existing=False,
    )
    second = service.create_pending_conversation_action(
        session_id=conversation.id,
        action_type="clarification",
        payload={"question": "第二个问题"},
        source_message_ids=["m2"],
        idempotency_key="clarification-2",
        created_by="ou_1",
        expire_existing=False,
    )

    pending = service.get_session_pending_actions(conversation.id)
    assert {item.id for item in pending} == {first.id, second.id}
    refreshed = service.session.get(ConversationSession, conversation.id)
    assert refreshed.pending_action_id == second.id


async def test_create_approval_expires_existing_pending(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    await service.transition_state(project.id, "complete")
    old = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Old?",
        options=[],
    )
    assert old.status == "pending"

    new = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="New?",
        options=[],
    )
    service.session.refresh(old)
    assert old.status == "expired"
    assert new.status == "pending"


async def test_resolve_approval_is_atomic(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    await service.transition_state(project.id, "complete")
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="REQUIREMENT_CONFIRM_PENDING",
        approval_type="confirmation",
        question="Confirm?",
        options=[{"action": "confirm", "label": "Confirm"}],
    )
    responded, updated = await service.resolve_approval(
        approval.id, "confirm", responded_by="ou_123"
    )
    assert responded.status == "responded"
    assert updated.current_state == "PLANNING_ARCHITECTURE"


async def test_resolve_free_text_input_without_button_options(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    project.current_state = "CLARIFICATION_PENDING"
    service.session.add(project)
    service.session.commit()
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="CLARIFICATION_PENDING",
        approval_type="input",
        question="请确认或补充后回复",
        options=[],
    )

    responded, updated = await service.resolve_approval(
        approval.id,
        "answer",
        responded_by="api_user",
        payload={"clarification_answer": "确认，以此为准"},
    )

    assert responded.status == "responded"
    assert responded.response == "answer"
    assert updated.current_state == "CODING"


async def test_resolve_input_can_route_correction_to_upstream_stage(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    project.current_state = "CLARIFICATION_PENDING"
    service.session.add(project)
    service.session.commit()
    approval = await service.create_approval(
        project_id=project.id,
        task_id=None,
        state="CLARIFICATION_PENDING",
        approval_type="input",
        question="请选择无法满足的外部验收方式",
        options=[],
    )

    responded, updated = await service.resolve_approval(
        approval.id,
        "needs_spec_change",
        responded_by="api_user",
        payload={"message": "该验收方式不是用户要求，请修订实现计划。"},
    )

    assert responded.status == "responded"
    assert responded.response == "needs_spec_change"
    assert updated.current_state == "PLANNING_IMPLEMENTATION_SPECS"


async def test_feedback_packet_commit_param(service):
    project = await service.create_project(
        name="test", task_type="feature", description="Test"
    )
    packet = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="x",
        commit=False,
    )
    # Not committed yet
    assert service.session.get(FeedbackPacket, packet.id) is packet
    service.session.commit()
    assert service.session.get(FeedbackPacket, packet.id) is not None


@pytest.mark.asyncio
async def test_confirmed_artifact_keeps_delivery_correction_and_quality_feedback_open(service):
    project = await service.create_project(
        name="feedback-lifecycle", task_type="feature", description="Test"
    )
    project.current_state = "ARCHITECTURE_CONFIRM_PENDING"
    service.session.commit()
    correction = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="PLANNING_ARCHITECTURE",
        source_task_id=None,
        source_round=2,
        kind="correction",
        summary="补充降级策略",
        recommended_rework_target="architecture",
    )
    quality_failure = await service.create_feedback_packet(
        project_id=project.id,
        source_stage="TESTING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="真实测试失败",
        recommended_rework_target="architecture",
    )

    await service.transition_state(project.id, "confirm", triggered_by="ou_user")

    assert service.session.get(FeedbackPacket, correction.id).status == "open"
    assert service.session.get(FeedbackPacket, quality_failure.id).status == "open"


# ---------------------------------------------------------------------------
# QuestionInteraction / Question / QuestionAnswer
# ---------------------------------------------------------------------------


async def test_create_question_interaction_with_questions(service):
    project = await service.create_project(name="qi-test", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="req_batch_1",
        questions=[
            {"question_id": "q_scope", "kind": "blocking", "question": "范围？",
             "reason": "不明确", "allow_free_text": False,
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True},
                         {"key": "b", "label": "B", "impact": "", "recommended": False}],
             "affected_trace_ids": ["REQ-001"]},
            {"question_id": "q_extra", "kind": "non_blocking", "question": "补充？",
             "reason": "", "allow_free_text": True, "options": [], "affected_trace_ids": []},
        ],
    )
    assert interaction.id.startswith("qi_")
    assert interaction.status == "pending"
    questions = service.get_questions_for_interaction(interaction.id)
    assert len(questions) == 2
    assert questions[0].question_id == "q_scope"
    assert questions[0].recommended_option == "a"
    assert questions[1].allow_free_text is True


async def test_submit_answer_and_resolvable(service):
    project = await service.create_project(name="qi-r", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "kind": "blocking", "question": "Q1",
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": []},
            {"question_id": "q2", "kind": "non_blocking", "question": "Q2",
             "options": [], "allow_free_text": True, "affected_trace_ids": []},
        ],
    )
    assert service.interaction_is_resolvable(interaction.id) is False
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(questions[0].id, "a", "A", False, "ou_user")
    assert service.interaction_is_resolvable(interaction.id) is True
    resolved = await service.resolve_question_interaction(interaction.id)
    assert resolved.status == "answered"


async def test_resolve_question_interaction_syncs_answers_to_confirmed_brief(service):
    project = await service.create_project(
        name="qi-brief", task_type="feature", description="Test",
        initial_context={"confirmed_brief": {"decisions": []}},
    )
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "decision_key": "requirements.scope", "kind": "blocking",
             "question": " scope?", "options": [], "allow_free_text": True,
             "affected_trace_ids": []},
        ],
    )
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(
        questions[0].id, "web only", "仅 Web 端", False, "ou_user"
    )
    await service.resolve_question_interaction(interaction.id)

    updated = service.get_project(project.id)
    brief = updated.context["confirmed_brief"]
    assert len(brief["decisions"]) == 1
    assert brief["decisions"][0]["key"] == "requirements.scope"
    assert brief["decisions"][0]["answer"] == "仅 Web 端"
    assert brief["decisions"][0]["status"] == "confirmed"
    record = next(
        item
        for item in service.get_active_context_records(project.id)
        if item.record_key == "requirements.scope"
    )
    assert record.value["answer"] == "仅 Web 端"
    assert record.source_type == "user"
    assert record.source_ref == interaction.id


async def test_later_answer_supersedes_same_confirmed_decision_key(service):
    project = await service.create_project(
        name="qi-brief-revision",
        task_type="feature",
        description="Test",
        initial_context={
            "confirmed_brief": {
                "decisions": [{
                    "key": "agent.identity_role",
                    "question": "Agent 是谁？",
                    "answer": "通用 AI 助手",
                    "status": "confirmed",
                }]
            }
        },
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=2,
        resume_state="PLANNING_REQUIREMENTS",
        agent="planner",
        stage="requirements",
        batch_key="identity_revision",
        questions=[{
            "question_id": "q_identity_revision",
            "decision_key": "agent.identity_role",
            "kind": "blocking",
            "question": "Agent 应以什么身份呈现？",
            "options": [],
            "allow_free_text": True,
            "affected_trace_ids": [],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "configured owner identity",
        "参考项目配置的主体身份",
        False,
        "ou_user",
    )
    await service.resolve_question_interaction(interaction.id)

    updated = service.get_project(project.id)
    decisions = updated.context["confirmed_brief"]["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["key"] == "agent.identity_role"
    assert decisions[0]["answer"] == "参考项目配置的主体身份"
    records = service.get_context_record_history(project.id, "agent.identity_role")
    assert len(records) == 2
    assert [item.status for item in records] == ["superseded", "active"]
    assert records[-1].value["answer"] == "参考项目配置的主体身份"


async def test_skip_blocking_is_resolvable(service):
    project = await service.create_project(name="qi-s", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="CODING", agent="coder", stage=None, batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    questions = service.get_questions_for_interaction(interaction.id)
    await service.submit_question_answer(questions[0].id, None, None, True, "ou_user")
    assert service.interaction_is_resolvable(interaction.id) is True


async def test_get_open_question_interaction(service):
    project = await service.create_project(name="qi-o", task_type="feature", description="Test")
    assert service.get_open_question_interaction(project.id) is None
    await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    open_it = service.get_open_question_interaction(project.id)
    assert open_it is not None


async def test_question_interactions_dedupe_same_batch_without_expiring_other_batches(
    service,
):
    project = await service.create_project(
        name="qi-dedupe", task_type="feature", description="Test"
    )
    question = [{
        "question_id": "q1", "kind": "blocking", "question": "Q1",
        "options": [], "allow_free_text": True, "affected_trace_ids": [],
    }]
    first = await service.create_question_interaction(
        project_id=project.id, origin_task_id="task-1", origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="batch-1", questions=question,
    )
    duplicate = await service.create_question_interaction(
        project_id=project.id, origin_task_id="task-1", origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="batch-1", questions=question,
    )
    second = await service.create_question_interaction(
        project_id=project.id, origin_task_id="task-2", origin_round=2,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="batch-2", questions=[{**question[0], "question_id": "q2"}],
    )
    reused_key_next_round = await service.create_question_interaction(
        project_id=project.id, origin_task_id="task-3", origin_round=2,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="batch-1", questions=[{**question[0], "question_id": "q3"}],
    )

    assert duplicate.id == first.id
    assert second.id != first.id
    assert reused_key_next_round.id not in {first.id, second.id}
    pending = service.session.exec(
        select(QuestionInteraction).where(
            QuestionInteraction.project_id == project.id,
            QuestionInteraction.status == "pending",
        )
    ).all()
    assert {item.id for item in pending} == {
        first.id, second.id, reused_key_next_round.id,
    }


async def test_expire_open_question_interactions(service):
    project = await service.create_project(name="qi-e", task_type="feature", description="Test")
    await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[{"question_id": "q1", "kind": "blocking", "question": "Q1",
                    "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
                    "allow_free_text": False, "affected_trace_ids": []}],
    )
    assert service.expire_open_question_interactions(project.id) == 1
    assert service.get_open_question_interaction(project.id) is None


async def test_serialize_question_interaction(service):
    project = await service.create_project(name="qi-ser", task_type="feature", description="Test")
    interaction = await service.create_question_interaction(
        project_id=project.id, origin_task_id=None, origin_round=1,
        resume_state="PLANNING_REQUIREMENTS", agent="planner", stage="requirements",
        batch_key="b1",
        questions=[
            {"question_id": "q1", "kind": "blocking", "question": "Q1",
             "options": [{"key": "a", "label": "A", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": ["REQ-001"]},
            {"question_id": "q2", "kind": "decision", "question": "Q2",
             "options": [{"key": "x", "label": "X", "impact": "", "recommended": False},
                         {"key": "y", "label": "Y", "impact": "", "recommended": True}],
             "allow_free_text": False, "affected_trace_ids": []},
        ],
    )
    data = service.serialize_question_interaction(interaction.id)
    assert data is not None
    assert len(data["questions"]) == 2
    assert data["questions"][0]["recommended_option"] == "a"
    assert data["questions"][1]["recommended_option"] == "y"
    assert data["is_resolvable"] is False


async def test_default_authorization_prefix_match(service):
    project = await service.create_project(name="dauth", task_type="feature", description="Test")
    assert service.get_default_authorization(project.id, "q_agent_scope") is None
    await service.create_default_authorization(project.id, "q_agent", "chat_kb")
    auth = service.get_default_authorization(project.id, "q_agent_scope")
    assert auth is not None
    assert auth.authorized_option == "chat_kb"
    assert service.get_default_authorization(project.id, "q_blog") is None


async def test_backfill_question_keys_uses_structured_dependency_identity(service):
    project = await service.create_project(
        name="question-key-migration", task_type="feature", description="Test"
    )
    interaction_ids = []
    for round_number in (1, 2):
        interaction = await service.create_question_interaction(
            project_id=project.id,
            origin_task_id=None,
            origin_round=round_number,
            resume_state="CODING",
            agent="coder",
            stage="coding",
            batch_key=f"dependency-{round_number}",
            questions=[{
                "question_id": f"q_reworded_{round_number}",
                "kind": "blocking",
                "question": f"Reworded question {round_number}",
                "reason": "external dependency",
                "allow_free_text": True,
                "options": [],
                "affected_trace_ids": ["AC-2", "AC-1"],
            }],
        )
        interaction_ids.append(interaction.id)

    assert service.backfill_question_decision_keys() == 2
    keys = {
        service.get_questions_for_interaction(interaction_id)[0].decision_key
        for interaction_id in interaction_ids
    }
    assert len(keys) == 1
    assert next(iter(keys)).startswith("resolution.coder.")


async def test_reconcile_runtime_grant_uses_exact_option_metadata(service):
    project = await service.create_project(
        name="grant-migration", task_type="feature", description="Test"
    )
    task = await service.create_task(project.id, "coder", "CODING", {})
    output = {
        "question_batch": {
            "questions": [{
                "question_id": "q_runtime",
                "options": [{
                    "key": "allow",
                    "grants_runtime_capabilities": ["HostAutomation"],
                }],
            }]
        }
    }
    await service.complete_task(task.id, output)
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=task.id,
        origin_round=1,
        resume_state="CODING",
        agent="coder",
        stage="coding",
        batch_key="grant",
        questions=[{
            "question_id": "q_runtime",
            "decision_key": "runtime.host_automation",
            "kind": "blocking",
            "question": "Allow runtime?",
            "reason": "deployment",
            "allow_free_text": False,
            "options": [{"key": "allow", "label": "Allow"}],
            "affected_trace_ids": [],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id, "allow", "Allow", False, "ou_user"
    )

    assert service.reconcile_runtime_capability_grants() == 1
    assert service.require_project(project.id).context["runtime_capability_grants"] == [{
        "agent": "coder",
        "state": "CODING",
        "question_id": "q_runtime",
        "capabilities": ["HostAutomation"],
    }]


async def test_reconcile_feedback_uses_only_passing_tester_links(service):
    project = await service.create_project(
        name="feedback-migration", task_type="feature", description="Test"
    )
    passed = await service.create_feedback_packet(
        project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="verified old issue",
        blocking=True,
    )
    failed = await service.create_feedback_packet(
        project.id,
        source_stage="CODING",
        source_task_id=None,
        source_round=1,
        kind="quality_failure",
        summary="still failing",
        blocking=True,
    )
    task = await service.create_task(project.id, "tester", "TESTING", {})
    await service.complete_task(task.id, {
        "passed": False,
        "test_evidence": [
            {"result": "pass", "feedback_ids": [passed.id]},
            {"result": "fail", "feedback_ids": [failed.id]},
        ],
    })

    assert service.reconcile_feedback_from_passing_tester_evidence() == 1
    assert service.session.get(FeedbackPacket, passed.id).status == "resolved"
    assert service.session.get(FeedbackPacket, failed.id).status == "open"


# ---------------------------------------------------------------------------
# ConversationInteraction
# ---------------------------------------------------------------------------


async def test_conversation_interaction_create_and_resolve(service):
    ci = await service.create_conversation_interaction(
        chat_id="oc_c", open_id="ou_u", source_message_id="om_1",
        source_text="帮我加博客",
        candidates=[{"id": "proj_a", "name": "A"}, {"id": "proj_b", "name": "B"}],
    )
    assert ci.id.startswith("ci_")
    assert ci.status == "pending"
    assert len(ci.candidates) == 2
    pending = service.get_pending_conversation_interaction("oc_c", "ou_u")
    assert pending is not None
    resolved = await service.resolve_conversation_interaction(ci.id, "proj_a")
    assert resolved.status == "resolved"
    assert resolved.selected_project_id == "proj_a"
    assert service.get_pending_conversation_interaction("oc_c", "ou_u") is None


async def test_conversation_interaction_expires_previous(service):
    await service.create_conversation_interaction(
        chat_id="oc_c2", open_id="ou_u2", source_message_id="om_1",
        source_text="x", candidates=[],
    )
    await service.create_conversation_interaction(
        chat_id="oc_c2", open_id="ou_u2", source_message_id="om_2",
        source_text="y", candidates=[],
    )
    from mac_system.models import ConversationInteraction
    pending = service.session.exec(
        select(ConversationInteraction).where(ConversationInteraction.chat_id == "oc_c2")
        .where(ConversationInteraction.status == "pending")
    ).all()
    assert len(pending) == 1


async def test_acceptance_not_verified_is_ledger_evidence_but_keeps_question_open(service):
    project = await service.create_project(
        name="external-wait", task_type="feature", description="Test"
    )
    interaction = await service.create_question_interaction(
        project_id=project.id,
        origin_task_id=None,
        origin_round=1,
        resume_state="ACCEPTANCE_PENDING",
        agent="acceptance_evaluator",
        stage="acceptance",
        batch_key="manual_acceptance",
        questions=[{
            "question_id": "q_phone",
            "decision_key": "acceptance.ac-net-001.manual_verdict",
            "kind": "blocking",
            "question": "Use a physical phone",
            "options": [{"key": "not_verified", "label": "Not verified"}],
            "allow_free_text": False,
            "affected_trace_ids": ["AC-NET-001"],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "not_verified",
        "Not verified",
        False,
        "ou_user",
        keep_pending=True,
    )

    service.session.refresh(question)
    assert question.status == "pending"
    assert service.interaction_is_resolvable(interaction.id) is False
    evidence = service.get_active_acceptance_evidence(project.id)["AC-NET-001"]
    assert evidence.verdict == "not_verified"
    assert evidence.source_type == "user"
    assert evidence.source_ref == "q_phone"


async def test_acceptance_evidence_is_versioned_and_can_be_invalidated(service):
    project = await service.create_project(
        name="ledger", task_type="feature", description="Test"
    )
    first = service.record_acceptance_evidence(
        project.id,
        "AC-001",
        verdict="not_verified",
        verification_method="external",
        evidence_scope="phone",
        source_type="user",
        source_ref="q1",
    )
    second = service.record_acceptance_evidence(
        project.id,
        "AC-001",
        verdict="pass",
        verification_method="external",
        evidence_scope="physical phone, complete conversation",
        source_type="user",
        source_ref="q1",
    )
    service.session.refresh(first)
    assert first.status == "superseded"
    assert second.version == 2
    assert service.invalidate_acceptance_evidence(
        project.id, reason="implementation changed"
    ) == 1
    service.session.refresh(second)
    assert second.status == "stale"
    assert "implementation changed" in second.limitations


async def test_failure_reports_preserve_evidence_and_close_after_success(service):
    project = await service.create_project(
        name="failure-loop", task_type="feature", description="Test"
    )
    first = await service.create_task(project.id, "coder", "CODING", {})
    await service.fail_task(
        first.id,
        "Agent contract violation: file task_20260727_000000_abcd missing",
        evidence={"reported": ["a.py"], "actual": []},
    )
    second = await service.create_task(project.id, "coder", "CODING", {})
    await service.fail_task(
        second.id,
        "Agent contract violation: file task_20260727_111111_dcba missing",
        evidence={"reported": ["a.py"], "actual": []},
    )
    history = service.get_task_failure_history(
        project.id, agent_type="coder", state="CODING"
    )
    assert len(history) == 2
    assert history[0].fingerprint == history[1].fingerprint
    assert history[-1].evidence["reported"] == ["a.py"]

    third = await service.create_task(project.id, "coder", "CODING", {})
    await service.complete_task(third.id, {"success": True})
    assert all(
        report.status == "resolved"
        for report in service.get_task_failure_history(
            project.id, agent_type="coder", state="CODING"
        )
    )
