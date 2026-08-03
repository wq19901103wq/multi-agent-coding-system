import hashlib
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from git import Repo


logger = logging.getLogger(__name__)


class WorktreeManagerError(Exception):
    pass


def _is_generated_runtime_path(path: str) -> bool:
    """Return whether a merge conflict is confined to disposable runtime state.

    These files are written while an app is running and must never make an
    agent branch authoritative over the user's live workspace copy.
    """
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith(".data/")
        or normalized.startswith(".mac/")
        or normalized.endswith(".db-shm")
        or normalized.endswith(".db-wal")
    )


def _is_removable_generated_runtime_path(path: str) -> bool:
    """Return whether an agent may remove a legacy tracked runtime artifact.

    Runtime bytes must never be committed as additions or modifications, but a
    project still needs a way to migrate old repositories that accidentally
    tracked them.  ``.mac`` remains orchestrator-owned and is never committed,
    including deletions.
    """
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith(".data/")
        or normalized.endswith(".db-shm")
        or normalized.endswith(".db-wal")
    )


def _stash_is_redundant_untracked_collision(
    workspace: Path, stash_commit: str
) -> bool:
    """Return true when a failed stash restore would change no bytes.

    ``git stash apply -u`` exits non-zero when the merge has just added an
    untracked path that already existed in the pre-merge workspace, even when
    both copies are byte-identical.  Treating that as data loss caused a
    successful quality-task merge to be retried indefinitely.  We only relax
    the error for the narrow safe case: the stash has no tracked changes and
    every untracked blob is already present with identical content.
    """
    no_tracked_changes = subprocess.run(
        ["git", "diff", "--quiet", f"{stash_commit}^1", stash_commit, "--"],
        cwd=workspace,
        check=False,
    ).returncode == 0
    if not no_tracked_changes:
        return False
    listed = subprocess.run(
        ["git", "ls-tree", "-rz", "--name-only", f"{stash_commit}^3"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return False
    paths = [value for value in listed.stdout.split(b"\0") if value]
    if not paths:
        return False
    workspace_resolved = workspace.resolve()
    for raw_path in paths:
        try:
            relative = raw_path.decode("utf-8", errors="strict")
            current = (workspace / relative).resolve()
        except (UnicodeDecodeError, OSError):
            return False
        if current != workspace_resolved and workspace_resolved not in current.parents:
            return False
        expected = subprocess.run(
            ["git", "show", f"{stash_commit}^3:{relative}"],
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if expected.returncode != 0 or not current.is_file():
            return False
        if current.read_bytes() != expected.stdout:
            return False
    return True


class WorktreeManager:
    def __init__(self, projects_dir: Path):
        self.projects_dir = Path(projects_dir).resolve()

    def create_worktree(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
        stage: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> Path:
        workspace = self._workspace(project_id, workspace_path)
        self._ensure_orchestrator_exclude(workspace)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name_parts = [agent_type]
        if stage:
            name_parts.append(stage)
        name_parts.extend([task_id, timestamp])
        worktree_name = "_".join(name_parts)
        worktree_path = (self._worktrees_dir(project_id, workspace) / worktree_name).resolve()

        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        # Ensure worktree parent exists
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create worktree with new branch based on current HEAD
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

        # Create .mac directory in worktree
        (worktree_path / ".mac").mkdir(exist_ok=True)
        self._link_ignored_dependencies(workspace, worktree_path)
        self.validate_worktree_identity(
            project_id,
            task_id,
            agent_type,
            worktree_path,
            workspace_path=workspace,
        )

        return worktree_path

    @staticmethod
    def _link_ignored_dependencies(workspace: Path, worktree: Path) -> None:
        """Reuse a Node workspace's installed dependencies without copying source."""
        source = workspace / "node_modules"
        target = worktree / "node_modules"
        if not source.is_dir() or target.exists() or target.is_symlink():
            return
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", "node_modules"],
            cwd=workspace,
            check=False,
            capture_output=True,
        )
        if ignored.returncode == 0:
            repo = Repo(workspace)
            exclude_path = Path(repo.git_dir) / "info" / "exclude"
            existing = exclude_path.read_text() if exclude_path.exists() else ""
            if "/node_modules" not in {line.strip() for line in existing.splitlines()}:
                prefix = "" if not existing or existing.endswith("\n") else "\n"
                with exclude_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{prefix}/node_modules\n")
            target.symlink_to(source, target_is_directory=True)

    @staticmethod
    def workspace_fingerprint(workspace: str | Path) -> dict[str, Any]:
        """Fingerprint non-runtime Git-visible workspace mutations."""
        root = Path(workspace).resolve()

        def _git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=root, check=False, capture_output=True, text=True
            )
            if result.returncode != 0:
                raise WorktreeManagerError(
                    result.stderr.strip() or "failed to inspect workspace"
                )
            return result.stdout

        excluded = (
            ":(exclude).mac/**",
            ":(exclude).data/**",
            ":(exclude)**/*.db-shm",
            ":(exclude)**/*.db-wal",
        )
        diff = _git("diff", "--binary", "HEAD", "--", ".", *excluded)
        status = _git("status", "--porcelain=v1", "--untracked-files=all")
        git_head = _git("rev-parse", "HEAD").strip()
        git_tree = _git("rev-parse", "HEAD^{tree}").strip()
        paths: list[str] = []
        untracked_hashes: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if _is_generated_runtime_path(path):
                continue
            paths.append(path)
            if line.startswith("?? "):
                candidate = root / path
                content_digest = hashlib.sha256()
                if candidate.is_symlink():
                    content_digest.update(str(candidate.readlink()).encode())
                elif candidate.is_file():
                    with candidate.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            content_digest.update(chunk)
                untracked_hashes.append(f"{path}:{content_digest.hexdigest()}")

        digest = hashlib.sha256()
        # A clean repository previously hashed to the same value after every
        # commit because only diff/status were included. The committed tree is
        # part of the delivered artifact identity; HEAD is retained separately
        # for provenance while tree identity avoids metadata-only churn.
        digest.update(git_tree.encode())
        digest.update(diff.encode())
        digest.update("\n".join(sorted(paths)).encode())
        digest.update("\n".join(sorted(untracked_hashes)).encode())
        return {
            "digest": digest.hexdigest(),
            "paths": sorted(paths),
            "git_head": git_head,
            "git_tree": git_tree,
        }

    def validate_worktree_identity(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
        worktree: Path,
        workspace_path: str | Path | None = None,
    ) -> None:
        """Fail closed when a worktree no longer belongs to its task branch.

        Copying a previous attempt with ``cp -R`` can overwrite the new
        worktree's ``.git`` pointer. Git commands then silently operate on the
        old branch while the orchestrator merges the empty expected branch.
        Validate both the canonical working tree and branch before every
        commit/merge boundary.
        """
        workspace = self._workspace(project_id, workspace_path)
        expected_path = Path(worktree).resolve()
        expected_branch = f"mac/{project_id}/{agent_type}/{task_id}"
        try:
            repo = Repo(expected_path)
            actual_path = (
                Path(repo.working_tree_dir).resolve()
                if repo.working_tree_dir
                else None
            )
            actual_branch = (
                repo.active_branch.name
                if not repo.head.is_detached
                else "<detached>"
            )
            common_dir = Path(repo.common_dir).resolve()
            workspace_git = (workspace / ".git").resolve()
        except Exception as exc:
            raise WorktreeManagerError(
                f"Invalid worktree metadata for {expected_path}: {exc}"
            ) from exc
        if actual_path != expected_path:
            raise WorktreeManagerError(
                "Worktree identity mismatch: "
                f"expected path {expected_path}, git resolves to {actual_path}"
            )
        if actual_branch != expected_branch:
            raise WorktreeManagerError(
                "Worktree branch mismatch: "
                f"expected {expected_branch}, got {actual_branch}"
            )
        if common_dir != workspace_git:
            raise WorktreeManagerError(
                "Worktree repository mismatch: "
                f"expected common dir {workspace_git}, got {common_dir}"
            )

    def adopt_worktree(
        self,
        project_id: str,
        previous_task_id: str,
        task_id: str,
        agent_type: str,
        worktree: Path,
        workspace_path: str | Path | None = None,
    ) -> Path:
        """Transfer an interrupted clarification worktree to its continuation task.

        Agent questions may be raised after files have already been changed.  The
        continuation must keep those uncommitted bytes while still satisfying the
        per-task branch identity invariant.  Rename the branch in place instead of
        copying the worktree or merging a partial result into the main workspace.
        """
        workspace = self._workspace(project_id, workspace_path)
        worktree = Path(worktree).resolve()
        self.validate_worktree_identity(
            project_id,
            previous_task_id,
            agent_type,
            worktree,
            workspace_path=workspace,
        )
        next_branch = f"mac/{project_id}/{agent_type}/{task_id}"
        try:
            Repo(worktree).git.branch("-m", next_branch)
        except Exception as exc:
            raise WorktreeManagerError(
                f"Failed to transfer worktree from {previous_task_id} to {task_id}: {exc}"
            ) from exc
        self.validate_worktree_identity(
            project_id,
            task_id,
            agent_type,
            worktree,
            workspace_path=workspace,
        )
        self._link_ignored_dependencies(workspace, worktree)
        return worktree

    def sync_mac_files(
        self,
        project_id: str,
        worktree: Path,
        state_doc: dict[str, Any],
        task_input: dict[str, Any],
        workspace_path: str | Path | None = None,
    ) -> None:
        workspace = self._workspace(project_id, workspace_path)
        mac_files = [
            "STATE.json",
            "ITERATION_BRIEF.json",
            "REQUIREMENTS.json",
            "ARCHITECTURE.json",
            "SPECS.json",
            "BUG_ANALYSIS.json",
            "DOC_OUTLINE.json",
            "CODER_OUTPUT.json",
            "TESTER_OUTPUT.json",
            "REVIEWER_OUTPUT.json",
            "TASK.json",
        ]

        # A role's previous output is not an input to its next attempt.  In
        # particular, copying TESTER_OUTPUT.json into a new Tester worktree let
        # the model relabel evidence from an older Requirements/Specs revision
        # as current without executing the affected behavior again.  Downstream
        # roles still receive the outputs they legitimately review.
        state = str(task_input.get("state") or "")
        stage_outputs = {
            "CODING": {"CODER_OUTPUT.json", "TESTER_OUTPUT.json", "REVIEWER_OUTPUT.json"},
            "TESTING": {"TESTER_OUTPUT.json", "REVIEWER_OUTPUT.json"},
            "REVIEW": {"REVIEWER_OUTPUT.json"},
        }
        excluded = stage_outputs.get(state, set())

        for filename in mac_files:
            if filename in excluded:
                continue
            src = self._project_mac_dir(project_id, workspace) / filename
            dst = worktree / ".mac" / filename
            if src.exists():
                shutil.copy2(src, dst)

        # Overwrite STATE.json with provided state_doc
        with open(worktree / ".mac" / "STATE.json", "w", encoding="utf-8") as f:
            json.dump(state_doc, f, ensure_ascii=False, indent=2)

        # Write TASK.json input
        task_doc = {
            "version": "1.0",
            "task_id": task_input["task_id"],
            "agent_type": task_input["agent_type"],
            "state": task_input["state"],
            "input": task_input.get("input", {}),
            "output": {},
        }
        with open(worktree / ".mac" / "TASK.json", "w", encoding="utf-8") as f:
            json.dump(task_doc, f, ensure_ascii=False, indent=2)

    def commit_worktree_changes(
        self,
        worktree: Path,
        message: str,
    ) -> None:
        """Stage and commit agent changes inside a worktree.

        .mac/ files are orchestrator-managed and should not be committed into
        the agent branch, otherwise merging the branch pollutes the workspace
        .mac directory and causes subsequent merges to conflict.
        """
        worktree = Path(worktree).resolve()
        repo = Repo(worktree)
        if repo.is_dirty(untracked_files=True):
            # Capture an Agent's explicit ``git rm --cached`` intent before
            # ``git add -A`` sees the still-present ignored file and stages it
            # again as a modification.
            deleted_runtime_paths = [
                path
                for path in subprocess.run(
                    [
                        "git",
                        "diff",
                        "--cached",
                        "--diff-filter=D",
                        "--name-only",
                        "-z",
                    ],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.split("\0")
                if path and _is_removable_generated_runtime_path(path)
            ]
            repo.git.add("-A")
            deleted_runtime_paths = sorted(
                set(deleted_runtime_paths)
                | {
                    path
                    for path in subprocess.run(
                        [
                            "git",
                            "diff",
                            "--cached",
                            "--diff-filter=D",
                            "--name-only",
                            "-z",
                        ],
                        cwd=worktree,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.split("\0")
                    if path and _is_removable_generated_runtime_path(path)
                }
            )
            # Unstage orchestrator-managed and generated files.
            repo.git.reset(
                "--",
                ".mac",
                "__pycache__",
                ".coverage",
                ":(glob).data/**",
                ":(glob)**/*.db-shm",
                ":(glob)**/*.db-wal",
            )
            # Preserve intentional removals of legacy tracked runtime files.
            # Their live workspace bytes are handled independently by the
            # merge/stash recovery path and may remain present but ignored.
            if deleted_runtime_paths:
                subprocess.run(
                    [
                        "git",
                        "rm",
                        "--cached",
                        "--ignore-unmatch",
                        "--",
                        *deleted_runtime_paths,
                    ],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            # If nothing remains staged, there is nothing to commit.
            if not repo.index.diff("HEAD", staged=True):
                return
            repo.git.commit("-m", message)

    def merge_worktree(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
        worktree: Path | None = None,
        workspace_path: str | Path | None = None,
    ) -> dict[str, str | bool]:
        workspace = self._workspace(project_id, workspace_path)
        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        if worktree is not None:
            self.validate_worktree_identity(
                project_id,
                task_id,
                agent_type,
                worktree,
                workspace_path=workspace,
            )
            self.commit_worktree_changes(
                worktree, f"[{agent_type}] {task_id} agent changes"
            )

        repo = Repo(workspace)
        existing_unmerged = [
            line.strip()
            for line in repo.git.diff(
                "--name-only", "--diff-filter=U"
            ).splitlines()
            if line.strip()
        ]
        if existing_unmerged:
            if not all(
                _is_generated_runtime_path(path) for path in existing_unmerged
            ):
                raise WorktreeManagerError(
                    "Workspace contains unresolved non-runtime merge conflicts: "
                    + ", ".join(existing_unmerged)
                )
            # A previous task may have merged successfully but failed while
            # restoring concurrently-written runtime files. Keep the working
            # bytes and reset only their conflict entries to current HEAD.
            repo.git.reset("HEAD", "--", *existing_unmerged)
        stash_commit: str | None = None
        merge_error: Exception | None = None
        try:
            # A dirty main workspace (uncommitted changes or untracked files) can
            # cause merge failures when the agent branch adds the same paths.
            # Stash workspace changes before merging; try to restore afterwards.
            if repo.is_dirty(untracked_files=True):
                repo.git.stash("push", "-u", "-m", f"[mac] pre-merge cleanup for {branch_name}")
                stash_commit = repo.git.rev_parse("refs/stash")

            # Merge the branch into the current branch (main/master)
            branch_tip = repo.git.rev_parse(branch_name)
            repo.git.merge(branch_name, no_ff=True, m=f"Merge {branch_name}")
            # A successful exit code is insufficient: assert that the exact
            # task branch tip is now reachable from the workspace HEAD.
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch_tip, "HEAD"],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if ancestor.returncode != 0:
                raise WorktreeManagerError(
                    f"Merge postcondition failed: {branch_tip} is not in HEAD"
                )
        except Exception as e:
            # Abort merge on conflict
            try:
                repo.git.merge("--abort")
            except Exception:
                pass
            merge_error = e

        if stash_commit is not None:
            try:
                # Apply first and only drop the exact saved stash after a
                # successful restore.  On conflict the stash is intentionally
                # preserved so user changes always retain a recovery point.
                repo.git.stash("apply", "--index", stash_commit)
            except Exception as exc:
                unmerged = [
                    line.strip()
                    for line in repo.git.diff(
                        "--name-only", "--diff-filter=U"
                    ).splitlines()
                    if line.strip()
                ]
                redundant_untracked = (
                    not unmerged
                    and "could not restore untracked files" in str(exc)
                    and _stash_is_redundant_untracked_collision(
                        workspace, stash_commit
                    )
                )
                if redundant_untracked:
                    logger.info(
                        "Skipped redundant untracked stash restore after merge; "
                        "workspace already contains identical bytes (stash=%s)",
                        stash_commit,
                    )
                elif unmerged and all(_is_generated_runtime_path(path) for path in unmerged):
                    # The merge changed only generated runtime files that were
                    # concurrently written in the workspace. Preserve the exact
                    # stashed workspace bytes, while resetting the index to the
                    # merged HEAD so no conflict leaks into the next task.
                    for path in unmerged:
                        try:
                            repo.git.checkout(stash_commit, "--", path)
                        except Exception:
                            # The stashed workspace intentionally lacked the
                            # path. Remove only the conflict entry; HEAD remains
                            # authoritative for tracked project content.
                            repo.git.rm("-f", "--ignore-unmatch", "--", path)
                        repo.git.reset("HEAD", "--", path)
                else:
                    operation = "merge failed" if merge_error else "merge succeeded"
                    raise WorktreeManagerError(
                        f"{operation}, but workspace changes could not be restored for "
                        f"{branch_name}; stash preserved at {stash_commit}: {exc}"
                    ) from exc
            else:
                pass
            stash_entries = repo.git.stash("list", "--format=%H %gd").splitlines()
            selector = next(
                (
                    line.split(maxsplit=1)[1]
                    for line in stash_entries
                    if line.split(maxsplit=1)[0] == stash_commit
                ),
                None,
            )
            if selector:
                repo.git.stash("drop", selector)

        if merge_error is not None:
            raise WorktreeManagerError(
                f"Merge failed for {branch_name}: {merge_error}"
            ) from merge_error
        canonical_head = repo.git.rev_parse("HEAD")
        return {
            "merged": True,
            "branch": branch_name,
            "branch_tip": branch_tip,
            "canonical_head": canonical_head,
            "merge_commit": canonical_head,
        }

    def remove_worktree(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
        worktree: Path,
        workspace_path: str | Path | None = None,
    ) -> None:
        workspace = self._workspace(project_id, workspace_path)
        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        # Remove worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

        # Delete branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )

    def remove_all_worktrees(
        self, project_id: str, workspace_path: str | Path | None = None
    ) -> None:
        """Remove every worktree and matching branch for a project.

        Used when a project is cancelled/restarted so that orphan worktrees do
        not accumulate.
        """
        workspace = self._workspace(project_id, workspace_path)
        if not workspace.exists():
            return

        for wt in self.list_worktrees(project_id, workspace_path):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "-f", str(wt)],
                    cwd=workspace,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if wt.exists():
                    shutil.rmtree(wt)
            except Exception:
                pass

        # Clean up any leftover mac/ branches.
        try:
            repo = Repo(workspace)
            prefix = f"mac/{project_id}/"
            for branch in list(repo.branches):
                if branch.name.startswith(prefix):
                    try:
                        repo.git.branch("-D", branch.name)
                    except Exception:
                        pass
        except Exception:
            pass

    def list_worktrees(
        self, project_id: str, workspace_path: str | Path | None = None
    ) -> list[Path]:
        workspace = self._workspace(project_id, workspace_path)
        directories = [self._worktrees_dir(project_id, workspace)]
        legacy = self._legacy_worktrees_dir(project_id, workspace)
        if legacy not in directories:
            directories.append(legacy)
        result: list[Path] = []
        for worktrees_dir in directories:
            if worktrees_dir.exists():
                result.extend(d for d in worktrees_dir.iterdir() if d.is_dir())
        return result

    def _workspace(
        self, project_id: str, workspace_path: str | Path | None = None
    ) -> Path:
        if workspace_path is not None:
            return Path(workspace_path).resolve()
        return self.projects_dir / project_id

    def _ensure_orchestrator_exclude(self, workspace: Path) -> None:
        """Keep MAC-owned metadata out of the project's working-tree status."""
        repo = Repo(workspace)
        exclude_path = Path(repo.git_dir) / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text() if exclude_path.exists() else ""
        entries = {line.strip() for line in existing.splitlines()}
        if ".mac/" in entries:
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}.mac/\n")

    def _is_bound_workspace(self, project_id: str, workspace: Path) -> bool:
        return workspace.resolve() != (self.projects_dir / project_id).resolve()

    def _worktrees_dir(self, project_id: str, workspace: Path) -> Path:
        if self._is_bound_workspace(project_id, workspace):
            return workspace / ".mac" / "worktrees" / project_id
        # Keep Git worktrees under the hidden orchestrator directory. Frameworks
        # such as Next.js recursively type-check a visible ``worktrees/`` folder
        # and can fail the main build on stale task source files.
        return workspace / ".mac" / "worktrees"

    def _legacy_worktrees_dir(self, project_id: str, workspace: Path) -> Path:
        if self._is_bound_workspace(project_id, workspace):
            return workspace / ".mac" / "worktrees" / project_id
        return workspace / "worktrees"

    def _project_mac_dir(self, project_id: str, workspace: Path) -> Path:
        if self._is_bound_workspace(project_id, workspace):
            return workspace / ".mac" / "projects" / project_id
        return workspace / ".mac"
