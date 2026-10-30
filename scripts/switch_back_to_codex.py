"""One-off host operation: switch this project's Project Lead back to Codex.

User-requested reverse of the Codex -> Kimi failover. Mirrors the durable
structures used by RootSessionStore.prepare_provider_handoff:

* archives the previous (failed) handoff into event_logs, exactly like the
  store's "return to Codex" branch;
* records a new provider_handoff with to_provider="codex", status="pending",
  so the next turn replays ``public_context`` before the user's message and
  ``_publish_handoff`` is deduped by the notification we enqueue here (the
  stock card text only describes the Kimi direction);
* sets provider="codex" and clears native_thread_id so the next turn starts a
  FRESH Codex thread via create_thread (resuming the old thread
  01a0e9fb-... is known to fail; resuming an unknown uuid would fail too and
  could re-trigger the automatic Kimi failover).

Read-only verification first; exits without writing when state is unexpected.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from uuid import uuid4

from sqlmodel import Session, select

from mac_system.core.project_service import generate_id
from mac_system.feishu.card import build_content_card
from mac_system.models import EventLog, NotificationOutbox, Project, RootAgentSession

PROJECT_ID = "proj_20260915_061732_6da6c03873c50a18"
DB_PATH = "sqlite:///mac_system.db"


def _now() -> datetime:
    return datetime.utcnow()


def main() -> None:
    from mac_system.database import _engine

    with Session(_engine) as session:
        row = session.exec(
            select(RootAgentSession).where(RootAgentSession.project_id == PROJECT_ID)
        ).first()
        if row is None:
            raise SystemExit("root session row missing; aborting")
        if row.provider != "kimi_code":
            raise SystemExit(f"unexpected provider {row.provider!r}; aborting")
        old_handoff = dict(row.provider_handoff or {})
        if not old_handoff:
            raise SystemExit("no previous handoff found; aborting")
        project = session.get(Project, PROJECT_ID)
        if project is None:
            raise SystemExit("project row missing; aborting")
        if not project.feishu_chat_id:
            raise SystemExit("project has no feishu_chat_id; aborting")
        if project.status == "cancelled":
            raise SystemExit("project is cancelled; aborting")

        # Reuse the public-instructions JSON captured by the previous handoff
        # so the reverse handoff carries identical, non-drifted history.
        old_public = str(old_handoff.get("public_context") or "")
        match = re.search(r"本项目最近公开指令及报告：\n(\[.*\])", old_public, re.S)
        if not match:
            raise SystemExit("could not extract public history JSON; aborting")
        history_json = match.group(1)
        json.loads(history_json)  # validate

        pending_brief = (
            "上一轮任务待办（用户原话“修复下 这些应该都可以自动审批”，本轮只做了排查，未改代码）：\n"
            "- 现象：9 月 27 日以来 128 次命令审批由 Owner 自动处理；"
            "1 次文件修改（新增 /private/tmp/dh-source-manifest.patch）因审批卡缺少文件路径、"
            "修改内容和用途，Owner 无法确认属于当前任务而转人工。\n"
            "- 根因方向：src/mac_system/core/root_agent.py 的审批转换逻辑未把 fileChange 前面"
            "文件变更事件的具体信息关联进审批请求；permissions 审批也未展示权限明细。\n"
            "- 修复方向：补齐操作对象、范围和用途，让 Owner 依据已有授权自动判断；信息不足时先补信息。"
        )
        public_context = (
            "MAC 执行助手切换：应用户要求（“切换回codex吧”），Project Lead 已从 Kimi 切回 Codex。"
            "Kimi 会话完成本轮后即停止，不会与 Codex 并发写入。\n"
            "你在新的 Codex 会话中接续同一项目。先只读核实当前工作区、已完成工作和公开状态；"
            "不得重复已完成的外部或不可逆操作。不确定某项操作是否完成时先核查，无法核查就提问。"
            "用户原话优先于旧报告和 Owner 转述。\n"
            f"工作区：{project.workspace_path}\n"
            "注意：工作树有大量未提交改动（既有现状，非待办任务）。\n"
            "本项目最近公开指令及报告：\n"
            + history_json
            + "\n"
            + pending_brief
            + "\n当前请求：\n继续完成上一轮请求。"
        )

        new_handoff = {
            "id": generate_id("handoff"),
            "from_provider": row.provider,
            "from_thread_id": row.native_thread_id,
            "to_provider": "codex",
            "to_thread_id": None,  # fresh thread is created on the next turn
            "reason": "用户要求切换回 Codex（Kimi 故障接管结束后的人工切回）",
            "public_context": public_context,
            "status": "pending",
        }

        session.add(EventLog(
            project_id=PROJECT_ID,
            level="INFO",
            message="Previous provider handoff archived after return to Codex",
            meta={"provider_handoff": old_handoff},
        ))
        session.add(EventLog(
            project_id=PROJECT_ID,
            level="INFO",
            message="Provider switched back to codex on explicit user request",
            meta={
                "from_provider": row.provider,
                "to_provider": "codex",
                "handoff_id": new_handoff["id"],
                "kimi_thread_id": row.native_thread_id,
                "old_codex_thread_id": old_handoff.get("from_thread_id"),
            },
        ))

        row.provider_handoff = new_handoff
        row.provider = "codex"
        row.native_thread_id = None
        row.updated_at = _now()
        session.add(row)

        context = dict(project.context or {})
        context["project_lead_provider"] = "codex"
        project.context = context
        project.updated_at = _now()
        session.add(project)

        dedupe_key = f"provider-handoff:{new_handoff['id']}"
        card = build_content_card(
            f"[{project.name or project.id}] 已切换回 Codex",
            "应你的要求，本项目执行助手已从 Kimi 切回 Codex。\n"
            "- Kimi 会话本轮结束后停止，新旧执行不会并发。\n"
            "- 你的下一条消息将开启**新的 Codex 线程**，并自动附带项目公开进度和未完成的"
            "“审批自动修复”任务上下文。\n"
            "- 原 Codex 线程因早前 resume 失败不再复用；全部历史记录和工作成果保留。"
            "旧接管记录已归档备查。\n"
            "- digital-human 项目当前仍是 Kimi 会话（空闲）；如需一并切回，直接说一声。",
            color="green",
        )
        session.add(NotificationOutbox(
            id=generate_id("outbox"),
            bot_account_id=project.bot_account_id,
            project_id=PROJECT_ID,
            conversation_id=project.feishu_chat_id,
            recipient_id=project.feishu_chat_id,
            recipient_type="chat_id",
            intent_type="report",
            dedupe_key=dedupe_key,
            payload={
                "card": card,
                "update_message_id": None,
                "projection_key": dedupe_key,
                "subject_type": "root_agent",
                "subject_id": PROJECT_ID,
                "lifecycle_status": "published",
                "selected_action": None,
                "view_state": None,
                "resolved_by": None,
                "resulting_state": "running",
            },
        ))
        session.commit()
        print(json.dumps({
            "handoff_id": new_handoff["id"],
            "provider": "codex",
            "native_thread_id": None,
            "dedupe_key": dedupe_key,
        }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
