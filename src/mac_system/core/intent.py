"""Intent router for classifying natural-language descriptions into task types.

The classifier decides which state machine (feature / bug_fix / documentation)
should handle a user's free-form project description. This is intentionally a
rule-based MVP implementation; the interface is stable enough to swap in an
LLM-based classifier later without changing callers.
"""

from __future__ import annotations

import re

from mac_system.config import get_settings
from mac_system.models import RouteDecision
from mac_system.state_machine import StateMachine


def _contains_word(text: str, word: str) -> bool:
    """Case-insensitive whole-word or token match.

    For English tokens we require a word boundary so that ``bug`` does not
    match ``debug``. For Chinese keywords the substring check is sufficient.
    Mixed tokens like ``agents.md`` are treated as atomic.
    """
    lowered = text.lower()
    word_lower = word.lower()

    # Pure ASCII tokens: use regex word boundary.
    if word.isascii():
        pattern = r"(?:^|[^a-z0-9_.])" + re.escape(word_lower) + r"(?:$|[^a-z0-9_.])"
        return re.search(pattern, lowered) is not None

    # Chinese / mixed tokens: substring match.
    return word_lower in lowered


def _any_word(text: str, words: list[str]) -> bool:
    return any(_contains_word(text, w) for w in words)


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


# Keyword sets. Order matters: we check bug_fix, then documentation, then feature.
BUG_FIX_STARTERS = [
    "bug",
    "修复",
    "报错",
    "异常",
    "崩溃",
    "不工作",
    "回归",
    "fix",
]

DOCUMENTATION_STARTERS = [
    "文档",
    "readme",
    "agents.md",
    "说明文档",
    "写一份",
]

FEATURE_EXPLICIT_STARTERS = [
    "实现",
    "开发",
    "新功能",
    "功能需求",
    "feature",
]

FEATURE_FALLBACK_HINTS = [
    "新增",
    "加一个",
    "支持",
]

# Confidence levels. Explicit starters (bug/doc/strong feature signals) are
# trusted above the high threshold. Feature fallback hints are ambiguous enough
# that we ask the user to confirm the inferred task type.
CONFIDENCE_EXPLICIT = 0.85
CONFIDENCE_FALLBACK = 0.75
CONFIDENCE_UNCOVERED = 0.4


class IntentRouter:
    """Rule-based demand classifier."""

    def __init__(
        self,
        high_threshold: float | None = None,
        low_threshold: float | None = None,
    ):
        settings = get_settings()
        self.high_threshold = high_threshold if high_threshold is not None else settings.intent_high_threshold
        self.low_threshold = low_threshold if low_threshold is not None else settings.intent_low_threshold

    def route_demand(self, description: str) -> RouteDecision:
        """Classify ``description`` and return a routing decision."""
        task_type: str | None = None
        confidence = CONFIDENCE_UNCOVERED
        reason: str | None = None

        if _any_word(description, BUG_FIX_STARTERS):
            task_type = "bug_fix"
            confidence = CONFIDENCE_EXPLICIT
            reason = "命中 bug 修复关键词"
        elif _any_word(description, DOCUMENTATION_STARTERS):
            task_type = "documentation"
            confidence = CONFIDENCE_EXPLICIT
            reason = "命中文档关键词"
        elif _any_word(description, FEATURE_EXPLICIT_STARTERS):
            task_type = "feature"
            confidence = CONFIDENCE_EXPLICIT
            reason = "命中功能开发强关键词"
        elif _any_word(description, FEATURE_FALLBACK_HINTS):
            task_type = "feature"
            confidence = CONFIDENCE_FALLBACK
            reason = "命中功能开发弱关键词"
        else:
            reason = "未匹配到已知任务类型关键词"

        initial_state: str | None = None
        if task_type is not None:
            try:
                initial_state = StateMachine(task_type).initial_state
            except FileNotFoundError:
                # Unknown task_type should not happen with the hard-coded sets
                # above, but keep the decision safe.
                task_type = None
                initial_state = None
                confidence = CONFIDENCE_UNCOVERED
                reason = "对应的状态机不存在"

        needs_disambiguation = self.low_threshold <= confidence < self.high_threshold
        return RouteDecision(
            task_type=task_type,
            initial_state=initial_state,
            confidence=confidence,
            needs_disambiguation=needs_disambiguation,
            reason=reason,
        )


def route_demand(description: str) -> RouteDecision:
    """Convenience function using default settings thresholds."""
    return IntentRouter().route_demand(description)


def suggest_name(description: str) -> str:
    """Derive a short display name from a description."""
    return _extract_name(description)
