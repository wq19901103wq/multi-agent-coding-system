"""Executable guards for the Codex-native architecture boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mac_system"


def test_progress_presentation_does_not_depend_on_runtime_or_persistence():
    source = (SRC / "core" / "root_progress.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert not any(
        module.startswith(("sqlmodel", "sqlalchemy", "mac_system.agents", "mac_system.core"))
        for module in imports
    )
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))


def test_removed_workflow_implementation_does_not_return():
    forbidden = [
        SRC / "state_machine.py",
        SRC / "agents" / "prompts.py",
        SRC / "core" / "failure_router.py",
        SRC / "core" / "loop_supervisor.py",
        SRC / "core" / "delivery_policy.py",
        SRC / "core" / "work_item.py",
        ROOT / "state_machines" / "delivery.json",
    ]
    assert not [str(path) for path in forbidden if path.exists()]


def test_mac_does_not_add_a_parallel_goal_state_machine():
    models = (SRC / "models.py").read_text(encoding="utf-8")
    service = (SRC / "core" / "project_service.py").read_text(encoding="utf-8")
    runtime = (SRC / "agents" / "root_runtime.py").read_text(encoding="utf-8")
    handlers = (SRC / "feishu" / "handlers.py").read_text(encoding="utf-8")

    assert "class ProjectGoal" not in models
    assert "goal: str" not in models.split("class ProjectOwnerState", 1)[1].split(
        "class UserOwnerProfile", 1
    )[0]
    assert "def update_project_owner_state" not in service
    assert "get_project_goal" not in service
    assert "claim_due_project_owner_goal_reviews" not in service
    assert '"thread/goal/' not in runtime
    assert '"/goal"' not in handlers


def test_root_has_no_mac_defined_workflow_prompt():
    source = (SRC / "core" / "root_agent.py").read_text(encoding="utf-8")
    assert "developer_instructions=None" in source
    assert "requirements_analyst" not in source
    assert "planner" not in source
    assert "tester" not in source
    assert "reviewer" not in source


def test_context_compaction_stays_inside_the_provider_native_turn():
    for path in (
        SRC / "core" / "root_agent.py",
        SRC / "agents" / "root_runtime.py",
        SRC / "agents" / "kimi_runtime.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "thread/compact/start" not in source
        assert "def compact_thread(" not in source
        assert "_compact_legacy_thread_if_needed" not in source


def test_feishu_natural_language_is_delivered_unchanged():
    source = (SRC / "feishu" / "handlers.py").read_text(encoding="utf-8")
    assert "visible_request = text" in source
    assert "visible_request=visible_request" in source
    assert "add_reaction(message_id, \"OnIt\")" in source
    assert "消息已收到" not in source
    assert "route_demand" not in source
    assert "suggest_name" not in source
    assert "StateMachine" not in source


def test_routing_boundary_has_no_prose_keyword_classifier():
    path = SRC / "feishu" / "handlers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = []
    prose_names = {"text", "content", "message", "description"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        names = {item.id for item in ast.walk(node.left) if isinstance(item, ast.Name)}
        if not names & prose_names:
            continue
        if any(
            isinstance(value, (ast.List, ast.Set, ast.Tuple))
            and len(value.elts) > 1
            and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts)
            for value in node.comparators
        ):
            violations.append(node.lineno)
    assert not violations


def test_all_product_messages_are_cards_through_outbox():
    violations = []
    allowed = {
        SRC / "feishu" / "bridge.py",
        SRC / "core" / "notification_service.py",
    }
    for path in SRC.rglob("*.py"):
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for marker in (".send_text(", "._send_interactive(", ".update_card("):
            if marker in source:
                violations.append(f"{path.relative_to(ROOT)}:{marker}")
    assert not violations


def test_generated_ids_have_64_random_bits():
    source = (SRC / "utils" / "identifiers.py").read_text(encoding="utf-8")
    assert 'secrets.token_hex(8)' in source


def test_deterministic_boundaries_do_not_rewrite_owner_semantics():
    owner = (SRC / "core" / "project_owner.py").read_text(encoding="utf-8")
    root = (SRC / "core" / "root_agent.py").read_text(encoding="utf-8")
    service = (SRC / "core" / "project_service.py").read_text(encoding="utf-8")
    runtime = (SRC / "agents" / "runtime.py").read_text(encoding="utf-8")

    assert "_DEFAULT_ADVICE_OPTIONS" not in owner
    assert "actionable_advice_options" not in owner
    assert "parent_decision_status" not in owner
    assert "parent_decision_status" not in root
    assert "_invalidate_owner_derivations_for_policy_change" not in service
    assert 'content.startswith("/")' not in service
    assert 'assignments[agent] = "claude_code"' not in runtime
