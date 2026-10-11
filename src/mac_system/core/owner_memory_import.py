"""Offline import of explicit user preferences from coding-agent histories."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from mac_system.agents.root_runtime import CodexAppServerThread
from mac_system.config import Settings
from mac_system.utils.redaction import redact_text


_SYSTEM_CONTRACT_RE = re.compile(
    r"<system_contract>.*?</system_contract>", re.DOTALL | re.IGNORECASE
)
_AMBIENT_CONTEXT_RE = re.compile(
    r"<in-app-browser-context\b.*?</in-app-browser-context>",
    re.DOTALL | re.IGNORECASE,
)
_GENERATED_ENVELOPE_RE = re.compile(
    r"^\s*<(?:system_contract|system-reminder|local-command-caveat|"
    r"command-name|command-message|command-args|local-command-stdout)\b",
    re.IGNORECASE,
)
_DOC_NAMES = {"AGENT.md", "AGENTS.md", "CLAUDE.md"}
_PRUNED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "site-packages",
    "Library", "Caches", "cache", "dist", "build", "__pycache__",
}


@dataclass(frozen=True)
class PreferenceSource:
    provider: str
    source_ref: str
    content: str
    timestamp: str = ""
    session_ref: str = ""
    turn_ref: str = ""
    project_ref: str = ""
    context_before: str = ""
    context_after: str = ""


def _json_lines(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                yield line_number, value


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in {
            "text", "input_text"
        }:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _clean_human_text(text: str) -> str:
    text = _SYSTEM_CONTRACT_RE.sub("", text)
    text = _AMBIENT_CONTEXT_RE.sub("", text)
    return redact_text(text).strip()


def _is_generated_envelope(text: str) -> bool:
    """Recognize provider protocol records that merely use the user role.

    Several coding-agent protocols encode system reminders, delegated task
    contracts, and local-command events as ``role=user`` messages.  The
    wrapper is protocol provenance, not natural-language intent, so filtering
    it here does not introduce keyword routing.
    """

    return bool(_GENERATED_ENVELOPE_RE.match(text))


def _relative_source_path(path: Path, home: Path) -> str:
    try:
        return path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


def _ref(provider: str, path: Path, line: int, content: str, *, home: Path) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{provider}:{_relative_source_path(path, home)}:{line}:{digest}"


def collect_codex_sources(home: Path) -> tuple[list[PreferenceSource], int]:
    files = [*home.glob("sessions/**/*.jsonl"), *home.glob("archived_sessions/*.jsonl")]
    rows: list[PreferenceSource] = []
    skipped_generated = 0
    for path in files:
        originator = ""
        session_ref = path.stem
        project_ref = ""
        previous_public = ""
        pending_row: int | None = None
        for line, value in _json_lines(path):
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            if value.get("type") == "session_meta":
                originator = str(payload.get("originator") or "")
                session_ref = str(payload.get("id") or session_ref)
                project_ref = str(payload.get("cwd") or "")
                continue
            if value.get("type") == "event_msg" and payload.get("type") == "agent_message":
                previous_public = _clean_human_text(str(payload.get("message") or ""))[-4_000:]
                if previous_public and pending_row is not None:
                    rows[pending_row] = replace(
                        rows[pending_row], context_after=previous_public
                    )
                    pending_row = None
                continue
            if value.get("type") != "event_msg" or payload.get("type") != "user_message":
                continue
            # MAC is itself a Codex client.  Its persistent root threads mix
            # human Feishu messages with Owner continuations that necessarily
            # use Codex's user-message transport.  The authoritative human
            # inputs already live in ProjectHistoryRound with explicit origin
            # metadata, so importing these transport sessions would turn
            # system-authored continuations into user preferences.
            if originator == "mac-system":
                skipped_generated += 1
                continue
            raw = payload.get("message")
            if not isinstance(raw, str):
                continue
            content = _clean_human_text(raw)
            if content:
                rows.append(
                    PreferenceSource(
                        "codex", _ref("codex", path, line, content, home=home), content,
                        str(value.get("timestamp") or ""),
                        session_ref=session_ref,
                        turn_ref=str(payload.get("turn_id") or payload.get("turnId") or line),
                        project_ref=project_ref,
                        context_before=previous_public,
                    )
                )
                pending_row = len(rows) - 1
    return rows, skipped_generated


def collect_claude_sources(home: Path) -> tuple[list[PreferenceSource], int]:
    """Read Claude's keyboard history, with transcript fallback for old installs."""

    rows: list[PreferenceSource] = []
    skipped_generated = 0
    # Claude Code records actual keyboard submissions separately from the
    # provider transcript.  Import it as the authoritative human source; the
    # source_ref includes session + timestamp so repeated text in a different
    # turn remains a distinct user event.
    history = home / "history.jsonl"
    for line, value in _json_lines(history):
        content = _clean_human_text(str(value.get("display") or ""))
        if not content or content.casefold() == "exit":
            continue
        session_ref = str(value.get("sessionId") or "")
        timestamp = str(value.get("timestamp") or "")
        identity = f"{session_ref}:{timestamp}:{content}"
        rows.append(
            PreferenceSource(
                "claude",
                _ref("claude", history, line, identity, home=home),
                content,
                timestamp,
                session_ref=session_ref,
                turn_ref=timestamp or str(line),
                project_ref=str(value.get("project") or ""),
            )
        )
    if rows:
        return rows, skipped_generated

    # Older Claude versions may lack history.jsonl. In that case only, inspect
    # provider transcripts for direct CLI entries; SDK sessions are generated
    # agent contracts and are intentionally not treated as human authority.
    for path in home.glob("projects/**/*.jsonl"):
        for line, value in _json_lines(path):
            message = value.get("message")
            if value.get("type") != "user" or not isinstance(message, dict):
                continue
            if value.get("entrypoint") != "cli" or value.get("isSidechain"):
                skipped_generated += 1
                continue
            if value.get("toolUseResult") is not None:
                skipped_generated += 1
                continue
            raw = message.get("content")
            if not isinstance(raw, str) or _is_generated_envelope(raw):
                skipped_generated += 1
                continue
            content = _clean_human_text(raw)
            if content:
                rows.append(
                    PreferenceSource(
                        "claude",
                        _ref("claude", path, line, content, home=home),
                        content,
                        str(value.get("timestamp") or ""),
                        session_ref=str(value.get("sessionId") or path.stem),
                        turn_ref=str(value.get("uuid") or line),
                        project_ref=_relative_source_path(path.parent, home),
                    )
                )
    return rows, skipped_generated


