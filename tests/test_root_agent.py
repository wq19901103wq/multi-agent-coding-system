import asyncio
import subprocess
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import mac_system.core.root_agent as root_agent_module
from mac_system.agents.root_runtime import (
    AgentEvent,
    DynamicTool,
    ToolContext,
    TurnResult,
)
from mac_system.agents.runtime import AgentRuntimeResult, FakeRuntime
from mac_system.config import Settings
from mac_system.core.project_service import ProjectService
from mac_system.core.root_agent import (
    _external_progress_detail,
    RootAgentEngine,
    _ProgressState,
    _codex_role_overrides,
    _external_progress_label,
    _external_tool_summary,
    _native_activity_card,
    _native_child_summary,
    _native_status_card,
    _record_progress_small_step,
    _progress_card_body,
    _provider_selection_guidance,
    _root_profile_key,
    _start_progress_big_step,
    _turn_completion_card,
    resolve_root_user_input,
)
from mac_system.models import (
    AgentSession,
    Approval,
    EventLog,
    Project,
    QuestionInteraction,
    RootAgentSession,
)


@pytest.fixture
def root_context(tmp_path):
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS.clear()
    db = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(db)
    workspace = tmp_path / "project"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    with Session(db) as session:
        project = Project(
            id="proj-root",
            name="个人 Agent",
            task_type="feature",
            description="完善个人 Agent",
            current_state="ACTIVE",
            execution_status="idle",
            workspace_path=str(workspace),
            context={"orchestration_mode": "root_agent"},
        )
        session.add(project)
        session.commit()
        engine = RootAgentEngine(
            session,
            FakeRuntime({}),
            Settings(PROJECTS_DIR=tmp_path),
        )
        yield session, engine
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS.clear()


@pytest.mark.asyncio
async def test_root_uses_native_codex_environment_and_multi_agent(
    root_context, monkeypatch
):
    session, engine = root_context
    captured = {}

    class FakeRoot:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "root-thread"
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, _text):
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-1",
                status="completed",
                final_message="已完成本轮工作",
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run("proj-root", "优化项目")

    assert captured["sandbox"] == "workspace-write"
    assert captured["approval_policy"] == "never"
    assert captured["environments"] is None
    assert captured["codex_home"] is None
    assert [tool.name for tool in captured["tools"]] == ["run_external_agent"]
    assert captured["developer_instructions"] is None
    assert captured["thread_config"] == {
        "model_provider": "mac_openai_https",
        "model_providers.mac_openai_https": {
            "name": "OpenAI HTTPS",
            "wire_api": "responses",
            "requires_openai_auth": True,
            "supports_websockets": False,
        },
        "model_auto_compact_token_limit": 80_000,
        "features.multi_agent_v2": True,
        "features.default_mode_request_user_input": True,
        "sandbox_workspace_write.network_access": True,
        "agents.analyst": {
            "description": (
                "Use for a bounded, independent requirements or architecture "
                "analysis when a separate view adds value. Return findings and "
                "tradeoffs; do not implement the change."
            )
        },
        "agents.reviewer": {
            "description": (
                "Use for independent verification of completed work against the "
                "user's request and observable test evidence. Report actionable "
                "findings; do not reimplement the change."
            )
        },
    }
    root = session.exec(select(RootAgentSession)).one()
    assert root.native_thread_id == "root-thread"
    assert session.get(Project, "proj-root").execution_status == "idle"


