from types import SimpleNamespace

from mac_system.core.failure_router import FailureRouter
from mac_system.core.delivery_policy import compile_delivery_policy
from mac_system.core.loop_supervisor import LoopSupervisor
from mac_system.core.runtime_manager import RuntimeManager
from mac_system.core.work_item import WorkItemCompiler
from mac_system.agents.orchestrator import AgentOrchestrator


def test_delivery_policy_keeps_user_visible_acceptance_boundary_atomic():
    policy = compile_delivery_policy(
        "iteration",
        {
            "risk_level": "low",
            "change_surface": "user_visible",
            "required_stages": ["acceptance"],
            "reason": "small visible change",
        },
    )

    assert policy["required_stages"] == [
        "coding",
        "testing",
        "review",
        "acceptance",
    ]


def test_delivery_policy_allows_low_risk_internal_iteration_to_stop_after_tests():
    policy = compile_delivery_policy(
        "iteration",
        {
            "risk_level": "low",
            "change_surface": "internal",
            "required_stages": [],
            "reason": "internal refactor",
        },
    )

    assert policy["required_stages"] == ["coding", "testing"]


def _feedback(*, target="coding", ac_id="AC-1", source="REVIEW", failure="implementation"):
    return SimpleNamespace(
        source_stage=source,
        failure_class=failure,
        recommended_rework_target=target,
        evidence=[{"affected_acceptance_criterion_ids": [ac_id]}],
    )


def test_work_item_compiler_scopes_feedback_to_explicit_ac_ids():
    contract = WorkItemCompiler().compile(
        state="CODING",
        agent_type="coder",
        objective="repair",
        supplied_contract={},
        relevant_facts=[],
        acceptance_criteria=[{"id": "AC-1"}, {"id": "AC-2"}],
        regression_acceptance_criteria=[],
        open_feedback=[{
            "feedback_id": "fb-1",
            "evidence": [{"affected_acceptance_criterion_ids": ["AC-2"]}],
            "failure_route": {
                "domain": "delivery",
                "target_stage": "coding",
                "is_project_rework": True,
                "signature": "route-1",
            },
        }],
        artifact_refs={"requirements": 2},
        context_version=7,
    )

    assert contract["contract_version"] == 2
    assert contract["owner"] == "coder"
    assert contract["in_scope_acceptance_criterion_ids"] == ["AC-2"]
    assert contract["feedback_refs"] == ["fb-1"]
    assert contract["feedback_routes"] == [{
        "domain": "delivery",
        "target_stage": "coding",
        "is_project_rework": True,
        "signature": "route-1",
    }]
    assert "assigned_feedback_resolved" in contract["completion_conditions"]


def test_changed_artifact_contract_cannot_be_narrowed_by_old_feedback():
    contract = WorkItemCompiler().compile(
        state="CODING",
        agent_type="coder",
        objective="reconcile revised contract",
        supplied_contract={"artifact_revision_changed": True},
        relevant_facts=[],
        acceptance_criteria=[{"id": "AC-1"}, {"id": "AC-2"}],
        regression_acceptance_criteria=[],
        open_feedback=[{
            "feedback_id": "fb-old",
            "evidence": [{"affected_acceptance_criterion_ids": ["AC-1"]}],
        }],
        artifact_refs={
            "requirements": {"version": 2, "round": 2, "status": "confirmed"},
            "specs": {"version": 3, "round": 3, "status": "confirmed"},
        },
        context_version=2,
    )

    assert contract["artifact_revision_changed"] is True
    assert contract["in_scope_acceptance_criterion_ids"] == ["AC-1", "AC-2"]
    assert "every_current_acceptance_criterion_reconciled_to_current_artifacts" in (
        contract["completion_conditions"]
    )
    assert len(contract["artifact_contract_key"]) == 64


def test_unmapped_blocking_feedback_keeps_the_current_contract_in_scope():
    contract = WorkItemCompiler().compile(
        state="CODING",
        agent_type="coder",
        objective="repair broad observed behavior",
        supplied_contract={},
        relevant_facts=[],
        acceptance_criteria=[{"id": "AC-1"}, {"id": "AC-2"}],
        regression_acceptance_criteria=[],
        open_feedback=[
            {
                "feedback_id": "fb-specific",
                "blocking": True,
                "evidence": [{"affected_acceptance_criterion_ids": ["AC-1"]}],
            },
            {
                "feedback_id": "fb-broad",
                "blocking": True,
                "summary": "Observed delivery still violates the revised contract",
                "evidence": [],
            },
        ],
        artifact_refs={"requirements": {"version": 1, "round": 1}},
        context_version=1,
    )

    assert contract["in_scope_acceptance_criterion_ids"] == ["AC-1", "AC-2"]


