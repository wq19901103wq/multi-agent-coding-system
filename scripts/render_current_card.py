#!/usr/bin/env .venv/bin/python
"""Render the confirmation card that would be sent for a project."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mac_system.config import get_settings
from mac_system.core.project_service import ProjectService
from mac_system.database import session_scope
from mac_system.feishu.bridge import NullFeishuBridge
from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.prompts import PromptRenderer
from mac_system.agents.runtime import FakeRuntime
from mac_system.core.worktree_manager import WorktreeManager
from mac_system.state_machine import StateMachine


class CaptureBridge(NullFeishuBridge):
    def __init__(self):
        self.captured = []

    async def send_confirmation_card(self, **kwargs):
        self.captured.append(("confirmation_card", kwargs))
        return "msg_capture"

    async def send_content_card(self, **kwargs):
        self.captured.append(("content_card", kwargs))
        return "msg_capture"

    async def send_input_card(self, **kwargs):
        self.captured.append(("input_card", kwargs))
        return "msg_capture"

    async def send_text(self, *args, **kwargs):
        self.captured.append(("text", args, kwargs))
        return "msg_capture"

    async def _send_interactive(self, *args, **kwargs):
        self.captured.append(("interactive", args, kwargs))
        return "msg_capture"


async def main(project_id: str):
    settings = get_settings()
    with session_scope() as session:
        service = ProjectService(session)
        project = service.require_project(project_id)
        sm = StateMachine(project.task_type)

        wm = WorktreeManager(settings.projects_dir)
        renderer = PromptRenderer({})
        runtime = FakeRuntime({})
        bridge = CaptureBridge()
        orchestrator = AgentOrchestrator(service, wm, runtime, renderer, feishu_bridge=bridge)

        # Trigger the human confirmation handler directly.
        await orchestrator._handle_human_confirmation(project, sm)

    for item in bridge.captured:
        kind = item[0]
        if kind == "confirmation_card":
            kwargs = item[1]
            print("=" * 60)
            print(f"TITLE: {kwargs['title']}")
            print(f"SUMMARY:\n{kwargs['summary']}\n")
            print("DETAILS:")
            for k, v in kwargs['details'].items():
                print(f"  {k}: {v}")
            print(f"OPTIONS: {kwargs['options']}")
            print("\nCARD JSON:")
            from mac_system.feishu.card import build_confirmation_card
            card = build_confirmation_card(
                title=kwargs['title'],
                summary=kwargs['summary'],
                details=kwargs['details'],
                options=kwargs['options'],
                approval_id=kwargs['approval_id'],
                state=kwargs.get('state', ''),
                chat_id=kwargs.get('chat_id'),
                chat_type=kwargs.get('chat_type', 'p2p'),
                show_actions=kwargs.get('show_actions', True),
            )
            print(json.dumps(card, ensure_ascii=False, indent=2))
        elif kind == "content_card":
            kwargs = item[1]
            print("-" * 60)
            print(f"CONTENT CARD ({kwargs.get('page', 1)}/{kwargs.get('total', 1)}): {kwargs['title']}")
            print(f"CONTENT:\n{kwargs['content']}")
            print("\nCARD JSON:")
            from mac_system.feishu.card import build_content_card
            card = build_content_card(
                title=kwargs['title'],
                content=kwargs['content'],
                page=kwargs.get('page', 1),
                total=kwargs.get('total', 1),
                color=kwargs.get('color', 'blue'),
            )
            print(json.dumps(card, ensure_ascii=False, indent=2))
        elif kind == "text":
            print("-" * 60)
            print(f"FOLLOW-UP TEXT:\n{item[1][1]}")
        elif kind == "input_card":
            kwargs = item[1]
            print("=" * 60)
            print(f"INPUT CARD: {kwargs['title']}")
            print(f"QUESTION: {kwargs['question']}")
            print(f"CONTEXT:\n{kwargs['context']}")


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "proj_20260710_145558_7919"
    asyncio.run(main(pid))