def collect_kimi_sources(home: Path) -> tuple[list[PreferenceSource], int]:
    rows: list[PreferenceSource] = []
    skipped_generated = 0
    for path in home.glob("sessions/**/context.jsonl"):
        previous_public = ""
        pending_row: int | None = None
        for line, value in _json_lines(path):
            if value.get("role") == "assistant":
                public = _clean_human_text(_content_text(value.get("content")))[-4_000:]
                if public:
                    previous_public = public
                    if pending_row is not None:
                        rows[pending_row] = replace(rows[pending_row], context_after=public)
                        pending_row = None
                continue
            if value.get("role") != "user":
                continue
            raw = _content_text(value.get("content"))
            if _is_generated_envelope(raw):
                skipped_generated += 1
                continue
            content = _clean_human_text(raw)
            if not content:
                skipped_generated += 1
                continue
            rows.append(
                    PreferenceSource(
                    "kimi", _ref("kimi", path, line, content, home=home), content,
                    str(value.get("timestamp") or ""),
                    session_ref=path.parent.name,
                    turn_ref=str(value.get("id") or line),
                    project_ref=_relative_source_path(path.parent, home),
                    context_before=previous_public,
                )
            )
            pending_row = len(rows) - 1
    return rows, skipped_generated


def collect_pi_sources(home: Path) -> tuple[list[PreferenceSource], int]:
    rows: list[PreferenceSource] = []
    for path in home.glob("agent/sessions/**/*.jsonl"):
        previous_public = ""
        pending_row: int | None = None
        for line, value in _json_lines(path):
            message = value.get("message")
            if (
                value.get("type") == "message"
                and isinstance(message, dict)
                and message.get("role") == "assistant"
            ):
                public = _clean_human_text(_content_text(message.get("content")))[-4_000:]
                if public:
                    previous_public = public
                    if pending_row is not None:
                        rows[pending_row] = replace(rows[pending_row], context_after=public)
                        pending_row = None
                continue
            if (
                value.get("type") != "message"
                or not isinstance(message, dict)
                or message.get("role") != "user"
            ):
                continue
            content = _clean_human_text(_content_text(message.get("content")))
            if content:
                rows.append(
                    PreferenceSource(
                        "pi", _ref("pi", path, line, content, home=home), content,
                        str(value.get("timestamp") or message.get("timestamp") or ""),
                        session_ref=path.stem,
                        turn_ref=str(message.get("id") or line),
                        project_ref=_relative_source_path(path.parent, home),
                        context_before=previous_public,
                    )
                )
                pending_row = len(rows) - 1
    return rows, 0


