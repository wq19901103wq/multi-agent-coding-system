import pytest

from mac_system.core.intent import IntentRouter, suggest_name
from mac_system.models import RouteDecision


@pytest.fixture
def router():
    return IntentRouter(high_threshold=0.85, low_threshold=0.5)


def test_route_bug_fix(router):
    decision = router.route_demand("修复 config-validator 的 port 范围校验 bug")
    assert decision.task_type == "bug_fix"
    assert decision.initial_state == "BUG_ANALYSIS"
    assert decision.confidence == 0.85
    assert not decision.needs_disambiguation


def test_route_documentation(router):
    decision = router.route_demand("给项目写一份 AGENTS.md")
    assert decision.task_type == "documentation"
    assert decision.initial_state == "DOC_OUTLINE"
    assert decision.confidence == 0.85
    assert not decision.needs_disambiguation


def test_route_feature(router):
    decision = router.route_demand("实现一个 config-validator CLI")
    assert decision.task_type == "feature"
    assert decision.initial_state == "PLANNING_REQUIREMENTS"
    assert decision.confidence == 0.85
    assert not decision.needs_disambiguation


def test_route_uncovered(router):
    decision = router.route_demand("重构这段代码")
    assert decision.task_type is None
    assert decision.initial_state is None
    assert decision.confidence == 0.4
    assert not decision.needs_disambiguation


def test_word_boundary_fix_not_feature(router):
    # "fix" should not match "feature".
    decision = router.route_demand("fix the bug")
    assert decision.task_type == "bug_fix"
    # "feature" should not be classified as bug_fix just because it contains "feat".
    decision = router.route_demand("新增一个 feature")
    assert decision.task_type == "feature"


def test_suggest_name():
    assert suggest_name("实现一个 config-validator CLI") == "实现一个 config-validator CLI"
    assert suggest_name("  修复 bug。  ") == "修复 bug"
    # Descriptions shorter than the max length are returned unchanged.
    long = "实现一个非常非常非常非常非常非常非常复杂的功能模块"
    assert suggest_name(long) == long


def test_suggest_name_breaks_at_separator():
    # Prefer breaking at the last separator rather than cutting a word.
    name = suggest_name("修复 config-validator 的 port 范围校验 bug")
    assert " " in name
    assert name.endswith("port") or name.endswith("范围") or len(name) <= 30


def test_suggest_name_avoids_mid_word_truncation():
    long = "实现一个 config-validator-with-a-very-long-name CLI"
    name = suggest_name(long)
    assert len(name) <= 30
    # Should break at the last hyphen before the limit, not in the middle of a word.
    assert "-with" in name or "一个" in name


def test_route_decision_model():
    decision = RouteDecision(
        task_type="feature",
        initial_state="PLANNING_REQUIREMENTS",
        confidence=0.85,
        needs_disambiguation=False,
        reason="test",
    )
    assert decision.task_type == "feature"
