"""Provider-neutral token usage normalization and presentation."""

from __future__ import annotations

from typing import Any


def canonical_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize provider counters without inventing unreported values."""

    if not isinstance(raw, dict):
        return {}
    aliases = {
        "totalTokens": ("totalTokens", "total_tokens"),
        "inputTokens": ("inputTokens", "input_tokens"),
        "cachedInputTokens": ("cachedInputTokens", "cached_input_tokens"),
        "cacheReadInputTokens": (
            "cacheReadInputTokens",
            "cache_read_input_tokens",
        ),
        "cacheCreationInputTokens": (
            "cacheCreationInputTokens",
            "cache_creation_input_tokens",
        ),
        "outputTokens": ("outputTokens", "output_tokens"),
        "reasoningOutputTokens": (
            "reasoningOutputTokens",
            "reasoning_output_tokens",
        ),
    }
    usage: dict[str, int] = {}
    for canonical, candidates in aliases.items():
        for key in candidates:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[canonical] = max(0, int(value))
                break
    if "totalTokens" not in usage:
        input_tokens = usage.get("inputTokens")
        output_tokens = usage.get("outputTokens")
        if input_tokens is not None and output_tokens is not None:
            usage["totalTokens"] = input_tokens + output_tokens
    return usage


def token_usage_summary(executor: str, usage: dict[str, int]) -> str | None:
    """Format one attributable usage row for a collapsed card detail panel."""

    total = usage.get("totalTokens")
    if not isinstance(total, int) or total <= 0:
        return None
    input_tokens = usage.get("inputTokens", 0)
    cached = usage.get("cachedInputTokens", 0)
    cache_read = usage.get("cacheReadInputTokens", 0)
    cache_creation = usage.get("cacheCreationInputTokens", 0)
    output = usage.get("outputTokens", 0)
    detail = f"输入 {input_tokens:,}"
    if cached:
        detail += f"，其中缓存 {cached:,}"
    if cache_read:
        detail += f"；缓存读取 {cache_read:,}"
    if cache_creation:
        detail += f"；缓存写入 {cache_creation:,}"
    detail += f"；输出 {output:,}"
    return f"Token · {executor}：{total:,}（{detail}）"
