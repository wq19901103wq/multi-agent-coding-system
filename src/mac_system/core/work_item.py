"""Compilation of bounded, stage-specific Agent work contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def extract_acceptance_criterion_ids(value: Any) -> set[str]:
    """Collect explicit AC references without interpreting prose or keywords."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "acceptance_criterion_id",
                "affected_acceptance_criterion_id",
            } and item:
                found.add(str(item))
            elif key in {
                "acceptance_criterion_ids",
                "affected_acceptance_criterion_ids",
                "affected_trace_ids",
            } and isinstance(item, list):
                found.update(str(entry) for entry in item if entry)
            else:
                found.update(extract_acceptance_criterion_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(extract_acceptance_criterion_ids(item))
    return found


class WorkItemCompiler:
    """Turn project state into the sole authoritative input for one Agent turn."""

    _COMPLETION_CONDITIONS: dict[str, list[str]] = {
        "planning": [
            "artifact_schema_valid",
            "no_unresolved_blockers",
            "acceptance_traceability_preserved",
        ],
        "implementation": [
            "assigned_feedback_resolved",
            "delivery_artifacts_updated_or_no_change_proven",
            "no_unreported_blockers",
        ],
        "quality": [
            "independent_evidence_recorded",
            "every_claim_linked_to_acceptance_criteria",
            "explicit_pass_or_structured_failure",
        ],
        "acceptance": [
            "public_entrypoint_exercised",
            "every_in_scope_criterion_has_direct_evidence",
            "running_revision_matches_artifact_revision",
        ],
    }

    @staticmethod
    def stage_kind(state: str) -> str:
        if state.startswith("PLANNING_") or state in {"BUG_ANALYSIS", "DOC_OUTLINE"}:
            return "planning"
        if state == "CODING":
            return "implementation"
        if state in {"TESTING", "REVIEW"}:
            return "quality"
        if state == "ACCEPTANCE_PENDING":
            return "acceptance"
        return "execution"

    def compile(
        self,
        *,
        state: str,
        agent_type: str,
        objective: str,
        supplied_contract: dict[str, Any],
        relevant_facts: list[dict[str, Any]],
        acceptance_criteria: list[dict[str, Any]],
        regression_acceptance_criteria: list[dict[str, Any]],
        open_feedback: list[dict[str, Any]],
        artifact_refs: dict[str, Any],
        context_version: int,
    ) -> dict[str, Any]:
        stage_kind = self.stage_kind(state)
        artifact_revision_changed = bool(
            supplied_contract.get("artifact_revision_changed")
        )
        feedback_refs: list[str] = []
        for item in open_feedback:
            if not isinstance(item, dict):
                continue
            if item.get("feedback_id"):
                feedback_refs.append(str(item["feedback_id"]))
            feedback_refs.extend(
                str(value)
                for value in (item.get("related_feedback_ids") or [])
                if value
            )
        feedback_refs = list(dict.fromkeys(feedback_refs))
        feedback_routes = [
            dict(item["failure_route"])
            for item in open_feedback
            if isinstance(item, dict) and isinstance(item.get("failure_route"), dict)
        ]
        affected_ids = extract_acceptance_criterion_ids(open_feedback)
        has_unmapped_feedback = any(
            isinstance(item, dict)
            and item.get("blocking") is not False
            and not extract_acceptance_criterion_ids(item)
            for item in open_feedback
        )
        current_ids = {
            str(item.get("id"))
            for item in acceptance_criteria
            if isinstance(item, dict) and item.get("id")
        }
        if affected_ids:
            affected_ids &= current_ids
        # A focused feedback repair may narrow an unchanged delivery contract
        # to the ACs named by that feedback.  Once Requirements/Architecture/
        # Specs change, however, the previous implementation and evidence are
        # no longer a valid baseline for *any* current AC.  Keeping the old
        # feedback-only scope here allowed a broad contract replay to reach
        # Coding while Coder and Tester still worked on one historical defect.
        in_scope_ids = sorted(
            current_ids
            if artifact_revision_changed or has_unmapped_feedback
            else (affected_ids or current_ids)
        )
        if supplied_contract.get("include_regression_in_scope"):
            in_scope_ids = sorted({
                *in_scope_ids,
                *(
                    str(item.get("id"))
                    for item in regression_acceptance_criteria
                    if isinstance(item, dict) and item.get("id")
                ),
            })
        capability_facts = [
            fact
            for fact in relevant_facts
            if isinstance(fact, dict) and fact.get("type") == "capability"
        ]
        artifact_contract_key = hashlib.sha256(
            json.dumps(
                artifact_refs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        completion_conditions = list(
            supplied_contract.get("completion_conditions")
            or self._COMPLETION_CONDITIONS.get(stage_kind, ["contract_satisfied"])
        )
        if artifact_revision_changed and stage_kind == "implementation":
            completion_conditions.append(
                "every_current_acceptance_criterion_reconciled_to_current_artifacts"
            )
        elif artifact_revision_changed and stage_kind == "quality":
            completion_conditions.append(
                "fresh_evidence_collected_for_current_artifact_contract"
            )
        return {
            "contract_version": 2,
            "objective": supplied_contract.get("objective") or objective or state,
            "owner": agent_type,
            "stage": state,
            "stage_kind": stage_kind,
            "relevant_facts": relevant_facts,
            "artifact_refs": supplied_contract.get("artifact_refs") or artifact_refs,
            "artifact_contract_key": artifact_contract_key,
            "artifact_revision_changed": artifact_revision_changed,
            "constraints": list(supplied_contract.get("constraints") or []),
            "acceptance_criteria": acceptance_criteria,
            "regression_acceptance_criteria": regression_acceptance_criteria,
            "in_scope_acceptance_criterion_ids": in_scope_ids,
            "required_capabilities": list(
                supplied_contract.get("required_capabilities") or capability_facts
            ),
            "allowed_deferred_obligations": list(
                supplied_contract.get("allowed_deferred_obligations") or []
            ),
            "runtime_contract": dict(
                supplied_contract.get("runtime_contract") or {}
            ),
            # Complete-reference delivery is a first-class execution contract,
            # not planning-only conversation history. Preserve its source-
            # derived inventory and probes for every downstream owner.
            "reference": dict(supplied_contract.get("reference") or {}),
            "feedback_refs": list(
                supplied_contract.get("feedback_refs") or feedback_refs
            ),
            "feedback_routes": list(
                supplied_contract.get("feedback_routes") or feedback_routes
            ),
            "completion_conditions": list(dict.fromkeys(completion_conditions)),
            "performance_budget": dict(
                supplied_contract.get("performance_budget")
                or {
                    "no_progress_retry_limit": 3,
                    "prefer_incremental_verification": True,
                }
            ),
            "context_version": context_version,
        }