def collect_zcode_sources(home: Path) -> tuple[list[PreferenceSource], int]:
    rows: list[PreferenceSource] = []
    skipped_generated = 0
    for path in home.glob("cli/rollout/model-io-*.jsonl"):
        for line, value in _json_lines(path):
            request = value.get("request")
            body = request.get("body") if isinstance(request, dict) else None
            if not isinstance(body, dict):
                continue
            messages = body.get("input") or body.get("messages") or []
            if not isinstance(messages, list):
                continue
            for message_index, message in enumerate(messages):
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                raw = message.get("content")
                # Tool results may use user-role arrays; only human text blocks
                # are accepted by _content_text.
                raw_text = _content_text(raw)
                if _is_generated_envelope(raw_text):
                    skipped_generated += 1
                    continue
                content = _clean_human_text(raw_text)
                if not content:
                    skipped_generated += 1
                    continue
                rows.append(
                    PreferenceSource(
                        "zcode", _ref("zcode", path, line, content, home=home), content,
                        str(value.get("startedAt") or ""),
                        session_ref=path.stem,
                        turn_ref=str(message_index),
                        project_ref=_relative_source_path(path.parent, home),
                        context_before=(
                            _clean_human_text(_content_text(messages[message_index - 1].get("content")))[-4_000:]
                            if message_index > 0
                            and isinstance(messages[message_index - 1], dict)
                            and messages[message_index - 1].get("role") == "assistant"
                            else ""
                        ),
                        context_after=(
                            _clean_human_text(_content_text(messages[message_index + 1].get("content")))[-4_000:]
                            if message_index + 1 < len(messages)
                            and isinstance(messages[message_index + 1], dict)
                            and messages[message_index + 1].get("role") == "assistant"
                            else ""
                        ),
                    )
                )
    return rows, skipped_generated


