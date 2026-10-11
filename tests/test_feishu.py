import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.config import Settings
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService
from mac_system.feishu.card import (
    build_content_card,
    build_progress_card,
    build_project_decision_card,
    build_question_batch_card,
)
from mac_system.feishu.handlers import (
    FeishuHandlerContext,
    handle_card_action,
    handle_text_message,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.feishu.accounts import FeishuBotAccount
from mac_system.feishu.ws_client import FeishuWebSocketClient, _run_account_process
from mac_system.models import Approval, Project, ProjectDecision, QuestionInteraction


class RecordingBridge:
    def __init__(self):
        self.cards = []
        self.updates = []
        self.deleted = []
        self.reactions = []

    async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
        self.cards.append((recipient, receive_id_type, card))
        return f"message-{len(self.cards)}"

    async def update_card(self, message_id, card):
        self.updates.append((message_id, card))

    async def delete_message(self, message_id):
        self.deleted.append(message_id)

    async def add_reaction(self, message_id, emoji_type="OnIt"):
        self.reactions.append((message_id, emoji_type))
        return f"reaction-{len(self.reactions)}"


def test_ws_account_process_receives_restart_recovery_turns(monkeypatch):
    from unittest.mock import Mock

    captured = {}
    parent_connection, child_connection = Mock(), Mock()

    class FakeProcess:
        def __init__(self, *, target, args, name, daemon):
            captured.update(
                target=target,
                args=args,
                name=name,
                daemon=daemon,
            )
            self.pid = 4321

        def start(self):
            captured["started"] = True

        def is_alive(self):
            return False

    class FakeContext:
        Process = FakeProcess

        def Pipe(self):
            return parent_connection, child_connection

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        "mac_system.feishu.ws_client.multiprocessing.get_context",
        lambda _method: FakeContext(),
    )
    account = FeishuBotAccount("bot-a", "app-id", "app-secret")
    recovery_turns = (
        {
            "project_id": "proj-1",
            "trigger": "安全续接",
            "visible_request": "继续",
            "parent_decision_id": None,
        },
    )
    owner_reviews = ("review-1",)

    FeishuWebSocketClient(
        account,
        startup_root_turns=recovery_turns,
        startup_owner_reviews=owner_reviews,
    ).start()

    assert captured["target"] is _run_account_process
    assert captured["args"] == (
        account, recovery_turns, owner_reviews, child_connection
    )
    child_connection.close.assert_called_once()
    assert captured["started"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_account_message_pipe_acknowledges_delivery(monkeypatch, rejected):
    import asyncio
    import multiprocessing
    import threading
    from unittest.mock import AsyncMock, Mock
    from mac_system.feishu import ws_client

    account = FeishuBotAccount("bot-a", "app-id", "app-secret")
    client = FeishuWebSocketClient(account)
    parent, child = multiprocessing.Pipe()
    client._message_connection = parent
    client._process = Mock()
    client._process.is_alive.return_value = True
    delivery = AsyncMock(side_effect=ValueError("rejected") if rejected else None)
    monkeypatch.setattr(ws_client, "_deliver_project_message", delivery)
    server = threading.Thread(
        target=ws_client._serve_project_messages,
        args=(account, child, asyncio.get_running_loop()),
        daemon=True,
    )
    server.start()
    try:
        if rejected:
            with pytest.raises(RuntimeError, match="rejected"):
                await client.submit_project_message("project-a", "保留原结果")
        else:
            await client.submit_project_message("project-a", "保留原结果")
        delivery.assert_awaited_once_with(account, "project-a", "保留原结果")
    finally:
        parent.close()
        await asyncio.to_thread(server.join, 2)
    assert not server.is_alive()


@pytest.mark.asyncio
async def test_account_pipe_schedules_owner_review_on_account_loop(monkeypatch):
    import asyncio
    import multiprocessing
    import threading
    from unittest.mock import AsyncMock, Mock
    from mac_system.feishu import ws_client

    account = FeishuBotAccount("bot-a", "app-id", "app-secret")
    client = FeishuWebSocketClient(account)
    parent, child = multiprocessing.Pipe()
    client._message_connection = parent
    client._process = Mock()
    client._process.is_alive.return_value = True
    schedule = AsyncMock()
    monkeypatch.setattr(ws_client, "_schedule_account_owner_review", schedule)
    server = threading.Thread(
        target=ws_client._serve_project_messages,
        args=(account, child, asyncio.get_running_loop()),
        daemon=True,
    )
    server.start()
    try:
        await client.submit_owner_review("project-a", "review-1")
        schedule.assert_awaited_once_with(account, "project-a", "review-1")
    finally:
        parent.close()
        await asyncio.to_thread(server.join, 2)
    assert not server.is_alive()


def test_account_message_timeout_does_not_replay_and_ignores_late_ack():
    from unittest.mock import Mock

    client = FeishuWebSocketClient(FeishuBotAccount("bot-a", "id", "secret"))
    client._process = Mock()
    client._process.is_alive.return_value = True
    connection = Mock()
    client._message_connection = connection
    connection.poll.return_value = False
    with pytest.raises(TimeoutError):
        client._submit_project_message("project-a", "first")
    first_id = connection.send.call_args.args[0][0]
    connection.send.assert_called_once()
    connection.poll.return_value = True
    replies = iter([False, True])
    def receive():
        if not next(replies):
            return first_id, True
        return connection.send.call_args.args[0][0], True
    connection.recv.side_effect = receive
    client._submit_project_message("project-a", "second")
    assert connection.send.call_count == 2
    assert connection.recv.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "waiting_user", "idle"])
async def test_account_delivery_preserves_native_steer(ctx, monkeypatch, status):
    from contextlib import asynccontextmanager
    from mac_system.feishu import ws_client

    project = await ctx.service.create_project("site", "feature", "initial")
    project.bot_account_id = "bot-a"
    ctx.service.session.add(project)
    ctx.service.session.commit()
    ctx.service.set_execution_status(project.id, status)
    @asynccontextmanager
    async def context(*args):
        yield ctx
    monkeypatch.setattr(ws_client, "_WsHandlerContext", context)
    await ws_client._deliver_project_message(
        FeishuBotAccount("bot-a", "id", "secret"), project.id, "核对结果"
    )
    if status == "idle":
        assert ctx.orchestrator.scheduled == [(project.id, "核对结果")]
        assert ctx.orchestrator.steered == []
    else:
        assert ctx.orchestrator.steered == [(project.id, "核对结果")]
        assert ctx.orchestrator.scheduled == []
    with pytest.raises(ValueError, match="unavailable"):
        await ws_client._deliver_project_message(
            FeishuBotAccount("bot-b", "id", "secret"), project.id, "wrong account"
        )
    assert len(ctx.orchestrator.scheduled) + len(ctx.orchestrator.steered) == 1


class Orchestrator:
    def __init__(self, service):
        self.project_service = service
        self.scheduled = []
        self.steered = []
        self.cancelled = []
        self.stopped = []
        self.new_threads = []
        self.visible_requests = []
        self.actor_open_ids = []
        self.request_origins = []
        self.parent_decision_ids = []
        self.reply_contexts = []
        self.steer_visible_requests = []
        self.steer_reply_contexts = []
        self.steer_result = True
        self.timeline_rotations = []
        self.models_set = []
        self.providers_set = []

    def schedule_root_agent(
        self,
        project_id,
        text,
        *,
        actor_open_id=None,
        visible_request=None,
        request_origin="human",
        parent_decision_id=None,
        reply_context=None,
    ):
        self.scheduled.append((project_id, text))
        self.actor_open_ids.append(actor_open_id)
        self.visible_requests.append(visible_request)
        self.request_origins.append(request_origin)
        self.parent_decision_ids.append(parent_decision_id)
        self.reply_contexts.append(reply_context)

    async def steer_root_agent(
        self,
        project_id,
        text,
        *,
        actor_open_id=None,
        visible_request=None,
        reply_context=None,
    ):
        self.steered.append((project_id, text))
        self.actor_open_ids.append(actor_open_id)
        self.steer_visible_requests.append(visible_request)
        self.steer_reply_contexts.append(reply_context)
        return self.steer_result

    async def rotate_root_progress_for_timeline_event(self, project_id):
        self.timeline_rotations.append(project_id)
        return True

    async def cancel_project(self, project_id, reason=""):
        self.cancelled.append((project_id, reason))
        await self.project_service.cancel_project(project_id, reason)

    async def stop_root_turn(self, project_id):
        self.stopped.append(project_id)
        return True

    async def start_new_root_thread(self, project_id):
        self.new_threads.append(project_id)
        return "old-thread", "new-thread"

    async def list_root_models(self, project_id):
        return [{"model": "gpt-6.1-sol", "displayName": "Sol", "description": "Catalog description"},
                {"model": "gpt-6-astra", "displayName": "Astra"}]

    async def set_root_model(self, project_id, model):
        self.models_set.append((project_id, model))
        return None, model

    async def set_root_provider(self, project_id, provider):
        self.providers_set.append((project_id, provider))
        if provider == "claude_code":
            raise RuntimeError("该助手尚未接入项目主会话，不能安全切换。")
        return "codex", provider

    def provider_catalog(self):
        return [
            {
                "provider": "kimi_code",
                "status": "available",
                "model": "kimi-code/k3-256k",
                "metrics": {
                    "sample_count": 2,
                    "completed_count": 2,
                    "average_duration_seconds": 12.5,
                },
            },
            {
                "provider": "zcode",
                "status": "disabled",
                "metrics": {"sample_count": 0},
            },
        ]


