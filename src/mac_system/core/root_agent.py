"""MAC host loop for a persistent Project Lead Agent.

The current default runtime is the Codex 0.144.1 adapter, not a MAC workflow
engine. MAC supplies transport, durable projections and mobile interaction;
the project loop depends only on ``ProjectLeadRuntime``.
Reference: openai/codex commit 44918ea10c0f99151c6710411b4322c2f5c96bea.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from sqlmodel import Session, select

from mac_system.agents.root_runtime import (
    AgentEvent,
    CodexAppServerThread,
    DynamicTool,
    ProjectLeadRuntime,
    ProjectLeadRuntimeContext,
    ProjectLeadRuntimeError,
    ProjectLeadRuntimeRegistry,
    ToolContext,
    TurnResult,
    _turn_error_code,
)
from mac_system.agents.kimi_runtime import KimiWireThread
from mac_system.agents.process_lifecycle import snapshot_host_execution, host_execution_has_exited
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import Settings, load_agent_config
from mac_system.core.lock_manager import get_default_lock_manager
from mac_system.core.notification_service import (
    deliver_card,
    is_retryable_delivery_error,
)
from mac_system.core.project_files import rewrite_project_file_links
from mac_system.core.project_owner import (
    ProjectOwner,
    ProjectOwnerResult,
)
from mac_system.core.project_service import ProjectService, generate_id
from mac_system.core.root_progress import (
    _AgentExecution as _AgentExecution,
    _ProgressBigStep as _ProgressBigStep,
    _ProgressCardView as _ProgressCardView,
    _ProgressSmallStep as _ProgressSmallStep,
    _ProgressState as _ProgressState,
    _agent_detail_lines as _agent_detail_lines,
    _agent_status_overview as _agent_status_overview,
    _complete_progress_big_step as _complete_progress_big_step,
    _duration_label as _duration_label,
    _execution_chain_summary as _execution_chain_summary,
    _execution_participants as _execution_participants,
    _normalize_agent_status as _normalize_agent_status,
    _progress_card_body as _progress_card_body,
    _progress_card_view as _progress_card_view,
    _progress_projection_key as _progress_projection_key,
    _progress_text as _progress_text,
    _progress_usage_lines as _progress_usage_lines,
    _record_progress_small_step as _record_progress_small_step,
    _render_progress_card as _render_progress_card,
    _start_progress_big_step as _start_progress_big_step,
    _sum_usage as _sum_usage,
    _upsert_agent_execution as _upsert_agent_execution,
    _usage_delta as _usage_delta,
    _usage_overview as _usage_overview,
)
from mac_system.core.root_sessions import (
    SHUTDOWN_INTERRUPTED_ERROR,
    RootSessionStore,
    prune_redundant_native_event_snapshots,
)
from mac_system.feishu.card import (
    build_content_card,
    build_project_decision_card,
    build_question_batch_card,
)
from mac_system.models import (
    AgentSession,
    Approval,
    EventLog,
    NotificationOutbox,
    Project,
    ProjectDecision,
    ProjectOwnerReview,
    QuestionInteraction,
)
from mac_system.utils.formatting import split_markdown_sections
from mac_system.utils.redaction import redact_secrets, redact_text
from mac_system.utils.usage import canonical_usage, token_usage_summary

if TYPE_CHECKING:
    from mac_system.feishu.bridge import FeishuBridge


logger = logging.getLogger(__name__)
_ROOT_PROFILE_VERSION = "codex-native-root-v2"
_DEFAULT_LEAD_MODEL = "gpt-6.1-sol"
_PROGRESS_HEARTBEAT_SECONDS = 60
_PROGRESS_DELIVERY_BACKOFF_SECONDS = (15, 30, 60, 120)
_NATIVE_INTERACTION_CARD_RETRY_SECONDS = 15


@dataclass
class _RootHandle:
    runtime: ProjectLeadRuntime
    loop: asyncio.AbstractEventLoop
    provider: str
    engine: "RootAgentEngine | None" = None
    closing: bool = False


@dataclass
class _PendingUserRequest:
    project_id: str
    interaction_id: str
    item_id: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[dict[str, Any]]
    response_builder: Callable[[dict[str, str]], dict[str, Any]]


@dataclass
class _TurnTiming:
    """Process-local milestones for one native Codex turn.

    Values are monotonic timestamps.  The durable profile stores only elapsed
    milliseconds, so clock changes and private prompt/tool payloads never leak
    into performance records.
    """

    scheduled: float
    run_started: float
    thread_ready: float | None = None
    turn_started: float | None = None
    first_item: float | None = None
    first_tool: float | None = None
    first_progress_card: float | None = None
    turn_completed: float | None = None
    final_card: float | None = None
    postprocess_completed: float | None = None
    protocol_duration_ms: int | None = None
    root_thread_id: str | None = None
    token_baseline: dict[str, int] | None = None
    token_latest: dict[str, int] | None = None
    context_input_tokens: int | None = None
    context_compacted: bool = False

    def mark_once(self, field_name: str) -> None:
        if getattr(self, field_name) is None:
            setattr(self, field_name, monotonic())

    def profile(self, *, status: str, turn_id: str | None) -> dict[str, Any]:
        end = (
            self.postprocess_completed
            or self.final_card
            or self.turn_completed
            or monotonic()
        )

        def elapsed(value: float | None) -> int | None:
            if value is None:
                return None
            return max(0, round((value - self.scheduled) * 1000))

        profile = {
            "profile_type": "root_turn_timing",
            "turn_id": turn_id,
            "status": status,
            "milestones_ms": {
                "queued": 0,
                "run_started": elapsed(self.run_started),
                "thread_ready": elapsed(self.thread_ready),
                "turn_started": elapsed(self.turn_started),
                "first_item": elapsed(self.first_item),
                "first_tool": elapsed(self.first_tool),
                "first_progress_card": elapsed(self.first_progress_card),
                "turn_completed": elapsed(self.turn_completed),
                "final_card": elapsed(self.final_card),
                "postprocess_completed": elapsed(self.postprocess_completed),
                "total": elapsed(end),
            },
            "protocol_duration_ms": self.protocol_duration_ms,
        }
        usage = self.turn_token_usage()
        if usage:
            profile["token_usage"] = usage
        if isinstance(self.context_input_tokens, int):
            # app-server's ``tokenUsage.last`` is one model request and thus
            # reflects the actual context presented to the model. The per-turn
            # cumulative delta repeats cached context across every tool cycle
            # and must not be used as a context-size observation.
            profile["context_input_tokens"] = self.context_input_tokens
            profile["context_input_source"] = "last_model_input"
        if self.context_compacted:
            profile["context_compacted"] = True
        return profile

    def turn_token_usage(self) -> dict[str, int]:
        # Native compaction is part of the turn and its usage is already in
        # the cumulative counter. Count that delta once, like all other work.
        return _usage_delta(self.token_baseline, self.token_latest)


_ROOT_THREADS: dict[str, _RootHandle] = {}
_ROOT_THREADS_GUARD = threading.Lock()
_PENDING_USER_REQUESTS: dict[str, _PendingUserRequest] = {}
_PENDING_USER_REQUESTS_GUARD = threading.Lock()
_SERVICE_LOOP: asyncio.AbstractEventLoop | None = None
_SERVICE_LOOP_GUARD = threading.Lock()
_ROOT_TURN_TASKS: set[asyncio.Task[Any] | ConcurrentFuture[Any]] = set()
_ROOT_TURN_TASKS_GUARD = threading.Lock()
_OWNER_REVIEW_TASKS: dict[str, asyncio.Task[Any] | ConcurrentFuture[Any]] = {}
_PROJECT_WORK: dict[str, set[asyncio.Task[Any]]] = {}
_PROJECT_RESET_EPOCH: dict[str, int] = {}
_PROJECT_RESETTING: set[str] = set()
_PROJECT_RESET_TASKS: dict[str, asyncio.Task[Any]] = {}
_RESET_WAIT_SECONDS = 30.0


class ProjectResetError(ProjectLeadRuntimeError):
    """Input was not accepted because explicit reset superseded it."""


@asynccontextmanager
async def _project_work(project_id: str, epoch: int | None = None):
    """Track host work so explicit reset can cancel even lock waiters."""
    task = asyncio.current_task()
    assert task is not None
    with _ROOT_THREADS_GUARD:
        if project_id in _PROJECT_RESETTING or (
            epoch is not None and epoch != _PROJECT_RESET_EPOCH.get(project_id, 0)
        ):
            raise ProjectResetError("项目正在重置或旧请求已被 /reset 取消，请重置完成后重新发送。")
        work = _PROJECT_WORK.setdefault(project_id, set())
        registered = task not in work
        work.add(task)
    try:
        yield
    finally:
        with _ROOT_THREADS_GUARD:
            work = _PROJECT_WORK.get(project_id)
            if work is not None and registered:
                work.discard(task)
                if not work:
                    _PROJECT_WORK.pop(project_id, None)


def _request_user_input_response(
    selected: dict[str, str],
) -> dict[str, Any]:
    return {
        "answers": {
            question_id: {"answers": [value] if value else []}
            for question_id, value in selected.items()
        }
    }


def _native_approval_spec(
    method: str,
    params: dict[str, Any],
    *,
    file_changes: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], Callable[[dict[str, str]], dict[str, Any]]]:
    """Map Codex v2 approval requests to one mobile choice and response.

    The method names and response shapes match Codex app-server 0.144.1.
    MAC does not classify commands or invent an approval protocol; the
    isolated Project Owner or the human chooses from Codex's native decisions.
    """

    provider_name = str(params.get("provider") or "Codex")
    question_id = "approval_decision"
    if method == "item/commandExecution/requestApproval":
        command = str(params.get("command") or "未提供命令")
        cwd = str(params.get("cwd") or "")
        reason = str(params.get("reason") or f"{provider_name} 请求在当前沙箱外执行此命令")
        details = [reason, f"目录：{cwd}" if cwd else ""]
        for key, label in (
            ("additionalPermissions", "额外权限"),
            ("networkApprovalContext", "网络访问范围"),
        ):
            if isinstance(params.get(key), dict):
                details.append(
                    f"{label}：{json.dumps(params[key], ensure_ascii=False)}"
                )
        raw_available = params.get("availableDecisions")
        exact_decisions = (
            list(raw_available)
            if isinstance(raw_available, list) and raw_available
            else None
        )
        if exact_decisions is None:
            # Older app-server versions omit availableDecisions. Preserve the
            # documented string decisions and add any explicit policy hints.
            exact_decisions = [
                "accept",
                "acceptForSession",
                "decline",
                "cancel",
            ]
            proposed_execpolicy = params.get("proposedExecpolicyAmendment")
            if isinstance(proposed_execpolicy, list) and proposed_execpolicy:
                exact_decisions.insert(
                    2,
                    {
                        "acceptWithExecpolicyAmendment": {
                            "execpolicy_amendment": proposed_execpolicy,
                        }
                    },
                )
            proposed_network = params.get("proposedNetworkPolicyAmendments")
            if isinstance(proposed_network, list):
                for amendment in proposed_network:
                    if isinstance(amendment, dict):
                        exact_decisions.insert(
                            -2,
                            {
                                "applyNetworkPolicyAmendment": {
                                    "network_policy_amendment": amendment,
                                }
                            },
                        )

        candidates: list[tuple[str, Any, str]] = []
        used_labels: set[str] = set()

        def add_candidate(label: str, decision: Any, description: str) -> None:
            resolved = label
            suffix = 2
            while resolved in used_labels:
                resolved = f"{label} {suffix}"
                suffix += 1
            used_labels.add(resolved)
            candidates.append((resolved, decision, description))

        string_candidates = {
            "accept": ("允许本次", "仅允许这一次执行"),
            "acceptForSession": (
                "本会话允许",
                "相同审批在本项目会话中不再重复询问",
            ),
            "decline": (
                "拒绝并继续",
                f"不执行这条命令，{provider_name} 继续寻找其他方案",
            ),
            "cancel": ("拒绝并停止", "不执行并停止当前轮次"),
        }
        for decision in exact_decisions:
            if isinstance(decision, str):
                presentation = string_candidates.get(decision)
                if presentation is not None:
                    add_candidate(presentation[0], decision, presentation[1])
                continue
            if not isinstance(decision, dict):
                continue
            execpolicy = decision.get("acceptWithExecpolicyAmendment")
            if isinstance(execpolicy, dict):
                amendment = execpolicy.get("execpolicy_amendment")
                if isinstance(amendment, list) and amendment:
                    summary = " ".join(
                        " ".join(redact_text(str(part)).split())
                        for part in amendment
                    )[:100]
                    add_candidate(
                        f"允许规则：{summary}"[:100],
                        decision,
                        f"仅后续匹配 Codex 原生规则 `{summary}` 的命令不再重复询问",
                    )
                continue
            network_policy = decision.get("applyNetworkPolicyAmendment")
            if not isinstance(network_policy, dict):
                continue
            amendment = network_policy.get("network_policy_amendment")
            if not isinstance(amendment, dict):
                continue
            host = " ".join(
                redact_text(str(amendment.get("host") or "该主机")).split()
            )[:80]
            action = str(amendment.get("action") or "")
            if action == "allow":
                label = f"本会话允许访问 {host}"
                description = f"应用网络规则，后续允许访问 {host}"
            elif action == "deny":
                label = f"本会话拒绝访问 {host}"
                description = f"应用网络规则，后续拒绝访问 {host}"
            else:
                label = f"本会话应用 {host} 网络规则"
                description = f"按 Codex 提议应用 {host} 的网络规则"
            add_candidate(label[:100], decision, description)

        if not candidates:
            raise RuntimeError(
                "Codex command approval offered no supported native decisions"
            )
        labels = {label: decision for label, decision, _ in candidates}
        questions = [{
            "question_id": question_id,
            "decision_key": question_id,
            "kind": "blocking",
            "question": f"是否允许执行：{command}",
            "reason": redact_text("\n".join(part for part in details if part)),
            "options": [
                {"key": label, "label": label, "description": description}
                for label, _decision, description in candidates
            ],
            "allow_free_text": False,
            "sensitivity": "internal",
        }]

        def build(selected: dict[str, str]) -> dict[str, Any]:
            choice = selected.get(question_id, "")
            if choice not in labels:
                raise ValueError("Command approval response did not match a native option")
            return {"decision": labels[choice]}

        return questions, build

    if method == "item/fileChange/requestApproval":
        reason = str(params.get("reason") or "Codex 请求写入受保护的文件")
        grant_root = str(params.get("grantRoot") or "")
        details = [reason, f"范围：{grant_root}" if grant_root else ""]
        if file_changes:
            details.append(f"文件修改（{len(file_changes)} 项）：")
            for change in file_changes:
                kind = change.get("kind")
                kind = kind if isinstance(kind, dict) else {}
                action = {"add": "新增", "delete": "删除", "update": "修改"}.get(
                    kind.get("type"), "未知操作"
                )
                path = str(change.get("path") or "未提供路径")
                move_path = kind.get("move_path")
                details.append(
                    f"{action}：{path}"
                    + (f" → {move_path}" if move_path else "")
                )
        else:
            details.append("未取得本次文件修改清单。")
        labels = {
            "允许本次": "accept",
            "本会话允许": "acceptForSession",
            "拒绝并继续": "decline",
            "拒绝并停止": "cancel",
        }
        questions = [{
            "question_id": question_id,
            "decision_key": question_id,
            "kind": "blocking",
            "question": "是否允许这次受保护文件修改？",
            "reason": redact_text("\n".join(part for part in details if part)),
            "options": [
                {"key": label, "label": label, "description": description}
                for label, description in (
                    ("允许本次", "仅允许这一次修改"),
                    ("本会话允许", "相同文件在本项目会话中不再重复询问"),
                    ("拒绝并继续", "不应用修改，Codex 继续处理"),
                    ("拒绝并停止", "不应用修改并停止当前轮次"),
                )
            ],
            "allow_free_text": False,
            "sensitivity": "internal",
        }]

        def build(selected: dict[str, str]) -> dict[str, Any]:
            choice = selected.get(question_id, "")
            if choice not in labels:
                raise ValueError("File approval response did not match a native option")
            return {"decision": labels[choice]}

        return questions, build

    if method == "item/permissions/requestApproval":
        requested = params.get("permissions")
        requested = requested if isinstance(requested, dict) else {}
        reason = str(params.get("reason") or "Codex 请求额外执行权限")
        cwd = str(params.get("cwd") or "")
        questions = [{
            "question_id": question_id,
            "decision_key": question_id,
            "kind": "blocking",
            "question": "是否授予 Codex 请求的额外权限？",
            "reason": redact_text("\n".join(part for part in (
                reason,
                f"目录：{cwd}" if cwd else "",
                f"请求权限：{json.dumps(requested, ensure_ascii=False)}",
            ) if part)),
            "options": [
                {"key": "允许本轮", "label": "允许本轮", "description": "权限仅在当前轮次有效"},
                {"key": "本会话允许", "label": "本会话允许", "description": "权限在当前项目会话中有效"},
                {"key": "拒绝", "label": "拒绝", "description": "不授予额外权限，Codex 继续处理"},
            ],
            "allow_free_text": False,
            "sensitivity": "internal",
        }]

        def build(selected: dict[str, str]) -> dict[str, Any]:
            choice = selected.get(question_id, "")
            if choice not in {"允许本轮", "本会话允许", "拒绝"}:
                raise ValueError(
                    "Permission approval response did not match a native option"
                )
            return {
                "permissions": requested if choice in {"允许本轮", "本会话允许"} else {},
                "scope": "session" if choice == "本会话允许" else "turn",
            }

        return questions, build

    raise RuntimeError(f"Unsupported Codex approval request: {method}")


def _native_approval_resolution(
    response: dict[str, Any],
) -> tuple[str, str]:
    """Describe the exact native approval result without reclassifying it.

    The copy is derived from Codex's response enum/object, so the visible card
    cannot claim that work is continuing after a native ``cancel`` response.
    """

    decision = response.get("decision")
    if decision == "cancel":
        return "✅ AI Project Owner 已代你选择，当前轮次已停止", "stopping"
    if decision == "decline":
        return "✅ AI Project Owner 已代你选择，已拒绝本次操作并继续处理", "running"
    if isinstance(decision, dict):
        network = decision.get("applyNetworkPolicyAmendment")
        if isinstance(network, dict):
            amendment = network.get("network_policy_amendment")
            if isinstance(amendment, dict) and amendment.get("action") == "deny":
                return (
                    "✅ AI Project Owner 已代你选择，已拒绝该网络访问并继续处理",
                    "running",
                )
    if "permissions" in response and not response.get("permissions"):
        return "✅ AI Project Owner 已代你选择，已拒绝额外权限并继续处理", "running"
    return "✅ AI Project Owner 已代你选择，已允许并继续处理", "running"


def register_root_service_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Bind long-lived root turns to the FastAPI lifespan loop."""

    global _SERVICE_LOOP
    with _SERVICE_LOOP_GUARD:
        _SERVICE_LOOP = loop


