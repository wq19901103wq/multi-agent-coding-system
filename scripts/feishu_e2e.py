#!/usr/bin/env python3
"""Automated Feishu-adapter E2E for the native Codex root architecture.

This script uses MAC's test injection endpoints and therefore is T4 automation,
not a T5 real-UI black-box test. T5 must still be performed in the signed-in
Feishu client/browser by clicking and typing like a user.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx


@dataclass
class Context:
    client: httpx.AsyncClient
    user: str
    chat: str


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _assert_file(project_id: str, relative_path: str, expected: str) -> None:
    path = REPOSITORY_ROOT / "projects" / project_id / relative_path
    if not path.is_file():
        raise AssertionError(f"expected artifact is missing: {path}")
    actual = path.read_text(encoding="utf-8")
    if actual != expected:
        raise AssertionError(
            f"unexpected artifact content: {path}; "
            f"expected={expected!r}, actual={actual!r}"
        )


def _headers() -> dict[str, str]:
    token = os.environ.get("CONTROL_API_TOKEN") or os.environ.get("ADMIN_TOKEN")
    return {"X-Control-API-Token": token} if token else {}


async def _json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} returned non-object JSON")
    return value


async def _wait(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 900,
    label: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await fetch()
        if predicate(latest):
            return latest
        await asyncio.sleep(1)
    raise TimeoutError(f"timed out waiting for {label}; latest={latest}")


async def _inject_text(ctx: Context, text: str) -> tuple[str, str]:
    event_id = f"evt-{uuid.uuid4().hex}"
    message_id = f"msg-{uuid.uuid4().hex}"
    await _json(
        ctx.client,
        "POST",
        "/test/inject_text_message",
        json={
            "open_id": ctx.user,
            "chat_id": ctx.chat,
            "chat_type": "p2p",
            "message_id": message_id,
            "event_id": event_id,
            "text": text,
        },
    )
    return event_id, message_id


async def _project_for_message(ctx: Context, message_id: str) -> str:
    event = await _wait(
        lambda: _json(ctx.client, "GET", f"/test/feishu/events/{message_id}"),
        lambda value: bool(value.get("found") and value.get("project_id")),
        timeout=60,
        label="Feishu event project binding",
    )
    return str(event["project_id"])


async def _inject_card_action(
    ctx: Context,
    *,
    approval_id: str,
    action: str,
    interaction_id: str,
    question_id: str | None = None,
    option_key: str | None = None,
    message_id: str | None = None,
) -> None:
    extras = {"interaction_id": interaction_id}
    if question_id:
        extras["question_id"] = question_id
    await _json(
        ctx.client,
        "POST",
        "/test/inject_card_action",
        json={
            "open_id": ctx.user,
            "approval_id": approval_id,
            "action": action,
            "option_key": option_key,
            "chat_id": ctx.chat,
            "chat_type": "p2p",
            "message_id": message_id,
            "event_id": f"evt-{uuid.uuid4().hex}",
            "extras": extras,
        },
    )


async def _wait_root_boundary(
    client: httpx.AsyncClient,
    project_id: str,
    *,
    previous_thread: str | None = None,
    after_cursor: int | None = None,
) -> dict[str, Any]:
    def ready(value: dict[str, Any]) -> bool:
        thread_id = value.get("native_thread_id")
        if value.get("status") == "failed":
            return True
        return bool(
            thread_id
            and (previous_thread is None or thread_id == previous_thread)
            and (
                after_cursor is None
                or int(value.get("event_cursor") or 0) > after_cursor
            )
            and value.get("status") in {"idle", "waiting_user"}
        )

    return await _wait(
        lambda: _json(client, "GET", f"/test/projects/{project_id}/runtime"),
        ready,
        label=f"root boundary for {project_id}",
    )


async def scenario_native_project_flow(ctx: Context) -> None:
    _, first_message = await _inject_text(
        ctx,
        "/create 在当前目录创建 hello.txt，文件内容必须恰好是单独一行 hello（末尾保留换行），完成后读取文件核验。",
    )
    project_id = await _project_for_message(ctx, first_message)
    first = await _wait_root_boundary(ctx.client, project_id)
    if first.get("status") == "failed":
        raise RuntimeError(f"first native turn failed: {first}")
    _assert_file(project_id, "hello.txt", "hello\n")

    await _inject_text(
        ctx,
        "继续迭代：把 hello.txt 改成恰好两行 hello 和 world（末尾保留换行），完成后读取文件核验。",
    )
    second = await _wait_root_boundary(
        ctx.client,
        project_id,
        previous_thread=str(first["native_thread_id"]),
        after_cursor=int(first.get("event_cursor") or 0),
    )
    if second.get("status") == "failed":
        raise RuntimeError(f"follow-up native turn failed: {second}")
    _assert_file(project_id, "hello.txt", "hello\nworld\n")
    print(
        f"PASS native_project_flow project={project_id} "
        f"thread={first['native_thread_id']}"
    )


async def scenario_project_thread_isolation(ctx: Context) -> None:
    _, left_message = await _inject_text(
        ctx,
        "/create 创建 left.txt，内容必须恰好是单独一行 left（末尾保留换行），完成后读取核验。",
    )
    left_id = await _project_for_message(ctx, left_message)
    left = await _wait_root_boundary(ctx.client, left_id)
    if left.get("status") == "failed":
        raise RuntimeError(f"left native turn failed: {left}")
    _assert_file(left_id, "left.txt", "left\n")
    _, right_message = await _inject_text(
        ctx,
        "/create 创建 right.txt，内容必须恰好是单独一行 right（末尾保留换行），完成后读取核验。",
    )
    right_id = await _project_for_message(ctx, right_message)
    right = await _wait_root_boundary(ctx.client, right_id)
    if right.get("status") == "failed":
        raise RuntimeError(f"right native turn failed: {right}")
    _assert_file(right_id, "right.txt", "right\n")
    if left_id == right_id or left["native_thread_id"] == right["native_thread_id"]:
        raise AssertionError("independent projects shared a project id or Codex thread")
    print(
        "PASS project_thread_isolation "
        f"projects=({left_id},{right_id}) "
        f"threads=({left['native_thread_id']},{right['native_thread_id']})"
    )


async def scenario_request_user_input(ctx: Context) -> None:
    _, message_id = await _inject_text(
        ctx,
        "/create 在写 choice.txt 之前，必须调用 request_user_input 询问一个选择题："
        "问题 id 必须是 color，选项必须是 blue 和 green。用户选择后，把所选英文单词写入 choice.txt，"
        "文件只能包含该单词和末尾换行，并读取核验。",
    )
    project_id = await _project_for_message(ctx, message_id)

    status = await _wait(
        lambda: _json(ctx.client, "GET", f"/projects/{project_id}/status"),
        lambda value: bool(
            value.get("execution_status") == "waiting_user"
            and ((value.get("pending_approval") or {}).get("payload") or {}).get(
                "question_batch_message_id"
            )
        ),
        timeout=120,
        label="native request_user_input",
    )
    runtime = await _json(
        ctx.client, "GET", f"/test/projects/{project_id}/runtime"
    )
    if runtime.get("status") != "waiting_user":
        raise AssertionError(f"root state is not synchronized: {runtime}")

    approval = status["pending_approval"]
    payload = approval.get("payload") or {}
    interaction_id = str(payload["question_interaction_id"])
    question_message_id = str(payload.get("question_batch_message_id") or "") or None
    await _inject_card_action(
        ctx,
        approval_id=str(approval["approval_id"]),
        action="select_question_option",
        interaction_id=interaction_id,
        question_id="color",
        option_key="green",
        message_id=question_message_id,
    )
    await _wait(
        lambda: _json(ctx.client, "GET", f"/projects/{project_id}/status"),
        lambda value: (
            ((value.get("pending_approval") or {}).get("payload") or {})
            .get("selected_options", {})
            .get("color")
            == "green"
        ),
        timeout=60,
        label="stored question selection",
    )
    await _inject_card_action(
        ctx,
        approval_id=str(approval["approval_id"]),
        action="submit_selected_options",
        interaction_id=interaction_id,
        message_id=question_message_id,
    )
    completed = await _wait_root_boundary(
        ctx.client,
        project_id,
        previous_thread=str(runtime["native_thread_id"]),
        after_cursor=int(runtime.get("event_cursor") or 0),
    )
    if completed.get("status") == "failed":
        raise RuntimeError(f"request_user_input turn failed: {completed}")
    if completed.get("status") == "waiting_user":
        raise AssertionError(
            f"Codex requested input again after a submitted answer: {completed}"
        )
    _assert_file(project_id, "choice.txt", "green\n")

    cards = await _json(
        ctx.client, "GET", f"/test/projects/{project_id}/cards"
    )
    question_cards = [
        item
        for item in cards.get("items", [])
        if item.get("projection_key") == f"question:{interaction_id}"
    ]
    if not question_cards or question_cards[-1].get("lifecycle_status") != "resolved_by_user":
        raise AssertionError(f"question card state was not resolved: {question_cards}")
    print(
        f"PASS request_user_input project={project_id} "
        f"thread={runtime['native_thread_id']}"
    )


SCENARIOS = {
    "native_project_flow": scenario_native_project_flow,
    "project_thread_isolation": scenario_project_thread_isolation,
    "request_user_input": scenario_request_user_input,
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    args = parser.parse_args()
    base_url = os.environ.get("MCS_BASE_URL", "http://127.0.0.1:18080")
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=_headers(),
        timeout=30,
    ) as client:
        await _json(client, "GET", "/health")
        user = os.environ.get("FEISHU_SIMULATOR_OPEN_ID")
        chat = os.environ.get("FEISHU_SIMULATOR_CHAT_ID")
        if not user or not chat:
            raise RuntimeError(
                "FEISHU_SIMULATOR_OPEN_ID and FEISHU_SIMULATOR_CHAT_ID are required"
            )
        ctx = Context(client, user, chat)
        names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
        for name in names:
            await SCENARIOS[name](ctx)


if __name__ == "__main__":
    asyncio.run(main())
