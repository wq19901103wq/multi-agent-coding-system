from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mac_system.config import Settings
from mac_system.core.project_files import (
    build_project_file_url,
    resolve_project_file,
    rewrite_card_project_file_links,
    rewrite_project_file_links,
    verify_project_file_signature,
)
from mac_system.models import Project


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        MAC_PUBLIC_BASE_URL="https://mac.example.test",
        MAC_FILE_LINK_SECRET="test-file-secret",
    )


def _project(tmp_path: Path) -> Project:
    return Project(
        id="proj_test",
        name="test",
        workspace_path=str(tmp_path),
    )


def test_rewrite_project_file_links_supports_absolute_relative_and_line_links(tmp_path):
    project = _project(tmp_path)
    report = tmp_path / "artifacts" / "验收 报告.json"
    report.parent.mkdir()
    report.write_text('{"ok": true}', encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("done", encoding="utf-8")

    text = (
        f"[验收报告](<{report}:12>)\n"
        "[说明](README.md)\n"
        "[官网](https://example.com/path)\n"
        "[外部文件](/tmp/outside.txt)"
    )
    rewritten = rewrite_project_file_links(text, project, _settings())

    assert "https://mac.example.test/files/proj_test/artifacts/" in rewritten
    assert "%E9%AA%8C%E6%94%B6%20%E6%8A%A5%E5%91%8A.json?sig=" in rewritten
    assert "https://mac.example.test/files/proj_test/README.md?sig=" in rewritten
    assert "[官网](https://example.com/path)" in rewritten
    assert "外部文件（尚未发布为手机可访问链接）" in rewritten
    assert ":12" not in rewritten


def test_file_url_is_signed_and_cannot_escape_or_publish_secrets(tmp_path):
    project = _project(tmp_path)
    settings = _settings()
    artifact = tmp_path / "result.pdf"
    artifact.write_bytes(b"pdf")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    private_key = tmp_path / "client.pem"
    private_key.write_text("private", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    url = build_project_file_url(project, artifact, settings)
    assert url is not None
    parsed = urlparse(url)
    signature = parse_qs(parsed.query)["sig"][0]
    assert verify_project_file_signature(
        project.id, "result.pdf", signature, settings
    )
    assert not verify_project_file_signature(
        project.id, "other.pdf", signature, settings
    )
    assert resolve_project_file(project, "../outside.txt") is None
    assert build_project_file_url(project, secret, settings) is None
    assert build_project_file_url(project, private_key, settings) is None


def test_rewrite_card_returns_copy_and_rewrites_nested_markdown(tmp_path):
    project = _project(tmp_path)
    artifact = tmp_path / "result.txt"
    artifact.write_text("ok", encoding="utf-8")
    card = {
        "elements": [
            {"tag": "markdown", "content": "[结果](result.txt)"},
        ]
    }

    rewritten = rewrite_card_project_file_links(card, project, _settings())

    assert card["elements"][0]["content"] == "[结果](result.txt)"
    assert "https://mac.example.test/files/proj_test/result.txt?sig=" in (
        rewritten["elements"][0]["content"]
    )


def test_mobile_report_never_publishes_host_only_links(tmp_path):
    project = _project(tmp_path)
    outside = tmp_path.parent / "outside.html"
    outside.write_text("<h1>outside</h1>", encoding="utf-8")

    rewritten = rewrite_project_file_links(
        "\n".join(
            [
                f"[临时文件](<{outside}>)",
                "[本机页面](http://127.0.0.1:8876/result.html)",
            ]
        ),
        project,
        _settings(),
    )

    assert "127.0.0.1" not in rewritten
    assert str(outside) not in rewritten
    assert rewritten.count("尚未发布为手机可访问链接") == 2


def test_visualize_artifact_is_rewritten_only_inside_project(tmp_path):
    project = _project(tmp_path)
    artifact = tmp_path / "flow.html"
    artifact.write_text("<h1>流程</h1>", encoding="utf-8")
    inside = (
        "visualize"
        + '{"path":"' + str(artifact) + '","title":"流程图"}'
        + ""
    )
    outside = (
        "visualize"
        + '{"path":"/private/tmp/flow.html","title":"临时流程图"}'
        + ""
    )

    rewritten_inside = rewrite_project_file_links(inside, project, _settings())
    rewritten_outside = rewrite_project_file_links(outside, project, _settings())

    assert "https://mac.example.test/files/proj_test/flow.html?sig=" in rewritten_inside
    assert "尚未发布为手机可访问链接" in rewritten_outside


def test_existing_project_file_in_inline_code_becomes_mobile_link(tmp_path):
    project = _project(tmp_path)
    artifact = tmp_path / "docs" / "report.html"
    artifact.parent.mkdir()
    artifact.write_text("<h1>ok</h1>", encoding="utf-8")

    rewritten = rewrite_project_file_links(
        "结果见 `docs/report.html`；命令仍是 `python run.py`。",
        project,
        _settings(),
    )

    assert "[docs/report.html](https://mac.example.test/files/" in rewritten
    assert "`python run.py`" in rewritten
