"""Text formatting helpers for Feishu messages and cards."""

from __future__ import annotations

import re
from typing import Any

# Feishu interactive card markdown element is soft-capped well below the 100 KB
# card payload limit; keeping chunks at ~3 k characters leaves headroom for
# JSON escaping and the card envelope.
DEFAULT_CARD_MARKDOWN_MAX_CHARS = 2800

# Plain text messages are limited to 10 000 UTF-8 bytes by Feishu; 4 k chars is
# a safe conservative chunk for mixed CJK/Latin content.
DEFAULT_TEXT_MESSAGE_MAX_CHARS = 4000


def split_text(text: str | None, max_chars: int = DEFAULT_TEXT_MESSAGE_MAX_CHARS) -> list[str]:
    """Split plain text into message-sized chunks at paragraph boundaries."""
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            # Hard split oversized paragraph at line boundaries first.
            for line in paragraph.split("\n"):
                if len(current) + len(line) + 1 > max_chars:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = f"{current}\n{line}".strip() if current else line
            if current:
                chunks.append(current)
                current = ""
            continue

        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}".strip() if current else paragraph

    if current:
        chunks.append(current)
    return chunks if chunks else [text]


def split_markdown_sections(
    text: str | None,
    max_chars: int = DEFAULT_CARD_MARKDOWN_MAX_CHARS,
) -> list[str]:
    """Split Markdown text into card-sized chunks at section boundaries.

    Tries to keep sections together; only splits a section if it alone exceeds
    ``max_chars``.  Returns at least one chunk.
    """
    if not text:
        return ["（暂无内容）"]
    if len(text) <= max_chars:
        return [text]

    # Split on lines that look like section headers (**title** or ## title).
    header_pattern = re.compile(r"^(\*\*[^*]+\*\*|#{1,6}\s+.+)$", re.MULTILINE)
    sections: list[str] = []
    last = 0
    for match in header_pattern.finditer(text):
        if match.start() > last:
            sections.append(text[last:match.start()].strip())
        last = match.start()
    if last < len(text):
        sections.append(text[last:].strip())

    # Drop empty leading chunk if the text started with a header.
    sections = [s for s in sections if s]

    chunks: list[str] = []
    current = ""
    for section in sections:
        if len(section) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            # Hard-split oversized section by paragraphs.
            paragraphs = [p for p in section.split("\n\n") if p.strip()]
            for para in paragraphs:
                if len(current) + len(para) + 2 > max_chars:
                    if current:
                        chunks.append(current)
                    current = para
                else:
                    current = f"{current}\n\n{para}".strip() if current else para
        else:
            if current and len(current) + len(section) + 2 > max_chars:
                chunks.append(current)
                current = section
            else:
                current = f"{current}\n\n{section}".strip() if current else section
    if current:
        chunks.append(current)
    return chunks if chunks else [text]


def _outside_code_blocks(text: str) -> list[tuple[str, bool]]:
    """Return segments of text tagged as inside/outside fenced code blocks."""
    parts: list[tuple[str, bool]] = []
    fence_re = re.compile(r"```+")
    idx = 0
    in_code = False
    for match in fence_re.finditer(text):
        parts.append((text[idx:match.start()], in_code))
        in_code = not in_code
        idx = match.end()
    parts.append((text[idx:], in_code))
    return parts


def normalize_markdown(text: str) -> str:
    """Fix common Feishu Markdown renderer quirks outside code fences.

    - Ensures ATX headings have a space after the hashes.
    - Ensures list markers are followed by a space.
    """
    lines = text.split("\n")
    in_code_fence = False
    out: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            out.append(raw_line)
            continue
        if in_code_fence:
            out.append(raw_line)
            continue

        # Heading: "#title" -> "# title"
        line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", raw_line)
        # Unordered list marker without space, but don't touch bold "**text**".
        line = re.sub(r"^(\*)(?!\*)(?!\s)(\S)", r"\1 \2", line)
        line = re.sub(r"^(-)(?!-)(?!\s)(\S)", r"\1 \2", line)
        line = re.sub(r"^(\+)(?!\+)(?!\s)(\S)", r"\1 \2", line)
        # Ordered list marker without space.
        line = re.sub(r"^(\d+\.)(?!\s)(\S)", r"\1 \2", line)
        out.append(line)
    return "\n".join(out)


def escape_html_outside_code(text: str) -> str:
    """Escape < and > outside fenced code blocks so they render literally."""
    parts = _outside_code_blocks(text)
    result: list[str] = []
    for segment, in_code in parts:
        if in_code:
            result.append(segment)
        else:
            result.append(segment.replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(result)


def format_card_markdown(text: str) -> str:
    """Prepare Markdown for Feishu Card 2.0 ``markdown`` elements."""
    text = normalize_markdown(text)
    text = escape_html_outside_code(text)
    return text


def format_tool_preview(name: str, tool_input: dict[str, Any] | None) -> str:
    """Render a tool call in a concise, human-readable way.

    Avoids dumping raw JSON on the user.
    """
    inp = tool_input or {}
    if name in {"Write", "write", "BashWrite"}:
        path = inp.get("file_path") or inp.get("path") or "未知文件"
        content = inp.get("content", "")
        length = len(content) if isinstance(content, str) else 0
        return f"写入文件 `{path}`（{length} 字符）"
    if name in {"Edit", "edit", "BashEdit"}:
        path = inp.get("file_path") or inp.get("path") or "未知文件"
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        old_len = len(old) if isinstance(old, str) else 0
        new_len = len(new) if isinstance(new, str) else 0
        return f"修改文件 `{path}`（旧 {old_len} / 新 {new_len} 字符）"
    if name in {"Read", "read", "BashRead"}:
        path = inp.get("file_path") or inp.get("path") or inp.get("file") or "未知文件"
        return f"读取文件 `{path}`"
    if name in {"Bash", "bash", "BashTool"}:
        cmd = str(inp.get("command", "")).strip()
        return f"执行命令 `{cmd[:120]}{'...' if len(cmd) > 120 else ''}`"
    if name in {"Glob", "glob"}:
        pattern = inp.get("pattern") or inp.get("path") or "*"
        return f"查找文件 `{pattern}`"
    if name in {"Grep", "grep"}:
        query = inp.get("query") or inp.get("pattern") or ""
        path = inp.get("path") or ""
        return f"搜索 `{query[:80]}`{' in `' + path + '`' if path else ''}"

    # Generic fallback: key=value pairs, skipping nested objects/arrays.
    pairs: list[str] = []
    for key, value in inp.items():
        if isinstance(value, str):
            pairs.append(f"{key}={value[:80]}{'...' if len(value) > 80 else ''}")
        elif isinstance(value, (int, float, bool)) or value is None:
            pairs.append(f"{key}={value}")
    if pairs:
        return f"调用 `{name}`：{', '.join(pairs)}"
    return f"调用工具 `{name}`"
