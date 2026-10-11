"""Thin compatibility facade around the native project Root Agent.

This class remains only so API and Feishu wiring share one dependency object.
It is not an Agent and contains no workflow, stage, retry or feedback policy.
"""

from __future__ import annotations

from typing import Any, Callable

from mac_system.agents.runtime import AgentRuntime
from mac_system.config import get_settings, load_agent_config
from mac_system.core.project_service import ProjectService
from mac_system.core.root_agent import RootAgentEngine, schedule_root_turn
from mac_system.core.root_sessions import RootSessionStore


class OrchestratorError(Exception):
    pass


class AgentOrchestrator:
    """Application wiring for one Root Agent; not a workflow orchestrator."""

    def __init__(
        self,
        project_service: ProjectService,
        runtime: AgentRuntime,
        prompt_renderer: Any = None,
        feishu_bridge: Any = None,
        lock_manager: Any = None,
        background_scheduler: Callable[[str], None] | None = None,
    ) -> None:
        self.project_service = project_service
        self.runtime = runtime
        self.feishu_bridge = feishu_bridge
        self.settings = get_settings()
        self.background_scheduler = background_scheduler

    def _engine(self) -> RootAgentEngine:
        return RootAgentEngine(
            self.project_service.session,
            self.runtime,
            self.settings,
            bot_account_id=self.project_service.bot_account_id,
            feishu_bridge=self.feishu_bridge,
        )

    async def run_root_agent(
        self, project_id: str, trigger: str | None = None
    ):
        project = self.project_service.require_project(project_id)
        from mac_system.feishu.accounts import create_feishu_bridge

        bridge = create_feishu_bridge(self.settings, project.bot_account_id)
        engine = RootAgentEngine(
            self.project_service.session,
            self.runtime,
            self.settings,
            bot_account_id=project.bot_account_id,
            feishu_bridge=bridge or self.feishu_bridge,
        )
        try:
            return await engine.run(
                project_id,
                trigger
                if trigger is not None
                else str(project.description or "继续当前项目"),
            )
        finally:
            if bridge is not None:
                await bridge.close()

    def schedule_root_agent(
        self,
        project_id: str,
        trigger: str,
        *,
        actor_open_id: str | None = None,
        visible_request: str | None = None,
        request_origin: str = "human",
        parent_decision_id: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> None:
        schedule_root_turn(
            self.project_service.session.get_bind(),
            self.runtime,
            self.settings,
            project_id,
            trigger,
            actor_open_id=actor_open_id,
            visible_request=visible_request,
            request_origin=request_origin,
            parent_decision_id=parent_decision_id,
            reply_context=reply_context,
        )

    def provider_catalog(self) -> list[dict[str, Any]]:
        """Return configured coding backends and measured availability.

        This is presentation evidence only.  Root still chooses an enabled
        provider through the generic dynamic tool, and ordinary messages are
        never routed here by keywords.
        """

        configured = load_agent_config(self.settings.agent_config_path).get(
            "runtimes", {}
        )
        configured = configured if isinstance(configured, dict) else {}
        available = set(self.runtime.available_providers())
        profiles = self.runtime.provider_profiles()
        names = sorted(set(configured) | available)
        metrics = RootSessionStore(self.project_service.session).provider_metrics(names)
        catalog: list[dict[str, Any]] = []
        for name in names:
            raw = configured.get(name)
            raw = raw if isinstance(raw, dict) else {}
            enabled = raw.get("enabled", True) is True
            actual = name in available
            if actual:
                status = "available"
            elif not enabled or raw.get("mode") == "desktop_only":
                status = "disabled"
            else:
                status = "unavailable"
            profile = dict(profiles.get(name) or {})
            catalog.append(
                {
                    "provider": name,
                    "status": status,
                    "description": profile.get("description")
                    or raw.get("description"),
                    "model": profile.get("model") or raw.get("model"),
                    "mode": profile.get("mode") or raw.get("mode"),
                    "metrics": metrics.get(name, {"sample_count": 0}),
                }
            )
        return catalog

    async def steer_root_agent(
        self,
        project_id: str,
        text: str,
        *,
        actor_open_id: str | None = None,
        visible_request: str | None = None,
        reply_context: dict[str, Any] | None = None,
    ) -> bool:
        return await self._engine().steer(
            project_id,
            text,
            actor_open_id=actor_open_id,
            visible_request=visible_request,
            reply_context=reply_context,
        )

    async def rotate_root_progress_for_timeline_event(
        self, project_id: str
    ) -> bool:
        """Freeze the older progress card before a newer human event."""

        return await self._engine().rotate_progress_for_timeline_event(project_id)

    async def start_new_root_thread(
        self, project_id: str
    ) -> tuple[str | None, str | None]:
        """Apply the explicit project-level new-thread control."""

        return await self._engine().start_new_thread(project_id)

    async def list_root_models(self, project_id: str) -> list[dict[str, Any]]:
        return await self._engine().list_lead_models(project_id)

    async def set_root_model(
        self, project_id: str, model: str | None
    ) -> tuple[str | None, str | None]:
        """Apply the explicit project-level ``/model`` control."""

        return await self._engine().set_lead_model(project_id, model)

    async def set_root_provider(
        self, project_id: str, provider: str
    ) -> tuple[str, str]:
        """Apply the explicit project-level ``/agent`` control."""

        return await self._engine().set_lead_provider(project_id, provider)

    async def tick(self, project_id: str) -> None:
        await self.run_root_agent(project_id)

    async def tick_until_stable(
        self, project_id: str, *, max_ticks: int = 1
    ) -> None:
        # A Codex turn owns its own tool loop and runs until its natural
        # boundary. Repeated outer ticks would duplicate the user's request.
        del max_ticks
        await self.run_root_agent(project_id)

    def _has_immediate_work(self, project_id: str) -> bool:
        project = self.project_service.require_project(project_id)
        return project.status == "active" and project.execution_status == "idle"

    def _schedule_followup_tick(self, project_id: str) -> None:
        if self.background_scheduler is not None:
            self.background_scheduler(project_id)
        else:
            self.schedule_root_agent(project_id, "继续当前项目")

    async def cancel_project(self, project_id: str, reason: str = "") -> None:
        project = self.project_service.require_project(project_id)
        running_task_ids = [
            task.id for task in self.project_service._running_tasks(project_id)
        ]
        await self.project_service.cancel_project(
            project_id, reason=reason, triggered_by="user"
        )
        for task_id in running_task_ids:
            try:
                self.runtime.cancel(task_id)
            except Exception:
                pass

        # Root turn interruption is best effort; the durable cancelled project
        # remains authoritative even if the provider is already terminal.
        try:
            await self._engine().interrupt(project.id)
        except Exception:
            pass

    async def stop_root_turn(self, project_id: str) -> bool:
        """Interrupt current work while preserving the Project and Root thread."""

        running_task_ids = [
            task.id for task in self.project_service._running_tasks(project_id)
        ]
        for task_id in running_task_ids:
            try:
                self.runtime.cancel(task_id)
            except Exception:
                pass
        interrupted = await self._engine().interrupt(project_id)
        return interrupted

    async def reset_root_runtime(self, project_id: str) -> None:
        await self._engine().reset_project_runtime(project_id)
