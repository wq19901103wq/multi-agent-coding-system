import json
from pathlib import Path

import pytest

from mac_system.core.worktree_manager import WorktreeManager


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
    assert (worktree / ".git").exists()


def test_sync_mac_files(manager, tmp_project):
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
    assert (worktree / ".mac" / "TASK.json").exists()

    state = json.loads((worktree / ".mac" / "STATE.json").read_text())
    assert state["current_state"] == "CODING"

    task = json.loads((worktree / ".mac" / "TASK.json").read_text())
    assert task["agent_type"] == "coder"
    assert task["input"]["description"] == "test"


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

    manager.merge_worktree("proj_test", "task_001", "coder")

    assert (tmp_project / "src" / "hello.py").exists()


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