def _card_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _card_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _card_nodes(child)


def test_request_user_input_card_uses_one_valid_v2_component_model():
    card = build_question_batch_card(
        "approval-1",
        "interaction-1",
        [{
            "question_id": "color-choice",
            "question": "颜色？",
            "allow_free_text": True,
            "options": [
                {"key": "blue", "label": "blue"},
                {"key": "green", "label": "green"},
            ],
        }],
        "Codex",
        "conversation",
        project_name="一个很长很长而且不适合作为卡片标题的项目描述",
    )

    nodes = list(_card_nodes(card))
    assert card["schema"] == "2.0"
    assert "elements" not in card
    assert all(node.get("tag") != "action" for node in nodes)
    buttons = [node for node in nodes if node.get("tag") == "button"]
    assert buttons
    assert all(button.get("behaviors") for button in buttons)
    assert card["header"]["title"]["content"] == "需要确认 1 个问题"
    rendered = str(card)
    assert "conversation" not in rendered
    assert "root_agent" not in rendered
    assert "项目：一个很长很长而且不适合作为卡片标… · 执行：Codex" in rendered


def test_native_approval_owner_review_does_not_claim_user_confirmation():
    card = build_question_batch_card(
        "approval-owner-review",
        "interaction-owner-review",
        [{
            "question_id": "approval_decision",
            "question": "是否允许执行只读检查？",
            "reason": "需要查看系统进程",
            "options": [
                {"key": "允许本次", "label": "允许本次"},
                {"key": "拒绝并停止", "label": "拒绝并停止"},
            ],
        }],
        "Codex",
        "native_approval",
        project_name="测试项目",
        owner_reviewing=True,
    )

    rendered = str(card)
    buttons = [node for node in _card_nodes(card) if node.get("tag") == "button"]
    assert card["header"]["title"]["content"] == "Owner 审阅中"
    assert card["header"]["template"] == "blue"
    assert buttons == []
    assert "通常无需你操作" in rendered
    assert "需要确认" not in rendered


def test_multi_question_free_text_fields_have_unique_names():
    card = build_question_batch_card(
        "approval-multi",
        "interaction-multi",
        [
            {
                "question_id": "scope",
                "question": "范围？",
                "allow_free_text": True,
                "options": [{"key": "A", "label": "A"}],
            },
            {
                "question_id": "style",
                "question": "风格？",
                "allow_free_text": True,
                "options": [{"key": "B", "label": "B"}],
            },
        ],
        "Codex",
        "conversation",
    )

    inputs = [node for node in _card_nodes(card) if node.get("tag") == "input"]
    names = [node["name"] for node in inputs]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert all(name.startswith("inline_free_text_value_") for name in names)


def test_resolved_native_approval_compacts_request_behind_ellipsis():
    card = build_question_batch_card(
        "approval-native",
        "interaction-native",
        [{
            "question_id": "approval_decision",
            "question": "是否允许执行一条很长的测试命令？",
            "reason": "用于验证当前项目",
            "options": [
                {"key": "允许本次", "label": "允许本次"},
                {
                    "key": "本会话允许同类命令",
                    "label": "本会话允许同类命令",
                },
            ],
        }],
        "Codex",
        "native_approval",
        project_name="测试项目",
        submitted_answers={
            "approval_decision": "本会话允许同类命令",
        },
        resolution_note="✅ AI Project Owner 已代你选择，正在继续处理",
    )

    nodes = list(_card_nodes(card))
    assert card["header"]["title"]["content"] == "已处理"
    assert card["header"]["template"] == "green"
    assert not [node for node in nodes if node.get("tag") == "button"]
    panels = [node for node in nodes if node.get("tag") == "collapsible_panel"]
    assert len(panels) == 1
    assert panels[0]["expanded"] is False
    assert panels[0]["header"]["title"]["content"] == "···"
    rendered = str(card)
    assert "已选择" in rendered
    assert "本会话允许同类命令" in rendered
    assert "是否允许执行一条很长的测试命令" in rendered


def test_native_approval_keeps_owner_assessment_inside_original_card():
    card = build_question_batch_card(
        "approval-native",
        "interaction-native",
        [{
            "question_id": "approval_decision",
            "question": "是否允许发布到公网？",
            "reason": "会产生外部影响",
            "options": [
                {"key": "允许本次", "label": "允许本次"},
                {"key": "拒绝", "label": "拒绝"},
            ],
        }],
        "Codex",
        "native_approval",
        project_name="测试项目",
        owner_decision={
            "id": "decision-assessment",
            "decision": "需要你决定是否公开发布",
            "rationale": "公开发布需要新的用户授权。",
            "impact": "服务将能被公网访问。",
            "status": "requires_user",
        },
    )

    rendered = str(card)
    assert card["header"]["title"]["content"] == "需要确认 1 个问题"
    assert "Project Owner 评估" in rendered
    assert "需要你决定是否公开发布" in rendered
    assert "公开发布需要新的用户授权" in rendered
    assert "提交答案" in rendered


def test_project_decision_card_defaults_to_decision_only_and_expands_evidence():
    compact = build_project_decision_card(
        "decision-compact",
        project_name="个人 Agent",
        question="是否继续发布？",
        decision="暂不发布",
        rationale="当前没有明确的公开发布授权。",
        impact="不会产生外部变更。",
        reversible=True,
        actor_model="gpt-owner-test",
        actor_usage={"inputTokens": 80, "outputTokens": 20, "totalTokens": 100},
        actor_duration_seconds=12.4,
        chat_id="oc-chat",
    )

    compact_rendered = str(compact)
    assert "暂不发布" in compact_rendered
    compact_panels = [
        node for node in _card_nodes(compact) if node.get("tag") == "collapsible_panel"
    ]
    assert [panel["header"]["title"]["content"] for panel in compact_panels] == [
        "···",
        "Agent 与用量详情",
    ]
    assert [panel["expanded"] for panel in compact_panels] == [False, False]
    assert "gpt-owner-test" in compact_rendered
    assert "Token · Project Owner：100" in compact_rendered
    assert "用时 12 秒" in compact_rendered
    assert "项目：个人 Agent · 执行：Project Owner" not in compact_rendered
    assert compact["config"]["summary"]["content"].startswith("个人 Agent")
    assert "查看依据" not in compact_rendered

    expanded = build_project_decision_card(
        "decision-compact",
        project_name="个人 Agent",
        question="是否继续发布？",
        decision="暂不发布",
        rationale="当前没有明确的公开发布授权。",
        impact="不会产生外部变更。",
        reversible=True,
        actor_model="gpt-owner-test",
        actor_usage={"inputTokens": 80, "outputTokens": 20, "totalTokens": 100},
        actor_duration_seconds=12.4,
        chat_id="oc-chat",
        details_expanded=True,
    )

    expanded_rendered = str(expanded)
    assert "当前没有明确的公开发布授权" in expanded_rendered
    assert "不会产生外部变更" in expanded_rendered
    assert "Project Owner" in expanded_rendered
    expanded_panels = [
        node for node in _card_nodes(expanded) if node.get("tag") == "collapsible_panel"
    ]
    assert [panel["expanded"] for panel in expanded_panels] == [True, False]
    assert "收起依据" not in expanded_rendered


def test_requires_user_card_is_a_choice_not_a_fake_decision():
    card = build_project_decision_card(
        "decision-choice",
        project_name="quant-trading",
        question="下一步优先什么？",
        decision="优先提升可解释性，还是缩短回测时间？",
        rationale="现有偏好不足。",
        impact="只影响下一步顺序。",
        reversible=False,
        options=[{"label": "提升可解释性"}, {"label": "缩短回测时间"}],
        status="requires_user",
    )

    rendered = str(card)
    assert card["header"]["title"]["content"] == "需要你选择"
    assert "**需要你选择**：优先提升可解释性，还是缩短回测时间？" in rendered
    assert "**决定**：" not in rendered
    assert "AI 已替你决定" not in card["config"]["summary"]["content"]


def test_progress_card_keeps_runtime_metadata_in_notation_footer():
    card = build_progress_card(
        "[项目] 项目进展",
        "",
        steps=["✅ 已定位问题", "▶️ 补充回归测试"],
        activities=["… 运行 pytest\n命令：pytest tests/ -q"],
        hidden_steps=["✅ 更早的准备工作"],
        hidden_activities=["✓ 更早的测试活动"],
        details=[
            (
                "Agent 与用量详情",
                ["Codex Root · gpt-test", "… Pi · 后端未报告 · 运行中"],
                False,
            )
        ],
        footer=[
            "执行：Codex · 模型：gpt-test · 已用 12 秒",
            "最近活动：12:34:56（1 秒前）",
        ],
    )

    elements = card["body"]["elements"]
    panels = [node for node in elements if node.get("tag") == "collapsible_panel"]
    footer = [node for node in elements if node.get("text_size") == "notation"]
    assert [panel["header"]["title"]["content"] for panel in panels] == [
        "···",
        "···",
        "Agent 与用量详情",
    ]
    assert [panel["expanded"] for panel in panels] == [False, False, False]
    visible_big_step = next(
        node for node in elements if "已定位问题" in str(node)
    )
    current_step_label = next(
        node for node in elements if "当前步骤详情" in str(node)
    )
    visible_activity = next(
        node for node in elements if "运行 pytest" in str(node)
    )
    assert elements.index(panels[0]) < elements.index(visible_big_step)
    assert elements.index(visible_big_step) < elements.index(current_step_label)
    assert elements.index(current_step_label) < elements.index(panels[1])
    assert elements.index(panels[1]) < elements.index(visible_activity)
    assert footer[-2]["content"].startswith("执行：Codex")
    assert footer[-1]["content"].startswith("最近活动：")
    assert elements.index(footer[-1]) < elements.index(panels[-1])
    assert "执行步骤" in elements[0]["content"]


