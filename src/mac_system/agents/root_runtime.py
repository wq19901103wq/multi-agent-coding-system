"""Thin Codex app-server client used by MAC's persistent root Agent.

The turn loop, built-in tools, context management, multi-Agent control and
sub-Agent sessions remain owned by Codex. MAC only transports user input,
answers app-server requests and projects native events onto its UI/storage.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import random
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from mac_system.agents.process_lifecycle import stop_process_tree
from mac_system.agents.proxy_recovery import recover_dead_proxy_nodes


JsonObject = dict[str, Any]
ToolHandler = Callable[..., Awaitable[Any] | Any]
EventHandler = Callable[["AgentEvent"], Awaitable[None] | None]
ServerRequestHandler = Callable[[str, JsonObject], Awaitable[JsonObject] | JsonObject]
logger = logging.getLogger(__name__)
_APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024
_PROCESS_CLOSED_METHOD = "mac/process/closed"
# Every Codex runtime MAC builds selects the built-in OpenAI provider
# (``requires_openai_auth``), whose HTTPS traffic goes to these hosts. Both
# must be reachable: a proxy node can pass short probes for one and still
# fail the other. Reachability probes inform recovery and progress; a
# temporary outage does not cancel the bounded continuation plan or change
# the native turn/steer protocol.
_CODEX_BUILTIN_PROVIDER_HOST = "chatgpt.com"
_CODEX_AUTH_PROVIDER_HOST = "auth.openai.com"
_PROXY_RECOVERY_TARGET_URLS = (
    f"https://{_CODEX_BUILTIN_PROVIDER_HOST}",
    f"https://{_CODEX_AUTH_PROVIDER_HOST}",
)
_STREAM_TRANSPORT_ERROR_CODES = frozenset(
    {
        "responseStreamDisconnected",
        "responseStreamConnectionFailed",
        "httpConnectionFailed",
    }
)
_NATIVE_RECONNECT_PROXY_GRACE_SECONDS = 30.0
_NATIVE_RECONNECT_PROXY_WINDOW_SECONDS = 120.0
_HOST_RUNTIME_ENV_KEYS = {
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "CONDA_PROMPT_MODIFIER",
    "PIP_PREFIX",
    "PIP_TARGET",
    "PYTHONHOME",
    "PYTHONPATH",
    "UV_ACTIVE",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "_OLD_VIRTUAL_PATH",
}


def _isolated_project_process_env(source: Mapping[str, str]) -> dict[str, str]:
    """Keep service credentials/network config but drop its Python runtime.

    Project Lead shell commands are children of the Codex app-server process.
    If MAC itself was launched from its maintenance virtualenv, blindly
    inheriting that environment makes project-level ``python``, ``pip`` and
    ``uv run --active`` mutate MAC's own dependencies.  The project may still
    discover or create an environment inside its workspace normally.
    """

    result = {
        key: value
        for key, value in source.items()
        if key not in _HOST_RUNTIME_ENV_KEYS
    }
    virtual_env = str(
        source.get("VIRTUAL_ENV") or source.get("CONDA_PREFIX") or ""
    ).strip()
    path_value = str(source.get("PATH") or "")
    if virtual_env and path_value:
        runtime_bin = os.path.normcase(
            os.path.realpath(os.path.join(virtual_env, "bin"))
        )
        result["PATH"] = os.pathsep.join(
            entry
            for entry in path_value.split(os.pathsep)
            if not entry
            or os.path.normcase(os.path.realpath(entry)) != runtime_bin
        )
    return result


def _event_turn_id(
    method: str, params: JsonObject, fallback: str | None = None
) -> str | None:
    """Return the turn id from the shape defined by the native protocol.

    ``turn/started`` and ``turn/completed`` carry the id inside ``turn.id``;
    item events and server requests carry a top-level ``turnId``.  Falling back
    is only appropriate for notifications that genuinely omit correlation.
    """

    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn")
        if isinstance(turn, dict):
            value = turn.get("id")
            if isinstance(value, str) and value:
                return value
    value = params.get("turnId")
    if isinstance(value, str) and value:
        return value
    return fallback


class ProjectLeadRuntimeError(RuntimeError):
    """Base error exposed by every Project Lead runtime implementation."""


class AppServerProtocolError(ProjectLeadRuntimeError):
    """Raised when the Codex app-server violates its JSON-RPC contract."""


@dataclass(frozen=True)
class DynamicTool:
    """One MAC extension advertised through Codex dynamic tools."""

    name: str
    description: str
    input_schema: JsonObject
    handler: ToolHandler
    defer_loading: bool = False

    def codex_spec(self) -> JsonObject:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "deferLoading": self.defer_loading,
        }


@dataclass(frozen=True)
class AgentEvent:
    """Native app-server event projected to MAC storage and UI."""

    kind: str
    thread_id: str
    turn_id: str | None
    payload: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    thread_id: str
    turn_id: str
    call_id: str
    tool_name: str


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    status: str
    final_message: str | None
    events: tuple[AgentEvent, ...]


_SERVER_OVERLOADED_RETRY_DELAYS_SECONDS = (
    30.0, 60.0, 120.0, 300.0, 600.0, 900.0,
)
_RESPONSE_STREAM_DISCONNECTED_RETRY_DELAYS_SECONDS = (
    5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0,
)


def _turn_error_code(result: TurnResult) -> str | None:
    """Read Codex's structured terminal error classification when present."""

    # Native retries emit intermediate errors with willRetry=true. Only the
    # terminal status authorizes another turn; a recovered answer must survive
    # those earlier errors, and an interrupted turn must stay interrupted.
    if result.status != "failed":
        return None

    for event in reversed(result.events):
        error: Any = None
        if event.kind == "turn/completed":
            turn = event.payload.get("turn")
            if isinstance(turn, dict):
                error = turn.get("error")
        elif event.kind == "error":
            error = event.payload.get("error")
        if isinstance(error, dict):
            return _continuation_error_code(error)
    return None


