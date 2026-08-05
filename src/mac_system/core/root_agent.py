"""MAC host adapter for a persistent Codex root Agent.

The control loop and multi-Agent behavior are Codex 0.144.1 behavior, not a
MAC workflow engine. MAC supplies transport, durable projections and the
Feishu implementation of Codex's native ``request_user_input`` request.
Reference: openai/codex commit 44918ea10c0f99151c6710411b4322c2f5c96bea.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from mac_system.agents.root_runtime import (
    AgentEvent,
    CodexAppServerThread,
    DynamicTool,
    ToolContext,
    TurnResult,
)
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import Settings, load_agent_config
from mac_system.core.lock_manager import get_default_lock_manager
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService, generate_id
from mac_system.core.root_sessions import (
    SHUTDOWN_INTERRUPTED_ERROR,
    RootSessionStore,
)
from mac_system.feishu.card import build_content_card, build_question_batch_card
from mac_system.models import AgentSession, Approval, Project
from mac_system.utils.formatting import split_markdown_sections
from mac_system.utils.redaction import redact_text

if TYPE_CHECKING:
    from mac_system.feishu.bridge import FeishuBridge


logger = logging.getLogger(__name__)
_ROOT_PROFILE_VERSION = "codex-native-root-v2"


@dataclass
class _RootHandle:
    runtime: CodexAppServerThread
    loop: asyncio.AbstractEventLoop


@dataclass
class _PendingUserRequest:
    project_id: str
    interaction_id: str
    item_id: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[dict[str, Any]]


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
    small_steps: list[_ProgressSmallStep] = field(default_factory=list)
    collapsed_small_steps: int = 0


@dataclass
class _ProgressState:
    started_monotonic: float
    last_event_monotonic: float
    last_event_at: datetime
    label: str
    color: str = "purple"
    narrative: str | None = None
    detail: str | None = None
    child_summary: str | None = None
    executor: str = "Codex Root"
    model: str | None = None
    root_model: str | None = None
    selection_reason: str | None = None
    external_agent_id: str | None = None
    big_steps: list[_ProgressBigStep] = field(default_factory=list)
    collapsed_big_steps: int = 0


_ROOT_THREADS: dict[str, _RootHandle] = {}
_ROOT_THREADS_GUARD = threading.Lock()
_PENDING_USER_REQUESTS: dict[str, _PendingUserRequest] = {}
_PENDING_USER_REQUESTS_GUARD = threading.Lock()
_SERVICE_LOOP: asyncio.AbstractEventLoop | None = None
_SERVICE_LOOP_GUARD = threading.Lock()
_ROOT_TURN_TASKS: set[asyncio.Task[Any] | ConcurrentFuture[Any]] = set()
_ROOT_TURN_TASKS_GUARD = threading.Lock()


def register_root_service_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Bind long-lived root turns to the FastAPI lifespan loop."""

    global _SERVICE_LOOP
    with _SERVICE_LOOP_GUARD:
        _SERVICE_LOOP = loop


def schedule_root_turn(
    bind: Any,
    runtime: AgentRuntime,
    settings: Settings,
    project_id: str,
    trigger: str,
) -> None:
    """Submit a root turn to the application loop and return immediately."""

    async def execute() -> None:
        bridge = None
        if settings.feishu_app_id and settings.feishu_app_secret:
            # Local import avoids loading the Feishu WebSocket wiring while the
            # application dependency graph itself is still being constructed.
            from mac_system.feishu.bridge import FeishuBridge

            bridge = FeishuBridge(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                encrypt_key=settings.feishu_encrypt_key,
                verification_token=settings.feishu_verification_token,
            )
        try:
            with Session(bind) as session:
                engine = RootAgentEngine(
                    session,
                    runtime,
                    settings,
                    feishu_bridge=bridge,
                )
                await engine.run(project_id, trigger)
        finally:
            if bridge is not None:
                await bridge.close()

    with _SERVICE_LOOP_GUARD:
        target = _SERVICE_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if target is None or target.is_closed() or not target.is_running():
        target = current
    if target is None:
        raise RuntimeError("Root Agent service loop is not running")
    if target is current:
        task: asyncio.Task[Any] | ConcurrentFuture[Any] = asyncio.create_task(
            execute(), name=f"mac-root-{project_id}"
        )
    else:
        task = asyncio.run_coroutine_threadsafe(execute(), target)
    with _ROOT_TURN_TASKS_GUARD:
        _ROOT_TURN_TASKS.add(task)

    def finished(done: asyncio.Task[Any] | ConcurrentFuture[Any]) -> None:
        with _ROOT_TURN_TASKS_GUARD:
            _ROOT_TURN_TASKS.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.error(
                "Background root Agent turn failed for %s: %s",
                project_id,
                done.exception(),
            )

    task.add_done_callback(finished)


def _now() -> datetime:
    return datetime.utcnow()


def _compact_text(value: Any, *, limit: int = 4_000) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    half = (limit - 40) // 2
    return f"{value[:half]}\n... [output compacted] ...\n{value[-half:]}"


def _provider_selection_guidance(
    profiles: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, float | int]],
) -> str:
    """Build compact evidence for Root's provider choice without routing it."""
    lines = [
        "Select a provider deliberately. Honor an explicit user choice. Otherwise "
        "use the configured descriptions and recent process metrics below; do not "
        "route by keywords or silently fall back.",
    ]
    for provider in sorted(profiles):
        profile = profiles[provider]
        description = str(profile.get("description") or "No configured description.")
        configured = [
            f"{key}={profile[key]}"
            for key in ("mode", "provider", "model")
            if profile.get(key)
        ]
        metric = metrics.get(provider, {})
        sample_count = int(metric.get("sample_count") or 0)
        if sample_count:
            completed = int(metric.get("completed_count") or 0)
            measured = (
                f"recent CLI completion {completed}/{sample_count}, "
                f"average duration {metric.get('average_duration_seconds')}s, "
                f"average tool calls {metric.get('average_tool_calls')}"
            )
            if "average_cost_usd" in metric:
                measured += f", average reported cost ${metric['average_cost_usd']}"
        else:
            measured = "no recent execution samples"
        suffix = f" ({', '.join(configured)})" if configured else ""
        lines.append(f"- {provider}: {description}{suffix}; {measured}.")
    return "\n".join(lines)


def _codex_role_overrides(agent_config: dict[str, Any]) -> dict[str, Any]:
    """Translate operator config to Codex's official ``agents.<role>`` layer."""

    configured = agent_config.get("codex_agent_roles")
    if not isinstance(configured, dict):
        return {}
    overrides: dict[str, Any] = {}
    official_fields = ("description", "config_file", "nickname_candidates")
    for role_name, raw_role in configured.items():
        if not isinstance(role_name, str) or not isinstance(raw_role, dict):
            continue
        if raw_role.get("enabled", True) is not True:
            continue
        role = {
            field: raw_role[field]
            for field in official_fields
            if raw_role.get(field) is not None
        }
        if role:
            overrides[f"agents.{role_name}"] = role
    return overrides


