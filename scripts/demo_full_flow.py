#!/usr/bin/env python3
"""Single clean end-to-end demo: one project through the full pipeline."""
import asyncio, json, os, re, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
from mac_system.config import get_settings
from mac_system.feishu.bridge import FeishuBridge

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
API_BASE = f"http://127.0.0.1:{os.getenv('MCS_PORT', '18080')}"
ID_RE = re.compile(r"\b(proj_[a-zA-Z0-9_]+)\b")


async def main():
    s = get_settings()
    sim = FeishuBridge(os.environ["FEISHU_SIMULATOR_APP_ID"], os.environ["FEISHU_SIMULATOR_APP_SECRET"])
    mac = FeishuBridge(s.feishu_app_id, s.feishu_app_secret)
    http = httpx.AsyncClient(timeout=600.0)
    chat_id = os.environ["FEISHU_SIMULATOR_CHAT_ID"]
    open_id = os.environ.get("FEISHU_SIMULATOR_OPEN_ID", "ou_simulator")

    try:
        # ── Phase 1: Create project via Feishu message ──
        print("=" * 60)
        print("Phase 1: Sending Feishu message to create project...")
        mac_open_id = await mac.get_bot_open_id()
        text = f'<at user_id="{mac_open_id}">@Code</at> 实现一个简单的文件搜索 CLI 工具'
        content = {"text": text}
        r = await sim._request("POST", "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_data={"receive_id": chat_id, "msg_type": "text", "content": json.dumps(content, ensure_ascii=False)})
        msg_id = r["data"]["message_id"]
        print(f"  Sent: {msg_id}")
        print("  Waiting for MAC bot to create project and send requirements card...")

        # Poll for a new project.
        project_id = None
        for _ in range(120):
            await asyncio.sleep(2)
            resp = await http.get(f"{API_BASE}/projects/{project_id}/status") if project_id else None
            # Find the latest project via API listing? No list endpoint. Poll DB instead.
            # Use the tick endpoint to discover.
            from mac_system.database import init_db, session_scope
            from mac_system.models import Project
            from sqlmodel import select
            init_db()
            with session_scope() as db:
                ps = db.exec(select(Project).order_by(Project.created_at.desc()).limit(1)).all()
                if ps and ps[0].current_state != "PLANNING_REQUIREMENTS":
                    project_id = ps[0].id
                    print(f"  Found project: {project_id} state={ps[0].current_state}")
                    break
        if not project_id:
            print("FAIL: No project found after 240s")
            return 1

        # ── Phase 2: Auto-click through all confirmation stages ──
        stages = [
            ("REQUIREMENT_CONFIRM_PENDING", "confirm"),
            ("ARCHITECTURE_CONFIRM_PENDING", "confirm"),
            ("IMPLEMENTATION_SPEC_CONFIRM_PENDING", "confirm"),
        ]
        for target_state, action in stages:
            print(f"\nPhase: Waiting for {target_state}...")
            # Poll for the target state.
            for _ in range(300):
                await asyncio.sleep(2)
                resp = await http.get(f"{API_BASE}/projects/{project_id}/status")
                if resp.status_code != 200:
                    continue
                status = resp.json()
                cs = status["current_state"]
                pa = status.get("pending_approval")
                print(f"  state={cs} pending_approval={'YES' if pa else 'no'}", end="\r")
                if pa:
                    approval_id = pa["approval_id"]
                    pa_type = pa.get("type", "")
                    print(f"\n  Found pending approval {approval_id} (type={pa_type})")
                    print(f"  Injecting {action}...")
                    await http.post(f"{API_BASE}/test/inject_card_action", json={
                        "open_id": open_id,
                        "approval_id": approval_id,
                        "action": action,
                        "option_key": None,
                        "state": target_state,
                        "chat_id": chat_id,
                        "chat_type": "group",
                    })
                    break
            else:
                print(f"\n  WARNING: Did not reach {target_state}")

        # ── Phase 3: Wait for ACCEPTANCE_PENDING ──
        print(f"\nPhase: Waiting for ACCEPTANCE_PENDING (auto through coding/testing/review)...")
        for _ in range(600):
            await asyncio.sleep(3)
            resp = await http.get(f"{API_BASE}/projects/{project_id}/status")
            if resp.status_code != 200:
                continue
            status = resp.json()
            cs = status["current_state"]
            pa = status.get("pending_approval")
            print(f"  state={cs} pending={'YES' if pa else 'no'}   ", end="\r")
            if cs == "ACCEPTANCE_PENDING" and pa:
                print(f"\n  Reached ACCEPTANCE_PENDING!")
                break
        else:
            print(f"\n  WARNING: Did not reach ACCEPTANCE_PENDING within timeout")

        # ── Phase 4: Also demo a reject flow ──
        print(f"\nPhase: Testing rejection flow...")
        text2 = f'<at user_id="{mac_open_id}">@Code</at> 修复登录页面偶尔报 500 的问题'
        content2 = {"text": text2}
        r2 = await sim._request("POST", "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_data={"receive_id": chat_id, "msg_type": "text", "content": json.dumps(content2, ensure_ascii=False)})
        print(f"  Sent bug report: {r2['data']['message_id']}")
        print("  Waiting for bug analysis card...")

        # Find the reject project.
        reject_pid = None
        for _ in range(120):
            await asyncio.sleep(2)
            from mac_system.database import init_db, session_scope
            from mac_system.models import Project
            from sqlmodel import select
            init_db()
            with session_scope() as db:
                ps = db.exec(select(Project).order_by(Project.created_at.desc()).limit(3)).all()
                for p in ps:
                    if "500" in (p.description or ""):
                        reject_pid = p.id
                        print(f"  Found reject project: {reject_pid} state={p.current_state}")
                        break
            if reject_pid:
                break

        if reject_pid:
            # Wait for BUG_FIX_CONFIRM_PENDING and reject it.
            for _ in range(300):
                await asyncio.sleep(2)
                resp = await http.get(f"{API_BASE}/projects/{reject_pid}/status")
                if resp.status_code != 200:
                    continue
                status = resp.json()
                cs = status["current_state"]
                pa = status.get("pending_approval")
                print(f"  state={cs} pending={'YES' if pa else 'no'}   ", end="\r")
                if pa:
                    approval_id = pa["approval_id"]
                    print(f"\n  Rejecting {approval_id}...")
                    await http.post(f"{API_BASE}/test/inject_card_action", json={
                        "open_id": open_id,
                        "approval_id": approval_id,
                        "action": "reject",
                        "option_key": None,
                        "state": cs,
                        "chat_id": chat_id,
                        "chat_type": "group",
                    })
                    # Wait a moment then send feedback.
                    await asyncio.sleep(3)
                    feedback_text = "修改意见：500 错误是因为 NPE，需要加空值检查，不是改登录接口本身"
                    content3 = {"text": feedback_text}
                    await sim._request("POST", "/open-apis/im/v1/messages",
                        params={"receive_id_type": "chat_id"},
                        json_data={"receive_id": chat_id, "msg_type": "text", "content": json.dumps(content3, ensure_ascii=False)})
                    print(f"  Sent feedback: {feedback_text}")
                    # Wait for re-planning.
                    await asyncio.sleep(10)
                    resp = await http.get(f"{API_BASE}/projects/{reject_pid}/status")
                    if resp.status_code == 200:
                        print(f"  After reject+feedback, state={resp.json()['current_state']}")
                    break

        print("\n" + "=" * 60)
        print("Demo complete!")
        print(f"  Full-flow project: {project_id}")
        if reject_pid:
            print(f"  Reject-flow project: {reject_pid}")
        return 0

    finally:
        await sim.close()
        await mac.close()
        await http.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
