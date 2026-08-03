import json
import subprocess

import pytest

from mac_system.core.worktree_manager import WorktreeManager, WorktreeManagerError


@pytest.fixture
def tmp_project(tmp_path):
    project_path = tmp_path / "proj_test"
    project_path.mkdir()
    (project_path / ".mac").mkdir()
    (project_path / "src").mkdir()
    (project_path / "tests").mkdir()
    (project_path / "worktrees").mkdir()

    # Write initial STATE.json
    with open(project_path / ".mac" / "STATE.json", "w", encoding="utf-8") as f:
        json.dump({"version": "1.0", "current_state": "INIT"}, f)

    # Write a requirement
    with open(project_path / ".mac" / "REQUIREMENTS.json", "w", encoding="utf-8") as f:
        json.dump({"summary": "test"}, f)

    # Git init
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=project_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, check=True)

    # Initial commit
    (project_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=project_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project_path, check=True)

    return project_path


@pytest.fixture
def manager(tmp_project):
    return WorktreeManager(tmp_project.parent)


def test_create_worktree(manager, tmp_project):
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_001",
        agent_type="coder",
    )
    assert worktree.exists()
    assert worktree.name.startswith("coder_task_001_")
    assert worktree.parent == tmp_project / ".mac" / "worktrees"
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".mac/" not in status
    assert (worktree / ".git").exists()


def test_create_worktree_links_ignored_node_dependencies(manager, tmp_project):
    (tmp_project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "ignore dependencies"],
        cwd=tmp_project,
        check=True,
    )
    (tmp_project / "node_modules").mkdir()
    (tmp_project / "node_modules" / "marker.js").write_text("installed\n")

    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_node",
        agent_type="coder",
    )

    assert (worktree / "node_modules").is_symlink()
    assert (worktree / "node_modules" / "marker.js").read_text() == "installed\n"
    assert subprocess.run(
        ["git", "status", "--short"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_workspace_fingerprint_detects_source_escape_and_ignores_runtime(
    manager, tmp_project
):
    before = manager.workspace_fingerprint(tmp_project)
    (tmp_project / ".mac" / "runtime.json").write_text("changed\n")
    assert manager.workspace_fingerprint(tmp_project)["digest"] == before["digest"]

    (tmp_project / "README.md").write_text("escaped coder edit\n")
    after = manager.workspace_fingerprint(tmp_project)
    assert after["digest"] != before["digest"]
    assert "README.md" in after["paths"]


def test_workspace_fingerprint_changes_between_clean_delivery_commits(
    manager, tmp_project
):
    before = manager.workspace_fingerprint(tmp_project)
    (tmp_project / "delivery.txt").write_text("revision two\n", encoding="utf-8")
    subprocess.run(["git", "add", "delivery.txt"], cwd=tmp_project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "delivery revision two"],
        cwd=tmp_project,
        check=True,
        capture_output=True,
    )
    after = manager.workspace_fingerprint(tmp_project)

    assert after["paths"] == []
    assert after["digest"] != before["digest"]
    assert after["git_head"] != before["git_head"]
    assert after["git_tree"] != before["git_tree"]


def test_remove_all_worktrees_cleans_hidden_and_legacy_locations(
    manager, tmp_project
):
    hidden = manager.create_worktree(
        project_id="proj_test",
        task_id="task_hidden",
        agent_type="coder",
    )
    legacy = tmp_project / "worktrees" / "legacy_orphan"
    legacy.mkdir(parents=True)

    manager.remove_all_worktrees("proj_test", workspace_path=tmp_project)

    assert not hidden.exists()
    assert not legacy.exists()


def test_adopt_worktree_preserves_uncommitted_clarification_changes(
    manager, tmp_project
):
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_question",
        agent_type="coder",
    )
    changed = worktree / "src" / "continued.py"
    changed.parent.mkdir(exist_ok=True)
    changed.write_text("answer = 42\n")

    adopted = manager.adopt_worktree(
        "proj_test",
        "task_question",
        "task_continuation",
        "coder",
        worktree,
        workspace_path=tmp_project,
    )

    assert adopted == worktree.resolve()
    assert changed.read_text() == "answer = 42\n"
    manager.validate_worktree_identity(
        "proj_test",
        "task_continuation",
        "coder",
        worktree,
        workspace_path=tmp_project,
    )
    manager.merge_worktree(
        "proj_test",
        "task_continuation",
        "coder",
        worktree,
        workspace_path=tmp_project,
    )
    assert (tmp_project / "src" / "continued.py").read_text() == "answer = 42\n"


