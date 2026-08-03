"""Architecture guards for the natural-language/Handler boundary.

These tests intentionally inspect source structure. A Handler may parse an
explicit protocol (slash commands, enum keys, paths), but it must not infer
user intent, select tools, or activate business rules from wording lists.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "src/mac_system/feishu/handlers.py"
INTENT = ROOT / "src/mac_system/core/intent.py"
PROJECT_SERVICE = ROOT / "src/mac_system/core/project_service.py"
CARD = ROOT / "src/mac_system/feishu/card.py"
MENU = ROOT / "src/mac_system/feishu/menu.py"
ROUTING_BOUNDARY_FILES = (
    HANDLER,
    INTENT,
    ROOT / "src/mac_system/agents/orchestrator.py",
    ROOT / "src/mac_system/core/project_service.py",
    ROOT / "src/mac_system/api/main.py",
    ROOT / "src/mac_system/feishu/ws_client.py",
)


def _assigned_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        return None
    return targets[0].id


def _is_string_collection(node: ast.AST | None) -> bool:
    return bool(
        isinstance(node, (ast.List, ast.Tuple, ast.Set))
        and len(node.elts) >= 2
        and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts)
    )


def _referenced_names(node: ast.AST) -> set[str]:
    names = {item.id.casefold() for item in ast.walk(node) if isinstance(item, ast.Name)}
    names.update(
        item.attr.casefold() for item in ast.walk(node) if isinstance(item, ast.Attribute)
    )
    return names


def test_all_routing_boundaries_reject_prose_keyword_classifiers():
    """Turn the prose-routing rule into a repository-wide executable guard.

    Protocol parsing may compare slash commands and enum IDs. What is forbidden
    is membership/regex classification over free-form user prose, or named
    keyword/synonym/starter lists in any deterministic routing boundary.
    """
    prose_names = {
        "text", "message", "msg", "content", "description", "reply",
        "user_message", "source_text", "raw_text",
    }
    classifier_name_fragments = (
        "keyword", "keywords", "synonym", "synonyms", "starter", "hint",
    )
    violations: list[str] = []
    for path in ROUTING_BOUNDARY_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                name = _assigned_name(node)
                if (
                    name
                    and _is_string_collection(node.value)
                    and any(part in name.casefold() for part in classifier_name_fragments)
                ):
                    violations.append(f"{path.name}:{node.lineno}:{name}")
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            ):
                if _referenced_names(node.left) & prose_names and any(
                    _is_string_collection(item) for item in node.comparators
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}:free-form prose membership classifier"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"search", "match", "fullmatch"}
                and len(node.args) >= 2
                and _referenced_names(node.args[1]) & prose_names
            ):
                violations.append(
                    f"{path.name}:{node.lineno}:regex classifier over free-form prose"
                )
    assert not violations, (
        "Natural language must be classified by an Agent into schema fields; "
        f"deterministic routing violations: {violations}"
    )


def test_handler_has_no_natural_language_keyword_router():
    tree = ast.parse(HANDLER.read_text(encoding="utf-8"), filename=str(HANDLER))
    violations: list[str] = []
    forbidden_fragments = ("keyword", "starter", "hint")
    # ``marker`` is allowed only for rendering/protocol presentation, never
    # for a collection that classifies user language.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        name = _assigned_name(node)
        value = node.value
        if not name or not _is_string_collection(value):
            continue
        lowered = name.casefold()
        if any(fragment in lowered for fragment in forbidden_fragments):
            violations.append(f"line {node.lineno}: {name}")
        if "marker" in lowered and name != "markdown_markers":
            violations.append(f"line {node.lineno}: {name}")

    source = HANDLER.read_text(encoding="utf-8")
    retired_case_patches = (
        "_match_repository_mention",
        "_requests_workspace_read",
        "_requests_remembered_repository_read",
        "_requests_capability_metadata",
        "_COMPLETE_REFERENCE_MARKERS",
        "_CUSTOM_ANSWER_OPTION_KEY_SYNONYMS",
        "_normalize_capability_bindings",
    )
    violations.extend(name for name in retired_case_patches if name in source)
    if "readonly_dirs=authorized_read_roots" not in source:
        violations.append("Coordinator does not receive configured host read roots")
    if 'action_type == "request_context"' not in source:
        violations.append("missing structured context negotiation boundary")
    if "_COORDINATOR_MAX_TOOL_CALLS = 8" not in source:
        violations.append("Coordinator read tool budget constant is not bounded")
    if "_COORDINATOR_MAX_DISTINCT_READ_FILES = 8" not in source:
        violations.append("Coordinator distinct-file budget is not bounded")
    if "max_tool_calls=(" not in source or "_COORDINATOR_MAX_TOOL_CALLS" not in source:
        violations.append("Coordinator first-turn read budget is not bounded")
    if "get_allowed_bind_roots as _configured_read_roots" not in source:
        violations.append("Coordinator read roots are not configuration-derived")
    assert not violations, (
        "Handler must consume Coordinator structure instead of classifying "
        f"natural-language wording: {violations}"
    )


def test_explicit_create_router_does_not_classify_description_words():
    source = INTENT.read_text(encoding="utf-8")
    forbidden = (
        "_contains_word",
        "_any_word",
        "BUG_FIX_STARTERS",
        "DOCUMENTATION_STARTERS",
        "FEATURE_EXPLICIT_STARTERS",
        "FEATURE_FALLBACK_HINTS",
        "命中 bug 修复关键词",
        "命中文档关键词",
    )
    found = [item for item in forbidden if item in source]
    assert not found, (
        "Explicit /create must use a neutral workflow or structured Agent "
        f"classification, not a wording list: {found}"
    )


def test_database_ids_use_at_least_64_random_bits():
    source = PROJECT_SERVICE.read_text(encoding="utf-8")
    assert "secrets.token_hex(8)" in source
    assert "secrets.token_hex(2)" not in source


def test_private_chat_cards_keep_internal_ids_in_callback_values_only():
    """Opaque database IDs are protocol fields, not user-facing content."""
    card_source = CARD.read_text(encoding="utf-8")
    menu_source = MENU.read_text(encoding="utf-8")
    assert 'f"审批ID：{approval_id}' not in card_source
    assert 'f"执行请求ID：{action_id}' not in menu_source
    assert 'f"执行请求 ID：{action_id}' not in menu_source


def test_user_visible_delivery_never_bypasses_card_outbox():
    """All product output must be cards delivered by the reliable boundary."""
    allowed = {
        ROOT / "src/mac_system/feishu/bridge.py",
        ROOT / "src/mac_system/core/notification_service.py",
    }
    violations: list[str] = []
    forbidden_calls = (
        ".send_text(",
        "._send_interactive(",
        ".send_content_card(",
        ".update_card(",
    )
    for path in (ROOT / "src/mac_system").rglob("*.py"):
        if path in allowed:
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(call in line for call in forbidden_calls):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}:{line.strip()}")
    assert not violations, (
        "User-visible Feishu output must go through deliver_card/CardProjection; "
        f"direct provider calls: {violations}"
    )