def test_project_advice_card_reuses_decision_card_disclosure_hierarchy():
    card = build_project_decision_card(
        "review-advice",
        project_name="auto-job",
        question="本轮汇报后的建议",
        decision="明早首轮运行后核对浏览器导航和新增职位数",
        rationale="修复尚未经过允许时段内的真实链路验证。",
        impact="只读核对，不自动发送简历。",
        reversible=False,
        status="advice",
        actor_model="gpt-owner-test",
        actor_usage={"inputTokens": 60, "outputTokens": 20, "totalTokens": 80},
        actor_duration_seconds=9.7,
        chat_id="oc-chat",
    )

    assert card["header"]["title"]["content"] == "AI 建议"
    assert card["header"]["template"] == "turquoise"
    rendered = str(card)
    assert "**建议**" in rendered
    assert "调整决定" not in rendered
    panels = [
        node for node in _card_nodes(card) if node.get("tag") == "collapsible_panel"
    ]
    assert [panel["header"]["title"]["content"] for panel in panels] == [
        "···",
        "Agent 与用量详情",
    ]
    assert [panel["expanded"] for panel in panels] == [False, False]


def test_uncertain_owner_card_renders_direct_choice_buttons():
    options = [
        {"label": "优先稳定性", "description": "先修复已知故障"},
        {"label": "优先视觉效果", "description": "先完善页面体验"},
        {"label": "优先性能", "description": "先降低页面加载时间"},
        {"label": "优先内容", "description": "先补齐对外信息"},
    ]
    card = build_project_decision_card(
        "owner-choice",
        project_name="个人网站",
        question="下一步优先做什么？",
        decision="下一步优先稳定性还是视觉效果？",
        rationale="两项都合理，需要你的偏好。",
        impact="只影响下一步顺序。",
        reversible=False,
        options=options,
        status="requires_user",
        chat_id="oc-chat",
    )

    buttons = [
        node for node in _card_nodes(card) if node.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == [
        "优先稳定性",
        "优先视觉效果",
        "优先性能",
        "优先内容",
    ]
    assert buttons[0]["type"] == "primary_filled"
    assert buttons[0]["behaviors"][0]["value"]["action"] == "select_owner_option"


def test_owner_advice_card_renders_choices_without_exposing_details():
    card = build_project_decision_card(
        "owner-advice",
        project_name="个人网站",
        question="本轮汇报后的建议",
        decision="先做一次真实入口验收",
        rationale="目前只有脚本测试结果。",
        impact="不会改变项目目标。",
        reversible=False,
        options=[
            {"label": "采纳建议", "description": "继续做真实入口验收"},
            {"label": "暂不采纳", "description": "保持当前状态"},
        ],
        status="advice",
        chat_id="oc-chat",
    )

    buttons = [node for node in _card_nodes(card) if node.get("tag") == "button"]
    assert card["header"]["title"]["content"] == "AI 建议"
    assert [button["text"]["content"] for button in buttons] == [
        "采纳建议",
        "暂不采纳",
    ]
    assert "等待你的选择" in str(card)


@pytest.fixture
def ctx(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = ProjectService(session)
        service.settings = Settings(
            PROJECTS_DIR=tmp_path / "projects",
            GIT_USER_NAME="MAC Test",
            GIT_USER_EMAIL="mac-test@example.com",
        )
        service.settings.projects_dir.mkdir()
        bridge = RecordingBridge()
        orchestrator = Orchestrator(service)
        yield FeishuHandlerContext(
            bridge,
            service,
            orchestrator,
            chat_id="oc-chat",
            chat_type="p2p",
        )


def message(text):
    return TextMessage(
        open_id="ou-user",
        message_id="om-1",
        text=text,
        is_command=text.startswith("/"),
        chat_id="oc-chat",
        chat_type="p2p",
    )


def message_in_chat(text, chat_id):
    item = message(text)
    item.chat_id = chat_id
    return item


@pytest.mark.asyncio
async def test_agents_command_reports_actual_provider_state_without_starting_turn(ctx):
    await handle_text_message(ctx, message("/agents"))

    card = ctx.bridge.cards[-1][2]
    rendered = str(card)
    assert "Kimi Code" in rendered
    assert "kimi-code/k3-256k" in rendered
    assert "2/2" in rendered
    assert "Zcode" in rendered
    assert "未启用" in rendered
    assert ctx.orchestrator.scheduled == []


@pytest.mark.asyncio
async def test_agent_command_switches_future_project_lead_without_running_turn(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    ctx.service.set_chat_user_context("oc-chat", "ou-user", project.id)

    await handle_text_message(ctx, message("/agent kimi"))

    assert ctx.orchestrator.providers_set == [(project.id, "kimi_code")]
    assert ctx.orchestrator.scheduled == []
    rendered = str(ctx.bridge.cards[-1][2])
    assert "Codex → Kimi" in rendered
    assert "下一条普通消息" in rendered


@pytest.mark.asyncio
async def test_agent_command_does_not_claim_unsupported_claude_switch(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    ctx.service.set_chat_user_context("oc-chat", "ou-user", project.id)

    await handle_text_message(ctx, message("/agent claude"))

    assert ctx.orchestrator.providers_set == [(project.id, "claude_code")]
    assert ctx.orchestrator.scheduled == []
    assert "不能安全切换" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_first_ordinary_message_creates_one_root_project(ctx):
    await handle_text_message(ctx, message("做一个个人网站"))
    project = ctx.service.session.exec(select(Project)).one()
    assert project.context["orchestration_mode"] == "root_agent"
    assert ctx.orchestrator.scheduled == [(project.id, "做一个个人网站")]
    assert len(ctx.bridge.cards) == 1
    assert ctx.bridge.reactions == [("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_idle_project_receives_message_unchanged(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    await handle_text_message(ctx, message("修复登录后白屏"))
    assert ctx.orchestrator.scheduled == [(project.id, "修复登录后白屏")]
    assert ctx.bridge.cards == []
    assert ctx.bridge.reactions == [("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_new_human_request_supersedes_unfinished_owner_alignment(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    decision = ctx.service.create_project_decision(
        project.id,
        request_item_id="old-alignment",
        native_turn_id=None,
        question="开始执行前需要确认",
        options=[],
        decision="旧请求还希望得到什么结果？",
        rationale="旧请求不清楚。",
        impact="影响旧请求。",
        reversible=False,
        confidence="low",
        answer_payload={"source_request": "旧请求"},
        context_refs=[],
        actor_model="owner-model",
        status="requires_user",
        decision_type="alignment",
    )
    ctx.service.update_project_decision(
        decision.id,
        status="requires_user",
        card_message_id="om-old-alignment",
    )
    ctx.service.set_execution_status(project.id, "waiting_user")
    ctx.orchestrator.steer_result = False

    await handle_text_message(ctx, message("这是新的明确请求"))

    stored = ctx.service.get_project_decision(decision.id)
    assert stored.status == "superseded"
    assert ctx.bridge.deleted == ["om-old-alignment"]
    assert ctx.orchestrator.scheduled == [(project.id, "这是新的明确请求")]


@pytest.mark.asyncio
async def test_each_new_turn_acknowledges_original_message_without_receipt_card(ctx):
    await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    await handle_text_message(ctx, message("第一轮只读问题"))
    await handle_text_message(ctx, message("第二轮只读问题"))
    assert ctx.bridge.cards == []
    assert ctx.bridge.updates == []
    assert ctx.bridge.reactions == [("om-1", "OnIt"), ("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_running_project_uses_native_steer(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    ctx.service.set_execution_status(project.id, "running")
    await handle_text_message(ctx, message("补充：不要改数据库"))
    assert ctx.orchestrator.steered == [(project.id, "补充：不要改数据库")]
    assert ctx.orchestrator.steer_visible_requests == ["补充：不要改数据库"]
    assert ctx.orchestrator.timeline_rotations == [project.id]
    assert ctx.orchestrator.scheduled == []
    assert ctx.bridge.cards == []
    assert ctx.bridge.reactions == [("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_reply_to_card_selects_its_project_and_adds_bounded_context(ctx):
    referenced = await ctx.service.create_project(
        "referenced",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    referenced_round = ctx.service.begin_project_history_round(
        referenced.id,
        user_request="发布网站首页",
    )
    referenced_round.native_turn_id = "turn-referenced"
    referenced_round.report_content = "首页已经发布。"
    ctx.service.session.add(referenced_round)
    ctx.service.session.commit()
    ctx.service.finish_unreported_project_history_round(
        referenced_round.id,
        status="completed",
    )
    card_message_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key="result-for-reply",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("网站已完成", "首页已经发布。"),
        project_id=referenced.id,
        projection_key="root-result:turn-referenced:1",
        subject_type="root_turn",
        subject_id="turn-referenced",
        lifecycle_status="completed",
        resulting_state="idle",
    )
    other = await ctx.service.create_project(
        "other",
        "feature",
        "other",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    assert ctx.service.get_chat_user_active_project("oc-chat", "ou-user").id == other.id

    reply = message("把按钮改成蓝色")
    reply.parent_id = card_message_id
    await handle_text_message(ctx, reply)

    assert ctx.service.get_chat_user_active_project("oc-chat", "ou-user").id == referenced.id
    project_id, prompt = ctx.orchestrator.scheduled[-1]
    assert project_id == referenced.id
    assert "用户正在回复一张结果卡" in prompt
    assert "网站已完成 首页已经发布" in prompt
    assert prompt.endswith("把按钮改成蓝色")
    assert ctx.orchestrator.visible_requests[-1] == "把按钮改成蓝色"
    assert ctx.orchestrator.reply_contexts[-1] == {
        "kind": "report",
        "subject_type": "root_turn",
        "subject_ref": "turn-referenced",
        "summary": "网站已完成 首页已经发布。",
        "request": "发布网站首页",
        "result": "首页已经发布。",
    }


@pytest.mark.asyncio
async def test_reply_to_running_progress_card_steers_its_turn(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    ctx.service.set_execution_status(project.id, "running")
    ctx.service.upsert_card_projection(
        projection_key=f"root-agent:{project.id}:activity",
        subject_type="root_agent",
        subject_id=project.id,
        card_kind="progress",
        provider_message_id="om-progress",
        project_id=project.id,
        lifecycle_status="running",
        resulting_state="running",
    )
    reply = message("这个检查先跳过")
    reply.parent_id = "om-progress"

    await handle_text_message(ctx, reply)

    project_id, prompt = ctx.orchestrator.steered[-1]
    assert project_id == project.id
    assert "用户正在回复一张进度卡" in prompt
    assert prompt.endswith("这个检查先跳过")
    assert ctx.orchestrator.steer_reply_contexts[-1] == {
        "kind": "progress",
        "subject_type": "root_agent",
        "subject_ref": project.id,
        "summary": "进度卡",
    }
    assert ctx.orchestrator.scheduled == []


@pytest.mark.asyncio
async def test_reply_to_stale_mac_card_does_not_fall_through_to_active_project(ctx):
    active = await ctx.service.create_project(
        "active",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    ctx.service.upsert_card_projection(
        projection_key="stale-result",
        subject_type="root_turn",
        subject_id="turn-deleted",
        card_kind="report",
        provider_message_id="om-stale",
        project_id="proj-deleted",
        lifecycle_status="completed",
        resulting_state="idle",
    )
    reply = message("继续修改")
    reply.parent_id = "om-stale"

    await handle_text_message(ctx, reply)

    assert ctx.service.get_chat_user_active_project("oc-chat", "ou-user").id == active.id
    assert ctx.orchestrator.scheduled == []
    assert ctx.orchestrator.steered == []
    assert ctx.bridge.cards[-1][2]["header"]["title"]["content"] == "原卡片所属项目不可用"


@pytest.mark.asyncio
async def test_same_turn_steer_rotates_progress_without_receipt_card(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    await handle_text_message(ctx, message("先开始这一轮"))
    ctx.service.set_execution_status(project.id, "running")
    await handle_text_message(ctx, message("补充本轮信息"))
    assert ctx.orchestrator.timeline_rotations == [project.id]
    assert ctx.bridge.cards == []
    assert ctx.bridge.updates == []
    assert ctx.bridge.reactions == [("om-1", "OnIt"), ("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_message_while_native_question_waits_uses_same_turn_steer(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "root:item-waiting",
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "选择风格",
            "options": [
                {"key": "简洁", "label": "简洁", "description": "保持简洁"},
                {"key": "炫酷", "label": "炫酷", "description": "增加视觉效果"},
            ],
        }],
        request_item_id="item-waiting",
    )
    interaction.card_message_id = "om-question"
    ctx.service.session.add(interaction)
    ctx.service.session.commit()
    ctx.service.set_execution_status(project.id, "waiting_user", reason="选择风格")
    await handle_text_message(ctx, message("先回答我另一个问题：现在做到哪了？"))
    assert ctx.orchestrator.steered == [
        (project.id, "先回答我另一个问题：现在做到哪了？")
    ]
    assert ctx.orchestrator.scheduled == []
    assert ctx.bridge.updates == []
    assert ctx.bridge.deleted == []
    assert ctx.service.get_question_interaction(interaction.id).status == "pending"


@pytest.mark.asyncio
async def test_unquoted_text_steers_while_single_free_text_question_stays_pending(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "root:item-free-text",
        [{
            "question_id": "delivery",
            "kind": "decision",
            "question": "怎么交付？",
            "allow_free_text": True,
            "options": [
                {"key": "公开", "label": "公开", "description": "公开发布"},
                {"key": "私有", "label": "私有", "description": "私有发布"},
            ],
        }],
        request_item_id="item-free-text",
    )
    interaction.card_message_id = "om-question"
    ctx.service.session.add(interaction)
    ctx.service.session.commit()
    ctx.service.upsert_card_projection(
        projection_key=f"question:{interaction.id}",
        subject_type="question_interaction",
        subject_id=interaction.id,
        card_kind="question",
        provider_message_id="om-question",
        project_id=project.id,
        lifecycle_status="pending",
        resulting_state="waiting_user",
    )
    ctx.service.set_execution_status(project.id, "waiting_user", reason="怎么交付？")
    await handle_text_message(ctx, message("都不选，直接给我现有文件链接"))

    stored = ctx.service.get_question_interaction(interaction.id)
    question = ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = ctx.service.get_question_answer(question.id)
    assert stored.status == "pending"
    assert answer is None
    assert ctx.bridge.updates == []
    assert ctx.bridge.deleted == []
    assert ctx.orchestrator.steered == [
        (project.id, "都不选，直接给我现有文件链接")
    ]
    assert ctx.orchestrator.scheduled == []


@pytest.mark.asyncio
async def test_explicit_create_without_description_waits_for_next_message(ctx):
    await handle_text_message(ctx, message("/create"))
    assert ctx.service.get_pending_intent("ou-user", "oc-chat").stage == "awaiting_description"
    await handle_text_message(ctx, message("做一个新工具"))
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is None
    assert ctx.orchestrator.scheduled[0][1] == "做一个新工具"


@pytest.mark.asyncio
async def test_pending_project_prompts_are_isolated_by_chat_and_user(ctx):
    ctx.service.upsert_pending_intent(
        "ou-user", None, "awaiting_description", chat_id="oc-chat"
    )
    ctx.service.upsert_pending_intent(
        "ou-user", None, "awaiting_description", chat_id="oc-other"
    )

    await handle_text_message(ctx, message_in_chat("另一个聊天的项目", "oc-other"))

    assert ctx.service.get_pending_intent("ou-user", "oc-other") is None
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is not None
    other = ctx.service.get_chat_user_selected_project("oc-other", "ou-user")
    assert other is not None
    assert ctx.service.get_chat_user_selected_project("oc-chat", "ou-user") is None


@pytest.mark.asyncio
async def test_return_from_create_card_clears_pending_input_and_updates_card(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key="create_prompt",
            extras={},
            chat_id="oc-chat",
            message_id="om-menu",
        ),
    )
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is not None

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id="",
            action="menu",
            state="",
            decision_id=None,
            option_key="show_menu",
            extras={},
            chat_id="oc-chat",
            message_id="om-create",
        ),
    )

    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is None
    assert "om-create" in ctx.bridge.deleted
    assert "已返回" in str(ctx.bridge.cards[-2][2])
    await handle_text_message(ctx, message("继续修改当前项目"))
    assert ctx.orchestrator.scheduled[-1] == (project.id, "继续修改当前项目")


@pytest.mark.asyncio
async def test_switch_is_explicit_project_selection(ctx):
    first = await ctx.service.create_project(
        "first", "feature", "one", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    second = await ctx.service.create_project(
        "second", "feature", "two", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    await handle_text_message(ctx, message(f"/switch {first.id}"))
    assert ctx.service.get_chat_user_selected_project("oc-chat", "ou-user").id == first.id
    assert first.id != second.id
    card = ctx.bridge.cards[-1][2]
    markdown = card["body"]["elements"][0]["content"]
    assert markdown == "后续消息将进入“first”的同一个 Codex 会话。"
    assert "**" not in markdown


@pytest.mark.asyncio
async def test_switch_accepts_project_name_and_unique_id_prefix(ctx):
    first = await ctx.service.create_project(
        "个人网站", "feature", "one", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    second = await ctx.service.create_project(
        "计费系统", "feature", "two", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/use 个人网站"))
    assert ctx.service.get_chat_user_selected_project("oc-chat", "ou-user").id == first.id

    unique_prefix = next(
        second.id[:length]
        for length in range(1, len(second.id) + 1)
        if not first.id.startswith(second.id[:length])
    )
    await handle_text_message(ctx, message(f"/use {unique_prefix}"))
    assert ctx.service.get_chat_user_selected_project("oc-chat", "ou-user").id == second.id


@pytest.mark.asyncio
async def test_ambiguous_project_name_prefix_returns_choice_card(ctx):
    await ctx.service.create_project(
        "个人网站 A", "feature", "one", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    await ctx.service.create_project(
        "个人网站 B", "feature", "two", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/use 个人网站"))

    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "选择项目"
    labels = [
        action["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element.get("actions", [])
    ]
    assert any("个人网站 A" in label for label in labels)
    assert any("个人网站 B" in label for label in labels)
    assert all("idle" not in label for label in labels)


@pytest.mark.asyncio
async def test_newthread_is_explicit_project_control(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )

    await handle_text_message(ctx, message("/n"))

    assert ctx.orchestrator.new_threads == [project.id]
    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "当前项目已清空对话"
    assert project.workspace_path in card["body"]["elements"][0]["content"]
    assert "下一条普通消息会开始新的 Codex 对话" in card["body"]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_help_presents_short_mobile_commands(ctx):
    await handle_text_message(ctx, message("/h"))

    markdown = ctx.bridge.cards[-1][2]["body"]["elements"][0]["content"]
    for command in (
        "/create", "/c", "/proj", "/ls", "/use", "/s", "/info", "/i",
        "/agents", "/a",
        "/new", "/n", "/open", "/o", "/bind", "/b", "/unbind", "/u", "/pause", "/p",
        "/resume", "/r", "/stop", "/x", "/menu", "/m", "/help", "/h",
        "/owner", "/ow",
    ):
        assert f"`{command}" in markdown
    assert "/newthread" not in markdown
    assert "/current" not in markdown


@pytest.mark.asyncio
async def test_readable_new_alias_resets_current_project_thread(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/new"))

    assert ctx.orchestrator.new_threads == [project.id]


@pytest.mark.asyncio
async def test_short_stop_alias_interrupts_turn_without_cancelling_project(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/x"))

    assert ctx.orchestrator.stopped == [project.id]
    assert ctx.service.require_project(project.id).status == "active"


@pytest.mark.asyncio
async def test_stop_expires_and_closes_pending_native_question(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "stop-pending",
        [{"question_id": "mode", "kind": "decision", "question": "Mode?"}],
        request_item_id="request-stop-pending",
    )
    interaction.card_message_id = "om-question"
    ctx.service.session.add(interaction)
    ctx.service.session.commit()
    ctx.service.upsert_card_projection(
        projection_key=f"question:{interaction.id}",
        subject_type="question_interaction",
        subject_id=interaction.id,
        card_kind="question",
        provider_message_id="om-question",
        project_id=project.id,
        lifecycle_status="pending",
        resulting_state="waiting_user",
    )

    await handle_text_message(ctx, message("/stop"))

    stored = ctx.service.get_question_interaction(interaction.id)
    assert stored.status == "expired"
    assert stored.resolution_source == "turn_stopped"
    assert ctx.bridge.deleted == ["om-question"]
    assert (
        ctx.service.get_card_projection(f"question:{interaction.id}").lifecycle_status
        == "recalled"
    )


@pytest.mark.asyncio
async def test_stop_failure_still_returns_visible_card(ctx):
    await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )

    async def fail_stop(_project_id):
        raise RuntimeError("native turn changed")

    ctx.orchestrator.stop_root_turn = fail_stop
    await handle_text_message(ctx, message("/stop"))

    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "停止失败"
    assert card["header"]["template"] == "red"
    assert "项目和目录未被删除" in str(card)


@pytest.mark.asyncio
async def test_open_directory_immediately_selects_project_and_routes_next_message(
    ctx, tmp_path, monkeypatch
):
    existing = await ctx.service.create_project(
        "existing",
        "feature",
        "keep iterating",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    repository = (tmp_path / "existing-repository").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )

    await handle_text_message(ctx, message(f"/bind {repository}"))

    assert ctx.service.get_chat_directory_binding("oc-chat") is None
    assert ctx.service.require_project(existing.id).status == "active"
    selected = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert selected is not None
    assert selected.id != existing.id
    assert selected.workspace_path == str(repository)
    assert ctx.orchestrator.scheduled == []

    await handle_text_message(ctx, message("修复已有仓库的 README"))
    assert ctx.orchestrator.scheduled[-1] == (
        selected.id,
        "修复已有仓库的 README",
    )


@pytest.mark.asyncio
async def test_open_directory_then_newthread_targets_opened_project(
    ctx, tmp_path, monkeypatch
):
    old = await ctx.service.create_project(
        "old", "feature", "old", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    repository = (tmp_path / "opened-repository").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )

    await handle_text_message(ctx, message(f"/open {repository}"))
    opened = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert opened is not None and opened.id != old.id

    await handle_text_message(ctx, message("/new"))
    assert ctx.orchestrator.new_threads == [opened.id]


@pytest.mark.asyncio
async def test_reopening_directory_resumes_only_within_same_chat(
    ctx, tmp_path, monkeypatch
):
    repository = (tmp_path / "shared-repository").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )

    await handle_text_message(ctx, message_in_chat(f"/open {repository}", "oc-chat"))
    first = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert first is not None

    await handle_text_message(ctx, message_in_chat(f"/open {repository}", "oc-chat"))
    reopened = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert reopened is not None and reopened.id == first.id

    await handle_text_message(ctx, message_in_chat(f"/open {repository}", "oc-other"))
    isolated = ctx.service.get_chat_user_selected_project("oc-other", "ou-user")
    assert isolated is not None and isolated.id != first.id
    assert isolated.workspace_path == first.workspace_path
    assert isolated.feishu_chat_id == "oc-other"


@pytest.mark.asyncio
async def test_switch_cannot_import_another_chats_thread(ctx):
    other = await ctx.service.create_project(
        "other",
        "feature",
        "other chat",
        creator_open_id="ou-user",
        feishu_chat_id="oc-other",
    )

    await handle_text_message(ctx, message_in_chat(f"/switch {other.id}", "oc-chat"))

    assert ctx.service.get_chat_user_selected_project("oc-chat", "ou-user") is None
    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "切换失败"


@pytest.mark.asyncio
async def test_manual_absolute_path_is_answer_not_unknown_command(
    ctx, tmp_path, monkeypatch
):
    repository = (tmp_path / "manual-repository").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )
    ctx.service.upsert_pending_intent(
        "ou-user",
        None,
        "awaiting_bind_dir",
        chat_id="oc-chat",
        chat_type="p2p",
    )

    await handle_text_message(ctx, message(str(repository)))

    selected = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert selected is not None
    assert selected.workspace_path == str(repository)
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is None
    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "项目已打开"


@pytest.mark.asyncio
async def test_known_command_supersedes_pending_manual_path(ctx):
    ctx.service.upsert_pending_intent(
        "ou-user",
        None,
        "awaiting_bind_dir",
        chat_id="oc-chat",
        chat_type="p2p",
    )

    await handle_text_message(ctx, message("/info"))

    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is None
    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "当前项目"


@pytest.mark.asyncio
async def test_missing_explicit_selection_never_falls_back_to_recent_project(ctx):
    old = await ctx.service.create_project(
        "old", "feature", "old", creator_open_id="ou-user", feishu_chat_id="oc-other"
    )

    await handle_text_message(ctx, message("创建当前聊天自己的项目"))

    selected = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert selected is not None and selected.id != old.id
    assert ctx.orchestrator.scheduled == [(selected.id, "创建当前聊天自己的项目")]


@pytest.mark.asyncio
async def test_current_card_uses_selected_project_workspace(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/info"))

    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "当前项目"
    markdown = card["body"]["elements"][0]["content"]
    assert project.workspace_path in markdown
    assert "绑定目录" not in markdown
    assert "执行状态**：空闲" in markdown
    assert "当前没有 Provider 返回可核验费用" in markdown


@pytest.mark.asyncio
async def test_owner_command_shows_complete_owner_memory_layers(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "交付个人网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    ctx.service.add_owner_memories(
        "ou-user",
        [
            {
                "preference": "低风险且可撤销的决定由 AI 处理",
                "evidence": "低风险且可撤销的决定由 AI 处理",
                "category": "autonomy",
                "scope": "global",
                "strength": "hard",
            },
            {
                "preference": "当前项目优先保证移动端体验",
                "evidence": "当前项目优先保证移动端体验",
                "category": "quality",
                "scope": "project",
                "strength": "hard",
            },
        ],
        project_id=project.id,
        source_ref="message-policy",
    )
    ctx.service.add_project_policies(
        project.id,
        [
            {
                "policy": "不能引入登录系统",
                "evidence": "不能引入登录系统",
                "source_ref": "message-project-policy",
                "strength": "hard",
            }
        ],
        source_ref="message-project-policy",
    )
    ctx.service.create_project_decision(
        project.id,
        request_item_id="direction-open",
        native_turn_id="turn-open",
        question="下一步做什么",
        options=[],
        decision="先完成移动端验收",
        rationale="移动端是当前目标",
        impact="",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
        decision_type="governance",
        execution_status="active",
    )
    ctx.service.create_project_decision(
        project.id,
        request_item_id="direction-done",
        native_turn_id="turn-done",
        question="先确定哪项业务方向",
        options=[],
        decision="采用离线优先方案",
        rationale="符合用户约束",
        impact="",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
        decision_type="governance",
        execution_status="completed",
    )
    ctx.service.create_project_decision(
        project.id,
        request_item_id="direction-pending",
        native_turn_id="turn-pending",
        question="是否保持移动端优先",
        options=[],
        decision="",
        rationale="需要用户确认方向",
        impact="",
        reversible=True,
        confidence="medium",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
        status="requires_user",
        decision_type="alignment",
    )
    ctx.service.create_project_decision(
        project.id,
        request_item_id="stale-advice",
        native_turn_id="turn-advice",
        question="旧建议不应成为当前待办",
        options=[],
        decision="历史建议",
        rationale="仅用于验证展示边界",
        impact="",
        reversible=True,
        confidence="medium",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
        status="requires_user",
        decision_type="advice",
    )
    replied_history = ctx.service.begin_project_history_round(
        project.id,
        user_request="是否采用离线优先方案",
    )
    replied_history.native_turn_id = "turn-offline-result"
    replied_history.report_content = "建议采用离线优先方案"
    replied_history.report_status = "completed"
    ctx.service.session.add(replied_history)
    ctx.service.session.commit()
    history = ctx.service.begin_project_history_round(
        project.id,
        user_request="同意",
        reply_context={
            "kind": "report",
            "subject_type": "root_turn",
            "subject_ref": "turn-offline-result",
            "summary": "是否采用离线优先方案？建议采用离线优先方案。",
        },
    )
    history.report_content = "已经按离线优先方案完成移动端页面验收。"
    ctx.service.session.add(history)
    ctx.service.session.commit()
    ctx.service.finish_unreported_project_history_round(
        history.id, status="completed"
    )

    await handle_text_message(ctx, message("/ow"))

    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "Project Owner"
    assert "当前执行**：空闲" in str(card)
    assert "当前项目的用户规则" in str(card)
    assert "不能引入登录系统" in str(card)
    assert "当前项目优先保证移动端体验" in str(card)
    assert "当前方向与未完成承诺" in str(card)
    assert "未完成·推进中" in str(card)
    assert "先完成移动端验收" in str(card)
    direction_section = card["body"]["elements"][0]["content"].split(
        "**当前方向与未完成承诺**：", 1
    )[1].split("**最近项目记录**：", 1)[0]
    assert "采用离线优先方案" not in direction_section
    assert "是否保持移动端优先" in str(card)
    assert "旧建议不应成为当前待办" not in str(card)
    assert "最近项目记录" in str(card)
    assert "用户：同意" in str(card)
    assert "回应的请求：是否采用离线优先方案" in str(card)
    assert "回应时结果：建议采用离线优先方案" in str(card)
    assert "本轮结果：已经按离线优先方案完成移动端页面验收" in str(card)
    assert "跨项目用户偏好" in str(card)
    assert "低风险且可撤销的决定由 AI 处理" in str(card)


@pytest.mark.asyncio
async def test_owner_command_can_disable_and_reenable_current_project(ctx):
    project = await ctx.service.create_project(
        "owner-switch", "feature", "交付网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/owner off"))
    assert ctx.service.is_project_owner_enabled(project.id) is False
    assert "后续汇报不再触发 Owner" in str(ctx.bridge.cards[-1][2])

    await handle_text_message(ctx, message("/ow status"))
    assert "Owner**：已关闭" in str(ctx.bridge.cards[-1][2])

    await handle_text_message(ctx, message("/owner on"))
    assert ctx.service.is_project_owner_enabled(project.id) is True
    assert "后续汇报、问题和审批会恢复" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_model_command_shows_current_model_without_argument(ctx):
    project = await ctx.service.create_project(
        "model-show", "feature", "交付网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/model"))

    assert "当前默认模型" in str(ctx.bridge.cards[-1][2])
    assert "gpt-6.1-sol" in str(ctx.bridge.cards[-1][2])
    assert ctx.orchestrator.models_set == []


@pytest.mark.asyncio
async def test_model_command_switches_and_restores_default(ctx):
    project = await ctx.service.create_project(
        "model-switch", "feature", "交付网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/model gpt-6-astra"))

    assert ctx.orchestrator.models_set == [(project.id, "gpt-6-astra")]
    assert "模型已切换" in str(ctx.bridge.cards[-1][2])
    assert "`gpt-6.1-sol` → `gpt-6-astra`" in str(ctx.bridge.cards[-1][2])

    await handle_text_message(ctx, message("/model default"))

    assert ctx.orchestrator.models_set[-1] == (project.id, None)
    assert "模型未变化" in str(ctx.bridge.cards[-1][2])
    assert "已是当前模型" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_model_command_requires_project_and_rejects_invalid_name(ctx):
    await handle_text_message(ctx, message("/model"))
    assert "请先创建或切换项目" in str(ctx.bridge.cards[-1][2])

    project = await ctx.service.create_project(
        "model-guard", "feature", "交付网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )

    await handle_text_message(ctx, message("/model bad name!"))
    assert "模型名不合法" in str(ctx.bridge.cards[-1][2])

    await handle_text_message(ctx, message("/model 模型x"))
    assert "模型名不合法" in str(ctx.bridge.cards[-1][2])
    assert ctx.orchestrator.models_set == []


@pytest.mark.asyncio
async def test_current_card_hides_old_usage_while_new_thread_is_pending(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    project.context = {**(project.context or {}), "root_thread_reset_pending": True}
    ctx.service.session.add(project)
    ctx.service.session.commit()

    await handle_text_message(ctx, message("/info"))

    markdown = ctx.bridge.cards[-1][2]["body"]["elements"][0]["content"]
    assert "新对话待开始" in markdown
    assert "最近一轮 Token" not in markdown


@pytest.mark.asyncio
async def test_legacy_directory_binding_is_ignored_on_next_message(
    ctx, tmp_path, monkeypatch
):
    repository = (tmp_path / "legacy-bound-repository").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )
    ctx.service.bind_directory_to_chat("oc-chat", str(repository), bound_by="ou-user")

    await handle_text_message(ctx, message("检查这个已有项目"))

    selected = ctx.service.get_chat_user_selected_project("oc-chat", "ou-user")
    assert selected is not None
    assert selected.workspace_path != str(repository)
    assert ctx.service.get_chat_directory_binding("oc-chat") is not None
    assert ctx.orchestrator.scheduled == [(selected.id, "检查这个已有项目")]


@pytest.mark.asyncio
async def test_request_user_input_card_updates_then_resolves(ctx, monkeypatch):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "batch",
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "风格？",
            "options": [
                {"key": "简洁", "label": "简洁"},
                {"key": "炫酷", "label": "炫酷"},
            ],
        }],
        request_item_id="request-style",
    )
    interaction.card_message_id = "om-card"
    ctx.service.session.add(interaction)
    ctx.service.session.commit()
    ctx.service.upsert_card_projection(
        projection_key=f"question:{interaction.id}",
        subject_type="question_interaction",
        subject_id=interaction.id,
        card_kind="question",
        provider_message_id="om-card",
        project_id=project.id,
        lifecycle_status="pending",
        resulting_state="waiting_user",
    )
    assessment = ctx.service.create_project_decision(
        project.id,
        request_item_id="assessment:request-style",
        native_turn_id="turn-style",
        question="风格？",
        options=[{"label": "简洁"}, {"label": "炫酷"}],
        decision="请选择风格",
        rationale="缺少偏好",
        impact="只影响视觉风格",
        reversible=False,
        confidence="low",
        answer_payload={"requires_user": True},
        context_refs=[],
        actor_model="owner-test",
        status="requires_user",
        decision_type="assessment",
    )
    ctx.service.update_project_decision(
        assessment.id,
        status="requires_user",
        card_message_id="om-assessment",
    )
    monkeypatch.setattr(
        "mac_system.core.root_agent.resolve_root_user_input", lambda *_: False
    )
    selected = await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=interaction.id,
            action="select_question_option",
            state="ROOT_AGENT_WAITING_USER",
            decision_id=None,
            option_key="炫酷",
            extras={"interaction_id": interaction.id, "question_id": "style"},
            chat_id="oc-chat",
            message_id="om-card",
        ),
    )
    assert selected is not None
    assert "炫酷" in str(selected)
    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=interaction.id,
            action="submit_selected_options",
            state="ROOT_AGENT_WAITING_USER",
            decision_id=None,
            option_key=None,
            extras={"interaction_id": interaction.id},
            chat_id="oc-chat",
            message_id="om-card",
        ),
    )
    assert ctx.service.session.get(QuestionInteraction, interaction.id).status == "answered"
    assert ctx.service.session.get(QuestionInteraction, interaction.id).selected_options == {
        "style": "炫酷"
    }
    assert ctx.service.session.exec(select(Approval)).first() is None
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert "om-card" in ctx.bridge.deleted
    assert "om-assessment" in ctx.bridge.deleted
    assert ctx.bridge.updates == []
    assert (
        ctx.service.get_card_projection(f"question:{interaction.id}").lifecycle_status
        == "recalled"
    )
    assert any("已提交：炫酷" in str(card) for _recipient, _kind, card in ctx.bridge.cards)
    stored_assessment = ctx.service.get_project_decision(assessment.id)
    assert stored_assessment.status == "resolved_by_user"
    assert stored_assessment.user_override == "炫酷"
    assert stored_assessment.card_message_id == "om-assessment"


@pytest.mark.asyncio
async def test_human_card_submission_cannot_overwrite_project_owner_claim(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "owner-won-race",
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "风格？",
            "options": [
                {"key": "简洁", "label": "简洁"},
                {"key": "炫酷", "label": "炫酷"},
            ],
        }],
        request_item_id="request-owner-won-race",
    )
    assert ctx.service.claim_question_interaction(
        interaction.id, resolution_source="project_owner"
    )

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=interaction.id,
            action="answer_question_option",
            state="ROOT_AGENT_WAITING_USER",
            decision_id=None,
            option_key="炫酷",
            extras={"interaction_id": interaction.id, "question_id": "style"},
            chat_id="oc-chat",
            message_id="om-card",
        ),
    )

    stored = ctx.service.get_question_interaction(interaction.id)
    question = ctx.service.get_questions_for_interaction(interaction.id)[0]
    assert stored.status == "resolving"
    assert stored.resolution_source == "project_owner"
    assert ctx.service.get_question_answer(question.id) is None
    assert ctx.orchestrator.scheduled == []
    assert "已经提交或失效" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_text_reply_to_single_question_card_is_the_free_text_answer(
    ctx, monkeypatch
):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "single-question",
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "风格？",
            "allow_free_text": True,
            "options": [],
        }],
        request_item_id="request-style-text",
    )
    ctx.service.upsert_card_projection(
        projection_key=f"question:{interaction.id}",
        subject_type="question_interaction",
        subject_id=interaction.id,
        card_kind="question",
        provider_message_id="om-question",
        project_id=project.id,
        lifecycle_status="pending",
        resulting_state="waiting_user",
    )
    monkeypatch.setattr(
        "mac_system.core.root_agent.resolve_root_user_input", lambda *_: False
    )
    reply = message("更简洁一些，保留蓝色")
    reply.parent_id = "om-question"

    await handle_text_message(ctx, reply)

    stored = ctx.service.get_question_interaction(interaction.id)
    question = ctx.service.get_questions_for_interaction(interaction.id)[0]
    answer = ctx.service.get_question_answer(question.id)
    assert stored.status == "answered"
    assert answer.answer == "更简洁一些，保留蓝色"
    assert ctx.bridge.updates == []
    assert ctx.bridge.deleted == ["om-question"]
    assert (
        ctx.service.get_card_projection(f"question:{interaction.id}").lifecycle_status
        == "recalled"
    )
    assert "答案已提交" in str(ctx.bridge.cards[-1][2])
    assert ctx.orchestrator.scheduled[-1][0] == project.id


async def _create_delivered_project_decision(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    interaction = await ctx.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "owner-style-question",
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "继续使用哪种风格？",
            "options": [{"key": "现代简洁", "label": "现代简洁"}],
        }],
        request_item_id="request-style-owner",
        requested_to="user",
        escalated_to_user=True,
    )
    question = ctx.service.get_questions_for_interaction(interaction.id)[0]
    await ctx.service.submit_question_answer(
        question.id,
        "现代简洁",
        "现代简洁",
        False,
        "project_owner",
        answer_source="project_owner",
        commit=False,
    )
    await ctx.service.resolve_question_interaction(
        interaction.id,
        commit=False,
        resolution_source="project_owner",
    )
    ctx.service.session.commit()
    question_card = build_question_batch_card(
        interaction.id,
        interaction.id,
        [{
            "question_id": "style",
            "kind": "decision",
            "question": "继续使用哪种风格？",
            "options": [{"key": "现代简洁", "label": "现代简洁"}],
        }],
        "root_agent",
        "conversation",
        project_name=project.name,
        chat_id="oc-chat",
        submitted_answers={"style": "现代简洁"},
        resolution_note="✅ AI Project Owner 已代你选择，正在继续处理",
    )
    question_message_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=f"question:{interaction.id}",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="question",
        card=question_card,
        project_id=project.id,
        projection_key=f"question:{interaction.id}",
        subject_type="question_interaction",
        subject_id=interaction.id,
        lifecycle_status="resolved_by_project_owner",
        resulting_state="running",
    )
    interaction.card_message_id = question_message_id
    ctx.service.session.add(interaction)
    ctx.service.session.commit()
    decision = ctx.service.create_project_decision(
        project.id,
        request_item_id="request-style-owner",
        native_turn_id="turn-owner",
        question="继续使用哪种风格？",
        options=[{"question_id": "style", "label": "现代简洁"}],
        decision="继续沿用现代简洁风格",
        rationale="上一轮结果已经使用该风格。",
        impact="保持界面一致。",
        reversible=True,
        confidence="high",
        answer_payload={"answers": {"style": {"answers": ["现代简洁"]}}},
        context_refs=["notification-result"],
        actor_model="gpt-owner-test",
        status="pending_delivery",
    )
    card = build_project_decision_card(
        decision.id,
        project_name=project.name,
        question=decision.question,
        decision=decision.decision,
        rationale=decision.rationale,
        impact=decision.impact,
        reversible=decision.reversible,
        status="applied",
        actor_model=decision.actor_model,
        chat_id="oc-chat",
    )
    message_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=f"decision:{decision.id}",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="decision",
        card=card,
        project_id=project.id,
        projection_key=f"decision:{decision.id}",
        subject_type="project_decision",
        subject_id=decision.id,
        lifecycle_status="applied",
        resulting_state="idle",
    )
    decision = ctx.service.update_project_decision(
        decision.id, status="applied", card_message_id=message_id
    )
    return project, decision, message_id


