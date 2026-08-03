"""Runtime/deployment revision bookkeeping for acceptance readiness."""

from __future__ import annotations

from typing import Any


class RuntimeManager:
    """Keep delivery revision separate from the process observed at acceptance."""

    @staticmethod
    def mark_artifact_revision(context: dict[str, Any], revision: str) -> dict[str, Any]:
        runtime = dict(context.get("runtime_snapshot") or {})
        if runtime.get("artifact_revision") != revision:
            runtime.update({
                "artifact_revision": revision,
                "deployment_status": "unknown",
            })
        return {**context, "runtime_snapshot": runtime}

    @staticmethod
    def record_observed_runtime(
        context: dict[str, Any], *, revision: str, environment: dict[str, Any]
    ) -> dict[str, Any]:
        runtime = dict(context.get("runtime_snapshot") or {})
        runtime.update({
            "artifact_revision": revision,
            "running_revision": revision,
            "deployment_status": "observed",
            "entry_points": list(environment.get("entry_points") or []),
            "started_services": list(environment.get("started_services") or []),
        })
        return {**context, "runtime_snapshot": runtime}

    @staticmethod
    def verification_contract(context: dict[str, Any]) -> dict[str, Any]:
        """Describe the delivery/runtime revision boundary to quality agents."""
        runtime = dict(context.get("runtime_snapshot") or {})
        return {
            "expected_artifact_revision": runtime.get("artifact_revision"),
            "last_observed_running_revision": runtime.get("running_revision"),
            "deployment_status": runtime.get("deployment_status", "unknown"),
            "known_entry_points": list(runtime.get("entry_points") or []),
            "known_started_services": list(runtime.get("started_services") or []),
        }

    @staticmethod
    def acceptance_contract(
        context: dict[str, Any],
        evidence_revision_key: str,
        *,
        workspace_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = dict(context.get("runtime_snapshot") or {})
        snapshot = dict(workspace_snapshot or {})
        artifact_revision = str(
            snapshot.get("digest") or runtime.get("artifact_revision") or ""
        )
        return {
            **RuntimeManager.verification_contract(context),
            # The evidence key includes prompts/specs/external evidence and is
            # not a Git revision. Keep it explicit so an Agent never compares
            # this opaque cache key with ``git rev-parse HEAD``.
            "evidence_revision_key": evidence_revision_key,
            "expected_artifact_revision": artifact_revision,
            "artifact_revision_semantics": "git_tree_plus_visible_workspace_changes_sha256",
            "expected_git_head": snapshot.get("git_head"),
            "expected_git_tree": snapshot.get("git_tree"),
        }