def _continuation_error_code(error: JsonObject) -> str | None:
    error_info = error.get("codexErrorInfo")
    error_code = None
    details = None
    if isinstance(error_info, str):
        error_code = error_info.strip() or None
    elif isinstance(error_info, dict):
        error_code = next(
            (code for code in error_info if isinstance(code, str) and code), None
        )
        details = error_info.get(error_code)
    if error_code in {"serverOverloaded", "internalServerError"}:
        return "serverOverloaded"
    if error_code not in {
        None, "other", "httpConnectionFailed", "responseStreamConnectionFailed",
        "responseStreamDisconnected", "responseTooManyFailedAttempts",
    }:
        return error_code
    message = error.get("message")
    message_text = (
        message.strip().casefold() if isinstance(message, str) else ""
    )
    # An account-level concurrency rejection is temporary: slots free up as
    # ongoing requests finish, so bounded backoff retries are safe. Match
    # only the upstream signature; other 401/403 failures (auth, quota,
    # permission) stay non-retryable below.
    if (
        "access_terminated_error" in message_text
        or "concurrent request limit" in message_text
    ):
        return "concurrentRequestLimit"
    http_status = details.get("httpStatusCode") if isinstance(details, dict) else None
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        if http_status in {429, 500, 502, 503, 504}:
            return "serverOverloaded"
        if 400 <= http_status < 500:
            return None
    if error_code in {
        "httpConnectionFailed", "responseStreamConnectionFailed",
        "responseStreamDisconnected",
    }:
        return error_code
    message = error.get("message")
    if isinstance(message, str):
        message = message.strip().casefold()
        if message.startswith("stream disconnected before completion:"):
            return "responseStreamDisconnected"
        status_prefix = "exceeded retry limit, last status: "
        if message.startswith(status_prefix):
            status_text = message[len(status_prefix):].partition(" ")[0]
            if status_text in {"429", "500", "502", "503", "504"}:
                return "serverOverloaded"
    return error_code


def _turn_retry_delays(result: TurnResult) -> tuple[float, ...]:
    """Return the bounded native-continuation retry policy for one failure."""

    return _error_retry_delays(_turn_error_code(result))


def _error_retry_delays(error_code: str | None) -> tuple[float, ...]:
    match error_code:
        case "serverOverloaded" | "concurrentRequestLimit":
            return _SERVER_OVERLOADED_RETRY_DELAYS_SECONDS
        case code if code in _STREAM_TRANSPORT_ERROR_CODES:
            return _RESPONSE_STREAM_DISCONNECTED_RETRY_DELAYS_SECONDS
        case _:
            return ()


async def _wait_for_server_capacity(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


def _https_reachable_blocking(
    host: str, port: int, timeout_seconds: float, proxy: tuple[str, int, str | None] | None
) -> bool:
    """Blocking HTTPS reachability check, optionally through a CONNECT proxy.

    Unlike the TCP-level probe, this completes the TLS handshake, so a proxy
    that accepts CONNECT but whose upstream node dead-drops the tunnel is
    reported unreachable. Raises on timeout-level uncertainty so callers keep
    their retry plan; returns False on a positively broken path.
    """

    import socket
    import ssl

    deadline = timeout_seconds
    if proxy is None:
        connect_target = (host, port)
    else:
        proxy_host, proxy_port, _authorization = proxy
        connect_target = (proxy_host, proxy_port)
    try:
        raw = socket.create_connection(connect_target, timeout=deadline)
    except ConnectionRefusedError:
        # A refused listener (typically a stopped local proxy) is a positively
        # broken path; only timeouts should stay probe uncertainty.
        return False
    if proxy is not None:
        raw.settimeout(deadline)
        raw.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(4096)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 200 " not in status_line:
            raw.close()
            return False
    try:
        raw.settimeout(deadline)
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.getpeercert()
    except (ssl.SSLError, OSError):
        try:
            raw.close()
        except OSError:
            pass
        return False
    return True


async def _probe_https_reachable(
    host: str, port: int, timeout_seconds: float
) -> bool:
    """HTTPS reachability probe honoring the ambient forward proxy."""

    proxy = None if host in {"localhost", "127.0.0.1", "::1"} else _proxy_connect_target()
    return await asyncio.to_thread(
        _https_reachable_blocking, host, port, timeout_seconds, proxy
    )


def _proxy_connect_target() -> tuple[str, int, str | None] | None:
    """Return ``(host, port, authorization)`` for the ambient forward proxy."""

    raw = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if not raw:
        return None
    parsed = urlparse(raw if "//" in raw else f"http://{raw}")
    if not parsed.hostname:
        return None
    authorization = None
    if parsed.username:
        credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
        authorization = "Basic " + base64.b64encode(credentials.encode()).decode()
    return parsed.hostname, parsed.port or 80, authorization


async def _probe_tcp_reachable(host: str, port: int, timeout_seconds: float) -> bool:
    """TCP-level reachability probe following the ambient proxy environment.

    A CONNECT through a forward proxy counts as reachable only on HTTP 200;
    any other CONNECT status means the proxy positively failed to reach the
    target. Uncertain outcomes (proxy connect refused, read timeout) raise so
    callers keep the bounded retry plan instead of fast-failing on a blip.
    """

    proxy = (
        None
        if host in {"localhost", "127.0.0.1", "::1"}
        else _proxy_connect_target()
    )
    if proxy is None:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout_seconds
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    proxy_host, proxy_port, authorization = proxy
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout_seconds
    )
    try:
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if authorization:
            request += f"Proxy-Authorization: {authorization}\r\n"
        writer.write(f"{request}\r\n".encode())
        await writer.drain()
        response = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout_seconds
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    status_line = response.split(b"\r\n", 1)[0]
    return b" 200" in status_line


@dataclass(frozen=True)
class ProjectLeadRuntimeContext:
    """Provider-neutral inputs required to host one persistent project Agent."""

    cwd: Path
    tools: tuple[DynamicTool, ...]
    event_handler: EventHandler | None = None
    server_request_handler: ServerRequestHandler | None = None
    # Optional per-project override for the provider's default model. Fixed
    # at native thread creation; changing it starts a new thread.
    lead_model: str | None = None


