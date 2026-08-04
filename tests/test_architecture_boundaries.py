"""Executable guards for the Codex-native architecture boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mac_system"


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


def test_root_has_no_mac_defined_workflow_prompt():
    source = (SRC / "core" / "root_agent.py").read_text(encoding="utf-8")
    assert "developer_instructions=None" in source
    assert "requirements_analyst" not in source
    assert "planner" not in source
    assert "tester" not in source
    assert "reviewer" not in source


def test_feishu_natural_language_is_delivered_unchanged():
    source = (SRC / "feishu" / "handlers.py").read_text(encoding="utf-8")
    assert "_deliver_to_root(ctx, project, text)" in source
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
    source = (SRC / "core" / "project_service.py").read_text(encoding="utf-8")
    assert 'secrets.token_hex(8)' in source
