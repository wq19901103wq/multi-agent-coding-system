import asyncio
from unittest.mock import AsyncMock

import pytest

import mac_system.core.root_agent as root
from mac_system.agents.root_runtime import CodexAppServerThread, AppServerProtocolError
from mac_system.feishu.handlers import handle_text_message, handle_card_action, _handle_command
from mac_system.feishu.models import ButtonAction
from tests.test_feishu import ctx, message
from tests.test_root_agent import root_context


def choices(card):
    return [item["behaviors"][0]["value"] for item in card["body"]["elements"] if item["tag"] == "button"]


def click(value, actor="ou-user"):
    return ButtonAction(open_id=actor, approval_id="", action=value["action"], state="",
                        decision_id=None, option_key=None, extras=value,
                        chat_id=value["chat_id"], chat_type=value["chat_type"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_picker_rotates_progress_after_catalog_await(ctx, failure):
    await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    events = []

    async def catalog(project_id):
        events.append("catalog")
        if failure:
            raise RuntimeError("unavailable")
        return [{"model": "model-one"}]

    async def rotate(project_id):
        events.append("rotate")

    ctx.orchestrator.list_root_models = catalog
    ctx.orchestrator.rotate_root_progress_for_timeline_event = rotate
    await handle_text_message(ctx, message("/model"))
    assert events[-2:] == ["catalog", "rotate"]


@pytest.mark.asyncio
async def test_picker_click_uses_existing_model_switch(ctx):
    project = await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    await handle_text_message(ctx, message("/model"))
    card = ctx.bridge.cards[-1][2]
    assert "Catalog description" in str(card)
    assert "不携带旧对话上下文" in str(card)
    options = choices(card)
    await handle_card_action(ctx, click(options[1]))
    assert ctx.orchestrator.models_set == [(project.id, "gpt-6-astra")]
    await handle_card_action(ctx, click(options[-1]))
    assert ctx.orchestrator.models_set[-1] == (project.id, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["project", "actor", "model"])
async def test_picker_rejects_stale_or_other_user_card(ctx, change):
    project = await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    await handle_text_message(ctx, message("/model"))
    value = choices(ctx.bridge.cards[-1][2])[1]
    actor = "ou-user"
    if change == "project":
        await ctx.service.create_project("other", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    elif change == "actor":
        actor = "other-user"
    else:
        project.context = {**project.context, "lead_model": "another-model"}
        ctx.service.session.add(project)
        ctx.service.session.commit()
    await handle_card_action(ctx, click(value, actor))
    assert not ctx.orchestrator.models_set
    assert "已失效" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["select_lead_model", "list_lead_models"])
async def test_switching_project_during_card_rotation_cannot_change_other_project(ctx, action_type):
    await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    ctx.orchestrator.list_root_models = AsyncMock(return_value=[{"model": f"model-{i}"} for i in range(13)])
    await handle_text_message(ctx, message("/model"))
    value = choices(ctx.bridge.cards[-1][2])[1 if action_type == "select_lead_model" else -1]

    async def rotate(project_id):
        await ctx.service.create_project("other", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")

    ctx.orchestrator.rotate_root_progress_for_timeline_event = rotate
    await handle_card_action(ctx, click(value))
    assert not ctx.orchestrator.models_set
    ctx.orchestrator.list_root_models.assert_awaited_once()
    assert "已失效" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_rejected_bound_model_command_preserves_pending_intent(ctx):
    await ctx.service.create_project("other", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    ctx.chat_id = "oc-chat"
    ctx.service.upsert_pending_intent("ou-user", None, "awaiting_description", chat_id="oc-chat")
    await _handle_command(ctx, message("/model model-one"), "/model model-one", expected_project_id="old-project")
    assert ctx.service.get_pending_intent("ou-user", "oc-chat") is not None
    assert not ctx.orchestrator.models_set
    assert "已失效" in str(ctx.bridge.cards[-1][2])


@pytest.mark.asyncio
async def test_picker_pages_without_changing_model(ctx):
    await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    ctx.orchestrator.list_root_models = AsyncMock(return_value=[{"model": f"model-{i}"} for i in range(13)])
    await handle_text_message(ctx, message("/model"))
    assert "当前默认模型未出现在本次目录中" in str(ctx.bridge.cards[-1][2])
    await handle_card_action(ctx, click(choices(ctx.bridge.cards[-1][2])[-1]))
    card = ctx.bridge.cards[-1][2]
    assert "第 2/2 页" in str(card)
    assert choices(card)[0]["model"] == "model-10"
    assert not ctx.orchestrator.models_set


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_picker_empty_or_failed_catalog_has_no_fake_options(ctx, failure):
    await ctx.service.create_project("picker", "feature", "test", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    ctx.orchestrator.list_root_models = AsyncMock(side_effect=RuntimeError("catalog unavailable")) if failure else AsyncMock(return_value=[])
    await handle_text_message(ctx, message("/model"))
    assert not choices(ctx.bridge.cards[-1][2])
    assert not ctx.orchestrator.models_set


@pytest.mark.asyncio
async def test_native_model_catalog_paginates_and_filters_hidden(tmp_path):
    runtime = CodexAppServerThread(codex_bin="unused", cwd=tmp_path, tools=[])
    runtime.start = AsyncMock()
    runtime._request = AsyncMock(side_effect=[
        {"data": [{"model": "first"}, {"model": "secret", "hidden": True}], "nextCursor": "cursor-2"},
        {"data": [{"model": "first"}, {"model": "second"}], "nextCursor": None},
    ])
    assert [item["model"] for item in await runtime.list_models()] == ["first", "second"]
    assert runtime._request.call_args.args == ("model/list", {"cursor": "cursor-2", "limit": 100, "includeHidden": False})
    assert runtime.thread_id is None
    runtime._request = AsyncMock(return_value={"data": [], "nextCursor": "loop"})
    with pytest.raises(AppServerProtocolError, match="分页异常"):
        await runtime.list_models()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_engine_catalog_always_closes_connection(root_context, monkeypatch, failure):
    session, engine = root_context
    catalog = AsyncMock()
    catalog.list_models.side_effect = RuntimeError("failed") if failure else None
    catalog.list_models.return_value = [{"model": "real-model"}]
    monkeypatch.setattr(root, "CodexAppServerThread", lambda **kw: catalog)
    if failure:
        with pytest.raises(RuntimeError, match="failed"):
            await engine.list_lead_models("proj-root")
    else:
        assert await engine.list_lead_models("proj-root") == [{"model": "real-model"}]
    catalog.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_model_picker_rejects_other_provider(root_context):
    session, engine = root_context
    engine.store.ensure("proj-root", provider="kimi_code")
    with pytest.raises(RuntimeError, match="仅支持 Codex"):
        await engine.list_lead_models("proj-root")


@pytest.mark.asyncio
async def test_catalog_timeout_cancels_query_and_closes(root_context, monkeypatch):
    session, engine = root_context
    cancelled = asyncio.Event()

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    catalog = AsyncMock()
    catalog.list_models.side_effect = blocked
    monkeypatch.setattr(root, "CodexAppServerThread", lambda **kw: catalog)
    original_wait_for = asyncio.wait_for

    async def short_wait(awaitable, timeout):
        assert timeout == 25
        return await original_wait_for(awaitable, 0.01)

    monkeypatch.setattr(root.asyncio, "wait_for", short_wait)
    with pytest.raises(asyncio.TimeoutError):
        await engine.list_lead_models("proj-root")
    assert cancelled.is_set()
    catalog.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_model_switches_are_serialized(root_context):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "old-thread")
    closing = asyncio.Event()
    release = asyncio.Event()

    async def close():
        closing.set()
        await release.wait()

    runtime = AsyncMock()
    runtime.close.side_effect = close
    root._ROOT_THREADS["proj-root"] = root._RootHandle(runtime, asyncio.get_running_loop(), "codex")
    first = asyncio.create_task(engine.set_lead_model("proj-root", "model-first"))
    await asyncio.wait_for(closing.wait(), 1)
    second = asyncio.create_task(engine.set_lead_model("proj-root", "model-second"))
    release.set()
    assert await asyncio.wait_for(asyncio.gather(first, second), 2) == [
        (None, "model-first"), ("model-first", "model-second")]
    assert engine.service.require_project("proj-root").context["lead_model"] == "model-second"
    assert engine.store.ensure("proj-root").native_thread_id is None
    runtime.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_while_busy_does_not_wait_for_execution_lock(root_context):
    session, engine = root_context
    engine.store.set_turn("proj-root", turn_id="active", status="running")
    lock = await root.get_default_lock_manager().acquire("proj-root")
    async with lock:
        with pytest.raises(RuntimeError, match="仍在处理"):
            await asyncio.wait_for(engine.set_lead_model("proj-root", "different"), 0.2)