@pytest.mark.asyncio
async def test_project_messages_resume_the_same_native_root_thread(
    root_context, monkeypatch
):
    _, engine = root_context
    calls = []

    class FakeRoot:
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "persistent-root"
            calls.append(("create", self.thread_id))
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            calls.append(("resume", thread_id))
            return thread_id

        async def run_turn(self, text):
            calls.append(("turn", text))
            return TurnResult(
                thread_id=self.thread_id,
                turn_id=f"turn-{len(calls)}",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run("proj-root", "第一轮")
    await engine.run("proj-root", "第二轮")

    assert calls == [
        ("create", "persistent-root"),
        ("turn", "第一轮"),
        ("turn", "第二轮"),
    ]


@pytest.mark.asyncio
async def test_explicit_new_thread_replaces_history_without_changing_project(
    root_context, monkeypatch
):
    session, engine = root_context
    created: list[str] = []
    closed: list[str] = []

    class FakeRoot:
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self, *, session_start_source=None):
            self.thread_id = f"thread-{len(created) + 1}"
            created.append((self.thread_id, session_start_source))
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, _text):
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-1",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            closed.append(self.thread_id)

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run("proj-root", "第一轮")
    project_before = session.get(Project, "proj-root")
    workspace_before = project_before.workspace_path

    old_thread, new_thread = await engine.start_new_thread("proj-root")

    assert (old_thread, new_thread) == ("thread-1", None)
    assert created == [("thread-1", None)]
    assert closed == ["thread-1"]
    root = session.exec(select(RootAgentSession)).one()
    assert root.native_thread_id is None
    assert session.get(Project, "proj-root").workspace_path == workspace_before
    event = session.exec(
        select(EventLog).where(
            EventLog.message == "Native Codex thread reset by explicit user command"
        )
    ).one()
    assert event.meta == {
        "old_thread_id": "thread-1",
        "new_thread_id": None,
        "replacement_pending": True,
    }

    await engine.run("proj-root", "新会话第一轮")

    assert created == [("thread-1", None), ("thread-2", "clear")]
    assert session.exec(select(RootAgentSession)).one().native_thread_id == "thread-2"
    assert "root_thread_reset_pending" not in (
        session.get(Project, "proj-root").context or {}
    )


@pytest.mark.asyncio
async def test_explicit_new_thread_rejects_active_turn(root_context):
    session, engine = root_context
    engine.store.set_turn("proj-root", turn_id="turn-active", status="running")

    assert session.get(Project, "proj-root").execution_status == "running"

    with pytest.raises(RuntimeError, match="当前项目仍在处理消息"):
        await engine.start_new_thread("proj-root")

    assert session.exec(select(RootAgentSession)).one().active_turn_id == "turn-active"


@pytest.mark.asyncio
async def test_native_event_projection_skips_stream_deltas_and_compacts_output(
    root_context,
):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "thread-1")

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "item/commandExecution/outputDelta",
            "thread-1",
            "turn-1",
            {"delta": "x" * 20_000},
        ),
    )
    await engine._on_event(
        "proj-root",
        AgentEvent(
            "turn/diff/updated",
            "thread-1",
            "turn-1",
            {"diff": "y" * 20_000},
        ),
    )
    assert list(session.exec(select(EventLog)).all()) == []

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "item/completed",
            "thread-1",
            "turn-1",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "exec-1",
                    "command": "pytest -q",
                    "status": "completed",
                    "aggregatedOutput": "z" * 20_000,
                }
            },
        ),
    )

    event = session.exec(select(EventLog)).one()
    stored_output = event.meta["payload"]["item"]["aggregatedOutput"]
    assert len(stored_output) <= 4_000
    assert "output compacted" in stored_output


def test_native_activity_cards_cover_official_item_lifecycle_without_fake_completion():
    assert _native_activity_card(
        "item/started",
        {"type": "commandExecution", "status": "inProgress"},
    ) == ("正在检查或验证项目", "purple", "running")
    assert _native_activity_card(
        "item/completed",
        {"type": "fileChange", "status": "completed"},
    ) == ("项目文件已更新，Codex 继续处理", "green", "running")
    assert _native_activity_card(
        "item/completed",
        {"type": "dynamicToolCall", "status": "failed"},
    ) == ("外部编码能力未完成，Codex 正在分析", "orange", "running")
    assert _native_activity_card(
        "item/completed",
        {"type": "agentMessage", "text": "已经定位问题，正在补回归。"},
    ) == ("已经定位问题，正在补回归。", "blue", "running")
    assert _native_activity_card(
        "item/started",
        {"type": "agentMessage", "text": ""},
    ) is None
    assert _native_activity_card(
        "item/completed",
        {
            "type": "reasoning",
            "summary": ["先定位协议边界，再补回归测试。"],
            "content": ["不应展示的内部推理"],
        },
    ) == ("Codex 已完成一轮分析，正在继续处理", "blue", "running")