@pytest.mark.asyncio
async def test_decision_evidence_expansion_updates_same_card_and_projection(ctx):
    _, decision, message_id = await _create_delivered_project_decision(ctx)

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="toggle_project_decision_details",
            state="applied",
            decision_id=decision.id,
            option_key=None,
            extras={"expanded": True},
            chat_id="oc-chat",
            message_id=message_id,
        ),
    )

    assert ctx.bridge.updates[-1][0] == message_id
    assert "上一轮结果已经使用该风格" in str(ctx.bridge.updates[-1][1])
    projection = ctx.service.get_card_projection(f"decision:{decision.id}")
    assert projection.selected_action is None
    assert projection.view_state == "details_expanded"

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="toggle_project_decision_details",
            state="applied",
            decision_id=decision.id,
            option_key=None,
            extras={"expanded": False},
            chat_id="oc-chat",
            message_id=message_id,
        ),
    )

    assert ctx.bridge.updates[-1][0] == message_id
    panels = [
        node
        for node in _card_nodes(ctx.bridge.updates[-1][1])
        if node.get("tag") == "collapsible_panel"
    ]
    assert panels[0]["header"]["title"]["content"] == "···"
    assert panels[0]["expanded"] is False
    projection = ctx.service.get_card_projection(f"decision:{decision.id}")
    assert projection.selected_action is None
    assert projection.view_state == "details_collapsed"