def _owner_human_context(
    history_round: Any | None,
    parent_decision: ProjectDecision | None,
    *,
    fallback_text: str = "",
) -> list[dict[str, Any]]:
    """Return the human words an Owner decision is proxying, newest first."""

    inherited = (
        (parent_decision.answer_payload or {}).get("human_context")
        if parent_decision is not None
        else []
    )
    context: list[dict[str, Any]] = [
        dict(item) for item in inherited if isinstance(item, dict)
    ] if isinstance(inherited, list) else []
    if history_round is not None:
        for index, item in enumerate(history_round.user_instructions or [], start=1):
            if not isinstance(item, dict) or not str(
                item.get("origin") or ""
            ).startswith("human"):
                continue
            value = {
                key: item[key]
                for key in ("text", "origin", "time")
                if item.get(key)
            }
            value["source_ref"] = f"{history_round.id}:instruction:{index}"
            if isinstance(item.get("reply_to"), dict) and item["reply_to"]:
                value["reply_to"] = dict(item["reply_to"])
            context.append(value)
    elif fallback_text.strip() and not context:
        context.append({"text": fallback_text.strip(), "origin": "human"})

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in reversed(context):
        key = (
            str(item.get("source_ref") or ""),
            str(item.get("time") or ""),
            str(item.get("text") or ""),
        )
        if not key[2] or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:12]


def _render_owner_human_context(context: list[dict[str, Any]]) -> str:
    """Render exact human context without turning it into another abstraction."""

    rows: list[str] = []
    for item in context:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        row = f"- 用户原话：{text}"
        reply_to = item.get("reply_to")
        if isinstance(reply_to, dict):
            reply = str(
                reply_to.get("request")
                or reply_to.get("result")
                or reply_to.get("summary")
                or ""
            ).strip()
            if reply:
                row += f"\n  回复对象：{reply}"
        rows.append(row)
    return "\n".join(rows)


