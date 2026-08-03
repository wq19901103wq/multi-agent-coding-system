"""Deterministic ownership and loop signatures for structured failures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mac_system.core.work_item import extract_acceptance_criterion_ids


@dataclass(frozen=True)
class FailureRoute:
    domain: str
    target_stage: str | None
    is_project_rework: bool


class FailureRouter:
    """Route from provenance and structured ownership, never error keywords."""

    _PROJECT_STAGES = {"requirements", "architecture", "specs", "coding"}

    @classmethod
    def for_feedback(cls, feedback: Any) -> FailureRoute:
        target = str(getattr(feedback, "recommended_rework_target", None) or "") or None
        source = str(getattr(feedback, "source_stage", "") or "")
        if target == "testing":
            return FailureRoute("verification", target, False)
        if target in cls._PROJECT_STAGES:
            return FailureRoute("delivery", target, True)
        if source == "ACCEPTANCE":
            return FailureRoute("acceptance_environment", target, False)
        return FailureRoute("orchestration", target, False)

    @staticmethod
    def signature(feedback: Any) -> str:
        payload = {
            "source_stage": str(getattr(feedback, "source_stage", "") or ""),
            "failure_class": str(getattr(feedback, "failure_class", "") or ""),
            "target": str(getattr(feedback, "recommended_rework_target", "") or ""),
            "acceptance_criterion_ids": sorted(
                extract_acceptance_criterion_ids(getattr(feedback, "evidence", []) or [])
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]

