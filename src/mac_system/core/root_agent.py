"""MAC host adapter for a persistent Codex root Agent.

The control loop and multi-Agent behavior are Codex 0.144.1 behavior, not a
MAC workflow engine. MAC supplies transport, durable projections and the
Feishu implementation of Codex's native ``request_user_input`` request.
Reference: openai/codex commit 44918ea10c0f99151c6710411b4322c2f5c96bea.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
from mac_system.config import Settings
from mac_system.core.lock_manager import get_default_lock_manager
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService, generate_id
from mac_system.core.root_sessions import RootSessionStore
from mac_system.feishu.card import build_content_card, build_question_batch_card
from mac_system.models import Approval, Project
from mac_system.utils.formatting import split_markdown_sections
from mac_system.utils.redaction import redact_text

if TYPE_CHECKING:
    from mac_system.feishu.bridge import FeishuBridge


logger = logging.getLogger(__name__)
_ROOT_PROFILE = "codex-native-root-v1"


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
        if item_type == "reasoning":
            return None
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
                "senderThreadId",
                "receiverThreadIds",
            )
            if key in item
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
    }
    wording = labels.get(item_type)
    if wording is None:
        return None
    if started:
        return wording[0], "purple", "running"
    if failed:
        return wording[2], "orange", "running"
    return wording[1], "green", "running"


def _native_status_card(
    event_kind: str, payload: dict[str, Any]
) -> tuple[str, str, str] | None:
    """Translate official non-item turn status into the shared progress card."""

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

    async def run(self, project_id: str, trigger: str) -> TurnResult:
        project = self.service.require_project(project_id)
        self.project = project
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            self.session.expire_all()
            project = self.service.require_project(project_id)
            root: CodexAppServerThread | None = None
            try:
                root = await self._root_thread(project)
                self.store.set_turn(project_id, turn_id=None, status="running")
                self.service.set_execution_status(project_id, "running")
                result = await root.run_turn(trigger)
                self.store.set_turn(project_id, turn_id=None, status="idle")
                refreshed = self.service.require_project(project_id)
                if refreshed.execution_status != "waiting_user":
                    self.service.set_execution_status(project_id, "idle")
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
                self._recover_failed_turn(project_id, exc)
                if isinstance(exc, Exception):
                    await self._publish_failure_safely(project_id, exc)
                raise
            finally:
                if root is not None:
                    # The persisted thread id, not a process-local transport,
                    # is the resumable Codex session boundary.
                    try:
                        await self._release_root(project_id, root)
                    except Exception:
                        logger.warning(
                            "Failed to close native Codex transport for %s",
                            project_id,
                            exc_info=True,
                        )

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

    async def _root_thread(self, project: Project) -> CodexAppServerThread:
        current_loop = asyncio.get_running_loop()
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
                tools=self._extension_tools(project),
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
                    "features.multi_agent_v2": True,
                    "features.default_mode_request_user_input": True,
                    # Official Codex workspace-write config. MAC projects are
                    # explicitly authorized to run their real API/E2E checks.
                    "sandbox_workspace_write.network_access": True,
                },
                # Use the normal Codex home so installed skills and standard
                # configuration behave exactly as they do in Codex.
                codex_home=None,
            )
            with _ROOT_THREADS_GUARD:
                _ROOT_THREADS[project.id] = _RootHandle(root, current_loop)
        else:
            root.tools = {tool.name: tool for tool in self._extension_tools(project)}
            root.event_handler = lambda event: self._on_event(project.id, event)
            root.server_request_handler = lambda method, params: self._on_server_request(
                project.id, method, params
            )

        durable = self.store.ensure(project.id)
        profile_current = (project.context or {}).get("root_agent_profile") == _ROOT_PROFILE
        if root.thread_id is None:
            if durable.native_thread_id and durable.status != "failed" and profile_current:
                await root.resume_thread(durable.native_thread_id)
            else:
                native_id = await root.create_thread()
                self.store.set_native_thread(project.id, native_id)
                project.context = {
                    **(project.context or {}),
                    "orchestration_mode": "root_agent",
                    "root_agent_profile": _ROOT_PROFILE,
                }
                project.current_state = "ACTIVE"
                project.updated_at = _now()
                self.session.add(project)
                self.session.commit()
        return root

    def _extension_tools(self, project: Project) -> list[DynamicTool]:
        """Expose one generic bridge to MAC's optional coding CLI providers."""

        providers = sorted(self.runtime.available_providers())
        if not providers:
            return []
        return [
            DynamicTool(
                name="run_external_agent",
                description=(
                    "Run one task with an optional external coding-agent CLI. "
                    "Codex native subagents remain available through Codex's own tools."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["provider", "task_name", "message", "access"],
                    "properties": {
                        "provider": {"type": "string", "enum": providers},
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
            task_name = str(arguments["task_name"])
            message = str(arguments["message"])
            access = str(arguments["access"])
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
            )
            self.store.update_agent(agent, status="running")
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
                timeout_seconds=self.settings.agent_timeout_seconds,
                task_id=agent.id,
            )
            answer = result.raw_result
            if answer is None and result.output_json is not None:
                answer = json.dumps(result.output_json, ensure_ascii=False)
            answer = redact_text(answer or "")
            payload = {
                "success": result.success,
                "provider": provider,
                "task_name": task_name,
                "message": answer,
                "cost": result.cost_info,
                "tool_calls": result.tool_calls,
            }
            self.store.update_agent(
                agent,
                status="completed" if result.success else "failed",
                latest_message=answer or None,
                result=payload,
            )
            estimated_cost = (result.cost_info or {}).get("estimated_cost_usd")
            if isinstance(estimated_cost, (int, float)):
                project.total_cost_usd = float(project.total_cost_usd or 0) + float(
                    estimated_cost
                )
                project.updated_at = _now()
                self.session.add(project)
                self.session.commit()
            return payload

        return await self.store.execute_tool_once(
            project.id, context, arguments, execute
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
        if self.feishu_bridge is None:
            return
        try:
            with Session(self.bind) as projection_session:
                service = ProjectService(projection_session)
                project = service.require_project(project_id)
                if not project.feishu_chat_id:
                    return

                label, color, lifecycle = activity

                card = build_content_card(
                    f"[{self._project_display_name(project)}] 项目进展",
                    f"**当前进展**：{label}",
                    color=color,
                )
                projection_key = f"root-agent:{project.id}:activity"
                projection = service.get_card_projection(projection_key)
                await deliver_card(
                    service,
                    self.feishu_bridge,
                    dedupe_key=f"{projection_key}:{event_identity}:{event.kind}",
                    recipient_id=project.feishu_chat_id,
                    recipient_type="chat_id",
                    intent_type="progress",
                    card=card,
                    project_id=project.id,
                    update_message_id=(
                        projection.provider_message_id
                        if projection is not None
                        else None
                    ),
                    projection_key=projection_key,
                    subject_type="root_agent",
                    subject_id=project.id,
                    lifecycle_status=lifecycle,
                    resulting_state=project.execution_status,
                )
        except Exception:
            logger.warning("Failed to project native Agent activity", exc_info=True)

    async def _publish_turn_status(
        self, project: Project, result: TurnResult
    ) -> None:
        """Close the shared progress projection when the native turn finishes."""

        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        projection_key = f"root-agent:{project.id}:activity"
        projection = self.service.get_card_projection(projection_key)
        label, color, lifecycle = _turn_completion_card(result.status)
        await deliver_card(
            self.service,
            self.feishu_bridge,
            dedupe_key=f"{projection_key}:{result.turn_id}:completed",
            recipient_id=project.feishu_chat_id,
            recipient_type="chat_id",
            intent_type="progress",
            card=build_content_card(
                f"[{self._project_display_name(project)}] 项目进展",
                f"**当前进展**：{label}",
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

        approval = await self.service.create_approval(
            project.id,
            None,
            "ROOT_AGENT_WAITING_USER",
            "input",
            questions[0]["question"],
            [],
            payload={"root_request_item_id": item_id},
            enforce_current_state=False,
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
            approval_id=approval.id,
            requested_to="user",
            escalated_to_user=True,
        )
        approval.payload = {
            **(approval.payload or {}),
            "question_interaction_id": interaction.id,
            "root_request_item_id": item_id,
        }
        self.session.add(approval)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = _PendingUserRequest(
            project.id, interaction.id, item_id, loop, future
        )
        with _PENDING_USER_REQUESTS_GUARD:
            _PENDING_USER_REQUESTS[item_id] = pending
        self.service.set_execution_status(
            project.id,
            "waiting_user",
            reason=questions[0]["question"],
            commit=False,
        )
        self.session.commit()
        durable = self.store.ensure(project.id)
        self.store.set_turn(
            project.id,
            turn_id=durable.active_turn_id,
            status="waiting_user",
        )

        if self.feishu_bridge is not None and project.feishu_chat_id:
            card = build_question_batch_card(
                approval.id,
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
                approval.payload = {
                    **(approval.payload or {}),
                    "question_batch_message_id": message_id,
                }
                self.session.add(approval)
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
                    await self.service.respond_approval(
                        approval.id,
                        "auto_resolution",
                        "codex_auto_resolution",
                        commit=False,
                    )
                    self.service.set_execution_status(
                        project.id, "running", commit=False
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
                            update_message_id=str(
                                (approval.payload or {}).get(
                                    "question_batch_message_id"
                                )
                                or ""
                            )
                            or None,
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
                )
                ProjectService(recovery_session).set_execution_status(
                    project_id,
                    "idle",
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
                    "**当前进展**：本轮未完成，已停止；可以直接重试。",
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
    if interaction is None or not interaction.approval_id:
        return False
    approval = service.session.get(Approval, interaction.approval_id)
    if approval is None:
        return False
    item_id = str((approval.payload or {}).get("root_request_item_id") or "")
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
    service.set_execution_status(interaction.project_id, "running")
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
