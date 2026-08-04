"""Thin compatibility facade around the native project Root Agent.

This class remains only so API and Feishu wiring share one dependency object.
It is not an Agent and contains no workflow, stage, retry or feedback policy.
"""

from __future__ import annotations

from typing import Any, Callable

from mac_system.agents.runtime import AgentRuntime
from mac_system.config import get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.root_agent import RootAgentEngine, schedule_root_turn


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
            feishu_bridge=self.feishu_bridge,
        )

    async def run_root_agent(
        self, project_id: str, trigger: str | None = None
    ):
        project = self.project_service.require_project(project_id)
        return await self._engine().run(
            project_id,
            trigger if trigger is not None else str(project.description or "继续当前项目"),
        )

    def schedule_root_agent(self, project_id: str, trigger: str) -> None:
        schedule_root_turn(
            self.project_service.session.get_bind(),
            self.runtime,
            self.settings,
            project_id,
            trigger,
        )

    async def steer_root_agent(self, project_id: str, text: str) -> bool:
        return await self._engine().steer(project_id, text)

    async def start_new_root_thread(self, project_id: str) -> tuple[str | None, str]:
        """Apply the explicit project-level new-thread control."""

        return await self._engine().start_new_thread(project_id)

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
