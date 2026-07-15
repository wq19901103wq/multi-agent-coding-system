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

        return worktree_path

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
            "REQUIREMENTS.json",
            "ARCHITECTURE.json",
            "SPECS.json",
            "BUG_ANALYSIS.json",
            "DOC_OUTLINE.json",
            "TASK.json",
        ]

        for filename in mac_files:
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
            repo.git.add("-A")
            # Unstage orchestrator-managed and generated files.
            repo.git.reset("--", ".mac", "__pycache__", ".coverage")
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
    ) -> None:
        workspace = self._workspace(project_id, workspace_path)
        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        if worktree is not None:
            self.commit_worktree_changes(
                worktree, f"[{agent_type}] {task_id} agent changes"
            )

        repo = Repo(workspace)
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
            repo.git.merge(branch_name, no_ff=True, m=f"Merge {branch_name}")
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
                operation = "merge failed" if merge_error else "merge succeeded"
                raise WorktreeManagerError(
                    f"{operation}, but workspace changes could not be restored for "
                    f"{branch_name}; stash preserved at {stash_commit}: {exc}"
                ) from exc
            else:
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
        worktrees_dir = self._worktrees_dir(project_id, workspace)
        if not worktrees_dir.exists():
            return []
        return [d for d in worktrees_dir.iterdir() if d.is_dir()]

    def _workspace(
        self, project_id: str, workspace_path: str | Path | None = None
    ) -> Path:
        if workspace_path is not None:
            return Path(workspace_path).resolve()
        return self.projects_dir / project_id

    def _is_bound_workspace(self, project_id: str, workspace: Path) -> bool:
        return workspace.resolve() != (self.projects_dir / project_id).resolve()

    def _worktrees_dir(self, project_id: str, workspace: Path) -> Path:
        if self._is_bound_workspace(project_id, workspace):
            return workspace / ".mac" / "worktrees" / project_id
        return workspace / "worktrees"

    def _project_mac_dir(self, project_id: str, workspace: Path) -> Path:
        if self._is_bound_workspace(project_id, workspace):
            return workspace / ".mac" / "projects" / project_id
        return workspace / ".mac"
