"""In-memory progress presentation, independent of execution and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any

from mac_system.feishu.card import build_progress_card
from mac_system.utils.identifiers import generate_id
from mac_system.utils.redaction import redact_text
from mac_system.utils.usage import canonical_usage, token_usage_summary


_VISIBLE_BIG_STEPS = 8


_HIDDEN_BIG_STEPS = 3


_VISIBLE_SMALL_STEPS = 7


_HIDDEN_SMALL_STEPS = 2


_AGENT_DETAIL_ROWS = 9


@dataclass
class _ProgressSmallStep:
    key: str
    label: str
    status: str
    detail: str | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass
class _ProgressBigStep:
    title: str
    status: str = "running"
    executor: str = "Codex Root"
    model: str | None = None
    selection_reason: str | None = None
    agent_id: str | None = None
    small_steps: list[_ProgressSmallStep] = field(default_factory=list)


@dataclass
class _AgentExecution:
    display: str
    kind: str
    status: str = "running"
    model: str | None = None
    activity: str | None = None
    detail: str | None = None
    selection_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    updated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass
class _ProgressState:
    started_monotonic: float
    last_event_monotonic: float
    last_event_at: datetime
    label: str
    color: str = "blue"
    status: str = "running"
    narrative: str | None = None
    detail: str | None = None
    child_summary: str | None = None
    executor: str = "Codex Root"
    root_provider_name: str = "Codex"
    model: str | None = None
    root_model: str | None = None
    selection_reason: str | None = None
    external_agent_id: str | None = None
    root_usage: dict[str, int] = field(default_factory=dict)
    native_child_baselines: dict[str, dict[str, int]] = field(default_factory=dict)
    native_child_latest: dict[str, dict[str, int]] = field(default_factory=dict)
    native_child_models: dict[str, str | None] = field(default_factory=dict)
    external_usage: dict[
        str, tuple[str, dict[str, int] | None]
    ] = field(default_factory=dict)
    agent_executions: dict[str, _AgentExecution] = field(default_factory=dict)
    parallel_agents_observed: bool = False
    big_steps: list[_ProgressBigStep] = field(default_factory=list)
    projection_id: str = field(default_factory=lambda: generate_id("progress"))
    projection_segment: int = 0
    projection_suspended: bool = False
    suspended_started_monotonic: float | None = None
    suspended_seconds: float = 0.0
    delivery_failures: int = 0
    next_delivery_attempt_monotonic: float = 0.0


@dataclass
class _ProgressCardView:
    current: str
    steps: list[str]
    activities: list[str]
    hidden_steps: list[str]
    hidden_activities: list[str]
    details: list[tuple[str, list[str], bool]]
    footer: list[str]


def _usage_delta(
    baseline: dict[str, int] | None,
    latest: dict[str, int] | None,
) -> dict[str, int]:
    if not latest:
        return {}
    previous = baseline or {}
    return {
        key: max(0, value - previous.get(key, 0))
        for key, value in latest.items()
        if isinstance(value, int)
    }


def _sum_usage(rows: list[dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        for key, value in row.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _progress_usage_lines(state: _ProgressState) -> list[str]:
    """Render only counters that are attributable to a concrete executor."""

    lines: list[str] = []
    known: list[dict[str, int]] = []
    unknown = 0

    root_usage = canonical_usage(state.root_usage)
    root_line = token_usage_summary(f"{state.root_provider_name} Root", root_usage)
    if root_line:
        lines.append(root_line)
        known.append(root_usage)

    child_rows = [
        _usage_delta(state.native_child_baselines.get(thread_id), latest)
        for thread_id, latest in state.native_child_latest.items()
    ]
    child_rows = [row for row in child_rows if row.get("totalTokens", 0) > 0]
    if child_rows:
        child_usage = _sum_usage(child_rows)
        child_line = token_usage_summary("Codex 子 Agent", child_usage)
        if child_line:
            lines.append(child_line)
            known.append(child_usage)
    elif state.native_child_models:
        lines.append("Token · Codex 子 Agent：后端未报告")
        unknown += 1

    external_by_executor: dict[str, list[dict[str, int]]] = {}
    external_unknown: dict[str, int] = {}
    for executor, usage in state.external_usage.values():
        normalized = canonical_usage(usage)
        if normalized.get("totalTokens", 0) > 0:
            external_by_executor.setdefault(executor, []).append(normalized)
        else:
            external_unknown[executor] = external_unknown.get(executor, 0) + 1
    for executor, rows in external_by_executor.items():
        aggregate = _sum_usage(rows)
        line = token_usage_summary(executor, aggregate)
        if line:
            lines.append(line)
            known.append(aggregate)
    for executor, count in sorted(external_unknown.items()):
        suffix = f"（{count} 次调用）" if count > 1 else ""
        lines.append(f"Token · {executor}：后端未报告{suffix}")
        unknown += count

    participant_count = len(known) + unknown
    if participant_count > 1 and known:
        total = _sum_usage(known)
        suffix = f"（不含 {unknown} 个未报告执行项）" if unknown else ""
        lines.append(f"Token · 可核验合计：{total.get('totalTokens', 0):,}{suffix}")
    return lines


def _execution_participants(state: _ProgressState) -> list[str]:
    participants = []
    for step in state.big_steps:
        executor = step.executor.removesuffix(" Root")
        if executor not in participants:
            participants.append(executor)
    if state.root_provider_name not in participants:
        participants.append(state.root_provider_name)
    if state.native_child_models and "Codex 子 Agent" not in participants:
        participants.append("Codex 子 Agent")
    for executor, _usage in state.external_usage.values():
        if executor not in participants:
            participants.append(executor)
    return participants


def _normalize_agent_status(value: str | None) -> str:
    status = str(value or "running")
    if status in {"completed", "shutdown"}:
        return "completed"
    if status in {"errored", "failed", "notFound"}:
        return "failed"
    if status in {"interrupted", "declined"}:
        return "interrupted"
    return "running"


def _upsert_agent_execution(
    state: _ProgressState,
    key: str,
    *,
    display: str,
    kind: str,
    status: str | None = None,
    model: str | None = None,
    activity: str | None = None,
    detail: str | None = None,
    selection_reason: str | None = None,
) -> _AgentExecution:
    execution = state.agent_executions.get(key)
    if execution is None:
        if any(
            other.status == "running"
            for other in state.agent_executions.values()
        ):
            state.parallel_agents_observed = True
        execution = _AgentExecution(display=display, kind=kind)
        state.agent_executions[key] = execution
    execution.display = display or execution.display
    execution.kind = kind or execution.kind
    execution.status = _normalize_agent_status(status or execution.status)
    execution.model = model or execution.model
    execution.activity = _progress_text(activity, 240) if activity else execution.activity
    execution.detail = _progress_text(detail, 220) if detail else execution.detail
    execution.selection_reason = (
        _progress_text(selection_reason, 320)
        if selection_reason
        else execution.selection_reason
    )
    execution.updated_at = datetime.now().astimezone()
    return execution


def _execution_chain_summary(state: _ProgressState) -> str:
    if not state.agent_executions:
        participants = _execution_participants(state)
        return f"执行链：{' → '.join(participants)}"
    displays: list[str] = []
    for execution in state.agent_executions.values():
        if execution.display not in displays:
            displays.append(execution.display)
    provider = state.root_provider_name
    returned_to_root = state.executor == f"{provider} Root"
    if state.parallel_agents_observed and len(displays) > 1:
        middle = f"并行（{'、'.join(displays)}）"
        suffix = f" → {provider}" if returned_to_root else ""
        return f"执行链：{provider} → {middle}{suffix}"
    chain = [state.big_steps[0].executor.removesuffix(" Root") if state.big_steps else provider]
    for step in state.big_steps:
        display = step.executor.removesuffix(" Root")
        if display != chain[-1]:
            chain.append(display)
    if returned_to_root and chain[-1] != provider:
        chain.append(provider)
    return f"执行链：{' → '.join(chain)}"


def _agent_status_overview(state: _ProgressState) -> str | None:
    if not state.agent_executions:
        return f"子 Agent：{state.child_summary}" if state.child_summary else None
    counts = {"running": 0, "completed": 0, "failed": 0, "interrupted": 0}
    for execution in state.agent_executions.values():
        counts[execution.status] = counts.get(execution.status, 0) + 1
    parts = [f"{len(state.agent_executions)} 个 Agent"]
    labels = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "失败",
        "interrupted": "已中断",
    }
    parts.extend(
        f"{count} {labels[status]}"
        for status, count in counts.items()
        if count
    )
    prefix = "并行执行" if state.parallel_agents_observed else "Agent"
    return f"{prefix}：{' · '.join(parts)}"


def _usage_overview(state: _ProgressState) -> str | None:
    known_totals: list[int] = []
    unknown = 0
    root = canonical_usage(state.root_usage)
    if root.get("totalTokens", 0) > 0:
        known_totals.append(root["totalTokens"])

    child_rows = {
        thread_id: _usage_delta(
            state.native_child_baselines.get(thread_id), latest
        )
        for thread_id, latest in state.native_child_latest.items()
    }
    reported_children = [
        row for row in child_rows.values() if row.get("totalTokens", 0) > 0
    ]
    known_totals.extend(row["totalTokens"] for row in reported_children)
    unknown += sum(
        1
        for thread_id in state.native_child_models
        if child_rows.get(thread_id, {}).get("totalTokens", 0) <= 0
    )

    for _executor, usage in state.external_usage.values():
        normalized = canonical_usage(usage)
        if normalized.get("totalTokens", 0) > 0:
            known_totals.append(normalized["totalTokens"])
        else:
            unknown += 1
    if not known_totals:
        return "Token：后端未报告" if unknown else None
    suffix = f"；另有 {unknown} 项未报告" if unknown else ""
    return f"Token：{sum(known_totals):,}（可核验{suffix}）"


def _agent_detail_lines(state: _ProgressState) -> list[str]:
    icons = {
        "running": "…",
        "completed": "✓",
        "failed": "!",
        "interrupted": "■",
    }
    labels = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "失败",
        "interrupted": "已中断",
    }
    lines = [_execution_chain_summary(state)]
    agent_overview = _agent_status_overview(state)
    if agent_overview:
        lines.append(agent_overview)
    usage_overview = _usage_overview(state)
    if usage_overview:
        lines.append(usage_overview)
    root_model = state.root_model or "后端未报告"
    lines.append(f"{state.root_provider_name} Root · {root_model}")
    executions = list(state.agent_executions.values())
    omitted_executions = max(0, len(executions) - 5)
    if omitted_executions:
        lines.append(f"… 较早 {omitted_executions} 个 Agent 执行已归档")
        executions = executions[-5:]
    for execution in executions:
        model = execution.model or "后端未报告"
        line = (
            f"{icons.get(execution.status, '·')} {execution.display} · {model} · "
            f"{labels.get(execution.status, execution.status)}"
        )
        if execution.activity:
            line += f"\n{execution.activity}"
        if execution.detail:
            line += f"\n{execution.detail}"
        if execution.selection_reason:
            line += f"\n原因：{execution.selection_reason}"
        lines.append(line)
    lines.extend(_progress_usage_lines(state))
    if len(lines) <= _AGENT_DETAIL_ROWS:
        return lines
    # Preserve the execution overview and the newest executor/usage evidence.
    head = lines[:4]
    tail_count = _AGENT_DETAIL_ROWS - len(head) - 1
    omitted = len(lines) - len(head) - tail_count
    return [*head, f"… 另有 {omitted} 条详情已归档", *lines[-tail_count:]]


def _duration_label(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    if minutes:
        return f"{minutes} 分 {remaining} 秒"
    return f"{remaining} 秒"


def _progress_text(value: str, limit: int = 240) -> str:
    return " ".join(redact_text(value).split())[:limit]


def _start_progress_big_step(
    state: _ProgressState,
    title: str,
    *,
    executor: str | None = None,
    model: str | None = None,
    selection_reason: str | None = None,
    agent_id: str | None = None,
    complete_previous: bool = True,
) -> None:
    """Start a dynamic user-visible phase from observed Agent commentary."""

    compact = _progress_text(title, 100)
    if not compact:
        return
    effective_executor = executor or state.executor
    current = state.big_steps[-1] if state.big_steps else None
    if (
        current is not None
        and current.title == compact
        and current.executor == effective_executor
        and current.agent_id == agent_id
    ):
        current.model = model or current.model
        current.selection_reason = selection_reason or current.selection_reason
        state.executor = current.executor
        state.model = current.model
        state.selection_reason = current.selection_reason
        return
    if complete_previous and current is not None and current.status == "running":
        current.status = "completed"
    step = _ProgressBigStep(
        title=compact,
        executor=effective_executor,
        model=model,
        agent_id=agent_id,
        selection_reason=(
            _progress_text(selection_reason, 320) if selection_reason else None
        ),
    )
    state.big_steps.append(step)
    state.executor = step.executor
    state.model = step.model
    state.selection_reason = step.selection_reason


def _record_progress_small_step(
    state: _ProgressState,
    *,
    key: str,
    label: str,
    status: str,
    detail: str | None = None,
    agent_id: str | None = None,
) -> None:
    if not state.big_steps:
        _start_progress_big_step(state, label)
    current = state.big_steps[-1]
    if agent_id is not None:
        current = next(
            (
                step
                for step in reversed(state.big_steps)
                if step.agent_id == agent_id
            ),
            current,
        )
    compact_label = _progress_text(label, 180)
    compact_detail = _progress_text(detail, 220) if detail else None
    for step in reversed(current.small_steps):
        if step.key == key:
            step.label = compact_label
            step.status = status
            step.detail = compact_detail or step.detail
            step.observed_at = datetime.now().astimezone()
            return
    current.small_steps.append(
        _ProgressSmallStep(
            key=key,
            label=compact_label,
            status=status,
            detail=compact_detail,
        )
    )


def _complete_progress_big_step(
    state: _ProgressState,
    status: str,
    *,
    agent_id: str | None = None,
) -> None:
    if not state.big_steps:
        return
    if agent_id is None:
        state.big_steps[-1].status = status
        return
    for step in reversed(state.big_steps):
        if step.agent_id == agent_id:
            step.status = status
            return


def _progress_card_body(
    state: _ProgressState,
    *,
    now_monotonic: float | None = None,
) -> str:
    """Return a text fallback for the structured progress-card view."""

    view = _progress_card_view(state, now_monotonic=now_monotonic)
    sections = [view.current] if view.current else []
    if view.hidden_steps:
        sections.append("**执行步骤 ···**：\n" + "\n".join(view.hidden_steps))
    if view.steps:
        sections.append("**执行步骤**：\n" + "\n".join(view.steps))
    if view.hidden_activities:
        sections.append(
            "**当前步骤详情 ···**：\n" + "\n".join(view.hidden_activities)
        )
    if view.activities:
        sections.append("**当前步骤详情**：\n" + "\n".join(view.activities))
    if view.footer:
        sections.append("\n".join(view.footer))
    for title, rows, _expanded in view.details:
        sections.append(f"**{title}**：\n" + "\n".join(rows))
    return "\n\n".join(sections)


def _progress_card_view(
    state: _ProgressState,
    *,
    now_monotonic: float | None = None,
) -> _ProgressCardView:
    """Project observed execution into a content-first mobile card."""

    current = monotonic() if now_monotonic is None else now_monotonic
    suspended = state.suspended_seconds
    if state.suspended_started_monotonic is not None:
        suspended += max(0.0, current - state.suspended_started_monotonic)
    elapsed = _duration_label(
        max(0.0, current - state.started_monotonic - suspended)
    )
    since_event = max(0, int(current - state.last_event_monotonic))
    current_lines: list[str] = []
    if state.status == "running" and since_event >= 30:
        current_lines.append(
            f"暂未收到新的 {state.root_provider_name} 事件；本轮仍在运行，通常表示正在等待模型或工具返回。"
        )
    big_step_lines: list[str] = []
    small_lines: list[str] = []
    hidden_big_step_lines: list[str] = []
    hidden_activity_lines: list[str] = []
    if state.big_steps:
        def big_step_line(step: _ProgressBigStep) -> str:
            icon = {
                "completed": "✅",
                "failed": "⚠️",
                "interrupted": "⏹",
                "running": "▶️",
            }.get(step.status, "•")
            execution = []
            if step.executor != f"{state.root_provider_name} Root":
                execution.append(step.executor)
            suffix = f" · {' · '.join(execution)}" if execution else ""
            return f"{icon} {step.title}{suffix}"

        visible_big_steps = state.big_steps[-_VISIBLE_BIG_STEPS:]
        hidden_big_steps = state.big_steps[:-_VISIBLE_BIG_STEPS]
        big_step_lines.extend(big_step_line(step) for step in visible_big_steps)
        if hidden_big_steps:
            omitted = max(0, len(hidden_big_steps) - _HIDDEN_BIG_STEPS)
            if omitted:
                hidden_big_step_lines.append(
                    f"… 较早 {omitted} 个大步骤已归档"
                )
            hidden_big_step_lines.extend(
                big_step_line(step) for step in hidden_big_steps[-_HIDDEN_BIG_STEPS:]
            )
        current = state.big_steps[-1]
        if (
            state.status == "running"
            and current.status == "running"
            and current.small_steps
        ):
            def small_step_line(step: _ProgressSmallStep) -> str:
                icon = {
                    "completed": "✓",
                    "failed": "!",
                    "running": "…",
                }.get(step.status, "·")
                detail = f"\n{step.detail}" if step.detail else ""
                return f"{icon} {step.label}{detail}"

            visible_small_steps = current.small_steps[-_VISIBLE_SMALL_STEPS:]
            hidden_small_steps = current.small_steps[:-_VISIBLE_SMALL_STEPS]
            small_lines.extend(small_step_line(step) for step in visible_small_steps)
            if hidden_small_steps:
                omitted = max(0, len(hidden_small_steps) - _HIDDEN_SMALL_STEPS)
                if omitted:
                    hidden_activity_lines.append(
                        f"… 较早 {omitted} 个小步骤已归档"
                    )
                hidden_activity_lines.extend(
                    small_step_line(step)
                    for step in hidden_small_steps[-_HIDDEN_SMALL_STEPS:]
                )
    executor = state.executor.removesuffix(" Root")
    execution_label = "当前执行" if state.status == "running" else "执行"
    duration_label = "已用" if state.status == "running" else "用时"
    footer = [
        f"{execution_label}：{executor} · {duration_label} {elapsed}",
        (
            "最近活动："
            f"{state.last_event_at.astimezone().strftime('%H:%M:%S')}"
            f"（{since_event} 秒前）"
        )
    ]
    details = [("Agent 与用量详情", _agent_detail_lines(state), False)]
    return _ProgressCardView(
        current="\n\n".join(current_lines),
        steps=big_step_lines,
        activities=small_lines,
        hidden_steps=hidden_big_step_lines,
        hidden_activities=hidden_activity_lines,
        details=details,
        footer=footer,
    )


def _render_progress_card(
    title: str,
    state: _ProgressState,
    *,
    color: str | None = None,
) -> dict[str, Any]:
    view = _progress_card_view(state)
    resolved_color = {
        "running": "blue",
        "completed": "green",
        "failed": "red",
        "interrupted": "grey",
    }.get(state.status, color or "blue")
    return build_progress_card(
        title,
        view.current,
        steps=view.steps,
        activities=view.activities,
        hidden_steps=view.hidden_steps,
        hidden_activities=view.hidden_activities,
        details=view.details,
        footer=view.footer,
        color=resolved_color,
    )


def _progress_projection_key(project_id: str, state: _ProgressState) -> str:
    """Identify one immutable chronological segment of a live turn."""

    return (
        f"root-agent:{project_id}:activity:"
        f"{state.projection_id}:{state.projection_segment}"
    )