class ProjectLeadRuntime(ABC):
    """Stable contract between MAC and a persistent Project Lead backend.

    Provider implementations own their native conversation and tool protocol.
    MAC relies only on this lifecycle, so adding another Project Lead does not
    require changing the project execution loop.
    """

    provider_id: ClassVar[str]
    display_name: ClassVar[str]
    thread_id: str | None
    active_turn_id: str | None
    active_model: str | None

    @abstractmethod
    def update_bindings(
        self,
        *,
        tools: list[DynamicTool],
        event_handler: EventHandler | None,
        server_request_handler: ServerRequestHandler | None,
    ) -> None:
        """Refresh MAC-owned tools and callbacks without replacing the thread."""

    @abstractmethod
    async def create_thread(self, *, session_start_source: str | None = None) -> str:
        """Create and attach a fresh native conversation."""

    @abstractmethod
    async def resume_thread(self, thread_id: str) -> str:
        """Attach an existing native conversation."""

    @abstractmethod
    async def run_turn(
        self, text: str, *, output_schema: JsonObject | None = None
    ) -> TurnResult:
        """Run one native turn and return its terminal projection.

        An empty string starts Codex's supported empty-input continuation turn;
        it does not synthesize another user message.
        """

    async def probe_provider_reachable(self, timeout_seconds: float = 5.0) -> bool | None:
        """Best-effort probe of the provider's network path.

        ``False`` means the path is positively broken, so bounded continuation
        retries would only burn the wait budget; ``None`` means the runtime
        cannot decide and callers must keep the native retry plan unchanged.
        """

        return None

    @abstractmethod
    async def steer(self, turn_id: str | None, text: str) -> None:
        """Append user input to the currently active turn."""

    @abstractmethod
    async def interrupt(self, turn_id: str | None = None) -> bool:
        """Interrupt the active turn while preserving the conversation."""

    async def clean_background_terminals(self) -> None:
        """Stop provider-owned background terminals when the provider supports it."""

        return None

    @abstractmethod
    async def close(self) -> None:
        """Release the transport without deleting provider-owned history."""


ProjectLeadRuntimeBuilder = Callable[
    [ProjectLeadRuntimeContext], ProjectLeadRuntime
]


class ProjectLeadRuntimeRegistry:
    """Explicit composition point for Project Lead provider implementations."""

    def __init__(
        self,
        builders: Mapping[str, ProjectLeadRuntimeBuilder] | None = None,
    ) -> None:
        self._builders: dict[str, ProjectLeadRuntimeBuilder] = {}
        for provider, builder in (builders or {}).items():
            self.register(provider, builder)

    def register(self, provider: str, builder: ProjectLeadRuntimeBuilder) -> None:
        provider_id = provider.strip()
        if not provider_id:
            raise ValueError("Project Lead provider id cannot be empty")
        if provider_id in self._builders:
            raise ValueError(f"Project Lead provider already registered: {provider_id}")
        self._builders[provider_id] = builder

    def create(
        self, provider: str, context: ProjectLeadRuntimeContext
    ) -> ProjectLeadRuntime:
        try:
            builder = self._builders[provider]
        except KeyError as exc:
            available = ", ".join(sorted(self._builders)) or "none"
            raise ProjectLeadRuntimeError(
                f"Project Lead provider is not registered: {provider}; "
                f"available: {available}"
            ) from exc
        return builder(context)

    def available_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._builders))