def test_native_activity_detail_shows_tool_identity_without_arguments_or_output():
    from mac_system.core.root_agent import _native_activity_detail

    assert _native_activity_detail(
        {
            "type": "mcpToolCall",
            "server": "browser",
            "tool": "click",
            "arguments": {"secret": "do-not-show"},
        }
    ) == "工具：browser/click"
    detail = _native_activity_detail(
        {
            "type": "commandExecution",
            "cwd": "/workspace/site",
            "command": "curl -H 'Authorization: secret' example.com",
            "aggregatedOutput": "secret output",
        }
    )
    assert detail == "工具：终端命令；目录：site"
    assert "secret" not in detail


def test_native_retry_error_updates_progress_without_ending_turn():
    assert _native_status_card("turn/started", {}) == (
        "Codex 已开始处理本轮消息。",
        "purple",
        "running",
    )
    assert _native_status_card(
        "error",
        {
            "error": {"message": "Reconnecting... 2/5"},
            "willRetry": True,
        },
    ) == (
        "与 Codex 的连接暂时中断，正在自动重连。",
        "orange",
        "running",
    )
    assert _native_status_card("warning", {"message": "notice"}) is None


def test_progress_card_reports_elapsed_and_stale_wait_without_fake_activity():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=115.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="正在检查或验证项目",
        narrative="已定位到 SSE 完成事件没有被前端识别，正在补回归测试。",
        child_summary="运行中 2，已完成 1",
    )
    _start_progress_big_step(state, "定位消息完成状态")
    _record_progress_small_step(
        state,
        key="inspect-sse",
        label="检查 SSE 完成事件",
        status="completed",
        detail="已确认前端没有识别完成事件",
    )
    _start_progress_big_step(state, "补充回归测试")
    _record_progress_small_step(
        state,
        key="run-tests",
        label="正在运行相关测试",
        status="running",
    )

    fresh = _progress_card_body(state, now_monotonic=125.0)
    assert "已用时间**：25 秒" in fresh
    assert "最近事件" in fresh
    assert "10 秒前" in fresh
    assert "子 Agent**：运行中 2，已完成 1" in fresh
    assert "大步骤**" in fresh
    assert "✅ 定位消息完成状态" in fresh
    assert "▶️ 补充回归测试" in fresh
    assert "当前大步骤 · 小步骤**" in fresh
    assert "正在运行相关测试" in fresh
    assert "等待模型或工具返回" not in fresh

    stale = _progress_card_body(state, now_monotonic=150.0)
    assert "已用时间**：50 秒" in stale
    assert "35 秒前" in stale
    assert "暂未收到新的 Codex 事件" in stale


def test_progress_card_marks_collapsed_history_instead_of_silently_dropping_it():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=100.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="正在处理",
    )
    for index in range(10):
        _start_progress_big_step(state, f"大步骤 {index}")
    for index in range(9):
        _record_progress_small_step(
            state,
            key=f"small-{index}",
            label=f"小步骤 {index}",
            status="completed",
        )

    body = _progress_card_body(state, now_monotonic=101.0)

    assert "更早 2 个大步骤已收起" in body
    assert "更早 2 个小步骤已收起" in body
    assert "大步骤 0" not in body
    assert "小步骤 0" not in body


def test_progress_big_step_survives_later_tool_activity(root_context):
    _session, engine = root_context
    engine._update_progress("proj-root", label="正在检查或验证项目")
    state = engine._progress["proj-root"]
    _start_progress_big_step(state, "已定位共同根因，正在补测试。")
    _record_progress_small_step(
        state,
        key="exec-1",
        label="正在检查或验证项目",
        status="running",
        detail="工具：终端命令；目录：site",
    )

    body = _progress_card_body(state)
    assert "▶️ 已定位共同根因，正在补测试。" in body
    assert "工具：终端命令；目录：site" in body