def latest_project_human_context(
    service: ProjectService,
    project_id: str,
    *,
    parent_decision_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recover the latest exact human context for continuation/restart."""

    parent_decision = (
        service.get_project_decision(parent_decision_id)
        if parent_decision_id
        else None
    )
    context: list[dict[str, Any]] = []
    human_rounds = [
        row
        for row in service.get_project_history_rounds(project_id)
        if any(
            isinstance(item, dict)
            and str(item.get("origin") or "").startswith("human")
            for item in (row.user_instructions or [])
        )
    ]
    for history_round in reversed(human_rounds[-5:]):
        context.extend(_owner_human_context(history_round, None))
    context.extend(_owner_human_context(None, parent_decision))

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in context:
        key = (
            str(item.get("source_ref") or ""),
            str(item.get("time") or ""),
            str(item.get("text") or ""),
        )
        if not key[2] or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:12]


def _newer_external_human_round(
    service: ProjectService,
    project_id: str,
    *,
    after: datetime,
    current_round_id: str | None = None,
) -> Any | None:
    """Return newer human authority that the reviewed round does not contain.

    A human steer recorded on the reviewed round is part of that round.  A
    separate human round after the decision/review boundary supersedes an
    older Owner continuation, even when that continuation finishes later.
    This is timeline ordering, not a second workflow state machine.
    """

    def local_naive(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value

    boundary = local_naive(after)
    for row in service.get_project_history_rounds(
        project_id, newest_first=True
    ):
        if current_round_id and row.id == current_round_id:
            continue
        for item in row.user_instructions or []:
            if not isinstance(item, dict) or not str(
                item.get("origin") or ""
            ).startswith("human"):
                continue
            observed_at = row.started_at
            if item.get("time"):
                try:
                    observed_at = datetime.fromisoformat(str(item["time"]))
                except ValueError:
                    pass
            if local_naive(observed_at) > boundary:
                return row
    return None


def build_restart_recovery_trigger(
    user_request: str,
    human_context: list[dict[str, Any]],
) -> str:
    """Build restart continuation without replacing humans with Owner prose."""

    trigger = (
        "MAC 服务重启中断了上一轮执行。请在同一个项目会话中先检查"
        "已经完成的工作、当前工作区和公开状态，然后继续完成用户上一轮请求。"
        "不要重复已经完成的不可逆动作；状态不确定时先只读检查。\n\n"
        f"用户上一轮请求：\n{user_request}"
    )
    rendered = _render_owner_human_context(human_context)
    if rendered:
        trigger += "\n\n用户原始上下文（优先于上述 Owner 转述）：\n" + rendered
    return trigger


def schedule_root_turn(
    bind: Any,
    runtime: AgentRuntime,
    settings: Settings,
    project_id: str,
    trigger: str,
    *,
    actor_open_id: str | None = None,
    visible_request: str | None = None,
    request_origin: str = "human",
    parent_decision_id: str | None = None,
    reply_context: dict[str, Any] | None = None,
) -> None:
    """Submit a root turn to the application loop and return immediately."""

    scheduled_monotonic = monotonic()
    with _ROOT_THREADS_GUARD:
        if project_id in _PROJECT_RESETTING:
            raise ProjectResetError("项目正在重置，请完成后重新发送。")
        scheduled_epoch = _PROJECT_RESET_EPOCH.get(project_id, 0)

    async def execute_inner() -> None:
        bridge = None
        try:
            with Session(bind) as session:
                project = ProjectService(session).require_project(project_id)
                from mac_system.feishu.accounts import create_feishu_bridge

                bridge = create_feishu_bridge(settings, project.bot_account_id)
                engine = RootAgentEngine(
                    session,
                    runtime,
                    settings,
                    bot_account_id=project.bot_account_id,
                    feishu_bridge=bridge,
                )
                await engine.run(
                    project_id,
                    trigger,
                    actor_open_id=actor_open_id,
                    scheduled_monotonic=scheduled_monotonic,
                    visible_request=visible_request,
                    request_origin=request_origin,
                    parent_decision_id=parent_decision_id,
                    reply_context=reply_context,
                )
        finally:
            if bridge is not None:
                await bridge.close()

    async def execute() -> None:
        try:
            async with _project_work(project_id, scheduled_epoch):
                await execute_inner()
        except ProjectResetError:
            if request_origin == "human":
                with Session(bind) as session:
                    service = ProjectService(session)
                    project = service.require_project(project_id)
                    service.enqueue_notification(
                        dedupe_key=generate_id("reset-cancelled-input"),
                        recipient_id=project.feishu_chat_id or actor_open_id or project.id,
                        recipient_type="chat_id" if project.feishu_chat_id else "open_id",
                        intent_type="interaction", project_id=project_id,
                        bot_account_id=project.bot_account_id,
                        payload={"card": build_content_card(
                            "消息未执行", "这条排队消息已被 /reset 取消；如仍需执行，请在重置成功后重新发送。", color="orange"
                        )},
                    )

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


def schedule_project_owner_review(
    bind: Any,
    runtime: AgentRuntime,
    settings: Settings,
    review_id: str,
) -> None:
    """Run durable Owner post-processing without holding the project lock."""

    with Session(bind) as lookup:
        review = lookup.get(ProjectOwnerReview, review_id)
        if review is None:
            return
        project_id = review.project_id
    with _ROOT_THREADS_GUARD:
        if project_id in _PROJECT_RESETTING:
            return
        scheduled_epoch = _PROJECT_RESET_EPOCH.get(project_id, 0)

    async def execute_inner() -> None:
        bridge = None
        try:
            with Session(bind) as session:
                service = ProjectService(session)
                review = service.mark_project_owner_review_running(review_id)
                if review is None:
                    return
                payload = dict(review.input_payload or {})
                project = service.get_project(review.project_id)
                if project is None:
                    service.fail_project_owner_review(
                        review.id, "Project no longer exists"
                    )
                    return
                if (project.context or {}).get("runtime_reset_required"):
                    service.supersede_project_owner_review(review.id)
                    return
                from mac_system.feishu.accounts import create_feishu_bridge

                bridge = create_feishu_bridge(settings, project.bot_account_id)
                engine = RootAgentEngine(
                    session,
                    runtime,
                    settings,
                    bot_account_id=project.bot_account_id,
                    feishu_bridge=bridge,
                )
                if not service.is_project_owner_enabled(project.id):
                    superseded = service.supersede_project_owner_review(review.id)
                    if superseded is not None:
                        await engine._sync_report_owner_review_card(
                            project, superseded
                        )
                    return
                try:
                    await engine._review_delivered_report(
                        project,
                        turn_id=str(review.native_turn_id or ""),
                        title=str(payload.get("title") or "本轮结果"),
                        content=str(payload.get("content") or ""),
                        user_request=str(payload.get("user_request") or ""),
                        request_origin=str(
                            payload.get("request_origin") or review.request_origin or "human"
                        ),
                        parent_decision_id=(
                            str(payload.get("parent_decision_id"))
                            if payload.get("parent_decision_id")
                            else review.parent_decision_id
                        ),
                        history_round_id=(
                            str(payload.get("history_round_id"))
                            if payload.get("history_round_id")
                            else None
                        ),
                        review_id=review.id,
                        request_item_id=review.request_item_id,
                    )
                except Exception as exc:
                    session.rollback()
                    failed = service.fail_project_owner_review(review.id, str(exc))
                    if failed is not None:
                        try:
                            await engine._sync_report_owner_review_card(
                                project, failed
                            )
                        except Exception:
                            logger.warning(
                                "Could not expose failed Project Owner review %s",
                                review.id,
                                exc_info=True,
                            )
                    raise
        finally:
            if bridge is not None:
                await bridge.close()

    async def execute() -> None:
        async with _project_work(project_id, scheduled_epoch):
            await execute_inner()

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
    with _ROOT_TURN_TASKS_GUARD:
        # Startup recovery and an outbox acknowledgement retry can both
        # dispatch this durable job. The account process owns one invocation,
        # including while its database status has not yet changed to running.
        existing = _OWNER_REVIEW_TASKS.get(review_id)
        if existing is not None and not existing.done():
            return
        if target is current:
            task: asyncio.Task[Any] | ConcurrentFuture[Any] = asyncio.create_task(
                execute(), name=f"mac-owner-review-{review_id}"
            )
        else:
            task = asyncio.run_coroutine_threadsafe(execute(), target)
        _OWNER_REVIEW_TASKS[review_id] = task
        _ROOT_TURN_TASKS.add(task)

    def finished(done: asyncio.Task[Any] | ConcurrentFuture[Any]) -> None:
        with _ROOT_TURN_TASKS_GUARD:
            _ROOT_TURN_TASKS.discard(done)
            if _OWNER_REVIEW_TASKS.get(review_id) is done:
                _OWNER_REVIEW_TASKS.pop(review_id, None)
        if not done.cancelled() and done.exception() is not None:
            logger.error(
                "Background Project Owner review failed for %s: %s",
                review_id,
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
        "route by keywords or silently fall back. When the current attempt fails "
        "or is blocked, inspect the returned evidence before choosing another "
        "provider: do not repeat a blocker shared by every agent. If an independent "
        "provider can plausibly add value, call one deliberately with a minimal "
        "sufficient task package containing the relevant failure evidence and "
        "acceptance criteria. Do not try every provider or build a fallback chain.",
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
                "agentThreadId",
                "agentPath",
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
    event_kind: str, item: dict[str, Any], provider: str = "Codex"
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
        # Private reasoning lifecycle is protocol noise, not a useful user
        # step. Public commentary remains the source for meaningful progress.
        return None

    if item_type == "plan":
        if started:
            return "正在整理执行步骤", "purple", "running"
        text = str(item.get("text") or "").strip()
        label = f"执行计划：{text[:600]}" if text else "执行计划已更新"
        return label, "blue", "running"

    labels = {
        "commandExecution": (
            "正在检查或验证项目",
            f"已完成一次检查或验证，{provider} 继续处理",
            f"一次检查或验证未通过，{provider} 正在分析",
        ),
        "fileChange": (
            "正在修改项目文件",
            f"项目文件已更新，{provider} 继续处理",
            f"文件修改未完成，{provider} 正在分析",
        ),
        "dynamicToolCall": (
            "正在调用外部编码能力",
            f"外部编码能力已返回，{provider} 继续处理",
            f"外部编码能力未完成，{provider} 正在分析",
        ),
        "nativeToolCall": (
            "正在使用项目工具",
            f"工具已返回结果，{provider} 继续处理",
            f"工具调用未完成，{provider} 正在分析",
        ),
        "mcpToolCall": (
            "正在使用已连接的工具",
            f"工具已返回结果，{provider} 继续处理",
            f"工具调用未完成，{provider} 正在分析",
        ),
        "webSearch": (
            "正在查找所需资料",
            f"资料查找完成，{provider} 继续处理",
            f"资料查找未完成，{provider} 正在分析",
        ),
        "collabAgentToolCall": (
            "子 Agent 正在处理",
            f"子 Agent 已返回，{provider} 继续处理",
            f"子 Agent 未完成任务，{provider} 正在分析",
        ),
        "subAgentActivity": (
            "子 Agent 正在处理",
            f"子 Agent 已返回，{provider} 继续处理",
            f"子 Agent 未完成任务，{provider} 正在分析",
        ),
        "contextCompaction": (
            "正在整理较长会话的上下文",
            f"上下文已整理，{provider} 继续处理",
            f"上下文整理未完成，{provider} 正在分析",
        ),
        "imageView": (
            "正在查看项目图片",
            f"图片已查看，{provider} 继续处理",
            f"图片查看未完成，{provider} 正在分析",
        ),
        "imageGeneration": (
            "正在生成图片",
            f"图片已生成，{provider} 继续处理",
            f"图片生成未完成，{provider} 正在分析",
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
    """Describe the active native tool with a short, redacted summary."""

    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        cwd = str(item.get("cwd") or "").strip()
        raw_command = redact_text(str(item.get("command") or "")).replace(
            "\n", " ⏎ "
        )
        command = _progress_text(raw_command, 180)
        parts = [f"命令：{command}" if command else "工具：终端命令"]
        if cwd:
            parts.append(f"目录：{Path(cwd).name}")
        return "；".join(parts)
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
    if item_type == "nativeToolCall":
        return "工具：" + _progress_text(str(item.get("tool") or "项目工具"), 100)
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or "").strip()
        name = "/".join(part for part in (server, tool) if part)
        return f"工具：{name}" if name else "工具：已连接能力"
    if item_type == "dynamicToolCall":
        # Dynamic tool identifiers are protocol/API names (for example
        # ``run_external_agent``), not useful mobile UI copy. The provider,
        # model and selection reason are already projected as a real step.
        return "工具：外部 Coding-Agent"
    if item_type in {"collabAgentToolCall", "subAgentActivity"}:
        tool = str(item.get("tool") or item.get("kind") or "").strip()
        return f"子 Agent 操作：{tool}" if tool else "子 Agent 正在执行任务"
    if item_type == "imageView":
        path = str(item.get("path") or "").strip()
        return f"图片：{Path(path).name}" if path else "工具：图片查看"
    return ""


def _native_status_card(
    event_kind: str, payload: dict[str, Any], provider: str = "Codex"
) -> tuple[str, str, str] | None:
    """Translate official non-item turn status into the shared progress card."""

    if event_kind == "turn/started":
        return f"{provider} 已开始处理本轮消息。", "purple", "running"
    if event_kind != "error":
        return None
    error = payload.get("error")
    if (
        isinstance(error, dict)
        and error.get("codexErrorInfo") == "serverOverloaded"
    ):
        return "所选模型暂时容量不足，正在等待同模型自动续跑。", "orange", "running"
    if payload.get("willRetry") is True:
        return f"与 {provider} 的连接暂时中断，正在自动重连。", "orange", "running"
    return f"{provider} 本轮遇到错误，正在结束本轮。", "red", "running"


def _turn_completion_card(status: str) -> tuple[str, str, str]:
    """Map the official turn terminal status without inventing a workflow state."""

    if status == "completed":
        return "本轮处理完成，项目可继续迭代。", "green", "completed"
    if status == "interrupted":
        return "本轮已中止，项目可继续发送新消息。", "orange", "interrupted"
    return "本轮未完成，项目可直接重试。", "red", "failed"


def _turn_failure_detail(result: TurnResult) -> str | None:
    """Return the provider's terminal error without classifying the failure.

    ``turn/completed`` is the authoritative lifecycle event, while some app-
    server versions also emit a preceding ``error`` notification.  Read both
    protocol shapes so the mobile projection does not replace a useful native
    error with the generic phrase "本轮未完成".
    """

    for event in reversed(result.events):
        error: Any = None
        if event.kind == "turn/completed":
            turn = event.payload.get("turn")
            if isinstance(turn, dict):
                error = turn.get("error")
        elif event.kind == "error":
            error = event.payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("codexErrorInfo")
        detail = _compact_text(redact_text(str(error or "")), limit=1_200)
        if detail:
            return detail
    return None


class RootAgentEngine:
    """Map one MAC project to one persistent Project Lead thread."""

    def __init__(
        self,
        session: Session,
        runtime: AgentRuntime,
        settings: Settings,
        *,
        bot_account_id: str = "default",
        feishu_bridge: FeishuBridge | None = None,
        lead_runtime_registry: ProjectLeadRuntimeRegistry | None = None,
    ) -> None:
        # ``runtime`` hosts optional external coding-agent tool calls. The
        # Project Lead runtime separately owns the project conversation.
        self.session = session
        self.runtime = runtime
        self.settings = settings
        self.feishu_bridge = feishu_bridge
        self.bind = session.get_bind()
        self.service = ProjectService(session, bot_account_id=bot_account_id)
        self.store = RootSessionStore(
            session, default_provider=settings.project_lead_provider
        )
        self.project: Project | None = None
        self._progress: dict[str, _ProgressState] = {}
        self._progress_delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_progress_delivery: dict[str, str] = {}
        self._progress_last_delivery: dict[str, float] = {}
        self._progress_flush_events: dict[str, asyncio.Event] = {}
        self._progress_min_delivery_interval = 1.5 if feishu_bridge is not None else 0.0
        self._turn_timings: dict[str, _TurnTiming] = {}
        self._agent_config = load_agent_config(settings.agent_config_path)
        self.lead_runtime_registry = lead_runtime_registry or ProjectLeadRuntimeRegistry(
            {"codex": self._create_codex_lead_runtime,
             "kimi_code": self._create_kimi_lead_runtime}
        )

    async def set_lead_provider(
        self, project_id: str, provider: str
    ) -> tuple[str, str]:
        """Apply an explicit provider switch only at an idle native boundary."""

        if provider not in {"codex", "kimi_code"}:
            raise RuntimeError("该助手尚未接入项目主会话，不能安全切换。")
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            self.session.expire_all()
            durable = self.store.ensure(project_id)
            if durable.status != "idle" or durable.active_turn_id is not None:
                raise RuntimeError("当前项目仍在处理消息，请等本轮结束后再切换助手。")
            if durable.provider == provider:
                return provider, provider
            with _ROOT_THREADS_GUARD:
                handle = _ROOT_THREADS.get(project_id)
            if handle is not None:
                await self._release_root(project_id, handle.runtime)
            return self.store.switch_provider(project_id, provider)

    async def run(self, project_id: str, trigger: str, **kwargs: Any) -> TurnResult:
        async with _project_work(project_id):
            return await self._run(project_id, trigger, **kwargs)

    async def _run(
        self,
        project_id: str,
        trigger: str,
        *,
        actor_open_id: str | None = None,
        scheduled_monotonic: float | None = None,
        visible_request: str | None = None,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> TurnResult:
        project = self.service.require_project(project_id)
        if project.bot_account_id != self.service.bot_account_id:
            self.service.bot_account_id = project.bot_account_id
        self.project = project
        if (project.context or {}).get("runtime_reset_required"):
            raise ProjectResetError("上次 /reset 尚未确认清理成功，请先重新执行 /reset。")
        current_user_request = (
            visible_request if visible_request is not None else trigger
        )
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            run_started = monotonic()
            request_scheduled_at = scheduled_monotonic or run_started
            self._turn_timings[project_id] = _TurnTiming(
                scheduled=request_scheduled_at,
                run_started=run_started,
            )
            self.session.expire_all()
            project = self.service.require_project(project_id)
            root: ProjectLeadRuntime | None = None
            heartbeat: asyncio.Task[None] | None = None
            native_turn_completed = False
            try:
                if request_origin == "human_aligned":
                    settled_alignment = []
                    for decision in self.service.get_project_decisions(
                        project.id, limit=100
                    ):
                        payload = decision.answer_payload or {}
                        if (
                            decision.decision_type == "alignment"
                            and decision.status == "resolved_by_user"
                            and str(payload.get("source_request") or "").strip()
                            == str(current_user_request or "").strip()
                            and decision.user_override
                        ):
                            settled_alignment.append(decision)
                    if settled_alignment:
                        settled_alignment.sort(key=lambda item: item.updated_at)
                        settled_text = "\n".join(
                            f"- {item.decision} → 用户选择：{item.user_override}"
                            for item in settled_alignment
                        )
                        trigger = (
                            f"{trigger}\n\n[以下是本次执行前多轮对齐中用户已经明确"
                            "选择的全部结果边界。它们共同生效，不得只保留最后一项：]\n"
                            f"{settled_text}"
                        )
                self.store.begin_turn(
                    project_id,
                    user_request=current_user_request,
                    actor_open_id=actor_open_id,
                    request_origin=request_origin,
                    parent_decision_id=parent_decision_id,
                    reply_context=reply_context,
                )
                current = monotonic()
                provider_name = "Kimi" if self.store.ensure(project_id).provider == "kimi_code" else "Codex"
                progress_state = _ProgressState(
                    started_monotonic=current,
                    last_event_monotonic=current,
                    last_event_at=datetime.now().astimezone(),
                    label="正在准备项目上下文",
                    root_provider_name=provider_name,
                    executor=f"{provider_name} Root",
                )
                # Short native Codex turns may answer directly without a
                # commentary or tool event. Seed one truthful big step from
                # the already-visible user request so the progress card does
                # not depend on commentary being emitted by chance.
                _start_progress_big_step(
                    progress_state,
                    current_user_request,
                    executor=f"{provider_name} Root",
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
                root, result = await self._run_with_failover(project, trigger, progress_state)
                native_turn_completed = True
                timing = self._turn_timings[project_id]
                timing.mark_once("turn_completed")
                progress_state.root_usage = timing.turn_token_usage()
                await self._stop_progress_heartbeat(heartbeat)
                heartbeat = None
                await self._flush_progress_snapshots(project_id)
                self.store.set_turn(project_id, turn_id=None, status="idle")
                if result.status in {"interrupted", "declined"}:
                    self.store.finish_active_history_round(
                        project_id, status=result.status
                    )
                try:
                    await self._publish_turn_status(
                        project,
                        result,
                        user_request=current_user_request,
                        request_origin=request_origin,
                        parent_decision_id=parent_decision_id,
                    )
                except Exception:
                    logger.warning(
                        "Root completion projection failed for %s",
                        project_id,
                        exc_info=True,
                    )
                if result.status == "failed":
                    # ``turn/completed`` with ``status=failed`` is a terminal
                    # native boundary.  The failure card is useful UI, but it
                    # must not be the only thing that closes the durable
                    # project round.  Keep this fallback after projection so a
                    # card/database error cannot leave ``idle + pending``.
                    self.store.finish_active_history_round(
                        project_id, status="failed"
                    )
                if result.final_message:
                    try:
                        await self._publish_final(
                            project,
                            result,
                            visible_request=current_user_request,
                            request_origin=request_origin,
                            parent_decision_id=parent_decision_id,
                        )
                    except Exception:
                        logger.warning(
                            "Root final projection failed for %s",
                            project_id,
                            exc_info=True,
                        )
                timing.mark_once("final_card")
                timing.mark_once("postprocess_completed")
                self._persist_turn_timing(project_id, result.status, result.turn_id)
                return result
            except BaseException as exc:
                await self._stop_progress_heartbeat(heartbeat)
                heartbeat = None
                if isinstance(exc, asyncio.CancelledError):
                    await self._cancel_progress_snapshots(project_id)
                else:
                    await self._flush_progress_snapshots(project_id)
                if root is None:
                    with _ROOT_THREADS_GUARD:
                        handle = _ROOT_THREADS.get(project_id)
                    if handle is not None:
                        root = handle.runtime
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
                if native_turn_completed:
                    # Result delivery and Owner review are post-processing.
                    # A shutdown here must not replay a Codex turn that already
                    # completed and may already have changed the workspace.
                    try:
                        self.store.set_turn(
                            project_id, turn_id=None, status="idle", error=None
                        )
                    except Exception:
                        logger.warning(
                            "Failed to keep completed Root turn idle during shutdown",
                            exc_info=True,
                        )
                else:
                    self._recover_failed_turn(project_id, reported_exc)
                    if isinstance(exc, Exception):
                        await self._publish_failure_safely(
                            project_id,
                            reported_exc,
                            user_request=current_user_request,
                            request_origin=request_origin,
                            parent_decision_id=parent_decision_id,
                        )
                timing = self._turn_timings.get(project_id)
                if timing is not None:
                    timing.mark_once("turn_completed")
                    timing.mark_once("final_card")
                    timing.mark_once("postprocess_completed")
                    self._persist_turn_timing(project_id, "failed", None)
                raise
            finally:
                await self._stop_progress_heartbeat(heartbeat)
                self._progress.pop(project_id, None)
                self._turn_timings.pop(project_id, None)

    async def steer(self, project_id: str, text: str, **kwargs: Any) -> bool:
        async with _project_work(project_id):
            return await self._steer(project_id, text, **kwargs)

    async def _steer(
        self,
        project_id: str,
        text: str,
        *,
        actor_open_id: str | None = None,
        visible_request: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> bool:
        if project_id in _PROJECT_RESETTING or (
            self.service.require_project(project_id).context or {}
        ).get("runtime_reset_required"):
            raise ProjectResetError("项目正在重置或清理尚未确认，请 /reset 成功后重新发送。")
        row = self.store.ensure(project_id)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        if (
            handle is None
            or row.status not in {"running", "owner_reviewing", "waiting_user"}
        ):
            return False
        current = asyncio.get_running_loop()
        active_turn_id = row.active_turn_id

        async def apply_steer() -> None:
            await handle.runtime.steer(active_turn_id, text)
            if handle.engine is not None:
                handle.engine._record_steered_message(
                    project_id,
                    visible_request if visible_request is not None else text,
                    actor_open_id=actor_open_id,
                    reply_context=reply_context,
                )

        try:
            if handle.loop is current:
                await apply_steer()
            elif handle.loop.is_running():
                pending = asyncio.run_coroutine_threadsafe(
                    apply_steer(), handle.loop
                )
                await asyncio.wrap_future(pending)
            else:
                return False
        except ProjectLeadRuntimeError:
            # Durable state can briefly lag the native turn boundary. Preserve
            # the user's message by letting the caller enqueue it as the next
            # turn instead of dropping the Feishu event.
            logger.warning(
                "Native steer rejected for %s; queueing input as a new turn",
                project_id,
                exc_info=True,
            )
            return False
        return True

    async def rotate_progress_for_timeline_event(self, project_id: str) -> bool:
        """Move live progress onto a new chronological card segment.

        Provider turn ownership and Feishu chronology are independent.  A
        newer human-visible event freezes the previous card even when the
        provider will accept the message through ``turn/steer`` in the same
        native turn.
        """

        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        target = (
            handle.engine
            if handle is not None and handle.engine is not None
            else self
        )

        async def rotate() -> bool:
            return await target._rotate_progress_for_timeline_event(project_id)

        current = asyncio.get_running_loop()
        if handle is None or handle.loop is current:
            return await rotate()
        if not handle.loop.is_running():
            return False
        pending = asyncio.run_coroutine_threadsafe(rotate(), handle.loop)
        return await asyncio.wrap_future(pending)

    def _record_steered_message(
        self,
        project_id: str,
        visible_request: str,
        *,
        actor_open_id: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> None:
        """Expose an accepted native steer on the owning turn's progress card.

        The user message is already durable in the provider thread.  This is a
        presentation projection only: it does not create a second turn or a
        receipt card.  A pending question suspends the progress projection, so
        its newer question card is updated by the Feishu adapter instead.
        """

        self.store.append_history_instruction(
            project_id,
            visible_request,
            actor_open_id=actor_open_id,
            reply_context=reply_context,
        )
        state = self._progress.get(project_id)
        if state is None:
            return
        state.last_event_monotonic = monotonic()
        state.last_event_at = datetime.now().astimezone()
        if state.projection_suspended:
            return
        compact = _progress_text(visible_request, 220)
        _record_progress_small_step(
            state,
            key=generate_id("user-steer"),
            label="已收到本轮补充",
            status="completed",
            detail=compact or None,
        )
        state.label = f"{state.root_provider_name} 正在结合最新补充继续处理"
        self._queue_progress_snapshot(
            project_id,
            event_identity=generate_id("root-steer"),
        )

    async def interrupt(self, project_id: str) -> bool:
        """Stop the current provider work without ending its persistent thread."""

        row = self.store.ensure(project_id)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        if handle is None:
            return False
        current = asyncio.get_running_loop()
        if handle.loop is current:
            interrupted = await handle.runtime.interrupt(row.active_turn_id)
            if interrupted:
                await handle.runtime.clean_background_terminals()
                # The native interrupt acknowledgement is the execution
                # boundary. Persist it before the Feishu handler confirms the
                # stop so the card and backend can never disagree while the
                # owning run coroutine unwinds.
                self.store.finish_active_history_round(
                    project_id, status="interrupted"
                )
                self.store.set_turn(project_id, turn_id=None, status="idle")
            return interrupted
        elif handle.loop.is_running():
            async def stop_provider_work() -> bool:
                interrupted = await handle.runtime.interrupt(row.active_turn_id)
                if interrupted:
                    await handle.runtime.clean_background_terminals()
                return interrupted

            pending = asyncio.run_coroutine_threadsafe(stop_provider_work(), handle.loop)
            interrupted = await asyncio.wrap_future(pending)
            if interrupted:
                self.store.finish_active_history_round(
                    project_id, status="interrupted"
                )
                self.store.set_turn(project_id, turn_id=None, status="idle")
            return interrupted
        else:
            return False

    async def reset_project_runtime(self, project_id: str) -> None:
        """Force-stop only this project's execution; retain native history."""
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
        with _SERVICE_LOOP_GUARD:
            target = handle.loop if handle is not None else _SERVICE_LOOP
        current = asyncio.get_running_loop()
        if target is not None and target is not current:
            if not target.is_running():
                raise RuntimeError("执行线程不可用，无法确认清理；未宣称恢复成功。")
            pending = asyncio.run_coroutine_threadsafe(
                self._reset_on_owner_loop(project_id), target
            )
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(pending)), 75)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("重置执行线程未及时响应；清理结果尚未确认，请勿认为已经恢复。") from exc
        else:
            await self._reset_on_owner_loop(project_id)

    async def _reset_on_owner_loop(self, project_id: str) -> None:
        async def execute() -> None:
            # The command and owning execution may use different loops and
            # SQLAlchemy Sessions. Never move the handler Session across them.
            with Session(self.bind) as session:
                engine = RootAgentEngine(
                    session, self.runtime, self.settings,
                    bot_account_id=self.service.bot_account_id,
                )
                await engine._force_reset(project_id)

        task = _PROJECT_RESET_TASKS.get(project_id)
        if task is None or task.done():
            task = asyncio.create_task(execute(), name=f"mac-reset-{project_id}")
            _PROJECT_RESET_TASKS[project_id] = task
            # Retrieve failures even if a disconnected command caller stops
            # waiting. Keep cleanup alive independently of message transport.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        await asyncio.shield(task)

    async def _force_reset(self, project_id: str) -> None:
        current = asyncio.get_running_loop()
        project = self.service.require_project(project_id)
        previous_status = self.store.ensure(project_id).status
        old_witness = (project.context or {}).get("runtime_reset_host_witness")
        with _ROOT_THREADS_GUARD:
            _PROJECT_RESETTING.add(project_id)
            _PROJECT_RESET_EPOCH[project_id] = _PROJECT_RESET_EPOCH.get(project_id, 0) + 1
            handle = _ROOT_THREADS.get(project_id)
            work = list(_PROJECT_WORK.get(project_id, ()))
        try:
            project.context = {
                **(project.context or {}), "runtime_reset_required": True,
                "runtime_reset_at": _now().isoformat(),
            }
            self.session.add(project)
            self.session.commit()
            # Retain the original witness across retries. Replacing it with
            # the new worker's PID would prevent post-restart recovery.
            witness = old_witness or snapshot_host_execution()
            project.context = {**project.context, "runtime_reset_host_witness": witness}
            self.session.add(project)
            self.session.commit()
            if handle is None and not work and previous_status in {"running", "waiting_user", "owner_reviewing", "resetting"}:
                if not old_witness or not host_execution_has_exited(old_witness):
                    raise RuntimeError("没有执行归属记录，且旧宿主或其执行仍未确认退出；不能安全清理。请先由维护者核查旧进程，再 /reset。")
            # Do not acquire the project lock or depend on a JSON-RPC reply:
            # both may be exactly what is wedged. Cancellation releases host
            # waits; close uses the owned OS process group, not a name search.
            waits = set()
            for task in work:
                if task.get_loop() is current:
                    task.cancel()
                    waits.add(task)
                    continue
                # Feishu handlers own short-lived loops in other threads.
                # Register completion on that loop before cancellation, so
                # its run_until_complete may close immediately afterwards.
                # Awaiting a second coroutine there would race loop shutdown.
                stopped: ConcurrentFuture = ConcurrentFuture()

                def cancel_and_observe(target=task, result=stopped) -> None:
                    def completed(done) -> None:
                        error = None if done.cancelled() else done.exception()
                        if error is None:
                            result.set_result(None)
                        else:
                            result.set_exception(error)

                    if target.done():
                        completed(target)
                    else:
                        target.add_done_callback(completed)
                        target.cancel()

                if task.done():
                    cancel_and_observe()
                elif not task.get_loop().is_running():
                    stopped.set_exception(RuntimeError("旧消息执行线程不可用，无法确认退出。"))
                else:
                    try:
                        task.get_loop().call_soon_threadsafe(cancel_and_observe)
                    except RuntimeError as exc:
                        stopped.set_exception(exc)
                waits.add(asyncio.wrap_future(stopped))
            cleanup = (
                asyncio.create_task(self._release_root(project_id, handle.runtime))
                if handle is not None else None
            )
            if cleanup is not None:
                waits.add(cleanup)
            if waits:
                done, pending = await asyncio.wait(waits, timeout=_RESET_WAIT_SECONDS)
                if pending:
                    for task in pending:
                        task.add_done_callback(lambda item: None if item.cancelled() else item.exception())
                    raise RuntimeError("旧执行或进程清理超时，尚未确认停止；项目保持隔离，可再次 /reset。")
                if cleanup is not None:
                    cleanup.result()  # Failure must keep the project fenced.
                for task in done:
                    if not task.cancelled():
                        error = task.exception()
                        if error is not None:
                            raise RuntimeError(f"旧执行清理失败：{error}") from error

            self.session.expire_all()
            self.service.expire_pending_question_interactions(
                project_id, resolution_source="project_reset"
            )
            for interaction in self.session.exec(select(QuestionInteraction).where(
                QuestionInteraction.project_id == project_id,
                QuestionInteraction.status == "resolving",
            )).all():
                interaction.status = "expired"
                interaction.resolution_source = "project_reset"
                self.session.add(interaction)
            for review in self.session.exec(select(ProjectOwnerReview).where(
                ProjectOwnerReview.project_id == project_id,
                ProjectOwnerReview.status.in_(("pending", "running")),
            )).all():
                self.service.supersede_project_owner_review(review.id)
            self.store.finish_active_history_round(project_id, status="interrupted")
            row = self.store.set_turn(project_id, turn_id=None, status="idle")
            row.restart_recovery_pending = False
            self.session.add(row)
            project = self.service.require_project(project_id)
            context = dict(project.context or {})
            context.pop("runtime_reset_required", None)
            context.pop("runtime_reset_host_witness", None)
            project.context = context
            self.session.add(project)
            await self.service.log_event(project_id, "INFO", "Project execution reset by explicit /reset", {
                "native_thread_id": row.native_thread_id,
                "cancelled_host_tasks": len(work), "process_cleanup_confirmed": True,
            })
            self.session.commit()
            with _ROOT_THREADS_GUARD:
                if _ROOT_THREADS.get(project_id) is handle:
                    _ROOT_THREADS.pop(project_id, None)
                _PROJECT_RESET_EPOCH[project_id] += 1
                _PROJECT_RESETTING.discard(project_id)
        except BaseException as exc:
            self.session.rollback()
            self.session.expire_all()
            row = self.store.set_turn(project_id, turn_id=None, status="resetting", error=str(exc))
            row.restart_recovery_pending = False
            self.session.add(row)
            self.session.commit()
            raise

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
                handle = _ROOT_THREADS.get(project_id)
            if handle is not None:
                await self._release_root(project_id, handle.runtime)

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

    async def list_lead_models(self, project_id: str) -> list[dict[str, Any]]:
        project = self.service.require_project(project_id)
        if self.store.ensure(project_id).provider != "codex":
            raise RuntimeError("当前 /model 模型选择仅支持 Codex；不会替你切换执行助手。")
        # A separate catalog connection cannot disturb the active project
        # thread, and does not start a model turn or spend inference tokens.
        catalog = CodexAppServerThread(
            codex_bin=self.settings.codex_bin, cwd=Path(project.workspace_path),
            tools=[], startup_timeout_seconds=15, request_timeout_seconds=15,
        )
        try:
            return await asyncio.wait_for(catalog.list_models(), timeout=25)
        finally:
            await catalog.close()

    async def set_lead_model(
        self, project_id: str, model: str | None
    ) -> tuple[str | None, str | None]:
        """Persist a per-project default model (``/model`` project control).

        A native thread fixes its model at ``thread/start``, so switching the
        model detaches the current thread like an explicit ``/newthread``:
        the cached runtime is released, the durable native id is cleared, and
        the next user turn starts a fresh thread carrying the new model.
        ``model=None`` restores the built-in default.
        """

        self.session.expire_all()
        durable = self.store.ensure(project_id)
        if durable.status != "idle" or durable.active_turn_id is not None:
            raise RuntimeError("当前项目仍在处理消息，请等本轮结束后再切换模型。")
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            self.session.expire_all()
            project = self.service.require_project(project_id)
            durable = self.store.ensure(project_id)
            if durable.provider != "codex":
                raise RuntimeError(
                    "当前项目的执行助手不是 Codex，`/model` 仅对 Codex 生效；"
                    "切回 Codex 后再切换模型。"
                )
            if durable.status != "idle" or durable.active_turn_id is not None:
                raise RuntimeError("当前项目仍在处理消息，请等本轮结束后再切换模型。")

            old_model = (project.context or {}).get("lead_model") or None
            if (model or _DEFAULT_LEAD_MODEL) == (old_model or _DEFAULT_LEAD_MODEL):
                # Already the effective choice: keep the current conversation
                # and native thread intact instead of detaching them.
                return old_model, model
            old_thread_id = durable.native_thread_id
            with _ROOT_THREADS_GUARD:
                handle = _ROOT_THREADS.get(project_id)
            if handle is not None:
                await self._release_root(project_id, handle.runtime)

            self.store.clear_native_thread(project_id)
            next_context = dict(project.context or {})
            if model is None:
                next_context.pop("lead_model", None)
            else:
                next_context["lead_model"] = model
            project.context = {
                **next_context,
                "root_thread_reset_pending": True,
            }
            project.updated_at = _now()
            self.session.add(project)
            await self.service.log_event(
                project_id,
                "INFO",
                "Project default model changed by explicit user command",
                {
                    "old_model": old_model,
                    "new_model": model,
                    "old_thread_id": old_thread_id,
                    "new_thread_id": None,
                    "replacement_pending": True,
                },
            )
            self.session.commit()
            return old_model, model

    async def _run_with_failover(self, project, trigger, progress_state):
        """Host-visible, one-way takeover after native recovery is exhausted.

        Only runtime failures cross this boundary. Delivery/Owner/DB errors
        after a successful turn can never cause a second execution.
        """
        durable = self.store.ensure(project.id)
        handoff = durable.provider_handoff or {}
        if handoff.get("to_provider") != durable.provider:
            handoff = {}
        if handoff.get("status") == "pending":
            await self._publish_handoff(project, handoff)
            trigger = handoff["public_context"] + "\n\n当前续接请求：\n" + trigger

        async def execute(text):
            provider = self.store.ensure(project.id).provider
            provider_name = "Kimi" if provider == "kimi_code" else "Codex"
            if progress_state.root_provider_name != provider_name:
                timing = self._turn_timings[project.id]
                progress_state.external_usage["previous-root"] = (
                    f"{progress_state.root_provider_name} Root", timing.turn_token_usage(),
                )
                timing.token_baseline = timing.token_latest = None
                progress_state.root_usage = {}
                progress_state.root_provider_name = provider_name
                progress_state.executor = f"{provider_name} Root"
                _start_progress_big_step(progress_state, "故障后接续任务", executor=progress_state.executor)
            root = await self._root_thread(project)
            timing = self._turn_timings[project.id]
            timing.thread_ready = monotonic()
            timing.root_thread_id = root.thread_id
            if self.store.ensure(project.id).provider == "codex":
                timing.token_baseline = self._latest_token_totals(project.id, root.thread_id)
            active_model = getattr(root, "active_model", None)
            progress_state.root_model = active_model
            progress_state.model = active_model
            return root, await root.run_turn(text)

        reason = None
        try:
            root, result = await execute(trigger)
        except (ProjectLeadRuntimeError, OSError) as exc:
            if not self._can_failover(project.id):
                raise
            reason = str(exc)
        else:
            # Retain the adapter's bounded native retries and its successful
            # result. Interrupted/declined turns and normal task failures stay
            # with their provider; only structured infrastructure errors move.
            if result.status == "failed" and self._can_failover(project.id) and _turn_error_code(result) in {
                "serverOverloaded", "responseStreamDisconnected", "responseStreamConnectionFailed",
                "httpConnectionFailed", "unauthorized", "usageLimitExceeded", "internalServerError",
                "responseTooManyFailedAttempts",
            }:
                reason = _turn_failure_detail(result) or "Codex provider unavailable"
            else:
                if handoff.get("status") == "pending":
                    self.store.finish_provider_handoff(project.id, result.status)
                return root, result

        # Closing includes the provider process tree. Failure to confirm that
        # its writers stopped aborts takeover, never starts concurrent writers.
        public = self._handoff_public_context(project, trigger)
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project.id)
        if handle is not None:
            await self._release_root(project.id, handle.runtime)
        durable = self.store.prepare_provider_handoff(
            project.id, provider="kimi_code", thread_id=str(uuid4()),
            reason=redact_text(reason or "Codex unavailable")[:1200], public_context=public,
        )
        await self._flush_progress_snapshots(project.id)
        await self._publish_handoff(project, durable.provider_handoff)
        self._update_progress(project.id, label="Codex 故障，Kimi 正在接续任务", detail="")
        root, result = await execute(public)
        self.store.finish_provider_handoff(project.id, result.status)
        return root, result

    def _can_failover(self, project_id):
        row = self.store.ensure(project_id)
        project = self.session.get(Project, project_id)
        execution_provider = (project.context or {}).get("execution_provider") if project else None
        return (self.settings.project_lead_failover_provider == "kimi_code"
                and not execution_provider
                and row.provider == "codex"
                and self.store.can_prepare_provider_handoff(
                    project_id, provider="kimi_code"
                ))

    def _handoff_public_context(self, project, trigger):
        # Only the current project's public management history, never native
        # rollouts, reasoning, tool output or another project's conversations.
        history = self.service.get_project_history_rounds(project.id)[-5:]
        public = [{"instructions": row.user_instructions,
                   "report": row.report_content if row.report_ref else "",
                   "report_status": row.report_status} for row in history]
        return (
            "MAC 自动接管：原 Codex 执行已停止。你在新的 Kimi 会话中接续同一项目。\n"
            "先只读核实当前工作区、已完成工作和公开状态；不得重复已完成的外部或不可逆操作。"
            "不确定某项操作是否完成时先核查，无法核查就提问。用户原话优先于旧报告和 Owner 转述。\n"
            f"工作区：{project.workspace_path}\n"
            "本项目最近公开指令及报告：\n" + json.dumps(public, ensure_ascii=False, default=str)
            + "\n当前请求：\n" + trigger
        )

    async def _publish_handoff(self, project, handoff):
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        key = f"provider-handoff:{handoff['id']}"
        await deliver_card(
            self.service, self.feishu_bridge, dedupe_key=key,
            recipient_id=project.feishu_chat_id, recipient_type="chat_id", intent_type="report",
            card=build_content_card(
                f"[{self._project_display_name(project)}] 助手自动接管",
                "Codex 未能恢复，旧执行已停止。已切换到 Kimi，正在接续本项目的公开进度。"
                "\n已有工作和原会话记录已保留；接管结果会另行汇报。\n\n原因："
                + handoff["reason"], color="orange",
            ), project_id=project.id, projection_key=key, subject_type="root_agent",
            subject_id=project.id, lifecycle_status="running", resulting_state="running",
        )

    def _create_kimi_lead_runtime(self, context):
        return KimiWireThread(
            kimi_bin=self.settings.kimi_bin, cwd=context.cwd, tools=list(context.tools),
            event_handler=context.event_handler, server_request_handler=context.server_request_handler,
            silence_timeout_seconds=self.settings.project_lead_silence_timeout_seconds,
        )

    async def _root_thread(self, project: Project) -> ProjectLeadRuntime:
        current_loop = asyncio.get_running_loop()
        extension_tools = self._extension_tools(project)
        root_profile = _root_profile_key(extension_tools)
        execution_provider = (project.context or {}).get("execution_provider")
        configured_provider = execution_provider or self.settings.project_lead_provider.strip() or "codex"
        durable = self.store.ensure(project.id, provider=configured_provider)
        provider = durable.provider
        if execution_provider and provider != execution_provider:
            raise ProjectLeadRuntimeError(
                "Project execution provider does not match its durable session; "
                "stop the previous runtime and explicitly switch the session first"
            )
        runtime_context = ProjectLeadRuntimeContext(
            cwd=Path(project.workspace_path),
            tools=tuple(extension_tools),
            event_handler=lambda event: self._on_event(project.id, event),
            server_request_handler=lambda method, params: self._on_server_request(
                project.id, method, params
            ),
            lead_model=(project.context or {}).get("lead_model") if provider == "codex" else None,
        )
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project.id)
        if handle is not None and handle.closing:
            await self._release_root(project.id, handle.runtime)
            handle = None
        root = handle.runtime if handle is not None else None
        if handle is not None and (
            handle.loop is not current_loop or handle.provider != provider
        ):
            await self._release_root(project.id, root)
            root = None

        if root is None:
            root = self.lead_runtime_registry.create(provider, runtime_context)
            with _ROOT_THREADS_GUARD:
                _ROOT_THREADS[project.id] = _RootHandle(
                    root, current_loop, provider, self
                )
        else:
            root.update_bindings(
                tools=extension_tools,
                event_handler=runtime_context.event_handler,
                server_request_handler=runtime_context.server_request_handler,
            )
            # A persistent provider transport may be reused by a later engine
            # instance.  Progress and interaction projections belong to the
            # engine that owns the current turn.
            handle.engine = self

        if root.thread_id is None:
            if durable.native_thread_id:
                # Resume the provider-owned conversation by its opaque id.
                # Provider-specific tool contracts remain inside the adapter.
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
                self.store.set_turn(project.id, turn_id=None, status="running")
                next_context = dict(project.context or {})
                next_context.pop("root_thread_reset_pending", None)
                project.context = {
                    **next_context,
                    "orchestration_mode": "root_agent",
                    "project_lead_provider": provider,
                    "root_agent_profile": root_profile,
                }
                project.updated_at = _now()
                self.session.add(project)
                self.session.commit()
        return root

    def _create_codex_lead_runtime(
        self, context: ProjectLeadRuntimeContext
    ) -> ProjectLeadRuntime:
        """Compose the default Project Lead from the Codex-native adapter."""

        return CodexAppServerThread(
            codex_bin=self.settings.codex_bin,
            silence_timeout_seconds=self.settings.project_lead_silence_timeout_seconds,
            cwd=context.cwd,
            tools=list(context.tools),
            # Do not replace or augment Codex's native workflow prompt.
            developer_instructions=None,
            event_handler=context.event_handler,
            server_request_handler=context.server_request_handler,
            # Match Codex itself: the Project Lead is a coding Agent with its
            # normal execution environment and native multi-Agent tools.
            sandbox="workspace-write",
            approval_policy="on-request",
            ephemeral=False,
            environments=None,
            thread_config={
                # A per-project ``/model`` choice overrides the built-in
                # default; the model is fixed when the native thread starts.
                "model": context.lead_model or _DEFAULT_LEAD_MODEL,
                "model_reasoning_effort": "xhigh",
                # The built-in OpenAI provider enables Responses over
                # WebSocket and cannot be overridden. This environment's
                # proxy accepts HTTPS but makes every WebSocket turn burn
                # through Codex's retry budget before falling back. Define an
                # equivalent authenticated provider through Codex's official
                # per-thread config surface and select HTTPS from the start.
                "model_provider": "mac_openai_https",
                "model_providers.mac_openai_https": {
                    "name": "OpenAI HTTPS",
                    "wire_api": "responses",
                    "requires_openai_auth": True,
                    "supports_websockets": False,
                    "stream_max_retries": 10,
                },
                "model_auto_compact_token_limit": 80_000,
                "features.multi_agent_v2": True,
                "features.default_mode_request_user_input": True,
                "sandbox_workspace_write.network_access": True,
                **_codex_role_overrides(self._agent_config),
            },
            codex_home=None,
        )

    def _extension_tools(self, project: Project) -> list[DynamicTool]:
        """Expose a stable bridge to MAC's optional coding CLI providers.

        Dynamic tools are fixed by Codex at thread/start. Availability and
        recent metrics therefore come from a live read tool rather than being
        embedded into a schema or description that would become stale.
        """

        external_agent_required = [
            "provider",
            "selection_reason",
            "task_name",
            "message",
            "access",
            "timeout_seconds",
        ]
        external_agent_properties = {
            "provider": {
                "type": "string",
                "description": (
                    "A currently available provider id returned by "
                    "list_external_agents."
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
                "maximum": 86400,
                "description": (
                    "Hard deadline for this provider call. Choose a value "
                    "that matches the bounded task and the configured limit; "
                    "the process is stopped when the deadline expires."
                ),
            },
        }
        return [
            DynamicTool(
                name="read_project_contract",
                description=(
                    "Read the current project direction with its authority status, "
                    "success criteria, durable user rules with original-source "
                    "context, and still-valid unfinished Owner "
                    "commitments. Use it when the requested outcome, scope, or "
                    "acceptance meaning may conflict with earlier decisions. It "
                    "contains no implementation plan and does not replace normal "
                    "code or repository inspection."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                handler=lambda arguments, context: self._read_project_contract(
                    project, arguments, context
                ),
            ),
            DynamicTool(
                name="list_external_agents",
                description=(
                    "List the optional external coding-agent CLIs currently "
                    "available to this MAC project, including non-secret "
                    "capability metadata and recent process metrics. Call this "
                    "before choosing run_external_agent when an external CLI "
                    "could add value."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                handler=lambda arguments, context: self._list_external_agents(
                    project, arguments, context
                ),
            ),
            DynamicTool(
                name="run_external_agent",
                description=(
                    "Run one task with an optional external coding-agent CLI. "
                    "Use list_external_agents for the live provider catalog and "
                    "selection evidence. Codex native subagents remain available "
                    "through Codex's own tools."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": external_agent_required,
                    "properties": external_agent_properties,
                },
                handler=lambda arguments, context: self._run_external_agent(
                    project, arguments, context
                ),
            ),
            DynamicTool(
                name="run_external_agents",
                description=(
                    "Run two to four independent optional external coding-agent "
                    "CLI tasks concurrently and return all results together. Use "
                    "this instead of sequential run_external_agent calls when the "
                    "tasks do not depend on one another or the user asks for parallel "
                    "execution. Root still chooses every provider and task from live "
                    "list_external_agents evidence."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agents"],
                    "properties": {
                        "agents": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": external_agent_required,
                                "properties": external_agent_properties,
                            },
                        }
                    },
                },
                handler=lambda arguments, context: self._run_external_agents(
                    project, arguments, context
                ),
            ),
        ]

    def _read_project_contract(
        self,
        project: Project,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Project existing governance records as one compact read-only view."""

        del arguments, context
        policies = [
            item
            for item in self.service.get_project_policies(project.id)
            if item.kind in {"explicit", "inferred"}
        ]
        commitments = self.service.get_open_project_commitments(project.id, limit=10)
        return {
            "project_rules": [
                {
                    "policy_ref": item.id,
                    "kind": item.kind,
                    "content": item.content,
                    "authority_level": (
                        "human_explicit"
                        if item.kind == "explicit"
                        else (
                            "advisory_inference"
                            if item.kind == "inferred"
                            else "legacy_unclassified"
                        )
                    ),
                    "evidence": item.evidence,
                    "source_ref": item.source_ref,
                    "source_type": item.source_actor,
                    "source_time": (
                        item.source_time.isoformat(timespec="seconds")
                        if item.source_time
                        else item.created_at.isoformat(timespec="seconds")
                    ),
                    "source_context": dict(item.source_context or {}),
                    "supersedes": list(item.supersedes_refs or []),
                    "updated_at": item.updated_at.isoformat(timespec="seconds"),
                }
                for item in policies
            ],
            "unfinished_owner_commitments": [
                {
                    "decision_ref": item.id,
                    "decision": item.decision,
                    "acceptance_criteria": list(item.acceptance_criteria),
                    "execution_status": item.execution_status,
                    "updated_at": item.updated_at.isoformat(timespec="seconds"),
                }
                for item in commitments
            ],
        }

    async def _list_external_agents(
        self,
        project: Project,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Return current provider evidence without changing project state."""

        del arguments, context
        providers = sorted(self._available_external_providers(project))
        profiles = {
            provider: profile
            for provider, profile in self.runtime.provider_profiles().items()
            if provider in providers
        }
        metrics = self.store.provider_metrics(providers)
        return {
            "providers": [
                {
                    "id": provider,
                    "profile": dict(profiles.get(provider) or {}),
                    "recent_metrics": dict(metrics.get(provider) or {}),
                }
                for provider in providers
            ],
            "configured_timeout_limit_seconds": max(
                10, self.settings.agent_timeout_seconds
            ),
            "selection_guidance": _provider_selection_guidance(profiles, metrics),
        }

    def _available_external_providers(self, project: Project) -> set[str]:
        providers = set(self.runtime.available_providers())
        execution_provider = (project.context or {}).get("execution_provider")
        return providers & {execution_provider} if execution_provider else providers

    async def _run_external_agent(
        self,
        project: Project,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Execute exactly one explicitly selected provider call."""

        # Each native request gets its own persistence session. Codex can issue
        # dynamic tool calls concurrently, and sharing one SQLAlchemy Session
        # across those coroutines would turn supported provider parallelism into
        # unsafe re-entrant transaction state.
        tool_session = Session(self.bind)
        tool_store = RootSessionStore(tool_session)

        async def execute(tool_call_id: str) -> dict[str, Any]:
            provider = str(arguments["provider"])
            available = self._available_external_providers(project)
            if provider not in available:
                raise ValueError(
                    f"External provider is not currently available: {provider}. "
                    "Call list_external_agents and choose one of its provider ids."
                )
            selection_reason = " ".join(
                redact_text(str(arguments["selection_reason"])).split()
            )[:300]
            task_name = str(arguments["task_name"])
            message = str(arguments["message"])
            access = str(arguments["access"])
            timeout_seconds = int(arguments["timeout_seconds"])
            if timeout_seconds > max(10, self.settings.agent_timeout_seconds):
                raise ValueError(
                    "Requested external provider timeout exceeds the configured limit"
                )
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
            agent = tool_store.create_agent(
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
            tool_store.update_agent(
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
                f"{display} 已完成外部任务，结果已返回主助手"
                if result.success
                else f"{display} 未完成外部任务，错误已返回主助手"
            )
            estimated_cost = (result.cost_info or {}).get("estimated_cost_usd")
            if isinstance(estimated_cost, (int, float)):
                with Session(self.bind) as cost_session:
                    cost_project = cost_session.get(Project, project.id)
                    if cost_project is not None:
                        cost_project.total_cost_usd = float(
                            cost_project.total_cost_usd or 0
                        ) + float(estimated_cost)
                        cost_project.updated_at = _now()
                        cost_session.add(cost_project)
                        cost_session.commit()
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
                usage=result.cost_info,
                usage_final=True,
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

        try:
            return await tool_store.execute_tool_once(
                project.id, context, arguments, execute
            )
        finally:
            tool_session.close()

    async def _run_external_agents(
        self,
        project: Project,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Execute one native batch request as concurrent provider calls."""

        agents = arguments.get("agents")
        if not isinstance(agents, list) or not 2 <= len(agents) <= 4:
            raise ValueError("run_external_agents requires two to four agent tasks")
        task_names = [
            str(agent.get("task_name") or "")
            for agent in agents
            if isinstance(agent, dict)
        ]
        if len(task_names) != len(agents) or len(set(task_names)) != len(task_names):
            raise ValueError("Each parallel external agent needs a unique task_name")

        batch_session = Session(self.bind)
        batch_store = RootSessionStore(batch_session)

        async def execute(_tool_call_id: str) -> dict[str, Any]:
            calls = []
            for index, agent_arguments in enumerate(agents):
                child_context = ToolContext(
                    thread_id=context.thread_id,
                    turn_id=context.turn_id,
                    call_id=f"{context.call_id}:{index}",
                    tool_name="run_external_agent",
                )
                calls.append(
                    self._run_external_agent(
                        project, dict(agent_arguments), child_context
                    )
                )
            raw_results = await asyncio.gather(*calls, return_exceptions=True)
            results: list[dict[str, Any]] = []
            for agent_arguments, raw_result in zip(agents, raw_results):
                if isinstance(raw_result, Exception):
                    results.append(
                        {
                            "success": False,
                            "provider": str(agent_arguments.get("provider") or ""),
                            "task_name": str(agent_arguments.get("task_name") or ""),
                            "error": _compact_text(
                                redact_text(str(raw_result)), limit=4_000
                            ),
                        }
                    )
                else:
                    results.append(raw_result)
            return {
                "success": all(result.get("success") is True for result in results),
                "parallel": True,
                "results": results,
            }

        try:
            return await batch_store.execute_tool_once(
                project.id, context, arguments, execute
            )
        finally:
            batch_session.close()

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
            # A progress card is a status indicator, not a transport
            # keepalive.  Refreshing it every 15 seconds made a half-open
            # Feishu connection accumulate requests until httpx exhausted its
            # pool.  Native activity still projects immediately.
            await asyncio.sleep(_PROGRESS_HEARTBEAT_SECONDS)
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
                label=label or "主助手正在处理本轮消息",
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
        if (
            state is None
            or state.projection_suspended
            or self.feishu_bridge is None
        ):
            return
        if monotonic() < state.next_delivery_attempt_monotonic:
            return
        try:
            with Session(self.bind) as projection_session:
                service = ProjectService(projection_session)
                project = service.require_project(project_id)
                if not project.feishu_chat_id:
                    return
                projection_key = _progress_projection_key(project.id, state)
                projection = service.get_card_projection(projection_key)
                await deliver_card(
                    service,
                    self.feishu_bridge,
                    dedupe_key=f"{projection_key}:{event_identity}",
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="progress",
                    card=_render_progress_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        state,
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
                timing = self._turn_timings.get(project_id)
                if timing is not None:
                    timing.mark_once("first_progress_card")
        except Exception:
            state.delivery_failures += 1
            backoff_index = min(
                state.delivery_failures - 1,
                len(_PROGRESS_DELIVERY_BACKOFF_SECONDS) - 1,
            )
            state.next_delivery_attempt_monotonic = (
                monotonic() + _PROGRESS_DELIVERY_BACKOFF_SECONDS[backoff_index]
            )
            logger.warning("Failed to project Root Agent progress", exc_info=True)
        else:
            state.delivery_failures = 0
            state.next_delivery_attempt_monotonic = 0.0

    def _persist_turn_timing(
        self,
        project_id: str,
        status: str,
        turn_id: str | None,
    ) -> None:
        """Persist one compact timing profile without affecting the turn."""

        timing = self._turn_timings.get(project_id)
        if timing is None:
            return
        try:
            with Session(self.bind) as profile_session:
                row = EventLog(
                    project_id=project_id,
                    level="INFO",
                    message="Root turn performance",
                    meta=timing.profile(status=status, turn_id=turn_id),
                )
                profile_session.add(row)
                profile_session.commit()
                prune_redundant_native_event_snapshots(
                    profile_session, project_id=project_id
                )
        except Exception:
            logger.warning(
                "Failed to persist Root turn timing for %s",
                project_id,
                exc_info=True,
            )

    def _latest_token_totals(
        self,
        project_id: str,
        thread_id: str | None,
    ) -> dict[str, int] | None:
        """Read one native thread's latest cumulative counter."""

        for row in self.service.get_project_events(project_id, limit=500):
            meta = row.meta if isinstance(row.meta, dict) else {}
            if meta.get("kind") != "thread/tokenUsage/updated":
                continue
            if thread_id is not None and meta.get("thread_id") != thread_id:
                continue
            payload = meta.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            usage = payload.get("tokenUsage")
            usage = usage if isinstance(usage, dict) else {}
            total = usage.get("total")
            if not isinstance(total, dict):
                continue
            return {
                key: int(value)
                for key, value in total.items()
                if isinstance(value, int)
            }
        return None

    def _queue_progress_snapshot(
        self,
        project_id: str,
        *,
        event_identity: str,
    ) -> None:
        """Coalesce UI updates without blocking Codex's native event stream."""

        state = self._progress.get(project_id)
        if state is not None and state.projection_suspended:
            return

        self._pending_progress_delivery[project_id] = event_identity
        task = self._progress_delivery_tasks.get(project_id)
        if task is not None and not task.done():
            return

        async def deliver_latest() -> None:
            try:
                while True:
                    # Do not spend another throttle interval merely to learn
                    # that no newer snapshot exists.  This was adding a fixed
                    # 1.5 seconds after every completed turn.
                    if project_id not in self._pending_progress_delivery:
                        return
                    wait_for = (
                        self._progress_last_delivery.get(project_id, 0.0)
                        + self._progress_min_delivery_interval
                        - monotonic()
                    )
                    if wait_for > 0:
                        flush_event = self._progress_flush_events.setdefault(
                            project_id, asyncio.Event()
                        )
                        try:
                            await asyncio.wait_for(
                                flush_event.wait(), timeout=wait_for
                            )
                        except asyncio.TimeoutError:
                            pass
                        flush_event.clear()
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
                self._progress_flush_events.pop(project_id, None)

        self._progress_delivery_tasks[project_id] = asyncio.create_task(
            deliver_latest(),
            name=f"mac-progress-delivery-{project_id}",
        )

    async def _suspend_progress_for_interaction(self, project_id: str) -> bool:
        """Freeze the current progress card before appending an interaction.

        Feishu can update a card in place but cannot move it in the message
        timeline.  Once a question or approval is appended below a progress
        card, that earlier card must become a historical snapshot; otherwise a
        later tool event would appear visually above the decision that enabled
        it.
        """

        state = self._progress.get(project_id)
        if state is None or state.projection_suspended:
            return False
        state.projection_suspended = True
        self._pending_progress_delivery.pop(project_id, None)
        delivery_task = self._progress_delivery_tasks.get(project_id)
        if delivery_task is not None:
            await delivery_task
        state.suspended_started_monotonic = monotonic()
        projection_key = _progress_projection_key(project_id, state)
        projection = self.service.get_card_projection(projection_key)
        if projection is not None:
            self.service.upsert_card_projection(
                projection_key=projection.projection_key,
                subject_type=projection.subject_type,
                subject_id=projection.subject_id,
                card_kind=projection.card_kind,
                provider_message_id=projection.provider_message_id,
                project_id=projection.project_id,
                conversation_id=projection.conversation_id,
                lifecycle_status="archived_for_interaction",
                selected_action=projection.selected_action,
                resolved_by=projection.resolved_by,
                resulting_state="waiting_user",
            )
        return True

    async def _rotate_progress_for_timeline_event(self, project_id: str) -> bool:
        """Archive the current card so later activity must append a new one."""

        state = self._progress.get(project_id)
        if state is None or state.projection_suspended:
            return False
        previous_projection_key = _progress_projection_key(project_id, state)
        # Change the key before cancelling queued work.  Any delivery that has
        # not yet selected its target will therefore resolve to the new
        # segment; the cancellation prevents an already-throttled snapshot
        # from patching the older card after the human message.
        state.projection_segment += 1
        await self._cancel_progress_snapshots(project_id)
        self._progress_last_delivery.pop(project_id, None)
        projection = self.service.get_card_projection(previous_projection_key)
        if projection is not None:
            self.service.upsert_card_projection(
                projection_key=projection.projection_key,
                subject_type=projection.subject_type,
                subject_id=projection.subject_id,
                card_kind=projection.card_kind,
                provider_message_id=projection.provider_message_id,
                project_id=projection.project_id,
                conversation_id=projection.conversation_id,
                lifecycle_status="archived_for_timeline_event",
                selected_action=projection.selected_action,
                resolved_by=projection.resolved_by,
                resulting_state=projection.resulting_state,
            )
        return True

    def _resume_progress_after_interaction(self, project_id: str) -> None:
        """Start a fresh card segment below the resolved interaction."""

        state = self._progress.get(project_id)
        if state is None or not state.projection_suspended:
            return
        if state.suspended_started_monotonic is not None:
            state.suspended_seconds += max(
                0.0, monotonic() - state.suspended_started_monotonic
            )
            state.suspended_started_monotonic = None
        state.projection_segment += 1
        state.projection_suspended = False
        # A new timeline segment should not inherit the previous card's
        # delivery throttle; the next real Codex event may be immediate.
        self._progress_last_delivery.pop(project_id, None)

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
                flush_event = self._progress_flush_events.setdefault(
                    project_id, asyncio.Event()
                )
                flush_event.set()
                await task

    async def _cancel_progress_snapshots(self, project_id: str) -> None:
        """Drop stale non-terminal UI work when the owning turn is cancelled."""

        self._pending_progress_delivery.pop(project_id, None)
        flush_event = self._progress_flush_events.pop(project_id, None)
        if flush_event is not None:
            flush_event.set()
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
        usage: dict[str, Any] | None = None,
        usage_final: bool = False,
    ) -> None:
        """Update the shared progress card from actual external CLI events."""
        if narrative is not None:
            narrative = " ".join(redact_text(narrative).split())[:1_200]
        self._update_progress(
            project_id,
            label=activity,
            color="purple",
            detail=observed_detail or "",
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
            execution_key = f"external:{agent_id}"
            is_new_execution = execution_key not in state.agent_executions
            running_before = any(
                execution.status == "running"
                for execution in state.agent_executions.values()
            )
            _upsert_agent_execution(
                state,
                execution_key,
                display=display,
                kind="external",
                status=step_status,
                model=model,
                activity=narrative or activity,
                detail=observed_detail,
                selection_reason=selection_reason,
            )
            if agent_id not in state.external_usage:
                state.external_usage[agent_id] = (display, None)
            if usage_final:
                state.external_usage[agent_id] = (
                    display,
                    canonical_usage(usage) or None,
                )
            if is_new_execution:
                _start_progress_big_step(
                    state,
                    f"交给 {display} 独立处理",
                    executor=display,
                    model=model,
                    selection_reason=selection_reason,
                    agent_id=execution_key,
                    complete_previous=not running_before,
                )
            state.external_agent_id = agent_id
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
                agent_id=execution_key,
            )
            if step_status in {"completed", "failed", "interrupted"}:
                _complete_progress_big_step(
                    state,
                    step_status,
                    agent_id=execution_key,
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
        timing = self._turn_timings.get(project_id)
        state = self._progress.get(project_id)
        is_root_event = bool(
            timing is not None
            and timing.root_thread_id
            and durable.thread_id == timing.root_thread_id
        )
        if timing is not None and is_root_event:
            if durable.kind == "turn/started":
                timing.mark_once("turn_started")
            elif durable.kind in {"item/started", "item/completed"}:
                timing.mark_once("first_item")
                item = durable.payload.get("item")
                if (
                    durable.kind == "item/completed"
                    and isinstance(item, dict)
                    and item.get("type") == "contextCompaction"
                ):
                    timing.context_compacted = True
                if isinstance(item, dict) and str(item.get("type") or "") in {
                    "commandExecution",
                    "fileChange",
                    "dynamicToolCall",
                    "mcpToolCall",
                    "webSearch",
                    "collabAgentToolCall",
                    "subAgentActivity",
                    "imageView",
                    "imageGeneration",
                }:
                    timing.mark_once("first_tool")
            elif durable.kind == "turn/completed":
                timing.mark_once("turn_completed")
                turn = durable.payload.get("turn")
                if isinstance(turn, dict):
                    duration = turn.get("durationMs")
                    if isinstance(duration, int):
                        timing.protocol_duration_ms = max(0, duration)
            elif durable.kind == "thread/tokenUsage/updated":
                usage = durable.payload.get("tokenUsage")
                usage = usage if isinstance(usage, dict) else {}
                total = usage.get("total")
                if isinstance(total, dict):
                    timing.token_latest = {
                        key: int(value)
                        for key, value in total.items()
                        if isinstance(value, int)
                    }
                last = usage.get("last")
                last = last if isinstance(last, dict) else {}
                context_input = last.get("inputTokens")
                if isinstance(context_input, int):
                    timing.context_input_tokens = context_input
        if state is not None and timing is not None and not is_root_event:
            child_thread_id = str(durable.thread_id or "").strip()
            if child_thread_id and durable.kind == "thread/tokenUsage/updated":
                if child_thread_id not in state.native_child_baselines:
                    state.native_child_baselines[child_thread_id] = (
                        self._latest_token_totals(project_id, child_thread_id) or {}
                    )
                usage = durable.payload.get("tokenUsage")
                usage = usage if isinstance(usage, dict) else {}
                total = usage.get("total")
                if isinstance(total, dict):
                    state.native_child_latest[child_thread_id] = {
                        key: int(value)
                        for key, value in total.items()
                        if isinstance(value, int)
                    }
                    state.native_child_models.setdefault(child_thread_id, None)
                    execution_key = f"native:{child_thread_id}"
                    existing = state.agent_executions.get(execution_key)
                    if existing is None:
                        native_count = sum(
                            execution.kind == "native"
                            for execution in state.agent_executions.values()
                        )
                        _upsert_agent_execution(
                            state,
                            execution_key,
                            display=f"Codex 子 Agent {native_count + 1}",
                            kind="native",
                            status="running",
                            activity="Codex 子 Agent 正在执行任务",
                        )
        if state is not None and durable.kind in {"item/started", "item/completed"}:
            item = durable.payload.get("item")
            if isinstance(item, dict):
                item_type = str(item.get("type") or "")
                model = str(item.get("model") or "").strip() or None
                child_ids: list[str] = []
                if item_type == "collabAgentToolCall":
                    receivers = item.get("receiverThreadIds")
                    if isinstance(receivers, list):
                        child_ids.extend(str(value) for value in receivers if value)
                    states = item.get("agentsStates")
                    if isinstance(states, dict):
                        child_ids.extend(str(value) for value in states)
                elif item_type == "subAgentActivity":
                    child_id = str(item.get("agentThreadId") or "").strip()
                    if child_id:
                        child_ids.append(child_id)
                for child_id in child_ids:
                    state.native_child_models[child_id] = (
                        model or state.native_child_models.get(child_id)
                    )
                    execution_key = f"native:{child_id}"
                    existing = state.agent_executions.get(execution_key)
                    if existing is not None:
                        display = existing.display
                    else:
                        native_count = sum(
                            execution.kind == "native"
                            for execution in state.agent_executions.values()
                        )
                        display = f"Codex 子 Agent {native_count + 1}"
                    raw_status = None
                    states = item.get("agentsStates")
                    if isinstance(states, dict):
                        child_state = states.get(child_id)
                        raw_status = (
                            child_state.get("status")
                            if isinstance(child_state, dict)
                            else child_state
                        )
                    if item_type == "subAgentActivity":
                        raw_status = item.get("kind") or raw_status
                    _upsert_agent_execution(
                        state,
                        execution_key,
                        display=display,
                        kind="native",
                        status=str(raw_status or "running"),
                        model=model,
                        activity="Codex 子 Agent 正在执行任务",
                    )
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
                    and durable.kind == "turn/started"
                ):
                    # Only the native lifecycle start may open a turn. Tool,
                    # token and process notifications can arrive after
                    # turn/completed (notably while background terminals are
                    # being cleaned) and must never resurrect an idle project.
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
        state = self._progress.get(project_id)
        provider = state.root_provider_name if state else "Codex"
        item = event.payload.get("item")
        if event.kind in {"item/started", "item/completed"} and isinstance(item, dict):
            activity = _native_activity_card(event.kind, item, provider)
            event_identity = str(item.get("id") or "item")
        else:
            activity = _native_status_card(event.kind, event.payload, provider)
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
                label = f"{provider} 已汇报阶段进展，正在继续处理"
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
            timing = self._turn_timings.get(project_id)
            event_from_child_thread = bool(
                timing is not None
                and timing.root_thread_id
                and event.thread_id
                and event.thread_id != timing.root_thread_id
            )
            if event_from_child_thread and event.thread_id:
                execution_key = f"native:{event.thread_id}"
                existing = state.agent_executions.get(execution_key)
                if existing is None:
                    native_count = sum(
                        execution.kind == "native"
                        for execution in state.agent_executions.values()
                    )
                    display = f"Codex 子 Agent {native_count + 1}"
                else:
                    display = existing.display
                child_status = "running"
                if event.kind == "error":
                    child_status = "failed"
                elif event.kind == "item/completed" and isinstance(item, dict):
                    if str(item.get("status") or "") in {"failed", "declined"}:
                        child_status = "failed"
                _upsert_agent_execution(
                    state,
                    execution_key,
                    display=display,
                    kind="native",
                    status=child_status,
                    model=(
                        state.native_child_models.get(str(event.thread_id))
                        or state.root_model
                    ),
                    activity=narrative or label,
                    detail=detail or None,
                )
            native_executor = (
                "Codex 子 Agent"
                if event_from_child_thread
                or item_type in {"collabAgentToolCall", "subAgentActivity"}
                else f"{provider} Root"
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
                    executor=f"{provider} Root",
                    model=observed_model,
                )
            elif item_type and item_type != "agentMessage":
                if item_type == "dynamicToolCall" and event.kind == "item/completed":
                    state.external_agent_id = None
                    _start_progress_big_step(
                        state,
                        "整理外部 coding-agent 返回结果",
                        executor=f"{provider} Root",
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
            elif not item_type and event.kind != "turn/started":
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
        self,
        project: Project,
        result: TurnResult,
        *,
        user_request: str,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> None:
        """Close the shared progress projection when the native turn finishes."""

        label, color, lifecycle = _turn_completion_card(result.status)
        failure_detail = (
            _turn_failure_detail(result) if result.status == "failed" else None
        )
        failure_content = ""
        failure_report_title = ""
        failure_report_ref = ""
        if result.status == "failed":
            failure_content = label
            if failure_detail:
                failure_content += f"\n\n**失败原因**：{failure_detail}"
            failure_content += (
                "\n\n项目仍保持可继续状态；任务未标记为完成。"
            )
            failure_report_ref = f"root-status:{result.turn_id}:report"
            failure_report_title = (
                f"[{self._project_display_name(project)}] 本轮未完成"
            )
            # Persist the provider's terminal result independently of Feishu
            # availability.  This also attaches the native turn id and clears
            # the active history pointer before the project returns idle.
            self.service.record_project_history_report(
                project.id,
                native_turn_id=result.turn_id,
                title=failure_report_title,
                content=failure_content,
                report_ref=failure_report_ref,
                report_status="failed",
                fallback_user_request=user_request,
                request_origin=request_origin,
                parent_decision_id=parent_decision_id,
            )
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        progress_label = (
            f"本轮未完成：{failure_detail}"
            if failure_detail
            else label
        )
        state = self._progress.get(project.id)
        if state is not None:
            if state.projection_suspended:
                self._resume_progress_after_interaction(project.id)
            state.status = lifecycle
            self._update_progress(
                project.id,
                label=progress_label,
                color=color,
                detail="",
                observed_event=True,
            )
            _complete_progress_big_step(
                state,
                lifecycle,
            )
            body = None
        else:
            body = f"**当前进展**：{progress_label}"
        projection_key = (
            _progress_projection_key(project.id, state)
            if state is not None
            else f"root-agent:{project.id}:activity:{result.turn_id}:terminal"
        )
        projection = self.service.get_card_projection(projection_key)
        try:
            await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"{projection_key}:{result.turn_id}:completed",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="progress",
                card=(
                    _render_progress_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        state,
                        color=color,
                    )
                    if state is not None
                    else build_content_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        body or "",
                        color=color,
                    )
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
        except Exception:
            # A stale or temporarily unreachable progress card must never
            # suppress the immutable terminal failure report below.
            logger.warning(
                "Root terminal progress projection failed for %s",
                project.id,
                exc_info=True,
            )
        if result.status == "failed":
            try:
                await deliver_card(
                    self.service,
                    self.feishu_bridge,
                    dedupe_key=failure_report_ref,
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="report",
                    card=build_content_card(
                        failure_report_title,
                        failure_content,
                        color="red",
                    ),
                    project_id=project.id,
                    projection_key=failure_report_ref,
                    subject_type="root_turn",
                    subject_id=result.turn_id,
                    lifecycle_status="failed",
                    resulting_state="idle",
                )
            except Exception:
                # The outbox owns delivery retries. Keep the public history
                # tied to this exact turn without claiming it was delivered.
                self.service.record_project_history_report(
                    project.id,
                    native_turn_id=result.turn_id,
                    title=failure_report_title,
                    content=failure_content,
                    report_ref=failure_report_ref,
                    report_status="delivery_pending",
                    fallback_user_request=user_request,
                    request_origin=request_origin,
                    parent_decision_id=parent_decision_id,
                )
                logger.warning(
                    "Root failure report is pending durable delivery for %s",
                    project.id,
                    exc_info=True,
                )
                return
            # Provider/runtime failures are terminal transport facts, not a
            # project-management decision.  Sending them through Owner turns
            # the same unchanged failure into an autonomous retry loop.
            return

    async def _on_server_request(
        self, project_id: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "item/tool/requestUserInput":
            return await self._request_user_input(project_id, params)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            return await self._request_native_approval(project_id, method, params)
        raise RuntimeError(f"Unsupported Codex server request: {method}")

    async def _project_owner_decide(
        self,
        project: Project,
        questions: list[dict[str, Any]],
    ) -> ProjectOwnerResult:
        """Consult one isolated Project Owner without changing the Root thread."""

        if not self.service.is_project_owner_enabled(project.id):
            raise RuntimeError("Project Owner 已关闭")
        root_session = self.store.ensure(project.id)
        return await ProjectOwner(self.service, self.settings).decide(
            project,
            questions,
            request_context={
                "text": root_session.last_user_request or "",
                "origin": root_session.last_request_origin or "human",
                "parent_decision_id": root_session.last_parent_decision_id,
            },
        )

    async def _apply_project_owner_decision(
        self,
        project: Project,
        item_id: str,
        questions: list[dict[str, Any]],
        result: ProjectOwnerResult,
        interaction: QuestionInteraction,
        response_builder: Callable[[dict[str, str]], dict[str, Any]] = _request_user_input_response,
    ) -> dict[str, Any] | None:
        """Apply an isolated Owner answer to the original interaction.

        The native question interaction remains the lifecycle source of truth.
        Project Owner adds a durable decision record and answers that same
        interaction. The old interactive card is then closed; the later Owner
        decision is appended at the newest point in the chat timeline.
        """

        if result.outcome != "decide" or not result.answers:
            return None
        selected = {
            question["question_id"]: result.answers[question["question_id"]]
            for question in questions
            if question["question_id"] in result.answers
        }
        if len(selected) != len(questions):
            return None
        current_interaction = self.service.get_question_interaction(interaction.id)
        if current_interaction is None or current_interaction.status != "pending":
            # Another responder may have completed the original card while the
            # isolated Owner was thinking. The first durable response wins.
            return None
        response = response_builder(selected)
        _resolution_note, resulting_state = _native_approval_resolution(response)
        existing = self.service.get_project_decision_by_request_item_id(
            project.id, item_id
        )
        if existing is not None and existing.status == "applied":
            return dict(existing.answer_payload or response)
        if not self.service.claim_question_interaction(
            interaction.id, resolution_source="project_owner"
        ):
            # The user completed the original card after the Owner started but
            # before its result was ready. The durable first responder wins.
            return None
        durable = self.store.ensure(project.id)
        question_text = "；".join(
            str(question.get("question") or "") for question in questions
        )
        options = [
            {
                "question_id": question["question_id"],
                "label": option.get("label"),
                "description": option.get("description"),
            }
            for question in questions
            for option in question.get("options") or []
        ]
        decision = self.service.create_project_decision(
            project.id,
            request_item_id=item_id,
            native_turn_id=durable.active_turn_id,
            question=question_text,
            options=options,
            decision=result.decision,
            rationale=result.rationale,
            impact=result.impact,
            reversible=result.reversible,
            confidence=result.confidence,
            answer_payload=response,
            context_refs=list(result.context_refs),
            authority_refs=list(result.authority_refs),
            actor_model=result.model,
            actor_usage=result.usage,
            actor_duration_seconds=result.duration_seconds,
            status="applying",
            decision_type=(
                "approval" if interaction.stage == "native_approval" else "question"
            ),
        )
        if self.feishu_bridge is None or not project.feishu_chat_id:
            self.service.update_project_decision(
                decision.id, status="application_failed"
            )
            self.service.release_question_interaction(
                interaction.id, resolution_source="project_owner"
            )
            return None
        try:
            stored_questions = self.service.get_questions_for_interaction(interaction.id)
            for question in stored_questions:
                value = result.answers[question.question_id]
                await self.service.submit_question_answer(
                    question.id,
                    value,
                    value,
                    False,
                    "project_owner",
                    answer_source="project_owner",
                    source_refs=list(
                        result.answer_source_refs.get(question.question_id, ())
                    ),
                    commit=False,
                )
            if not self.service.complete_question_interaction_claim(
                interaction.id,
                claimed_by="project_owner",
                resolution_source="project_owner",
                commit=False,
            ):
                raise RuntimeError("Project Owner reservation was lost")
            decision.status = "applied"
            decision.updated_at = _now()
            self.session.add(decision)
            self.session.commit()
        except Exception:
            self.session.rollback()
            failed = self.service.update_project_decision(
                decision.id, status="application_failed"
            )
            self.service.release_question_interaction(
                interaction.id, resolution_source="project_owner"
            )
            if interaction.stage == "native_approval":
                logger.warning(
                    "Project Owner approval could not be applied; preserving original approval card",
                    exc_info=True,
                )
                return None
            failed_card = build_project_decision_card(
                failed.id,
                project_name=self._project_display_name(project),
                question=failed.question,
                decision=failed.decision,
                rationale=failed.rationale,
                impact=failed.impact,
                reversible=failed.reversible,
                status=failed.status,
                actor_model=failed.actor_model,
                actor_usage=failed.actor_usage,
                actor_duration_seconds=failed.actor_duration_seconds,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
            )
            try:
                await deliver_card(
                    self.service,
                    self.feishu_bridge,
                    dedupe_key=f"project-owner-application-failed:{failed.id}",
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="decision",
                    card=failed_card,
                    project_id=project.id,
                    update_message_id=failed.card_message_id,
                    projection_key=f"decision:{failed.id}",
                    subject_type="project_decision",
                    subject_id=failed.id,
                    lifecycle_status="application_failed",
                    resulting_state="running",
                )
            except Exception:
                logger.warning(
                    "Could not update failed Project Owner decision card",
                    exc_info=True,
                )
            logger.warning(
                "Project Owner decision could not be applied; asking the user instead",
                exc_info=True,
            )
            return None

        recalled = False
        if interaction.card_message_id:
            try:
                await self.feishu_bridge.delete_message(interaction.card_message_id)
                recalled = True
            except Exception:
                # The durable interaction already rejects late callbacks. Do
                # not rewrite the historical card with facts that happened
                # later merely because Feishu could not recall the message.
                logger.warning(
                    "Project Owner answer applied but old interaction card could not be recalled",
                    exc_info=True,
                )
        self.service.upsert_card_projection(
            projection_key=f"question:{interaction.id}",
            project_id=project.id,
            subject_type="question_interaction",
            subject_id=interaction.id,
            card_kind="question",
            provider_message_id=interaction.card_message_id,
            lifecycle_status="recalled" if recalled else "closed_unrecalled",
            selected_action="project_owner",
            resolved_by="project_owner",
            resulting_state=resulting_state,
        )

        # Only claim success publicly after the selected answer and the native
        # question lifecycle are durable. The decision card is the later event;
        # notification delivery can retry independently
        # without rolling back an answer Codex has already received.
        card = build_project_decision_card(
            decision.id,
            project_name=self._project_display_name(project),
            question=decision.question,
            decision=decision.decision,
            rationale=decision.rationale,
            impact=decision.impact,
            reversible=decision.reversible,
            status="applied",
            actor_model=decision.actor_model,
            actor_usage=decision.actor_usage,
            actor_duration_seconds=decision.actor_duration_seconds,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
        )
        try:
            message_id = await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"project-owner:{item_id}",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="decision",
                card=card,
                project_id=project.id,
                projection_key=f"decision:{decision.id}",
                subject_type="project_decision",
                subject_id=decision.id,
                lifecycle_status="applied",
                resulting_state="running",
            )
        except Exception:
            logger.warning(
                "Project Owner answer applied but decision card delivery failed",
                exc_info=True,
            )
        else:
            self.service.update_project_decision(
                decision.id,
                status="applied",
                card_message_id=message_id,
            )
        return response

    async def _publish_owner_needs_user(
        self,
        project: Project,
        *,
        request_item_id: str,
        native_turn_id: str | None,
        question: str,
        result: ProjectOwnerResult,
        interaction: QuestionInteraction | None = None,
        questions: list[dict[str, Any]] | None = None,
        presentation_status: str = "requires_user",
        decision_type: str | None = None,
        source_request: str = "",
        public_context: dict[str, Any] | None = None,
    ) -> ProjectDecision | None:
        """Persist an Owner recommendation, embedding report reviews in-place."""

        existing = self.service.get_project_decision_by_request_item_id(
            project.id, request_item_id
        )
        if existing is not None:
            return existing
        if interaction is not None:
            # The original native interaction is already the authoritative
            # unresolved choice. A defer is not a second project decision.
            return None
        options = list(result.user_options)
        decision = self.service.create_project_decision(
            project.id,
            request_item_id=request_item_id,
            native_turn_id=native_turn_id,
            question=question,
            options=options,
            decision=result.decision,
            rationale=result.rationale,
            impact=result.impact,
            reversible=False,
            confidence=result.confidence or "low",
            answer_payload={
                "requires_user": True,
                **redact_secrets(public_context or {}),
                **(
                    {"source_request": redact_text(source_request)[:4_000]}
                    if source_request
                    else {}
                ),
            },
            context_refs=list(result.context_refs),
            authority_refs=list(result.authority_refs),
            actor_model=result.model,
            actor_usage=result.usage,
            actor_duration_seconds=result.duration_seconds,
            status="requires_user",
            decision_type=decision_type or (
                "approval_assessment"
                if interaction is not None and interaction.stage == "native_approval"
                else "assessment"
            ),
        )
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return decision
        card = build_project_decision_card(
            decision.id,
            project_name=self._project_display_name(project),
            question=decision.question,
            decision=decision.decision,
            rationale=decision.rationale,
            impact=decision.impact,
            reversible=False,
            options=list(decision.options or []),
            status=presentation_status,
            actor_model=decision.actor_model,
            actor_usage=decision.actor_usage,
            actor_duration_seconds=decision.actor_duration_seconds,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
        )
        try:
            message_id = await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"project-owner-needs-user:{request_item_id}",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type=("advice" if presentation_status == "advice" else "decision"),
                card=card,
                project_id=project.id,
                projection_key=f"decision:{decision.id}",
                subject_type="project_decision",
                subject_id=decision.id,
                lifecycle_status="requires_user",
                resulting_state="waiting_user",
            )
        except Exception:
            self.service.update_project_decision(
                decision.id,
                status="delivery_failed",
            )
            logger.warning("Project Owner assessment card delivery failed", exc_info=True)
            return decision
        self.service.update_project_decision(
            decision.id, status="requires_user", card_message_id=message_id
        )
        return self.service.get_project_decision(decision.id)

    async def _sync_governance_decision_card(
        self, project: Project, decision: ProjectDecision
    ) -> None:
        """Append the latest governance state without rewriting its old card."""

        if bool((decision.answer_payload or {}).get("embedded_report")):
            return

        if (
            self.feishu_bridge is None
            or not project.feishu_chat_id
            or not decision.card_message_id
        ):
            return
        card = build_project_decision_card(
            decision.id,
            project_name=self._project_display_name(project),
            question=decision.question,
            decision=decision.decision,
            rationale=decision.rationale,
            impact=decision.impact,
            reversible=decision.reversible,
            status=decision.status,
            user_override=decision.user_override,
            actor_model=decision.actor_model,
            actor_usage=decision.actor_usage,
            actor_duration_seconds=decision.actor_duration_seconds,
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            execution_status=decision.execution_status,
            execution_rounds=decision.execution_rounds,
        )
        try:
            message_id = await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=(
                    f"governance-decision-update:{decision.id}:"
                    f"{decision.updated_at.isoformat()}"
                ),
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="decision",
                card=card,
                project_id=project.id,
                projection_key=f"decision:{decision.id}",
                subject_type="project_decision",
                subject_id=decision.id,
                lifecycle_status=decision.execution_status,
                resulting_state=project.execution_status,
            )
            if message_id and message_id != decision.card_message_id:
                self.service.update_project_decision(
                    decision.id,
                    status=decision.status,
                    card_message_id=message_id,
                    execution_status=decision.execution_status,
                )
        except Exception:
            logger.warning(
                "Could not sync Project Owner governance decision %s",
                decision.id,
                exc_info=True,
            )

    async def _review_delivered_report(
        self,
        project: Project,
        *,
        turn_id: str,
        title: str,
        content: str,
        user_request: str = "",
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        history_round_id: str | None = None,
        review_id: str | None = None,
        request_item_id: str | None = None,
    ) -> None:
        """Let the isolated Owner review one user-visible logical report.

        The report is already delivered before this method runs. A decision is
        appended after it and only then, when requested by the Owner, handed
        back to the persistent Root thread as a new native turn.
        """

        source_id = request_item_id or f"report:{turn_id}"
        if not self.service.is_project_owner_enabled(project.id):
            if review_id is not None:
                superseded = self.service.supersede_project_owner_review(review_id)
                if superseded is not None:
                    await self._sync_report_owner_review_card(project, superseded)
            return
        existing_review = self.service.get_project_owner_review_by_request_item_id(
            project.id, source_id
        )
        existing_decision = self.service.get_project_decision_by_request_item_id(
            project.id, source_id
        )
        if existing_decision is not None:
            return
        if existing_review is not None and (
            review_id is None
            or existing_review.id != review_id
            or existing_review.status == "completed"
        ):
            return
        try:
            review_kwargs: dict[str, Any] = {
                "title": title,
                "content": content,
                "user_request": user_request,
                "native_turn_id": turn_id,
            }
            if history_round_id:
                review_kwargs["history_round_id"] = history_round_id
            if request_origin != "human":
                review_kwargs["request_origin"] = request_origin
            if parent_decision_id:
                review_kwargs["parent_decision_id"] = parent_decision_id
            result = await ProjectOwner(
                self.service, self.settings
            ).review_report(project, **review_kwargs)
        except Exception:
            logger.warning(
                "Project Owner could not review delivered report %s",
                turn_id,
                exc_info=True,
            )
            # Durable background jobs must not remain ``running`` forever.
            # Let their scheduler record the failure and reconcile the source
            # report card. Direct diagnostic calls keep the historical
            # best-effort behavior.
            if review_id is not None:
                raise
            return
        if not self.service.is_project_owner_enabled(project.id):
            if review_id is not None:
                superseded = self.service.supersede_project_owner_review(review_id)
                if superseded is not None:
                    await self._sync_report_owner_review_card(project, superseded)
            return
        if review_id is not None:
            self.service.session.expire_all()
            current_review = self.service.session.get(ProjectOwnerReview, review_id)
            if current_review is None or current_review.status != "running":
                if current_review is not None:
                    await self._sync_report_owner_review_card(project, current_review)
                return
        review_context_refs = list(
            dict.fromkeys([source_id, *result.context_refs])
        )
        review = self.service.create_project_owner_review(
            project.id,
            request_item_id=source_id,
            native_turn_id=turn_id,
            outcome=result.outcome,
            decision=result.decision,
            rationale=result.rationale,
            impact=result.impact,
            confidence=result.confidence,
            context_refs=review_context_refs,
            authority_refs=list(result.authority_refs),
            actor_model=result.model,
            actor_usage=result.usage,
            actor_duration_seconds=result.duration_seconds,
            continue_execution=result.continue_execution,
            memory_updates=list(result.preference_observations),
            policy_updates=list(result.project_policy_observations),
            status="completed",
            review_id=review_id,
            request_origin=request_origin,
            parent_decision_id=parent_decision_id,
        )
        # Rule extraction records user authority independently of this review's
        # conclusion. Persist it before a newer report can supersede the review,
        # or a correction that arrived during review could be lost.
        owner_open_id = self.service.get_project_owner_open_id(project.id)
        authority_actor_ids: set[str] = set()
        authority_round = self.service.get_project_history_round(
            project.id,
            round_id=history_round_id,
            native_turn_id=(None if history_round_id else turn_id),
        )
        if authority_round is not None:
            authority_actor_ids = {
                str(item.get("actor_open_id") or "").strip()
                for item in (authority_round.user_instructions or [])
                if isinstance(item, dict)
                and str(item.get("origin") or "").startswith("human")
                and str(item.get("actor_open_id") or "").strip()
            }
        preference_owner_id = (
            next(iter(authority_actor_ids))
            if len(authority_actor_ids) == 1
            else owner_open_id if not authority_actor_ids else None
        )
        if preference_owner_id and result.preference_observations:
            self.service.add_owner_memories(
                preference_owner_id,
                list(result.preference_observations),
                project_id=project.id,
                source_ref=source_id,
            )
        # Multi-human governance is intentionally not implemented yet. A
        # non-owner can steer the current turn, but cannot silently rewrite the
        # durable project rules of the single supported human Owner.
        policy_actor_is_owner = (
            not authority_actor_ids
            or authority_actor_ids == {str(owner_open_id or "")}
        )
        if result.project_policy_observations and policy_actor_is_owner:
            self.service.add_project_policies(
                project.id,
                list(result.project_policy_observations),
                source_ref=source_id,
            )
        if (
            review_id is not None
            and not self.service.is_project_owner_review_current(review.id)
        ):
            superseded = self.service.supersede_project_owner_review(review.id)
            if superseded is not None:
                await self._sync_report_owner_review_card(project, superseded)
            return
        # The Owner conclusion is a current judgment over human authority,
        # history and unfinished decisions. It does not maintain a separate
        # workflow state machine or schedule autonomous wake-ups.
        parent_decision = (
            self.service.get_project_decision(parent_decision_id)
            if parent_decision_id
            else None
        )
        human_context = _owner_human_context(
            authority_round,
            parent_decision,
            fallback_text=(user_request if request_origin == "human" else ""),
        )
        if not human_context:
            human_context = latest_project_human_context(
                self.service,
                project.id,
                parent_decision_id=parent_decision_id,
            )

        # The model may correctly review an old report while a newer human
        # round is already present.  Re-check immediately before applying any
        # Owner outcome: an older continuation must never overtake a newer
        # human correction, pause or replacement request.
        # Compare against the execution round being reviewed, not an older
        # linked Owner decision. Human input that predates this round was
        # already available to it and must not make its result look stale.
        freshness_boundary = (
            authority_round.started_at
            if authority_round is not None
            else review.created_at
        )
        newer_human_round = _newer_external_human_round(
            self.service,
            project.id,
            after=freshness_boundary,
            current_round_id=(authority_round.id if authority_round is not None else None),
        )
        if newer_human_round is not None:
            superseded_review = self.service.supersede_project_owner_review(
                review.id
            )
            if (
                parent_decision is not None
                and parent_decision.execution_status == "active"
            ):
                parent_decision = self.service.update_project_decision(
                    parent_decision.id,
                    status=parent_decision.status,
                    execution_status="superseded",
                    result_ref=source_id,
                )
            await self.service.log_event(
                project.id,
                "INFO",
                "Project Owner result superseded by newer human instruction",
                {
                    "review_id": review.id,
                    "reviewed_round_id": (
                        authority_round.id if authority_round is not None else None
                    ),
                    "newer_human_round_id": newer_human_round.id,
                },
            )
            self.service.session.commit()
            if superseded_review is not None:
                await self._sync_report_owner_review_card(
                    project,
                    superseded_review,
                    parent_decision=parent_decision,
                )
            return

        if result.outcome == "no_action":
            if parent_decision is not None:
                parent_decision = self.service.update_project_decision(
                    parent_decision.id,
                    status=parent_decision.status,
                    execution_status="completed",
                    result_ref=source_id,
                )
            await self._sync_report_owner_review_card(
                project, review, parent_decision=parent_decision
            )
            return

        if result.outcome == "advise" and result.decision:
            # This execution round has ended. The advice is a separate,
            # non-executing user choice and must not leave the old Owner
            # commitment looking unfinished in later reviews.
            if parent_decision is not None:
                parent_decision = self.service.update_project_decision(
                    parent_decision.id,
                    status=parent_decision.status,
                    execution_status="completed",
                    result_ref=source_id,
                )
            await self._sync_report_owner_review_card(
                project, review, parent_decision=parent_decision
            )
            await self._publish_owner_needs_user(
                project,
                request_item_id=source_id,
                native_turn_id=turn_id,
                question="本轮汇报后的建议",
                result=result,
                presentation_status="advice",
                decision_type="advice",
                public_context={
                    "human_context": human_context,
                    "source_report": {"title": title, "content": content},
                },
            )
            return

        if result.outcome == "require_user" and result.decision:
            # A newly requested user choice owns any possible continuation.
            # Close the report's parent commitment so it cannot be revived as
            # parallel unfinished work after the user answers something else.
            if parent_decision is not None:
                parent_decision = self.service.update_project_decision(
                    parent_decision.id,
                    status=parent_decision.status,
                    execution_status="completed",
                    result_ref=source_id,
                )
            await self._sync_report_owner_review_card(
                project, review, parent_decision=parent_decision
            )
            await self._publish_owner_needs_user(
                project,
                request_item_id=source_id,
                native_turn_id=turn_id,
                question="本轮汇报后的项目决定",
                result=result,
                public_context={
                    "human_context": human_context,
                    "source_report": {"title": title, "content": content},
                },
            )
            return

        if (
            parent_decision is not None
            and result.outcome == "decide"
            and result.continue_execution
        ):
            parent_decision = self.service.update_project_decision(
                parent_decision.id,
                status=parent_decision.status,
                execution_status="active",
                result_ref=source_id,
                increment_execution_rounds=True,
            )
            await self._sync_report_owner_review_card(
                project, review, parent_decision=parent_decision
            )
            rendered_human_context = _render_owner_human_context(human_context)
            trigger = (
                (
                    "[最近真人原始上下文；这是主线，优先于 Root 汇报和 Owner 转述]\n"
                    + rendered_human_context
                    + "\n\n"
                    if rendered_human_context
                    else ""
                )
                + "[AI Project Owner 已根据最新真人主线审阅本轮结果。下面的当前决定是本轮唯一的"
                "执行方向；旧 Owner 决定只用于历史关联，不是新的用户权威，也不得重复注入为当前任务。"
                "如需要新的用户偏好、授权或外部事实，使用 request_user_input。]\n"
                f"当前决定：{result.decision}\n"
                "范围边界：只完成这个当前决定，不因本轮汇报里的局部障碍、相邻发现或旧决定扩大目标、"
                "范围、风险或另造流程。"
            )
            schedule_root_turn(
                self.bind,
                self.runtime,
                self.settings,
                project.id,
                trigger,
                visible_request=f"Owner 决定：{result.decision}",
                request_origin="project_owner",
                parent_decision_id=parent_decision.id,
            )
            if result.user_options:
                await self._publish_owner_needs_user(
                    project,
                    request_item_id=f"{source_id}:next-step-options",
                    native_turn_id=turn_id,
                    question="本轮推进中的下一步选择",
                    result=result,
                    presentation_status="advice",
                    decision_type="advice",
                    public_context={
                        "human_context": human_context,
                        "source_report": {"title": title, "content": content},
                    },
                )
            return

        if result.outcome != "decide" or not result.decision:
            await self._sync_report_owner_review_card(project, review)
            return

        if parent_decision is not None:
            parent_decision = self.service.update_project_decision(
                parent_decision.id,
                status=parent_decision.status,
                execution_status="completed",
                result_ref=source_id,
            )

        previous_active = self.service.get_active_project_decision(project.id)
        if previous_active is not None:
            previous_active = self.service.update_project_decision(
                previous_active.id,
                status=previous_active.status,
                execution_status="superseded",
                result_ref=source_id,
            )

        report_card = dict((review.input_payload or {}).get("report_card") or {})
        decision = self.service.create_project_decision(
            project.id,
            request_item_id=source_id,
            native_turn_id=turn_id,
            question="本轮汇报后的项目决定",
            options=[],
            decision=result.decision,
            rationale=result.rationale,
            impact=result.impact,
            reversible=result.reversible,
            confidence=result.confidence,
            answer_payload={
                "source": "report_review",
                **(
                    {"embedded_report": True, "report_card": report_card}
                    if report_card
                    else {}
                ),
                "continue_execution": result.continue_execution,
                "human_context": human_context,
            },
            context_refs=review_context_refs,
            authority_refs=list(result.authority_refs),
            actor_model=result.model,
            actor_usage=result.usage,
            actor_duration_seconds=result.duration_seconds,
            status="pending_delivery",
            decision_type="governance",
            execution_status=("active" if result.continue_execution else "completed"),
            execution_rounds=(1 if result.continue_execution else 0),
            acceptance_criteria=(
                list(parent_decision.acceptance_criteria)
                if parent_decision is not None
                else []
            ),
        )
        message_id = str(report_card.get("message_id") or "").strip()
        decision = self.service.update_project_decision(
            decision.id,
            status="applied",
            card_message_id=(message_id or None),
        )
        await self._sync_report_owner_review_card(
            project, review, decision=decision, parent_decision=parent_decision
        )

        if result.continue_execution:
            rendered_human_context = _render_owner_human_context(human_context)
            trigger = (
                (
                    "[最近真人原始上下文；这是主线，优先于 Root 汇报和 Owner 转述]\n"
                    + rendered_human_context
                    + "\n\n"
                    if rendered_human_context
                    else ""
                )
                + "[AI Project Owner 已根据最新真人主线审阅本轮结果。下面的当前决定是本轮唯一的"
                "执行方向；本轮汇报只作为证据，不能据此扩展相邻工作。"
                "如需要新的用户偏好、授权或外部事实，使用 request_user_input。]\n"
                f"当前决定：{result.decision}\n"
                "范围边界：只完成这个当前决定，不因局部障碍、相邻发现或旧 Owner 判断扩大目标、"
                "范围、风险或另造流程。"
            )
            schedule_root_turn(
                self.bind,
                self.runtime,
                self.settings,
                project.id,
                trigger,
                visible_request=f"Owner 决定：{result.decision}",
                request_origin="project_owner",
                parent_decision_id=decision.id,
            )
        if result.user_options:
            await self._publish_owner_needs_user(
                project,
                request_item_id=f"{source_id}:next-step-options",
                native_turn_id=turn_id,
                question=(
                    "本轮推进中的下一步选择"
                    if result.continue_execution
                    else "本轮汇报后的下一步选择"
                ),
                result=result,
                presentation_status="advice",
                decision_type="advice",
                public_context={
                    "human_context": human_context,
                    "source_report": {"title": title, "content": content},
                },
            )

    async def _sync_report_owner_review_card(
        self,
        project: Project,
        review: Any,
        *,
        conclusion: str | None = None,
        service: ProjectService | None = None,
        decision: ProjectDecision | None = None,
        parent_decision: ProjectDecision | None = None,
    ) -> None:
        """Project durable Owner state back onto the source report card."""

        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        projection_service = service or self.service
        report_card = dict((review.input_payload or {}).get("report_card") or {})
        projection_key = str(report_card.get("projection_key") or "").strip()
        if not projection_key:
            return
        projection = projection_service.get_card_projection(projection_key)
        # Superseded describes the judgment, not the card's position in the
        # timeline. Freeze only when a human instruction was actually added
        # after this card; input that predates the card must still allow its
        # terminal Owner status to replace "reviewing".
        if (
            review.status == "superseded"
            and projection is not None
            and _newer_external_human_round(
                projection_service,
                project.id,
                after=projection.created_at,
            )
            is not None
        ):
            return
        message_id = str(
            report_card.get("message_id")
            or (projection.provider_message_id if projection is not None else "")
            or ""
        ).strip()
        if not message_id:
            return

        current_project = projection_service.get_project(project.id) or project
        execution_status_text = {
            "idle": "当前空闲",
            "running": "正在执行",
            "owner_reviewing": "Owner 审阅中",
            "waiting_user": "等待你决定",
            "waiting_external": "等待外部条件",
        }.get(
            current_project.execution_status,
            f"执行 {current_project.execution_status}",
        )

        duration = review.actor_duration_seconds
        duration_text = (
            f" · {max(0, round(duration))} 秒"
            if isinstance(duration, (int, float))
            else ""
        )
        if review.status in {"pending", "running"}:
            visible_status = "Project Owner · 审阅中"
            lifecycle = "owner_reviewing"
            details: list[tuple[str, list[str], bool]] = []
        elif review.status == "failed":
            visible_status = "Project Owner · 审阅未完成"
            lifecycle = "owner_review_failed"
            details = [
                (
                    "Owner 审阅详情",
                    [f"失败原因：{str(review.last_error or '后端未返回具体原因')[:1_000]}"],
                    False,
                )
            ]
        elif review.status == "superseded":
            visible_status = "Project Owner · 已审阅 · 后续消息已接管"
            lifecycle = "superseded"
            details = []
        else:
            outcome_text = conclusion or {
                "no_action": "无需新决定",
                "advise": "有一项建议",
                "require_user": "需要你决定",
                "decide": "已决定",
            }.get(str(review.outcome or ""), "审阅完成")
            visible_status = (
                f"Project Owner · 已审阅 · {outcome_text}{duration_text}"
            )
            lifecycle = "completed"
            review_rows: list[str] = []
            if review.rationale:
                review_rows.append(f"判断依据：{review.rationale}")
            if review.impact:
                review_rows.append(f"结果影响：{review.impact}")
            if review.authority_refs:
                review_rows.append(
                    "依据来源：" + "；".join(str(item) for item in review.authority_refs)
                )
            if parent_decision is not None:
                review_rows.append(
                    {
                        "active": "上一决定仍在执行",
                        "completed": "上一决定已完成",
                        "blocked": "上一决定受阻",
                        "needs_revision": "上一决定需要调整",
                        "superseded": "上一决定已被替代",
                    }.get(
                        parent_decision.execution_status,
                        f"上一决定：{parent_decision.execution_status}",
                    )
                )
            if review.memory_updates:
                review_rows.append(
                    "记住的偏好："
                    + "；".join(
                        str(item.get("preference") or "")
                        for item in review.memory_updates
                        if isinstance(item, dict) and item.get("preference")
                    )
                )
            if review.policy_updates:
                review_rows.append(
                    "项目原则："
                    + "；".join(
                        str(item.get("policy") or "")
                        for item in review.policy_updates
                        if isinstance(item, dict) and item.get("policy")
                    )
                )
            agent_rows = [
                "Project Owner"
                + (f" · {review.actor_model}" if review.actor_model else " · 模型未报告")
            ]
            usage_line = token_usage_summary(
                "Project Owner", canonical_usage(review.actor_usage)
            )
            agent_rows.append(usage_line or "Token · Project Owner：后端未报告")
            details = []
            if review_rows:
                details.append(("Owner 判断依据", review_rows, False))
            details.append(("Agent 与用量详情", agent_rows, False))

        if decision is None and review.status == "completed":
            decision = projection_service.get_project_decision_by_request_item_id(
                project.id, str(review.request_item_id or "")
            )
        owner_review = None
        if review.status == "completed" and review.outcome == "decide":
            owner_review = {
                "outcome": review.outcome,
                "decision": review.decision,
                "decision_id": decision.id if decision is not None else "",
                "options": list(decision.options or []) if decision is not None else [],
                "reversible": bool(decision.reversible) if decision is not None else False,
                "chat_id": project.feishu_chat_id,
                "chat_type": project.feishu_chat_type,
            }

        card = build_content_card(
            str(report_card.get("title") or "本轮结果"),
            str(report_card.get("content") or ""),
            page=int(report_card.get("page") or 1),
            total=int(report_card.get("total") or 1),
            color=str(report_card.get("color") or "blue"),
            footer=[visible_status, execution_status_text],
            details=details,
            owner_review=owner_review,
        )
        try:
            await deliver_card(
                projection_service,
                self.feishu_bridge,
                dedupe_key=(
                    f"project-owner-source-status:{review.id}:"
                    f"{review.status}:{review.outcome}:{conclusion or ''}"
                ),
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="report",
                card=card,
                project_id=project.id,
                update_message_id=message_id,
                projection_key=projection_key,
                subject_type="root_turn",
                subject_id=str(review.native_turn_id or review.id),
                lifecycle_status=lifecycle,
                resulting_state="idle",
            )
        except Exception:
            # Card delivery has its own durable outbox retry lifecycle. It must
            # not roll a successfully completed Owner judgment back to failed,
            # or prevent a pending review from being scheduled.
            logger.warning(
                "Project Owner source card sync failed for %s",
                review.id,
                exc_info=True,
            )

    async def _queue_delivered_report_review(
        self,
        project: Project,
        *,
        turn_id: str,
        title: str,
        content: str,
        user_request: str,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        service: ProjectService | None = None,
        report_card: dict[str, Any] | None = None,
    ) -> None:
        """Persist then schedule report review outside the Root project lock."""

        review_service = service or self.service
        if not review_service.is_project_owner_enabled(project.id):
            return
        source_id = f"report:{turn_id}"
        review = review_service.ensure_project_owner_review_job(
            project.id,
            request_item_id=source_id,
            native_turn_id=turn_id,
            title=title,
            content=content,
            user_request=user_request,
            report_card=report_card,
            request_origin=request_origin,
            parent_decision_id=parent_decision_id,
        )
        if review.status == "pending":
            await self._sync_report_owner_review_card(
                project, review, service=review_service
            )
            schedule_project_owner_review(
                self.bind,
                self.runtime,
                self.settings,
                review.id,
            )

    async def _escalate_native_approval_to_user(
        self,
        project: Project,
        interaction: QuestionInteraction,
        questions: list[dict[str, Any]],
        *,
        request_key: str,
        owner_result: ProjectOwnerResult | None = None,
        pending_notice: str | None = None,
    ) -> None:
        """Turn one unresolved Owner review into an actual human approval.

        The original native approval remains the lifecycle source of truth.
        Updating that same unfinished card preserves timeline order while the
        routing fields make clear that the user was asked only after Owner
        could not reliably complete the approval.
        """

        current = self.service.get_question_interaction(interaction.id)
        if current is None or current.status != "pending":
            return
        current.requested_to = "user"
        current.escalated_to_user = True
        self.session.add(current)
        self.session.commit()
        if (
            self.feishu_bridge is None
            or not project.feishu_chat_id
            or not current.card_message_id
        ):
            return

        owner_decision = None
        if owner_result is not None:
            owner_decision = {
                "decision": owner_result.decision,
                "rationale": owner_result.rationale,
                "impact": owner_result.impact,
                "status": "requires_user",
            }
        card = build_question_batch_card(
            current.id,
            current.id,
            questions,
            "Kimi" if self.store.ensure(project.id).provider == "kimi_code" else "Codex",
            "native_approval",
            state="ROOT_AGENT_WAITING_USER",
            project_name=self._project_display_name(project),
            chat_id=project.feishu_chat_id,
            chat_type=project.feishu_chat_type,
            owner_decision=owner_decision,
            pending_notice=pending_notice,
        )
        try:
            await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"root-native-approval-escalated:{request_key}",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="question",
                card=card,
                project_id=project.id,
                update_message_id=current.card_message_id,
                projection_key=f"question:{current.id}",
                subject_type="question_interaction",
                subject_id=current.id,
                lifecycle_status="pending",
                resulting_state="waiting_user",
            )
        except Exception:
            logger.warning(
                "Native approval could not be switched to user confirmation",
                exc_info=True,
            )

    def _native_file_changes(
        self, project_id: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Recover public scope from the matching native item, never its diff.

        Codex v2 sends paths/kinds in item/started, not in the subsequent
        fileChange/requestApproval. Match the full identity, including child
        threads, so a reused item ID cannot borrow another request's scope.
        """
        if not all(params.get(key) for key in ("threadId", "turnId", "itemId")):
            return []
        meta = self.session.exec(
            select(EventLog.meta).where(
                EventLog.project_id == project_id,
                EventLog.meta["profile_type"].as_string() == "root_agent_event",
                EventLog.meta["kind"].as_string() == "item/started",
                EventLog.meta["thread_id"].as_string() == params["threadId"],
                EventLog.meta["turn_id"].as_string() == params["turnId"],
                EventLog.meta["payload"]["item"]["id"].as_string() == params["itemId"],
                EventLog.meta["payload"]["item"]["type"].as_string() == "fileChange",
            ).order_by(EventLog.id.desc()).limit(1)
        ).first()
        if not isinstance(meta, dict):
            return []
        changes = meta["payload"]["item"].get("changes")
        return [
            {"path": change.get("path"), "kind": change.get("kind")}
            for change in changes or []
            if isinstance(change, dict)
        ]

    async def _deliver_native_interaction_card(
        self,
        *,
        interaction: QuestionInteraction,
        delivery: dict[str, Any],
    ) -> str:
        """Keep the native request pending until its original card is visible.

        The outbox can finish a failed send independently. Re-entering its
        idempotent delivery boundary observes that success without posting a
        duplicate, then lets the waiting Owner/user flow resume.
        """

        retrying = False
        while True:
            if retrying:
                # The outbox drain uses another Session, so refresh its status.
                self.session.expire_all()
                notification = self.session.exec(
                    select(NotificationOutbox).where(
                        NotificationOutbox.dedupe_key == delivery["dedupe_key"]
                    )
                ).first()
                if notification is not None and notification.status == "terminal_failed":
                    raise RuntimeError(
                        "Native interaction card delivery permanently failed: "
                        f"{notification.last_error or notification.id}"
                    )
                if (
                    notification is not None
                    and notification.status == "sent"
                    and notification.provider_message_id
                ):
                    # The outbox already projected this card. Reading its ID
                    # directly avoids re-opening a card the user just answered.
                    interaction.card_message_id = notification.provider_message_id
                    self.session.add(interaction)
                    self.session.commit()
                    return notification.provider_message_id
            try:
                message_id = await deliver_card(
                    self.service, self.feishu_bridge, **delivery
                )
            except Exception as exc:
                if not is_retryable_delivery_error(exc):
                    raise
                if not retrying:
                    logger.warning(
                        "Native interaction card delivery interrupted; waiting to retry",
                        exc_info=True,
                    )
            else:
                if message_id:
                    interaction.card_message_id = message_id
                    self.session.add(interaction)
                    self.session.commit()
                    return message_id
            retrying = True
            await asyncio.sleep(_NATIVE_INTERACTION_CARD_RETRY_SECONDS)

    async def _request_native_approval(
        self,
        project_id: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Project a Codex v2 approval and return its native response shape."""

        project = self.service.require_project(project_id)
        item_id = str(params.get("itemId") or "").strip()
        callback_id = str(params.get("approvalId") or item_id).strip()
        if not item_id or not callback_id:
            raise RuntimeError("Codex approval request is incomplete")
        request_key = f"native-approval:{method}:{callback_id}"
        questions, response_builder = _native_approval_spec(
            method,
            params,
            file_changes=(
                self._native_file_changes(project_id, params)
                if method == "item/fileChange/requestApproval"
                else None
            ),
        )
        questions = [
            {**question, "request_kind": "approval"} for question in questions
        ]

        existing = self.service.get_project_decision_by_request_item_id(
            project.id, request_key
        )
        if existing is not None and existing.status == "applied":
            return dict(existing.answer_payload or {})
        owner_first = bool(
            self.feishu_bridge is not None
            and project.feishu_chat_id
            and self.service.is_project_owner_enabled(project.id)
        )
        interaction = await self.service.create_question_interaction(
            project.id,
            None,
            1,
            "ACTIVE",
            "root_agent",
            "native_approval",
            request_key,
            questions,
            request_item_id=request_key,
            requested_to="project_owner" if owner_first else "user",
            escalated_to_user=not owner_first,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingUserRequest(
            project.id,
            interaction.id,
            request_key,
            loop,
            future,
            response_builder,
        )
        with _PENDING_USER_REQUESTS_GUARD:
            _PENDING_USER_REQUESTS[request_key] = pending
        durable = self.store.ensure(project.id)
        self.store.set_turn(
            project.id,
            turn_id=durable.active_turn_id,
            status="owner_reviewing" if owner_first else "waiting_user",
            reason=questions[0]["question"],
        )

        card_delivered = False
        progress_suspended = False
        if self.feishu_bridge is not None and project.feishu_chat_id:
            progress_suspended = await self._suspend_progress_for_interaction(
                project.id
            )
            card = build_question_batch_card(
                interaction.id,
                interaction.id,
                questions,
                str(params.get("provider") or "Codex"),
                "native_approval",
                state="ROOT_AGENT_WAITING_USER",
                project_name=self._project_display_name(project),
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
                owner_reviewing=owner_first,
            )
            try:
                await self._deliver_native_interaction_card(
                    interaction=interaction,
                    delivery={
                        "dedupe_key": f"root-native-approval:{request_key}",
                        "recipient_id": project.feishu_chat_id,
                        "recipient_type": "chat_id",
                        "intent_type": "question",
                        "card": card,
                        "project_id": project.id,
                        "projection_key": f"question:{interaction.id}",
                        "subject_type": "question_interaction",
                        "subject_id": interaction.id,
                        "lifecycle_status": "pending",
                        "resulting_state": (
                            "owner_reviewing" if owner_first else "waiting_user"
                        ),
                    },
                )
            except (Exception, asyncio.CancelledError):
                if progress_suspended:
                    self._resume_progress_after_interaction(project.id)
                    progress_suspended = False
                if interaction.status == "pending":
                    interaction.status = "expired"
                    interaction.resolution_source = "card_delivery_interrupted"
                    self.session.add(interaction)
                    self.session.commit()
                with _PENDING_USER_REQUESTS_GUARD:
                    if _PENDING_USER_REQUESTS.get(request_key) is pending:
                        _PENDING_USER_REQUESTS.pop(request_key, None)
                raise
            else:
                card_delivered = True
                if owner_first:
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="owner_reviewing",
                        reason=f"AI Project Owner 正在审阅：{questions[0]['question']}",
                    )

        try:
            if future.done():
                return await future
            if card_delivered and owner_first:
                try:
                    owner_result = await self._project_owner_decide(project, questions)
                    if future.done():
                        return await future
                    owner_response = await self._apply_project_owner_decision(
                        project,
                        request_key,
                        questions,
                        owner_result,
                        interaction,
                        response_builder,
                    )
                    if owner_response is not None:
                        durable = self.store.ensure(project.id)
                        self.store.set_turn(
                            project.id,
                            turn_id=durable.active_turn_id,
                            status="running",
                        )
                        return owner_response
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="waiting_user",
                        reason=questions[0]["question"],
                    )
                    await self._escalate_native_approval_to_user(
                        project,
                        interaction,
                        questions,
                        request_key=request_key,
                        owner_result=owner_result,
                    )
                except Exception:
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="waiting_user",
                        reason=questions[0]["question"],
                    )
                    logger.warning(
                        "Project Owner could not review native approval; preserving user choice",
                        exc_info=True,
                    )
                    await self._escalate_native_approval_to_user(
                        project,
                        interaction,
                        questions,
                        request_key=request_key,
                        pending_notice=(
                            "AI Project Owner 本次审阅未完成，请你确认后继续。"
                        ),
                    )
            return await future
        finally:
            if progress_suspended:
                current_interaction = self.service.get_question_interaction(
                    interaction.id
                )
                if (
                    current_interaction is not None
                    and current_interaction.status != "pending"
                ):
                    self._resume_progress_after_interaction(project.id)
            with _PENDING_USER_REQUESTS_GUARD:
                current = _PENDING_USER_REQUESTS.get(request_key)
                if current is pending:
                    _PENDING_USER_REQUESTS.pop(request_key, None)

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
                    "request_kind": "question",
                }
            )

        existing = self.service.get_project_decision_by_request_item_id(
            project.id, item_id
        )
        if existing is not None and existing.status == "applied":
            return dict(existing.answer_payload or {"answers": {}})
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
            project.id,
            interaction.id,
            item_id,
            loop,
            future,
            _request_user_input_response,
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

        owner_eligible = not any(
            question["sensitivity"] == "secret" for question in questions
        )
        question_card_delivered = False
        progress_suspended = False
        if self.feishu_bridge is not None and project.feishu_chat_id:
            progress_suspended = await self._suspend_progress_for_interaction(
                project.id
            )
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
                await self._deliver_native_interaction_card(
                    interaction=interaction,
                    delivery={
                        "dedupe_key": f"root-question:{item_id}",
                        "recipient_id": project.feishu_chat_id,
                        "recipient_type": "chat_id",
                        "intent_type": "question",
                        "card": card,
                        "project_id": project.id,
                        "projection_key": f"question:{interaction.id}",
                        "subject_type": "question_interaction",
                        "subject_id": interaction.id,
                        "lifecycle_status": "pending",
                        "resulting_state": (
                            "owner_reviewing" if owner_eligible else "waiting_user"
                        ),
                    },
                )
            except (Exception, asyncio.CancelledError):
                if progress_suspended:
                    self._resume_progress_after_interaction(project.id)
                    progress_suspended = False
                if interaction.status == "pending":
                    interaction.status = "expired"
                    interaction.resolution_source = "card_delivery_interrupted"
                    self.session.add(interaction)
                    self.session.commit()
                with _PENDING_USER_REQUESTS_GUARD:
                    if _PENDING_USER_REQUESTS.get(item_id) is pending:
                        _PENDING_USER_REQUESTS.pop(item_id, None)
                raise
            else:
                question_card_delivered = True

        try:
            if future.done():
                return await future
            can_consult_owner = bool(
                question_card_delivered
                and owner_eligible
                and self.service.is_project_owner_enabled(project.id)
            )
            if can_consult_owner:
                self.store.set_turn(
                    project.id,
                    turn_id=durable.active_turn_id,
                    status="owner_reviewing",
                    reason=f"AI Project Owner 正在审阅：{questions[0]['question']}",
                )
                try:
                    owner_result = await self._project_owner_decide(project, questions)
                    if future.done():
                        return await future
                    owner_response = await self._apply_project_owner_decision(
                        project,
                        item_id,
                        questions,
                        owner_result,
                        interaction,
                    )
                    if owner_response is not None:
                        durable = self.store.ensure(project.id)
                        self.store.set_turn(
                            project.id,
                            turn_id=durable.active_turn_id,
                            status="running",
                        )
                        return owner_response
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="waiting_user",
                        reason=questions[0]["question"],
                    )
                    # The native question already contains the unresolved
                    # choice. A deferred Owner answer adds no new judgment, so
                    # do not duplicate it as a second "AI decision" card.
                except Exception:
                    self.store.set_turn(
                        project.id,
                        turn_id=durable.active_turn_id,
                        status="waiting_user",
                        reason=questions[0]["question"],
                    )
                    logger.warning(
                        "Project Owner could not decide; preserving native user question",
                        exc_info=True,
                    )

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
                        recalled = False
                        if interaction.card_message_id:
                            try:
                                await self.feishu_bridge.delete_message(
                                    interaction.card_message_id
                                )
                                recalled = True
                            except Exception:
                                logger.warning(
                                    "Timed-out question card could not be recalled",
                                    exc_info=True,
                                )
                        self.service.upsert_card_projection(
                            projection_key=f"question:{interaction.id}",
                            project_id=project.id,
                            subject_type="question_interaction",
                            subject_id=interaction.id,
                            card_kind="question",
                            provider_message_id=interaction.card_message_id,
                            lifecycle_status=(
                                "recalled" if recalled else "closed_unrecalled"
                            ),
                            selected_action="auto_resolution",
                            resolved_by="codex_auto_resolution",
                            resulting_state="running",
                        )
                        await deliver_card(
                            self.service,
                            self.feishu_bridge,
                            dedupe_key=f"root-question-timeout:{item_id}",
                            recipient_id=project.feishu_chat_id,
                            recipient_type="chat_id",
                            intent_type="question",
                            card=build_content_card(
                                f"[{self._project_display_name(project)}] 已继续",
                                "用户未在限定时间内回复；主助手将按最佳判断继续。",
                                color="orange",
                            ),
                            project_id=project.id,
                            projection_key=f"question:{interaction.id}:auto-resolution",
                            subject_type="question_resolution",
                            subject_id=interaction.id,
                            lifecycle_status="auto_resolved",
                            resolved_by="codex_auto_resolution",
                            resulting_state="running",
                        )
                    return {"answers": {}}
            return await future
        finally:
            if progress_suspended:
                current_interaction = self.service.get_question_interaction(
                    interaction.id
                )
                if (
                    current_interaction is not None
                    and current_interaction.status != "pending"
                ):
                    self._resume_progress_after_interaction(project.id)
            with _PENDING_USER_REQUESTS_GUARD:
                current = _PENDING_USER_REQUESTS.get(item_id)
                if current is pending:
                    _PENDING_USER_REQUESTS.pop(item_id, None)

    async def _publish_final(
        self,
        project: Project,
        result: TurnResult,
        *,
        visible_request: str = "",
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> None:
        # Codex may emit public commentary before a tool call and retain that
        # text as ``final_message`` when the turn is later interrupted. It is
        # progress evidence, not a successful final answer. The terminal
        # progress projection already reports interrupted/failed faithfully.
        if result.status != "completed":
            return
        raw_content = result.final_message or ""
        content = rewrite_project_file_links(
            raw_content, project, self.settings
        )
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
            timing = self._turn_timings.get(project.id)
            if timing is not None:
                timing.mark_once("final_card")
            return
        chunks = split_markdown_sections(content)
        if not chunks:
            return
        delivered_chunks = 0
        last_message_id: str | None = None
        for index, chunk in enumerate(chunks, start=1):
            card = build_content_card(
                f"[{self._project_display_name(project)}] 本轮结果",
                chunk,
                page=index,
                total=len(chunks),
                color="blue",
            )
            try:
                message_id = await deliver_card(
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
                delivered_chunks += 1
                if index == len(chunks):
                    last_message_id = message_id
            except Exception:
                logger.warning("Root result card delivery failed", exc_info=True)
        if delivered_chunks == len(chunks):
            timing = self._turn_timings.get(project.id)
            if timing is not None:
                # The user-visible report is complete here. Owner review runs
                # afterwards and is measured separately as post-processing.
                timing.mark_once("final_card")
            review_context: dict[str, Any] = {}
            if request_origin != "human":
                review_context["request_origin"] = request_origin
            if parent_decision_id:
                review_context["parent_decision_id"] = parent_decision_id
            await self._queue_delivered_report_review(
                project,
                turn_id=result.turn_id,
                title=f"[{self._project_display_name(project)}] 本轮结果",
                content=content,
                user_request=visible_request,
                report_card={
                    "title": f"[{self._project_display_name(project)}] 本轮结果",
                    "content": chunks[-1],
                    "page": len(chunks),
                    "total": len(chunks),
                    "color": "blue",
                    "projection_key": f"root-result:{result.turn_id}:{len(chunks)}",
                    "message_id": last_message_id,
                },
                **review_context,
            )

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
                recovery_session.add(EventLog(
                    project_id=project_id,
                    level="ERROR",
                    message="Root turn failed",
                    meta={
                        "error_type": type(exc).__name__,
                        "error": redact_text(str(exc))[:1200],
                    },
                ))
                recovery_session.commit()
        except Exception:
            logger.error(
                "Failed to recover durable Root Agent state for %s",
                project_id,
                exc_info=True,
            )

    async def _publish_failure_safely(
        self,
        project_id: str,
        exc: Exception,
        *,
        user_request: str,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> None:
        """Publish failure from an independent transaction boundary."""

        try:
            with Session(self.bind) as delivery_session:
                service = ProjectService(delivery_session)
                project = service.require_project(project_id)
                await self._publish_failure(
                    project,
                    exc,
                    service=service,
                    user_request=user_request,
                    request_origin=request_origin,
                    parent_decision_id=parent_decision_id,
                )
        except Exception:
            logger.warning("Root failure projection failed", exc_info=True)

    async def _publish_failure(
        self,
        project: Project,
        exc: Exception,
        *,
        service: ProjectService | None = None,
        user_request: str,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
    ) -> None:
        delivery_service = service or self.service
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        failure_id = generate_id("root-failure")
        visible_error = _compact_text(redact_text(str(exc)), limit=1_200)
        failure_content = (
            "项目助手未能完成本轮工作；项目未被标记为完成。"
            "系统已记录错误，可以直接重试。"
            f"\n\n**错误信息**：{visible_error}"
            if visible_error
            else (
                "项目助手未能完成本轮工作；项目未被标记为完成。"
                "系统已记录错误，可以直接重试。"
            )
        )
        failure_projection_key = f"root-failure:{failure_id}:report"
        try:
            self._update_progress(
                project.id,
                label="本轮未完成，已停止；可以直接重试",
                color="red",
                detail="",
            )
            state = self._progress.get(project.id)
            if state is not None:
                if state.projection_suspended:
                    self._resume_progress_after_interaction(project.id)
                state.status = "failed"
                _complete_progress_big_step(state, "failed")
            projection_key = (
                _progress_projection_key(project.id, state)
                if state is not None
                else f"root-agent:{project.id}:activity:{failure_id}:terminal"
            )
            projection = delivery_service.get_card_projection(projection_key)
            await deliver_card(
                delivery_service,
                self.feishu_bridge,
                dedupe_key=f"{failure_id}:progress",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="progress",
                card=(
                    _render_progress_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        state,
                        color="red",
                    )
                    if state is not None
                    else build_content_card(
                        f"[{self._project_display_name(project)}] 项目进展",
                        "**当前进展**：本轮未完成，已停止；可以直接重试。",
                        color="red",
                    )
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
                dedupe_key=f"{failure_id}:report",
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="report",
                card=build_content_card(
                    f"[{self._project_display_name(project)}] 本轮未完成",
                    failure_content,
                    color="red",
                ),
                project_id=project.id,
                projection_key=failure_projection_key,
                subject_type="root_turn",
                subject_id=project.id,
                lifecycle_status="failed",
                resulting_state="idle",
            )
        except Exception:
            logger.warning("Root failure card delivery failed", exc_info=True)

    @staticmethod
    async def _release_root(project_id: str, root: ProjectLeadRuntime) -> None:
        # Keep ownership until shutdown is confirmed. A failed close must not
        # allow a subsequent request to create a second writer.
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
            if handle is not None and handle.runtime is root:
                handle.closing = True
        if handle is None or handle.loop is asyncio.get_running_loop():
            await root.close()
        elif handle.loop.is_running():
            pending = asyncio.run_coroutine_threadsafe(root.close(), handle.loop)
            await asyncio.wrap_future(pending)
        else:
            raise RuntimeError("Cannot confirm previous provider stopped: its event loop is unavailable")
        with _ROOT_THREADS_GUARD:
            handle = _ROOT_THREADS.get(project_id)
            if handle is not None and handle.runtime is root and project_id not in _PROJECT_RESETTING:
                _ROOT_THREADS.pop(project_id, None)

    @staticmethod
    def _project_display_name(project: Project) -> str:
        name = str(project.name or "").strip()
        if not name or "\n" in name:
            return "当前项目"
        if re.fullmatch(r"proj_\d{8}_\d{6}_[0-9a-f]{16}", name):
            return "当前项目"
        return name if len(name) <= 16 else f"{name[:16]}…"


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

    selected: dict[str, str] = {}
    for question in service.get_questions_for_interaction(interaction.id):
        answer = service.get_question_answer(question.id)
        if answer is None:
            continue
        value = str(answer.answer_label or answer.answer or "").strip()
        selected[question.question_id] = value
    response = pending.response_builder(selected)

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
    """Stop active native turns, then close their transports during shutdown."""

    current = asyncio.get_running_loop()
    with _ROOT_THREADS_GUARD:
        handles = list(_ROOT_THREADS.items())
    shutdown_rounds: dict[str, tuple[Any, str]] = {}
    for project_id, handle in handles:
        if handle.engine is None:
            continue
        try:
            with Session(handle.engine.bind) as session:
                row = RootSessionStore(session).ensure(project_id)
                if (
                    row.status in {"running", "owner_reviewing", "waiting_user"}
                    and row.active_history_round_id
                ):
                    shutdown_rounds[project_id] = (
                        handle.engine.bind,
                        row.active_history_round_id,
                    )
        except Exception:
            logger.warning(
                "Could not capture active Root round before shutdown: %s",
                project_id,
                exc_info=True,
            )

    async def interrupt_before_close(handle: _RootHandle) -> None:
        """Use Codex's terminal interrupt boundary before ending its process.

        Killing an app-server while its turn is live leaves the persisted Codex
        thread with an active writer.  A later ``thread/resume`` then cannot
        attach, so restart recovery repeatedly fails instead of recovering the
        user's work.  ``interrupt`` waits for Codex's terminal event; this
        bounded wrapper only falls back to close when that confirmation cannot
        arrive during shutdown.
        """

        turn_id = handle.runtime.active_turn_id
        if not turn_id:
            return
        try:
            if handle.loop is current:
                await asyncio.wait_for(handle.runtime.interrupt(turn_id), timeout=20)
            elif handle.loop.is_running():
                pending = asyncio.run_coroutine_threadsafe(
                    handle.runtime.interrupt(turn_id), handle.loop
                )
                await asyncio.wait_for(asyncio.wrap_future(pending), timeout=20)
            else:
                logger.warning(
                    "Cannot interrupt active Root turn before shutdown: event loop is unavailable"
                )
        except Exception:
            logger.warning(
                "Native Root turn did not confirm interruption before shutdown",
                exc_info=True,
            )

    # Do this before cancelling host tasks.  Their readers receive the native
    # terminal event that clears Codex's writer ownership.
    await asyncio.gather(
        *(interrupt_before_close(handle) for _, handle in handles),
        return_exceptions=True,
    )

    with _ROOT_TURN_TASKS_GUARD:
        turn_tasks = list(_ROOT_TURN_TASKS)
        _ROOT_TURN_TASKS.clear()
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

    for project_id, (bind, history_round_id) in shutdown_rounds.items():
        try:
            with Session(bind) as session:
                if RootSessionStore(session).mark_shutdown_interrupted_for_recovery(
                    project_id, history_round_id
                ):
                    logger.warning(
                        "Queued gracefully interrupted Root turn for restart recovery: %s",
                        project_id,
                    )
        except Exception:
            logger.warning(
                "Could not queue interrupted Root turn for restart recovery: %s",
                project_id,
                exc_info=True,
            )

    with _ROOT_THREADS_GUARD:
        _ROOT_THREADS.clear()
    for _, handle in handles:
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
