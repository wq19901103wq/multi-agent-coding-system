"""One-off host operation: switch the MAC project Project Lead back to Codex.

User-requested (2026-10-03, “切回codex”) reverse of this morning's automatic
Codex -> Kimi failover (handoff_20261003_062434, reason: initialize timed out
after 60s). Mirrors scripts/switch_back_to_codex.py and the durable structures
used by RootSessionStore.prepare_provider_handoff:

* archives the current pending Kimi handoff into event_logs;
* records a new provider_handoff with to_provider="codex", status="pending",
  so the next turn replays ``public_context`` before the user's message and
  ``_publish_handoff`` is deduped by the notification enqueued here;
* sets provider="codex" and clears native_thread_id so the next turn starts a
  FRESH Codex thread (resuming the failed Kimi uuid would fail and could
  re-trigger confusion; the old Codex thread 01a0e9fb-... is long archived).

Read-only verification first; exits without writing when state is unexpected.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime

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

        # Reuse the public-instructions JSON captured by the Kimi takeover
        # handoff so the reverse handoff carries identical, non-drifted history.
        old_public = str(old_handoff.get("public_context") or "")
        match = re.search(r"本项目最近公开指令及报告：\n(\[.*\])", old_public, re.S)
        if not match:
            raise SystemExit("could not extract public history JSON; aborting")
        history_json = match.group(1)
        json.loads(history_json)  # validate

        pending_brief = (
            "上一轮任务待办（Kimi 接管会话本轮已完成排查与实现，尚未上线）：\n"
            "- 现象：10-02 修复上线后，Owner 每轮审阅仍全部走 decide，推进时没有给用户下一步选项。\n"
            "- 根因：单 outcome 结构下“续推已授权工作”(decide/continue) 与“给下一步选项”互斥；"
            "且 prompt 允许把具体建议写进 decide=false 的文本，绕过选项卡。\n"
            "- 已完成修复（工作树未提交，服务未重启，未生效）：decide 结果可携带 user_options；"
            "续推同轮会另外发布“本轮推进中的下一步选择”选项卡；prompt 收紧建议必须选项化；"
            "新增 2 个回归测试。\n"
            "- 已验证：tests/test_project_owner.py + tests/test_root_agent.py 共 156 项通过。"
            "全量 pytest、editable reinstall、服务重启和上线后核对尚未执行；\n"
            "- 附带发现：10-03 出现过一次 steer 空文本异常（invalid type: null, expected a string），"
            "与本修复无关，尚未处理。\n"
            "- 下一步：跑全量 T1–T3 → pip install -e . + 重启服务（会打断在跑的 digital-human turn，"
            "需择时机）→ T4 核对后通过飞书回执确认生效。"
        )
        public_context = (
            "MAC 执行助手切换：应用户要求（“切回codex”），Project Lead 已从 Kimi 切回 Codex。"
            "本轮 Kimi 会话结束后即停止，不会与 Codex 并发写入。\n"
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
            "reason": "用户要求切换回 Codex（自动接管会话正常结束后的切回）",
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
            "“下一步选项修复上线”任务上下文。\n"
            "- 今早切回后 Codex 曾因初始化 60 秒超时自动切到 Kimi；若再次出现，请先留意机器负载"
            "（digital-human 批量任务），超时本轮会原样报错，不会自动切回。\n"
            "- 旧接管记录已归档备查；全部历史记录和工作成果保留。",
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
