#!/usr/bin/env .venv/bin/python
"""Full-flow end-to-end test via the MAC API.

This drives a project through the entire pipeline using the test endpoints,
so the Feishu cards are still sent for real but we do not need a second
"simulator" bot.  Confirmation button clicks are injected via
/test/inject_card_action.
"""
import argparse
import asyncio
import json
import sys
import time

import os

import requests

API_BASE = f"http://127.0.0.1:{os.getenv('MCS_PORT', '18080')}"


class _Response:
    def __init__(self, response: requests.Response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> dict:
        return self._response.json()

    def raise_for_status(self) -> None:
        self._response.raise_for_status()


class RequestsAsyncClient:
    """Async-compatible wrapper around requests to avoid httpx absolute-URI issues."""

    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout
        self._session = requests.Session()

    async def __aenter__(self) -> "RequestsAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        self._session.close()

    async def post(self, url: str, **kwargs: object) -> _Response:
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout
        return _Response(await asyncio.to_thread(self._session.post, url, **kwargs))

    async def get(self, url: str, **kwargs: object) -> _Response:
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout
        return _Response(await asyncio.to_thread(self._session.get, url, **kwargs))

# Use a real group chat_id from the environment if available, otherwise a
# placeholder (the card will be addressed to the group even though we cannot
# see it without Feishu access).
CHAT_ID = "oc_aca93ea61c7494e26e3a03c91437fc0d"
OPEN_ID = "ou_test_fullflow_001"


# Default task configuration; can be overridden via CLI args.
TASK_TYPE = "feature"
PROJECT_NAME = "full-flow-render-test"
PROJECT_DESCRIPTION = "实现一个简单的 todo list CLI：支持添加、列出、删除任务，数据存 JSON 文件"


async def create_project(http: RequestsAsyncClient) -> str:
    resp = await http.post(
        f"{API_BASE}/projects",
        json={
            "name": PROJECT_NAME,
            "task_type": TASK_TYPE,
            "description": PROJECT_DESCRIPTION,
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


async def tick(project_id: str, http: RequestsAsyncClient) -> dict:
    resp = await http.post(f"{API_BASE}/test/tick/{project_id}", timeout=600.0)
    resp.raise_for_status()
    return resp.json()


async def get_status(project_id: str, http: RequestsAsyncClient) -> dict:
    resp = await http.get(f"{API_BASE}/projects/{project_id}/status")
    resp.raise_for_status()
    return resp.json()


async def wait_for_pending(
    project_id: str,
    http: RequestsAsyncClient,
    expected_state: str | None = None,
    timeout: float = 600.0,
) -> dict:
    """Wait for a pending approval, tick once if we land on a confirmation/input state without a card."""
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    while time.monotonic() < deadline:
        status = await get_status(project_id, http)
        pa = status.get("pending_approval")
        cs = status["current_state"]
        print(f"  {cs} pending={'YES' if pa else 'no'}          \r", end="", flush=True)
        if pa and (expected_state is None or cs == expected_state):
            print()
            return status
        # If the project has moved past the expected state (e.g. auto-resolved or
        # we polled too slowly), return the current status so the caller can adapt.
        if expected_state and cs != expected_state and _state_after(cs, expected_state):
            print()
            return status
        # When the orchestrator lands on a human confirmation/input state it needs
        # one more tick to create the approval/card.  Only tick if no approval is
        # present yet (a background task may already have created it).
        if (
            cs != last_state
            and not pa
            and (cs.endswith("_PENDING") or cs.endswith("_INPUT"))
        ):
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
        "PLANNING_IMPLEMENTATION_SPECS",
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
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
    http: RequestsAsyncClient,
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
        timeout=1800.0,
    )
    resp.raise_for_status()
    print(f"  Injected {action} for {approval_id}", flush=True)


async def wait_for_state(
    project_id: str,
    state: str,
    http: RequestsAsyncClient,
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAC full-flow live E2E via API")
    parser.add_argument("--task-type", default=TASK_TYPE, help="Task type (feature, bug_fix, documentation)")
    parser.add_argument("--name", default=PROJECT_NAME, help="Project name")
    parser.add_argument("--description", default=PROJECT_DESCRIPTION, help="Task description")
    return parser.parse_args()


async def main() -> int:
    global TASK_TYPE, PROJECT_NAME, PROJECT_DESCRIPTION
    args = _parse_args()
    TASK_TYPE = args.task_type
    PROJECT_NAME = args.name
    PROJECT_DESCRIPTION = args.description

    async with RequestsAsyncClient(timeout=300.0) as http:
        project_id = await create_project(http)

        # Kick off the first agent execution.
        print("\nKicking off first tick...")
        await tick(project_id, http)

        async def _wait_for_state_change(from_state: str, from_approval_id: str | None = None) -> dict:
            """Poll until the injected action has been applied (state or pending changed)."""
            while True:
                s = await get_status(project_id, http)
                pa = s.get("pending_approval")
                if s["current_state"] != from_state:
                    return s
                if from_approval_id is not None and (pa is None or pa["approval_id"] != from_approval_id):
                    return s
                await asyncio.sleep(2.0)

        # Confirm every intermediate confirmation state until acceptance.
        print("\nConfirming intermediate approvals until acceptance...")
        while True:
            status = await wait_for_pending(http=http, project_id=project_id, timeout=1800.0)
            state = status["current_state"]
            pa = status.get("pending_approval")
            if state == "ACCEPTANCE_PENDING":
                print("Reached ACCEPTANCE_PENDING")
                break
            approval_id = pa["approval_id"]
            print(f"  Confirming intermediate state {state}...")
            await inject_action(
                project_id,
                approval_id,
                "confirm",
                state,
                http,
            )
            status = await _wait_for_state_change(state, approval_id)

        # Confirm acceptance.
        approval_id = status["pending_approval"]["approval_id"]
        await inject_action(
            project_id,
            approval_id,
            "confirm",
            "ACCEPTANCE_PENDING",
            http,
        )

        # Wait for completion.
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
