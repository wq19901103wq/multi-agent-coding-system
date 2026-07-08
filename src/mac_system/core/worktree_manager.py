import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from git import Repo


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
    ) -> Path:
        workspace = self._workspace(project_id)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name_parts = [agent_type]
        if stage:
            name_parts.append(stage)
        name_parts.extend([task_id, timestamp])
        worktree_name = "_".join(name_parts)
        worktree_path = (workspace / "worktrees" / worktree_name).resolve()

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
    ) -> None:
        workspace = self._workspace(project_id)
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
            src = workspace / ".mac" / filename
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

    def merge_worktree(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
    ) -> None:
        workspace = self._workspace(project_id)
        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        repo = Repo(workspace)
        try:
            # Merge the branch into the current branch (main/master)
            repo.git.merge(branch_name, no_ff=True, m=f"Merge {branch_name}")
        except Exception as e:
            # Abort merge on conflict
            try:
                repo.git.merge("--abort")
            except Exception:
                pass
            raise WorktreeManagerError(f"Merge failed for {branch_name}: {e}") from e

    def remove_worktree(
        self,
        project_id: str,
        task_id: str,
        agent_type: str,
        worktree: Path,
    ) -> None:
        workspace = self._workspace(project_id)
        branch_name = f"mac/{project_id}/{agent_type}/{task_id}"

        # Remove worktree
        subprocess.run(
            ["git", "worktree", "remove", str(worktree)],
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

    def list_worktrees(self, project_id: str) -> list[Path]:
        workspace = self._workspace(project_id)
        worktrees_dir = workspace / "worktrees"
        if not worktrees_dir.exists():
            return []
        return [d for d in worktrees_dir.iterdir() if d.is_dir()]

    def _workspace(self, project_id: str) -> Path:
        return self.projects_dir / project_id