@pytest.mark.asyncio
async def test_owner_choice_button_updates_card_and_continues_root(ctx):
    project = await ctx.service.create_project(
        "owner-choice", "feature", "完善网站", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    options = [
        {"label": "优先稳定性", "description": "先修复已知故障"},
        {"label": "优先视觉效果", "description": "先完善页面体验"},
    ]
    decision = ctx.service.create_project_decision(
        project.id,
        request_item_id="report:owner-choice",
        native_turn_id="turn-owner-choice",
        question="下一步优先做什么？",
        options=options,
        decision="下一步优先稳定性还是视觉效果？",
        rationale="两项都合理，需要你的偏好。",
        impact="只影响下一步顺序。",
        reversible=False,
        confidence="low",
        answer_payload={"requires_user": True},
        context_refs=["report:owner-choice"],
        actor_model="owner-test",
        status="requires_user",
        decision_type="assessment",
    )
    card = build_project_decision_card(
        decision.id,
        project_name=project.name,
        question=decision.question,
        decision=decision.decision,
        rationale=decision.rationale,
        impact=decision.impact,
        reversible=False,
        options=options,
        status="requires_user",
        chat_id="oc-chat",
    )
    message_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=f"decision:{decision.id}",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="decision",
        card=card,
        project_id=project.id,
        projection_key=f"decision:{decision.id}",
        subject_type="project_decision",
        subject_id=decision.id,
        lifecycle_status="requires_user",
        resulting_state="waiting_user",
    )
    ctx.service.update_project_decision(
        decision.id, status="requires_user", card_message_id=message_id
    )

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="select_owner_option",
            state="requires_user",
            decision_id=decision.id,
            option_key="优先稳定性",
            extras={},
            chat_id="oc-chat",
            message_id=message_id,
        ),
    )

    stored = ctx.service.get_project_decision(decision.id)
    assert stored.status == "resolved_by_user"
    assert stored.user_override == "优先稳定性"
    choice_log = next(
        event
        for event in ctx.service.get_project_events(project.id)
        if event.message == "User choice recorded"
    )
    assert choice_log.meta["source"] == "project_owner_decision"
    assert choice_log.meta["decision_id"] == decision.id
    assert choice_log.meta["choice"] == "优先稳定性"
    assert choice_log.meta["responded_by"] == "ou-user"
    assert message_id in ctx.bridge.deleted
    assert ctx.bridge.updates == []
    assert "你的选择" in str(ctx.bridge.cards[-1][2])
    assert "优先稳定性" in str(ctx.bridge.cards[-1][2])
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert ctx.orchestrator.visible_requests[-1] == "我选择：优先稳定性"


