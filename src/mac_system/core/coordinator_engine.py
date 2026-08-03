"""The MAC dynamic delivery loop and its seven Coordinator host tools."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import json
import logging
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from mac_system.agents.coordinator_runtime import (
    CodexAppServerCoordinator,
    CoordinatorEvent,
    CoordinatorTool,
    CoordinatorToolContext,
    CoordinatorTurnResult,
)
from mac_system.agents.runtime import AgentRuntime
from mac_system.config import Settings
from mac_system.core.coordinator_sessions import CoordinatorSessionStore
from mac_system.core.lock_manager import get_default_lock_manager
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.feishu import FeishuBridge
from mac_system.feishu.card import build_content_card, build_question_batch_card
from mac_system.models import CoordinatorAgentSession, Project
from mac_system.utils.formatting import split_markdown_sections


logger = logging.getLogger(__name__)
_ROOT_CAPABILITY_PROFILE = "codex-delegation-only-v2-isolated-home"

@dataclass
class _RootHandle:
    runtime: CodexAppServerCoordinator
    loop: asyncio.AbstractEventLoop


@dataclass
class _AgentRunHandle:
    project_id: str
    task: asyncio.Task[None] | ConcurrentFuture[None]
    loop: asyncio.AbstractEventLoop


_ROOT_RUNTIMES: dict[str, _RootHandle] = {}
_ROOT_RUNTIME_GUARD = threading.Lock()
_AGENT_RUNS: dict[str, _AgentRunHandle] = {}
_AGENT_RUN_GUARD = threading.Lock()
_COORDINATOR_WAKEUPS: dict[str, _AgentRunHandle] = {}
_COORDINATOR_WAKEUP_GENERATIONS: dict[str, int] = {}
_COORDINATOR_WAKEUP_GUARD = threading.Lock()
_SERVICE_LOOP: asyncio.AbstractEventLoop | None = None
_SERVICE_LOOP_GUARD = threading.Lock()


def register_coordinator_service_loop(
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Bind background Coordinator work to the application-owned event loop.

    Feishu's SDK invokes handlers on short-lived event loops. A child Agent
    spawned from one of those handlers must outlive the handler, so its task
    belongs to FastAPI's lifespan loop rather than the caller's loop.
    """

    global _SERVICE_LOOP
    with _SERVICE_LOOP_GUARD:
        _SERVICE_LOOP = loop


def _submit_background(
    coroutine: Any,
    *,
    name: str,
) -> tuple[asyncio.Task[Any] | ConcurrentFuture[Any], asyncio.AbstractEventLoop]:
    """Submit durable background work to the service loop when available."""

    current = asyncio.get_running_loop()
    with _SERVICE_LOOP_GUARD:
        target = _SERVICE_LOOP
    if target is None or target.is_closed() or not target.is_running():
        target = current
    if target is current:
        return asyncio.create_task(coroutine, name=name), target
    return asyncio.run_coroutine_threadsafe(coroutine, target), target


async def _join_foreign_agent_task(task: asyncio.Future[Any]) -> Any:
    """Observe a service-loop Task without transferring cancellation to it."""

    return await asyncio.shield(task)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _root_instructions() -> str:
    return """You are the sole Coordinator for one MAC software project.

You decide dynamically what to do next. Do not produce a workflow graph and do
not use fixed phases. Use the host tools to inspect/implement/verify through
bounded agents. Agent results return to this same session.

Rules:
- You never modify project files yourself. Delegate file work with mac_spawn_agent.
- Only you may call mac_request_user_input. First answer from known project facts or
  ask an agent to inspect; ask the user only for preferences, authorization, or
  external facts unavailable to the system.
- Prefer 2-3 concrete choices and mark one recommendation when a choice exists.
- Send feedback to the same agent with mac_send_message when follow-up is useful.
- Do not call mac_send_message while that Agent is still running. Wait for its
  terminal result first; MAC rejects concurrent continuations so one Agent
  cannot have two tasks writing the same worktree.
- If a failed write Agent reports retained_worktree=true, continue that same
  Agent with mac_send_message so its unmerged files are preserved. Do not spawn
  a replacement writer from the canonical workspace.
- Use mac_report for user-visible progress and meaningful results, not hidden chain
  of thought or raw tool logs.
- mac_list_agents is compact by default. When one terminal Agent's exact evidence
  is needed, request only that agent with include_full=true; never load every
  historical full result into one turn.
- Agent lifecycle cards are emitted from durable spawn/send/wait results. Never
  use report to announce intended or supposedly running Agent work. Report only
  facts already returned by a host tool, and include the real task names.
- A result.host_delivery receipt is authoritative MAC host state. If it says
  merged=true, the listed canonical_head already contains the Agent branch;
  do not trust contradictory Agent prose or spawn a recovery integrator.
- Use a read_only agent for independent verification after material changes.
- When the objective references another local project or requests full parity,
  your first work calls must spawn read-only capability inventory and
  user-visible behavioral-probe agents. Do not report planned work before the
  spawn results. Then pass only returned evidence to implementation agents. Do
  not reduce "complete" to examples named in the user message, and do not ask
  the user for facts that a bounded agent can inspect locally.
- Call mac_complete_project only when the requested outcome is implemented and
  independently verified. Never declare success from an agent claim alone.
- Keep context minimal: pass each agent its objective, relevant confirmed facts,
  unresolved evidence, boundaries, and completion condition only.
"""


def _agent_contract(objective: str, access: str) -> str:
    return f"""You are a bounded coding agent controlled by MAC's Coordinator.

Objective:
{objective}

Access: {access}

Work directly in the supplied repository. Investigate enough to solve the
objective; do not ask the user and do not invent external facts. If information
is unavailable, return a blocker for the Coordinator. Do not decide the global
workflow. For write access, make the requested changes and run proportionate
verification. For read_only access, do not modify delivery files.

Return exactly one JSON object and no Markdown. Use this small envelope (the
contents of the arrays may be strings or structured evidence objects):
{{"status":"completed|blocked|failed","summary":"concise outcome",
  "changes":[],"verification":[],"blockers":[]}}
Use completed only when your bounded objective is actually satisfied. Keep
evidence concise and factual.
"""