def test_quality_work_item_can_include_regression_ids_in_authoritative_scope():
    contract = WorkItemCompiler().compile(
        state="TESTING",
        agent_type="tester",
        objective="verify current feature and regression baseline",
        supplied_contract={"include_regression_in_scope": True},
        relevant_facts=[],
        acceptance_criteria=[{"id": "AC-CURRENT"}],
        regression_acceptance_criteria=[{"id": "AC-HISTORICAL"}],
        open_feedback=[],
        artifact_refs={},
        context_version=1,
    )

    assert contract["in_scope_acceptance_criterion_ids"] == [
        "AC-CURRENT",
        "AC-HISTORICAL",
    ]


def test_work_item_preserves_complete_reference_inventory_and_probes():
    reference = {
        "mode": "full",
        "capability_inventory": {"capabilities": [{"id": "cap-1"}]},
        "behavioral_probes": {"probes": [{"capability_id": "cap-1"}]},
    }
    contract = WorkItemCompiler().compile(
        state="CODING",
        agent_type="coder",
        objective="migrate complete reference",
        supplied_contract={"reference": reference},
        relevant_facts=[],
        acceptance_criteria=[],
        regression_acceptance_criteria=[],
        open_feedback=[],
        artifact_refs={},
        context_version=1,
    )

    assert contract["reference"] == reference


def test_failure_router_uses_structured_ownership_not_summary_keywords():
    verifier = FailureRouter.for_feedback(_feedback(target="testing", failure="test_bug"))
    delivery = FailureRouter.for_feedback(_feedback(target="coding", failure="anything"))

    assert verifier.domain == "verification"
    assert verifier.is_project_rework is False
    assert delivery.domain == "delivery"
    assert delivery.is_project_rework is True


def test_loop_supervisor_blocks_third_identical_structured_route():
    current = _feedback()
    assessment = LoopSupervisor(threshold=3).assess(
        current, [current, _feedback(), _feedback(ac_id="AC-2")]
    )
    assert assessment.repeated is False

    repeated = LoopSupervisor(threshold=3).assess(
        current, [current, _feedback(), _feedback()]
    )
    assert repeated.repeated is True
    assert repeated.occurrences == 3
    assert "不证明根因位于更上游" in repeated.reason


def test_runtime_manager_separates_artifact_and_running_revisions():
    context = RuntimeManager.mark_artifact_revision({}, "rev-1")
    assert context["runtime_snapshot"]["deployment_status"] == "unknown"
    observed = RuntimeManager.record_observed_runtime(
        context,
        revision="rev-1",
        environment={
            "entry_points": ["http://127.0.0.1:8000"],
            "started_services": ["web"],
        },
    )
    contract = RuntimeManager.acceptance_contract(
        observed,
        "evidence-key-1",
        workspace_snapshot={
            "digest": "artifact-digest-1",
            "git_head": "abc123",
            "git_tree": "tree123",
        },
    )
    assert contract["last_observed_running_revision"] == "rev-1"
    assert contract["known_entry_points"] == ["http://127.0.0.1:8000"]
    assert contract["known_started_services"] == ["web"]
    assert contract["evidence_revision_key"] == "evidence-key-1"
    assert contract["expected_artifact_revision"] == "artifact-digest-1"
    assert contract["expected_git_head"] == "abc123"
    assert contract["expected_git_tree"] == "tree123"

    changed = RuntimeManager.mark_artifact_revision(observed, "rev-2")
    assert changed["runtime_snapshot"]["deployment_status"] == "unknown"
    assert changed["runtime_snapshot"]["running_revision"] == "rev-1"

    verification = RuntimeManager.verification_contract(changed)
    assert verification == {
        "expected_artifact_revision": "rev-2",
        "last_observed_running_revision": "rev-1",
        "deployment_status": "unknown",
        "known_entry_points": ["http://127.0.0.1:8000"],
        "known_started_services": ["web"],
    }


def test_loop_supervisor_selects_alternate_headless_provider():
    runtime = SimpleNamespace(
        providers={"claude_code": object(), "codex": object()},
        provider_for=lambda agent_type: "claude_code",
    )
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.runtime = runtime
    project = SimpleNamespace(context={
        "loop_supervisor": {"prohibited_default_target": "specs"}
    })

    assert orchestrator._loop_alternate_provider(
        project, "spec_writer", "PLANNING_IMPLEMENTATION_SPECS"
    ) == "codex"
