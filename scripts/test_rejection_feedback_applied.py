#!/usr/bin/env .venv/bin/python
"""Verify that rejection feedback is actually reflected in the revised requirements."""
import asyncio
import json
import os
import sys
import time

import httpx

API_BASE = f"http://127.0.0.1:{os.getenv('MCS_PORT', '18080')}"
CHAT_ID = "oc_aca93ea61c7494e26e3a03c91437fc0d"
OPEN_ID = "ou_test_rejection_feedback_001"


async def create_project(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        f"{API_BASE}/projects",
        json={
            "name": "rejection-feedback-applied-test",
            "task_type": "feature",
            "description": "实现一个简单的 todo list CLI：支持添加、列出、删除任务，数据存 JSON 文件",
            "approval_mode": "normal",
            "creator_open_id": OPEN_ID,
            "feishu_chat_id": CHAT_ID,
            "feishu_chat_type": "group",
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def tick(project_id: str, http: httpx.AsyncClient) -> dict:
    resp = await http.post(f"{API_BASE}/test/tick/{project_id}", timeout=600.0)
    resp.raise_for_status()
    return resp.json()


async def get_status(project_id: str, http: httpx.AsyncClient) -> dict:
    resp = await http.get(f"{API_BASE}/projects/{project_id}/status")
    resp.raise_for_status()
    return resp.json()


async def wait_for_pending(project_id: str, http: httpx.AsyncClient, expected_state: str, timeout: float = 600.0) -> dict:
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        pa = status.get("pending_approval")
        cs = status["current_state"]
        if pa and cs == expected_state:
            return status
        if cs != last_state and (cs.endswith("_PENDING") or cs.endswith("_INPUT")):
            last_state = cs
            await tick(project_id, http)
            continue
        last_state = cs
        await asyncio.sleep(3.0)
    raise TimeoutError(f"No pending approval for {expected_state}")


async def inject_action(project_id: str, approval_id: str, action: str, state: str, http: httpx.AsyncClient) -> None:
    resp = await http.post(
        f"{API_BASE}/test/inject_card_action",
        json={
            "open_id": OPEN_ID,
            "approval_id": approval_id,
            "action": action,
            "state": state,
            "chat_id": CHAT_ID,
            "chat_type": "group",
        },
        timeout=600.0,
    )
    resp.raise_for_status()


async def send_text(project_id: str, text: str, http: httpx.AsyncClient) -> None:
    resp = await http.post(
        f"{API_BASE}/test/inject_text_message",
        json={"open_id": OPEN_ID, "text": text, "chat_id": CHAT_ID, "chat_type": "group"},
        timeout=600.0,
    )
    resp.raise_for_status()


async def main() -> int:
    feedback = "必须支持任务优先级和截止日期字段"
    async with httpx.AsyncClient(timeout=300.0) as http:
        project_id = await create_project(http)
        print(f"Created {project_id}")
        await tick(project_id, http)

        status = await wait_for_pending(project_id, http, "REQUIREMENT_CONFIRM_PENDING")
        print("First requirements ready; rejecting...")
        await inject_action(project_id, status["pending_approval"]["approval_id"], "reject", "REQUIREMENT_CONFIRM_PENDING", http)

        # wait to land back in PLANNING_REQUIREMENTS
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            status = await get_status(project_id, http)
            if status["current_state"] == "PLANNING_REQUIREMENTS":
                break
            await asyncio.sleep(2.0)

        print(f"Sending feedback: {feedback}")
        await send_text(project_id, feedback, http)

        status = await wait_for_pending(project_id, http, "REQUIREMENT_CONFIRM_PENDING", timeout=600.0)
        print("Revised requirements ready; checking content...")

        # Read requirements from main workspace
        import os
        workspace = f"/Users/yihanwang/multi-agent-coding-system/.worktrees/feat-core/projects/{project_id}"
        req_path = f"{workspace}/.mac/REQUIREMENTS.json"
        reqs = json.load(open(req_path, encoding="utf-8"))
        combined = json.dumps(reqs, ensure_ascii=False)
        print("\n--- Revised REQUIREMENTS.json (first 1000 chars) ---")
        print(combined[:1000])

        keywords = ["优先级", "截止日期", "优先级", "deadline", "priority"]
        found = any(k in combined for k in keywords)
        print(f"\nFeedback keywords found in requirements: {found}")
        return 0 if found else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        import traceback
        print(f"Test failed: {exc}")
        traceback.print_exc()
        sys.exit(1)
