#!/usr/bin/env python3
"""Import durable user preferences from local coding-agent histories."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import func
from sqlmodel import select

from mac_system.config import get_settings
from mac_system.core.owner_memory_import import (
    PreferenceSource,
    chunk_sources,
    collect_claude_sources,
    collect_codex_sources,
    collect_document_sources,
    collect_kimi_sources,
    collect_pi_sources,
    collect_zcode_sources,
    consolidate_candidates,
    deduplicate_sources,
    discover_agent_documents,
    preference_evidence_sources,
    run_memory_audit,
    run_extraction,
    source_archive_rows,
)
from mac_system.core.project_service import ProjectService
from mac_system.database import init_db, session_scope
from mac_system.models import Project, ProjectUser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-id", help="Target Feishu owner; defaults to the owner with most projects")
    parser.add_argument(
        "--bot-account", default="default", help="Bot account owning the target Feishu identity"
    )
    parser.add_argument("--apply", action="store_true", help="Persist extracted global preferences")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Extract only human sources not already present in the provenance archive",
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Archive original human inputs without running preference extraction",
    )
    parser.add_argument("--max-batch-chars", type=int, default=100_000)
    parser.add_argument("--report", type=Path, help="Write a JSON audit report")
    return parser.parse_args()


def default_open_id(bot_account_id: str) -> str:
    with session_scope() as session:
        rows = session.exec(
            select(ProjectUser.open_id, func.count(ProjectUser.project_id))
            .join(Project, Project.id == ProjectUser.project_id)
            .where(ProjectUser.role == "owner")
            .where(Project.bot_account_id == bot_account_id)
            .group_by(ProjectUser.open_id)
            .order_by(func.count(ProjectUser.project_id).desc())
        ).all()
    if not rows:
        raise RuntimeError("No Project Owner user exists; pass --open-id")
    return str(rows[0][0])


def main() -> int:
    args = parse_args()
    settings = get_settings()
    # The importer is also a maintenance entry point and may be run before the
    # server after an upgrade.  Apply additive migrations before reading new
    # Owner memory columns; existing data is preserved.
    init_db()
    home = Path.home()
    collectors = {
        "codex": lambda: collect_codex_sources(home / ".codex"),
        "claude": lambda: collect_claude_sources(home / ".claude"),
        "kimi": lambda: collect_kimi_sources(home / ".kimi"),
        "pi": lambda: collect_pi_sources(home / ".pi"),
        "zcode": lambda: collect_zcode_sources(home / ".zcode"),
        "agent_doc": lambda: collect_document_sources(discover_agent_documents(home)),
    }
    all_rows = []
    scan: dict[str, dict[str, int]] = {}
    for provider, collect in collectors.items():
        rows, skipped = collect()
        all_rows.extend(rows)
        scan[provider] = {"records": len(rows), "generated_records_skipped": skipped}
    open_id = args.open_id or default_open_id(args.bot_account)
    # MAC-native history is stored in the application database rather than a
    # provider transcript. Include complete human turns and legacy Feishu
    # messages before extraction so preference learning sees their public
    # question/result context.
    with session_scope() as session:
        mac_rows = [
            PreferenceSource(
                provider=item["provider"],
                source_ref=item["source_ref"],
                content=item["content"],
                timestamp=item.get("timestamp", ""),
                session_ref=item.get("session_ref", ""),
                turn_ref=item.get("turn_ref", ""),
                project_ref=item.get("project_ref", ""),
                context_before=item.get("context_before", ""),
                context_after=item.get("context_after", ""),
            )
            for item in ProjectService(
                session, bot_account_id=args.bot_account
            ).contextual_mac_history_sources(open_id)
        ]
    all_rows.extend(mac_rows)
    scan["mac"] = {"records": len(mac_rows), "generated_records_skipped": 0}
    rows = deduplicate_sources(all_rows)
    with session_scope() as session:
        archived_refs = ProjectService(
            session, bot_account_id=args.bot_account
        ).get_owner_source_refs(open_id)
    extraction_rows = preference_evidence_sources(rows)
    if args.incremental:
        extraction_rows = [
            row for row in extraction_rows if row.source_ref not in archived_refs
        ]
    candidates = []
    if not args.archive_only:
        # Agent instruction documents are archived for provenance/search only.
        # They describe a repository's process and are not human chat evidence
        # that may be promoted into a cross-project user preference.
        for index, batch in enumerate(
            chunk_sources(extraction_rows, max(20_000, args.max_batch_chars)), start=1
        ):
            print(f"Owner preference extraction batch {index}: {len(batch)} sources", flush=True)
            candidates.extend(run_extraction(batch, settings))

    # Preserve original sources here; the one audit pass below performs both
    # cross-batch semantic deduplication and reconciliation with active memory.
    candidates = consolidate_candidates(candidates, settings)
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in sorted(
        candidates,
        key=lambda value: (value.get("confidence") != "high", value.get("preference", "")),
    ):
        if item.get("confidence") == "low":
            continue
        normalized = " ".join(item.get("preference", "").split()).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(item)

    with session_scope() as session:
        existing_rows = ProjectService(
            session, bot_account_id=args.bot_account
        ).get_effective_owner_memories(open_id)
        existing_policies = [
            {
                "memory_ref": row.id,
                "content": row.content,
                "evidence": row.evidence,
                "source_ref": row.source_ref,
                "strength": row.strength,
                "category": row.category,
            }
            for row in existing_rows
        ]
    unique = (
        run_memory_audit(unique, existing_policies, settings)
        if unique and not args.archive_only
        else []
    )
    added = []
    archived_sources = 0
    removed_sources = 0
    if args.apply:
        with session_scope() as session:
            service = ProjectService(session, bot_account_id=args.bot_account)
            archived_sources, removed_sources = service.sync_owner_sources(
                open_id,
                source_archive_rows(rows),
                providers={*collectors, "mac"},
            )
            for item in unique:
                rows_added = service.add_owner_memories(
                    open_id,
                    [{
                        **item,
                        "scope": "global",
                        "replaces": item.get("replaces", ""),
                        "learning_type": "explicit",
                        "source_refs": [item["source_ref"]],
                    }],
                    project_id=None,
                    source_ref=item["source_ref"],
                )
                added.extend(row.content for row in rows_added)
            active_count = len(service.get_owner_memories(open_id))
    else:
        active_count = None
    report = {
        "open_id": open_id,
        "bot_account_id": args.bot_account,
        "applied": args.apply,
        "incremental": args.incremental,
        "scan": scan,
        "unique_sources": len(rows),
        "new_sources_considered": len(extraction_rows),
        "candidate_preferences": len(candidates),
        "accepted_preferences": unique,
        "new_memories": added,
        "archived_sources": archived_sources,
        "removed_sources": removed_sources,
        "active_memory_count": active_count,
        "provider_distribution": dict(Counter(row.provider for row in rows)),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