def test_sync_mac_files(manager, tmp_project):
    (tmp_project / ".mac" / "TESTER_OUTPUT.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_001",
        agent_type="coder",
    )
    manager.sync_mac_files(
        project_id="proj_test",
        worktree=worktree,
        state_doc={"version": "1.0", "current_state": "CODING"},
        task_input={
            "task_id": "task_001",
            "agent_type": "coder",
            "state": "CODING",
            "input": {"description": "test"},
        },
    )

    assert (worktree / ".mac" / "STATE.json").exists()
    assert (worktree / ".mac" / "REQUIREMENTS.json").exists()
    assert not (worktree / ".mac" / "TESTER_OUTPUT.json").exists()
    assert (worktree / ".mac" / "TASK.json").exists()

    state = json.loads((worktree / ".mac" / "STATE.json").read_text())
    assert state["current_state"] == "CODING"

    task = json.loads((worktree / ".mac" / "TASK.json").read_text())
    assert task["agent_type"] == "coder"
    assert task["input"]["description"] == "test"


def test_sync_mac_files_only_exposes_outputs_from_upstream_stages(
    manager, tmp_project
):
    for filename in (
        "CODER_OUTPUT.json",
        "TESTER_OUTPUT.json",
        "REVIEWER_OUTPUT.json",
    ):
        (tmp_project / ".mac" / filename).write_text(
            json.dumps({"source": filename}), encoding="utf-8"
        )

    tester_tree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_tester",
        agent_type="tester",
    )
    manager.sync_mac_files(
        project_id="proj_test",
        worktree=tester_tree,
        state_doc={"current_state": "TESTING"},
        task_input={
            "task_id": "task_tester",
            "agent_type": "tester",
            "state": "TESTING",
        },
    )
    assert (tester_tree / ".mac" / "CODER_OUTPUT.json").exists()
    assert not (tester_tree / ".mac" / "TESTER_OUTPUT.json").exists()
    assert not (tester_tree / ".mac" / "REVIEWER_OUTPUT.json").exists()

    reviewer_tree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_reviewer",
        agent_type="reviewer",
    )
    manager.sync_mac_files(
        project_id="proj_test",
        worktree=reviewer_tree,
        state_doc={"current_state": "REVIEW"},
        task_input={
            "task_id": "task_reviewer",
            "agent_type": "reviewer",
            "state": "REVIEW",
        },
    )
    assert (reviewer_tree / ".mac" / "CODER_OUTPUT.json").exists()
    assert (reviewer_tree / ".mac" / "TESTER_OUTPUT.json").exists()
    assert not (reviewer_tree / ".mac" / "REVIEWER_OUTPUT.json").exists()


