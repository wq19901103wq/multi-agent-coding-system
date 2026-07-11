"""Simple admin panel for monitoring and managing projects."""

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from mac_system.config import get_settings
from mac_system.core.project_service import ProjectNotFoundError, ProjectService
from mac_system.state_machine import StateMachine

from mac_system.api.dependencies import get_orchestrator, get_project_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_token_dependency(token: str | None = Query(None)) -> str | None:
    if get_settings().admin_token and token != get_settings().admin_token:
        raise HTTPException(status_code=403, detail="INVALID_ADMIN_TOKEN")
    return token


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")


def _render_projects_page(
    projects: list[Any],
    total: int,
    offset: int,
    limit: int,
    token: str | None,
    base_url: str,
) -> str:
    token_param = f"&token={html.escape(token)}" if token else ""
    rows = []
    for p in projects:
        sm = StateMachine(p.task_type)
        states = [s for s in sm.states if s != p.current_state]
        state_options = "".join(
            f'<option value="{html.escape(s)}">{html.escape(sm.display_name(s))}</option>'
            for s in states
        )
        pending = ""
        status = service.get_project_status(p.id) if 'service' in locals() else None
        # Avoid calling service here; keep page lightweight.
        rows.append(
            f"""
            <tr>
              <td>{html.escape(p.id)}</td>
              <td>{html.escape(p.name or "-")}</td>
              <td>{html.escape(p.task_type)}</td>
              <td>{html.escape(p.status)}</td>
              <td>{html.escape(p.current_state)} ({html.escape(sm.display_name(p.current_state))})</td>
              <td>{_fmt_dt(p.created_at)}</td>
              <td>{_fmt_dt(p.updated_at)}</td>
              <td>${p.total_cost_usd or 0:.4f}</td>
              <td>
                <form method="post" action="{base_url}/admin/projects/{html.escape(p.id)}/cancel?{token_param.lstrip('&')}" style="display:inline">
                  <button type="submit" onclick="return confirm('确定终止？')">终止</button>
                </form>
                <form method="post" action="{base_url}/admin/projects/{html.escape(p.id)}/rollback?{token_param.lstrip('&')}" style="display:inline">
                  <select name="target_state" required>{state_options}</select>
                  <button type="submit" onclick="return confirm('确定回退？')">回退</button>
                </form>
                <form method="post" action="{base_url}/admin/projects/{html.escape(p.id)}/tick?{token_param.lstrip('&')}" style="display:inline">
                  <button type="submit">tick</button>
                </form>
              </td>
            </tr>
            """
        )

    prev_link = ""
    next_link = ""
    if offset > 0:
        prev_offset = max(0, offset - limit)
        prev_link = f'<a href="{base_url}/admin/projects?offset={prev_offset}&limit={limit}{token_param}">上一页</a>'
    if offset + limit < total:
        next_offset = offset + limit
        next_link = f'<a href="{base_url}/admin/projects?offset={next_offset}&limit={limit}{token_param}">下一页</a>'

    rows_html = "".join(rows) if rows else "<tr><td colspan=\"9\">暂无项目</td></tr>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>MAC System Admin</title>
  <meta http-equiv="refresh" content="30">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 20px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px; text-align: left; vertical-align: top; }}
    th {{ background: #f5f5f5; }}
    tr:hover {{ background: #f9f9f9; }}
    form {{ margin: 2px 0; }}
    button {{ cursor: pointer; }}
    .nav {{ margin-top: 12px; }}
  </style>
</head>
<body>
  <h1>MAC System 项目面板</h1>
  <p>共 {total} 个项目，当前第 {offset + 1} - {min(offset + limit, total)} 个。</p>
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>名称</th>
        <th>类型</th>
        <th>状态</th>
        <th>当前阶段</th>
        <th>创建时间</th>
        <th>更新时间</th>
        <th>成本</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <div class="nav">{prev_link} {next_link}</div>
</body>
</html>"""


@router.get("/projects", response_class=HTMLResponse)
def admin_projects(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    service: ProjectService = Depends(get_project_service),
    token: str | None = Depends(_admin_token_dependency),
) -> HTMLResponse:
    projects, total = service.list_projects(offset=offset, limit=limit)
    base_url = str(request.base_url).rstrip("/")
    html_content = _render_projects_page(projects, total, offset, limit, token, base_url)
    return HTMLResponse(content=html_content)


@router.post("/projects/{project_id}/cancel")
async def admin_cancel_project(
    project_id: str,
    request: Request,
    service: ProjectService = Depends(get_project_service),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    token: str | None = Depends(_admin_token_dependency),
) -> RedirectResponse:
    try:
        await orchestrator.cancel_project(project_id, reason="通过管理面板终止")
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return RedirectResponse(
        url=_list_url(request, token), status_code=303
    )


@router.post("/projects/{project_id}/tick")
async def admin_tick_project(
    project_id: str,
    request: Request,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    token: str | None = Depends(_admin_token_dependency),
) -> RedirectResponse:
    try:
        await orchestrator.tick(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return RedirectResponse(
        url=_list_url(request, token), status_code=303
    )


@router.post("/projects/{project_id}/rollback")
async def admin_rollback_project(
    project_id: str,
    request: Request,
    service: ProjectService = Depends(get_project_service),
    token: str | None = Depends(_admin_token_dependency),
) -> RedirectResponse:
    body = await request.body()
    params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
    target_state = params.get("target_state", [""])[0].strip()
    if not target_state:
        raise HTTPException(status_code=400, detail="TARGET_STATE_REQUIRED")
    project = service.require_project(project_id)
    state_machine = StateMachine(project.task_type)
    if target_state not in state_machine.states:
        raise HTTPException(status_code=400, detail="TARGET_STATE_UNREACHABLE")
    project.current_state = target_state
    project.updated_at = datetime.now(timezone.utc)
    service.session.commit()
    return RedirectResponse(
        url=_list_url(request, token), status_code=303
    )


def _list_url(request: Request, token: str | None) -> str:
    url = str(request.base_url).rstrip("/") + "/admin/projects"
    if token:
        url += f"?token={token}"
    return url