def _native_agent_instructions(access: str) -> str:
    return f"""You are a bounded coding agent controlled by MAC's Coordinator.
Access: {access}.

Work directly in the supplied repository and complete the objective in this
thread. Investigate, implement, and verify as needed. Do not ask the user; if an
external fact is unavailable, report the exact blocker to the Coordinator. Do
not decide global workflow. For read_only access, never modify delivery files.
Treat the supplied repository, its declared toolchain, and bounded executable
lookups as the discovery boundary. Do not recursively scan the user's home,
system roots, sibling repositories, or unrelated volumes to hunt for runtimes,
dependencies, credentials, or evidence. Prefer repository configuration,
explicit paths in the objective, PATH lookups, and narrowly targeted probes. If
those are insufficient, report the missing environment fact to the Coordinator.
Run only one resource-intensive verification command at a time and wait for its
result. If output capture is delayed or absent, inspect the existing process and
its result artifact; never launch an equivalent verification concurrently.
Your final message must concisely state outcome, changed files, verification and
remaining blockers. The Coordinator may send follow-up evidence to this same
thread later.
"""


def _agent_uses_isolated_worktree(access: str, provider: str) -> bool:
    """Give native verifiers a disposable writable checkout for real tests.

    Codex's read-only sandbox also blocks temporary databases and pytest's
    scratch files. A verifier still has read-only *delivery* access when it
    runs in an isolated worktree whose changes are always discarded.
    """
    return access == "write" or (access == "read_only" and provider == "codex")