@pytest.mark.asyncio
async def test_owner_advice_record_only_choice_updates_card_without_root_turn(ctx):
    project = await ctx.service.create_project(
        "owner-advice",
        "feature",
        "完善网站",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    options = [
        {"label": "采纳建议", "description": "继续验证", "action": "execute"},
        {"label": "暂不采纳", "description": "保持现状", "action": "record_only"},
    ]
    decision = ctx.service.create_project_decision(
        project.id,
        request_item_id="report:owner-advice",
        native_turn_id="turn-owner-advice",
        question="是否采纳本轮建议？",
        options=options,
        decision="先做一次真实入口验证",
        rationale="当前只有脚本结果。",
        impact="不改变项目目标。",
        reversible=False,
        confidence="medium",
        answer_payload={"requires_user": True},
        context_refs=["report:owner-advice"],
        actor_model="owner-test",
        status="requires_user",
        decision_type="advice",
    )
    card = build_project_decision_card(
        decision.id,
        project_name=project.name,
        question=decision.question,
        decision=decision.decision,
        rationale=decision.rationale,
        impact=decision.impact,
        reversible=False,
        options=options,
        status="advice",
        chat_id="oc-chat",
    )
    message_id = await deliver_card(
        ctx.service,
        ctx.bridge,
        dedupe_key=f"decision:{decision.id}",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="advice",
        card=card,
        project_id=project.id,
        projection_key=f"decision:{decision.id}",
        subject_type="project_decision",
        subject_id=decision.id,
        lifecycle_status="requires_user",
        resulting_state="waiting_user",
    )
    ctx.service.update_project_decision(
        decision.id, status="requires_user", card_message_id=message_id
    )
    before = len(ctx.orchestrator.scheduled)

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="select_owner_option",
            state="requires_user",
            decision_id=decision.id,
            option_key="暂不采纳",
            extras={},
            chat_id="oc-chat",
            message_id=message_id,
        ),
    )

    stored = ctx.service.get_project_decision(decision.id)
    assert stored.status == "resolved_by_user"
    assert stored.user_override == "暂不采纳"
    assert len(ctx.orchestrator.scheduled) == before