def discover_agent_documents(user_home: Path) -> list[Path]:
    """Use Spotlight on macOS, with a bounded filesystem fallback."""

    paths: set[Path] = set()
    try:
        result = subprocess.run(
            [
                "mdfind",
                'kMDItemFSName == "AGENTS.md"c || '
                'kMDItemFSName == "AGENT.md"c || '
                'kMDItemFSName == "CLAUDE.md"c',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        for raw in result.stdout.splitlines():
            path = Path(raw).expanduser()
            try:
                path.relative_to(user_home)
            except ValueError:
                continue
            if path.name in _DOC_NAMES and not (_PRUNED_DIRS & set(path.parts)):
                paths.add(path.resolve())
    except (OSError, subprocess.TimeoutExpired):
        pass
    for root in [user_home, user_home / ".codex", user_home / ".claude"]:
        if not root.exists():
            continue
        for name in _DOC_NAMES:
            path = root / name
            if path.is_file():
                paths.add(path.resolve())
    return sorted(paths)


def collect_document_sources(paths: Iterable[Path]) -> tuple[list[PreferenceSource], int]:
    rows: list[PreferenceSource] = []
    for path in paths:
        try:
            content = redact_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        content = content.strip()
        if not content:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        rows.append(PreferenceSource("agent_doc", f"agent_doc:{path}:{digest}", content))
    return rows, 0


def source_archive_rows(rows: Iterable[PreferenceSource]) -> list[dict[str, str]]:
    """Project collected human records into the Owner's read-only archive."""

    return [
        {
            "provider": row.provider,
            "kind": "user_document" if row.provider == "agent_doc" else "human_input",
            "source_ref": row.source_ref,
            "timestamp": row.timestamp,
            "session_ref": row.session_ref,
            "turn_ref": row.turn_ref,
            "project_ref": row.project_ref,
            "context_before": row.context_before,
            "context_after": row.context_after,
            "content": row.content,
        }
        for row in rows
    ]


def deduplicate_sources(rows: Iterable[PreferenceSource]) -> list[PreferenceSource]:
    """Drop only replayed provider events, never equal human wording.

    A repeated "继续" or the same choice in another session is separate
    evidence.  Providers such as ZCode replay accumulated history, so their
    stable session/turn/source identity must be used instead of text equality.
    """

    seen: set[str] = set()
    result: list[PreferenceSource] = []
    for row in rows:
        normalized = " ".join(row.content.split()).casefold()
        if not normalized:
            continue
        identity = row.source_ref
        if row.provider == "zcode":
            identity = f"{row.session_ref}:{row.turn_ref}:{normalized}"
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def chunk_sources(rows: list[PreferenceSource], max_chars: int) -> list[list[PreferenceSource]]:
    chunks: list[list[PreferenceSource]] = []
    current: list[PreferenceSource] = []
    current_chars = 0
    for row in rows:
        # Preserve every record while bounding pathological generated prompts.
        bounded = PreferenceSource(
            row.provider,
            row.source_ref,
            row.content[:20_000],
            row.timestamp,
            row.session_ref,
            row.turn_ref,
            row.project_ref,
            row.context_before[:4_000],
            row.context_after[:4_000],
        )
        size = len(bounded.content) + len(bounded.source_ref) + 100
        if current and current_chars + size > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(bounded)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def preference_evidence_sources(
    rows: list[PreferenceSource],
) -> list[PreferenceSource]:
    """Return human event sources eligible for global preference extraction.

    Repository instruction documents remain in the searchable provenance
    archive, but cannot become cross-project user authority.
    """

    return [row for row in rows if row.provider != "agent_doc"]


_EXTRACT_INSTRUCTIONS = """
你是 Project Owner 的离线偏好审计器。输入只包含 coding-agent 中记录为 user 的历史消息。
记录为 user 不代表一定由真人直接输入：忽略系统合同、Agent 角色说明、任务包装、
工具结果、项目事实和一次性需求。只提取用户明确表达、可以跨项目复用的工作偏好、交互偏好、授权边界
和质量原则。不要把单次技术选择升级为长期偏好。evidence 必须是对应 source content 的逐字子串，
context_before/context_after 只用于解释“继续”“1”“不是这个”等指代，不能作为偏好证据，也不能覆盖
用户原话。相同文字出现在不同 Turn 时是不同的选择证据，不得仅因文字相同合并。source_ref 必须逐字
使用输入值。证据不充分就不要输出。最终只返回 schema 对象。
""".strip()


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["preferences"],
        "properties": {
            "preferences": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "preference", "evidence", "source_ref", "category",
                        "strength", "confidence",
                    ],
                    "properties": {
                        "preference": {"type": "string", "maxLength": 500},
                        "evidence": {"type": "string", "maxLength": 500},
                        "source_ref": {"type": "string", "maxLength": 800},
                        "category": {"type": "string", "maxLength": 80},
                        "strength": {"type": "string", "enum": ["hard", "default"]},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                },
            }
        },
    }