def test_merge_worktree(manager, tmp_project):
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_001",
        agent_type="coder",
    )
    # Make a change in worktree
    (worktree / "src").mkdir(exist_ok=True)
    (worktree / "src" / "hello.py").write_text("print('hello')")

    import subprocess

    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add hello"], cwd=worktree, check=True)

    receipt = manager.merge_worktree("proj_test", "task_001", "coder")

    assert (tmp_project / "src" / "hello.py").exists()
    assert receipt["merged"] is True
    assert receipt["branch_tip"]
    assert receipt["merge_commit"] == receipt["canonical_head"]
    assert receipt["canonical_head"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_merge_rejects_worktree_whose_git_pointer_was_overwritten(
    manager, tmp_project
):
    """A retry must never commit through a previous attempt's .git pointer."""
    first = manager.create_worktree(
        project_id="proj_test",
        task_id="task_first",
        agent_type="coder",
    )
    second = manager.create_worktree(
        project_id="proj_test",
        task_id="task_second",
        agent_type="coder",
    )
    first_git_pointer = (first / ".git").read_text(encoding="utf-8")
    (second / ".git").write_text(first_git_pointer, encoding="utf-8")
    (second / "src").mkdir(exist_ok=True)
    (second / "src" / "wrong_branch.py").write_text("bad = True\n")

    with pytest.raises(WorktreeManagerError, match="branch mismatch"):
        manager.merge_worktree(
            "proj_test",
            "task_second",
            "coder",
            second,
            workspace_path=tmp_project,
        )

    assert not (tmp_project / "src" / "wrong_branch.py").exists()


def test_merge_conflict_preserves_user_stash(manager, tmp_project):
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_conflict",
        agent_type="coder",
    )
    (worktree / "README.md").write_text("agent version\n")

    import subprocess

    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent edit"], cwd=worktree, check=True
    )
    (tmp_project / "README.md").write_text("user version\n")

    with pytest.raises(WorktreeManagerError, match="stash preserved"):
        manager.merge_worktree(
            "proj_test", "task_conflict", "coder", workspace_path=tmp_project
        )

    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=tmp_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "pre-merge cleanup" in stash_list


def test_merge_accepts_identical_untracked_file_added_by_agent(
    manager, tmp_project
):
    """An identical untracked collision is already restored, not data loss."""
    import subprocess

    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_identical_untracked",
        agent_type="tester",
    )
    relative = "tests/test_generated_integration.py"
    (worktree / relative).parent.mkdir(parents=True, exist_ok=True)
    (worktree / relative).write_text("def test_generated():\n    assert True\n")
    subprocess.run(["git", "add", str(relative)], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "tester adds integration evidence"],
        cwd=worktree,
        check=True,
    )

    (tmp_project / relative).parent.mkdir(parents=True, exist_ok=True)
    (tmp_project / relative).write_text("def test_generated():\n    assert True\n")

    manager.merge_worktree(
        "proj_test",
        "task_identical_untracked",
        "tester",
        worktree,
        workspace_path=tmp_project,
    )

    assert (tmp_project / relative).read_text() == "def test_generated():\n    assert True\n"
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(relative)],
        cwd=tmp_project,
        capture_output=True,
        check=False,
    ).returncode == 0