@pytest.mark.asyncio
async def test_native_commentary_remains_visible_during_following_tool(root_context):
    _session, engine = root_context
    await engine._project_native_activity(
        "proj-root",
        AgentEvent(
            "item/completed",
            "thread-1",
            "turn-1",
            {
                "item": {
                    "type": "agentMessage",
                    "id": "commentary-1",
                    "phase": "commentary",
                    "text": "已定位共同根因，正在补齐连续两轮测试。",
                }
            },
        ),
    )
    await engine._project_native_activity(
        "proj-root",
        AgentEvent(
            "item/started",
            "thread-1",
            "turn-1",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "exec-1",
                    "status": "inProgress",
                    "cwd": "/workspace/site",
                }
            },
        ),
    )
    await engine._flush_progress_snapshots("proj-root")

    state = engine._progress["proj-root"]
    assert state.label == "正在检查或验证项目"
    assert state.narrative == "已定位共同根因，正在补齐连续两轮测试。"
    assert state.detail == "工具：终端命令；目录：site"
    assert state.big_steps[-1].title == "已定位共同根因，正在补齐连续两轮测试。"
    assert state.big_steps[-1].small_steps[-1].detail == "工具：终端命令；目录：site"


@pytest.mark.asyncio
async def test_progress_delivery_is_non_blocking_coalesced_and_flushable(
    root_context, monkeypatch
):
    _session, engine = root_context
    engine._progress_min_delivery_interval = 0
    gate = asyncio.Event()
    delivered = []

    async def slow_delivery(project_id, *, event_identity):
        delivered.append((project_id, event_identity))
        await gate.wait()

    monkeypatch.setattr(engine, "_publish_progress_snapshot", slow_delivery)

    engine._queue_progress_snapshot("proj-root", event_identity="first")
    await asyncio.sleep(0)
    engine._queue_progress_snapshot("proj-root", event_identity="second")
    engine._queue_progress_snapshot("proj-root", event_identity="latest")

    assert delivered == [("proj-root", "first")]
    assert engine._pending_progress_delivery == {"proj-root": "latest"}

    gate.set()
    await engine._flush_progress_snapshots("proj-root")

    assert delivered == [
        ("proj-root", "first"),
        ("proj-root", "latest"),
    ]
    assert engine._pending_progress_delivery == {}
    assert engine._progress_delivery_tasks == {}


def test_native_child_summary_uses_official_states_without_thread_ids():
    summary = _native_child_summary(
        {
            "agentsStates": {
                "thread-secret-1": {"status": "running", "message": "work"},
                "thread-secret-2": {"status": "completed"},
                "thread-secret-3": {"status": "running"},
            }
        }
    )

    assert summary == "运行中 2，已完成 1"
    assert "thread-secret" not in summary


def test_native_turn_terminal_status_closes_shared_progress_projection():
    assert _turn_completion_card("completed") == (
        "本轮处理完成，项目可继续迭代。",
        "green",
        "completed",
    )
    assert _turn_completion_card("interrupted") == (
        "本轮已中止，项目可继续发送新消息。",
        "orange",
        "interrupted",
    )
    assert _turn_completion_card("failed") == (
        "本轮未完成，项目可直接重试。",
        "red",
        "failed",
    )


@pytest.mark.asyncio
async def test_interrupted_turn_does_not_publish_partial_commentary_as_final(
    root_context,
):
    session, engine = root_context
    project = session.get(Project, "proj-root")

    await engine._publish_final(
        project,
        TurnResult(
            thread_id="thread-1",
            turn_id="turn-interrupted",
            status="interrupted",
            final_message="我准备执行下一步。",
            events=(),
        ),
    )

    from mac_system.models import NotificationOutbox

    assert session.exec(select(NotificationOutbox)).all() == []


@pytest.mark.asyncio
async def test_event_projection_failure_does_not_abort_native_turn(
    root_context, monkeypatch
):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "thread-1")

    def fail_projection(*_args, **_kwargs):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("UPDATE root_agent_sessions", {}, Exception("locked"))

    monkeypatch.setattr(
        "mac_system.core.root_agent.RootSessionStore.append_event",
        fail_projection,
    )

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "item/completed",
            "thread-1",
            "turn-1",
            {"item": {"type": "agentMessage", "id": "message-1", "text": "done"}},
        ),
    )

    assert list(session.exec(select(EventLog)).all()) == []


