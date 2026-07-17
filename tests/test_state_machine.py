import pytest

from mac_system.state_machine import StateMachine


def test_feature_state_machine_basic():
    sm = StateMachine("feature")
    assert sm._config["machine_type"] == "delivery"
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
    assert sm.next_state("ARCHITECTURE_CONFIRM_PENDING", "confirm") == "PLANNING_IMPLEMENTATION_SPECS"
    assert sm.next_state("ARCHITECTURE_CONFIRM_PENDING", "select_option") is None
    assert set(sm.available_events("ARCHITECTURE_CONFIRM_PENDING")) == {"confirm", "reject"}
    assert sm.next_state("PLANNING_IMPLEMENTATION_SPECS", "complete") == "IMPLEMENTATION_SPEC_CONFIRM_PENDING"
    assert sm.next_state("IMPLEMENTATION_SPEC_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "CODING_CONFIRM_PENDING"
    assert sm.next_state("CODING_CONFIRM_PENDING", "confirm") == "TESTING"
    assert sm.next_state("CODING_CONFIRM_PENDING", "reject") == "REWORK"
    assert sm.next_state("TESTING", "complete") == "TESTING_CONFIRM_PENDING"
    assert sm.next_state("TESTING_CONFIRM_PENDING", "confirm") == "REVIEW"
    assert sm.next_state("REVIEW", "complete") == "REVIEW_CONFIRM_PENDING"
    assert sm.next_state("REVIEW_CONFIRM_PENDING", "confirm") == "ACCEPTANCE_PENDING"
    assert sm.next_state("ACCEPTANCE_PENDING", "confirm") == "MERGING"
    assert sm.next_state("MERGING", "complete") == "COMPLETED"


def test_agent_questions_resume_the_originating_stage():
    sm = StateMachine("feature")
    assert sm.next_state("TESTING", "ask_user") == "TESTING_INPUT_PENDING"
    assert sm.next_state("TESTING_INPUT_PENDING", "answer") == "TESTING"
    assert sm.next_state("REVIEW", "ask_user") == "REVIEW_INPUT_PENDING"
    assert sm.next_state("REVIEW_INPUT_PENDING", "answer") == "REVIEW"


def test_removed_product_spec_states_migrate_to_unified_requirements():
    sm = StateMachine("feature")
    assert sm.display_name("PLANNING_PRODUCT_SPEC") == sm.display_name("PLANNING_REQUIREMENTS")
    assert sm.next_state("PRODUCT_SPEC_CONFIRM_PENDING", "confirm") == "PLANNING_ARCHITECTURE"


def test_profiles_share_lifecycle_but_define_rework_targets():
    feature = StateMachine("feature")
    bug_fix = StateMachine("bug_fix")
    documentation = StateMachine("documentation")

    assert feature._template == bug_fix._template == documentation._template
    assert feature.available_events("REWORK") == [
        "to_requirements", "to_architecture",
        "to_specs", "to_coding", "to_testing",
    ]
    assert bug_fix.available_events("REWORK") == ["to_analysis", "to_coding", "to_testing"]
    assert documentation.available_events("REWORK") == ["to_outline", "to_coding"]


def test_bug_fix_state_machine():
    sm = StateMachine("bug_fix")
    assert sm.initial_state == "BUG_ANALYSIS"
    assert sm.next_state("BUG_ANALYSIS", "complete") == "BUG_FIX_CONFIRM_PENDING"
    assert sm.next_state("BUG_FIX_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "CODING_CONFIRM_PENDING"
    assert sm.next_state("CODING_CONFIRM_PENDING", "confirm") == "TESTING"
    assert sm.next_state("CODING", "bug_analysis_question") == "BUG_ANALYSIS"


def test_documentation_state_machine():
    sm = StateMachine("documentation")
    assert sm.initial_state == "DOC_OUTLINE"
    assert sm.next_state("DOC_OUTLINE", "complete") == "OUTLINE_CONFIRM_PENDING"
    assert sm.next_state("OUTLINE_CONFIRM_PENDING", "confirm") == "CODING"
    assert sm.next_state("CODING", "complete") == "CODING_CONFIRM_PENDING"
    assert sm.next_state("CODING_CONFIRM_PENDING", "confirm") == "REVIEW"
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
