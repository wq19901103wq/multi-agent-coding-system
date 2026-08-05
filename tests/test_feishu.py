import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.config import Settings
from mac_system.core.project_service import ProjectService
from mac_system.feishu.card import build_question_batch_card
from mac_system.feishu.handlers import (
    FeishuHandlerContext,
    handle_card_action,
    handle_text_message,
)
from mac_system.feishu.models import ButtonAction, TextMessage
from mac_system.models import Approval, Project, QuestionInteraction


class RecordingBridge:
    def __init__(self):
        self.cards = []
        self.updates = []
        self.reactions = []

    async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
        self.cards.append((recipient, receive_id_type, card))
        return f"message-{len(self.cards)}"

    async def update_card(self, message_id, card):
        self.updates.append((message_id, card))

    async def add_reaction(self, message_id, emoji_type="OnIt"):
        self.reactions.append((message_id, emoji_type))
        return f"reaction-{len(self.reactions)}"


class Orchestrator:
    def __init__(self, service):
        self.project_service = service
        self.scheduled = []
        self.steered = []
        self.cancelled = []
        self.stopped = []
        self.new_threads = []
        self.steer_result = True

    def schedule_root_agent(self, project_id, text):
        self.scheduled.append((project_id, text))

    async def steer_root_agent(self, project_id, text):
        self.steered.append((project_id, text))
        return self.steer_result

    async def cancel_project(self, project_id, reason=""):
        self.cancelled.append((project_id, reason))
        await self.project_service.cancel_project(project_id, reason)

    async def stop_root_turn(self, project_id):
        self.stopped.append(project_id)
        return True

    async def start_new_root_thread(self, project_id):
        self.new_threads.append(project_id)
        return "old-thread", "new-thread"


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
    )

    nodes = list(_card_nodes(card))
    assert card["schema"] == "2.0"
    assert "elements" not in card
    assert all(node.get("tag") != "action" for node in nodes)
    buttons = [node for node in nodes if node.get("tag") == "button"]
    assert buttons
    assert all(button.get("behaviors") for button in buttons)


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
    assert ctx.orchestrator.scheduled == []
    assert ctx.bridge.cards == []
    assert ctx.bridge.reactions == [("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_same_turn_steer_uses_message_reaction_without_receipt_card(ctx):
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
    assert ctx.bridge.cards == []
    assert ctx.bridge.updates == []
    assert ctx.bridge.reactions == [("om-1", "OnIt"), ("om-1", "OnIt")]


@pytest.mark.asyncio
async def test_message_while_native_question_waits_uses_same_turn_steer(ctx):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    ctx.service.set_execution_status(project.id, "waiting_user", reason="选择风格")
    await handle_text_message(ctx, message("先回答我另一个问题：现在做到哪了？"))
    assert ctx.orchestrator.steered == [
        (project.id, "先回答我另一个问题：现在做到哪了？")
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
    assert ctx.bridge.updates[-1][0] == "om-create"
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
    assert card["header"]["title"]["content"] == "当前项目已开始新对话"
    assert project.workspace_path in card["body"]["elements"][0]["content"]
    assert "后续消息不会携带旧对话历史" in card["body"]["elements"][0]["content"]


@pytest.mark.asyncio
async def test_help_presents_short_mobile_commands(ctx):
    await handle_text_message(ctx, message("/h"))

    markdown = ctx.bridge.cards[-1][2]["body"]["elements"][0]["content"]
    for command in (
        "/create", "/c", "/proj", "/ls", "/use", "/s", "/info", "/i",
        "/new", "/n", "/open", "/o", "/bind", "/b", "/unbind", "/u", "/pause", "/p",
        "/resume", "/r", "/stop", "/x", "/menu", "/m", "/help", "/h",
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


@pytest.mark.asyncio
async def test_legacy_future_directory_binding_is_opened_on_next_message(
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
    assert selected.workspace_path == str(repository)
    assert ctx.service.get_chat_directory_binding("oc-chat") is None
    assert ctx.orchestrator.scheduled == [(selected.id, "检查这个已有项目")]


@pytest.mark.asyncio
async def test_request_user_input_card_updates_then_resolves(ctx, monkeypatch):
    project = await ctx.service.create_project(
        "site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat"
    )
    approval = await ctx.service.create_approval(
        project.id, None, "ROOT_AGENT_WAITING_USER", "input", "风格？", []
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
        approval_id=approval.id,
    )
    approval.payload = {**approval.payload, "question_interaction_id": interaction.id}
    ctx.service.session.add(approval)
    ctx.service.session.commit()
    monkeypatch.setattr(
        "mac_system.core.root_agent.resolve_root_user_input", lambda *_: False
    )
    selected = await handle_card_action(
        ctx,
        ButtonAction(
            open_id="ou-user",
            approval_id=approval.id,
            action="select_question_option",
            state=approval.state,
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
            approval_id=approval.id,
            action="submit_selected_options",
            state=approval.state,
            decision_id=None,
            option_key=None,
            extras={"interaction_id": interaction.id},
            chat_id="oc-chat",
            message_id="om-card",
        ),
    )
    assert ctx.service.session.get(Approval, approval.id).status == "responded"
    assert ctx.service.session.get(QuestionInteraction, interaction.id).status == "answered"
    assert ctx.orchestrator.scheduled[-1][0] == project.id
    assert ctx.bridge.updates[-1][0] == "om-card"


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