async def extract_preferences(
    rows: list[PreferenceSource], settings: Settings
) -> list[dict[str, str]]:
    payload = {
        "sources": [
            {
                "provider": row.provider,
                "source_ref": row.source_ref,
                "timestamp": row.timestamp,
                "session_ref": row.session_ref,
                "turn_ref": row.turn_ref,
                "project_ref": row.project_ref,
                "context_before": row.context_before,
                "content": row.content,
                "context_after": row.context_after,
            }
            for row in rows
        ]
    }
    with tempfile.TemporaryDirectory(prefix="mac-owner-memory-import-") as raw_dir:
        owner_dir = Path(raw_dir)
        runtime = CodexAppServerThread(
            codex_bin=settings.codex_bin,
            cwd=owner_dir,
            tools=[],
            developer_instructions=_EXTRACT_INSTRUCTIONS,
            request_timeout_seconds=min(240, max(60, settings.agent_timeout_seconds)),
            sandbox="read-only",
            approval_policy="never",
            ephemeral=True,
            environments=[],
            thread_config={
                "model": "gpt-6.1-sol",
                "model_provider": "mac_openai_https",
                "model_reasoning_effort": "xhigh",
                "model_reasoning_summary": "none",
                "model_providers.mac_openai_https": {
                    "name": "OpenAI HTTPS",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "supports_websockets": False,
                    "stream_max_retries": 10,
                },
            },
            codex_home=owner_dir / "codex-home",
        )
        try:
            result = await runtime.run_turn(
                json.dumps(payload, ensure_ascii=False), output_schema=_output_schema()
            )
        finally:
            await runtime.close()
    if result.status != "completed" or not result.final_message:
        raise RuntimeError(f"Owner preference extraction failed: {result.status}")
    parsed = json.loads(result.final_message)
    candidates = parsed.get("preferences") if isinstance(parsed, dict) else []
    by_ref = {row.source_ref: row.content for row in rows}
    valid: list[dict[str, str]] = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        evidence = str(item.get("evidence") or "")
        if evidence and evidence in by_ref.get(source_ref, ""):
            valid.append({key: str(value) for key, value in item.items()})
    return valid


def run_extraction(rows: list[PreferenceSource], settings: Settings) -> list[dict[str, str]]:
    return asyncio.run(extract_preferences(rows, settings))


def consolidate_candidates(
    candidates: list[dict[str, str]], settings: Settings | None = None
) -> list[dict[str, str]]:
    """Drop exact duplicate candidates without changing their provenance.

    Semantic reconciliation belongs to the single audit pass below. Re-running
    extraction on synthetic ``candidate:*`` messages loses the original human
    source and makes an Owner inference look like new user authority.
    """

    del settings  # Kept for maintenance-script compatibility.
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in candidates:
        preference = " ".join(str(item.get("preference") or "").split())
        evidence = str(item.get("evidence") or "")
        source_ref = str(item.get("source_ref") or "")
        identity = (preference.casefold(), evidence, source_ref)
        if not preference or not evidence or not source_ref or identity in seen:
            continue
        seen.add(identity)
        result.append(dict(item))
    return result