class CodexAppServerThread(ProjectLeadRuntime):
    """Drive one persistent Codex root thread through app-server JSON-RPC."""

    provider_id = "codex"
    display_name = "Codex"

    def __init__(
        self,
        *,
        codex_bin: Path | str,
        cwd: Path,
        tools: list[DynamicTool],
        developer_instructions: str | None = None,
        model: str | None = None,
        event_handler: EventHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        request_timeout_seconds: float = 60.0,
        # A cold app-server after a host restart can legitimately take
        # minutes to answer the first ``initialize``/``thread/start``.
        # Those handshake calls use this long-but-bounded budget instead of
        # the ordinary per-request timeout, so recovery can still take over
        # when the process is truly stuck.
        startup_timeout_seconds: float | None = None,
        sandbox: str = "read-only",
        approval_policy: str = "never",
        ephemeral: bool = False,
        environments: list[JsonObject] | None = None,
        thread_config: JsonObject | None = None,
        codex_home: Path | str | None = None,
        silence_timeout_seconds: float = 300,
        continuation_max_attempts: int = 8,
        continuation_wait_budget_seconds: float = 2030,
        continuation_jitter_ratio: float = 0,
        provider_probe_enabled: bool = True,
        proxy_recovery_enabled: bool = False,
        proxy_recovery_socket: str = "/tmp/verge/verge-mihomo.sock",
        proxy_recovery_cooldown_seconds: float = 120.0,
    ) -> None:
        if not 0 <= continuation_max_attempts <= 8:
            raise ValueError("continuation_max_attempts must be between 0 and 8")
        if (
            not isfinite(continuation_wait_budget_seconds)
            or not 0 <= continuation_wait_budget_seconds <= 2030
        ):
            raise ValueError("continuation_wait_budget_seconds must be between 0 and 2030")
        if not isfinite(continuation_jitter_ratio) or not 0 <= continuation_jitter_ratio <= 1:
            raise ValueError("continuation_jitter_ratio must be between 0 and 1")
        self.codex_bin = str(codex_bin)
        self.cwd = Path(cwd).resolve()
        self.tools = {tool.name: tool for tool in tools}
        self.developer_instructions = developer_instructions
        self.model = model
        # ``thread/start`` and ``thread/resume`` report the effective model
        # selected by Codex even when the caller leaves ``model`` unset.
        self.active_model: str | None = model
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler
        self.request_timeout_seconds = request_timeout_seconds
        self.startup_timeout_seconds = (
            startup_timeout_seconds
            if startup_timeout_seconds is not None
            else max(300.0, request_timeout_seconds)
        )
        self.silence_timeout_seconds = silence_timeout_seconds
        self.continuation_max_attempts = continuation_max_attempts
        self.continuation_wait_budget_seconds = continuation_wait_budget_seconds
        self.continuation_jitter_ratio = continuation_jitter_ratio
        self.provider_probe_enabled = provider_probe_enabled
        self._proxy_recovery_enabled = proxy_recovery_enabled
        self._proxy_recovery_socket = proxy_recovery_socket
        self._proxy_recovery_cooldown_seconds = proxy_recovery_cooldown_seconds
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.ephemeral = ephemeral
        # Codex app-server treats an explicitly empty environment list as a
        # sticky thread capability boundary. With no execution environment,
        # Codex does not register shell, apply_patch, or view_image for the
        # thread, while host-provided dynamicTools remain available. ``None``
        # preserves Codex's default environment for normal coding agents.
        self.environments = environments
        self.thread_config = dict(thread_config or {})
        self.codex_home = Path(codex_home).expanduser().resolve() if codex_home else None

        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self._turn_event_generation = 0
        self._turn_terminal_event = asyncio.Event()
        self._turn_terminal_event.set()
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        # Server requests and notifications share one ordered stream. Keeping
        # separate queues and racing two ``get`` tasks can drop one message
        # when both become ready in the same loop iteration.
        self._inbound: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._stderr_lines: deque[str] = deque(maxlen=80)
        self._interrupt_generation = 0
        self._continuation_interrupt = asyncio.Event()
        self._continuation_waiting = False
        self._stream_watchdog_turn_id: str | None = None
        self._native_proxy_recovery_task: asyncio.Task[None] | None = None

    def update_bindings(
        self,
        *,
        tools: list[DynamicTool],
        event_handler: EventHandler | None,
        server_request_handler: ServerRequestHandler | None,
    ) -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler

    async def start(self) -> None:
        if (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        ):
            return
        if self._process is not None or self._reader_task is not None:
            await self.close()
        # Notifications, server requests, EOF and stderr belong to one
        # transport. Retire its readers before replacing the queue so a
        # reconnect cannot consume a previous process's closing event.
        self._inbound = asyncio.Queue()
        self._stderr_lines.clear()
        process_env = _isolated_project_process_env(os.environ)
        if self.codex_home is not None:
            self.codex_home.mkdir(parents=True, exist_ok=True)
            # Isolate root-thread sessions/configuration from the operator's
            # global MCP servers, plugins, skills and instructions. Reuse only
            # the Codex login file; never copy its secret contents.
            source_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            source_auth = source_home / "auth.json"
            target_auth = self.codex_home / "auth.json"
            if source_auth.exists() and not target_auth.exists():
                target_auth.symlink_to(source_auth)
            process_env["CODEX_HOME"] = str(self.codex_home)
        self._process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=process_env,
            start_new_session=True,
            # JSON-RPC is newline-delimited, but a restored Codex thread can
            # legitimately return one response larger than asyncio's 64 KiB
            # default StreamReader limit. Keep a bounded ceiling so long-lived
            # projects resume without LimitOverrunError.
            limit=_APP_SERVER_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(
            self._read_messages(), name="mac-codex-root-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="mac-codex-root-stderr"
        )
        try:
            try:
                await asyncio.wait_for(
                    self._request(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "mac-system",
                                "title": "MAC",
                                "version": "1.0",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                        use_timeout=False,
                    ),
                    self.startup_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise AppServerProtocolError(
                    f"initialize timed out after "
                    f"{self.startup_timeout_seconds:g}s"
                ) from exc
            await self._notify("initialized")
        except (Exception, asyncio.CancelledError):
            # A live reader alone does not make a failed handshake usable.
            # Release it here even when the caller has not yet received the
            # runtime instance, then allow its next resume to initialize anew.
            try:
                await self.close()
            except Exception:
                logger.exception(
                    "Failed to close Codex app-server after handshake failure"
                )
            raise

    async def list_models(self) -> list[JsonObject]:
        """Read the native picker catalog without creating a thread/turn."""
        await self.start()
        models: dict[str, JsonObject] = {}
        cursor = None
        seen = set()
        for _ in range(20):
            page = await self._request("model/list", {
                "cursor": cursor, "limit": 100, "includeHidden": False,
            })
            for item in page.get("data", []):
                if isinstance(item, dict) and item.get("model") and not item.get("hidden"):
                    models[str(item["model"])] = item
            cursor = page.get("nextCursor")
            if not cursor:
                return list(models.values())
            if cursor in seen:
                break
            seen.add(cursor)
        raise AppServerProtocolError("模型目录分页异常，请重试。")

    async def close(self) -> None:
        async with self._close_lock:
            await self._close()

    async def _close(self) -> None:
        if self._native_proxy_recovery_task is not None:
            self._native_proxy_recovery_task.cancel()
        process = self._process
        if process is not None:
            await stop_process_tree(process, isolated_group=True)
        self._process = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        error = AppServerProtocolError("Codex app-server closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self.active_turn_id = None
        self._turn_terminal_event.set()
        self._inbound.put_nowait({"method": _PROCESS_CLOSED_METHOD, "params": {"error": "Codex app-server closed"}})

    async def create_thread(self, *, session_start_source: str | None = None) -> str:
        await self.start()
        params: JsonObject = {
            "cwd": str(self.cwd),
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
            "dynamicTools": [tool.codex_spec() for tool in self.tools.values()],
            "ephemeral": self.ephemeral,
        }
        if session_start_source is not None:
            params["sessionStartSource"] = session_start_source
        if self.developer_instructions is not None:
            params["developerInstructions"] = self.developer_instructions
        if self.environments is not None:
            params["environments"] = self.environments
        if self.thread_config:
            params["config"] = self.thread_config
        if self.model:
            params["model"] = self.model
        # The first ``thread/start`` on a freshly spawned app-server pays the
        # same cold-start cost as the handshake, so it shares the startup
        # budget instead of the ordinary per-request timeout.
        try:
            response = await asyncio.wait_for(
                self._request("thread/start", params, use_timeout=False),
                self.startup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AppServerProtocolError(
                f"thread/start timed out after "
                f"{self.startup_timeout_seconds:g}s"
            ) from exc
        self.thread_id = self._extract_thread_id(response)
        reported_model = response.get("model")
        if isinstance(reported_model, str) and reported_model.strip():
            self.active_model = reported_model.strip()
        return self.thread_id

    async def resume_thread(self, thread_id: str) -> str:
        await self.start()
        params: JsonObject = {
            "threadId": thread_id,
            "cwd": str(self.cwd),
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
            # MAC already owns its UI/history projection and does not consume
            # restored turns.  Codex's native cheap-resume path avoids sending
            # an ever-growing rollout back as one enormous JSON line.
            "excludeTurns": True,
        }
        if self.developer_instructions is not None:
            params["developerInstructions"] = self.developer_instructions
        if self.thread_config:
            params["config"] = self.thread_config
        if self.model:
            params["model"] = self.model
        # A native resume must parse the complete rollout before it can return,
        # even when ``excludeTurns`` keeps that history out of the response.
        # Large persistent projects can legitimately take longer than the
        # ordinary JSON-RPC timeout. The startup budget allows at least five
        # minutes but bounds a stalled resume so host recovery can take over.
        try:
            response = await asyncio.wait_for(
                self._request("thread/resume", params, use_timeout=False),
                self.startup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AppServerProtocolError(
                "Codex thread/resume stopped responding after "
                f"{self.startup_timeout_seconds:g}s"
            ) from exc
        self.thread_id = self._extract_thread_id(response)
        reported_model = response.get("model")
        if isinstance(reported_model, str) and reported_model.strip():
            self.active_model = reported_model.strip()
        return self.thread_id

    async def probe_provider_reachable(self, timeout_seconds: float = 5.0) -> bool | None:
        """Probe the built-in ChatGPT provider's network path.

        All MAC-built Codex runtimes authenticate against the built-in OpenAI
        provider, so both its chat and auth hosts must answer: a proxy node
        that only reaches one leaves real turns failing. Any probe-side
        uncertainty stays ``None``. Results inform recovery and progress,
        without ending the bounded continuation plan early.
        """

        if not self.provider_probe_enabled:
            return None
        results = await asyncio.gather(
            _probe_https_reachable(_CODEX_BUILTIN_PROVIDER_HOST, 443, timeout_seconds),
            _probe_https_reachable(_CODEX_AUTH_PROVIDER_HOST, 443, timeout_seconds),
            return_exceptions=True,
        )
        if any(result is False for result in results):
            return False
        if any(isinstance(result, BaseException) for result in results):
            return None
        return True

    async def run_turn(
        self, text: str, *, output_schema: JsonObject | None = None
    ) -> TurnResult:
        async with self._turn_lock:
            self._continuation_interrupt.clear()
            interrupt_generation = self._interrupt_generation

            def retry_code(result: TurnResult) -> str | None:
                # A human stop wins over both the watchdog and retry backoff.
                if self._interrupt_generation != interrupt_generation:
                    return None
                if (
                    result.status == "interrupted"
                    and result.turn_id == self._stream_watchdog_turn_id
                ):
                    return "responseStreamDisconnected"
                return _turn_error_code(result)

            result = await self._run_turn_locked(text, output_schema=output_schema)
            events = list(result.events)
            retry_error_code = retry_code(result)
            # Only transport-reported stream failures count as proxy evidence;
            # a local stream-watchdog silence is ambiguous and must not force
            # a node switch on its own.
            stream_failure_streak = (
                1
                if _turn_error_code(result) in _STREAM_TRANSPORT_ERROR_CODES
                else 0
            )
            delays = _error_retry_delays(retry_error_code)
            if not delays:
                return result
            plan: list[float] = []
            stop_reason = "attempts_exhausted"
            for delay in delays:
                if len(plan) >= self.continuation_max_attempts:
                    stop_reason = "disabled" if not plan else "attempt_limit"
                    break
                delay *= random.uniform(1 - self.continuation_jitter_ratio, 1)
                if sum(plan) + delay > self.continuation_wait_budget_seconds:
                    stop_reason = "wait_budget"
                    break
                plan.append(delay)
            attempts = 0
            scheduled_wait = 0.0

            async def emit_retry(phase: str, **extra: Any) -> None:
                event = AgentEvent(
                    kind="mac/continuationRetry",
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                    payload={
                        "phase": phase,
                        "attempt": attempts,
                        "maxAttempts": len(plan),
                        "scheduledWaitSeconds": scheduled_wait,
                        "waitBudgetSeconds": self.continuation_wait_budget_seconds,
                        "errorCode": retry_error_code,
                        **extra,
                    },
                )
                events.append(event)
                try:
                    await self._emit(event)
                except Exception:
                    logger.warning("Continuation retry projection failed", exc_info=True)

            for attempt, delay_seconds in enumerate(plan, start=1):
                # Probe after a failed continuation to inform recovery and
                # progress. A failed probe is only a point-in-time observation:
                # the network can recover during the remaining backoff.
                reachable: bool | None = None
                if attempts >= 1:
                    reachable = await self.probe_provider_reachable()
                    if _turn_error_code(result) in _STREAM_TRANSPORT_ERROR_CODES:
                        stream_failure_streak += 1
                    else:
                        stream_failure_streak = 0
                    # A proxy node can pass short probes yet keep killing the
                    # long-lived model stream, in which case every attempt
                    # dies with the same stream-class error and probing alone
                    # never triggers a node switch. Require two consecutive
                    # transport failures before forcing a re-evaluation: one
                    # upstream disconnect is not node evidence.
                    stream_path_broken = (
                        self._proxy_recovery_enabled
                        and stream_failure_streak >= 2
                    )
                    if reachable is False or stream_path_broken:
                        # A dead local proxy node surfaces as transport-level
                        # failures. Try the existing recovery path before waiting.
                        # Ask the mihomo controller for a healthy node first,
                        # validated against every Codex provider endpoint.
                        switches = await recover_dead_proxy_nodes(
                            _PROXY_RECOVERY_TARGET_URLS,
                            socket_path=self._proxy_recovery_socket,
                            cooldown_seconds=self._proxy_recovery_cooldown_seconds,
                            force=stream_path_broken and reachable is not False,
                        )
                        if switches:
                            reachable = await self.probe_provider_reachable()
                            if reachable is True:
                                await emit_retry(
                                    "proxyRecovered",
                                    switches=[list(s) for s in switches],
                                )
                logger.warning(
                    "Codex native continuation retry %s after %s; waiting %.0fs",
                    attempt,
                    retry_error_code,
                    delay_seconds,
                )
                scheduled_wait += delay_seconds
                self._continuation_waiting = True
                try:
                    await emit_retry(
                        "waiting",
                        attempt=attempt,
                        delaySeconds=delay_seconds,
                        providerReachable=reachable,
                        retryAt=(
                            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                        ).isoformat(),
                    )
                    if self._interrupt_generation == interrupt_generation:
                        await self._wait_for_continuation(delay_seconds)
                    if self._interrupt_generation == interrupt_generation:
                        await emit_retry("resuming", attempt=attempt)
                finally:
                    self._continuation_waiting = False
                if self._interrupt_generation != interrupt_generation:
                    await emit_retry("cancelled", stopReason="user_interrupt")
                    return TurnResult(
                        thread_id=result.thread_id,
                        turn_id=result.turn_id,
                        status="interrupted",
                        final_message=result.final_message,
                        events=tuple(events),
                    )
                # The failed turn already persisted its user input and any
                # completed tool effects. Codex's supported empty-input turn
                # continues that native history without replaying the human
                # message or inventing a recovery instruction.
                attempts = attempt
                result = await self._run_turn_locked(
                    "", output_schema=output_schema
                )
                events.extend(result.events)
                if result.status == "completed":
                    await emit_retry("recovered")
                    break
                if self._interrupt_generation != interrupt_generation or (
                    result.status == "interrupted"
                    and retry_code(result) != "responseStreamDisconnected"
                ):
                    await emit_retry("cancelled", stopReason="turn_interrupted")
                    break
                # The same outage surfaces under different retryable
                # classifications across attempts (serverOverloaded,
                # httpConnectionFailed, responseStreamDisconnected, or an
                # "other" terminal message in upstream transport format).
                # Only a failure that no longer carries a bounded retry plan
                # (401/403, quota, unconfirmed "other") ends the
                # continuation, so the terminal provider error reaches the
                # user unchanged.
                if not _error_retry_delays(retry_code(result)):
                    await emit_retry("exhausted", stopReason="error_changed")
                    break
            else:
                await emit_retry("exhausted", stopReason=stop_reason)
            if (
                result.status == "interrupted"
                and retry_code(result) == "responseStreamDisconnected"
            ):
                raise AppServerProtocolError(
                    "Codex connection did not recover within the bounded "
                    "same-thread continuation attempts"
                )
            if tuple(events) == result.events:
                return result
            return TurnResult(
                thread_id=result.thread_id,
                turn_id=result.turn_id,
                status=result.status,
                final_message=result.final_message,
                events=tuple(events),
            )

    async def _wait_for_continuation(self, delay_seconds: float) -> None:
        wait_task = asyncio.create_task(_wait_for_server_capacity(delay_seconds))
        interrupt_task = asyncio.create_task(self._continuation_interrupt.wait())
        try:
            done, _pending = await asyncio.wait(
                {wait_task, interrupt_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if wait_task in done:
                await wait_task
        finally:
            for task in (wait_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wait_task, interrupt_task, return_exceptions=True)

    async def _run_turn_locked(
        self, text: str, *, output_schema: JsonObject | None = None
    ) -> TurnResult:
        if self.thread_id is None:
            await self.create_thread()
        assert self.thread_id is not None
        turn_event_generation = self._turn_event_generation
        turn_input: list[JsonObject] = []
        if text:
            turn_input.append({"type": "text", "text": text, "textElements": []})
        params: JsonObject = {
            "threadId": self.thread_id,
            "input": turn_input,
        }
        if output_schema is not None:
            # Native Codex v2 structured output. Keeping this turn-scoped
            # avoids a second model pass merely to acknowledge a terminal
            # submission tool.
            params["outputSchema"] = output_schema
        response = await self._request("turn/start", params)
        turn = response.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerProtocolError("turn/start response has no turn id")
        turn_id = turn["id"]
        if self._turn_event_generation == turn_event_generation:
            self.active_turn_id = turn_id
            self._turn_terminal_event.clear()
        current_turn_id = turn_id
        events: list[AgentEvent] = []
        final_messages: dict[str, str] = {}
        request_tasks: set[asyncio.Task[None]] = set()
        running_tools: set[str] = set()
        self._stream_watchdog_turn_id = None
        reconnect_started: float | None = None
        reconnect_errors: deque[float] = deque()
        last_proxy_attempt: float | None = None
        loop = asyncio.get_running_loop()

        def can_recover_proxy(turn_id: str) -> bool:
            return (
                self.active_turn_id == turn_id
                and reconnect_started is not None
                and not self._continuation_interrupt.is_set()
                and not running_tools
                and not any(not task.done() for task in request_tasks)
            )

        async def recover_native_proxy(turn_id: str, error_code: str) -> None:
            # Repair the network underneath Codex's native retry loop. Never
            # interrupt/restart the turn or replay its input to do so.
            try:
                if not can_recover_proxy(turn_id):
                    return
                reachable = await self.probe_provider_reachable()
                if not can_recover_proxy(turn_id):
                    return
                switches = await recover_dead_proxy_nodes(
                    _PROXY_RECOVERY_TARGET_URLS,
                    socket_path=self._proxy_recovery_socket,
                    cooldown_seconds=self._proxy_recovery_cooldown_seconds,
                    force=reachable is not False,
                )
                if switches and can_recover_proxy(turn_id):
                    event = AgentEvent(
                        "mac/continuationRetry", self.thread_id, turn_id,
                        {"phase": "proxyRecovered", "nativeRetry": True,
                         "errorCode": error_code,
                         "switches": [list(s) for s in switches]},
                    )
                    events.append(event)
                    await self._emit(event)
            except Exception:
                # A controller/projection failure must not end a live native
                # turn or hide the original provider error.
                logger.warning("Native reconnect proxy recovery failed", exc_info=True)

        try:
            while True:
                waiting_for_work = running_tools or any(
                    not task.done() for task in request_tasks
                )
                if waiting_for_work:
                    reconnect_started = None
                    if self._native_proxy_recovery_task is not None:
                        self._native_proxy_recovery_task.cancel()
                timeout = self.silence_timeout_seconds
                if reconnect_started is not None:
                    timeout = min(timeout, max(
                        0.001,
                        reconnect_started + self.silence_timeout_seconds - loop.time(),
                    ))
                    if loop.time() >= reconnect_started + self.silence_timeout_seconds:
                        # Retry notifications are not forward progress. Ask
                        # Codex to end the stalled native turn, then consume its
                        # real terminal event before reusing the existing
                        # bounded, empty-input continuation policy.
                        if self.active_turn_id == current_turn_id:
                            self._stream_watchdog_turn_id = current_turn_id
                            await self._interrupt_active_turn(current_turn_id)
                        reconnect_started = None
                try:
                    message = await asyncio.wait_for(
                        self._inbound.get(), timeout,
                    )
                except asyncio.TimeoutError as exc:
                    # Native long commands and pending user/host requests are
                    # legitimate silence. A stalled model stream must cross
                    # Codex's terminal boundary before an empty-input retry:
                    # replaying the original request could repeat tool effects.
                    if running_tools or any(not task.done() for task in request_tasks):
                        continue
                    if reconnect_started is not None:
                        continue
                    if self.active_turn_id == current_turn_id:
                        self._stream_watchdog_turn_id = current_turn_id
                        await self._interrupt_active_turn(current_turn_id)
                        continue
                    if self._turn_terminal_event.is_set() and not self._inbound.empty():
                        # The terminal notification may already be queued by
                        # the reader; consume it before deciding to fail.
                        continue
                    raise AppServerProtocolError("Codex model stream stopped responding") from exc
                method = str(message.get("method") or "")
                if method == _PROCESS_CLOSED_METHOD:
                    raise AppServerProtocolError(
                        str(
                            (message.get("params") or {}).get("error")
                            or "Codex app-server closed"
                        )
                    )
                params = message.get("params")
                params = params if isinstance(params, dict) else {}

                if "id" in message:
                    if self._native_proxy_recovery_task is not None:
                        self._native_proxy_recovery_task.cancel()
                    request_thread_id = str(
                        params.get("threadId") or self.thread_id or ""
                    )
                    request_turn_id = _event_turn_id(
                        method, params, current_turn_id
                    )
                    event = AgentEvent(
                        "tool_call" if method == "item/tool/call" else "server_request",
                        request_thread_id,
                        request_turn_id,
                        {"method": method, **params},
                    )
                    events.append(event)
                    await self._emit(event)
                    # Codex app-server can issue multiple dynamic-tool requests
                    # for one turn and waits for each response independently.
                    # Keep consuming the native stream while a slow external
                    # coding-agent runs, otherwise nominally parallel tool calls
                    # become serialized in MAC's transport layer.
                    task = asyncio.create_task(
                        self._answer_server_request(message["id"], method, params),
                        name=f"mac-codex-server-request-{message['id']}",
                    )
                    request_tasks.add(task)
                    task.add_done_callback(request_tasks.discard)
                    continue

                event = AgentEvent(
                    method,
                    str(params.get("threadId") or self.thread_id or ""),
                    _event_turn_id(method, params, current_turn_id),
                    params,
                )
                events.append(event)
                await self._emit(event)
                item = params.get("item") or {}
                is_current = (
                    event.thread_id == self.thread_id
                    and event.turn_id == current_turn_id
                )
                if is_current and method == "error" and params.get("willRetry") is True:
                    error = params.get("error") or {}
                    code = _continuation_error_code(error) if isinstance(error, dict) else None
                    if code in _STREAM_TRANSPORT_ERROR_CODES:
                        now = loop.time()
                        if reconnect_started is None:
                            reconnect_started = now
                        # Short-lived progress resets the silence watchdog,
                        # but not evidence of repeated transport failures.
                        while (
                            reconnect_errors
                            and now - reconnect_errors[0] > _NATIVE_RECONNECT_PROXY_WINDOW_SECONDS
                        ):
                            reconnect_errors.popleft()
                        reconnect_errors.append(now)
                        recovery = self._native_proxy_recovery_task
                        if (
                            self._proxy_recovery_enabled
                            and len(reconnect_errors) >= 2
                            and now - reconnect_errors[0] >= _NATIVE_RECONNECT_PROXY_GRACE_SECONDS
                            and (
                                last_proxy_attempt is None
                                or now - last_proxy_attempt >= self._proxy_recovery_cooldown_seconds
                            )
                            and (recovery is None or recovery.done())
                            and can_recover_proxy(current_turn_id)
                        ):
                            last_proxy_attempt = now
                            self._native_proxy_recovery_task = asyncio.create_task(
                                recover_native_proxy(current_turn_id, code),
                                name=f"mac-codex-native-proxy-recovery-{current_turn_id}",
                            )
                elif is_current and (
                    method.startswith(("item/agentMessage/", "item/reasoning/"))
                    or (method in {"item/started", "item/completed"} and item.get("type") in {
                        "agentMessage", "reasoning", "commandExecution", "fileChange",
                        "dynamicToolCall", "mcpToolCall", "collabAgentToolCall", "webSearch",
                    })
                ):
                    reconnect_started = None
                if is_current and method == "item/started" and item.get("type") in {
                    "commandExecution", "fileChange", "dynamicToolCall",
                    "mcpToolCall", "collabAgentToolCall", "webSearch",
                }:
                    running_tools.add(str(item.get("id")))
                    if self._native_proxy_recovery_task is not None:
                        self._native_proxy_recovery_task.cancel()
                elif is_current and method == "item/completed":
                    running_tools.discard(str(item.get("id")))
                if method == "turn/started" and params.get("threadId") == self.thread_id:
                    started_turn_id = _event_turn_id(method, params)
                    if started_turn_id:
                        # Lifecycle notifications are the authoritative source
                        # of the currently active turn. A newer turn can win a
                        # race with the turn/start response, so the waiter must
                        # follow the same identity as steer/interrupt instead
                        # of remaining pinned to the response's stale id.
                        if started_turn_id != current_turn_id:
                            reconnect_started = None
                            reconnect_errors.clear()
                            if self._native_proxy_recovery_task is not None:
                                self._native_proxy_recovery_task.cancel()
                            running_tools.clear()
                        current_turn_id = started_turn_id
                if method == "item/completed":
                    item = params.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        value = item.get("text")
                        if (
                            event.thread_id == self.thread_id
                            and event.turn_id
                            and isinstance(value, str)
                            and value.strip()
                        ):
                            final_messages[event.turn_id] = value
                if method == "turn/completed" and params.get("threadId") == self.thread_id:
                    completed = params.get("turn")
                    completed = completed if isinstance(completed, dict) else {}
                    completed_turn_id = str(completed.get("id") or "")
                    if completed_turn_id and completed_turn_id != current_turn_id:
                        continue
                    # Codex defines turn/completed as the authoritative end of
                    # the turn.  Do not extend that lifecycle by waiting on a
                    # client-side request task: the app-server may already have
                    # resolved/cancelled the request while a presentation hook
                    # is still unwinding.  The finally block cancels any such
                    # stale local task before the thread is released.
                    return TurnResult(
                        thread_id=self.thread_id,
                        turn_id=completed_turn_id or current_turn_id,
                        status=str(completed.get("status") or "unknown"),
                        final_message=final_messages.get(current_turn_id),
                        events=tuple(events),
                    )
        finally:
            if self._native_proxy_recovery_task is not None:
                self._native_proxy_recovery_task.cancel()
                await asyncio.gather(self._native_proxy_recovery_task, return_exceptions=True)
                self._native_proxy_recovery_task = None
            pending_requests = [task for task in request_tasks if not task.done()]
            for task in pending_requests:
                task.cancel()
            if pending_requests:
                await asyncio.gather(*pending_requests, return_exceptions=True)

    async def _answer_server_request(
        self, request_id: Any, method: str, params: JsonObject
    ) -> None:
        """Answer one native app-server request without blocking its reader loop."""

        try:
            if method == "item/tool/call":
                await self._execute_tool_request(request_id, params)
            elif self.server_request_handler is not None:
                try:
                    response = self.server_request_handler(method, params)
                    if inspect.isawaitable(response):
                        response = await response
                    await self._respond_result(request_id, response)
                except Exception as exc:
                    await self._respond_error(request_id, str(exc))
            else:
                await self._respond_error(
                    request_id, f"Unsupported request: {method}"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A process/transport failure is surfaced by the main reader loop;
            # do not let a detached response task produce an unobserved error.
            logger.exception(
                "Failed to answer Codex app-server request %s (%s)",
                request_id,
                method,
            )

    async def steer(self, turn_id: str | None, text: str) -> None:
        if self.thread_id is None:
            raise AppServerProtocolError("Cannot steer before creating a thread")
        target_turn_id = self.active_turn_id or turn_id
        if not target_turn_id:
            raise AppServerProtocolError(
                "Cannot steer without an active turn"
            )
        try:
            await self._request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": target_turn_id,
                    "input": [{"type": "text", "text": text, "textElements": []}],
                },
            )
        except AppServerProtocolError:
            replacement_turn_id = self.active_turn_id
            if replacement_turn_id and replacement_turn_id != target_turn_id:
                await self._request(
                    "turn/steer",
                    {
                        "threadId": self.thread_id,
                        "expectedTurnId": replacement_turn_id,
                        "input": [
                            {"type": "text", "text": text, "textElements": []}
                        ],
                    },
                )
            else:
                raise

    async def interrupt(self, turn_id: str | None = None) -> bool:
        """Interrupt the actually active native turn."""

        self._interrupt_generation += 1
        self._continuation_interrupt.set()
        if self._native_proxy_recovery_task is not None:
            self._native_proxy_recovery_task.cancel()
        if self._continuation_waiting:
            return True
        return await self._interrupt_active_turn(turn_id)

    async def _interrupt_active_turn(self, turn_id: str | None = None) -> bool:
        if self.thread_id is None:
            raise AppServerProtocolError("Cannot interrupt before creating a thread")
        target_turn_id = self.active_turn_id or turn_id
        if not target_turn_id:
            return False
        try:
            await self._request(
                "turn/interrupt",
                {"threadId": self.thread_id, "turnId": target_turn_id},
            )
        except AppServerProtocolError:
            # A goal can cross a turn boundary while the interrupt request is
            # in flight. Retry once only when the native notification stream
            # has supplied a different current id; never parse provider error
            # strings or invent routing rules for individual failures.
            replacement_turn_id = self.active_turn_id
            if replacement_turn_id and replacement_turn_id != target_turn_id:
                await self._request(
                    "turn/interrupt",
                    {
                        "threadId": self.thread_id,
                        "turnId": replacement_turn_id,
                    },
                )
            elif self.active_turn_id is None:
                return True
            else:
                raise
        # The JSON-RPC response only acknowledges the request. Codex defines
        # turn/completed(status=interrupted) as the authoritative boundary, so
        # MAC must not project "stopped" before that native event arrives.
        if self.active_turn_id is not None:
            try:
                await asyncio.wait_for(
                    self._turn_terminal_event.wait(),
                    timeout=self.request_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise AppServerProtocolError(
                    "turn/interrupt was acknowledged but no terminal event arrived"
                ) from exc
        return True

    async def clean_background_terminals(self) -> None:
        """Stop every background terminal owned by this native Codex thread."""

        if self.thread_id is None:
            return
        await self._request(
            "thread/backgroundTerminals/clean", {"threadId": self.thread_id}
        )

    def _observe_notification(self, message: JsonObject) -> None:
        """Maintain live turn identity directly from native events."""

        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        event_thread_id = str(params.get("threadId") or "")
        if self.thread_id and event_thread_id and event_thread_id != self.thread_id:
            return
        if method == "turn/started":
            self._turn_event_generation += 1
            self.active_turn_id = _event_turn_id(method, params)
            self._turn_terminal_event.clear()
        elif method == "turn/completed":
            self._turn_event_generation += 1
            completed_turn_id = _event_turn_id(method, params)
            if not self.active_turn_id or completed_turn_id == self.active_turn_id:
                self.active_turn_id = None
                self._turn_terminal_event.set()

    async def _execute_tool_request(self, request_id: Any, params: JsonObject) -> None:
        name = params.get("tool")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            await self._respond_tool(request_id, f"Unknown MAC tool: {name}", success=False)
            return
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            context = ToolContext(
                thread_id=str(params.get("threadId") or ""),
                turn_id=str(params.get("turnId") or ""),
                call_id=str(params.get("callId") or ""),
                tool_name=tool.name,
            )
            handler_parameters = inspect.signature(tool.handler).parameters
            result = (
                tool.handler(arguments, context)
                if len(handler_parameters) >= 2
                else tool.handler(arguments)
            )
            if inspect.isawaitable(result):
                result = await result
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            await self._respond_tool(request_id, text, success=True)
        except Exception as exc:
            await self._respond_tool(request_id, str(exc), success=False)

    async def _respond_tool(self, request_id: Any, text: str, *, success: bool) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "contentItems": [{"type": "inputText", "text": text}],
                    "success": success,
                },
            }
        )

    async def _respond_error(self, request_id: Any, message: str) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": message},
            }
        )

    async def _respond_result(self, request_id: Any, result: JsonObject) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _request(
        self,
        method: str,
        params: JsonObject,
        *,
        use_timeout: bool = True,
    ) -> JsonObject:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            message = (
                await asyncio.wait_for(future, self.request_timeout_seconds)
                if use_timeout
                else await future
            )
        except asyncio.TimeoutError as exc:
            reader_failed = self._reader_task is None or self._reader_task.done()
            detail = " after the app-server reader stopped" if reader_failed else ""
            logger.warning(
                "Codex app-server %s timed out (request_id=%s)%s",
                method, request_id, detail,
            )
            raise AppServerProtocolError(
                f"{method} timed out after {self.request_timeout_seconds:g}s{detail}"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # reset may close while stdin is blocked
        error = message.get("error")
        if isinstance(error, dict):
            raise AppServerProtocolError(
                f"{method} failed: {error.get('message') or error}"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerProtocolError(f"{method} returned a non-object result")
        return result

    async def _notify(self, method: str, params: JsonObject | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerProtocolError("Codex app-server is not running")
        data = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            process.stdin.write((data + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AppServerProtocolError("Invalid JSON-RPC from app-server") from exc
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    self._observe_notification(message)
                    await self._inbound.put(message)
                    continue
                request_id = message.get("id")
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
            await self._process.wait()
            await asyncio.sleep(0)
            stderr = "\n".join(self._stderr_lines)
            error = AppServerProtocolError(
                "Codex app-server stdout closed"
                + (f"; stderr: {stderr[-4000:]}" if stderr else "")
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            await self._inbound.put(
                {"method": _PROCESS_CLOSED_METHOD, "params": {"error": str(error)}}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
            await self._inbound.put(
                {"method": _PROCESS_CLOSED_METHOD, "params": {"error": str(exc)}}
            )

    async def _drain_stderr(self) -> None:
        """Continuously drain stderr so a verbose app-server cannot deadlock."""
        assert self._process is not None and self._process.stderr is not None
        while line := await self._process.stderr.readline():
            message = line.decode("utf-8", errors="replace").rstrip()
            if message:
                self._stderr_lines.append(message)
                logger.debug("Codex app-server stderr: %s", message)

    async def _emit(self, event: AgentEvent) -> None:
        if self.event_handler is None:
            return
        result = self.event_handler(event)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _extract_thread_id(response: JsonObject) -> str:
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerProtocolError("thread response has no thread id")
        return thread["id"]
