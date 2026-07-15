#!/usr/bin/env .venv/bin/python
"""Full-flow test with a rejection at the requirement confirmation stage."""
import asyncio
import os
import sys
import time

import httpx

API_BASE = f"http://127.0.0.1:{os.getenv('MCS_PORT', '18080')}"
CHAT_ID = "oc_aca93ea61c7494e26e3a03c91437fc0d"
OPEN_ID = "ou_test_rejection_001"


async def create_project(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        f"{API_BASE}/projects",
        json={
            "name": "rejection-flow-test",
            "task_type": "feature",
            "description": "实现一个简单的 todo list CLI：支持添加、列出、删除任务，数据存 JSON 文件",
            "approval_mode": "normal",
            "creator_open_id": OPEN_ID,
            "feishu_chat_id": CHAT_ID,
            "feishu_chat_type": "group",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"Created project {data['id']}")
    return data["id"]


async def tick(project_id: str, http: httpx.AsyncClient) -> dict:
    resp = await http.post(f"{API_BASE}/test/tick/{project_id}", timeout=600.0)
    resp.raise_for_status()
    return resp.json()


async def get_status(project_id: str, http: httpx.AsyncClient) -> dict:
    resp = await http.get(f"{API_BASE}/projects/{project_id}/status")
    resp.raise_for_status()
    return resp.json()


async def wait_for_pending(
    project_id: str,
    http: httpx.AsyncClient,
    expected_state: str,
    timeout: float = 600.0,
) -> dict:
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        pa = status.get("pending_approval")
        cs = status["current_state"]
        if pa and cs == expected_state:
            return status
        # If we reached a human confirmation/input state but no approval yet,
        # tick once more so the orchestrator creates the approval and card.
        if cs != last_state and (cs.endswith("_PENDING") or cs.endswith("_INPUT")):
            last_state = cs
            await tick(project_id, http)
            continue
        last_state = cs
        await asyncio.sleep(3.0)
    raise TimeoutError(f"No pending approval for {expected_state}")


async def wait_for_state(
    project_id: str,
    state: str,
    http: httpx.AsyncClient,
    timeout: float = 600.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        if status["current_state"] == state:
            return status
        await asyncio.sleep(3.0)
    raise TimeoutError(f"Did not reach {state}")


async def inject_action(
    project_id: str,
    approval_id: str,
    action: str,
    state: str,
    http: httpx.AsyncClient,
) -> None:
    print(f"Injecting {action} for {approval_id}")
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
    print(f"Sending text feedback: {text}")
    resp = await http.post(
        f"{API_BASE}/test/inject_text_message",
        json={
            "open_id": OPEN_ID,
            "text": text,
            "chat_id": CHAT_ID,
            "chat_type": "group",
        },
        timeout=600.0,
    )
    resp.raise_for_status()


async def main() -> int:
    control_token = os.getenv("CONTROL_API_TOKEN") or os.getenv("ADMIN_TOKEN")
    headers = {"Authorization": f"Bearer {control_token}"} if control_token else {}
    async with httpx.AsyncClient(timeout=300.0, headers=headers) as http:
        project_id = await create_project(http)
        await tick(project_id, http)

        # Stage 1: reject requirements
        print("Waiting for REQUIREMENT_CONFIRM_PENDING...")
        status = await wait_for_pending(project_id, http, "REQUIREMENT_CONFIRM_PENDING")
        await inject_action(
            project_id,
            status["pending_approval"]["approval_id"],
            "reject",
            "REQUIREMENT_CONFIRM_PENDING",
            http,
        )

        # Wait for the project to land back in PLANNING_REQUIREMENTS.
        print("Waiting for PLANNING_REQUIREMENTS after rejection...")
        await wait_for_state(project_id, "PLANNING_REQUIREMENTS", http, timeout=120.0)

        # Send feedback text.
        await send_text(project_id, "请增加任务优先级和截止日期字段", http)

        # Stage 2: confirm revised requirements
        print("Waiting for revised REQUIREMENT_CONFIRM_PENDING...")
        status = await wait_for_pending(project_id, http, "REQUIREMENT_CONFIRM_PENDING")
        await inject_action(
            project_id,
            status["pending_approval"]["approval_id"],
            "confirm",
            "REQUIREMENT_CONFIRM_PENDING",
            http,
        )

        # Stage 3: confirm architecture
        print("Waiting for ARCHITECTURE_CONFIRM_PENDING...")
        status = await wait_for_pending(project_id, http, "ARCHITECTURE_CONFIRM_PENDING")
        await inject_action(
            project_id,
            status["pending_approval"]["approval_id"],
            "confirm",
            "ARCHITECTURE_CONFIRM_PENDING",
            http,
        )

        # Stage 4: confirm specs
        print("Waiting for SPEC_CONFIRM_PENDING...")
        status = await wait_for_pending(project_id, http, "IMPLEMENTATION_SPEC_CONFIRM_PENDING")
        await inject_action(
            project_id,
            status["pending_approval"]["approval_id"],
            "confirm",
            "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
            http,
        )

        # Stage 5: wait for acceptance
        print("Waiting for ACCEPTANCE_PENDING...")
        status = await wait_for_pending(project_id, http, "ACCEPTANCE_PENDING", timeout=1800.0)
        await inject_action(
            project_id,
            status["pending_approval"]["approval_id"],
            "confirm",
            "ACCEPTANCE_PENDING",
            http,
        )

        # Wait for completion
        print("Waiting for COMPLETED...")
        await wait_for_state(project_id, "COMPLETED", http, timeout=300.0)
        print(f"\nProject {project_id} reached COMPLETED!")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        import traceback
        print(f"\nRejection flow test failed: {exc}")
        traceback.print_exc()
        sys.exit(1)