@pytest.mark.asyncio
async def test_failed_turn_releases_transport_and_resumes_same_thread(
    root_context, monkeypatch
):
    session, engine = root_context
    calls = []

    class FakeRoot:
        attempts = 0

        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "durable-thread"
            calls.append(("create", self.thread_id))
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            calls.append(("resume", thread_id))
            return thread_id

        async def run_turn(self, text):
            type(self).attempts += 1
            calls.append(("turn", text))
            if type(self).attempts == 1:
                raise RuntimeError("provider transport failed")
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-2",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            calls.append(("close", self.thread_id))

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    with pytest.raises(RuntimeError, match="provider transport failed"):
        await engine.run("proj-root", "第一轮")

    session.expire_all()
    root = session.exec(select(RootAgentSession)).one()
    assert root.native_thread_id == "durable-thread"
    assert root.status == "idle"
    assert root.active_turn_id is None
    assert root.last_error == "provider transport failed"
    assert session.get(Project, "proj-root").execution_status == "idle"
    assert await engine.steer("proj-root", "不会发给已结束的进程") is False

    await engine.run("proj-root", "第二轮")

    assert ("resume", "durable-thread") in calls
    assert calls.count(("close", "durable-thread")) == 1


@pytest.mark.asyncio
async def test_native_request_user_input_waits_for_durable_feishu_answer(root_context):
    session, engine = root_context
    request_task = asyncio.create_task(
        engine._request_user_input(
            "proj-root",
            {
                "itemId": "request-1",
                "questions": [
                    {
                        "id": "style",
                        "header": "界面风格",
                        "question": "你更喜欢哪种风格？",
                        "isOther": True,
                        "isSecret": False,
                        "options": [
                            {"label": "现代简洁", "description": "减少装饰"},
                            {"label": "未来科技", "description": "更强视觉效果"},
                        ],
                    }
                ],
            },
        )
    )

    for _ in range(50):
        await asyncio.sleep(0.01)
        interaction = session.exec(select(QuestionInteraction)).first()
        if interaction is not None:
            break
    else:
        raise AssertionError("request_user_input was not persisted")

    root = session.exec(select(RootAgentSession)).one()
    assert root.status == "waiting_user"
    assert session.get(Project, "proj-root").execution_status == "waiting_user"

    service = ProjectService(session)
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "未来科技",
        "未来科技",
        False,
        "ou-user",
    )
    await service.resolve_question_interaction(interaction.id)

    assert resolve_root_user_input(service, interaction.id) is True
    session.refresh(root)
    assert root.status == "running"
    response = await asyncio.wait_for(request_task, timeout=1)
    assert response == {"answers": {"style": {"answers": ["未来科技"]}}}
    assert interaction.request_item_id == "request-1"
    assert interaction.approval_id is None
    assert session.exec(select(Approval)).first() is None


@pytest.mark.asyncio
async def test_native_nonblocking_question_auto_resolves(root_context):
    session, engine = root_context
    response = await engine._request_user_input(
        "proj-root",
        {
            "itemId": "request-auto",
            "autoResolutionMs": 10,
            "questions": [
                {
                    "id": "accent",
                    "header": "强调色",
                    "question": "偏好哪个强调色？",
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {"label": "蓝色", "description": "冷色"},
                        {"label": "橙色", "description": "暖色"},
                    ],
                }
            ],
        },
    )

    assert response == {"answers": {}}
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.status == "answered"
    assert interaction.resolution_source == "auto_resolution"
    assert session.get(Project, "proj-root").execution_status == "running"
    assert session.exec(select(RootAgentSession)).one().status == "running"


@pytest.mark.asyncio
async def test_question_card_delivery_failure_does_not_answer_codex_with_empty_data(
    root_context,
):
    session, engine = root_context

    class BrokenBridge:
        async def _send_interactive(self, *_args, **_kwargs):
            raise RuntimeError("card transport unavailable")

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = BrokenBridge()

    request_task = asyncio.create_task(
        engine._request_user_input(
            "proj-root",
            {
                "itemId": "request-card-failed",
                "questions": [
                    {
                        "id": "color",
                        "header": "颜色",
                        "question": "选择颜色",
                        "isOther": False,
                        "isSecret": False,
                        "options": [
                            {"label": "blue", "description": "蓝色"},
                            {"label": "green", "description": "绿色"},
                        ],
                    }
                ],
            },
        )
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        interaction = session.exec(select(QuestionInteraction)).first()
        if interaction is not None:
            break
    else:
        raise AssertionError("request_user_input was not persisted")
    assert request_task.done() is False

    service = ProjectService(session)
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id, "green", "green", False, "ou-user"
    )
    await service.resolve_question_interaction(interaction.id)
    assert resolve_root_user_input(service, interaction.id) is True
    assert await asyncio.wait_for(request_task, timeout=1) == {
        "answers": {"color": {"answers": ["green"]}}
    }


