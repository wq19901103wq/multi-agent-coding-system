import asyncio
import subprocess

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.agents.root_runtime import AgentEvent, ToolContext, TurnResult
from mac_system.agents.runtime import FakeRuntime
from mac_system.config import Settings
from mac_system.core.project_service import ProjectService
from mac_system.core.root_agent import (
    RootAgentEngine,
    _external_progress_label,
    _native_activity_card,
    _native_status_card,
    _provider_selection_guidance,
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
        "features.multi_agent_v2": True,
        "features.default_mode_request_user_input": True,
        "sandbox_workspace_write.network_access": True,
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
        ("resume", "persistent-root"),
        ("turn", "第二轮"),
    ]


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
        {"type": "reasoning", "text": "internal"},
    ) is None


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
    assert calls.count(("close", "durable-thread")) == 2


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
    approval = session.get(Approval, interaction.approval_id)
    assert approval.payload["root_request_item_id"] == "request-1"


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
                "average_cost_usd": 0.08,
            }
        },
    )

    assert "recent CLI completion 3/4" in guidance
    assert "average duration 12.5s" in guidance
    assert "average reported cost $0.08" in guidance
    assert "headless_cli" in guidance


def test_external_progress_is_user_facing_and_does_not_expose_tool_input():
    detail = '{"name":"Read","input":{"file_path":"/secret/path"}}'
    label = _external_progress_label("codex", "tool", detail)
    assert label == "Codex 正在查看项目资料"
    assert "/secret/path" not in label


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
        },
        ToolContext("thread-1", "turn-1", "call-1", tool.name),
    )

    assert result["success"] is True
    assert result["provider"] == "FakeRuntime"
    agent = session.exec(select(AgentSession)).one()
    assert agent.status == "completed"
    assert agent.objective == "检查界面"
    assert agent.result["selection_reason"] == "用于独立检查界面"
    updated_tool = engine._extension_tools(project)[0]
    assert "recent CLI completion 1/1" in updated_tool.description
