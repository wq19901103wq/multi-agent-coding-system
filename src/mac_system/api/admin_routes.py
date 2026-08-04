"""Small read-mostly admin view for native Codex project threads."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.config import get_settings
from mac_system.core.project_service import ProjectNotFoundError, ProjectService
from mac_system.models import RootAgentSession

from mac_system.api.dependencies import get_orchestrator, get_project_service


router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_token(token: str | None = Query(None)) -> str | None:
    if get_settings().admin_token and token != get_settings().admin_token:
        raise HTTPException(status_code=403, detail="INVALID_ADMIN_TOKEN")
    return token


def _time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")


def _page(title: str, body: str, token: str | None) -> str:
    suffix = f"?token={html.escape(token)}" if token else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f8fa;color:#24292f;margin:0;padding:24px}}
main{{max-width:1200px;margin:auto}}nav,.card{{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:18px;margin-bottom:18px}}
nav a{{color:#0969da;text-decoration:none;font-weight:600}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #d8dee4;text-align:left;vertical-align:top}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}}.muted{{color:#57606a}}button{{padding:6px 10px;margin-right:6px}}
</style></head><body><main><nav><a href="/admin/projects{suffix}">MAC 项目</a></nav>{body}</main></body></html>"""


@router.get("/projects", response_class=HTMLResponse)
def projects(
    token: str | None = Depends(_admin_token),
    service: ProjectService = Depends(get_project_service),
) -> HTMLResponse:
    rows = []
    for project in service.list_projects():
        root = service.session.exec(
            select(RootAgentSession).where(
                RootAgentSession.project_id == project.id
            )
        ).first()
        root_status = root.status if root else "not_started"
        suffix = f"?token={html.escape(token)}" if token else ""
        rows.append(
            "<tr>"
            f'<td><a href="/admin/projects/{html.escape(project.id)}{suffix}">{html.escape(project.name or project.id)}</a></td>'
            f"<td>{html.escape(project.status)}</td>"
            f"<td>{html.escape(project.execution_status)}</td>"
            f"<td>{html.escape(root_status)}</td>"
            f'<td class="mono">{html.escape(project.workspace_path)}</td>'
            f"<td>{_time(project.updated_at)}</td>"
            "</tr>"
        )
    body = (
        '<section class="card"><h1>项目</h1><table><thead><tr>'
        "<th>项目</th><th>生命周期</th><th>执行</th><th>Codex</th><th>工作目录</th><th>更新</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )
    return HTMLResponse(_page("MAC 项目", body, token))


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    project_id: str,
    token: str | None = Depends(_admin_token),
    service: ProjectService = Depends(get_project_service),
) -> HTMLResponse:
    try:
        project = service.require_project(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from exc
    root = service.session.exec(
        select(RootAgentSession).where(
            RootAgentSession.project_id == project.id
        )
    ).first()
    events = service.get_project_events(project.id, limit=80)
    event_rows = "".join(
        "<tr>"
        f"<td>{_time(event.created_at)}</td>"
        f"<td>{html.escape(event.level)}</td>"
        f"<td>{html.escape(event.message)}</td>"
        "</tr>"
        for event in events
    )
    token_field = f'<input type="hidden" name="token" value="{html.escape(token)}">' if token else ""
    controls = ""
    if project.status != "cancelled":
        controls = (
            f'<form method="post" action="/admin/projects/{project.id}/cancel">{token_field}'
            '<button type="submit">取消项目</button></form>'
        )
    body = f"""
<section class="card"><h1>{html.escape(project.name or project.id)}</h1>
<p><strong>项目：</strong><span class="mono">{html.escape(project.id)}</span></p>
<p><strong>生命周期：</strong>{html.escape(project.status)}　<strong>执行：</strong>{html.escape(project.execution_status)}</p>
<p><strong>Codex thread：</strong><span class="mono">{html.escape(root.native_thread_id if root and root.native_thread_id else '-')}</span></p>
<p><strong>当前 turn：</strong><span class="mono">{html.escape(root.active_turn_id if root and root.active_turn_id else '-')}</span></p>
<p><strong>工作目录：</strong><span class="mono">{html.escape(project.workspace_path)}</span></p>{controls}</section>
<section class="card"><h2>原生事件</h2><table><thead><tr><th>时间</th><th>级别</th><th>事件</th></tr></thead><tbody>{event_rows}</tbody></table></section>
"""
    return HTMLResponse(_page(project.name or project.id, body, token))


@router.get("/projects/{project_id}/profile")
def project_profile(
    project_id: str,
    _token: str | None = Depends(_admin_token),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, object]:
    project = service.require_project(project_id)
    root = service.session.exec(
        select(RootAgentSession).where(
            RootAgentSession.project_id == project.id
        )
    ).first()
    return {
        "project_id": project.id,
        "execution_status": project.execution_status,
        "native_thread_id": root.native_thread_id if root else None,
        "active_turn_id": root.active_turn_id if root else None,
        "event_count": root.event_cursor if root else 0,
        "last_error": root.last_error if root else None,
    }


@router.post("/projects/{project_id}/cancel")
async def cancel_project(
    project_id: str,
    token: str | None = Depends(_admin_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> RedirectResponse:
    await orchestrator.cancel_project(project_id, reason="管理员取消")
    suffix = f"?token={token}" if token else ""
    return RedirectResponse(f"/admin/projects/{project_id}{suffix}", status_code=303)


@router.post("/projects/{project_id}/tick")
async def tick_project(
    project_id: str,
    token: str | None = Depends(_admin_token),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> RedirectResponse:
    orchestrator.schedule_root_agent(project_id, "继续当前任务")
    suffix = f"?token={token}" if token else ""
    return RedirectResponse(f"/admin/projects/{project_id}{suffix}", status_code=303)
