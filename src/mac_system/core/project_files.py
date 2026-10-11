"""Safe public links for files that a project report explicitly references."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from mac_system.config import Settings
from mac_system.models import Project


_MARKDOWN_LINK_RE = re.compile(
    r"(?P<prefix>!?\[[^\]\n]*\]\()"
    r"(?P<destination><[^>\n]+>|[^)\s]+)"
    r"(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?"
    r"(?P<suffix>\))"
)
_LINE_SUFFIX_RE = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?$")
_VISUALIZE_ARTIFACT_RE = re.compile(r"visualize(?P<payload>\{.*?\})")
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?P<value>[^`\n]+)`(?!`)")
_BLOCKED_PARTS = {".git", ".mac"}
_BLOCKED_NAMES = {
    ".mac-secrets.json",
    "id_rsa",
    "id_ed25519",
}
_BLOCKED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


def _link_secret(settings: Settings) -> str | None:
    return (
        settings.mac_file_link_secret
        or settings.control_api_token
        or settings.admin_token
    )


def _public_base_url(settings: Settings) -> str | None:
    value = (settings.mac_public_base_url or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _is_public_file(relative_path: Path) -> bool:
    parts = set(relative_path.parts)
    name = relative_path.name.lower()
    if parts & _BLOCKED_PARTS:
        return False
    if name == ".env" or name.startswith(".env."):
        return False
    if name in _BLOCKED_NAMES or relative_path.suffix.lower() in _BLOCKED_SUFFIXES:
        return False
    return True


def resolve_project_file(project: Project, requested_path: str | Path) -> tuple[Path, str] | None:
    """Resolve a project-relative path without permitting workspace escape."""

    workspace = Path(project.workspace_path).expanduser().resolve()
    raw = str(requested_path).strip()
    if raw.startswith("file://"):
        raw = raw[7:]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(workspace)
    except ValueError:
        return None
    if not relative.parts or not _is_public_file(relative) or not candidate.is_file():
        return None
    return candidate, relative.as_posix()


def project_file_signature(project_id: str, relative_path: str, secret: str) -> str:
    payload = f"{project_id}\n{relative_path}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_project_file_signature(
    project_id: str,
    relative_path: str,
    signature: str,
    settings: Settings,
) -> bool:
    secret = _link_secret(settings)
    if not secret or not signature:
        return False
    expected = project_file_signature(project_id, relative_path, secret)
    return hmac.compare_digest(signature, expected)


def build_project_file_url(
    project: Project,
    requested_path: str | Path,
    settings: Settings,
) -> str | None:
    """Build a signed mobile-accessible URL for one existing project file."""

    base_url = _public_base_url(settings)
    secret = _link_secret(settings)
    resolved = resolve_project_file(project, requested_path)
    if base_url is None or secret is None or resolved is None:
        return None
    _, relative_path = resolved
    signature = project_file_signature(project.id, relative_path, secret)
    return (
        f"{base_url}/files/{quote(project.id, safe='')}/"
        f"{quote(relative_path, safe='/')}?sig={signature}"
    )


def _path_without_line_suffix(project: Project, destination: str) -> str:
    """Accept Codex's ``/path/file:line`` link form when the file exists."""

    if resolve_project_file(project, destination) is not None:
        return destination
    match = _LINE_SUFFIX_RE.match(destination)
    return match.group("path") if match else destination


def rewrite_project_file_links(
    text: str,
    project: Project,
    settings: Settings,
) -> str:
    """Rewrite only Markdown links that resolve to files in this project."""

    if not text:
        return text

    def unpublished(label: str) -> str:
        visible = label.strip() or "文件"
        return f"{visible}（尚未发布为手机可访问链接）"

    def rewrite_visualize(match: re.Match[str]) -> str:
        try:
            payload = json.loads(match.group("payload"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return "可视化文件尚未发布为手机可访问链接。"
        requested_path = str(payload.get("path") or "").strip()
        title = str(payload.get("title") or "打开可视化").strip()
        public_url = build_project_file_url(project, requested_path, settings)
        if public_url is None:
            return unpublished(title)
        return f"[{title}]({public_url})"

    text = _VISUALIZE_ARTIFACT_RE.sub(rewrite_visualize, text)

    def replace(match: re.Match[str]) -> str:
        wrapped = match.group("destination")
        destination = wrapped[1:-1] if wrapped.startswith("<") else wrapped
        parsed = urlparse(destination)
        if parsed.scheme in {"http", "https"}:
            host = (parsed.hostname or "").lower()
            if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
                label = match.group("prefix").split("[", 1)[-1].rsplit("]", 1)[0]
                return unpublished(label)
            return match.group(0)
        if destination.startswith(("mailto:", "#")):
            return match.group(0)
        requested_path = _path_without_line_suffix(project, destination)
        public_url = build_project_file_url(project, requested_path, settings)
        if public_url is None:
            candidate = destination[7:] if destination.startswith("file://") else destination
            if Path(candidate).expanduser().is_absolute():
                label = match.group("prefix").split("[", 1)[-1].rsplit("]", 1)[0]
                return unpublished(label)
            return match.group(0)
        return (
            f"{match.group('prefix')}{public_url}"
            f"{match.group('title') or ''}{match.group('suffix')}"
        )

    text = _MARKDOWN_LINK_RE.sub(replace, text)

    # Coding Agents commonly report an artifact as `docs/report.html`
    # instead of constructing a Markdown link. They should not need to know
    # MAC's signing endpoint: when the inline-code value is exactly one
    # existing project file, turn it into the same mobile-safe link here.
    # Commands, snippets and nonexistent paths remain untouched.
    def rewrite_inline_code(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        requested_path = _path_without_line_suffix(project, value)
        public_url = build_project_file_url(project, requested_path, settings)
        if public_url is None:
            return match.group(0)
        resolved = resolve_project_file(project, requested_path)
        label = resolved[1] if resolved is not None else value
        return f"[{label}]({public_url})"

    return _INLINE_CODE_RE.sub(rewrite_inline_code, text)


def rewrite_card_project_file_links(
    card: dict[str, Any],
    project: Project,
    settings: Settings,
) -> dict[str, Any]:
    """Rewrite Markdown links throughout a card without mutating the caller."""

    rewritten = deepcopy(card)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: walk(child) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child) for child in value]
        if isinstance(value, str) and (
            "](" in value or "visualize" in value or "`" in value
        ):
            return rewrite_project_file_links(value, project, settings)
        return value

    return walk(rewritten)