def _root_profile_key(tools: list[DynamicTool]) -> str:
    """Version persisted threads by the dynamic-tool protocol they contain.

    Codex 0.144.1 accepts dynamic tools only on ``thread/start``. Its official
    ``thread/resume`` protocol cannot replace them, so resuming a thread with a
    changed schema would give the model and MAC handler different contracts.
    """

    contract = [
        {"name": tool.name, "input_schema": tool.input_schema}
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    digest = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{_ROOT_PROFILE_VERSION}:{digest}"


def _external_progress_label(provider: str, kind: str, detail: str) -> str | None:
    """Translate external CLI progress into concise user-facing activity."""
    display = {
        "claude_code": "Claude Code",
        "kimi_code": "Kimi Code",
        "codex": "Codex",
        "pi": "Pi",
    }.get(provider, provider)
    if kind == "thinking":
        return f"{display} 正在分析任务"
    if kind == "commentary":
        return f"{display} 已汇报阶段进展，正在继续处理"
    if kind != "tool":
        return None
    try:
        tool = str((json.loads(detail) or {}).get("name") or "").lower()
    except (TypeError, ValueError, json.JSONDecodeError):
        tool = ""
    if tool in {"read", "grep", "find", "search", "ls", "glob"}:
        action = "正在查看项目资料"
    elif tool in {"edit", "write", "file_change"}:
        action = "正在修改项目文件"
    elif tool in {"bash", "command", "shell", "command_execution"}:
        action = "正在执行检查或测试"
    elif tool in {"browser", "host_automation"}:
        action = "正在操作本机应用"
    else:
        action = "正在继续执行任务"
    return f"{display} {action}"


def _external_progress_detail(kind: str, detail: str) -> str | None:
    """Keep provider activity useful while excluding tool arguments and output."""

    if kind != "tool":
        return None
    try:
        payload = json.loads(detail) or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    name = str(payload.get("name") or payload.get("tool") or "").strip()
    return f"工具：{name[:120]}" if name else "工具：编码工具"


def _external_tool_summary(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Return useful activity evidence without reinjecting tool payloads."""
    names: list[str] = []
    for call in tool_calls:
        name = call.get("name") or call.get("tool") or call.get("type")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return {"count": len(tool_calls), "names": names[:12]}


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
) -> None:
    """Start a dynamic user-visible phase from observed Agent commentary."""

    compact = _progress_text(title)
    if not compact:
        return
    effective_executor = executor or state.executor
    current = state.big_steps[-1] if state.big_steps else None
    if (
        current is not None
        and current.title == compact
        and current.executor == effective_executor
    ):
        current.model = model or current.model
        current.selection_reason = selection_reason or current.selection_reason
        state.executor = current.executor
        state.model = current.model
        state.selection_reason = current.selection_reason
        return
    if current is not None and current.status == "running":
        current.status = "completed"
    step = _ProgressBigStep(
        title=compact,
        executor=effective_executor,
        model=model,
        selection_reason=(
            _progress_text(selection_reason, 320) if selection_reason else None
        ),
    )
    state.big_steps.append(step)
    if len(state.big_steps) > 8:
        state.collapsed_big_steps += len(state.big_steps) - 8
        state.big_steps = state.big_steps[-8:]
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
) -> None:
    if not state.big_steps:
        _start_progress_big_step(state, "理解并处理本轮任务")
    current = state.big_steps[-1]
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
    if len(current.small_steps) > 7:
        current.collapsed_small_steps += len(current.small_steps) - 7
        current.small_steps = current.small_steps[-7:]


def _complete_progress_big_step(state: _ProgressState, status: str) -> None:
    if state.big_steps:
        state.big_steps[-1].status = status


def _progress_card_body(
    state: _ProgressState,
    *,
    now_monotonic: float | None = None,
) -> str:
    """Render only observed state; elapsed time is not synthetic progress."""

    current = monotonic() if now_monotonic is None else now_monotonic
    elapsed = _duration_label(current - state.started_monotonic)
    since_event = max(0, int(current - state.last_event_monotonic))
    model = state.model or "后端未报告"
    lines = [
        f"**当前执行主体**：{state.executor}",
        f"**模型**：{model}",
    ]
    if state.selection_reason:
        lines.append(f"**选择原因**：{state.selection_reason}")
    lines.extend([
        f"**当前状态**：{state.label}",
        f"**已用时间**：{elapsed}",
        (
            "**最近事件**："
            f"{state.last_event_at.astimezone().strftime('%H:%M:%S')}"
            f"（{since_event} 秒前）"
        ),
    ])
    if state.child_summary:
        lines.append(f"**子 Agent**：{state.child_summary}")
    if state.big_steps:
        big_step_lines = []
        if state.collapsed_big_steps:
            big_step_lines.append(
                f"… 更早 {state.collapsed_big_steps} 个大步骤已收起"
            )
        current_step = state.big_steps[-1]
        for step in state.big_steps:
            icon = {
                "completed": "✅",
                "failed": "⚠️",
                "running": "▶️",
            }.get(step.status, "•")
            execution = []
            if step.executor != "Codex Root":
                execution.append(step.executor)
            # Root model is already shown once in the card metadata. Repeat
            # model/provider details only where execution actually crossed an
            # external coding-agent boundary.
            if step.executor != "Codex Root" and step.model:
                execution.append(step.model)
            suffix = f" · {' · '.join(execution)}" if execution else ""
            big_step_lines.append(f"{icon} {step.title}{suffix}")
            # The active external step already exposes this in the card
            # metadata. Preserve it here only after that step becomes history.
            if step.selection_reason and step is not current_step:
                big_step_lines.append(f"  原因：{step.selection_reason}")
        lines.append("**大步骤**：\n" + "\n".join(big_step_lines))
        current = state.big_steps[-1]
        if current.small_steps:
            small_lines = []
            if current.collapsed_small_steps:
                small_lines.append(
                    f"… 更早 {current.collapsed_small_steps} 个小步骤已收起"
                )
            for step in current.small_steps:
                icon = {
                    "completed": "✓",
                    "failed": "!",
                    "running": "…",
                }.get(step.status, "·")
                detail = f" — {step.detail}" if step.detail else ""
                small_lines.append(
                    f"{icon} {step.observed_at.strftime('%H:%M:%S')} {step.label}{detail}"
                )
            lines.append("**当前大步骤 · 小步骤**：\n" + "\n".join(small_lines))
    if since_event >= 30:
        lines.append(
            "**状态说明**：暂未收到新的 Codex 事件；本轮仍在运行，"
            "通常表示正在等待模型或工具返回。"
        )
    return "\n\n".join(lines)


def _native_child_summary(item: dict[str, Any]) -> str | None:
    """Summarize Codex's official child-agent states without exposing IDs."""

    raw_states = item.get("agentsStates")
    if not isinstance(raw_states, dict) or not raw_states:
        return None
    labels = {
        "pendingInit": "准备中",
        "running": "运行中",
        "interrupted": "已中止",
        "completed": "已完成",
        "errored": "异常",
        "shutdown": "已关闭",
        "notFound": "不可用",
    }
    counts: dict[str, int] = {}
    for value in raw_states.values():
        status = value.get("status") if isinstance(value, dict) else value
        status_text = str(status or "unknown")
        counts[status_text] = counts.get(status_text, 0) + 1
    return "，".join(
        f"{labels.get(status, status)} {count}"
        for status, count in counts.items()
    )


def _durable_native_event(event: AgentEvent) -> AgentEvent | None:
    """Keep Codex execution untouched while storing only stable UI boundaries."""

    kind_lower = event.kind.lower()
    if (
        kind_lower.endswith("delta")
        or event.kind
        in {
            "turn/diff/updated",
            "item/commandExecution/terminalInteraction",
        }
        or not event.kind
    ):
        return None

    payload = dict(event.payload)
    item = payload.get("item")
    if event.kind in {"item/started", "item/completed"} and isinstance(item, dict):
        item_type = str(item.get("type") or "")
        compact_item = {
            key: item[key]
            for key in (
                "type",
                "id",
                "status",
                "command",
                "cwd",
                "processId",
                "exitCode",
                "kind",
                "tool",
                "server",
                "namespace",
                "pluginId",
                "durationMs",
                "path",
                "phase",
                "senderThreadId",
                "receiverThreadIds",
                "model",
                "reasoningEffort",
            )
            if key in item
        }
        if item_type == "collabAgentToolCall":
            raw_states = item.get("agentsStates")
            if isinstance(raw_states, dict):
                compact_item["agentsStates"] = {
                    str(thread_id): {"status": state.get("status")}
                    for thread_id, state in raw_states.items()
                    if isinstance(state, dict) and state.get("status")
                }
        if item_type == "commandExecution":
            output = _compact_text(item.get("aggregatedOutput"))
            if output is not None:
                compact_item["aggregatedOutput"] = output
        elif item_type == "fileChange":
            compact_item["changes"] = [
                {
                    "path": change.get("path"),
                    "kind": change.get("kind"),
                }
                for change in item.get("changes", [])
                if isinstance(change, dict)
            ]
        else:
            text = _compact_text(item.get("text"), limit=12_000)
            if text is not None:
                compact_item["text"] = text
        payload["item"] = compact_item

    return AgentEvent(
        kind=event.kind,
        thread_id=event.thread_id,
        turn_id=event.turn_id,
        payload=payload,
    )


def _native_activity_card(
    event_kind: str, item: dict[str, Any]
) -> tuple[str, str, str] | None:
    """Translate official Codex item lifecycle into user-facing progress.

    Item completion is not project completion.  Every projection therefore
    remains a running progress card until the native turn itself finishes.
    """

    item_type = str(item.get("type") or "")
    status = str(item.get("status") or "")
    started = event_kind == "item/started" or status == "inProgress"
    failed = status in {"failed", "declined"}

    if item_type == "agentMessage":
        text = str(item.get("text") or "").strip()
        if event_kind != "item/completed" or not text:
            return None
        return text[:1_200], "blue", "running"

    if item_type == "reasoning":
        if started:
            return "Codex 正在分析任务", "purple", "running"
        return "Codex 已完成一轮分析，正在继续处理", "blue", "running"

    if item_type == "plan":
        if started:
            return "正在整理执行步骤", "purple", "running"
        text = str(item.get("text") or "").strip()
        label = f"执行计划：{text[:600]}" if text else "执行计划已更新"
        return label, "blue", "running"

    labels = {
        "commandExecution": (
            "正在检查或验证项目",
            "已完成一次检查或验证，Codex 继续处理",
            "一次检查或验证未通过，Codex 正在分析",
        ),
        "fileChange": (
            "正在修改项目文件",
            "项目文件已更新，Codex 继续处理",
            "文件修改未完成，Codex 正在分析",
        ),
        "dynamicToolCall": (
            "正在调用外部编码能力",
            "外部编码能力已返回，Codex 继续处理",
            "外部编码能力未完成，Codex 正在分析",
        ),
        "mcpToolCall": (
            "正在使用已连接的工具",
            "工具已返回结果，Codex 继续处理",
            "工具调用未完成，Codex 正在分析",
        ),
        "webSearch": (
            "正在查找所需资料",
            "资料查找完成，Codex 继续处理",
            "资料查找未完成，Codex 正在分析",
        ),
        "collabAgentToolCall": (
            "子 Agent 正在处理",
            "子 Agent 已返回，Codex 继续处理",
            "子 Agent 未完成任务，Codex 正在分析",
        ),
        "subAgentActivity": (
            "子 Agent 正在处理",
            "子 Agent 已返回，Codex 继续处理",
            "子 Agent 未完成任务，Codex 正在分析",
        ),
        "contextCompaction": (
            "正在整理较长会话的上下文",
            "上下文已整理，Codex 继续处理",
            "上下文整理未完成，Codex 正在分析",
        ),
        "imageView": (
            "正在查看项目图片",
            "图片已查看，Codex 继续处理",
            "图片查看未完成，Codex 正在分析",
        ),
        "imageGeneration": (
            "正在生成图片",
            "图片已生成，Codex 继续处理",
            "图片生成未完成，Codex 正在分析",
        ),
    }
    wording = labels.get(item_type)
    if wording is None:
        return None
    if started:
        return wording[0], "purple", "running"
    if failed:
        return wording[2], "orange", "running"
    return wording[1], "green", "running"


def _native_activity_detail(item: dict[str, Any]) -> str:
    """Describe the active native tool without leaking arguments or output."""

    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        cwd = str(item.get("cwd") or "").strip()
        return f"工具：终端命令；目录：{Path(cwd).name}" if cwd else "工具：终端命令"
    if item_type == "fileChange":
        paths = [
            str(change.get("path") or "").strip()
            for change in (item.get("changes") or [])
            if isinstance(change, dict) and str(change.get("path") or "").strip()
        ]
        if paths:
            shown = "、".join(paths[:3])
            suffix = f" 等 {len(paths)} 个文件" if len(paths) > 3 else ""
            return f"文件：{shown}{suffix}"
        return "工具：文件修改"
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or "").strip()
        name = "/".join(part for part in (server, tool) if part)
        return f"工具：{name}" if name else "工具：已连接能力"
    if item_type == "dynamicToolCall":
        namespace = str(item.get("namespace") or "").strip()
        tool = str(item.get("tool") or "").strip()
        name = "/".join(part for part in (namespace, tool) if part)
        return f"工具：{name}" if name else "工具：外部编码能力"
    if item_type in {"collabAgentToolCall", "subAgentActivity"}:
        tool = str(item.get("tool") or item.get("kind") or "").strip()
        return f"子 Agent 操作：{tool}" if tool else "子 Agent 正在执行任务"
    if item_type == "imageView":
        path = str(item.get("path") or "").strip()
        return f"图片：{Path(path).name}" if path else "工具：图片查看"
    return ""


def _native_status_card(
    event_kind: str, payload: dict[str, Any]
) -> tuple[str, str, str] | None:
    """Translate official non-item turn status into the shared progress card."""

    if event_kind == "turn/started":
        return "Codex 已开始处理本轮消息。", "purple", "running"
    if event_kind != "error":
        return None
    if payload.get("willRetry") is True:
        return "与 Codex 的连接暂时中断，正在自动重连。", "orange", "running"
    return "Codex 本轮遇到错误，正在结束本轮。", "red", "running"


def _turn_completion_card(status: str) -> tuple[str, str, str]:
    """Map the official turn terminal status without inventing a workflow state."""

    if status == "completed":
        return "本轮处理完成，项目可继续迭代。", "green", "completed"
    if status == "interrupted":
        return "本轮已中止，项目可继续发送新消息。", "orange", "interrupted"
    return "本轮未完成，项目可直接重试。", "red", "failed"


class RootAgentEngine:
    """Map one MAC project to one persistent Codex root thread."""

    def __init__(
        self,
        session: Session,
        runtime: AgentRuntime,
        settings: Settings,
        *,
        feishu_bridge: FeishuBridge | None = None,
    ) -> None:
        # ``runtime`` is reserved for optional non-Codex provider extensions.
        # Native Codex owns its own child threads and workspace execution.
        self.session = session
        self.runtime = runtime
        self.settings = settings
        self.feishu_bridge = feishu_bridge
        self.bind = session.get_bind()
        self.service = ProjectService(session)
        self.store = RootSessionStore(session)
        self.project: Project | None = None
        self._progress: dict[str, _ProgressState] = {}
        self._progress_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_progress_delivery: dict[str, str] = {}
        self._progress_last_delivery: dict[str, float] = {}
        self._progress_min_delivery_interval = 1.5 if feishu_bridge is not None else 0.0
        self._agent_config = load_agent_config(settings.agent_config_path)

    async def run(self, project_id: str, trigger: str) -> TurnResult:
        project = self.service.require_project(project_id)
        self.project = project
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            self.session.expire_all()
            project = self.service.require_project(project_id)
            root: CodexAppServerThread | None = None
            heartbeat: asyncio.Task[None] | None = None
            try:
                self.store.set_turn(project_id, turn_id=None, status="running")
                current = monotonic()
                progress_state = _ProgressState(
                    started_monotonic=current,
                    last_event_monotonic=current,
                    last_event_at=datetime.now().astimezone(),
                    label="正在准备 Codex 项目上下文",
                )
                _start_progress_big_step(progress_state, "理解并准备本轮任务")
                _record_progress_small_step(
                    progress_state,
                    key="root-start",
                    label="正在准备项目上下文",
                    status="running",
                )
                self._progress[project_id] = progress_state
                self._queue_progress_snapshot(
                    project_id,
                    event_identity=generate_id("root-start"),
                )
                heartbeat = asyncio.create_task(
                    self._progress_heartbeat(project_id),
                    name=f"mac-progress-{project_id}",
                )
                root = await self._root_thread(project)
                active_model = getattr(root, "active_model", None)
                progress_state.root_model = active_model
                progress_state.model = active_model
                result = await root.run_turn(trigger)
                await self._stop_progress_heartbeat(heartbeat)
                heartbeat = None
                await self._flush_progress_snapshots(project_id)
                self.store.set_turn(project_id, turn_id=None, status="idle")
                try:
                    await self._publish_turn_status(project, result)
                except Exception:
                    logger.warning(
                        "Root completion projection failed for %s",
                        project_id,
                        exc_info=True,
                    )
                if result.final_message:
                    try:
                        await self._publish_final(project, result)
                    except Exception:
                        logger.warning(
                            "Root final projection failed for %s",
                            project_id,
                            exc_info=True,
                        )
                return result
            except BaseException as exc:
                await self._stop_progress_heartbeat(heartbeat)
                heartbeat = None
                if isinstance(exc, asyncio.CancelledError):
                    await self._cancel_progress_snapshots(project_id)
                else:
                    await self._flush_progress_snapshots(project_id)
                if root is not None:
                    try:
                        await self._release_root(project_id, root)
                    except Exception:
                        logger.warning(
                            "Failed to close failed native Codex transport for %s",
                            project_id,
                            exc_info=True,
                        )
                    finally:
                        root = None
                # A failed commit leaves SQLAlchemy's Session unusable until a
                # rollback. Recovery uses a new short transaction so cleanup
                # cannot be prevented by the failure that triggered it.
                reported_exc = (
                    exc
                    if isinstance(exc, Exception)
                    else RuntimeError(SHUTDOWN_INTERRUPTED_ERROR)
                )
                self._recover_failed_turn(project_id, reported_exc)
                if isinstance(exc, Exception):
                    await self._publish_failure_safely(project_id, reported_exc)
                raise
            finally:
                await self._stop_progress_heartbeat(heartbeat)
                self._progress.pop(project_id, None)

    async def steer(self, project_id: str, text: str) -> bool:
        row = self.store.ensure(project_id)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        if (
            handle is None
            or not row.active_turn_id
            or row.status not in {"running", "waiting_user"}
        ):
            return False
        current = asyncio.get_running_loop()
        if handle.loop is current:
            await handle.runtime.steer(row.active_turn_id, text)
        elif handle.loop.is_running():
            pending = asyncio.run_coroutine_threadsafe(
                handle.runtime.steer(row.active_turn_id, text), handle.loop
            )
            await asyncio.wrap_future(pending)
        else:
            return False
        return True

    async def interrupt(self, project_id: str) -> bool:
        """Interrupt the current native Codex turn without ending its thread."""

        row = self.store.ensure(project_id)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        if handle is None or not row.active_turn_id:
            return False
        current = asyncio.get_running_loop()
        if handle.loop is current:
            await handle.runtime.interrupt(row.active_turn_id)
        elif handle.loop.is_running():
            pending = asyncio.run_coroutine_threadsafe(
                handle.runtime.interrupt(row.active_turn_id), handle.loop
            )
            await asyncio.wrap_future(pending)
        else:
            return False
        return True

    async def start_new_thread(
        self, project_id: str
    ) -> tuple[str | None, str | None]:
        """Detach an idle project from its current native Codex thread.

        The replacement ``thread/start`` is deliberately deferred until the next
        user turn. Codex does not persist an empty rollout, while Feishu message
        handling and background turns may run on different event loops. Creating
        and then closing an empty app-server thread here would therefore leave a
        thread id that the next transport cannot resume.
        """

        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            self.session.expire_all()
            project = self.service.require_project(project_id)
            durable = self.store.ensure(project_id)
            if durable.status != "idle" or durable.active_turn_id is not None:
                raise RuntimeError("当前项目仍在处理消息，请等本轮结束后再新建会话。")

            old_thread_id = durable.native_thread_id
            with _ROOT_THREADS_GUARD:
                handle = _ROOT_THREADS.pop(project_id, None)
            if handle is not None:
                current = asyncio.get_running_loop()
                if handle.loop is current:
                    await handle.runtime.close()
                elif handle.loop.is_running():
                    pending = asyncio.run_coroutine_threadsafe(
                        handle.runtime.close(), handle.loop
                    )
                    await asyncio.wrap_future(pending)
                else:
                    # A stopped owner loop cannot safely service the old JSON-RPC
                    # client. It has already been detached from MAC and the next
                    # line prevents the rollout from being resumed.
                    logger.warning(
                        "Discarding Codex transport owned by a stopped loop for %s",
                        project_id,
                    )

            self.store.clear_native_thread(project_id)
            project.context = {
                **(project.context or {}),
                "root_thread_reset_pending": True,
            }
            project.updated_at = _now()
            self.session.add(project)
            await self.service.log_event(
                project_id,
                "INFO",
                "Native Codex thread reset by explicit user command",
                {
                    "old_thread_id": old_thread_id,
                    "new_thread_id": None,
                    "replacement_pending": True,
                },
            )
            self.session.commit()
            return old_thread_id, None

    async def _root_thread(self, project: Project) -> CodexAppServerThread:
        current_loop = asyncio.get_running_loop()
        extension_tools = self._extension_tools(project)
        root_profile = _root_profile_key(extension_tools)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project.id)
        root = handle.runtime if handle is not None else None
        if handle is not None and handle.loop is not current_loop:
            if handle.loop.is_running():
                pending = asyncio.run_coroutine_threadsafe(root.close(), handle.loop)
                await asyncio.wrap_future(pending)
            with _ROOT_THREADS_GUARD:
                _ROOT_THREADS.pop(project.id, None)
            root = None

        if root is None:
            root = CodexAppServerThread(
                codex_bin=self.settings.codex_bin,
                cwd=Path(project.workspace_path),
                tools=extension_tools,
                # Do not replace or augment Codex's native workflow prompt.
                developer_instructions=None,
                event_handler=lambda event: self._on_event(project.id, event),
                server_request_handler=lambda method, params: self._on_server_request(
                    project.id, method, params
                ),
                # Match Codex itself: the root Agent is a coding Agent with its
                # normal execution environment and native multi-Agent tools.
                sandbox="workspace-write",
                approval_policy="never",
                ephemeral=False,
                environments=None,
                thread_config={
                    # The built-in OpenAI provider enables Responses over
                    # WebSocket and cannot be overridden. This environment's
                    # proxy accepts HTTPS but makes every WebSocket turn burn
                    # through Codex's retry budget before falling back. Define
                    # an equivalent authenticated provider through Codex's
                    # official per-thread config surface and select HTTPS from
                    # the start instead of reimplementing transport fallback.
                    "model_provider": "mac_openai_https",
                    "model_providers.mac_openai_https": {
                        "name": "OpenAI HTTPS",
                        "wire_api": "responses",
                        "requires_openai_auth": True,
                        "supports_websockets": False,
                    },
                    # Keep long-lived mobile projects responsive using Codex's
                    # native compactor. The default follows the model's very
                    # large context window; real MAC history had reached 110k
                    # input tokens before that threshold was approached.
                    "model_auto_compact_token_limit": 80_000,
                    "features.multi_agent_v2": True,
                    "features.default_mode_request_user_input": True,
                    # Official Codex workspace-write config. MAC projects are
                    # explicitly authorized to run their real API/E2E checks.
                    "sandbox_workspace_write.network_access": True,
                    **_codex_role_overrides(self._agent_config),
                },
                # Use the normal Codex home so installed skills and standard
                # configuration behave exactly as they do in Codex.
                codex_home=None,
            )
            with _ROOT_THREADS_GUARD:
                _ROOT_THREADS[project.id] = _RootHandle(root, current_loop)
        else:
            root.tools = {tool.name: tool for tool in extension_tools}
            root.event_handler = lambda event: self._on_event(project.id, event)
            root.server_request_handler = lambda method, params: self._on_server_request(
                project.id, method, params
            )

        durable = self.store.ensure(project.id)
        profile_current = (
            (project.context or {}).get("root_agent_profile") == root_profile
        )
        if root.thread_id is None:
            if durable.native_thread_id and durable.status != "failed" and profile_current:
                await root.resume_thread(durable.native_thread_id)
            else:
                reset_pending = bool(
                    (project.context or {}).get("root_thread_reset_pending")
                )
                if reset_pending:
                    native_id = await root.create_thread(
                        session_start_source="clear"
                    )
                else:
                    native_id = await root.create_thread()
                self.store.set_native_thread(project.id, native_id)
                next_context = dict(project.context or {})
                next_context.pop("root_thread_reset_pending", None)
                project.context = {
                    **next_context,
                    "orchestration_mode": "root_agent",
                    "root_agent_profile": root_profile,
                }
                project.updated_at = _now()
                self.session.add(project)
                self.session.commit()
        return root

    def _extension_tools(self, project: Project) -> list[DynamicTool]:
        """Expose one generic bridge to MAC's optional coding CLI providers."""

        providers = sorted(self.runtime.available_providers())
        if not providers:
            return []
        profiles = self.runtime.provider_profiles()
        metrics = self.store.provider_metrics(providers)
        provider_guidance = _provider_selection_guidance(profiles, metrics)
        return [
            DynamicTool(
                name="run_external_agent",
                description=(
                    "Run one task with an optional external coding-agent CLI. "
                    "Codex native subagents remain available through Codex's own tools.\n"
                    f"{provider_guidance}"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "provider",
                        "selection_reason",
                        "task_name",
                        "message",
                        "access",
                        "timeout_seconds",
                    ],
                    "properties": {
                        "provider": {
                            "type": "string",
                            "enum": providers,
                            "description": (
                                "Provider selected from the evidence in the tool description."
                            ),
                        },
                        "selection_reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                            "description": (
                                "Short user-facing reason this provider adds value for "
                                "this task. Use the same language as the user's request "
                                "and avoid internal routing or protocol jargon."
                            ),
                        },
                        "task_name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Unique stable label for this execution.",
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Complete task sent unchanged to the provider.",
                        },
                        "access": {
                            "type": "string",
                            "enum": [
                                "read_only",
                                "workspace_write",
                                "host_automation",
                            ],
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 10,
                            "maximum": max(10, self.settings.agent_timeout_seconds),
                            "description": (
                                "Hard deadline for this provider call. Choose a value "
                                "that matches the bounded task; the process is stopped "
                                "when the deadline expires."
                            ),
                        },
                    },
                },
                handler=lambda arguments, context: self._run_external_agent(
                    project, arguments, context
                ),
            )
        ]

    async def _run_external_agent(
        self,
        project: Project,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Execute exactly one explicitly selected provider call."""

        async def execute(tool_call_id: str) -> dict[str, Any]:
            provider = str(arguments["provider"])
            selection_reason = " ".join(
                redact_text(str(arguments["selection_reason"])).split()
            )[:300]
            task_name = str(arguments["task_name"])
            message = str(arguments["message"])
            access = str(arguments["access"])
            timeout_seconds = int(arguments["timeout_seconds"])
            capabilities = {
                "read_only": ["Read"],
                "workspace_write": ["Read", "Edit", "Write", "Bash"],
                "host_automation": [
                    "Read",
                    "Edit",
                    "Write",
                    "Bash",
                    "HostAutomation",
                ],
            }[access]
            agent = self.store.create_agent(
                project.id,
                task_name=task_name,
                provider=provider,
                access=access,
                objective=message,
                tool_call_id=tool_call_id,
                selection_reason=selection_reason,
            )
            display = {
                "claude_code": "Claude Code",
                "kimi_code": "Kimi Code",
                "codex": "Codex",
                "pi": "Pi",
            }.get(provider, provider)
            initial_activity = f"已选择 {display}，正在启动外部任务"
            self.store.update_agent(
                agent, status="running", latest_message=initial_activity
            )
            await self._project_external_agent_activity(
                project.id,
                agent.id,
                provider,
                selection_reason,
                initial_activity,
                step_status="running",
            )

            last_activity = initial_activity

            async def on_progress(kind: str, detail: str) -> None:
                nonlocal last_activity
                activity = _external_progress_label(provider, kind, detail)
                if activity is None:
                    return
                if activity == last_activity and kind != "commentary":
                    return
                last_activity = activity
                # The progress card is the live projection. Persisting every
                # transient activity string in a second transaction creates
                # avoidable SQLite writer contention with the outbox while the
                # provider is running. The durable AgentSession is written at
                # start and once at completion.
                await self._project_external_agent_activity(
                    project.id,
                    agent.id,
                    provider,
                    selection_reason,
                    activity,
                    observed_detail=_external_progress_detail(kind, detail),
                    narrative=(
                        " ".join(redact_text(detail).split())[:1_200]
                        if kind == "commentary"
                        else None
                    ),
                    step_status=("completed" if kind == "tool" else "running"),
                )

            result = await self.runtime.execute(
                agent_type="external",
                preferred_provider=provider,
                task_prompt=message,
                system_prompt="",
                workspace=Path(project.workspace_path),
                worktree=Path(project.workspace_path),
                context_files=[],
                output_schema=None,
                allowed_tools=capabilities,
                readonly_dirs=[],
                timeout_seconds=timeout_seconds,
                task_id=agent.id,
                progress_callback=on_progress,
            )
            answer = result.raw_result
            if answer is None and result.output_json is not None:
                answer = json.dumps(result.output_json, ensure_ascii=False)
            answer = redact_text(answer or "")
            failure_detail = None
            if not result.success:
                if result.failure:
                    failure_detail = redact_text(
                        json.dumps(result.failure, ensure_ascii=False)
                    )
                elif result.logs:
                    failure_detail = _compact_text(
                        redact_text(result.logs), limit=4_000
                    )
            stored_payload = {
                "success": result.success,
                "provider": provider,
                "selection_reason": selection_reason,
                "task_name": task_name,
                "timeout_seconds": timeout_seconds,
                "message": answer,
                "error": failure_detail,
                "cost": result.cost_info,
                "tool_calls": result.tool_calls,
            }
            # Finish any in-flight card transaction before committing the
            # authoritative provider result. This keeps presentation writes
            # from turning a successful provider execution into a failed Root
            # tool call on SQLite's single-writer boundary.
            await self._flush_progress_snapshots(project.id)
            with Session(self.bind) as result_session:
                persisted_agent = result_session.get(AgentSession, agent.id)
                if persisted_agent is None:
                    raise RuntimeError(f"external agent session disappeared: {agent.id}")
                RootSessionStore(result_session).update_agent(
                    persisted_agent,
                    status="completed" if result.success else "failed",
                    latest_message=answer or failure_detail,
                    result=stored_payload,
                )
            completion_activity = (
                f"{display} 已完成外部任务，结果已返回 Codex"
                if result.success
                else f"{display} 未完成外部任务，错误已返回 Codex"
            )
            estimated_cost = (result.cost_info or {}).get("estimated_cost_usd")
            if isinstance(estimated_cost, (int, float)):
                project.total_cost_usd = float(project.total_cost_usd or 0) + float(
                    estimated_cost
                )
                project.updated_at = _now()
                self.session.add(project)
                self.session.commit()
            await self._project_external_agent_activity(
                project.id,
                agent.id,
                provider,
                selection_reason,
                completion_activity,
                observed_detail=(
                    f"工具调用：{len(result.tool_calls)} 次"
                    if result.tool_calls
                    else "未调用工具"
                ),
                narrative=(answer or failure_detail or "")[:1_200] or None,
                step_status="completed" if result.success else "failed",
            )
            await self._flush_progress_snapshots(project.id)
            return {
                "success": result.success,
                "provider": provider,
                "selection_reason": selection_reason,
                "task_name": task_name,
                "timeout_seconds": timeout_seconds,
                "message": _compact_text(answer, limit=8_000) or "",
                "message_truncated": len(answer) > 8_000,
                "error": failure_detail,
                "cost": result.cost_info,
                "tool_activity": _external_tool_summary(result.tool_calls),
            }

        return await self.store.execute_tool_once(
            project.id, context, arguments, execute
        )

    @staticmethod
    async def _stop_progress_heartbeat(
        task: asyncio.Task[None] | None,
    ) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _progress_heartbeat(self, project_id: str) -> None:
        """Refresh elapsed/staleness fields without inventing Agent activity."""

        while True:
            await asyncio.sleep(15)
            self._queue_progress_snapshot(
                project_id,
                event_identity=generate_id("root-heartbeat"),
            )

    def _update_progress(
        self,
        project_id: str,
        *,
        label: str | None = None,
        color: str | None = None,
        detail: str | None = None,
        narrative: str | None = None,
        child_summary: str | None = None,
        observed_event: bool = True,
    ) -> None:
        state = self._progress.get(project_id)
        if state is None:
            current = monotonic()
            state = _ProgressState(
                started_monotonic=current,
                last_event_monotonic=current,
                last_event_at=datetime.now().astimezone(),
                label=label or "Codex 正在处理本轮消息",
            )
            self._progress[project_id] = state
        if label is not None:
            state.label = label
        if color is not None:
            state.color = color
        if detail is not None:
            state.detail = detail
        if narrative is not None:
            state.narrative = narrative
        if child_summary is not None:
            state.child_summary = child_summary
        if observed_event:
            state.last_event_monotonic = monotonic()
            state.last_event_at = datetime.now().astimezone()

    async def _publish_progress_snapshot(
        self,
        project_id: str,
        *,
        event_identity: str,
    ) -> None:
        state = self._progress.get(project_id)
        if state is None or self.feishu_bridge is None:
            return
        try:
            with Session(self.bind) as projection_session:
                service = ProjectService(projection_session)
                project = service.require_project(project_id)
                if not project.feishu_chat_id:
                    return
                projection_key = f"root-agent:{project.id}:activity"
                projection = service.get_card_projection(projection_key)
                await deliver_card(
                    service,
                    self.feishu_bridge,
                    dedupe_key=f"{projection_key}:{event_identity}",
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="progress",
                    card=build_content_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        _progress_card_body(state),
                        color=state.color,
                    ),
                    project_id=project.id,
                    update_message_id=(
                        projection.provider_message_id
                        if projection is not None
                        and projection.lifecycle_status == "running"
                        else None
                    ),
                    projection_key=projection_key,
                    subject_type="root_agent",
                    subject_id=project.id,
                    lifecycle_status="running",
                    resulting_state=project.execution_status,
                )
        except Exception:
            logger.warning("Failed to project Root Agent progress", exc_info=True)

    def _queue_progress_snapshot(
        self,
        project_id: str,
        *,
        event_identity: str,
    ) -> None:
        """Coalesce UI updates without blocking Codex's native event stream."""

        self._pending_progress_delivery[project_id] = event_identity
        task = self._progress_delivery_tasks.get(project_id)
        if task is not None and not task.done():
            return

        async def deliver_latest() -> None:
            try:
                while True:
                    wait_for = (
                        self._progress_last_delivery.get(project_id, 0.0)
                        + self._progress_min_delivery_interval
                        - monotonic()
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                    identity = self._pending_progress_delivery.pop(project_id, None)
                    if identity is None:
                        return
                    await self._publish_progress_snapshot(
                        project_id,
                        event_identity=identity,
                    )
                    self._progress_last_delivery[project_id] = monotonic()
            finally:
                self._progress_delivery_tasks.pop(project_id, None)

        self._progress_delivery_tasks[project_id] = asyncio.create_task(
            deliver_latest(),
            name=f"mac-progress-delivery-{project_id}",
        )

    async def _flush_progress_snapshots(self, project_id: str) -> None:
        """Wait until the latest queued UI state is durably projected."""

        while True:
            task = self._progress_delivery_tasks.get(project_id)
            if task is None:
                if project_id not in self._pending_progress_delivery:
                    return
                self._queue_progress_snapshot(
                    project_id,
                    event_identity=self._pending_progress_delivery[project_id],
                )
                task = self._progress_delivery_tasks.get(project_id)
            if task is not None:
                await task

    async def _cancel_progress_snapshots(self, project_id: str) -> None:
        """Drop stale non-terminal UI work when the owning turn is cancelled."""

        self._pending_progress_delivery.pop(project_id, None)
        task = self._progress_delivery_tasks.pop(project_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _project_external_agent_activity(
        self,
        project_id: str,
        agent_id: str,
        provider: str,
        selection_reason: str,
        activity: str,
        observed_detail: str | None = None,
        narrative: str | None = None,
        step_status: str = "running",
    ) -> None:
        """Update the shared progress card from actual external CLI events."""
        detail = f"选择原因：{selection_reason}"
        if observed_detail:
            detail = f"{detail}\n\n{observed_detail}"
        if narrative is not None:
            narrative = " ".join(redact_text(narrative).split())[:1_200]
        self._update_progress(
            project_id,
            label=activity,
            color="purple",
            detail=detail,
            narrative=narrative,
        )
        state = self._progress.get(project_id)
        if state is not None:
            display = {
                "claude_code": "Claude Code",
                "kimi_code": "Kimi Code",
                "codex": "Codex CLI",
                "pi": "Pi",
            }.get(provider, provider)
            profile = self.runtime.provider_profiles().get(provider, {})
            model = str(profile.get("model") or "").strip() or None
            if provider == "claude_code":
                model = self.settings.claude_model or model
            if state.external_agent_id != agent_id:
                state.external_agent_id = agent_id
                _start_progress_big_step(
                    state,
                    f"交给 {display} 独立处理",
                    executor=display,
                    model=model,
                    selection_reason=selection_reason,
                )
            state.executor = display
            state.model = model
            state.selection_reason = selection_reason
            small_step_label = narrative or activity
            _record_progress_small_step(
                state,
                key=f"external:{agent_id}:{activity}:{small_step_label}",
                label=small_step_label,
                status=step_status,
                detail=observed_detail,
            )
        self._queue_progress_snapshot(
            project_id,
            event_identity=f"external:{agent_id}:{activity}",
        )

    async def _on_event(self, project_id: str, event: AgentEvent) -> None:
        # Codex owns token streaming. Persist stable event boundaries only;
        # high-frequency output/diff events remain inside the native turn.
        if event.kind == "account/rateLimits/updated":
            return
        durable = _durable_native_event(event)
        if durable is None:
            return
        # Native event projection is observability, not part of Codex's
        # execution transaction. A locked/broken projection database must not
        # abort the provider turn or poison its owning Session.
        try:
            with Session(self.bind) as event_session:
                event_store = RootSessionStore(event_session)
                event_store.append_event(project_id, durable)
                native = event_store.ensure(project_id)
                if (
                    durable.turn_id
                    and durable.thread_id == native.native_thread_id
                ):
                    event_store.set_turn(
                        project_id, turn_id=durable.turn_id, status="running"
                    )
        except Exception:
            logger.warning(
                "Dropped native Codex event projection for %s (%s)",
                project_id,
                durable.kind,
                exc_info=True,
            )
        self._update_progress(project_id)
        await self._project_native_activity(project_id, durable)

    async def _project_native_activity(self, project_id: str, event: AgentEvent) -> None:
        item = event.payload.get("item")
        if event.kind in {"item/started", "item/completed"} and isinstance(item, dict):
            activity = _native_activity_card(event.kind, item)
            event_identity = str(item.get("id") or "item")
        else:
            activity = _native_status_card(event.kind, event.payload)
            error = event.payload.get("error")
            message = error.get("message") if isinstance(error, dict) else "status"
            event_identity = f"{event.turn_id or 'turn'}:{message}"
        if activity is None:
            return
        label, color, _lifecycle = activity
        child_summary = _native_child_summary(item) if isinstance(item, dict) else None
        detail = _native_activity_detail(item) if isinstance(item, dict) else ""
        narrative = None
        item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        if isinstance(item, dict) and str(item.get("type") or "") == "agentMessage":
            phase = str(item.get("phase") or "")
            text = " ".join(redact_text(str(item.get("text") or "")).split())
            if event.kind == "item/completed" and text and phase == "commentary":
                # Commentary is Codex's public, user-facing progress text. Keep
                # the latest one visible while later tool events update the
                # current activity; do not expose private reasoning content.
                narrative = text[:1_200]
                label = "Codex 已汇报阶段进展，正在继续处理"
        self._update_progress(
            project_id,
            label=label,
            color=color,
            detail=detail,
            narrative=narrative,
            child_summary=child_summary,
            observed_event=False,
        )
        state = self._progress.get(project_id)
        if state is not None:
            if event.kind == "turn/started":
                _record_progress_small_step(
                    state,
                    key="root-start",
                    label="项目上下文已准备完成",
                    status="completed",
                )
            native_executor = (
                "Codex 子 Agent"
                if item_type in {"collabAgentToolCall", "subAgentActivity"}
                else "Codex Root"
            )
            observed_model = None
            if isinstance(item, dict):
                observed_model = str(item.get("model") or "").strip() or None
            observed_model = observed_model or state.root_model
            if narrative:
                state.external_agent_id = None
                _start_progress_big_step(
                    state,
                    narrative,
                    executor="Codex Root",
                    model=observed_model,
                )
            elif item_type and item_type != "agentMessage":
                if item_type == "dynamicToolCall" and event.kind == "item/completed":
                    state.external_agent_id = None
                    _start_progress_big_step(
                        state,
                        "整理外部 coding-agent 返回结果",
                        executor="Codex Root",
                        model=observed_model,
                    )
                if state.external_agent_id is None:
                    state.executor = native_executor
                    state.model = observed_model
                    state.selection_reason = None
                status = "running" if event.kind == "item/started" else "completed"
                if str(item.get("status") or "") in {"failed", "declined"}:
                    status = "failed"
                _record_progress_small_step(
                    state,
                    key=event_identity,
                    label=label,
                    status=status,
                    detail=detail or None,
                )
            elif not item_type:
                _record_progress_small_step(
                    state,
                    key=event_identity,
                    label=label,
                    status="running",
                )
        self._queue_progress_snapshot(
            project_id,
            event_identity=f"{event_identity}:{event.kind}",
        )

    async def _publish_turn_status(
        self, project: Project, result: TurnResult
    ) -> None:
        """Close the shared progress projection when the native turn finishes."""

        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        projection_key = f"root-agent:{project.id}:activity"
        projection = self.service.get_card_projection(projection_key)
        label, color, lifecycle = _turn_completion_card(result.status)
        state = self._progress.get(project.id)
        if state is not None:
            self._update_progress(
                project.id,
                label=label,
                color=color,
                observed_event=True,
            )
            _complete_progress_big_step(
                state,
                "completed" if result.status == "completed" else "failed",
            )
            body = _progress_card_body(state)
        else:
            body = f"**当前进展**：{label}"
        await deliver_card(
            self.service,
            self.feishu_bridge,
            dedupe_key=f"{projection_key}:{result.turn_id}:completed",
            recipient_id=project.feishu_chat_id,
            recipient_type="chat_id",
            intent_type="progress",
            card=build_content_card(
                f"[{self._project_display_name(project)}] 项目进展",
                body,
                color=color,
            ),
            project_id=project.id,
            update_message_id=(
                projection.provider_message_id if projection is not None else None
            ),
            projection_key=projection_key,
            subject_type="root_agent",
            subject_id=project.id,
            lifecycle_status=lifecycle,
            resulting_state="idle",
        )

    async def _on_server_request(
        self, project_id: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method != "item/tool/requestUserInput":
            raise RuntimeError(f"Unsupported Codex server request: {method}")
        return await self._request_user_input(project_id, params)

    async def _request_user_input(
        self, project_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.service.require_project(project_id)
        item_id = str(params.get("itemId") or "").strip()
        raw_questions = params.get("questions") or []
        if not item_id or not raw_questions:
            raise RuntimeError("Codex request_user_input request is incomplete")

        questions: list[dict[str, Any]] = []
        for raw in raw_questions:
            question_id = str(raw.get("id") or "").strip()
            options = [
                {
                    "key": str(option.get("label") or ""),
                    "label": str(option.get("label") or ""),
                    "description": str(option.get("description") or ""),
                }
                for option in (raw.get("options") or [])
                if str(option.get("label") or "").strip()
            ]
            questions.append(
                {
                    "question_id": question_id,
                    "decision_key": question_id,
                    "kind": "decision",
                    "question": str(raw.get("question") or ""),
                    "reason": str(raw.get("header") or ""),
                    "options": options,
                    "allow_free_text": bool(raw.get("isOther")) or not options,
                    "sensitivity": "secret" if raw.get("isSecret") else "internal",
                }
            )

        interaction = await self.service.create_question_interaction(
            project.id,
            None,
            1,
            "ACTIVE",
            "root_agent",
            "conversation",
            f"root:{item_id}",
            questions,
            request_item_id=item_id,
            requested_to="user",
            escalated_to_user=True,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingUserRequest(
            project.id, interaction.id, item_id, loop, future
        )
        with _PENDING_USER_REQUESTS_GUARD:
            _PENDING_USER_REQUESTS[item_id] = pending
        durable = self.store.ensure(project.id)
        self.store.set_turn(
            project.id,
            turn_id=durable.active_turn_id,
            status="waiting_user",
            reason=questions[0]["question"],
        )

        if self.feishu_bridge is not None and project.feishu_chat_id:
            card = build_question_batch_card(
                interaction.id,
                interaction.id,
                questions,
                "root_agent",
                "conversation",
                state="ROOT_AGENT_WAITING_USER",
                project_name=self._project_display_name(project),
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
            )
            try:
                message_id = await deliver_card(
                    self.service,
                    self.feishu_bridge,
                    dedupe_key=f"root-question:{item_id}",
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="question",
                    card=card,
                    project_id=project.id,
                    projection_key=f"question:{interaction.id}",
                    subject_type="question_interaction",
                    subject_id=interaction.id,
                    lifecycle_status="pending",
                    resulting_state="waiting_user",
                )
            except Exception:
                logger.warning(
                    "Root question card delivery failed; Codex request remains pending",
                    exc_info=True,
                )
            else:
                interaction.card_message_id = message_id
                self.session.add(interaction)
                self.session.commit()

        try:
            auto_resolution_ms = params.get("autoResolutionMs")
            if isinstance(auto_resolution_ms, (int, float)):
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=float(auto_resolution_ms) / 1000.0,
                    )
                except asyncio.TimeoutError:
                    for question in self.service.get_questions_for_interaction(
                        interaction.id
                    ):
                        if question.status == "pending":
                            await self.service.submit_question_answer(
                                question.id,
                                None,
                                None,
                                True,
                                "codex_auto_resolution",
                                answer_source="auto_resolution",
                                commit=False,
                            )
                    await self.service.resolve_question_interaction(
                        interaction.id,
                        commit=False,
                        resolution_source="auto_resolution",
                    )
                    if interaction.approval_id:
                        await self.service.respond_approval(
                            interaction.approval_id,
                            "auto_resolution",
                            "codex_auto_resolution",
                            commit=False,
                        )
                    self.session.commit()
                    durable = self.store.ensure(project.id)
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="running",
                    )
                    if self.feishu_bridge is not None and project.feishu_chat_id:
                        await deliver_card(
                            self.service,
                            self.feishu_bridge,
                            dedupe_key=f"root-question-timeout:{item_id}",
                            recipient_id=project.feishu_chat_id,
                            recipient_type="chat_id",
                            intent_type="question",
                            card=build_content_card(
                                f"[{self._project_display_name(project)}] 已继续",
                                "用户未在限定时间内回复；Codex 将按最佳判断继续。",
                                color="orange",
                            ),
                            project_id=project.id,
                            update_message_id=interaction.card_message_id,
                            projection_key=f"question:{interaction.id}",
                            subject_type="question_interaction",
                            subject_id=interaction.id,
                            lifecycle_status="auto_resolved",
                            resolved_by="codex_auto_resolution",
                            resulting_state="running",
                        )
                    return {"answers": {}}
            return await future
        finally:
            with _PENDING_USER_REQUESTS_GUARD:
                current = _PENDING_USER_REQUESTS.get(item_id)
                if current is pending:
                    _PENDING_USER_REQUESTS.pop(item_id, None)

    async def _publish_final(self, project: Project, result: TurnResult) -> None:
        # Codex may emit public commentary before a tool call and retain that
        # text as ``final_message`` when the turn is later interrupted. It is
        # progress evidence, not a successful final answer. The terminal
        # progress projection already reports interrupted/failed faithfully.
        if result.status != "completed":
            return
        content = result.final_message or ""
        if self.feishu_bridge is None or not project.feishu_chat_id:
            self.service.enqueue_notification(
                dedupe_key=f"root-result:{result.turn_id}",
                recipient_id=project.feishu_chat_id or project.id,
                recipient_type="chat_id",
                intent_type="report",
                payload={
                    "title": f"[{self._project_display_name(project)}] 本轮结果",
                    "content": content,
                    "kind": "result",
                },
                project_id=project.id,
            )
            return
        chunks = split_markdown_sections(content)
        if not chunks:
            return
        for index, chunk in enumerate(chunks, start=1):
            card = build_content_card(
                f"[{self._project_display_name(project)}] 本轮结果",
                chunk,
                page=index,
                total=len(chunks),
                color="blue",
            )
            try:
                await deliver_card(
                    self.service,
                    self.feishu_bridge,
                    dedupe_key=f"root-result:{result.turn_id}:{index}",
                    recipient_id=project.feishu_chat_id or project.id,
                    recipient_type="chat_id",
                    intent_type="report",
                    card=card,
                    project_id=project.id,
                    projection_key=f"root-result:{result.turn_id}:{index}",
                    subject_type="root_turn",
                    subject_id=result.turn_id,
                    lifecycle_status="completed",
                    resulting_state="idle",
                )
            except Exception:
                logger.warning("Root result card delivery failed", exc_info=True)

    def _recover_failed_turn(self, project_id: str, exc: BaseException) -> None:
        """Recover durable UI state without relying on the failed Session."""

        try:
            self.session.rollback()
        except Exception:
            logger.warning("Failed to roll back Root Agent Session", exc_info=True)
        try:
            with Session(self.bind) as recovery_session:
                RootSessionStore(recovery_session).set_turn(
                    project_id,
                    turn_id=None,
                    status="idle",
                    error=str(exc),
                    reason=f"Root Agent failed: {exc}",
                )
        except Exception:
            logger.error(
                "Failed to recover durable Root Agent state for %s",
                project_id,
                exc_info=True,
            )

    async def _publish_failure_safely(
        self, project_id: str, exc: Exception
    ) -> None:
        """Publish failure from an independent transaction boundary."""

        try:
            with Session(self.bind) as delivery_session:
                service = ProjectService(delivery_session)
                project = service.require_project(project_id)
                await self._publish_failure(project, exc, service=service)
        except Exception:
            logger.warning("Root failure projection failed", exc_info=True)

    async def _publish_failure(
        self,
        project: Project,
        exc: Exception,
        *,
        service: ProjectService | None = None,
    ) -> None:
        delivery_service = service or self.service
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        projection_key = f"root-agent:{project.id}:activity"
        try:
            self._update_progress(
                project.id,
                label="本轮未完成，已停止；可以直接重试",
                color="red",
            )
            state = self._progress.get(project.id)
            if state is not None:
                _complete_progress_big_step(state, "failed")
            projection = delivery_service.get_card_projection(projection_key)
            await deliver_card(
                delivery_service,
                self.feishu_bridge,
                dedupe_key=generate_id("root-failure-progress"),
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="progress",
                card=build_content_card(
                    f"[{self._project_display_name(project)}] 项目进展",
                    (
                        _progress_card_body(state)
                        if state is not None
                        else "**当前进展**：本轮未完成，已停止；可以直接重试。"
                    ),
                    color="red",
                ),
                project_id=project.id,
                update_message_id=(
                    projection.provider_message_id if projection is not None else None
                ),
                projection_key=projection_key,
                subject_type="root_agent",
                subject_id=project.id,
                lifecycle_status="failed",
                resulting_state="idle",
            )
        except Exception:
            logger.warning("Root failure progress projection failed", exc_info=True)
        try:
            await deliver_card(
                delivery_service,
                self.feishu_bridge,
                dedupe_key=generate_id("root-failure"),
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="report",
                card=build_content_card(
                    f"[{self._project_display_name(project)}] 本轮未完成",
                    "Codex 执行进程未能完成本轮工作；项目未被标记为完成。"
                    "系统已记录错误，可以直接重试。",
                    color="red",
                ),
                project_id=project.id,
                subject_type="root_turn",
                subject_id=project.id,
                lifecycle_status="failed",
                resulting_state="idle",
            )
        except Exception:
            logger.warning("Root failure card delivery failed", exc_info=True)

    @staticmethod
    async def _release_root(project_id: str, root: CodexAppServerThread) -> None:
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
            if handle is not None and handle.runtime is root:
                _ROOT_THREADS.pop(project_id, None)
        await root.close()

    @staticmethod
    def _project_display_name(project: Project) -> str:
        name = str(project.name or "").strip()
        if not name or len(name) > 24 or "\n" in name:
            return "当前项目"
        return name


def resolve_root_user_input(
    service: ProjectService,
    interaction_id: str,
) -> bool:
    """Return durable Feishu answers to the blocked native Codex request."""

    interaction = service.get_question_interaction(interaction_id)
    if interaction is None:
        return False
    item_id = str(interaction.request_item_id or "")
    if not item_id and interaction.approval_id:
        approval = service.session.get(Approval, interaction.approval_id)
        item_id = (
            str((approval.payload or {}).get("root_request_item_id") or "")
            if approval
            else ""
        )
    if not item_id:
        return False
    with _PENDING_USER_REQUESTS_GUARD:
        pending = _PENDING_USER_REQUESTS.get(item_id)
    if pending is None or pending.project_id != interaction.project_id:
        return False

    answers: dict[str, Any] = {}
    for question in service.get_questions_for_interaction(interaction.id):
        answer = service.get_question_answer(question.id)
        if answer is None:
            continue
        value = str(answer.answer_label or answer.answer or "").strip()
        answers[question.question_id] = {"answers": [value] if value else []}
    response = {"answers": answers}

    def deliver() -> None:
        if not pending.future.done():
            pending.future.set_result(response)

    pending.loop.call_soon_threadsafe(deliver)
    store = RootSessionStore(service.session)
    durable = store.ensure(interaction.project_id)
    store.set_turn(
        interaction.project_id,
        turn_id=durable.active_turn_id,
        status="running",
    )
    return True


async def close_root_threads() -> None:
    """Close process-local app-server transports during shutdown."""

    with _ROOT_TURN_TASKS_GUARD:
        turn_tasks = list(_ROOT_TURN_TASKS)
        _ROOT_TURN_TASKS.clear()
    current = asyncio.get_running_loop()
    joins: list[asyncio.Future[Any]] = []
    for task in turn_tasks:
        task.cancel()
        if isinstance(task, asyncio.Future):
            if task.get_loop() is current:
                joins.append(task)
        else:
            joins.append(asyncio.wrap_future(task))
    if joins:
        await asyncio.gather(*joins, return_exceptions=True)

    with _ROOT_THREADS_GUARD:
        handles = list(_ROOT_THREADS.values())
        _ROOT_THREADS.clear()
    for handle in handles:
        try:
            if handle.loop is current:
                await handle.runtime.close()
            elif handle.loop.is_running():
                pending = asyncio.run_coroutine_threadsafe(
                    handle.runtime.close(), handle.loop
                )
                await asyncio.wrap_future(pending)
        except Exception:
            logger.warning("Failed to close root Agent thread", exc_info=True)

    with _PENDING_USER_REQUESTS_GUARD:
        pending_requests = list(_PENDING_USER_REQUESTS.values())
        _PENDING_USER_REQUESTS.clear()
    for pending in pending_requests:
        pending.loop.call_soon_threadsafe(pending.future.cancel)
