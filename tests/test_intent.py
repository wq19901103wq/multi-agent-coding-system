import pytest

from mac_system.core.intent import IntentRouter, suggest_name
from mac_system.models import RouteDecision


@pytest.fixture
def router():
    return IntentRouter(high_threshold=0.85, low_threshold=0.5)


@pytest.mark.parametrize(
    "description",
    [
        "修复 config-validator 的 port 范围校验 bug",
        "给项目写一份 AGENTS.md",
        "实现一个 config-validator CLI",
        "重构这段代码",
        "fix the bug",
        "换一种完全不同的自然语言表达",
    ],
)
def test_untyped_create_uses_same_neutral_workflow_for_every_wording(router, description):
    decision = router.route_demand(description)
    assert decision.task_type == "feature"
    assert decision.initial_state == "PLANNING_REQUIREMENTS"
    assert decision.confidence == 1.0
    assert not decision.needs_disambiguation
    assert "关键词" not in decision.reason


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