@pytest.mark.asyncio
async def test_owner_alignment_choice_resumes_original_request(ctx):
    project = await ctx.service.create_project(
        "site",
        "feature",
        "initial",
        creator_open_id="ou-user",
        feishu_chat_id="oc-chat",
    )
    decision = ctx.service.create_project_decision(
        project.id,
        request_item_id="owner-alignment-1",
        native_turn_id=None,
        question="开始执行前需要确认",
        options=[
            {
                "label": "长期稳定",
                "description": "先保证稳定可用。",
                "action": "execute",
            },
            {
                "label": "快速见效",
                "description": "先交付明显变化。",
                "action": "execute",
            },
        ],
        decision="这次更看重快速看到效果，还是长期稳定？",
        rationale="历史中没有可靠偏好。",
        impact="影响本轮优先顺序。",
        reversible=False,
        confidence="low",
        answer_payload={
            "requires_user": True,
            "source_request": "把个人网站继续完善一下",
        },
        context_refs=[],
        actor_model="owner-model",
        status="requires_user",
        decision_type="alignment",
    )
    ctx.service.update_project_decision(
        decision.id, status="requires_user", card_message_id="om-alignment"
    )

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="select_owner_option",
            state="waiting_user",
            decision_id=decision.id,
            option_key="长期稳定",
            extras={},
            chat_id="oc-chat",
            message_id="om-alignment",
        ),
    )

    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert "原请求：把个人网站继续完善一下" in ctx.orchestrator.scheduled[-1][1]
    assert "用户选择：长期稳定" in ctx.orchestrator.scheduled[-1][1]
    assert ctx.orchestrator.visible_requests[-1] == "把个人网站继续完善一下"
    assert ctx.orchestrator.request_origins[-1] == "human_aligned"
    assert ctx.orchestrator.parent_decision_ids[-1] == decision.id
    assert ctx.orchestrator.reply_contexts[-1] == {
        "kind": "decision",
        "subject_type": "project_decision",
        "subject_ref": decision.id,
        "summary": (
            "事项：开始执行前需要确认；Owner 方案："
            "这次更看重快速看到效果，还是长期稳定？；用户选择：长期稳定"
        ),
    }


