from pathlib import Path

import pytest

from mac_system import config
from mac_system.config import Settings


@pytest.mark.parametrize("available", ["current", "legacy", "both"])
def test_default_codex_bin_prefers_chatgpt_bundled_binary(
    monkeypatch, tmp_path: Path, available: str
) -> None:
    resources = tmp_path / "ChatGPT.app" / "Contents" / "Resources"
    current = resources / "codex-cli" / "bin" / "codex"
    legacy = resources / "codex"
    for name, path in [("current", current), ("legacy", legacy)]:
        if available in {name, "both"}:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
    monkeypatch.setattr(config, "_CHATGPT_BUNDLED_CODEX_PATHS", (current, legacy))
    monkeypatch.delenv("CODEX_BIN", raising=False)

    settings = Settings(_env_file=None)

    assert settings.codex_bin == (legacy if available == "legacy" else current)


def test_codex_bin_environment_override_wins_over_bundled_default(
    monkeypatch, tmp_path: Path
) -> None:
    bundled = tmp_path / "bundled-codex"
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(0o755)
    configured = tmp_path / "configured-codex"
    monkeypatch.setattr(config, "_CHATGPT_BUNDLED_CODEX_PATHS", (bundled,))
    monkeypatch.setenv("CODEX_BIN", str(configured))

    settings = Settings(_env_file=None)

    assert settings.codex_bin == configured


def test_default_codex_bin_falls_back_to_path_lookup(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "_CHATGPT_BUNDLED_CODEX_PATHS", (tmp_path / "missing",))
    monkeypatch.delenv("CODEX_BIN", raising=False)

    settings = Settings(_env_file=None)

    assert settings.codex_bin == Path("codex")


@pytest.mark.parametrize("invalid_kind", ["directory", "non_executable"])
def test_default_codex_bin_skips_unusable_bundle(monkeypatch, tmp_path, invalid_kind):
    invalid = tmp_path / "invalid-codex"
    if invalid_kind == "directory":
        invalid.mkdir()
    else:
        invalid.write_text("#!/bin/sh\n", encoding="utf-8")
        invalid.chmod(0o644)
    usable = tmp_path / "usable-codex"
    usable.write_text("#!/bin/sh\n", encoding="utf-8")
    usable.chmod(0o755)
    monkeypatch.setattr(config, "_CHATGPT_BUNDLED_CODEX_PATHS", (invalid, usable))
    monkeypatch.delenv("CODEX_BIN", raising=False)

    assert Settings(_env_file=None).codex_bin == usable