_AUDIT_INSTRUCTIONS = """
你是 Project Owner 记忆库的合并审计器。existing_policies 已经生效；candidates 是新提取的候选。
一次完成两件事：先合并 candidates 内语义重复的候选，再与 existing_policies 对照。每组重复候选
只保留一条证据最清楚的原始候选，不要合成新措辞或新来源。只输出对现有记忆有实质新增价值、
可跨项目复用且证据明确的候选。仅措辞不同不算新增。与现有记忆冲突时不要输出，除非候选证据明确是
更晚的用户纠正，此时 replaces 必须逐字填写被替换记忆的 memory_ref；否则 replaces 为空。
evidence 必须是候选 original_evidence 的逐字子串，
source_ref 必须逐字使用候选 source_ref。不要改写来源。最终只返回 schema 对象。
""".strip()


def _audit_schema() -> dict[str, Any]:
    schema = _output_schema()
    item = schema["properties"]["preferences"]["items"]
    item["required"].append("replaces")
    item["properties"]["replaces"] = {"type": "string", "maxLength": 500}
    return schema


async def audit_new_preferences(
    candidates: list[dict[str, str]],
    existing_policies: list[dict[str, str]],
    settings: Settings,
) -> list[dict[str, str]]:
    payload = {
        "existing_policies": existing_policies,
        "candidates": [
            {
                "source_ref": item.get("source_ref", ""),
                "preference": item.get("preference", ""),
                "original_evidence": item.get("evidence", ""),
                "category": item.get("category", "general"),
                "strength": item.get("strength", "default"),
            }
            for item in candidates
        ],
    }
    with tempfile.TemporaryDirectory(prefix="mac-owner-memory-audit-") as raw_dir:
        owner_dir = Path(raw_dir)
        runtime = CodexAppServerThread(
            codex_bin=settings.codex_bin,
            cwd=owner_dir,
            tools=[],
            developer_instructions=_AUDIT_INSTRUCTIONS,
            request_timeout_seconds=min(240, max(60, settings.agent_timeout_seconds)),
            sandbox="read-only",
            approval_policy="never",
            ephemeral=True,
            environments=[],
            thread_config={
                "model": "gpt-6.1-sol",
                "model_provider": "mac_openai_https",
                "model_reasoning_effort": "xhigh",
                "model_reasoning_summary": "none",
                "model_providers.mac_openai_https": {
                    "name": "OpenAI HTTPS",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "supports_websockets": False,
                    "stream_max_retries": 10,
                },
            },
            codex_home=owner_dir / "codex-home",
        )
        try:
            result = await runtime.run_turn(
                json.dumps(payload, ensure_ascii=False), output_schema=_audit_schema()
            )
        finally:
            await runtime.close()
    if result.status != "completed" or not result.final_message:
        raise RuntimeError(f"Owner memory audit failed: {result.status}")
    parsed = json.loads(result.final_message)
    values = parsed.get("preferences") if isinstance(parsed, dict) else []
    by_ref: dict[str, list[dict[str, str]]] = {}
    for candidate in candidates:
        by_ref.setdefault(str(candidate.get("source_ref") or ""), []).append(candidate)
    existing_refs = {
        str(item.get("memory_ref") or item.get("id") or "")
        for item in existing_policies
        if str(item.get("memory_ref") or item.get("id") or "")
    }
    accepted: list[dict[str, str]] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        evidence = str(item.get("evidence") or "")
        preference = str(item.get("preference") or "")
        source = next(
            (
                candidate
                for candidate in by_ref.get(source_ref, [])
                if evidence in str(candidate.get("evidence") or "")
                and preference == str(candidate.get("preference") or "")
            ),
            None,
        )
        replaces = str(item.get("replaces") or "")
        if source is None:
            continue
        if replaces and replaces not in existing_refs:
            continue
        # Persist the original extracted candidate, never an audit-stage rewrite.
        accepted.append({**source, "replaces": replaces})
    return accepted


def run_memory_audit(
    candidates: list[dict[str, str]],
    existing_policies: list[dict[str, str]],
    settings: Settings,
) -> list[dict[str, str]]:
    return asyncio.run(audit_new_preferences(candidates, existing_policies, settings))