class CoordinatorEngine:
    """Bind a durable MAC project to a native root Coordinator session."""

    def __init__(
        self,
        session: Session,
        runtime: AgentRuntime,
        worktrees: WorktreeManager,
        settings: Settings,
        *,
        feishu_bridge: FeishuBridge | None = None,
    ) -> None:
        self.session = session
        self.service = ProjectService(session)
        self.store = CoordinatorSessionStore(session)
        self.runtime = runtime
        self.worktrees = worktrees
        self.settings = settings
        self.feishu_bridge = feishu_bridge
        self.project: Project | None = None

    async def run(self, project_id: str, trigger: str) -> CoordinatorTurnResult:
        project = self.service.require_project(project_id)
        if project.context.get("orchestration_mode") != "agent_loop":
            raise ValueError(f"Project {project_id} does not use agent_loop")
        self.project = project
        lock = await get_default_lock_manager().acquire(project_id)
        async with lock:
            durable_before = self.store.ensure(project_id)
            recovering_failed_thread = (
                durable_before.status == "failed"
                or (project.context or {}).get("coordinator_capability_profile")
                != _ROOT_CAPABILITY_PROFILE
            )
            root = await self._root_runtime(project)
            if recovering_failed_thread:
                brief = (project.context or {}).get("confirmed_brief") or {}
                agents = [
                    {
                        "task_name": item.task_name,
                        "status": item.status,
                        "summary": item.latest_message,
                    }
                    for item in self.store.list_agents(project_id)
                ]
                trigger = (
                    "Recover this project in a fresh Coordinator thread. The prior "
                    "provider thread failed and is not authoritative. Reconcile durable "
                    "MAC state with mac_list_agents before acting.\n"
                    f"Project objective: {project.description or ''}\n"
                    "Confirmed context: "
                    + json.dumps(brief, ensure_ascii=False)
                    + "\nDurable agent summary: "
                    + json.dumps(agents, ensure_ascii=False)
                    + "\nCurrent user trigger: "
                    + trigger
                )
            self.store.set_turn(project_id, turn_id=None, status="running")
            self.service.set_execution_status(project_id, "coordinating")
            try:
                result = await root.run_turn(trigger)
            except Exception as exc:
                self.store.set_turn(
                    project_id, turn_id=None, status="failed", error=str(exc)
                )
                self._settle_project_after_root_turn(
                    project_id, reason=f"Coordinator failed: {exc}"
                )
                await self._release_root(project_id, root)
                raise
            try:
                visible_tool_used = any(
                    event.kind == "tool_call"
                    and str(event.payload.get("tool") or "")
                    in {"mac_report", "mac_request_user_input", "mac_complete_project"}
                    for event in result.events
                )
                if result.final_message and not visible_tool_used:
                    await self._report(
                        {
                            "title": f"[{project.name or project.id}] Coordinator",
                            "message": result.final_message,
                            "kind": "result",
                        },
                        f"turn:{result.turn_id}:final",
                    )
                self.store.set_turn(project_id, turn_id=None, status="idle")
                refreshed = self.service.require_project(project_id)
                if refreshed.status == "active" and refreshed.execution_status not in {
                    "waiting_user",
                    "waiting_external",
                }:
                    self._settle_project_after_root_turn(project_id)
            finally:
                # Feishu handlers run on short-lived event loops. Keep the
                # provider thread durable, but close the stdio adapter at each
                # turn boundary; the next trigger uses thread/resume.
                await self._release_root(project_id, root)
            return result

    def _settle_project_after_root_turn(
        self, project_id: str, *, reason: str | None = None
    ) -> None:
        """Derive project activity from durable Tasks at a root turn boundary."""
        status = (
            "running_agent"
            if self.service._running_tasks(project_id)
            else "idle"
        )
        self.service.set_execution_status(project_id, status, reason=reason)

    async def steer(self, project_id: str, text: str) -> bool:
        row = self.store.ensure(project_id)
        with _ROOT_RUNTIME_GUARD:
            handle = _ROOT_RUNTIMES.get(project_id)
        if handle is None or not row.active_turn_id:
            return False
        current = asyncio.get_running_loop()
        if handle.loop is current:
            await handle.runtime.steer(row.active_turn_id, text)
        elif handle.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                handle.runtime.steer(row.active_turn_id, text), handle.loop
            )
            await asyncio.wrap_future(future)
        else:
            return False
        return True

    async def _root_runtime(self, project: Project) -> CodexAppServerCoordinator:
        current_loop = asyncio.get_running_loop()
        with _ROOT_RUNTIME_GUARD:
            handle = _ROOT_RUNTIMES.get(project.id)
        root = handle.runtime if handle is not None else None
        tools = self._tools(project)
        if handle is not None and handle.loop is not current_loop:
            # A native subprocess and its stream readers belong to one event
            # loop. Persisted thread identity allows us to close the adapter on
            # its owner loop and resume it safely on the caller's loop.
            if handle.loop.is_running():
                future = asyncio.run_coroutine_threadsafe(root.close(), handle.loop)
                await asyncio.wrap_future(future)
            root = None
            with _ROOT_RUNTIME_GUARD:
                _ROOT_RUNTIMES.pop(project.id, None)
        if root is None:
            root = CodexAppServerCoordinator(
                codex_bin=self.settings.codex_bin,
                cwd=Path(project.workspace_path),
                tools=tools,
                developer_instructions=_root_instructions(),
                event_handler=lambda event: self._on_event(project.id, event),
                sandbox="read-only",
                approval_policy="never",
                ephemeral=False,
                # Match Codex app-server's native capability model: the root
                # Coordinator has no execution environment and therefore
                # cannot bypass MAC delegation with shell/apply_patch. Child
                # coding agents omit this override and keep their environment.
                environments=[],
                # Do not expose Codex's own child-agent controls alongside
                # MAC's durable spawn/send/wait/list protocol. Otherwise the
                # model can create an untracked Codex child that MAC cannot
                # project to cards, resume, or account for.
                thread_config={
                    # app-server's ``config`` map uses Codex's same dotted
                    # override keys as ``codex -c``; nested JSON is interpreted
                    # as a literal TOML table and does not provide this mask.
                    "features.multi_agent": False,
                    "features.multi_agent_v2": False,
                    "features.apps": False,
                    "features.plugins": False,
                    "features.js_repl": False,
                    "features.in_app_browser": False,
                },
                codex_home=self.settings.coordinator_codex_home,
            )
            with _ROOT_RUNTIME_GUARD:
                _ROOT_RUNTIMES[project.id] = _RootHandle(root, current_loop)
        else:
            # API and Feishu handlers use short-lived DB sessions. Rebind tool
            # closures and projections to the current transaction boundary.
            root.tools = {tool.name: tool for tool in tools}
            root.event_handler = lambda event: self._on_event(project.id, event)

        durable = self.store.ensure(project.id)
        profile_is_current = (
            (project.context or {}).get("coordinator_capability_profile")
            == _ROOT_CAPABILITY_PROFILE
        )
        if root.thread_id is None:
            if durable.native_thread_id and durable.status != "failed" and profile_is_current:
                await root.resume_thread(durable.native_thread_id)
            else:
                native_id = await root.create_thread()
                self.store.set_native_thread(project.id, native_id)
                context = dict(project.context or {})
                context["coordinator_capability_profile"] = _ROOT_CAPABILITY_PROFILE
                project.context = context
                project.updated_at = _utcnow()
                self.session.add(project)
                self.session.commit()
        return root

    @staticmethod
    async def _release_root(
        project_id: str, root: CodexAppServerCoordinator
    ) -> None:
        with _ROOT_RUNTIME_GUARD:
            handle = _ROOT_RUNTIMES.get(project_id)
            if handle is not None and handle.runtime is root:
                _ROOT_RUNTIMES.pop(project_id, None)
        await root.close()

    async def _on_event(self, project_id: str, event: CoordinatorEvent) -> None:
        # Token/text deltas are transient provider transport, not durable
        # project facts. Persist lifecycle/tool/final/usage boundaries only.
        if event.kind.endswith("/delta") or event.kind == "account/rateLimits/updated":
            return
        self.store.append_event(project_id, event)
        if event.turn_id:
            self.store.set_turn(project_id, turn_id=event.turn_id, status="running")

    def _tools(self, project: Project) -> list[CoordinatorTool]:
        def tool(name: str, description: str, schema: dict[str, Any], handler):
            async def persisted(arguments: dict[str, Any], context: CoordinatorToolContext):
                return await self.store.execute_tool_once(
                    project.id,
                    context,
                    arguments,
                    lambda tool_call_id: handler(arguments, tool_call_id),
                )

            return CoordinatorTool(name, description, schema, persisted)

        return [
            tool(
                "mac_spawn_agent",
                "Run one bounded coding or verification task. Available backends: "
                + ", ".join(sorted(self.runtime.available_providers())),
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_name", "message", "access"],
                    "properties": {
                        "task_name": {"type": "string"},
                        "message": {"type": "string"},
                        "provider": {"type": "string"},
                        "access": {"type": "string", "enum": ["read_only", "write"]},
                        "context_fork": {"type": "string", "enum": ["none", "project"]},
                    },
                },
                self._spawn_agent,
            ),
            tool(
                "mac_send_message",
                "Send follow-up evidence or a correction to an existing agent task.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["agent", "message"],
                    "properties": {
                        "agent": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
                self._send_message,
            ),
            tool(
                "mac_wait_agent",
                "Return the latest durable activity for agents owned by this project.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 60}},
                },
                self._wait_agent,
            ),
            tool(
                "mac_list_agents",
                "List real persisted Agent status compactly, or fetch full details "
                "for explicitly named Agents.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "agents": {
                            "type": "array",
                            "maxItems": 10,
                            "items": {"type": "string"},
                        },
                        "include_full": {"type": "boolean"},
                    },
                },
                self._list_agents,
            ),
            tool(
                "mac_request_user_input",
                "Ask the user up to three choice questions when the system cannot decide.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["questions"],
                    "properties": {
                        "questions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["id", "question", "options"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "question": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "options": {
                                        "type": "array",
                                        "minItems": 2,
                                        "maxItems": 3,
                                        "items": {
                                            "type": "object",
                                            "required": ["key", "label"],
                                            "properties": {
                                                "key": {"type": "string"},
                                                "label": {"type": "string"},
                                                "description": {"type": "string"},
                                                "recommended": {"type": "boolean"},
                                            },
                                        },
                                    },
                                },
                            },
                        }
                    },
                },
                self._request_user_input,
            ),
            tool(
                "mac_report",
                "Publish a meaningful progress or result card to the user.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "message"],
                    "properties": {
                        "title": {"type": "string"},
                        "message": {"type": "string"},
                        "kind": {"type": "string", "enum": ["progress", "result", "warning"]},
                    },
                },
                self._report,
            ),
            tool(
                "mac_complete_project",
                "Mark the project complete after implementation and independent verification.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["summary", "evidence"],
                    "properties": {
                        "summary": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                },
                self._complete_project,
            ),
        ]

    async def _spawn_agent(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        task_name = str(args["task_name"]).strip()
        objective = str(args["message"]).strip()
        access = str(args["access"])
        available = self.runtime.available_providers()
        requested = str(args.get("provider") or "").strip()
        if requested and requested not in available:
            return {
                "status": "failed",
                "error": f"Requested provider is unavailable: {requested}",
                "available": sorted(available),
            }
        provider = requested or self.runtime.provider_for("coder")
        if provider not in available:
            return {"status": "failed", "error": f"No available provider: {provider}", "available": sorted(available)}
        if access == "write":
            active_writer = next(
                (
                    item
                    for item in self.store.list_agents(project.id)
                    if item.access == "write"
                    and item.status == "running"
                    and item.task_name != task_name
                ),
                None,
            )
            if active_writer is not None:
                return {
                    "status": "blocked",
                    "error": "Another write Agent is already running",
                    "active_writer": active_writer.task_name,
                }

        row = self.store.create_agent(
            project.id,
            task_name=task_name,
            provider=provider,
            access=access,
            objective=objective,
            tool_call_id=tool_call_id,
        )
        if row.status in {"running", "completed"}:
            return self._agent_view(row)
        task = await self.service.create_task(
            project.id,
            agent_type="dynamic_agent",
            state="COORDINATOR_AGENT",
            input_data={
                "objective": objective,
                "provider": provider,
                "access": access,
                "_profiling": {"execution_started_at": _utcnow().isoformat()},
            },
        )
        row.task_id = task.id
        row.status = "running"
        row.updated_at = _utcnow()
        self.session.add(row)
        self.session.commit()
        self.service.set_execution_status(project.id, "running_agent")

        worktree = Path(project.workspace_path)
        owns_worktree = _agent_uses_isolated_worktree(access, provider)
        if owns_worktree:
            try:
                worktree = self.worktrees.create_worktree(
                    project.id,
                    task.id,
                    "dynamic_agent",
                    workspace_path=project.workspace_path,
                )
                row.worktree_path = str(worktree)
            except Exception as exc:
                return await self._agent_failed(row, task.id, f"Worktree creation failed: {exc}")
        await self.service.start_task(task.id, str(worktree))
        self.session.add(row)
        self.session.commit()
        await self._publish_agent_progress(
            row,
            task.id,
            lifecycle="running",
            detail="已接收任务，正在执行",
        )

        self._schedule_agent_run(
            row.id,
            task.id,
            objective,
            worktree,
            owns_worktree,
            provider,
        )
        return self._agent_view(row)

    def _schedule_agent_run(
        self,
        row_id: str,
        task_id: str,
        objective: str,
        worktree: Path,
        owns_worktree: bool,
        provider: str,
    ) -> None:
        """Start a bounded Agent like Codex spawn: return identity, run in background."""
        bind = self.session.get_bind()
        project_id = self._require_project().id

        async def execute() -> None:
            completed_task_name = row_id
            background_bridge, owns_bridge = self._fresh_background_bridge()
            try:
                with Session(bind) as child_session:
                    child_engine = CoordinatorEngine(
                        child_session,
                        self.runtime,
                        self.worktrees,
                        self.settings,
                        feishu_bridge=background_bridge,
                    )
                    child_engine.project = child_engine.service.require_project(
                        project_id
                    )
                    row = child_session.get(CoordinatorAgentSession, row_id)
                    if row is None:
                        raise RuntimeError(f"Coordinator Agent disappeared: {row_id}")
                    completed_task_name = row.task_name
                    await child_engine._execute_agent(
                        row,
                        task_id,
                        objective,
                        Path(worktree),
                        owns_worktree,
                        provider,
                    )
            finally:
                if owns_bridge and background_bridge is not None:
                    await background_bridge.close()
            self._schedule_coordinator_wakeup(
                bind,
                project_id=project_id,
                task_name=completed_task_name,
                task_id=task_id,
            )

        background, loop = _submit_background(
            execute(), name=f"mac-agent-{task_id}"
        )
        handle = _AgentRunHandle(project_id, background, loop)
        with _AGENT_RUN_GUARD:
            _AGENT_RUNS[task_id] = handle

        def finished(done: asyncio.Task[None] | ConcurrentFuture[None]) -> None:
            with _AGENT_RUN_GUARD:
                current = _AGENT_RUNS.get(task_id)
                if current is not None and current.task is done:
                    _AGENT_RUNS.pop(task_id, None)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                logger.error(
                    "Background Coordinator Agent crashed: %s",
                    task_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        background.add_done_callback(finished)

    def _fresh_background_bridge(self) -> tuple[Any | None, bool]:
        """Detach background notifications from a per-event Feishu client."""
        if (
            isinstance(self.feishu_bridge, FeishuBridge)
            and self.settings.feishu_app_id
            and self.settings.feishu_app_secret
        ):
            return (
                FeishuBridge(
                    app_id=self.settings.feishu_app_id,
                    app_secret=self.settings.feishu_app_secret,
                    encrypt_key=self.settings.feishu_encrypt_key,
                    verification_token=self.settings.feishu_verification_token,
                ),
                True,
            )
        # Test doubles and application-owned long-lived bridges keep their
        # existing lifetime contract.
        return self.feishu_bridge, False

    def _schedule_coordinator_wakeup(
        self,
        bind: Any,
        *,
        project_id: str,
        task_name: str,
        task_id: str,
    ) -> None:
        """Wake an idle parent after a background Agent reaches a terminal state.

        Codex's parent/child protocol delivers child completion back to the
        parent mailbox. MAC persists the same boundary and starts a new root
        turn only when the previous root turn has already ended. If the parent
        is still inside ``mac_wait_agent``, that turn consumes the event and no
        extra wakeup is created.
        """

        wakeup_id = f"mac-coordinator-wakeup-{project_id}"

        async def wake() -> None:
            await asyncio.sleep(0.25)
            while True:
                # Completion can race with an already-running parent turn. Do
                # not drop the signal: wait until the parent is idle, then
                # reconcile all durable Agent results in one coalesced turn.
                while True:
                    with Session(bind) as probe_session:
                        probe_store = CoordinatorSessionStore(probe_session)
                        coordinator = probe_store.ensure(project_id)
                        if not coordinator.native_thread_id:
                            # Direct host/test-created Agents have no parent
                            # Coordinator thread to wake.
                            return
                        parent_running = bool(
                            coordinator.status == "running"
                            or coordinator.active_turn_id
                        )
                    if not parent_running:
                        break
                    await asyncio.sleep(0.25)

                with _COORDINATOR_WAKEUP_GUARD:
                    observed_generation = _COORDINATOR_WAKEUP_GENERATIONS.get(
                        project_id, 0
                    )

                background_bridge, owns_bridge = self._fresh_background_bridge()
                try:
                    with Session(bind) as wake_session:
                        wake_service = ProjectService(wake_session)
                        project = wake_service.require_project(project_id)
                        if project.status != "active" or project.execution_status in {
                            "waiting_user",
                            "waiting_external",
                        }:
                            return
                        wake_engine = CoordinatorEngine(
                            wake_session,
                            self.runtime,
                            self.worktrees,
                            self.settings,
                            feishu_bridge=background_bridge,
                        )
                        await wake_engine.run(
                            project_id,
                            (
                                "One or more durable background Agents reached a "
                                "terminal state. Reconcile all current results with "
                                "mac_list_agents and continue the project without "
                                "asking the user to repeat context. "
                                f"Batch initiator: Agent={task_name}; task_id={task_id}."
                            ),
                        )
                except Exception:
                    logger.exception(
                        "Failed to wake Coordinator after Agent completion: %s",
                        task_id,
                    )
                    return
                finally:
                    if owns_bridge and background_bridge is not None:
                        await background_bridge.close()

                with _COORDINATOR_WAKEUP_GUARD:
                    latest_generation = _COORDINATOR_WAKEUP_GENERATIONS.get(
                        project_id, 0
                    )
                if latest_generation == observed_generation:
                    return

        with _COORDINATOR_WAKEUP_GUARD:
            _COORDINATOR_WAKEUP_GENERATIONS[project_id] = (
                _COORDINATOR_WAKEUP_GENERATIONS.get(project_id, 0) + 1
            )
            current = _COORDINATOR_WAKEUPS.get(wakeup_id)
            if current is not None and not current.task.done():
                return
            background, loop = _submit_background(wake(), name=wakeup_id)
            handle = _AgentRunHandle(project_id, background, loop)
            _COORDINATOR_WAKEUPS[wakeup_id] = handle

        def finished(done: asyncio.Task[None] | ConcurrentFuture[None]) -> None:
            with _COORDINATOR_WAKEUP_GUARD:
                current = _COORDINATOR_WAKEUPS.get(wakeup_id)
                if current is not None and current.task is done:
                    _COORDINATOR_WAKEUPS.pop(wakeup_id, None)
                    _COORDINATOR_WAKEUP_GENERATIONS.pop(project_id, None)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                logger.error(
                    "Background Coordinator wakeup crashed: %s",
                    task_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        background.add_done_callback(finished)

    async def _execute_agent(
        self,
        row: CoordinatorAgentSession,
        task_id: str,
        objective: str,
        worktree: Path,
        owns_worktree: bool,
        provider: str,
    ) -> dict[str, Any]:
        """Execute one already-persisted Agent and close its durable boundary."""
        project = self._require_project()
        access = row.access

        if provider == "codex":
            return await self._run_native_codex_agent(
                row, task_id, objective, worktree, owns_worktree
            )

        schema = Path(__file__).resolve().parents[3] / "specs/schemas/coordinator_agent_output.json"
        allowed_tools = ["Read", "Bash"]
        if access == "write":
            allowed_tools.extend(["Edit", "Write"])
        try:
            result = await self.runtime.execute(
                agent_type="dynamic_agent",
                task_prompt=objective,
                system_prompt=_agent_contract(objective, access),
                workspace=Path(project.workspace_path),
                worktree=worktree,
                context_files=[],
                output_schema=schema,
                allowed_tools=allowed_tools,
                readonly_dirs=[Path(project.workspace_path)],
                timeout_seconds=self.settings.agent_timeout_seconds,
                task_id=task_id,
                max_tool_calls=200 if access == "write" else 120,
                preferred_provider=provider,
            )
        except Exception as exc:
            return await self._agent_failed(row, task_id, f"Runtime failed: {exc}")

        if not result.success or not result.output_json:
            return await self._agent_failed(row, task_id, result.logs or "Agent failed")
        output = dict(result.output_json)
        if output.get("status") in {"success", "pass"}:
            output["status"] = "completed"
        output.setdefault("changes", [])
        output.setdefault("verification", [])
        output.setdefault("blockers", [])
        self.session.refresh(row)
        if row.task_id != task_id:
            # A later continuation owns the durable Agent identity and, for a
            # writer, may own the same adopted worktree. Close only this Task;
            # never merge/remove its workspace or overwrite the newer status.
            await self.service.complete_task(task_id, output, result.cost_info)
            self._sync_agent_execution_status(project.id)
            await self._publish_agent_progress(
                row,
                task_id,
                lifecycle=str(output.get("status") or "completed"),
                detail="较早任务已结束；Agent 当前状态由后续任务继续维护",
            )
            return {
                "agent": row.task_name,
                "status": str(output.get("status") or "completed"),
                "superseded": True,
                "summary": output.get("summary"),
            }
        host_delivery: dict[str, Any] | None = None
        if owns_worktree and output.get("status") == "completed":
            try:
                host_delivery = self.worktrees.merge_worktree(
                    project.id,
                    task_id,
                    "dynamic_agent",
                    worktree,
                    workspace_path=project.workspace_path,
                )
            except Exception as exc:
                return await self._agent_failed(row, task_id, f"Merge failed: {exc}")
            finally:
                try:
                    self.worktrees.remove_worktree(
                        project.id,
                        task_id,
                        "dynamic_agent",
                        worktree,
                        workspace_path=project.workspace_path,
                    )
                except Exception:
                    logger.exception("Failed to remove dynamic Agent worktree")
                row.worktree_path = None

        if host_delivery is not None:
            output["host_delivery"] = host_delivery

        await self.service.complete_task(task_id, output, result.cost_info)
        row.status = str(output.get("status") or "completed")
        row.latest_message = output.get("summary")
        row.result = {**output, "provider": provider, "tool_call_count": len(result.tool_calls or [])}
        row.updated_at = _utcnow()
        self.session.add(row)
        self.session.commit()
        self._sync_agent_execution_status(project.id)
        await self._publish_agent_progress(
            row,
            task_id,
            lifecycle=row.status,
            detail="本轮已结束，结果已返回 Coordinator",
        )
        return self._agent_view(row)

    def _sync_agent_execution_status(self, project_id: str) -> None:
        status = "running_agent" if self.service._running_tasks(project_id) else "coordinating"
        self.service.set_execution_status(project_id, status)

    async def _run_native_codex_agent(
        self,
        row: CoordinatorAgentSession,
        task_id: str,
        message: str,
        worktree: Path,
        owns_worktree: bool,
    ) -> dict[str, Any]:
        """Run or resume one Codex child thread and persist its native id."""
        project = self._require_project()
        child = CodexAppServerCoordinator(
            codex_bin=self.settings.codex_bin,
            cwd=worktree,
            tools=[],
            developer_instructions=_native_agent_instructions(row.access),
            sandbox="workspace-write" if owns_worktree else "read-only",
            approval_policy="never",
            ephemeral=False,
            # Coding agents keep Codex's shell/apply_patch environment, but do
            # not inherit unrelated operator MCP servers/plugins (notably
            # node_repl, whose unbounded output can break app-server framing).
            codex_home=self.settings.agent_codex_home,
            thread_config={
                "features.apps": False,
                "features.plugins": False,
                "features.js_repl": False,
                "features.in_app_browser": False,
            },
        )
        try:
            if row.native_session_id:
                try:
                    await child.resume_thread(row.native_session_id)
                except Exception:
                    # A capability-profile deployment can move child sessions
                    # to an isolated Codex home. Recreate once from durable MAC
                    # facts instead of failing or silently losing the task.
                    previous = json.dumps(row.result or {}, ensure_ascii=False)
                    row.native_session_id = await child.create_thread()
                    message = (
                        "Recover this bounded Agent task in a fresh native thread.\n"
                        f"Original objective:\n{row.objective}\n\n"
                        f"Previous durable result:\n{previous}\n\n"
                        f"Current Coordinator message:\n{message}"
                    )
                    self.session.add(row)
                    self.session.commit()
            else:
                row.native_session_id = await child.create_thread()
                self.session.add(row)
                self.session.commit()
            turn = await asyncio.wait_for(
                child.run_turn(message),
                timeout=self.settings.agent_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return await self._agent_failed(
                row,
                task_id,
                (
                    "Codex native turn timed out after "
                    f"{self.settings.agent_timeout_seconds}s; resumable worktree retained"
                ),
            )
        except Exception as exc:
            return await self._agent_failed(row, task_id, f"Codex native session failed: {exc}")
        finally:
            await child.close()

        if turn.status != "completed" or not turn.final_message:
            return await self._agent_failed(
                row,
                task_id,
                f"Codex native turn ended with status={turn.status}",
            )
        self.session.refresh(row)
        if row.task_id != task_id:
            output = {
                "status": "completed",
                "summary": turn.final_message,
                "changes": [],
                "verification": [],
                "blockers": [],
                "native_thread_id": row.native_session_id,
                "native_turn_id": turn.turn_id,
            }
            await self.service.complete_task(task_id, output, None)
            self._sync_agent_execution_status(project.id)
            await self._publish_agent_progress(
                row,
                task_id,
                lifecycle="completed",
                detail="较早任务已结束；Agent 当前状态由后续任务继续维护",
            )
            return {
                "agent": row.task_name,
                "status": "completed",
                "superseded": True,
                "summary": turn.final_message,
            }
        changed_paths: list[str] = []
        host_delivery: dict[str, Any] | None = None
        if owns_worktree:
            if row.access == "write":
                status = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=worktree,
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout
                changed_paths = [
                    line[3:].strip()
                    for line in status.splitlines()
                    if len(line) >= 4
                ]
            try:
                if row.access == "write":
                    host_delivery = self.worktrees.merge_worktree(
                        project.id,
                        task_id,
                        "dynamic_agent",
                        worktree,
                        workspace_path=project.workspace_path,
                    )
            except Exception as exc:
                return await self._agent_failed(row, task_id, f"Merge failed: {exc}")
            finally:
                try:
                    self.worktrees.remove_worktree(
                        project.id,
                        task_id,
                        "dynamic_agent",
                        worktree,
                        workspace_path=project.workspace_path,
                    )
                except Exception:
                    logger.exception("Failed to remove native Codex Agent worktree")
                row.worktree_path = None

        output = {
            "status": "completed",
            "summary": turn.final_message,
            "changes": changed_paths,
            "verification": [],
            "blockers": [],
            "native_thread_id": row.native_session_id,
            "native_turn_id": turn.turn_id,
        }
        if host_delivery is not None:
            output["host_delivery"] = host_delivery
        await self.service.complete_task(task_id, output, None)
        row.status = "completed"
        row.latest_message = turn.final_message
        row.result = {
            **output,
            "provider": "codex",
            "event_count": len(turn.events),
        }
        row.updated_at = _utcnow()
        self.session.add(row)
        self.session.commit()
        self._sync_agent_execution_status(project.id)
        await self._publish_agent_progress(
            row,
            task_id,
            lifecycle="completed",
            detail="本轮已结束，结果已返回 Coordinator",
        )
        return self._agent_view(row)

    async def _agent_failed(
        self, row: CoordinatorAgentSession, task_id: str, error: str
    ) -> dict[str, Any]:
        await self.service.fail_task(task_id, error)
        self.session.refresh(row)
        if (
            row.access == "read_only"
            and row.worktree_path
            and self.project is not None
        ):
            try:
                self.worktrees.remove_worktree(
                    self.project.id,
                    task_id,
                    "dynamic_agent",
                    Path(row.worktree_path),
                    workspace_path=self.project.workspace_path,
                )
            except Exception:
                logger.exception("Failed to discard verifier worktree")
            row.worktree_path = None
        if row.task_id != task_id:
            # A superseded Task may finish after a continuation was accepted.
            # Its card still receives the terminal state, but it must not
            # clobber the durable Agent session that points at the newer Task.
            if self.project is not None:
                self._sync_agent_execution_status(self.project.id)
            await self._publish_agent_progress(
                row,
                task_id,
                lifecycle="failed",
                detail=error[:500],
            )
            return {
                "agent": row.task_name,
                "status": "failed",
                "superseded": True,
                "error": error[:4000],
            }
        row.status = "failed"
        row.latest_message = error[:1000]
        row.result = {**(row.result or {}), "error": error[:4000]}
        row.updated_at = _utcnow()
        self.session.add(row)
        self.session.commit()
        if self.project is not None:
            self._sync_agent_execution_status(self.project.id)
        await self._publish_agent_progress(
            row,
            task_id,
            lifecycle="failed",
            detail=error[:500],
        )
        return self._agent_view(row)

    async def _send_message(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        original = self.store.get_agent(project.id, str(args["agent"]))
        if original is None:
            return {"status": "failed", "error": "Agent not found"}
        self.session.refresh(original)
        if original.status == "running":
            return {
                "status": "blocked",
                "error": (
                    "Agent is still running; wait for its terminal result before "
                    "sending a continuation"
                ),
                "agent": self._agent_view(original),
            }
        continuation = (
            f"Continue task {original.task_name}. Original objective:\n"
            f"{original.objective}\n\nPrevious durable result:\n"
            f"{json.dumps(original.result, ensure_ascii=False)}\n\n"
            f"Coordinator follow-up:\n{args['message']}"
        )
        previous_task_id = original.task_id
        task = await self.service.create_task(
            project.id,
            agent_type="dynamic_agent",
            state="COORDINATOR_AGENT",
            input_data={
                "objective": continuation,
                "provider": original.provider,
                "access": original.access,
                "continuation_of": original.task_name,
                "coordinator_tool_call_id": tool_call_id,
                "_profiling": {"execution_started_at": _utcnow().isoformat()},
            },
        )
        worktree = Path(project.workspace_path)
        owns_worktree = _agent_uses_isolated_worktree(
            original.access, original.provider
        )
        if owns_worktree and original.worktree_path and previous_task_id:
            try:
                worktree = self.worktrees.adopt_worktree(
                    project.id,
                    previous_task_id,
                    task.id,
                    "dynamic_agent",
                    Path(original.worktree_path),
                    workspace_path=project.workspace_path,
                )
            except Exception as exc:
                return await self._agent_failed(
                    original, task.id, f"Worktree continuation failed: {exc}"
                )
        elif owns_worktree:
            try:
                worktree = self.worktrees.create_worktree(
                    project.id,
                    task.id,
                    "dynamic_agent",
                    workspace_path=project.workspace_path,
                )
            except Exception as exc:
                return await self._agent_failed(
                    original, task.id, f"Worktree creation failed: {exc}"
                )
        original.task_id = task.id
        original.worktree_path = str(worktree) if owns_worktree else None
        original.status = "running"
        original.updated_at = _utcnow()
        self.session.add(original)
        self.session.commit()
        self.service.set_execution_status(project.id, "running_agent")
        await self.service.start_task(task.id, str(worktree))
        await self._publish_agent_progress(
            original,
            task.id,
            lifecycle="running",
            detail="已收到 Coordinator 反馈，保留现有工作区继续处理",
        )
        self._schedule_agent_run(
            original.id,
            task.id,
            continuation,
            worktree,
            owns_worktree,
            original.provider,
        )
        return self._agent_view(original)

    async def _publish_agent_progress(
        self,
        row: CoordinatorAgentSession,
        task_id: str,
        *,
        lifecycle: str,
        detail: str,
    ) -> None:
        """Project one durable agent task onto one updateable Feishu card."""
        project = self._require_project()
        if self.feishu_bridge is None or not project.feishu_chat_id:
            return
        projection_key = f"coordinator-agent:{row.id}:task:{task_id}:progress"
        projection = self.service.get_card_projection(projection_key)
        labels = {
            "running": "正在执行",
            "completed": "已完成",
            "blocked": "等待处理",
            "failed": "执行失败",
        }
        colors = {
            "running": "purple",
            "completed": "green",
            "blocked": "orange",
            "failed": "red",
        }
        status_label = labels.get(lifecycle, lifecycle)
        card = build_content_card(
            f"[{project.name or project.id}] {row.task_name} · {status_label}",
            (
                f"**执行后端**：{row.provider}\n"
                f"**权限**：{'可修改项目' if row.access == 'write' else '只读验收'}\n"
                f"**状态**：{status_label}\n\n{detail}"
            ),
            color=colors.get(lifecycle, "blue"),
        )
        try:
            await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"{projection_key}:{lifecycle}",
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
                subject_type="coordinator_agent",
                subject_id=row.id,
                lifecycle_status=lifecycle,
                resulting_state=project.execution_status,
            )
        except Exception:
            logger.warning(
                "Failed to publish Coordinator agent progress for %s",
                task_id,
                exc_info=True,
            )

    async def _wait_agent(self, args: dict[str, Any], _tool_call_id: str) -> dict[str, Any]:
        timeout = min(60, max(0, int(args.get("timeout_seconds") or 0)))
        project = self._require_project()
        current_loop = asyncio.get_running_loop()
        with _AGENT_RUN_GUARD:
            handles = [
                handle
                for handle in _AGENT_RUNS.values()
                if handle.project_id == project.id and not handle.task.done()
            ]
        waiters: list[asyncio.Future[Any]] = []
        for handle in handles:
            if handle.loop is current_loop and isinstance(
                handle.task, asyncio.Future
            ):
                local = handle.task
            elif isinstance(handle.task, asyncio.Future):
                # ``asyncio.wrap_future`` accepts concurrent.futures.Future,
                # not an asyncio.Task owned by another loop.  Feishu handlers
                # use short-lived loops while application-owned Agent tasks use
                # the FastAPI lifespan loop, so bridge the join on the owning
                # loop and expose only its concurrent Future locally.
                foreign_join = asyncio.run_coroutine_threadsafe(
                    _join_foreign_agent_task(handle.task), handle.loop
                )
                local = asyncio.wrap_future(foreign_join)
            else:
                local = asyncio.wrap_future(handle.task)
            # A timed-out/cancelled wait tool must never cancel the durable
            # Agent it is observing.  The shield is the local mailbox join,
            # not ownership of the child task.
            waiters.append(asyncio.shield(local))
        if waiters and timeout:
            _done, pending = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in pending:
                waiter.cancel()
        self.session.expire_all()
        return await self._list_agents({}, _tool_call_id)

    async def _list_agents(self, args: dict[str, Any], _tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        requested = {
            str(name) for name in (args.get("agents") or []) if str(name).strip()
        }
        include_full = bool(args.get("include_full"))
        rows = self.store.list_agents(project.id)
        if requested:
            rows = [row for row in rows if row.task_name in requested]
        return {
            "agents": [
                self._agent_view(row, include_full=include_full)
                for row in rows
            ],
            "compact": not include_full,
        }

    async def _request_user_input(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        questions = []
        for raw in args.get("questions") or []:
            options = list(raw.get("options") or [])
            recommended = next((str(o.get("key")) for o in options if o.get("recommended")), None)
            questions.append({
                "question_id": str(raw["id"]),
                "decision_key": str(raw["id"]),
                "kind": "decision",
                "question": str(raw["question"]),
                "reason": str(raw.get("reason") or ""),
                "options": options,
                "allow_free_text": False,
                "recommended_option": recommended,
            })
        approval = await self.service.create_approval(
            project.id,
            None,
            "COORDINATOR_WAITING_USER",
            "input",
            questions[0]["question"],
            [],
            payload={"coordinator_tool_call_id": tool_call_id},
            enforce_current_state=False,
        )
        interaction = await self.service.create_question_interaction(
            project.id,
            None,
            1,
            "COORDINATING",
            "coordinator",
            "coordination",
            f"coordinator:{tool_call_id}",
            questions,
            approval_id=approval.id,
            requested_to="user",
            escalated_to_user=True,
        )
        approval.payload = {**approval.payload, "question_interaction_id": interaction.id}
        self.session.add(approval)
        self.service.set_execution_status(project.id, "waiting_user", reason=questions[0]["question"], commit=False)
        self.session.commit()
        if self.feishu_bridge is not None and project.feishu_chat_id:
            card = build_question_batch_card(
                approval.id,
                interaction.id,
                questions,
                "coordinator",
                "coordination",
                state="COORDINATOR_WAITING_USER",
                project_name=project.name or project.id,
                chat_id=project.feishu_chat_id,
                chat_type=project.feishu_chat_type,
            )
            message_id = await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=f"coordinator-question:{tool_call_id}",
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
            approval.payload = {**approval.payload, "question_batch_message_id": message_id}
            self.session.add(approval)
            self.session.commit()
        return {"status": "waiting_user", "interaction_id": interaction.id, "question_count": len(questions)}

    async def _report(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        kind = str(args.get("kind") or "progress")
        base_key = f"coordinator-report:{tool_call_id}"
        if self.feishu_bridge is None or not project.feishu_chat_id:
            notification = self.service.enqueue_notification(
                dedupe_key=base_key,
                recipient_id=project.feishu_chat_id or project.id,
                recipient_type="chat_id",
                intent_type="report" if kind == "result" else "progress",
                payload={"title": str(args["title"]), "content": str(args["message"]), "kind": kind},
                project_id=project.id,
            )
            self.service.upsert_card_projection(
                projection_key=base_key,
                subject_type="notification",
                subject_id=notification.id,
                card_kind=kind,
                project_id=project.id,
                lifecycle_status="recorded",
                resulting_state=project.execution_status,
            )
            return {"status": "recorded", "notification_id": notification.id}
        chunks = split_markdown_sections(str(args["message"]))
        ids: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_key = f"{base_key}:{index}"
            card = build_content_card(
                str(args["title"]),
                chunk,
                page=index,
                total=len(chunks),
                color="red" if kind == "warning" else "blue",
            )
            message_id = await deliver_card(
                self.service,
                self.feishu_bridge,
                dedupe_key=chunk_key,
                recipient_id=project.feishu_chat_id,
                recipient_type="chat_id",
                intent_type="report" if kind == "result" else "progress",
                card=card,
                project_id=project.id,
                projection_key=chunk_key,
                resulting_state=project.execution_status,
            )
            if message_id:
                ids.append(message_id)
        return {"status": "sent", "message_ids": ids}

    async def _complete_project(self, args: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        project = self._require_project()
        completed_agents = [row for row in self.store.list_agents(project.id) if row.status == "completed"]
        material = [row for row in completed_agents if row.access == "write"]
        independent = [row for row in completed_agents if row.access == "read_only"]
        if not material or not independent:
            return {"status": "rejected", "reason": "Completion requires material work and an independent read_only verification agent."}
        project.status = "completed"
        project.previous_state = project.current_state
        project.current_state = "COMPLETED"
        project.execution_status = "idle"
        project.waiting_reason = None
        project.updated_at = _utcnow()
        project.context = {
            **project.context,
            "completion": {
                "summary": str(args["summary"]),
                "evidence": list(args.get("evidence") or []),
                "tool_call_id": tool_call_id,
                "completed_at": _utcnow().isoformat(),
            },
        }
        self.session.add(project)
        self.session.commit()
        await self._report(
            {"title": f"[{project.name or project.id}] 已完成", "message": str(args["summary"]), "kind": "result"},
            f"{tool_call_id}:completion-report",
        )
        return {"status": "completed", "project_id": project.id}

    def _require_project(self) -> Project:
        if self.project is None:
            raise RuntimeError("Coordinator tool used outside a project turn")
        return self.service.require_project(self.project.id)

    @staticmethod
    def _agent_view(
        row: CoordinatorAgentSession,
        *,
        include_full: bool = False,
    ) -> dict[str, Any]:
        summary = row.latest_message
        truncated = False
        if not include_full and summary and len(summary) > 1200:
            summary = summary[:1200].rstrip() + "\n…[summary truncated; request this agent with include_full=true]"
            truncated = True
        view = {
            "agent": row.task_name,
            "provider": row.provider,
            "access": row.access,
            "status": row.status,
            "summary": summary,
            "summary_truncated": truncated,
            "retained_worktree": bool(row.worktree_path),
        }
        if include_full:
            view["result"] = row.result
        return view


async def close_coordinator_runtimes() -> None:
    """Close process-local app-server adapters during application shutdown."""
    with _AGENT_RUN_GUARD:
        agent_handles = list(_AGENT_RUNS.values())
        _AGENT_RUNS.clear()
    with _COORDINATOR_WAKEUP_GUARD:
        agent_handles.extend(_COORDINATOR_WAKEUPS.values())
        _COORDINATOR_WAKEUPS.clear()
        _COORDINATOR_WAKEUP_GENERATIONS.clear()
    current = asyncio.get_running_loop()
    agent_tasks: list[asyncio.Future[Any]] = []
    for handle in agent_handles:
        if handle.loop is current and isinstance(handle.task, asyncio.Future):
            handle.task.cancel()
            agent_tasks.append(handle.task)
        elif isinstance(handle.task, asyncio.Future):
            # asyncio Tasks are owned by their creating loop.  Cancelling or
            # wrapping a foreign Task directly repeats the same cross-loop
            # failure that ``mac_wait_agent`` must avoid.  Request
            # cancellation on the owner loop, then bridge only the join back
            # to this shutdown loop.
            if handle.loop.is_closed():
                continue
            handle.loop.call_soon_threadsafe(handle.task.cancel)
            foreign_join = asyncio.run_coroutine_threadsafe(
                _join_foreign_agent_task(handle.task), handle.loop
            )
            agent_tasks.append(asyncio.wrap_future(foreign_join))
        else:
            handle.task.cancel()
            agent_tasks.append(asyncio.wrap_future(handle.task))
    if agent_tasks:
        await asyncio.gather(*agent_tasks, return_exceptions=True)

    with _ROOT_RUNTIME_GUARD:
        handles = list(_ROOT_RUNTIMES.values())
        _ROOT_RUNTIMES.clear()
    local = [handle.runtime for handle in handles if handle.loop is current]
    remote = [handle for handle in handles if handle.loop is not current and handle.loop.is_running()]
    await asyncio.gather(*(runtime.close() for runtime in local), return_exceptions=True)
    futures = [
        asyncio.run_coroutine_threadsafe(handle.runtime.close(), handle.loop)
        for handle in remote
    ]
    if futures:
        await asyncio.gather(
            *(asyncio.wrap_future(future) for future in futures),
            return_exceptions=True,
        )
