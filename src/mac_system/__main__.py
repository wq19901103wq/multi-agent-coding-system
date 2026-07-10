"""MAC System CLI entry point."""

import asyncio
import os
import sys
from pathlib import Path

import click
import uvicorn

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import ClaudeCodeRuntime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.database import init_db, session_scope
from mac_system.state_machine import StateMachine


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
def cli(debug: bool) -> None:
    """Multi-Agent Coding System CLI."""
    settings = get_settings()
    if debug:
        # Re-create settings with debug enabled is not trivial due to lru_cache;
        # just set the env var for subprocesses.
        os.environ["DEBUG"] = "true"
    init_db()


@cli.command("server")
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--port", default=8000, help="Bind port.")
@click.option("--reload", is_flag=True, help="Enable auto-reload.")
def server(host: str, port: int, reload: bool) -> None:
    """Start the FastAPI server."""
    uvicorn.run(
        "mac_system.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@cli.command("create-project")
@click.option("--name", required=True, help="Project name.")
@click.option("--task-type", default="feature", help="Task type (feature, bug_fix, documentation).")
@click.option("--description", required=True, help="Task description.")
@click.option("--approval-mode", default="auto", help="Approval mode (strict, normal, night, auto).")
@click.option("--creator-open-id", default=None, help="Creator Feishu open_id.")
def create_project(
    name: str,
    task_type: str,
    description: str,
    approval_mode: str,
    creator_open_id: str | None,
) -> None:
    """Create a new project and advance it from INIT."""
    with session_scope() as session:
        service = ProjectService(session)
        project = asyncio.run(
            service.create_project(
                name=name,
                task_type=task_type,
                description=description,
                approval_mode=approval_mode,
                creator_open_id=creator_open_id,
            )
        )
        click.echo(f"Created project {project.id}")
        click.echo(f"Workspace: {project.workspace_path}")
        click.echo(f"Current state: {project.current_state}")


@cli.command("status")
@click.argument("project_id")
def status(project_id: str) -> None:
    """Show project status."""
    with session_scope() as session:
        service = ProjectService(session)
        project = service.get_project(project_id)
        if project is None:
            click.echo(f"Project {project_id} not found", err=True)
            sys.exit(1)
        click.echo(f"Project: {project.id}")
        click.echo(f"Name: {project.name}")
        click.echo(f"Status: {project.status}")
        click.echo(f"Current state: {project.current_state}")
        click.echo(f"Previous state: {project.previous_state}")
        click.echo(f"Workspace: {project.workspace_path}")


@cli.command("tick")
@click.argument("project_id")
def tick(project_id: str) -> None:
    """Execute a single orchestrator tick for a project."""
    settings = get_settings()

    async def _tick() -> None:
        with session_scope() as session:
            service = ProjectService(session)
            wm = WorktreeManager(settings.projects_dir)
            renderer = PromptRenderer(get_agent_config())
            runtime = ClaudeCodeRuntime(
                claude_bin=settings.claude_bin,
                anthropic_api_key=settings.anthropic_api_key,
                anthropic_auth_token=settings.anthropic_auth_token,
                claude_model=settings.claude_model,
                anthropic_base_url=settings.anthropic_base_url,
                anthropic_default_opus_model=settings.anthropic_default_opus_model,
                anthropic_default_sonnet_model=settings.anthropic_default_sonnet_model,
                anthropic_default_haiku_model=settings.anthropic_default_haiku_model,
                claude_code_effort_level=settings.claude_code_effort_level,
                api_timeout_ms=settings.api_timeout_ms,
            )
            orchestrator = AgentOrchestrator(service, wm, runtime, renderer)
            await orchestrator.tick(project_id)
            project = service.get_project(project_id)
            click.echo(f"Tick complete. Current state: {project.current_state}")

    asyncio.run(_tick())


@cli.command("run")
@click.option("--name", required=True, help="Project name.")
@click.option("--task-type", default="feature", help="Task type.")
@click.option("--description", required=True, help="Task description.")
@click.option("--approval-mode", default="auto", help="Approval mode.")
@click.option("--max-ticks", default=50, help="Maximum ticks before aborting.")
@click.option("--auto-accept", is_flag=True, help="Auto-confirm ACCEPTANCE_PENDING (use with care).")
@click.option("--agent-timeout", default=None, type=int, help="Per-agent timeout in seconds (overrides AGENT_TIMEOUT_SECONDS).")
def run(
    name: str,
    task_type: str,
    description: str,
    approval_mode: str,
    max_ticks: int,
    auto_accept: bool,
    agent_timeout: int | None,
) -> None:
    """Create a project and run orchestrator ticks until terminal or human pending."""
    settings = get_settings()

    async def _run() -> None:
        if agent_timeout is not None:
            os.environ["AGENT_TIMEOUT_SECONDS"] = str(agent_timeout)
            get_settings.cache_clear()
        run_settings = get_settings()

        with session_scope() as session:
            service = ProjectService(session)
            project = await service.create_project(
                name=name,
                task_type=task_type,
                description=description,
                approval_mode=approval_mode,
            )
            project_id = project.id
            click.echo(f"Created project {project_id} at state {project.current_state}")

        wm = WorktreeManager(run_settings.projects_dir)
        renderer = PromptRenderer(get_agent_config())
        runtime = ClaudeCodeRuntime(
            claude_bin=run_settings.claude_bin,
            anthropic_api_key=run_settings.anthropic_api_key,
            anthropic_auth_token=run_settings.anthropic_auth_token,
            claude_model=run_settings.claude_model,
            anthropic_base_url=run_settings.anthropic_base_url,
            anthropic_default_opus_model=run_settings.anthropic_default_opus_model,
            anthropic_default_sonnet_model=run_settings.anthropic_default_sonnet_model,
            anthropic_default_haiku_model=run_settings.anthropic_default_haiku_model,
            claude_code_effort_level=run_settings.claude_code_effort_level,
            api_timeout_ms=run_settings.api_timeout_ms,
        )

        for i in range(max_ticks):
            with session_scope() as session:
                service = ProjectService(session)
                project = service.get_project(project_id)
                state_machine = StateMachine(project.task_type)
                if project.status in ("paused", "cancelled"):
                    click.echo(f"Project {project_id} is {project.status}. Stopping.")
                    break
                if state_machine.is_terminal(project.current_state):
                    click.echo(f"Reached terminal state {project.current_state}")
                    break

                if project.current_state == "ACCEPTANCE_PENDING" and not auto_accept:
                    click.echo(
                        f"Project {project_id} reached ACCEPTANCE_PENDING. "
                        "Run with --auto-accept to continue, or use the API to submit a decision."
                    )
                    break

                orchestrator = AgentOrchestrator(service, wm, runtime, renderer)
                await orchestrator.tick(project_id)
                project = service.get_project(project_id)
                click.echo(f"Tick {i + 1}: {project.current_state}")

                if project.current_state == "ACCEPTANCE_PENDING" and auto_accept:
                    await service.transition_state(
                        project_id, "confirm", triggered_by="cli_auto_accept"
                    )
                    project = service.get_project(project_id)
                    click.echo(f"Auto-accepted, state: {project.current_state}")

    try:
        asyncio.run(_run())
    except Exception as e:
        click.echo(f"Run failed: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
