import json
from pathlib import Path

import pytest

from mac_system.agents.root_runtime import TurnResult
from mac_system.config import Settings
from mac_system.core import owner_memory_import
from mac_system.core.owner_memory_import import (
    PreferenceSource,
    collect_claude_sources,
    collect_codex_sources,
    collect_document_sources,
    collect_kimi_sources,
    collect_pi_sources,
    collect_zcode_sources,
    consolidate_candidates,
    deduplicate_sources,
    preference_evidence_sources,
    source_archive_rows,
)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("extract_preferences", ([],)),
        ("audit_new_preferences", ([], [])),
    ],
)
async def test_owner_memory_operations_use_sol(monkeypatch, operation, arguments):
    captured = {}

    class FakeRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_turn(self, text, *, output_schema):
            return TurnResult(
                thread_id="memory-test",
                turn_id="turn-test",
                status="completed",
                final_message='{"preferences": []}',
                events=(),
            )

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(owner_memory_import, "CodexAppServerThread", FakeRuntime)

    result = await getattr(owner_memory_import, operation)(
        *arguments, settings=Settings(_env_file=None)
    )

    assert result == []
    assert captured["thread_config"]["model"] == "gpt-6.1-sol"
    assert captured["thread_config"]["model_reasoning_effort"] == "xhigh"
    assert captured["ephemeral"] is True
    assert captured["sandbox"] == "read-only"
    assert captured["closed"] is True


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_collectors_keep_human_prompts_and_drop_generated_envelopes(tmp_path):
    codex = tmp_path / ".codex"
    _jsonl(
        codex / "sessions" / "2026" / "x.jsonl",
        [
            {
                "timestamp": "2026-01-01",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "<in-app-browser-context>noise</in-app-browser-context>优先选择题",
                },
            }
        ],
    )
    claude = tmp_path / ".claude"
    _jsonl(
        claude / "projects" / "p" / "x.jsonl",
        [
            {
                "type": "user",
                "entrypoint": "sdk-cli",
                "isSidechain": False,
                "message": {"role": "user", "content": "generated contract"},
            },
            {
                "type": "user",
                "entrypoint": "cli",
                "isSidechain": False,
                "message": {"role": "user", "content": "架构尽量简单"},
            },
        ],
    )
    kimi = tmp_path / ".kimi"
    _jsonl(
        kimi / "sessions" / "a" / "b" / "context.jsonl",
        [
            {
                "role": "user",
                "content": "<system_contract>generated</system_contract><task>generated</task>",
            },
            {"role": "user", "content": "减少用户输入"},
        ],
    )
    pi = tmp_path / ".pi"
    _jsonl(
        pi / "agent" / "sessions" / "p" / "x.jsonl",
        [
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "先给结论"}],
                },
            }
        ],
    )

    codex_rows, _ = collect_codex_sources(codex)
    claude_rows, claude_skipped = collect_claude_sources(claude)
    kimi_rows, kimi_skipped = collect_kimi_sources(kimi)
    pi_rows, _ = collect_pi_sources(pi)

    assert [row.content for row in codex_rows] == ["优先选择题"]
    assert [row.content for row in claude_rows] == ["架构尽量简单"]
    assert claude_skipped == 1
    assert [row.content for row in kimi_rows] == ["减少用户输入"]
    assert kimi_skipped == 1
    assert [row.content for row in pi_rows] == ["先给结论"]
    assert "sessions/a/b/context.jsonl" in kimi_rows[0].source_ref
    assert kimi_rows[0].session_ref == "b"


def test_codex_collector_uses_session_originator_not_user_transport_role(tmp_path):
    codex = tmp_path / ".codex"
    _jsonl(
        codex / "sessions" / "2026" / "desktop.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"originator": "Codex Desktop"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "保留真人输入"},
            },
        ],
    )
    _jsonl(
        codex / "sessions" / "2026" / "mac.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"originator": "mac-system"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Owner 决定：继续执行。",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "飞书真人输入"},
            },
        ],
    )

    rows, skipped = collect_codex_sources(codex)

    assert [row.content for row in rows] == ["保留真人输入"]
    assert skipped == 2


def test_zcode_deduplicates_accumulated_user_history_and_reads_agent_docs(tmp_path):
    zcode = tmp_path / ".zcode"
    body = {
        "input": [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": "<system-reminder>generated protocol context</system-reminder>",
            },
            {"role": "user", "content": "重要信息展示完整"},
        ]
    }
    _jsonl(
        zcode / "cli" / "rollout" / "model-io-x.jsonl",
        [
            {"request": {"body": body}, "startedAt": "1"},
            {"request": {"body": body}, "startedAt": "2"},
        ],
    )
    doc = tmp_path / "repo" / "AGENTS.md"
    doc.parent.mkdir()
    doc.write_text("所有修复都要通用。", encoding="utf-8")

    zcode_rows, skipped = collect_zcode_sources(zcode)
    doc_rows, _ = collect_document_sources([doc])
    unique = deduplicate_sources([*zcode_rows, *doc_rows])

    assert len(zcode_rows) == 2
    assert skipped == 2
    assert [row.content for row in unique] == [
        "重要信息展示完整",
        "所有修复都要通用。",
    ]
    archived = source_archive_rows(unique)
    assert archived[0]["kind"] == "human_input"
    assert archived[1]["kind"] == "user_document"


def test_agent_documents_are_archived_but_not_global_preference_evidence():
    rows = [
        PreferenceSource(
            provider="mac", source_ref="human:1", content="架构保持简单"
        ),
        PreferenceSource(
            provider="agent_doc", source_ref="agent_doc:repo", content="必须跑 T5"
        ),
    ]

    assert [row.source_ref for row in preference_evidence_sources(rows)] == ["human:1"]
    assert [row["kind"] for row in source_archive_rows(rows)] == [
        "human_input",
        "user_document",
    ]


def test_candidate_consolidation_preserves_original_human_provenance():
    candidate = {
        "preference": "架构保持简单",
        "evidence": "整个架构还是要简洁",
        "source_ref": "mac:turn-1:user",
        "category": "architecture",
        "strength": "hard",
        "confidence": "high",
    }

    assert consolidate_candidates([candidate, dict(candidate)]) == [candidate]


def test_equal_text_in_different_turns_is_preserved(tmp_path):
    claude = tmp_path / ".claude"
    _jsonl(
        claude / "history.jsonl",
        [
            {"display": "继续", "sessionId": "s1", "timestamp": 1, "project": "/p"},
            {"display": "继续", "sessionId": "s1", "timestamp": 2, "project": "/p"},
            {"display": "exit", "sessionId": "s1", "timestamp": 3, "project": "/p"},
        ],
    )

    rows, _ = collect_claude_sources(claude)
    unique = deduplicate_sources(rows)

    assert [row.content for row in unique] == ["继续", "继续"]
    assert unique[0].turn_ref != unique[1].turn_ref