def test_extension_tool_list_does_not_reimplement_codex_multi_agent(root_context):
    _, engine = root_context
    project = engine.service.require_project("proj-root")
    names = [tool.name for tool in engine._extension_tools(project)]
    assert names == ["run_external_agent"]
    assert not ({"spawn_agent", "send_message", "wait", "reviewer"} & set(names))


def test_external_tool_exposes_provider_evidence_without_routing_rules(root_context):
    _, engine = root_context
    project = engine.service.require_project("proj-root")
    tool = engine._extension_tools(project)[0]

    assert "Select a provider deliberately" in tool.description
    assert "FakeRuntime" in tool.description
    assert "no recent execution samples" in tool.description
    assert "selection_reason" in tool.input_schema["required"]
    assert "timeout_seconds" in tool.input_schema["required"]
    reason_description = tool.input_schema["properties"]["selection_reason"][
        "description"
    ]
    assert "same language as the user's request" in reason_description
    assert "avoid internal routing or protocol jargon" in reason_description
    assert (
        tool.input_schema["properties"]["timeout_seconds"]["maximum"]
        == engine.settings.agent_timeout_seconds
    )


def test_provider_guidance_reports_only_aggregate_process_metrics():
    guidance = _provider_selection_guidance(
        {
            "codex": {
                "description": "Independent execution.",
                "mode": "headless_cli",
            }
        },
        {
            "codex": {
                "sample_count": 4,
                "completed_count": 3,
                "average_duration_seconds": 12.5,
                "average_tool_calls": 2.5,
                "average_cost_usd": 0.08,
            }
        },
    )

    assert "recent CLI completion 3/4" in guidance
    assert "average duration 12.5s" in guidance
    assert "average tool calls 2.5" in guidance
    assert "average reported cost $0.08" in guidance
    assert "headless_cli" in guidance


def test_codex_roles_are_operator_config_and_use_official_fields_only():
    overrides = _codex_role_overrides(
        {
            "codex_agent_roles": {
                "analyst": {
                    "enabled": True,
                    "description": "Analyze only.",
                    "nickname_candidates": ["Ada"],
                    "custom_pipeline": "forbidden",
                },
                "disabled": {
                    "enabled": False,
                    "description": "Do not expose.",
                },
            }
        }
    )

    assert overrides == {
        "agents.analyst": {
            "description": "Analyze only.",
            "nickname_candidates": ["Ada"],
        }
    }


def test_persisted_root_profile_changes_when_dynamic_tool_contract_changes():
    base = DynamicTool(
        name="bridge",
        description="old description",
        input_schema={
            "type": "object",
            "required": ["provider"],
            "properties": {"provider": {"type": "string"}},
        },
        handler=lambda _arguments, _context: {},
    )
    description_only = DynamicTool(
        name="bridge",
        description="new description and metrics",
        input_schema=base.input_schema,
        handler=base.handler,
    )
    changed_schema = DynamicTool(
        name="bridge",
        description=base.description,
        input_schema={
            **base.input_schema,
            "required": ["provider", "timeout_seconds"],
        },
        handler=base.handler,
    )

    assert _root_profile_key([base]) == _root_profile_key([description_only])
    assert _root_profile_key([base]) != _root_profile_key([changed_schema])


def test_external_progress_is_user_facing_and_does_not_expose_tool_input():
    detail = '{"name":"Read","input":{"file_path":"/secret/path"}}'
    label = _external_progress_label("codex", "tool", detail)
    assert label == "Codex 正在查看项目资料"
    assert "/secret/path" not in label
    assert _external_progress_detail("tool", detail) == "工具：Read"

    assert _external_progress_detail(
        "thinking", "不应展示的内部推理；token=sk-test-secret"
    ) is None
    assert _external_progress_label(
        "pi", "commentary", "正在核对协议"
    ) == "Pi 已汇报阶段进展，正在继续处理"


