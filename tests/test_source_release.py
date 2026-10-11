"""Verify the local release whitelist and content manifest."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_source_release.py"
SPEC = importlib.util.spec_from_file_location("prepare_source_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "source"
    for name in release.TOP_LEVEL_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"public content: {name}\n")
    for directory, names in (("docs", release.DOC_FILES), ("scripts", release.SCRIPT_FILES),
                             ("src/mac_system", ("__init__.py", "main.py")),
                             ("tests", ("test_example.py",)),
                             (".github/workflows", ("ci.yml",))):
        for name in names:
            path = root / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"public content: {name}\n")
    (root / "scripts/start_server.sh").chmod(0o755)
    return root


def test_candidate_excludes_host_data_and_hashes_every_file(source_tree, tmp_path):
    private_files = (".env", ".env.bak20261010", ".mac-secrets.json", "mac_system.db",
                     "mac_system.db-wal", "server.log", "AGENTS.md", ".git/config",
                     ".mac/events.json", "projects/private.py", "docs/private-history.md",
                     "scripts/switch_mac_back_to_codex_20261003.py",
                     "src/mac_system/__pycache__/main.pyc")
    for name in private_files:
        path = source_tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private marker not for publication")
    output = tmp_path / "candidate.tar.gz"
    count = release.prepare_release(source_tree, output)
    with tarfile.open(output) as archive:
        names = archive.getnames()
        manifest = json.load(archive.extractfile("mac-system-source/PUBLIC_MANIFEST.json"))
        assert len(manifest["files"]) == count == len(names) - 1
        assert manifest["license_status"] == "not_selected"
        for item in manifest["files"]:
            content = archive.extractfile(f'mac-system-source/{item["path"]}').read()
            assert hashlib.sha256(content).hexdigest() == item["sha256"]
            assert b"private marker" not in content
        assert archive.getmember("mac-system-source/scripts/start_server.sh").mode == 0o755
        for name in private_files:
            assert f"mac-system-source/{name}" not in names


def test_candidate_is_reproducible_and_does_not_overwrite(source_tree, tmp_path):
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    release.prepare_release(source_tree, first)
    release.prepare_release(source_tree, second)
    original = first.read_bytes()
    assert second.read_bytes() == original
    with pytest.raises(FileExistsError):
        release.prepare_release(source_tree, first)
    assert first.read_bytes() == original


@pytest.mark.parametrize("target", ["README.md", "src/mac_system/main.py", "docs"])
def test_candidate_refuses_symlinks(source_tree, tmp_path, target):
    path = source_tree / target
    outside = tmp_path / "private.txt"
    outside.write_text("private content")
    if path.is_dir():
        path.rename(source_tree / "original_docs")
        path.symlink_to(source_tree / "original_docs", target_is_directory=True)
    else:
        path.unlink()
        path.symlink_to(outside)
    output = tmp_path / "candidate.tar.gz"
    with pytest.raises(ValueError, match="Symlinks"):
        release.prepare_release(source_tree, output)
    assert not output.exists()


def test_candidate_fails_before_output_if_required_file_missing(source_tree, tmp_path):
    (source_tree / "README.md").unlink()
    output = tmp_path / "candidate.tar.gz"
    with pytest.raises(ValueError, match="Required source file"):
        release.prepare_release(source_tree, output)
    assert not output.exists()