def test_commit_worktree_changes_excludes_runtime_database_files(
    manager, tmp_project
):
    import subprocess

    (tmp_project / ".data").mkdir()
    (tmp_project / ".data" / "agent.db-wal").write_bytes(b"base\0")
    subprocess.run(["git", "add", ".data/agent.db-wal"], cwd=tmp_project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "track legacy runtime file"],
        cwd=tmp_project,
        check=True,
    )
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_runtime_exclusion",
        agent_type="coder",
    )
    (worktree / ".data" / "agent.db-wal").write_bytes(b"runtime\0")
    (worktree / "src").mkdir(exist_ok=True)
    (worktree / "src" / "feature.py").write_text("done = True\n")

    manager.commit_worktree_changes(worktree, "agent changes")

    changed = subprocess.run(
        ["git", "show", "--name-only", "--format="],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/feature.py" in changed
    assert ".data/agent.db-wal" not in changed
    assert (worktree / ".data" / "agent.db-wal").read_bytes() == b"runtime\0"


def test_commit_worktree_changes_allows_untracking_legacy_runtime_files(
    manager, tmp_project
):
    import subprocess

    (tmp_project / ".data").mkdir()
    runtime_file = tmp_project / ".data" / "agent.db-wal"
    runtime_file.write_bytes(b"legacy\0")
    subprocess.run(["git", "add", ".data/agent.db-wal"], cwd=tmp_project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "track legacy runtime file"],
        cwd=tmp_project,
        check=True,
    )
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_runtime_untrack",
        agent_type="coder",
    )
    (worktree / ".data" / "agent.db-wal").unlink()

    manager.commit_worktree_changes(worktree, "untrack runtime artifact")

    changed = subprocess.run(
        ["git", "show", "--name-status", "--format="],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "D\t.data/agent.db-wal" in changed


def test_commit_worktree_changes_preserves_rm_cached_with_live_runtime_bytes(
    manager, tmp_project
):
    import subprocess

    (tmp_project / ".data").mkdir()
    runtime_file = tmp_project / ".data" / "agent.db-wal"
    runtime_file.write_bytes(b"live-runtime\0")
    (tmp_project / ".gitignore").write_text(".data/\n")
    subprocess.run(
        ["git", "add", "-f", ".data/agent.db-wal", ".gitignore"],
        cwd=tmp_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "track legacy runtime file"],
        cwd=tmp_project,
        check=True,
    )
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_runtime_rm_cached",
        agent_type="coder",
    )
    subprocess.run(
        ["git", "rm", "--cached", ".data/agent.db-wal"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )

    manager.commit_worktree_changes(worktree, "untrack live runtime artifact")

    changed = subprocess.run(
        ["git", "show", "--name-status", "--format="],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "D\t.data/agent.db-wal" in changed
    assert (worktree / ".data" / "agent.db-wal").read_bytes() == b"live-runtime\0"


def test_merge_restores_workspace_runtime_bytes_after_binary_stash_conflict(
    manager, tmp_project
):
    import subprocess

    (tmp_project / ".data").mkdir()
    runtime_file = tmp_project / ".data" / "agent.db-wal"
    runtime_file.write_bytes(b"base\0")
    subprocess.run(["git", "add", ".data/agent.db-wal"], cwd=tmp_project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "track legacy runtime file"],
        cwd=tmp_project,
        check=True,
    )
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_runtime_conflict",
        agent_type="coder",
    )
    (worktree / ".data" / "agent.db-wal").write_bytes(b"agent\0")
    subprocess.run(["git", "add", ".data/agent.db-wal"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent runtime write"],
        cwd=worktree,
        check=True,
    )
    runtime_file.write_bytes(b"workspace\0")

    manager.merge_worktree(
        "proj_test",
        "task_runtime_conflict",
        "coder",
        workspace_path=tmp_project,
    )

    assert runtime_file.read_bytes() == b"workspace\0"
    unmerged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=tmp_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert unmerged == ""


def test_remove_worktree(manager, tmp_project):
    worktree = manager.create_worktree(
        project_id="proj_test",
        task_id="task_001",
        agent_type="coder",
    )
    manager.remove_worktree("proj_test", "task_001", "coder", worktree)
    assert not worktree.exists()


def test_remove_all_worktrees(manager, tmp_project):
    wt1 = manager.create_worktree("proj_test", "task_001", "coder")
    wt2 = manager.create_worktree("proj_test", "task_002", "reviewer")
    manager.remove_all_worktrees("proj_test")
    assert not wt1.exists()
    assert not wt2.exists()
    assert manager.list_worktrees("proj_test") == []


def test_bound_repository_uses_namespaced_metadata_and_worktrees(tmp_path):
    import subprocess

    repo = tmp_path / "existing"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Existing\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    project_id = "proj_bound"
    mac_dir = repo / ".mac" / "projects" / project_id
    mac_dir.mkdir(parents=True)
    (repo / ".mac" / "worktrees" / project_id).mkdir(parents=True)
    (repo / ".git" / "info" / "exclude").write_text(".mac/worktrees/\n")
    (mac_dir / "STATE.json").write_text(json.dumps({"current_state": "CODING"}))

    manager = WorktreeManager(tmp_path / "system-projects")
    worktree = manager.create_worktree(
        project_id, "task_bound", "coder", workspace_path=repo
    )
    assert worktree.parent == repo / ".mac" / "worktrees" / project_id
    manager.sync_mac_files(
        project_id,
        worktree,
        {"current_state": "CODING"},
        {"task_id": "task_bound", "agent_type": "coder", "state": "CODING"},
        workspace_path=repo,
    )
    assert json.loads((worktree / ".mac" / "STATE.json").read_text())["current_state"] == "CODING"

    (worktree / "feature.txt").write_text("implemented\n")
    manager.merge_worktree(
        project_id, "task_bound", "coder", worktree, workspace_path=repo
    )
    assert (repo / "feature.txt").read_text() == "implemented\n"
    manager.remove_worktree(
        project_id, "task_bound", "coder", worktree, workspace_path=repo
    )
