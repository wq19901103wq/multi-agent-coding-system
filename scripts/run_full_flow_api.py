#!/usr/bin/env .venv/bin/python
"""Full-flow end-to-end test via the MAC API.

This drives a project through the entire pipeline using the test endpoints,
so the Feishu cards are still sent for real but we do not need a second
"simulator" bot.  Confirmation button clicks are injected via
/test/inject_card_action.
"""
import asyncio
import json
import sys
import time

import httpx

API_BASE = "http://127.0.0.1:8000"

# Use a real group chat_id from the environment if available, otherwise a
# placeholder (the card will be addressed to the group even though we cannot
# see it without Feishu access).
CHAT_ID = "oc_aca93ea61c7494e26e3a03c91437fc0d"
OPEN_ID = "ou_test_fullflow_001"


async def create_project(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        f"{API_BASE}/projects",
        json={
            "name": "full-flow-render-test",
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
    print(f"Created project {data['id']} at {data['current_state']}")
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
    expected_state: str | None = None,
    timeout: float = 600.0,
) -> dict:
    """Wait for a pending approval, tick the orchestrator when needed."""
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        pa = status.get("pending_approval")
        cs = status["current_state"]
        if expected_state:
            print(f"  {cs} pending={'YES' if pa else 'no'}          \r", end="", flush=True)
        if pa and (expected_state is None or cs == expected_state):
            print()
            return status
        # If the project has moved past the expected state (e.g. auto-resolved or
        # we polled too slowly), return the current status so the caller can adapt.
        if expected_state and cs != expected_state and _state_after(cs, expected_state):
            print()
            return status
        # If we reached a human confirmation/input state but no approval yet,
        # tick once more so the orchestrator creates the approval and sends the card.
        if cs != last_state and (cs.endswith("_PENDING") or cs.endswith("_INPUT")):
            last_state = cs
            await tick(project_id, http)
            continue
        last_state = cs
        await asyncio.sleep(3.0)
    raise TimeoutError(f"No pending approval after {timeout}s")


def _state_after(current: str, expected: str) -> bool:
    """Return True if current is a later stage than expected in the feature pipeline."""
    order = [
        "PLANNING_REQUIREMENTS",
        "REQUIREMENT_CONFIRM_PENDING",
        "PLANNING_ARCHITECTURE",
        "ARCHITECTURE_CONFIRM_PENDING",
        "PLANNING_SPECS",
        "SPEC_CONFIRM_PENDING",
        "CODING",
        "TESTING",
        "REVIEW",
        "ACCEPTANCE_PENDING",
        "MERGING",
        "COMPLETED",
    ]
    try:
        return order.index(current) > order.index(expected)
    except ValueError:
        return False


async def inject_action(
    project_id: str,
    approval_id: str,
    action: str,
    state: str,
    http: httpx.AsyncClient,
) -> None:
    print(f"  Injecting {action} for {approval_id} (state={state})...", flush=True)
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
    )
    resp.raise_for_status()
    print(f"  Injected {action} for {approval_id}", flush=True)


async def wait_for_state(
    project_id: str,
    state: str,
    http: httpx.AsyncClient,
    timeout: float = 900.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        if status["current_state"] == state:
            return status
        print(f"  waiting for {state}: {status['current_state']}    \r", end="", flush=True)
        await asyncio.sleep(5.0)
    raise TimeoutError(f"Did not reach {state} within {timeout}s")


async def main() -> int:
    async with httpx.AsyncClient(timeout=300.0) as http:
        project_id = await create_project(http)

        # Kick off the first agent execution.
        print("\nKicking off first tick...")
        await tick(project_id, http)

        # Stage 1: confirm requirements
        print("Waiting for REQUIREMENT_CONFIRM_PENDING...")
        status = await wait_for_pending(http=http, project_id=project_id, expected_state="REQUIREMENT_CONFIRM_PENDING")
        await inject_action(project_id, status["pending_approval"]["approval_id"], "confirm", "REQUIREMENT_CONFIRM_PENDING", http)

        # Stage 2: confirm architecture
        print("Waiting for ARCHITECTURE_CONFIRM_PENDING...")
        status = await wait_for_pending(http=http, project_id=project_id, expected_state="ARCHITECTURE_CONFIRM_PENDING")
        await inject_action(project_id, status["pending_approval"]["approval_id"], "confirm", "ARCHITECTURE_CONFIRM_PENDING", http)

        # Stage 3: confirm specs
        print("Waiting for SPEC_CONFIRM_PENDING...")
        status = await wait_for_pending(http=http, project_id=project_id, expected_state="SPEC_CONFIRM_PENDING")
        await inject_action(project_id, status["pending_approval"]["approval_id"], "confirm", "SPEC_CONFIRM_PENDING", http)

        # Stage 4: wait for ACCEPTANCE_PENDING (coding/testing/review run auto)
        print("\nWaiting for ACCEPTANCE_PENDING (coding/testing/review)...")
        status = await wait_for_pending(http=http, project_id=project_id, expected_state="ACCEPTANCE_PENDING", timeout=1800.0)
        print(f"Reached ACCEPTANCE_PENDING")

        # Show a preview of what the acceptance card looks like.
        print("\nAcceptance card preview (first 800 chars):")
        from mac_system.agents.orchestrator import AgentOrchestrator
        from mac_system.agents.prompts import PromptRenderer
        from mac_system.agents.runtime import FakeRuntime
        from mac_system.config import get_settings
        from mac_system.core.project_service import ProjectService
        from mac_system.core.worktree_manager import WorktreeManager
        from mac_system.database import session_scope
        from mac_system.feishu.bridge import NullFeishuBridge
        from mac_system.state_machine import StateMachine

        class CaptureBridge(NullFeishuBridge):
            def __init__(self):
                self.captured = []
            async def send_confirmation_card(self, **kwargs):
                self.captured.append(("card", kwargs))
            async def send_content_card(self, **kwargs):
                self.captured.append(("content", kwargs))
            async def send_text(self, *args, **kwargs):
                self.captured.append(("text", args[1]))

        settings = get_settings()
        with session_scope() as session:
            service = ProjectService(session)
            project = service.require_project(project_id)
            wm = WorktreeManager(settings.projects_dir)
            renderer = PromptRenderer({})
            bridge = CaptureBridge()
            orchestrator = AgentOrchestrator(service, wm, FakeRuntime({}), renderer, feishu_bridge=bridge)
            sm = StateMachine(project.task_type)
            await orchestrator._handle_human_confirmation(project, sm)

        for kind, payload in bridge.captured:
            if kind == "card":
                print(f"Title: {payload['title']}")
                print(f"Summary:\n{payload['summary'][:800]}")
                print(f"Details: {payload['details']}")
                print(f"Options: {[o['label'] for o in payload['options']]}")
            elif kind == "content":
                print(f"\nContent card ({payload.get('page', 1)}/{payload.get('total', 1)}): {payload['title']}")
                print(f"{payload['content'][:600]}...")
            elif kind == "text":
                print(f"\nFollow-up text:\n{payload[:600]}...")

        # Confirm acceptance
        await inject_action(project_id, status["pending_approval"]["approval_id"], "confirm", "ACCEPTANCE_PENDING", http)

        # Wait for completion
        print("\nWaiting for COMPLETED...")
        status = await wait_for_state(project_id, "COMPLETED", http, timeout=120.0)
        print(f"\nProject {project_id} reached COMPLETED!")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        import traceback
        print(f"\nFull-flow test failed: {exc}")
        traceback.print_exc()
        sys.exit(1)
