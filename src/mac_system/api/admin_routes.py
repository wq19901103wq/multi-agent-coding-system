"""Simple admin panel for monitoring and managing projects."""

from __future__ import annotations

import html
import json
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


def _status_badge(status: str) -> str:
    color = {
        "active": "green",
        "cancelled": "red",
        "paused": "orange",
        "completed": "blue",
        "pending": "gray",
        "running": "blue",
        "failed": "red",
        "responded": "green",
        "expired": "gray",
    }.get(status, "gray")
    return f'<span class="badge badge-{color}">{html.escape(status)}</span>'


def _base_html(title: str, body: str, token: str | None = None) -> str:
    token_q = f"?token={html.escape(token)}" if token else ""
    nav = f"""
    <nav>
      <a href="/admin/projects{token_q}">📋 项目列表</a>
      <span class="muted">|</span>
      <span>MAC System Admin</span>
    </nav>
    """
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta http-equiv="refresh" content="60">
  <style>
    :root {{
      --bg: #f6f8fa;
      --card: #ffffff;
      --text: #24292f;
      --muted: #57606a;
      --border: #d0d7de;
      --green: #1f883d;
      --red: #cf222e;
      --orange: #9a6700;
      --blue: #0969da;
      --gray: #6e7781;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px;
      line-height: 1.5;
    }}
    nav {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 20px;
      display: flex;
      gap: 12px;
      align-items: center;
    }}
    nav a {{ text-decoration: none; color: var(--blue); font-weight: 600; }}
    nav a:hover {{ text-decoration: underline; }}
    .muted {{ color: var(--muted); }}
    h1 {{ font-size: 22px; margin: 0 0 16px; }}
    h2 {{ font-size: 18px; margin: 24px 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ background: #f6f8fa; font-weight: 600; }}
    tr:hover td {{ background: #f6f8fa; }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
      color: #fff;
      background: var(--gray);
    }}
    .badge-green {{ background: var(--green); }}
    .badge-red {{ background: var(--red); }}
    .badge-orange {{ background: var(--orange); }}
    .badge-blue {{ background: var(--blue); }}
    .badge-gray {{ background: var(--gray); }}
    button {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 10px;
      cursor: pointer;
      font-size: 12px;
    }}
    button:hover {{ background: #f3f4f6; }}
    button.danger {{ color: var(--red); border-color: var(--red); }}
    button.danger:hover {{ background: #fff5f5; }}
    button.primary {{ color: var(--blue); border-color: var(--blue); }}
    select {{ border: 1px solid var(--border); border-radius: 6px; padding: 4px; font-size: 12px; }}
    form {{ display: inline; margin: 0 4px 0 0; }}
    .nav {{ margin-top: 16px; display: flex; gap: 12px; }}
    .nav a {{
      padding: 6px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      text-decoration: none;
      color: var(--text);
      background: var(--card);
    }}
    .nav a:hover {{ background: #f3f4f6; }}
    .timeline {{
      position: relative;
      padding-left: 28px;
    }}
    .timeline::before {{
      content: "";
      position: absolute;
      left: 8px;
      top: 4px;
      bottom: 4px;
      width: 2px;
      background: var(--border);
    }}
    .timeline-item {{
      position: relative;
      margin-bottom: 20px;
    }}
    .timeline-item::before {{
      content: "";
      position: absolute;
      left: -24px;
      top: 4px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--blue);
      border: 2px solid var(--card);
    }}
    .timeline-item.failed::before {{ background: var(--red); }}
    .timeline-item.running::before {{ background: var(--orange); }}
    .timeline-item.completed::before {{ background: var(--green); }}
    .timeline-header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
    }}
    .timeline-title {{ font-weight: 600; font-size: 15px; }}
    .timeline-time {{ color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .pre {{
      background: #f6f8fa;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      max-height: 200px;
      overflow-y: auto;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
    }}
    .stat {{ font-size: 13px; }}
    .stat strong {{ display: block; font-size: 20px; margin-bottom: 4px; }}
  </style>
</head>
<body>
  {nav}
  {body}
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
    token_param = f"&token={html.escape(token)}" if token else ""
    rows = []
    for p in projects:
        sm = StateMachine(p.task_type)
        detail_url = f"/admin/projects/{html.escape(p.id)}?{token_param.lstrip('&')}"
        rows.append(
            f"""
            <tr>
              <td class="mono"><a href="{detail_url}">{html.escape(p.id)}</a></td>
              <td>{html.escape(p.name or "-")}</td>
              <td>{html.escape(p.task_type)}</td>
              <td>{_status_badge(p.status)}</td>
              <td>{html.escape(sm.display_name(p.current_state))}<br><span class="mono muted">{html.escape(p.current_state)}</span></td>
              <td>{_fmt_dt(p.created_at)}</td>
              <td>{_fmt_dt(p.updated_at)}</td>
              <td>${p.total_cost_usd or 0:.4f}</td>
              <td>
                <form method="post" action="/admin/projects/{html.escape(p.id)}/cancel?{token_param.lstrip('&')}" onsubmit="return confirm('确定终止项目？')">
                  <button type="submit" class="danger">终止</button>
                </form>
                <form method="post" action="/admin/projects/{html.escape(p.id)}/tick?{token_param.lstrip('&')}">
                  <button type="submit" class="primary">tick</button>
                </form>
                <a href="{detail_url}"><button>详情</button></a>
              </td>
            </tr>
            """
        )

    prev_link = ""
    next_link = ""
    if offset > 0:
        prev_offset = max(0, offset - limit)
        prev_link = f'<a href="/admin/projects?offset={prev_offset}&limit={limit}{token_param}">上一页</a>'
    if offset + limit < total:
        next_offset = offset + limit
        next_link = f'<a href="/admin/projects?offset={next_offset}&limit={limit}{token_param}">下一页</a>'

    rows_html = "".join(rows) if rows else '<tr><td colspan="9" class="muted">暂无项目</td></tr>'

    body = f"""
    <h1>项目列表</h1>
    <div class="card">
      <p class="muted">共 {total} 个项目，当前第 {offset + 1} - {min(offset + limit, total)} 个。</p>
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
      <div class="nav">{prev_link}{next_link}</div>
    </div>
    """
    return HTMLResponse(content=_base_html("MAC System Admin", body, token))


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def admin_project_detail(
    project_id: str,
    request: Request,
    service: ProjectService = Depends(get_project_service),
    token: str | None = Depends(_admin_token_dependency),
) -> HTMLResponse:
    project = service.require_project(project_id)
    sm = StateMachine(project.task_type)
    token_param = f"&token={html.escape(token)}" if token else ""

    tasks = service.get_project_tasks(project_id)
    approvals = service.get_project_approvals(project_id)
    events = service.get_project_events(project_id)

    approval_by_task = {a.task_id: a for a in approvals if a.task_id}
    state_transitions = [e for e in events if "State transition" in e.message]

    # Header stats
    header = f"""
    <h1>{html.escape(project.name or "未命名项目")}</h1>
    <div class="card">
      <div class="grid">
        <div class="stat"><strong class="mono">{html.escape(project.id)}</strong>项目 ID</div>
        <div class="stat"><strong>{html.escape(project.task_type)}</strong>任务类型</div>
        <div class="stat"><strong>{_status_badge(project.status)}</strong>项目状态</div>
        <div class="stat"><strong>{html.escape(sm.display_name(project.current_state))}</strong>当前阶段</div>
        <div class="stat"><strong>${project.total_cost_usd or 0:.4f}</strong>总成本</div>
        <div class="stat"><strong>{_fmt_dt(project.created_at)}</strong>创建时间</div>
      </div>
      <p style="margin-top:12px"><span class="muted">需求描述：</span>{html.escape(project.description or "-")}</p>
      <div style="margin-top:12px">
        <form method="post" action="/admin/projects/{html.escape(project_id)}/cancel?{token_param.lstrip('&')}" onsubmit="return confirm('确定终止项目？')" style="display:inline">
          <button type="submit" class="danger">终止项目</button>
        </form>
        <form method="post" action="/admin/projects/{html.escape(project_id)}/tick?{token_param.lstrip('&')}" style="display:inline">
          <button type="submit" class="primary">tick</button>
        </form>
      </div>
    </div>
    """

    # Timeline
    timeline_items = []
    for task in tasks:
        task_state_display = sm.display_name(task.state)
        cls = task.status
        output_summary = ""
        if task.output_data:
            summary = task.output_data.get("summary", "")
            next_event = task.output_data.get("next_state_event", "")
            if summary:
                output_summary += f"<p><span class=\"muted\">产出摘要：</span>{html.escape(str(summary))}</p>"
            if next_event:
                output_summary += f"<p><span class=\"muted\">流转事件：</span>{html.escape(next_event)}</p>"
            if not output_summary:
                output_summary = f'<div class="pre">{html.escape(_truncate_json(task.output_data))}</div>'
        elif task.error_message:
            output_summary = f'<p class="badge badge-red">失败：{html.escape(task.error_message)}</p>'

        input_summary = ""
        desc = task.input_data.get("description", "")
        rejection = task.input_data.get("rejection_note", "")
        if desc:
            input_summary += f"<p><span class=\"muted\">描述：</span>{html.escape(str(desc))}</p>"
        if rejection:
            input_summary += f'<p><span class="muted">反馈：</span><span style="color:var(--red)">{html.escape(str(rejection))}</span></p>'

        approval_html = ""
        approval = approval_by_task.get(task.id)
        if approval:
            options_html = ""
            for opt in approval.payload.get("options", []):
                label = opt.get("label", opt.get("action", ""))
                chosen = approval.response == opt.get("action")
                mark = " ✅" if chosen else ""
                options_html += f"<li>{html.escape(label)}{mark}</li>"
            approval_html = f"""
            <div style="margin-top:10px;padding:10px;background:#f6f8fa;border-radius:6px">
              <div><span class="muted">审批：</span>{html.escape(approval.question or "-")} {_status_badge(approval.status)}</div>
              {f'<ul style="margin:6px 0 0 18px">{options_html}</ul>' if options_html else ''}
              {f'<div class="muted" style="margin-top:6px">回复人：{html.escape(approval.responded_by or "-")}</div>' if approval.responded_by else ''}
            </div>
            """

        task_events = [e for e in events if task.created_at <= e.created_at]
        if task != tasks[-1]:
            next_task = tasks[tasks.index(task) + 1]
            task_events = [e for e in task_events if e.created_at < next_task.created_at]
        events_html = ""
        if task_events:
            events_html = '<ul class="muted" style="margin:8px 0 0 18px;font-size:12px">'
            for e in task_events[:5]:
                events_html += f"<li>[{html.escape(e.level)}] {_fmt_dt(e.created_at)} {html.escape(e.message)}</li>"
            events_html += "</ul>"

        timeline_items.append(f"""
        <div class="timeline-item {cls}">
          <div class="timeline-header">
            <div>
              <span class="timeline-title">{html.escape(task_state_display)}</span>
              <span class="mono muted">{html.escape(task.state)}</span>
              {_status_badge(task.status)}
              <span class="mono muted">{html.escape(task.agent_type)}</span>
            </div>
            <div class="timeline-time">{_fmt_dt(task.created_at)}</div>
          </div>
          <div style="margin-top:8px">
            <p class="mono muted">任务：{html.escape(task.id)}</p>
            {input_summary}
            {output_summary}
            {approval_html}
            {events_html}
          </div>
        </div>
        """)

    timeline_html = (
        '<div class="timeline">' + "".join(timeline_items) + "</div>"
        if timeline_items
        else '<p class="muted">暂无任务记录</p>'
    )

    transitions_html = ""
    if state_transitions:
        transitions_html = '<table style="margin-top:8px"><thead><tr><th>时间</th><th>流转</th></tr></thead><tbody>'
        for e in state_transitions:
            meta = e.meta or {}
            from_state = meta.get("from_state", "")
            to_state = meta.get("to_state", "")
            transitions_html += f"""
            <tr>
              <td class="mono">{_fmt_dt(e.created_at)}</td>
              <td>{html.escape(from_state)} → {html.escape(to_state)}</td>
            </tr>
            """
        transitions_html += "</tbody></table>"
    else:
        transitions_html = '<p class="muted">暂无状态流转记录</p>'

    events_html = ""
    if events:
        events_html = '<table style="margin-top:8px"><thead><tr><th>时间</th><th>级别</th><th>消息</th></tr></thead><tbody>'
        for e in events[:50]:
            events_html += f"""
            <tr>
              <td class="mono">{_fmt_dt(e.created_at)}</td>
              <td>{_status_badge(e.level)}</td>
              <td>{html.escape(e.message)}</td>
            </tr>
            """
        events_html += "</tbody></table>"
    else:
        events_html = '<p class="muted">暂无事件</p>'

    body = header + f"""
    <div class="card">
      <h2>执行与审批时间线</h2>
      {timeline_html}
    </div>
    <div class="card">
      <h2>状态流转记录</h2>
      {transitions_html}
    </div>
    <div class="card">
      <h2>最近事件</h2>
      {events_html}
    </div>
    """
    return HTMLResponse(content=_base_html(f"项目 {project.name or project.id}", body, token))


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
    return RedirectResponse(url=_detail_or_list_url(request, project_id, token), status_code=303)


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
    return RedirectResponse(url=_detail_or_list_url(request, project_id, token), status_code=303)


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
    return RedirectResponse(url=_detail_or_list_url(request, project_id, token), status_code=303)


def _detail_or_list_url(request: Request, project_id: str, token: str | None) -> str:
    """Redirect back to the detail page if possible, otherwise the list page."""
    referer = request.headers.get("referer", "")
    if referer and "/admin/projects/" in referer:
        url = referer
    else:
        url = str(request.base_url).rstrip("/") + f"/admin/projects/{project_id}"
    if token:
        sep = "&" if "?" in url else "?"
        url += f"{sep}token={token}"
    return url


def _truncate_json(data: Any, max_len: int = 400) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > max_len:
        text = text[:max_len] + "\n...（已截断）"
    return text
