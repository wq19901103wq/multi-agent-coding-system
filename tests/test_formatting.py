from mac_system.utils.formatting import (
    format_card_markdown,
    format_tool_preview,
    split_markdown_sections,
    split_text,
)


def test_split_text_keeps_short_text():
    assert split_text("hello") == ["hello"]


def test_split_text_splits_at_paragraphs():
    text = "\n\n".join([f"para {i}" for i in range(100)])
    chunks = split_text(text, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_split_markdown_sections_keeps_short_text():
    assert split_markdown_sections("short") == ["short"]


def test_split_markdown_sections_respects_headers():
    text = "\n\n".join([f"## Section {i}\n\ncontent {i}" for i in range(50)])
    chunks = split_markdown_sections(text, max_chars=500)
    assert len(chunks) > 1
    # Each chunk should start at a header boundary.
    for chunk in chunks[1:]:
        assert chunk.startswith("## Section")


def test_format_card_markdown_fixes_heading_without_space():
    assert "## title" in format_card_markdown("##title")


def test_format_card_markdown_preserves_bold():
    assert "**bold**" in format_card_markdown("**bold**")


def test_format_card_markdown_escapes_angle_brackets():
    text = "use <arg> here"
    formatted = format_card_markdown(text)
    assert "<arg>" not in formatted
    assert "&lt;arg&gt;" in formatted


def test_format_card_markdown_preserves_code_block():
    text = "```\nuse <arg>\n```"
    formatted = format_card_markdown(text)
    assert "<arg>" in formatted
    assert "&lt;arg&gt;" not in formatted


def test_format_tool_preview_known_tools():
    assert "写入文件" in format_tool_preview("Write", {"file_path": "a.py", "content": "x"})
    assert "修改文件" in format_tool_preview("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
    assert "读取文件" in format_tool_preview("Read", {"file_path": "a.py"})
    assert "执行命令" in format_tool_preview("Bash", {"command": "ls"})


def test_format_tool_preview_unknown_tool():
    preview = format_tool_preview("CustomTool", {"foo": "bar", "nested": {"x": 1}})
    assert "CustomTool" in preview
    assert "foo=bar" in preview
    assert "nested" not in preview