def test_external_tool_summary_excludes_inputs_and_outputs():
    summary = _external_tool_summary(
        [
            {"name": "Read", "input": {"file_path": "/secret"}},
            {"name": "Read", "output": "large output"},
            {"tool": "Bash", "input": {"command": "secret"}},
        ]
    )
    assert summary == {"count": 3, "names": ["Read", "Bash"]}
    assert "secret" not in str(summary)


@pytest.mark.asyncio
async def test_external_provider_keeps_public_text_with_tool_status(root_context):
    _session, engine = root_context
    await engine._project_external_agent_activity(
        "proj-root",
        "agent-1",
        "pi",
        "需要宿主自动化完成部署",
        "Pi 正在执行检查或测试",
        observed_detail="工具：bash",
        narrative="服务已重启，正在核对健康检查；token=sk-test-secret",
    )
    await engine._flush_progress_snapshots("proj-root")

    state = engine._progress["proj-root"]
    assert state.label == "Pi 正在执行检查或测试"
    assert state.narrative.startswith("服务已重启，正在核对健康检查")
    assert "sk-test-secret" not in state.narrative
    assert "选择原因：需要宿主自动化完成部署" in state.detail
    assert "工具：bash" in state.detail
    body = _progress_card_body(state)
    assert "当前执行主体**：Pi" in body
    assert "模型**：后端未报告" in body
    assert "选择原因**：需要宿主自动化完成部署" in body
    assert "交给 Pi 独立处理" in body
    assert "服务已重启，正在核对健康检查" in body
    assert len(state.big_steps) == 1
    assert body.count("需要宿主自动化完成部署") == 1


@pytest.mark.asyncio
async def test_external_provider_is_one_explicit_tool_call(root_context):
    session, engine = root_context
    project = engine.service.require_project("proj-root")
    tool = engine._extension_tools(project)[0]
    result = await tool.handler(
        {
            "provider": "FakeRuntime",
            "selection_reason": "用于独立检查界面",
            "task_name": "ui-check",
            "message": "检查界面",
            "access": "read_only",
            "timeout_seconds": 60,
        },
        ToolContext("thread-1", "turn-1", "call-1", tool.name),
    )

    assert result["success"] is True
    assert result["provider"] == "FakeRuntime"
    agent = session.exec(select(AgentSession)).one()
    assert agent.status == "completed"
    assert agent.objective == "检查界面"
    assert agent.result["selection_reason"] == "用于独立检查界面"
    assert agent.result["timeout_seconds"] == 60
    assert "tool_calls" not in result
    assert result["tool_activity"] == {"count": 0, "names": []}
    updated_tool = engine._extension_tools(project)[0]
    assert "recent CLI completion 1/1" in updated_tool.description


@pytest.mark.asyncio
async def test_external_provider_returns_failure_and_compact_activity_to_root(
    root_context,
):
    session, engine = root_context

    class RecordingRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({})
            self.timeout_seconds = None

        async def execute(self, *args, **kwargs):
            self.timeout_seconds = kwargs["timeout_seconds"]
            return AgentRuntimeResult(
                success=False,
                output_json=None,
                output_files=[],
                logs="provider failed with a long diagnostic",
                cost_info=None,
                tool_calls=[
                    {"name": "Read", "input": {"file_path": "/secret/path"}}
                ],
                failure={"message": "provider unavailable"},
            )

    runtime = RecordingRuntime()
    engine.runtime = runtime
    project = engine.service.require_project("proj-root")
    tool = engine._extension_tools(project)[0]
    result = await tool.handler(
        {
            "provider": "RecordingRuntime",
            "selection_reason": "独立执行",
            "task_name": "bounded-check",
            "message": "检查项目",
            "access": "read_only",
            "timeout_seconds": 37,
        },
        ToolContext("thread-1", "turn-1", "call-failure", tool.name),
    )

    assert runtime.timeout_seconds == 37
    assert result["success"] is False
    assert "provider unavailable" in result["error"]
    assert result["tool_activity"] == {"count": 1, "names": ["Read"]}
    assert "/secret/path" not in str(result)
    agent = session.exec(select(AgentSession)).one()
    assert agent.result["tool_calls"][0]["input"]["file_path"] == "/secret/path"
