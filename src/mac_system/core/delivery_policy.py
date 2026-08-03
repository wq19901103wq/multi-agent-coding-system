"""Deterministic compilation of Coordinator-selected delivery risk metadata."""

from __future__ import annotations

from typing import Any


ALL_QUALITY_STAGES = (
    "coding",
    "testing",
    "review",
    "acceptance",
    "acceptance_evaluator",
)


def compile_delivery_policy(
    task_type: str,
    raw: dict[str, Any] | None,
    *,
    reference_mode: str = "none",
) -> dict[str, Any]:
    """Validate a semantic policy and enforce conservative safety invariants.

    The Coordinator decides risk/surface from meaning.  This function never
    examines user prose; it only converts schema enums into an executable
    stage set. Missing or malformed policy remains conservative.
    """
    source = dict(raw or {})
    risk = str(source.get("risk_level") or "high")
    surface = str(source.get("change_surface") or "mixed")
    if risk not in {"low", "medium", "high"}:
        risk = "high"
    if surface not in {"user_visible", "internal", "documentation", "mixed"}:
        surface = "mixed"

    if task_type != "iteration" or reference_mode == "full":
        required = list(ALL_QUALITY_STAGES)
        reason = "完整交付或参考复刻使用完整独立质量链"
    else:
        required = ["coding", "testing"]
        if risk in {"medium", "high"}:
            required.append("review")
        if surface in {"user_visible", "mixed"}:
            required.append("acceptance")
        if risk == "high":
            if "review" not in required:
                required.append("review")
            if "acceptance" not in required:
                required.append("acceptance")
            required.append("acceptance_evaluator")
        reason = str(source.get("reason") or "根据结构化风险和变更表面选择最小充分证据链")

    # An explicit policy may request extra independent evidence, never remove
    # the deterministic minimum derived above.
    for stage in source.get("required_stages") or []:
        if stage in ALL_QUALITY_STAGES and stage not in required:
            required.append(stage)

    # Active acceptance depends on independent review evidence.  The semantic
    # evaluator is deliberately separate: ordinary evidence is compiled by a
    # deterministic policy, while high-risk or explicitly requested work may
    # add the extra model judgement.
    if "acceptance" in required or "acceptance_evaluator" in required:
        for dependency in ("review", "acceptance"):
            if dependency not in required:
                required.append(dependency)

    return {
        "version": 1,
        "risk_level": risk,
        "change_surface": surface,
        "required_stages": [stage for stage in ALL_QUALITY_STAGES if stage in required],
        "reason": reason,
        "source": "coordinator" if raw else "conservative_default",
    }


def stage_required(policy: dict[str, Any] | None, stage: str) -> bool:
    # Planning and recovery stages are outside this policy's scope.  Treating
    # an unknown stage as optional would silently skip requirements/spec work.
    if stage not in ALL_QUALITY_STAGES:
        return True
    if not policy:
        return True
    return stage in set(policy.get("required_stages") or ALL_QUALITY_STAGES)
