"""Create a local source candidate without host data or Git history."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


TOP_LEVEL_FILES = (
    ".env.example", ".gitignore", "README.md", "CURRENT_STATE.md",
    "SECURITY.md", "CONTRIBUTING.md", "LICENSE", "pyproject.toml", "logging.yaml",
    "agent_config.json",
)
DOC_FILES = (
    "architecture.md", "feishu_onboarding.md", "mac-design-principles.md",
    "testing-taxonomy.md", "interaction-spec.md", "release-readiness.md",
    "source-candidate-kimi-review-20261011.md", "long-conversation-maintenance.md",
    "architecture-review-20261012.md",
)
SCRIPT_FILES = (
    "start_server.sh", "stop_server.sh", "run_server.sh",
    "restart_server_once.sh", "com.mac-system.dev.plist.template",
    "restart_clear_pool_watchdog.sh", "deploy_codex_startup_fix_watchdog.sh",
    "feishu_e2e.py", "evaluate_project_owner.py", "import_owner_preferences.py",
    "import_cc_connect_feishu_accounts.py", "prepare_source_release.py",
    "deploy_long_conversation_once.py",
)


def source_files(root: Path) -> list[Path]:
    paths = [root / name for name in TOP_LEVEL_FILES]
    paths.extend(root / "docs" / name for name in DOC_FILES)
    paths.extend(root / "scripts" / name for name in SCRIPT_FILES)
    for directory, pattern in (("src", "*.py"), ("tests", "*.py"),
                               (".github/workflows", "*.yml")):
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f"Required source directory unavailable: {directory}")
        paths.extend(base.rglob(pattern))
    if (root / "uv.lock").exists():
        paths.append(root / "uv.lock")
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"Symlinks are not release source: {path.relative_to(root)}")
        parent = path.parent
        while parent != root:
            if parent.is_symlink():
                raise ValueError(f"Symlinks are not release source: {path.relative_to(root)}")
            parent = parent.parent
        if not path.is_file():
            raise ValueError(f"Required source file unavailable: {path.relative_to(root)}")
    return sorted(set(paths))


def prepare_release(root: Path, output: Path) -> int:
    root = root.resolve()
    snapshot = []
    for path in source_files(root):
        snapshot.append((path.relative_to(root).as_posix(), path.read_bytes(),
                         0o755 if path.stat().st_mode & 0o111 else 0o644))
    manifest = {
        "schema_version": 1,
        "license_status": "selected",
        "license": "MIT",
        "publication_status": "local_candidate_only",
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content, mode in snapshot],
    }
    snapshot.append(("PUBLIC_MANIFEST.json",
                     json.dumps(manifest, indent=2, ensure_ascii=False).encode() + b"\n",
                     0o644))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, content, mode in snapshot:
                    entry = tarfile.TarInfo(f"mac-system-source/{name}")
                    entry.size = len(content)
                    entry.mode = mode
                    archive.addfile(entry, io.BytesIO(content))
    return len(snapshot) - 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/mac-system-source.tar.gz"))
    arguments = parser.parse_args()
    try:
        count = prepare_release(Path(__file__).resolve().parents[1], arguments.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Source candidate not created: {error}\n")
    print(f"Local source candidate: {arguments.output} ({count} files; not published)")


if __name__ == "__main__":
    main()
