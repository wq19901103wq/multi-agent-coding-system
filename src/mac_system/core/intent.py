"""Project naming and the deterministic default for explicit ``/create``.

Natural-language intent belongs to the Conversation Coordinator. This module
must not infer task type from words in a user description. Explicit command
options remain authoritative; an untyped ``/create`` uses the common delivery
profile's neutral ``feature`` entry point.
"""

from __future__ import annotations

from mac_system.models import RouteDecision
from mac_system.state_machine import StateMachine


def _extract_name(description: str, max_length: int = 30) -> str:
    """Generate a short project name from the description."""
    cleaned = description.strip().strip(".。,，!！?？")
    if len(cleaned) <= max_length:
        return cleaned
    # Try to break at the last separator before max_length so we don't cut a word.
    truncated = cleaned[:max_length]
    separators = (" ", "，", ",", "、", "；", ";", "：", ":", "-", "_")
    best_idx = -1
    for sep in separators:
        idx = truncated.rfind(sep)
        if idx > best_idx:
            best_idx = idx
    if best_idx >= 3:
        return cleaned[:best_idx].strip(".。,，!！?？")
    return truncated.strip(".。,，!！?？")


CONFIDENCE_EXPLICIT = 0.85


class IntentRouter:
    """Backward-compatible deterministic router for explicit ``/create``."""

    def __init__(
        self,
        high_threshold: float | None = None,
        low_threshold: float | None = None,
    ):
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold

    def route_demand(self, description: str) -> RouteDecision:
        """Return the neutral common workflow without inspecting description."""
        task_type = "feature"
        return RouteDecision(
            task_type=task_type,
            initial_state=StateMachine(task_type).initial_state,
            confidence=1.0,
            needs_disambiguation=False,
            reason="未显式指定类型，使用统一交付流程",
        )


def route_demand(description: str) -> RouteDecision:
    """Convenience function using default settings thresholds."""
    return IntentRouter().route_demand(description)


def suggest_name(description: str) -> str:
    """Derive a short display name from a description."""
    return _extract_name(description)
