"""MAC command-line entry point."""

from __future__ import annotations

import asyncio
import sys

import click
import uvicorn

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import build_agent_runtime
from mac_system.config import get_agent_config, get_settings
from mac_system.core.project_service import ProjectService
from mac_system.database import init_db, session_scope


@click.group()
def cli() -> None:
    """MAC: Codex on a mobile-friendly Feishu surface."""

    init_db()


@cli.command("server")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=get_settings().mcs_port, type=int)
def server(host: str, port: int) -> None:
    """Start the API and Feishu WebSocket service without hot reload."""

    uvicorn.run("mac_system.api.main:app", host=host, port=port, reload=False)


@cli.command("create-project")
@click.option("--name", required=True)
@click.option("--description", required=True)
@click.option("--directory", default=None)
def create_project(name: str, description: str, directory: str | None) -> None:
    """Create one persistent native Codex project thread."""

    async def create() -> None:
        with session_scope() as session:
            project = await ProjectService(session).create_project(
                name=name,
                task_type="feature",
                description=description,
                bound_directory=directory,
                initial_context={"orchestration_mode": "root_agent"},
            )
            click.echo(project.id)
            click.echo(project.workspace_path)

    asyncio.run(create())


@cli.command("status")
@click.argument("project_id")
def status(project_id: str) -> None:
    with session_scope() as session:
        project = ProjectService(session).get_project(project_id)
        if project is None:
            click.echo("PROJECT_NOT_FOUND", err=True)
            raise SystemExit(1)
        click.echo(f"{project.id} {project.status} {project.execution_status}")
        click.echo(project.workspace_path)


@cli.command("run")
@click.option("--name", required=True)
@click.option("--description", required=True)
@click.option("--directory", default=None)
def run(name: str, description: str, directory: str | None) -> None:
    """Create a project and run exactly one Codex turn."""

    async def execute() -> None:
        settings = get_settings()
        with session_scope() as session:
            service = ProjectService(session)
            project = await service.create_project(
                name=name,
                task_type="feature",
                description=description,
                bound_directory=directory,
                initial_context={"orchestration_mode": "root_agent"},
            )
            orchestrator = AgentOrchestrator(
                service,
                build_agent_runtime(settings, get_agent_config()),
            )
            result = await orchestrator.run_root_agent(project.id, description)
            click.echo(result.final_message or result.status)

    try:
        asyncio.run(execute())
    except Exception as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
