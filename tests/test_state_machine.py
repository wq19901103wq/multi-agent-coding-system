import pytest

from mac_system.state_machine import StateMachine


def test_feature_state_machine_basic():
    sm = StateMachine("feature")
    assert sm.task_type == "feature"
    assert sm.initial_state == "PLANNING_REQUIREMENTS"
    assert sm.state_type("PLANNING_REQUIREMENTS") == "agent_execution"
    assert sm.agent_for("PLANNING_REQUIREMENTS") == "planner"
    assert sm.stage_for("PLANNING_REQUIREMENTS") == "requirements"


def test_feature_transitions():
    sm = StateMachine("feature")
    assert sm.next_state("PLANNING_REQUIREMENTS", "complete") == "REQUIREMENT_CONFIRM_PENDING"
    assert sm.next_state("REQUIREMENT_CONFIRM_PENDING", "confirm") == "PLANNING_ARCHITECTURE"
    assert sm.next_state("REQUIREMENT_CONFIRM_PENDING", "reject") == "PLANNING_REQUIREMENTS"
    assert sm.next_state("PLANNING_ARCHITECTURE", "complete") == "ARCHITECTURE_CONFIRM_PENDING"
    assert sm.next_state("ARCHITECTURE_CONFIRM_PENDING", "confirm") == "PLANNING_SPECS"
    assert sm.next_state("ARCHITECTURE_CONFIRM_PENDING", "select_option") == "PLANNING_ARCHITECTURE"
    assert sm.next_state("PLANNING_SPECS", "complete") == "SPEC_CONFIRM_PENDING"
    assert sm.next_state("SPEC_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "TESTING"
    assert sm.next_state("TESTING", "complete") == "REVIEW"
    assert sm.next_state("REVIEW", "complete") == "ACCEPTANCE_PENDING"
    assert sm.next_state("ACCEPTANCE_PENDING", "confirm") == "MERGING"
    assert sm.next_state("MERGING", "complete") == "COMPLETED"


def test_bug_fix_state_machine():
    sm = StateMachine("bug_fix")
    assert sm.initial_state == "BUG_ANALYSIS"
    assert sm.next_state("BUG_ANALYSIS", "complete") == "BUG_FIX_CONFIRM_PENDING"
    assert sm.next_state("BUG_FIX_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "TESTING"
    assert sm.next_state("CODING", "bug_analysis_question") == "BUG_ANALYSIS"


def test_documentation_state_machine():
    sm = StateMachine("documentation")
    assert sm.initial_state == "DOC_OUTLINE"
    assert sm.next_state("DOC_OUTLINE", "complete") == "OUTLINE_CONFIRM_PENDING"
    assert sm.next_state("OUTLINE_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "REVIEW"
    assert sm.next_state("CODING", "outline_question") == "DOC_OUTLINE"


def test_terminal_states():
    sm = StateMachine("feature")
    assert sm.is_terminal("COMPLETED")
    assert sm.is_terminal("FAILED")
    assert sm.is_terminal("CANCELLED")
    assert not sm.is_terminal("CODING")
    assert sm.is_idle("PAUSED")


def test_invalid_transition():
    sm = StateMachine("feature")
    assert sm.next_state("PLANNING_REQUIREMENTS", "confirm") is None


def test_unknown_state_raises():
    sm = StateMachine("feature")
    with pytest.raises(ValueError):
        sm.get_state("NOT_A_STATE")
