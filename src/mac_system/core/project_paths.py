from pathlib import Path

from mac_system.models import Project


def project_mac_dir(project: Project) -> Path:
    """Return the project-specific metadata directory.

    System-managed workspaces retain the legacy ``.mac`` layout. Projects
    attached to an existing repository use a namespaced directory so several
    MAC projects can safely share one codebase.
    """
    workspace = Path(project.workspace_path)
    if getattr(project, "bound_directory", None):
        return workspace / ".mac" / "projects" / project.id
    return workspace / ".mac"


def project_worktrees_dir(project: Project) -> Path:
    workspace = Path(project.workspace_path)
    if getattr(project, "bound_directory", None):
        return workspace / ".mac" / "worktrees" / project.id
    return workspace / "worktrees"