@pytest.mark.asyncio
async def test_adjust_decision_button_appends_prompt_then_routes_user_override(ctx):
    project, decision, message_id = await _create_delivered_project_decision(ctx)

    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=decision.id,
            action="adjust_project_decision",
            state="applied",
            decision_id=decision.id,
            option_key=None,
            extras={},
            chat_id="oc-chat",
            message_id=message_id,
        ),
    )

    stored = ctx.service.session.get(ProjectDecision, decision.id)
    assert stored.status == "awaiting_adjustment"
    assert ctx.service.get_pending_intent("ou-user", "oc-chat").stage == (
        "awaiting_decision_adjustment"
    )
    assert ctx.bridge.updates == []
    assert "请说明怎么调整" in str(ctx.bridge.cards[-1][2])
    assert message_id not in ctx.bridge.deleted

    await handle_text_message(ctx, message("改成未来科技风格，但保留现有布局"))

    stored = ctx.service.session.get(ProjectDecision, decision.id)
    assert stored.status == "adjustment_requested"
    assert stored.user_override == "改成未来科技风格，但保留现有布局"
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is None
    assert ctx.bridge.updates == []
    assert "你的调整" in str(ctx.bridge.cards[-1][2])
    assert "改成未来科技风格，但保留现有布局" in str(ctx.bridge.cards[-1][2])
    assert "message-3" in ctx.bridge.deleted
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert "原决定：继续沿用现代简洁风格" in ctx.orchestrator.scheduled[-1][1]
    assert ctx.orchestrator.scheduled[-1][1].endswith("改成未来科技风格，但保留现有布局")
    assert ctx.orchestrator.visible_requests[-1] == "改成未来科技风格，但保留现有布局"


@pytest.mark.asyncio
async def test_direct_reply_to_decision_card_is_an_explicit_adjustment(ctx):
    project, decision, message_id = await _create_delivered_project_decision(ctx)
    reply = message("改为暖色，但其他内容保持不变")
    reply.parent_id = message_id

    await handle_text_message(ctx, reply)

    stored = ctx.service.session.get(ProjectDecision, decision.id)
    assert stored.status == "adjustment_requested"
    assert stored.user_override == "改为暖色，但其他内容保持不变"
    assert ctx.bridge.updates == []
    assert "你的调整" in str(ctx.bridge.cards[-1][2])
    assert "改为暖色，但其他内容保持不变" in str(ctx.bridge.cards[-1][2])
    assert message_id not in ctx.bridge.deleted
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert ctx.orchestrator.visible_requests[-1] == "改为暖色，但其他内容保持不变"
    assert ctx.service.get_chat_user_active_project("oc-chat", "ou-user").id == project.id


@pytest.mark.asyncio
async def test_adjustment_card_failure_does_not_drop_decision_adjustment(
    ctx, monkeypatch
):
    project, decision, message_id = await _create_delivered_project_decision(ctx)
    from mac_system.feishu import handlers

    real_deliver_card = handlers.deliver_card

    async def fail_adjustment_card(*args, **kwargs):
        if kwargs.get("subject_type") == "project_decision_adjustment":
            raise RuntimeError("adjustment card temporarily unavailable")
        return await real_deliver_card(*args, **kwargs)

    monkeypatch.setattr(handlers, "deliver_card", fail_adjustment_card)
    reply = message("改为暖色，但其他内容保持不变")
    reply.parent_id = message_id

    await handle_text_message(ctx, reply)

    stored = ctx.service.session.get(ProjectDecision, decision.id)
    assert stored.status == "adjustment_requested"
    assert stored.user_override == "改为暖色，但其他内容保持不变"
    assert ctx.bridge.updates == []
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert ctx.orchestrator.visible_requests[-1] == "改为暖色，但其他内容保持不变"


@pytest.mark.asyncio
async def test_unknown_card_action_is_rejected_without_side_effect(ctx):
    await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id="old",
            action="confirm",
            state="unknown",
            decision_id=None,
            option_key=None,
            extras={},
            chat_id="oc-chat",
        ),
    )
    assert "当前不可操作" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_cancel_command_interrupts_selected_project_but_keeps_it_active(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    await handle_text_message(ctx, message("/cancel"))
    assert ctx.orchestrator.stopped == [project.id]
    assert ctx.service.require_project(project.id).status == "active"
