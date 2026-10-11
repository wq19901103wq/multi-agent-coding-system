import asyncio
import importlib.util
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import mac_system.core.root_agent as root_agent_module
from mac_system.agents.root_runtime import (
    AgentEvent,
    AppServerProtocolError,
    DynamicTool,
    ProjectLeadRuntimeRegistry,
    ToolContext,
    TurnResult,
)
from mac_system.agents.runtime import AgentRuntimeResult, FakeRuntime
from mac_system.config import Settings
from mac_system.core.notification_service import deliver_card
from mac_system.core.project_service import ProjectService
from mac_system.core.project_owner import ProjectOwnerResult
from mac_system.core.root_agent import (
    build_restart_recovery_trigger,
    _external_progress_detail,
    RootAgentEngine,
    _ProgressState,
    _TurnTiming,
    _codex_role_overrides,
    _external_progress_label,
    _external_tool_summary,
    _native_activity_card,
    _native_activity_detail,
    _native_child_summary,
    _native_approval_spec,
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
    NotificationOutbox,
    Project,
    ProjectDecision,
    ProjectPolicy,
    ProjectOwnerReview,
    ProjectOwnerState,
    ProjectHistoryRound,
    ProjectUser,
    QuestionAnswer,
    QuestionInteraction,
    RootAgentSession,
)
from mac_system.feishu.card import build_content_card
from mac_system.utils.usage import canonical_usage


class _FakeLeadBindings:
    def update_bindings(
        self, *, tools, event_handler, server_request_handler
    ) -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.event_handler = event_handler
        self.server_request_handler = server_request_handler


def test_native_approval_specs_follow_codex_v2_response_shapes():
    command_questions, command_response = _native_approval_spec(
        "item/commandExecution/requestApproval",
        {
            "command": "git add -A",
            "cwd": "/workspace",
            "availableDecisions": ["accept", "acceptForSession", "decline"],
        },
    )
    assert "git add -A" in command_questions[0]["question"]
    assert command_response({"approval_decision": "允许本次"}) == {
        "decision": "accept"
    }
    assert command_response({"approval_decision": "本会话允许"}) == {
        "decision": "acceptForSession"
    }
    with pytest.raises(ValueError, match="native option"):
        command_response({"approval_decision": "不存在的选择"})

    execpolicy_decision = {
        "acceptWithExecpolicyAmendment": {
            "execpolicy_amendment": ["python", "-m", "pytest"],
        }
    }
    policy_questions, policy_response = _native_approval_spec(
        "item/commandExecution/requestApproval",
        {
            "command": "python -m pytest -q",
            "cwd": "/workspace",
            "availableDecisions": [
                "accept",
                execpolicy_decision,
                "cancel",
            ],
        },
    )
    policy_options = policy_questions[0]["options"]
    assert [option["label"] for option in policy_options] == [
        "允许本次",
        "允许规则：python -m pytest",
        "拒绝并停止",
    ]
    assert "python -m pytest" in policy_options[1]["description"]
    assert policy_response(
        {"approval_decision": "允许规则：python -m pytest"}
    ) == {"decision": execpolicy_decision}

    network_decision = {
        "applyNetworkPolicyAmendment": {
            "network_policy_amendment": {
                "host": "packages.example.test",
                "action": "allow",
            }
        }
    }
    network_questions, network_response = _native_approval_spec(
        "item/commandExecution/requestApproval",
        {
            "command": "download dependencies",
            "cwd": "/workspace",
            "availableDecisions": ["accept", network_decision, "cancel"],
        },
    )
    network_label = network_questions[0]["options"][1]["label"]
    assert network_label == "本会话允许访问 packages.example.test"
    assert network_response({"approval_decision": network_label}) == {
        "decision": network_decision
    }

    permission_questions, permission_response = _native_approval_spec(
        "item/permissions/requestApproval",
        {
            "cwd": "/workspace",
            "permissions": {"fileSystem": {"write": ["/workspace/.git"]}},
        },
    )
    assert permission_questions[0]["question"].startswith("是否授予")
    assert permission_response({"approval_decision": "允许本轮"}) == {
        "permissions": {"fileSystem": {"write": ["/workspace/.git"]}},
        "scope": "turn",
    }
    assert permission_response({"approval_decision": "拒绝"}) == {
        "permissions": {},
        "scope": "turn",
    }
    with pytest.raises(ValueError, match="native option"):
        permission_response({"approval_decision": "不存在的选择"})


def test_file_approval_shows_operations_and_move_destination_without_diff():
    questions, response = _native_approval_spec(
        "item/fileChange/requestApproval",
        {"reason": None, "grantRoot": "/workspace"},
        file_changes=[
            {"path": "/workspace/new.txt", "kind": {"type": "add"}},
            {"path": "/workspace/old.txt", "kind": {"type": "delete"}},
            {
                "path": "/workspace/before.txt",
                "kind": {"type": "update", "move_path": "/workspace/after.txt"},
                "diff": "PRIVATE_SOURCE_CONTENT",
            },
        ],
    )
    reason = questions[0]["reason"]
    assert "新增：/workspace/new.txt" in reason
    assert "删除：/workspace/old.txt" in reason
    assert "修改：/workspace/before.txt → /workspace/after.txt" in reason
    assert "范围：/workspace" in reason
    assert "PRIVATE_SOURCE_CONTENT" not in str(questions)
    assert response({"approval_decision": "本会话允许"}) == {
        "decision": "acceptForSession"
    }


@pytest.mark.parametrize("mismatch", ["project", "thread", "turn", "item", "type", "event"])
def test_file_approval_scope_uses_exact_native_identity(root_context, mismatch):
    session, engine = root_context
    params = {"threadId": "child-thread", "turnId": "turn-1", "itemId": "patch-1"}
    meta = {
        "profile_type": "root_agent_event",
        "kind": "item/started",
        "thread_id": "child-thread",
        "turn_id": "turn-1",
        "payload": {"item": {
            "id": "patch-1", "type": "fileChange",
            "changes": [{"path": "/tmp/correct.patch", "kind": {"type": "add"},
                         "diff": "PRIVATE_SOURCE_CONTENT"}],
        }},
    }
    session.add(EventLog(project_id="proj-root", level="INFO", message="native item", meta=meta))
    session.commit()
    wrong = root_agent_module.json.loads(root_agent_module.json.dumps(meta))
    wrong["payload"]["item"]["changes"][0]["path"] = "/tmp/wrong.patch"
    if mismatch in {"thread", "turn"}:
        wrong[f"{mismatch}_id"] = "unrelated"
    elif mismatch == "item":
        wrong["payload"]["item"]["id"] = "unrelated"
    elif mismatch == "type":
        wrong["payload"]["item"]["type"] = "commandExecution"
    elif mismatch == "event":
        wrong["kind"] = "item/completed"
    session.add(EventLog(
        project_id="other-project" if mismatch == "project" else "proj-root",
        level="INFO", message="native item", meta=wrong,
    ))
    session.commit()
    assert engine._native_file_changes("proj-root", params) == [
        {"path": "/tmp/correct.patch", "kind": {"type": "add"}}
    ]
    assert engine._native_file_changes("proj-root", {**params, "itemId": "missing"}) == []
    assert engine._native_file_changes("proj-root", {"itemId": "patch-1"}) == []


def test_restart_recovery_trigger_keeps_exact_human_reply_context():
    trigger = build_restart_recovery_trigger(
        "继续 Owner 决定：改用 DeepSeek 做 Judge",
        [{
            "text": "我是说生成样本改用火山 DeepSeek，Judge 继续用 Codex。",
            "origin": "human",
            "reply_to": {"request": "样本生成接口要切到哪里？"},
        }],
    )

    assert "继续 Owner 决定：改用 DeepSeek 做 Judge" in trigger
    assert "用户原始上下文（优先于上述 Owner 转述）" in trigger
    assert "Judge 继续用 Codex" in trigger
    assert "回复对象：样本生成接口要切到哪里" in trigger


def test_native_approval_resolution_matches_native_outcome():
    assert root_agent_module._native_approval_resolution(
        {"decision": "cancel"}
    ) == ("✅ AI Project Owner 已代你选择，当前轮次已停止", "stopping")
    assert root_agent_module._native_approval_resolution(
        {"decision": "decline"}
    ) == (
        "✅ AI Project Owner 已代你选择，已拒绝本次操作并继续处理",
        "running",
    )
    assert root_agent_module._native_approval_resolution(
        {
            "decision": {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": ["python", "-m", "pytest"],
                }
            }
        }
    ) == ("✅ AI Project Owner 已代你选择，已允许并继续处理", "running")


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
async def test_explicit_project_lead_switch_is_idle_and_does_not_run_turn(root_context):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "codex-thread")

    assert await engine.set_lead_provider("proj-root", "kimi_code") == (
        "codex", "kimi_code"
    )
    assert engine.store.ensure("proj-root").native_thread_id is None
    assert session.get(Project, "proj-root").context["execution_provider"] == "kimi_code"
    with pytest.raises(RuntimeError, match="尚未接入"):
        await engine.set_lead_provider("proj-root", "claude_code")


@pytest.mark.asyncio
async def test_repeated_owner_dispatch_runs_one_review(root_context, monkeypatch):
    from unittest.mock import AsyncMock

    session, engine = root_context
    session.add(ProjectOwnerReview(
        id="review-repeated", project_id="proj-root", request_item_id="report:done",
        status="pending", outcome="pending",
    ))
    session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    invocations = []
    async def review(self, project, **kwargs):
        invocations.append(kwargs["review_id"])
        entered.set()
        await release.wait()
        row = self.session.get(ProjectOwnerReview, kwargs["review_id"])
        row.status = "completed"
        self.session.add(row)
        self.session.commit()

    monkeypatch.setattr(root_agent_module, "_SERVICE_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(RootAgentEngine, "_review_delivered_report", review)
    monkeypatch.setattr("mac_system.feishu.accounts.create_feishu_bridge", lambda *args: AsyncMock())
    def dispatch():
        root_agent_module.schedule_project_owner_review(
            engine.bind, engine.runtime, engine.settings, "review-repeated"
        )

    dispatch()
    task = root_agent_module._OWNER_REVIEW_TASKS["review-repeated"]
    try:
        dispatch()  # duplicate before the job's database claim
        await asyncio.wait_for(entered.wait(), 1)
        dispatch()  # lost IPC acknowledgement while review is running
        assert root_agent_module._OWNER_REVIEW_TASKS["review-repeated"] is task
        release.set()
        await asyncio.wait_for(task, 1)
        assert invocations == ["review-repeated"]
        session.expire_all()
        assert session.get(ProjectOwnerReview, "review-repeated").status == "completed"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_project_display_name_hides_internal_ids_and_compacts_long_names():
    internal = Project(
        id="proj-current",
        name="proj_20260806_151800_fad4f39ba9558e90",
        task_type="feature",
        description="test",
        workspace_path="/tmp/project",
    )
    named = Project(
        id="proj-named",
        name="这是一个很长但仍然有意义的用户项目名称",
        task_type="feature",
        description="test",
        workspace_path="/tmp/project",
    )

    assert RootAgentEngine._project_display_name(internal) == "当前项目"
    assert RootAgentEngine._project_display_name(named) == "这是一个很长但仍然有意义的用户项…"


def test_project_contract_tool_projects_existing_governance_without_new_state(
    root_context,
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    policy = engine.service.add_project_policies(
        project.id,
        [{
            "policy": "交互状态必须与后台一致",
            "evidence": "交互状态必须与后台一致",
            "source_ref": "message-policy",
            "kind": "explicit",
            "category": "interaction",
            "strength": "default",
            "source_time": "2026-08-15T09:00:00",
            "source_context": {
                "original_text": "交互状态必须与后台一致",
                "context_before": "先看最近一轮",
            },
        }],
        source_ref="message-policy",
    )[0]
    engine.service.add_project_policies(
        project.id,
        [{
            "policy": "旧格式规则不再进入活跃上下文",
            "evidence": "旧格式规则",
            "source_ref": "legacy-policy",
            "kind": "constraint",
            "category": "legacy",
        }],
        source_ref="legacy-policy",
    )
    decision = engine.service.create_project_decision(
        project.id,
        request_item_id="owner-next",
        native_turn_id="turn-owner",
        question="下一步",
        options=[],
        decision="先解决进度不可见",
        rationale="这是当前最大用户问题。",
        impact="手机端能判断是否卡住。",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
        status="applied",
        decision_type="governance",
        execution_status="active",
        acceptance_criteria=["执行中持续显示当前动作"],
    )

    tool = next(
        item
        for item in engine._extension_tools(project)
        if item.name == "read_project_contract"
    )
    payload = tool.handler(
        {}, ToolContext("thread-1", "turn-1", "call-contract", tool.name)
    )

    assert "direction" not in payload
    assert payload["project_rules"][0]["policy_ref"] == policy.id
    assert payload["project_rules"][0]["source_type"] == "human"
    assert payload["project_rules"][0]["source_context"]["context_before"] == "先看最近一轮"
    assert len(payload["project_rules"]) == 1
    assert payload["unfinished_owner_commitments"][0]["decision_ref"] == decision.id


@pytest.mark.asyncio
async def test_human_request_enters_root_without_owner_preflight(
    root_context, monkeypatch
):
    session, engine = root_context
    session.add(ProjectUser(project_id="proj-root", open_id="ou-owner", role="owner"))
    project = session.get(Project, "proj-root")
    turns = []

    class FakeRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "root-after-alignment"
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, text):
            turns.append(text)
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-direct",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    result = await engine.run(project.id, "当前工作目录是什么？")

    assert result.status == "completed"
    assert turns == ["当前工作目录是什么？"]
    assert session.exec(select(ProjectDecision)).all() == []


@pytest.mark.asyncio
async def test_stop_cleans_provider_background_terminals(root_context):
    _session, engine = root_context
    calls = []

    class RunningRoot:
        async def interrupt(self, turn_id=None):
            calls.append(("interrupt", turn_id))
            return True

        async def clean_background_terminals(self):
            calls.append(("clean", None))

    engine.store.set_turn("proj-root", turn_id="turn-running", status="running")
    loop = asyncio.get_running_loop()
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS["proj-root"] = root_agent_module._RootHandle(
            RunningRoot(), loop, "codex"
        )

    assert await engine.interrupt("proj-root") is True
    assert calls == [("interrupt", "turn-running"), ("clean", None)]
    assert engine.store.ensure("proj-root").status == "idle"
    assert engine.service.require_project("proj-root").execution_status == "idle"


@pytest.mark.asyncio
async def test_shutdown_interrupts_active_root_before_closing_transport():
    calls = []

    class RunningRoot:
        active_turn_id = "turn-running"

        async def interrupt(self, turn_id=None):
            calls.append(("interrupt", turn_id))
            self.active_turn_id = None
            return True

        async def close(self):
            calls.append(("close", None))

    loop = asyncio.get_running_loop()
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS["shutdown-project"] = (
            root_agent_module._RootHandle(RunningRoot(), loop, "codex")
        )

    await root_agent_module.close_root_threads()

    assert calls == [("interrupt", "turn-running"), ("close", None)]


@pytest.mark.asyncio
async def test_shutdown_queues_gracefully_interrupted_root_for_recovery(root_context):
    _session, engine = root_context
    engine.store.set_native_thread("proj-root", "thread-1")
    engine.store.begin_turn("proj-root", user_request="继续用户请求")
    engine.store.set_turn("proj-root", turn_id="turn-running", status="running")

    class RunningRoot:
        active_turn_id = "turn-running"

        async def interrupt(self, _turn_id=None):
            engine.store.finish_active_history_round(
                "proj-root", status="interrupted"
            )
            engine.store.set_turn("proj-root", turn_id=None, status="idle")
            self.active_turn_id = None
            return True

        async def close(self):
            return None

    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS["proj-root"] = (
            root_agent_module._RootHandle(
                RunningRoot(), asyncio.get_running_loop(), "codex", engine=engine
            )
        )

    await root_agent_module.close_root_threads()

    engine.session.expire_all()
    assert engine.store.ensure("proj-root").restart_recovery_pending is True
    assert engine.store.get_restart_recovery("proj-root") == (
        "thread-1", "继续用户请求", "human", None
    )


def test_turn_timing_profile_contains_only_elapsed_protocol_milestones():
    timing = _TurnTiming(scheduled=100.0, run_started=100.125)
    timing.thread_ready = 100.5
    timing.turn_started = 100.75
    timing.first_item = 101.0
    timing.first_tool = 101.25
    timing.first_progress_card = 101.5
    timing.turn_completed = 103.0
    timing.final_card = 103.25
    timing.postprocess_completed = 104.0
    timing.protocol_duration_ms = 2_200

    profile = timing.profile(status="completed", turn_id="turn-1")

    assert profile == {
        "profile_type": "root_turn_timing",
        "turn_id": "turn-1",
        "status": "completed",
        "milestones_ms": {
            "queued": 0,
            "run_started": 125,
            "thread_ready": 500,
            "turn_started": 750,
            "first_item": 1000,
            "first_tool": 1250,
            "first_progress_card": 1500,
            "turn_completed": 3000,
            "final_card": 3250,
            "postprocess_completed": 4000,
            "total": 4000,
        },
        "protocol_duration_ms": 2200,
    }


def test_turn_timing_reports_per_turn_token_delta_not_thread_lifetime_total():
    timing = _TurnTiming(scheduled=1.0, run_started=1.0)
    timing.token_baseline = {
        "totalTokens": 100_000,
        "inputTokens": 90_000,
        "cachedInputTokens": 70_000,
        "outputTokens": 10_000,
    }
    timing.token_latest = {
        "totalTokens": 103_500,
        "inputTokens": 93_200,
        "cachedInputTokens": 72_800,
        "outputTokens": 10_300,
    }

    assert timing.turn_token_usage() == {
        "totalTokens": 3_500,
        "inputTokens": 3_200,
        "cachedInputTokens": 2_800,
        "outputTokens": 300,
    }
    assert timing.profile(status="completed", turn_id="turn-2")["token_usage"] == {
        "totalTokens": 3_500,
        "inputTokens": 3_200,
        "cachedInputTokens": 2_800,
        "outputTokens": 300,
    }


def test_turn_timing_counts_native_compaction_once_in_the_turn_delta():
    timing = _TurnTiming(scheduled=1.0, run_started=1.0)
    timing.token_baseline = {
        "totalTokens": 100_000,
        "inputTokens": 90_000,
        "outputTokens": 10_000,
    }
    timing.token_latest = {
        "totalTokens": 105_500,
        "inputTokens": 95_000,
        "outputTokens": 10_500,
    }
    timing.context_input_tokens = 18_400
    timing.context_compacted = True

    profile = timing.profile(status="completed", turn_id="turn-3")

    assert profile["token_usage"] == {
        "totalTokens": 5_500,
        "inputTokens": 5_000,
        "outputTokens": 500,
    }
    assert profile["context_input_tokens"] == 18_400
    assert profile["context_input_source"] == "last_model_input"
    assert profile["context_compacted"] is True


@pytest.mark.asyncio
async def test_oversized_resumed_thread_runs_original_messages_without_host_compaction(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    engine.store.set_native_thread(project.id, "persistent-thread")
    await engine.service.log_event(
        project.id,
        "INFO",
        "thread/tokenUsage/updated",
        meta={
            "kind": "thread/tokenUsage/updated",
            "thread_id": "persistent-thread",
            "payload": {
                "tokenUsage": {
                    "total": {"totalTokens": 1_000_000},
                    "last": {"inputTokens": 130_000},
                }
            },
        },
    )
    calls = []
    baselines = []
    workspaces = []

    class Root(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.event_handler = kwargs["event_handler"]
            workspaces.append(kwargs["cwd"])

        async def create_thread(self):
            raise AssertionError("The existing thread must not be replaced")

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            calls.append(("resume", thread_id))
            return thread_id

        async def compact_thread(self):
            calls.append(("host-compact", self.thread_id))
            raise AssertionError("Compaction belongs to the native turn")

        async def run_turn(self, text):
            calls.append(("turn", text))
            baseline = engine._turn_timings[project.id].token_baseline
            baselines.append(baseline)
            await self.event_handler(AgentEvent(
                "thread/tokenUsage/updated", self.thread_id, "native-turn",
                {"tokenUsage": {
                    "total": {"totalTokens": baseline["totalTokens"] + 2_000},
                    "last": {"inputTokens": 130_000},
                }},
            ))
            return TurnResult(
                thread_id=self.thread_id,
                turn_id=f"native-turn-{len(baselines)}",
                status="completed",
                final_message=None,
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", Root)

    await engine.run(project.id, "先查看，不要修改文件")
    await engine.run(project.id, "继续查看当前状态")

    assert calls == [
        ("resume", "persistent-thread"),
        ("turn", "先查看，不要修改文件"),
        ("turn", "继续查看当前状态"),
    ]
    assert baselines == [{"totalTokens": 1_000_000}, {"totalTokens": 1_002_000}]
    assert [str(path) for path in workspaces] == [project.workspace_path]
    assert engine.store.ensure(project.id).native_thread_id == "persistent-thread"


@pytest.mark.asyncio
async def test_compaction_metrics_require_a_completed_root_item(root_context):
    _session, engine = root_context
    timing = _TurnTiming(scheduled=1.0, run_started=1.0, root_thread_id="root-thread")
    engine._turn_timings["proj-root"] = timing
    item = {"type": "contextCompaction", "id": "compact-1"}

    await engine._on_event("proj-root", AgentEvent(
        "item/started", "root-thread", "root-turn", {"item": item},
    ))
    assert timing.context_compacted is False

    await engine._on_event("proj-root", AgentEvent(
        "item/completed", "child-thread", "child-turn", {"item": item},
    ))
    assert timing.context_compacted is False

    await engine._on_event("proj-root", AgentEvent(
        "item/completed", "root-thread", "root-turn", {"item": item},
    ))
    assert timing.profile(status="completed", turn_id="root-turn")["context_compacted"] is True
    assert timing.turn_completed is None


def test_progress_card_attributes_token_usage_and_marks_unreported_providers():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=100.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="正在整理结果",
        root_usage={
            "totalTokens": 26_408,
            "inputTokens": 26_206,
            "cachedInputTokens": 24_064,
            "outputTokens": 202,
        },
        native_child_baselines={
            "child-1": {"totalTokens": 10, "inputTokens": 8, "outputTokens": 2}
        },
        native_child_latest={
            "child-1": {"totalTokens": 110, "inputTokens": 88, "outputTokens": 22}
        },
        native_child_models={"child-1": "gpt-5"},
        external_usage={"agent-1": ("Kimi Code", None)},
    )

    body = _progress_card_body(state, now_monotonic=101.0)

    assert "Token · Codex Root：26,408" in body
    assert "输入 26,206，其中缓存 24,064；输出 202" in body
    assert "Token · Codex 子 Agent：100" in body
    assert "Token · Kimi Code：后端未报告" in body
    assert "Token · 可核验合计：26,508（不含 1 个未报告执行项）" in body
    assert "执行链：Codex → Codex 子 Agent → Kimi Code" in body


def test_external_usage_normalizes_provider_specific_counter_names():
    usage = canonical_usage(
        {
            "input_tokens": 30,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
            "output_tokens": 10,
            "total_tokens": 65,
        }
    )

    assert usage == {
        "totalTokens": 65,
        "inputTokens": 30,
        "cacheReadInputTokens": 20,
        "cacheCreationInputTokens": 5,
        "outputTokens": 10,
    }


@pytest.mark.asyncio
async def test_root_uses_native_codex_environment_and_multi_agent(
    root_context, monkeypatch
):
    session, engine = root_context
    captured = {}

    class FakeRoot(_FakeLeadBindings):
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
    assert captured["approval_policy"] == "on-request"
    assert captured["environments"] is None
    assert captured["codex_home"] is None
    assert [tool.name for tool in captured["tools"]] == [
        "read_project_contract",
        "list_external_agents",
        "run_external_agent",
        "run_external_agents",
    ]
    assert captured["developer_instructions"] is None
    assert captured["thread_config"] == {
        "model": "gpt-6.1-sol",
        "model_reasoning_effort": "xhigh",
        "model_provider": "mac_openai_https",
        "model_providers.mac_openai_https": {
            "name": "OpenAI HTTPS",
            "wire_api": "responses",
            "requires_openai_auth": True,
            "supports_websockets": False,
            "stream_max_retries": 10,
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
async def test_root_uses_persisted_project_lead_provider_through_registry(root_context):
    _session, engine = root_context
    turns: list[str] = []

    class AlternativeLead(_FakeLeadBindings):
        def __init__(self, context):
            self.thread_id = None
            self.active_turn_id = None
            self.active_model = "alternative-model"
            self.update_bindings(
                tools=list(context.tools),
                event_handler=context.event_handler,
                server_request_handler=context.server_request_handler,
            )

        async def create_thread(self, *, session_start_source=None):
            self.thread_id = "alternative-thread"
            return self.thread_id

        async def resume_thread(self, thread_id):
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, text, *, output_schema=None):
            turns.append(text)
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="alternative-turn",
                status="completed",
                final_message=None,
                events=(),
            )

        async def steer(self, _turn_id, _text):
            return None

        async def interrupt(self, _turn_id=None):
            return True

        async def close(self):
            return None

    engine.lead_runtime_registry = ProjectLeadRuntimeRegistry(
        {"pi": AlternativeLead}
    )
    engine.store.ensure("proj-root", provider="pi")

    await engine.run("proj-root", "由替代 Project Lead 执行")

    assert turns == ["由替代 Project Lead 执行"]


@pytest.mark.asyncio
async def test_project_messages_resume_the_same_native_root_thread(
    root_context, monkeypatch
):
    _, engine = root_context
    calls = []

    class FakeRoot(_FakeLeadBindings):
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
async def test_provider_contract_change_never_replaces_persistent_root_thread(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    project.context = {**(project.context or {}), "root_agent_profile": "old-contract"}
    session.add(project)
    durable = engine.store.ensure(project.id)
    durable.native_thread_id = "existing-thread"
    durable.status = "failed"
    session.add(durable)
    session.commit()
    calls = []

    class FakeRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self, **_kwargs):
            calls.append("create")
            self.thread_id = "replacement-thread"
            return self.thread_id

        async def resume_thread(self, thread_id):
            calls.append(("resume", thread_id))
            self.thread_id = thread_id
            return thread_id

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    root = await engine._root_thread(project)

    assert root.thread_id == "existing-thread"
    assert calls == [("resume", "existing-thread")]
    assert engine.store.ensure(project.id).native_thread_id == "existing-thread"


@pytest.mark.asyncio
async def test_every_turn_seeds_a_big_step_from_the_user_request(
    root_context, monkeypatch
):
    _, engine = root_context
    observed_steps: list[str] = []

    class FakeRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "root-without-commentary"
            return self.thread_id

        async def run_turn(self, _text):
            observed_steps.extend(
                step.title for step in engine._progress["proj-root"].big_steps
            )
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-without-commentary",
                status="completed",
                final_message="直接回答，不产生 commentary 或工具事件。",
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run(
        "proj-root",
        "[内部卡片上下文]\n解释为什么简单问答有时没有大步骤",
        visible_request="解释为什么简单问答有时没有大步骤",
    )

    assert observed_steps == ["解释为什么简单问答有时没有大步骤"]


@pytest.mark.asyncio
async def test_runtime_exception_reviews_failure_with_current_visible_request(
    root_context, monkeypatch
):
    _session, engine = root_context
    published: list[dict[str, object]] = []

    class FailingRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "failing-root"
            return self.thread_id

        async def run_turn(self, _text):
            raise RuntimeError("provider connection closed")

        async def close(self):
            return None

    async def publish_failure(_project_id, _exc, **kwargs):
        published.append(kwargs)

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FailingRoot)
    monkeypatch.setattr(engine, "_publish_failure_safely", publish_failure)

    with pytest.raises(RuntimeError, match="provider connection closed"):
        await engine.run(
            "proj-root",
            "[内部卡片上下文]\n继续完成项目",
            visible_request="继续完成项目",
        )

    assert published == [
        {
            "user_request": "继续完成项目",
            "request_origin": "human",
            "parent_decision_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_failed_native_turn_closes_history_even_without_feishu(
    root_context, monkeypatch
):
    session, engine = root_context

    class FailedRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")

        async def create_thread(self):
            self.thread_id = "thread-failed"
            return self.thread_id

        async def run_turn(self, _text):
            return TurnResult(
                thread_id=self.thread_id,
                turn_id="turn-stream-failed",
                status="failed",
                final_message=None,
                events=(
                    AgentEvent(
                        "turn/completed",
                        self.thread_id,
                        "turn-stream-failed",
                        {
                            "turn": {
                                "id": "turn-stream-failed",
                                "status": "failed",
                                "error": {"message": "stream disconnected"},
                            }
                        },
                    ),
                ),
            )

        async def close(self):
            return None

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FailedRoot)

    result = await engine.run("proj-root", "继续完成当前主线")

    history = session.exec(select(ProjectHistoryRound)).one()
    root = session.exec(select(RootAgentSession)).one()
    assert result.status == "failed"
    assert history.native_turn_id == "turn-stream-failed"
    assert history.report_status == "failed"
    assert history.completed_at is not None
    assert "stream disconnected" in history.report_content
    assert root.status == "idle"
    assert root.active_history_round_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["terminal", "reconnecting"])
async def test_native_recovered_answer_is_persisted_and_delivered_once(
    root_context, monkeypatch, failure_mode
):
    from tests.test_root_runtime import _CapacityProcess, _ReconnectingProcess

    session, engine = root_context
    process = _CapacityProcess(
        failures=1,
        codex_error_info={"responseStreamDisconnected": {"httpStatusCode": None}},
        recovery_status="completed",
    )
    if failure_mode == "reconnecting":
        process = _ReconnectingProcess()
        engine.settings.project_lead_silence_timeout_seconds = 0.025
    waits = []

    async def fake_subprocess(*_args, **_kwargs):
        return process

    async def no_wait(delay_seconds):
        waits.append(delay_seconds)

    class RecordingBridge:
        def __init__(self):
            self.cards = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append(card)
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            return None

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-recovery-test"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(
        "mac_system.agents.root_runtime._wait_for_server_capacity", no_wait
    )
    monkeypatch.setattr(
        root_agent_module, "schedule_project_owner_review", lambda *a, **kw: None
    )

    try:
        result = await engine.run("proj-root", "保留并送达恢复成功的答案")
        # Replaying delivery must keep the same durable intent and card.
        await engine._publish_final(project, result)
    finally:
        await root_agent_module.close_root_threads()

    reports = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.dedupe_key == f"root-result:{result.turn_id}:1"
        )
    ).all()
    history = session.exec(select(ProjectHistoryRound)).one()
    assert process.turn_attempts == 2
    assert waits == [5.0]
    assert result.final_message == "done"
    assert len(reports) == 1
    assert reports[0].status == "sent"
    assert reports[0].provider_message_id
    assert "done" in str(reports[0].payload)
    assert sum("done" in str(card) for card in engine.feishu_bridge.cards) == 1
    assert history.native_turn_id == result.turn_id
    assert history.report_content == "done"
    assert history.report_status == "completed"
    assert history.completed_at is not None


@pytest.mark.asyncio
async def test_explicit_new_thread_replaces_history_without_changing_project(
    root_context, monkeypatch
):
    session, engine = root_context
    created: list[str] = []
    closed: list[str] = []

    class FakeRoot(_FakeLeadBindings):
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
async def test_set_lead_model_replaces_thread_and_applies_to_new_runtime(
    root_context, monkeypatch
):
    session, engine = root_context
    created: list[str] = []
    models: list[str] = []

    class FakeRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}
            self.event_handler = kwargs.get("event_handler")
            self.server_request_handler = kwargs.get("server_request_handler")
            models.append((kwargs.get("thread_config") or {}).get("model"))

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
                final_message="第一轮完成",
                events=(),
            )

        async def close(self):
            pass

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run("proj-root", "第一轮")
    history_before = session.exec(select(ProjectHistoryRound)).all()
    workspace_before = session.get(Project, "proj-root").workspace_path
    assert history_before, "第一轮应产生项目记录"

    old_model, new_model = await engine.set_lead_model("proj-root", "gpt-6-astra")

    assert (old_model, new_model) == (None, "gpt-6-astra")
    project = session.get(Project, "proj-root")
    assert project.context["lead_model"] == "gpt-6-astra"
    assert project.context["root_thread_reset_pending"] is True
    assert project.workspace_path == workspace_before
    assert session.exec(select(RootAgentSession)).one().native_thread_id is None

    await engine.run("proj-root", "新模型第一轮")

    assert created == [("thread-1", None), ("thread-2", "clear")]
    assert models == ["gpt-6.1-sol", "gpt-6-astra"]
    # 切换模型只替换原生对话线程；项目记录（指令与已送达汇报）全部保留。
    history_after = session.exec(select(ProjectHistoryRound)).all()
    assert [row.id for row in history_before] == [
        row.id for row in history_after[: len(history_before)]
    ]
    assert [row.user_instructions for row in history_before] == [
        row.user_instructions for row in history_after[: len(history_before)]
    ]

    # Restoring the default removes the override entirely.
    await engine.set_lead_model("proj-root", None)
    assert "lead_model" not in (session.get(Project, "proj-root").context or {})


@pytest.mark.asyncio
async def test_set_lead_model_rejects_active_turn(root_context):
    session, engine = root_context
    engine.store.set_turn("proj-root", turn_id="turn-active", status="running")

    with pytest.raises(RuntimeError, match="当前项目仍在处理消息"):
        await engine.set_lead_model("proj-root", "gpt-6-astra")

    assert "lead_model" not in (session.get(Project, "proj-root").context or {})


@pytest.mark.asyncio
async def test_set_lead_model_rejects_non_codex_provider(root_context):
    session, engine = root_context
    engine.store.ensure("proj-root", provider="kimi_code")

    with pytest.raises(RuntimeError, match="仅对 Codex 生效"):
        await engine.set_lead_model("proj-root", "gpt-6-astra")

    assert "lead_model" not in (session.get(Project, "proj-root").context or {})


@pytest.mark.asyncio
async def test_set_lead_model_same_choice_keeps_current_thread(
    root_context, monkeypatch
):
    session, engine = root_context
    created: list[str] = []

    class FakeRoot(_FakeLeadBindings):
        def __init__(self, **kwargs):
            self.thread_id = None
            self.tools = {}

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
            pass

    monkeypatch.setattr("mac_system.core.root_agent.CodexAppServerThread", FakeRoot)

    await engine.run("proj-root", "第一轮")

    # 选择与当前生效模型相同（含显式写默认名）：不替换线程、不丢对话。
    old_model, new_model = await engine.set_lead_model("proj-root", "gpt-6.1-sol")
    assert (old_model, new_model) == (None, "gpt-6.1-sol")
    old_model, new_model = await engine.set_lead_model("proj-root", None)
    assert (old_model, new_model) == (None, None)

    assert created == [("thread-1", None)]
    assert session.exec(select(RootAgentSession)).one().native_thread_id == "thread-1"
    assert "root_thread_reset_pending" not in (
        session.get(Project, "proj-root").context or {}
    )


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


@pytest.mark.asyncio
async def test_terminal_native_event_does_not_restore_running_projection(root_context):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "thread-1")
    engine.store.set_turn("proj-root", turn_id=None, status="idle")

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "turn/completed",
            "thread-1",
            "turn-1",
            {"turn": {"id": "turn-1", "status": "completed"}},
        ),
    )
    await engine._on_event(
        "proj-root",
        AgentEvent(
            "process/exited",
            "thread-1",
            "turn-1",
            {"processId": "terminal-1", "exitCode": 0},
        ),
    )
    await engine._on_event(
        "proj-root",
        AgentEvent(
            "item/completed",
            "thread-1",
            "turn-1",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "exec-late",
                    "status": "interrupted",
                }
            },
        ),
    )

    root = session.exec(select(RootAgentSession)).one()
    project = session.get(Project, "proj-root")
    assert root.status == "idle"
    assert root.active_turn_id is None
    assert project.execution_status == "idle"


@pytest.mark.asyncio
async def test_child_token_events_are_counted_separately_from_root(root_context):
    _session, engine = root_context
    engine.store.set_native_thread("proj-root", "root-thread")
    engine._turn_timings["proj-root"] = _TurnTiming(
        scheduled=1.0,
        run_started=1.0,
        root_thread_id="root-thread",
        token_baseline={
            "totalTokens": 1_000,
            "inputTokens": 900,
            "outputTokens": 100,
        },
    )
    engine._update_progress("proj-root", label="正在处理")

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "thread/tokenUsage/updated",
            "child-thread",
            "child-turn",
            {
                "tokenUsage": {
                    "total": {
                        "totalTokens": 300,
                        "inputTokens": 250,
                        "outputTokens": 50,
                    }
                }
            },
        ),
    )

    timing = engine._turn_timings["proj-root"]
    state = engine._progress["proj-root"]
    assert timing.token_latest is None
    assert state.native_child_baselines["child-thread"] == {}
    assert state.native_child_latest["child-thread"]["totalTokens"] == 300

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "thread/tokenUsage/updated",
            "root-thread",
            "root-turn",
            {
                "tokenUsage": {
                    "total": {
                        "totalTokens": 1_200,
                        "inputTokens": 1_070,
                        "outputTokens": 130,
                    }
                }
            },
        ),
    )

    assert timing.token_latest == {
        "totalTokens": 1_200,
        "inputTokens": 1_070,
        "outputTokens": 130,
    }
    assert timing.turn_token_usage() == {
        "totalTokens": 200,
        "inputTokens": 170,
        "outputTokens": 30,
    }


@pytest.mark.asyncio
async def test_native_child_agents_are_available_in_expandable_execution_details(
    root_context,
):
    _session, engine = root_context
    engine.store.set_native_thread("proj-root", "root-thread")
    engine._turn_timings["proj-root"] = _TurnTiming(
        scheduled=1.0,
        run_started=1.0,
        root_thread_id="root-thread",
    )
    engine._update_progress("proj-root", label="正在并行验证")

    await engine._on_event(
        "proj-root",
        AgentEvent(
            "item/completed",
            "root-thread",
            "root-turn",
            {
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "spawn-1",
                    "status": "completed",
                    "receiverThreadIds": ["child-a", "child-b"],
                    "model": "gpt-test",
                    "agentsStates": {
                        "child-a": {"status": "running"},
                        "child-b": {"status": "completed"},
                    },
                }
            },
        ),
    )

    state = engine._progress["proj-root"]
    view = root_agent_module._progress_card_view(state)
    detail_text = "\n".join(view.details[0][1])

    assert "并行执行：2 个 Agent · 1 运行中 · 1 已完成" in view.details[0][1]
    assert "Codex 子 Agent 1 · gpt-test · 运行中" in detail_text
    assert "Codex 子 Agent 2 · gpt-test · 已完成" in detail_text
    assert "child-a" not in detail_text
    assert "child-b" not in detail_text
    assert view.details[0][2] is False


def test_latest_token_totals_filters_by_native_thread(root_context):
    _session, engine = root_context
    engine.store.append_event(
        "proj-root",
        AgentEvent(
            "thread/tokenUsage/updated",
            "root-thread",
            "root-turn",
            {"tokenUsage": {"total": {"totalTokens": 100}}},
        ),
    )
    engine.store.append_event(
        "proj-root",
        AgentEvent(
            "thread/tokenUsage/updated",
            "child-thread",
            "child-turn",
            {"tokenUsage": {"total": {"totalTokens": 900}}},
        ),
    )

    assert engine._latest_token_totals("proj-root", "root-thread") == {
        "totalTokens": 100
    }
    assert engine._latest_token_totals("proj-root", "child-thread") == {
        "totalTokens": 900
    }


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
    ) is None


def test_dynamic_tool_activity_hides_internal_protocol_identifier():
    detail = _native_activity_detail(
        {
            "type": "dynamicToolCall",
            "namespace": "mac",
            "tool": "run_external_agent",
        }
    )

    assert detail == "工具：外部 Coding-Agent"
    assert "run_external_agent" not in detail


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
    assert detail == (
        "命令：curl -H 'Authorization: [REDACTED]' example.com；目录：site"
    )
    assert "secret" not in detail
    assert _native_activity_detail(
        {
            "type": "commandExecution",
            "command": "sed -n '1,20p' README.md\ngit status --short",
        }
    ) == "命令：sed -n '1,20p' README.md ⏎ git status --short"


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
    assert _native_status_card(
        "error",
        {
            "error": {"codexErrorInfo": "serverOverloaded"},
            "willRetry": False,
        },
    ) == (
        "所选模型暂时容量不足，正在等待同模型自动续跑。",
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
    assert "已用 25 秒" in fresh
    assert "最近活动" in fresh
    assert "10 秒前" in fresh
    assert "子 Agent：运行中 2，已完成 1" in fresh
    assert "**执行步骤**" in fresh
    assert "✅ 定位消息完成状态" in fresh
    assert "▶️ 补充回归测试" in fresh
    assert "**当前步骤详情**" in fresh
    assert "正在运行相关测试" in fresh
    assert "等待模型或工具返回" not in fresh

    stale = _progress_card_body(state, now_monotonic=150.0)
    assert "已用 50 秒" in stale
    assert "35 秒前" in stale
    assert "暂未收到新的 Codex 事件" in stale


def test_progress_card_excludes_time_waiting_for_user():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=110.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="等待用户选择",
        suspended_started_monotonic=110.0,
    )

    waiting = _progress_card_body(state, now_monotonic=3_710.0)
    assert "已用 10 秒" in waiting

    state.suspended_started_monotonic = None
    state.suspended_seconds = 3_600.0
    resumed = _progress_card_body(state, now_monotonic=3_725.0)
    assert "已用 25 秒" in resumed


def test_progress_card_puts_overflow_behind_expandable_ellipsis():
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

    view = root_agent_module._progress_card_view(state, now_monotonic=101.0)
    card = root_agent_module._render_progress_card("[项目] 项目进展", state)
    panels = [
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "collapsible_panel"
    ]

    assert "大步骤 0" not in "\n".join(view.steps)
    assert "小步骤 0" not in "\n".join(view.activities)
    assert "大步骤 0" in "\n".join(view.hidden_steps)
    assert "小步骤 0" in "\n".join(view.hidden_activities)
    assert [panel["header"]["title"]["content"] for panel in panels[:2]] == [
        "···",
        "···",
    ]
    assert all(panel["expanded"] is False for panel in panels[:2])

    elements = card["body"]["elements"]
    visible_big_step = next(
        element
        for element in elements
        if "大步骤 2" in str(element)
    )
    visible_small_step = next(
        element
        for element in elements
        if "小步骤 2" in str(element)
    )
    assert elements.index(panels[0]) < elements.index(visible_big_step)
    assert elements.index(visible_big_step) < elements.index(panels[1])
    assert elements.index(panels[1]) < elements.index(visible_small_step)


def test_progress_card_has_a_hard_bound_for_long_running_turns():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=100.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="正在处理",
    )
    for index in range(200):
        _start_progress_big_step(state, f"大步骤 {index}")
    for index in range(200):
        _record_progress_small_step(
            state,
            key=f"small-{index}",
            label=f"小步骤 {index}",
            status="completed",
        )
    for index in range(100):
        root_agent_module._upsert_agent_execution(
            state,
            f"agent-{index}",
            display=f"Agent {index}",
            kind="native",
            status="completed",
        )

    view = root_agent_module._progress_card_view(state, now_monotonic=101.0)
    card = root_agent_module._render_progress_card("[项目] 项目进展", state)

    assert len(view.steps) == 8
    assert len(view.hidden_steps) <= 4
    assert len(view.activities) == 7
    assert len(view.hidden_activities) <= 3
    assert len(view.details[0][1]) <= 9
    assert "已归档" in str(card)

    def count_card_elements(elements):
        return len(elements) + sum(
            count_card_elements(element.get("elements", [])) for element in elements
        )

    assert count_card_elements(card["body"]["elements"]) <= 40


@pytest.mark.asyncio
async def test_rejected_native_steer_is_queued_as_a_new_turn(root_context):
    _session, engine = root_context

    class StaleRuntime:
        async def steer(self, _turn_id, _text):
            raise AppServerProtocolError("turn is no longer active")

    engine.store.set_turn("proj-root", turn_id="stale-turn", status="running")
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS["proj-root"] = root_agent_module._RootHandle(
            runtime=StaleRuntime(),
            loop=asyncio.get_running_loop(),
            provider="codex",
        )

    assert await engine.steer("proj-root", "现在是什么状态") is False


@pytest.mark.asyncio
async def test_accepted_native_steer_updates_owning_progress_state(root_context):
    _session, engine = root_context

    class ActiveRuntime:
        def __init__(self):
            self.inputs = []

        async def steer(self, turn_id, text):
            self.inputs.append((turn_id, text))

    runtime = ActiveRuntime()
    current = monotonic()
    state = _ProgressState(
        started_monotonic=current,
        last_event_monotonic=current,
        last_event_at=datetime.now().astimezone(),
        label="正在处理",
    )
    _start_progress_big_step(state, "实现功能")
    engine._progress["proj-root"] = state
    engine.store.begin_turn(
        "proj-root",
        user_request="Owner 决定继续完成当前结果",
        request_origin="project_owner",
        parent_decision_id="decision-parent",
    )
    engine.store.set_turn("proj-root", turn_id="turn-active", status="running")
    with root_agent_module._ROOT_THREADS_GUARD:
        root_agent_module._ROOT_THREADS["proj-root"] = root_agent_module._RootHandle(
            runtime=runtime,
            loop=asyncio.get_running_loop(),
            provider="codex",
            engine=engine,
        )

    assert await engine.steer(
        "proj-root",
        "[卡片上下文]\n不要改数据库",
        visible_request="不要改数据库",
    ) is True
    assert runtime.inputs == [
        ("turn-active", "[卡片上下文]\n不要改数据库")
    ]
    assert state.label == "Codex 正在结合最新补充继续处理"
    assert state.big_steps[-1].small_steps[-1].label == "已收到本轮补充"
    assert state.big_steps[-1].small_steps[-1].detail == "不要改数据库"
    history = engine.service.get_project_history_round("proj-root")
    assert history is not None
    assert [item["text"] for item in history.user_instructions] == [
        "Owner 决定继续完成当前结果",
        "不要改数据库",
    ]
    assert [item["origin"] for item in history.user_instructions] == [
        "project_owner",
        "human_steer",
    ]
    await engine._flush_progress_snapshots("proj-root")


@pytest.mark.asyncio
async def test_human_timeline_event_rotates_progress_to_a_new_card(
    root_context,
):
    _session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    engine._progress_min_delivery_interval = 0
    project = engine.service.require_project("proj-root")
    project.feishu_chat_id = "oc-chat"
    engine.service.session.add(project)
    engine.service.session.commit()
    engine.feishu_bridge = RecordingBridge()
    current = monotonic()
    state = _ProgressState(
        started_monotonic=current,
        last_event_monotonic=current,
        last_event_at=datetime.now().astimezone(),
        label="正在处理原请求",
    )
    _start_progress_big_step(state, "实现原请求")
    engine._progress[project.id] = state

    await engine._publish_progress_snapshot(project.id, event_identity="initial")
    old_key = root_agent_module._progress_projection_key(project.id, state)
    assert await engine.rotate_progress_for_timeline_event(project.id) is True

    state.label = "正在结合最新补充继续处理"
    engine._queue_progress_snapshot(project.id, event_identity="after-human-message")
    await engine._flush_progress_snapshots(project.id)

    assert state.projection_segment == 1
    assert engine.service.get_card_projection(old_key).lifecycle_status == (
        "archived_for_timeline_event"
    )
    assert len(engine.feishu_bridge.cards) == 2
    assert engine.feishu_bridge.updates == []


@pytest.mark.asyncio
async def test_progress_projection_backs_off_after_transport_failure(root_context):
    _session, engine = root_context

    class FailingBridge:
        def __init__(self):
            self.calls = 0

        async def _send_interactive(self, *args, **kwargs):
            self.calls += 1
            raise httpx.PoolTimeout("connection pool exhausted")

    project = engine.service.require_project("proj-root")
    project.feishu_chat_id = "oc-chat"
    engine.service.session.add(project)
    engine.service.session.commit()
    bridge = FailingBridge()
    engine.feishu_bridge = bridge
    now = monotonic()
    state = _ProgressState(
        started_monotonic=now,
        last_event_monotonic=now,
        last_event_at=datetime.now().astimezone(),
        label="正在处理请求",
    )
    engine._progress[project.id] = state

    await engine._publish_progress_snapshot(project.id, event_identity="first")
    await engine._publish_progress_snapshot(project.id, event_identity="second")

    assert bridge.calls == 1
    assert state.delivery_failures == 1
    assert state.next_delivery_attempt_monotonic > monotonic()


def test_failed_root_turn_records_redacted_cause(root_context):
    session, engine = root_context

    engine._recover_failed_turn(
        "proj-root", RuntimeError("upstream reset; api_key=sk-secret-value")
    )

    failures = session.exec(
        select(EventLog).where(EventLog.message == "Root turn failed")
    ).all()
    assert len(failures) == 1
    assert failures[0].meta["error_type"] == "RuntimeError"
    assert "upstream reset" in failures[0].meta["error"]
    assert "sk-secret-value" not in str(failures[0].meta)


def test_completed_progress_card_uses_color_and_hides_small_steps():
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=101.0,
        last_event_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        label="本轮处理完成，项目可继续迭代。",
        status="completed",
    )
    _start_progress_big_step(state, "完成界面调整")
    state.big_steps[-1].status = "completed"
    _record_progress_small_step(
        state,
        key="pytest",
        label="运行测试",
        status="completed",
    )

    view = root_agent_module._progress_card_view(state, now_monotonic=102.0)
    card = root_agent_module._render_progress_card("[项目] 项目进展", state)

    assert view.current == ""
    assert view.activities == []
    assert "本轮处理完成" not in str(card)
    assert "运行测试" not in str(card)
    assert "完成界面调整" in str(card)
    assert card["header"]["template"] == "green"


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


@pytest.mark.asyncio
async def test_terminal_flush_bypasses_progress_throttle(root_context, monkeypatch):
    _session, engine = root_context
    engine._progress_min_delivery_interval = 60
    engine._progress_last_delivery["proj-root"] = root_agent_module.monotonic()
    delivered = []

    async def record_delivery(project_id, *, event_identity):
        delivered.append((project_id, event_identity))

    monkeypatch.setattr(engine, "_publish_progress_snapshot", record_delivery)
    engine._queue_progress_snapshot("proj-root", event_identity="terminal-state")

    await asyncio.wait_for(
        engine._flush_progress_snapshots("proj-root"), timeout=0.5
    )

    assert delivered == [("proj-root", "terminal-state")]


@pytest.mark.asyncio
async def test_interaction_progress_freeze_completes_with_continuous_updates(
    root_context, monkeypatch
):
    _session, engine = root_context
    current = monotonic()
    state = _ProgressState(
        started_monotonic=current,
        last_event_monotonic=current,
        last_event_at=datetime.now().astimezone(),
        label="正在处理审批",
    )
    engine._progress["proj-root"] = state
    engine._progress_min_delivery_interval = 0
    delivered = []

    async def publish_progress(project_id, *, event_identity):
        delivered.append(event_identity)
        await asyncio.sleep(0)
        engine._queue_progress_snapshot(
            project_id, event_identity=f"followup-{len(delivered)}"
        )

    monkeypatch.setattr(engine, "_publish_progress_snapshot", publish_progress)
    engine._queue_progress_snapshot("proj-root", event_identity="before-approval")

    assert await asyncio.wait_for(
        engine._suspend_progress_for_interaction("proj-root"), timeout=0.5
    ) is True
    assert state.projection_suspended is True
    assert engine._pending_progress_delivery == {}
    assert engine._progress_delivery_tasks == {}
    assert len(delivered) <= 1


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


def test_native_turn_failure_detail_reads_authoritative_completion_error():
    result = TurnResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="failed",
        final_message=None,
        events=(
            AgentEvent(
                "turn/completed",
                "thread-1",
                "turn-1",
                {
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {"message": "Selected model is at capacity."},
                    }
                },
            ),
        ),
    )

    assert root_agent_module._turn_failure_detail(result) == (
        "Selected model is at capacity."
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
async def test_completed_report_is_published_before_owner_review(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    reviewed = []

    async def review(_project, **kwargs):
        reviewed.append((len(engine.feishu_bridge.cards), kwargs))

    monkeypatch.setattr(engine, "_queue_delivered_report_review", review)
    await engine._publish_final(
        project,
        TurnResult(
            thread_id="thread-1",
            turn_id="turn-report-review",
            status="completed",
            final_message="汇报正文",
            events=(),
        ),
        visible_request="只汇报当前状态，不要继续执行",
    )

    assert len(engine.feishu_bridge.cards) == 1
    assert "汇报正文" in str(engine.feishu_bridge.cards[0][2])
    assert reviewed == [(1, {
        "turn_id": "turn-report-review",
        "title": "[个人 Agent] 本轮结果",
        "content": "汇报正文",
        "user_request": "只汇报当前状态，不要继续执行",
        "report_card": {
            "title": "[个人 Agent] 本轮结果",
            "content": "汇报正文",
            "page": 1,
            "total": 1,
            "color": "blue",
            "projection_key": "root-result:turn-report-review:1",
            "message_id": "om-1",
        },
    })]


@pytest.mark.asyncio
async def test_report_card_exposes_owner_review_lifecycle_without_extra_no_action_card(
    root_context, monkeypatch
):
    session, engine = root_context
    engine.store.ensure("proj-root")

    class RecordingBridge:
        def __init__(self):
            self.sent = []
            self.updated = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.sent.append((recipient, receive_id_type, card))
            return f"om-{len(self.sent)}"

        async def update_card(self, message_id, card):
            self.updated.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_project_owner_review",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._publish_final(
        project,
        TurnResult(
            thread_id="thread-1",
            turn_id="turn-owner-visible",
            status="completed",
            final_message="任务已完成，测试通过。",
            events=(),
        ),
        visible_request="完成并汇报",
    )

    assert len(bridge.sent) == 1
    assert len(bridge.updated) == 1
    assert "Project Owner · 审阅中" in str(bridge.updated[-1][1])
    review = session.exec(select(ProjectOwnerReview)).one()
    assert review.status == "pending"
    assert review.input_payload["report_card"]["message_id"] == "om-1"
    assert len(scheduled) == 1

    async def no_action(
        _owner, _project, *, title, content, user_request="", native_turn_id=None
    ):
        return ProjectOwnerResult(
            outcome="no_action",
            decision="",
            rationale="当前汇报没有待决策事项。",
            impact="",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
            usage={"inputTokens": 80, "outputTokens": 20, "totalTokens": 100},
            duration_seconds=4.6,
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", no_action
    )
    engine.service.mark_project_owner_review_running(review.id)
    await engine._review_delivered_report(
        project,
        turn_id="turn-owner-visible",
        title="[个人 Agent] 本轮结果",
        content="任务已完成，测试通过。",
        user_request="完成并汇报",
        review_id=review.id,
    )

    assert len(bridge.sent) == 1
    assert len(bridge.updated) == 2
    completed_card = bridge.updated[-1][1]
    assert "Project Owner · 已审阅 · 无需新决定 · 5 秒" in str(completed_card)
    assert "Agent 与用量详情" in str(completed_card)
    projection = engine.service.get_card_projection(
        "root-result:turn-owner-visible:1"
    )
    assert projection.lifecycle_status == "completed"


@pytest.mark.asyncio
async def test_delivered_maintenance_audit_resumes_same_root_without_fabricating_history(
    root_context, monkeypatch
):
    session, engine = root_context
    source = Path(__file__).resolve().parents[1] / "scripts/deploy_long_conversation_once.py"
    spec = importlib.util.spec_from_file_location("maintenance_resume_test", source)
    maintenance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maintenance)
    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.store.set_native_thread(project.id, "original-long-thread")
    engine.store.begin_turn(project.id, user_request="等已有任务自然结束，修复后核对长会话和飞书结果卡。")
    engine.store.set_turn(project.id, turn_id="original-turn", status="idle")
    before = [row.model_dump(mode="json") for row in session.exec(select(ProjectHistoryRound)).all()]
    notice_id = maintenance.notify(engine.bind, project.id, "resume", "restored", "重启完成，仍须真实验收。")
    session.expire_all()
    notice = session.get(NotificationOutbox, notice_id)
    notice.status = "sent"
    notice.provider_message_id = "om-maintenance-delivered"
    session.add(notice)
    session.commit()
    review_id = maintenance.dispatch_acceptance(engine.bind, project.id, notice_id, "resume")
    review = engine.service.mark_project_owner_review_running(review_id)
    assert engine.service.is_project_owner_review_current(review_id)

    class RecordingBridge:
        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            return "om-owner-audit"

        async def update_card(self, message_id, card):
            return None

    engine.feishu_bridge = RecordingBridge()

    async def review_report(_owner, _project, **kwargs):
        assert kwargs["request_origin"] == "owner_audit"
        assert kwargs["content"] == "重启完成，仍须真实验收。"
        assert not kwargs["native_turn_id"]
        return ProjectOwnerResult(
            outcome="decide", decision="继续已授权的原长会话与真实飞书验收。",
            rationale="维护完成还不等于验收完成。", impact="恢复本项目验收，不打断其他项目。",
            reversible=True, confidence="high", answers={}, context_refs=(),
            model="gpt-owner-test", continue_execution=True,
        )

    scheduled = []
    monkeypatch.setattr("mac_system.core.project_owner.ProjectOwner.review_report", review_report)
    monkeypatch.setattr(root_agent_module, "schedule_root_turn",
                        lambda *args, **kwargs: scheduled.append((args, kwargs)))
    payload = review.input_payload
    await engine._review_delivered_report(
        project, turn_id="", title=payload["title"], content=payload["content"],
        request_origin="owner_audit", review_id=review_id, request_item_id=review.request_item_id,
    )
    assert len(scheduled) == 1
    assert scheduled[0][0][3] == project.id
    assert "等已有任务自然结束" in scheduled[0][0][4]
    assert "真实飞书验收" in scheduled[0][0][4]
    assert scheduled[0][1]["request_origin"] == "project_owner"
    session.expire_all()
    assert session.exec(select(ProjectOwnerReview)).one().status == "completed"
    assert session.exec(select(RootAgentSession)).one().native_thread_id == "original-long-thread"
    assert [row.model_dump(mode="json") for row in session.exec(select(ProjectHistoryRound)).all()] == before


@pytest.mark.asyncio
async def test_durable_owner_review_propagates_runtime_failure(root_context, monkeypatch):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    review = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-owner-fails",
        native_turn_id="turn-owner-fails",
        title="本轮结果",
        content="汇报正文",
        user_request="完成任务",
    )
    engine.service.mark_project_owner_review_running(review.id)

    async def fail(
        _owner, _project, *, title, content, user_request="", native_turn_id=None
    ):
        raise RuntimeError("owner provider unavailable")

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", fail
    )
    with pytest.raises(RuntimeError, match="owner provider unavailable"):
        await engine._review_delivered_report(
            project,
            turn_id="turn-owner-fails",
            title="本轮结果",
            content="汇报正文",
            review_id=review.id,
        )


@pytest.mark.asyncio
async def test_stale_owner_review_cannot_decide_or_update_project(root_context, monkeypatch):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    stale = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-stale",
        native_turn_id="turn-stale",
        title="本轮结果",
        content="旧汇报",
        user_request="旧请求",
    )
    engine.service.mark_project_owner_review_running(stale.id)
    engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-newer",
        native_turn_id="turn-newer",
        title="本轮结果",
        content="新汇报",
        user_request="新请求",
    )

    async def decide(
        _owner, _project, *, title, content, user_request="", native_turn_id=None
    ):
        return ProjectOwnerResult(
            outcome="decide",
            decision="继续旧方向",
            rationale="旧汇报里的依据",
            impact="将启动旧任务",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
            continue_execution=True,
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", decide
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-stale",
        title="本轮结果",
        content="旧汇报",
        review_id=stale.id,
    )

    assert engine.service.get_project_owner_review_by_request_item_id(
        project.id, "report:turn-stale"
    ).status == "superseded"
    assert session.exec(select(ProjectDecision)).first() is None
    assert session.exec(select(ProjectOwnerState)).first() is None
    assert scheduled == []


@pytest.mark.asyncio
async def test_failed_terminal_status_stops_after_visible_failure_card(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, _message_id, card):
            self.cards.append(("update", "chat_id", card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    reviewed = []

    async def review(_project, **kwargs):
        reviewed.append((len(engine.feishu_bridge.cards), kwargs))

    monkeypatch.setattr(engine, "_queue_delivered_report_review", review)
    await engine._publish_turn_status(
        project,
        TurnResult(
            thread_id="thread-1",
            turn_id="turn-failed-review",
            status="failed",
            final_message=None,
            events=(
                AgentEvent(
                    "error",
                    "thread-1",
                    "turn-failed-review",
                    {"error": {"message": "provider at capacity"}},
                ),
            ),
        ),
        user_request="继续迭代，直到封箱期超额收益超过 10%",
    )

    assert len(engine.feishu_bridge.cards) == 2
    assert "provider at capacity" in str(engine.feishu_bridge.cards[-1][2])
    assert reviewed == []


@pytest.mark.asyncio
async def test_exception_failure_report_exposes_safe_error_and_stops(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, _message_id, card):
            self.cards.append(("update", "chat_id", card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    reviewed = []

    async def review(_project, **kwargs):
        reviewed.append(kwargs)

    monkeypatch.setattr(engine, "_queue_delivered_report_review", review)
    await engine._publish_failure(
        project,
        RuntimeError("provider connection closed"),
        user_request="修复连接问题并继续完成任务",
    )

    assert len(engine.feishu_bridge.cards) == 2
    assert "provider connection closed" in str(engine.feishu_bridge.cards[-1][2])
    assert reviewed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("continue_execution", [True, False])
async def test_report_owner_decision_updates_result_card_and_schedules_root_followup(
    root_context, monkeypatch, continue_execution
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    store = engine.store
    history = store.begin_turn(
        project.id,
        user_request="我是说用火山的 DeepSeek 生成样本，Judge 继续用 Codex。",
        reply_context={"request": "生成 C0 样本时使用哪个 DeepSeek 接口？"},
    )
    store.set_turn(project.id, turn_id="turn-report-owner", status="running")
    history_round = engine.service.get_project_history_round(
        project.id, round_id=history.active_history_round_id
    )
    assert history_round is not None
    report_message_id = await deliver_card(
        engine.service,
        bridge,
        dedupe_key="report-before-owner-review",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("本轮结果", "A、B 两个方案均已验证。"),
        project_id=project.id,
        projection_key="root-result:turn-report-owner:1",
        subject_type="root_turn",
        subject_id="turn-report-owner",
    )
    review_job = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-report-owner",
        native_turn_id="turn-report-owner",
        title="本轮结果",
        content="A、B 两个方案均已验证。",
        user_request="目标补充移动端入口",
        report_card={
            "title": "本轮结果",
            "content": "A、B 两个方案均已验证。",
            "message_id": report_message_id,
            "projection_key": "root-result:turn-report-owner:1",
        },
    )
    engine.service.mark_project_owner_review_running(review_job.id)
    monkeypatch.setattr(
        engine.service, "is_project_owner_review_current", lambda _review_id: True
    )

    async def review_report(
        _owner, _project, *, title, content, user_request="", native_turn_id=None
    ):
        assert title == "本轮结果"
        assert "两个方案" in content
        return ProjectOwnerResult(
            outcome="decide",
            decision="采用 A 方案",
            rationale="A 方案风险更低。",
            impact="按 A 方案继续实现。",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=("visible-report",),
            model="gpt-owner-test",
            continue_execution=continue_execution,
            project_policy_observations=({
                "policy": "移动端入口属于本项目验收范围",
                "evidence": "目标补充移动端入口",
                "source_ref": "current_request",
                "kind": "constraint",
                "category": "scope",
                "strength": "hard",
                "replaces": "",
            },),
        )

    scheduled = []
    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", review_report
    )
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-report-owner",
        title="本轮结果",
        content="A、B 两个方案均已验证。",
        user_request="目标补充移动端入口",
        review_id=review_job.id,
    )

    assert len(bridge.cards) == 1
    assert "本轮结果" in str(bridge.cards[0][2])
    assert len(bridge.updates) == 1
    assert bridge.updates[0][0] == report_message_id
    assert "Owner 决定" in str(bridge.updates[0][1])
    assert "采用 A 方案" in str(bridge.updates[0][1])
    assert "Owner 判断依据" in str(bridge.updates[0][1])
    decision = session.exec(select(ProjectDecision)).one()
    review = session.exec(select(ProjectOwnerReview)).one()
    assert review.outcome == "decide"
    assert review.request_item_id == "report:turn-report-owner"
    assert session.exec(select(ProjectOwnerState)).all() == []
    policy = session.exec(select(ProjectPolicy)).one()
    assert policy.content == "移动端入口属于本项目验收范围"
    assert review.policy_updates[0]["source_ref"] == "current_request"
    assert decision.request_item_id == "report:turn-report-owner"
    assert decision.answer_payload["source"] == "report_review"
    assert decision.answer_payload["embedded_report"] is True
    assert decision.answer_payload["continue_execution"] is continue_execution
    assert decision.answer_payload["human_context"] == [{
        "text": "我是说用火山的 DeepSeek 生成样本，Judge 继续用 Codex。",
        "origin": "human",
        "time": history_round.user_instructions[0]["time"],
        "source_ref": f"{history_round.id}:instruction:1",
        "reply_to": {"request": "生成 C0 样本时使用哪个 DeepSeek 接口？"},
    }]
    assert decision.card_message_id == report_message_id
    assert len(scheduled) == int(continue_execution)
    assert decision.execution_status == ("active" if continue_execution else "completed")
    assert decision.execution_rounds == int(continue_execution)
    if not continue_execution:
        return
    trigger = scheduled[0][0][4]
    assert "采用 A 方案" in trigger
    assert "最近真人原始上下文；这是主线" in trigger
    assert "Judge 继续用 Codex" in trigger
    assert "生成 C0 样本时使用哪个 DeepSeek 接口" in trigger
    assert trigger.index("Judge 继续用 Codex") < trigger.index("采用 A 方案")
    assert "完成个人 Agent 交付" not in trigger
    assert scheduled[0][1]["visible_request"].startswith("Owner 决定")
    assert scheduled[0][1]["request_origin"] == "project_owner"
    assert scheduled[0][1]["parent_decision_id"] == decision.id


@pytest.mark.asyncio
@pytest.mark.parametrize("continue_execution", [True, False])
@pytest.mark.parametrize("has_parent", [True, False])
async def test_report_owner_decide_publishes_next_step_options(
    root_context, monkeypatch, continue_execution, has_parent
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    store = engine.store
    history = store.begin_turn(
        project.id,
        user_request="其他实验和 Wiki 继续推进，实验先停下。",
    )
    store.set_turn(project.id, turn_id="turn-report-options", status="running")
    history_round = engine.service.get_project_history_round(
        project.id, round_id=history.active_history_round_id
    )
    assert history_round is not None
    parent = (
        engine.service.create_project_decision(
            project.id,
            request_item_id="report:options-parent",
            native_turn_id="turn-options-parent",
            question="完成已授权工作",
            options=[],
            decision="继续完成现有实验与整批 Wiki",
            rationale="用户已要求继续。",
            impact="不启动未选择的新方向。",
            reversible=True,
            confidence="high",
            answer_payload={"continue_execution": True},
            context_refs=["human:continue"],
            actor_model="gpt-owner-test",
            status="applied",
            decision_type="governance",
            execution_status="active",
            execution_rounds=1,
        )
        if has_parent
        else None
    )
    report_message_id = await deliver_card(
        engine.service,
        bridge,
        dedupe_key="report-before-owner-options",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("本轮结果", "实验与 Wiki 均有进展，提速方案仍待决定。"),
        project_id=project.id,
        projection_key="root-result:turn-report-options:1",
        subject_type="root_turn",
        subject_id="turn-report-options",
    )
    review_job = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-report-options",
        native_turn_id="turn-report-options",
        title="本轮结果",
        content="实验与 Wiki 均有进展，提速方案仍待决定。",
        user_request="",
        report_card={
            "title": "本轮结果",
            "content": "实验与 Wiki 均有进展，提速方案仍待决定。",
            "message_id": report_message_id,
            "projection_key": "root-result:turn-report-options:1",
        },
    )
    engine.service.mark_project_owner_review_running(review_job.id)
    monkeypatch.setattr(
        engine.service, "is_project_owner_review_current", lambda _review_id: True
    )

    async def review_report(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="decide",
            decision="继续完成现有实验与整批 Wiki",
            rationale="用户已明确授权继续，工作尚未完成。",
            impact="按原目标继续推进。",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=("visible-report",),
            model="gpt-owner-test",
            continue_execution=continue_execution,
            user_options=({
                "label": "采纳提速方案",
                "description": "用已验证的设计改造检索，缩短后续等待。",
                "action": "execute",
            }, {
                "label": "暂不改造",
                "description": "保持现状，只记录该方向。",
                "action": "record_only",
            }),
        )

    scheduled = []
    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", review_report
    )
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-report-options",
        title="本轮结果",
        content="实验与 Wiki 均有进展，提速方案仍待决定。",
        review_id=review_job.id,
        parent_decision_id=(parent.id if parent is not None else None),
    )

    assert len(scheduled) == int(continue_execution)
    if continue_execution:
        assert "继续完成现有实验与整批 Wiki" in scheduled[0][0][4]
    assert len(bridge.cards) == 2
    options_card = bridge.cards[-1][2]
    assert options_card["header"]["title"]["content"] == "AI 建议"
    buttons = [
        node
        for node in options_card["body"]["elements"]
        if node.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == [
        "采纳提速方案",
        "暂不改造",
    ]
    decisions = session.exec(select(ProjectDecision)).all()
    advice = next(row for row in decisions if row.decision_type == "advice")
    assert advice.status == "requires_user"
    assert [option["label"] for option in advice.options] == [
        "采纳提速方案",
        "暂不改造",
    ]
    governance = [row for row in decisions if row.decision_type == "governance"][-1]
    assert governance.execution_status == (
        "active" if continue_execution else "completed"
    )
    review = session.exec(select(ProjectOwnerReview)).one()
    assert review.outcome == "decide"
    assert review.continue_execution is continue_execution
    assert advice.answer_payload["source_report"]["content"] == (
        "实验与 Wiki 均有进展，提速方案仍待决定。"
    )


@pytest.mark.asyncio
async def test_superseded_owner_review_still_persists_verified_user_rule(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    job = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:rule-correction",
        native_turn_id="turn-rule-correction",
        title="规则更正",
        content="已收到更正。",
        user_request="固定名单不要再用",
    )
    engine.service.mark_project_owner_review_running(job.id)

    async def review_report(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="no_action",
            decision="",
            rationale="",
            impact="",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=("round-new:instruction:1",),
            authority_refs=("round-new:instruction:1",),
            model="owner-test",
            project_policy_observations=({
                "policy": "固定名单不再作为基准",
                "evidence": "固定名单不要再用",
                "source_ref": "round-new:instruction:1",
                "source_time": "2026-08-15T13:00:00",
                "source_context": {
                    "original_text": "固定名单不要再用",
                },
                "kind": "definition",
                "category": "benchmark",
                "strength": "hard",
                "replaces": [],
            },),
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", review_report
    )
    monkeypatch.setattr(
        engine.service,
        "is_project_owner_review_current",
        lambda _review_id: False,
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-rule-correction",
        title="规则更正",
        content="已收到更正。",
        user_request="固定名单不要再用",
        review_id=job.id,
    )

    session.expire_all()
    assert session.get(ProjectOwnerReview, job.id).status == "superseded"
    rules = session.exec(select(ProjectPolicy)).all()
    assert [rule.content for rule in rules] == ["固定名单不再作为基准"]


@pytest.mark.asyncio
async def test_superseded_owner_review_closes_card_when_human_input_predates_it(
    root_context,
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.updates = []

        async def _send_interactive(self, _recipient, _card, receive_id_type="open_id"):
            return "om-owner-source"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    human_round = engine.service.begin_project_history_round(
        project.id,
        user_request="先把汇报说清楚",
        request_origin="human",
        started_at=datetime.utcnow() - timedelta(seconds=2),
    )
    human_round.user_instructions = [{
        **human_round.user_instructions[0],
        "time": (datetime.utcnow() - timedelta(seconds=2)).isoformat(),
    }]
    session.add(human_round)
    session.commit()
    message_id = await deliver_card(
        engine.service,
        bridge,
        dedupe_key="report-before-superseded-owner",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("本轮结果", "已完成汇报。"),
        project_id=project.id,
        projection_key="root-result:turn-superseded:1",
        subject_type="root_turn",
        subject_id="turn-superseded",
    )
    review = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-superseded",
        native_turn_id="turn-superseded",
        title="本轮结果",
        content="已完成汇报。",
        user_request="先把汇报说清楚",
        report_card={
            "title": "本轮结果",
            "content": "已完成汇报。",
            "message_id": message_id,
            "projection_key": "root-result:turn-superseded:1",
        },
    )
    review = engine.service.supersede_project_owner_review(review.id)

    await engine._sync_report_owner_review_card(project, review)

    assert len(bridge.updates) == 1
    assert bridge.updates[0][0] == "om-owner-source"
    assert "Project Owner · 已审阅 · 后续消息已接管" in str(
        bridge.updates[0][1]
    )
    projection = engine.service.get_card_projection(
        "root-result:turn-superseded:1"
    )
    assert projection.lifecycle_status == "superseded"


@pytest.mark.asyncio
async def test_owner_followup_closes_linked_decision_on_a_new_latest_card(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    parent = engine.service.create_project_decision(
        project.id,
        request_item_id="report:turn-human",
        native_turn_id="turn-human",
        question="本轮汇报后的项目决定",
        options=[],
        decision="完成现有功能并跑通验收",
        rationale="用户要求把功能做完。",
        impact="在既定范围内继续。",
        reversible=True,
        confidence="high",
        answer_payload={
            "source": "report_review",
            "continue_execution": True,
            "human_context": [{
                "text": "先完成现有功能并跑通验收，不要转去做别的优化。",
                "origin": "human",
                "time": "2026-08-21T08:00:00",
                "source_ref": "human:finish-existing",
            }],
        },
        context_refs=["report:turn-human"],
        actor_model="gpt-owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
        execution_rounds=1,
        acceptance_criteria=["测试通过"],
    )
    parent = engine.service.update_project_decision(
        parent.id, status="applied", card_message_id="om-owner"
    )
    async def completed(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="no_action",
            decision="",
            rationale="验收已经全部通过。",
            impact="",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", completed
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-owner-execution",
        title="本轮结果",
        content="功能完成，测试通过。",
        user_request="Owner 决定：完成现有功能并跑通验收",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    updated = engine.service.get_project_decision(parent.id)
    assert updated.execution_status == "completed"
    assert updated.result_refs == ["report:turn-owner-execution"]
    # The decision ledger itself is authoritative; a stale compatibility
    # snapshot is intentionally no longer updated by current reviews.
    assert engine.service.get_active_project_decision(project.id) is None
    assert session.exec(select(ProjectDecision)).all() == [updated]
    assert scheduled == []
    assert engine.feishu_bridge.cards == []
    assert engine.feishu_bridge.updates == []
    assert updated.card_message_id == "om-owner"


@pytest.mark.asyncio
async def test_owner_followup_reworks_linked_decision_on_a_new_latest_card(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    parent = engine.service.create_project_decision(
        project.id,
        request_item_id="report:turn-human",
        native_turn_id="turn-human",
        question="本轮汇报后的项目决定",
        options=[],
        decision="完成现有功能并跑通验收",
        rationale="用户要求把功能做完。",
        impact="在既定范围内继续。",
        reversible=True,
        confidence="high",
        answer_payload={
            "source": "report_review",
            "continue_execution": True,
            "human_context": [{
                "text": "先完成现有功能并跑通验收，不要转去做别的优化。",
                "origin": "human",
                "time": "2026-08-21T08:00:00",
                "source_ref": "human:finish-existing",
            }],
        },
        context_refs=["report:turn-human"],
        actor_model="gpt-owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
        execution_rounds=1,
        acceptance_criteria=["真实浏览器验收通过"],
    )
    parent = engine.service.update_project_decision(
        parent.id, status="applied", card_message_id="om-owner"
    )

    async def needs_revision(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="decide",
            decision="改用当前可用的真实浏览器继续验收",
            rationale="实现已经完成，但原浏览器工具不可用。",
            impact="只改变验收工具，不扩大项目范围。",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
            continue_execution=True,
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", needs_revision
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-owner-rework",
        title="本轮未完成",
        content="实现完成，但真实浏览器工具不可用。",
        user_request="Owner 决定：完成现有功能并跑通验收",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    updated = engine.service.get_project_decision(parent.id)
    assert updated.execution_status == "active"
    assert updated.execution_rounds == 2
    assert updated.result_refs == ["report:turn-owner-rework"]
    assert session.exec(select(ProjectDecision)).all() == [updated]
    assert len(scheduled) == 1
    trigger = scheduled[0][0][4]
    assert "改用当前可用的真实浏览器继续验收" in trigger
    assert "当前决定" in trigger
    assert trigger.count("先完成现有功能并跑通验收") == 1
    assert "原决定：完成现有功能并跑通验收" not in trigger
    assert "真实浏览器验收通过" not in trigger
    assert "旧 Owner 决定只用于历史关联" in trigger
    assert "相邻发现或旧决定扩大" in trigger
    assert scheduled[0][1]["visible_request"] == (
        "Owner 决定：改用当前可用的真实浏览器继续验收"
    )
    assert scheduled[0][1]["request_origin"] == "project_owner"
    assert scheduled[0][1]["parent_decision_id"] == parent.id
    assert engine.feishu_bridge.cards == []
    assert engine.feishu_bridge.updates == []
    assert updated.card_message_id == "om-owner"


@pytest.mark.asyncio
async def test_newer_human_round_supersedes_an_older_owner_followup(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    parent = engine.service.create_project_decision(
        project.id,
        request_item_id="report:before-human-pause",
        native_turn_id="turn-before-human-pause",
        question="本轮汇报后的项目决定",
        options=[],
        decision="继续验证下一个候选",
        rationale="旧结果仍有后续工作。",
        impact="继续当前实验。",
        reversible=True,
        confidence="high",
        answer_payload={
            "source": "report_review",
            "continue_execution": True,
            "human_context": [{
                "text": "继续迭代",
                "origin": "human",
                "time": "2026-08-23T14:20:00",
                "source_ref": "human:continue",
            }],
        },
        context_refs=["report:before-human-pause"],
        actor_model="gpt-owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
        execution_rounds=1,
    )
    stale_owner_round = engine.service.begin_project_history_round(
        project.id,
        user_request="Owner 决定：继续验证下一个候选",
        request_origin="project_owner",
        parent_decision_id=parent.id,
        started_at=parent.created_at + timedelta(seconds=1),
    )
    pause_round = engine.service.begin_project_history_round(
        project.id,
        user_request="等下",
        request_origin="human",
        started_at=parent.created_at + timedelta(seconds=2),
    )
    engine.service.finish_unreported_project_history_round(
        pause_round.id, status="completed"
    )

    async def stale_continue(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="decide",
            decision="继续跑下一轮80题",
            rationale="旧 Owner 要求继续。",
            impact="将继续实验。",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
            continue_execution=True,
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", stale_continue
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-stale-owner-after-pause",
        title="本轮结果",
        content="旧 Owner 续跑只复述了原流程。",
        user_request="Owner 决定：继续验证下一个候选",
        request_origin="project_owner",
        parent_decision_id=parent.id,
        history_round_id=stale_owner_round.id,
    )

    review = engine.service.get_project_owner_review_by_request_item_id(
        project.id, "report:turn-stale-owner-after-pause"
    )
    assert review is not None
    assert review.status == "superseded"
    assert engine.service.get_project_decision(parent.id).execution_status == (
        "superseded"
    )
    assert scheduled == []
    assert session.exec(select(ProjectDecision)).all() == [
        engine.service.get_project_decision(parent.id)
    ]
    event = session.exec(
        select(EventLog).where(
            EventLog.message
            == "Project Owner result superseded by newer human instruction"
        )
    ).one()
    assert event.meta["newer_human_round_id"] == pause_round.id


def test_owner_freshness_ignores_human_input_that_predates_reviewed_round(
    root_context,
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    base = datetime.now() - timedelta(seconds=10)
    earlier_round = engine.service.begin_project_history_round(
        project.id,
        user_request="Owner 决定：整理汇报",
        request_origin="project_owner",
        started_at=base,
    )
    earlier_round.user_instructions = [
        *earlier_round.user_instructions,
        {
            "text": "汇报时说详细一点",
            "origin": "human_steer",
            "time": (base + timedelta(seconds=1)).isoformat(),
        },
    ]
    session.add(earlier_round)
    session.commit()
    reviewed_round = engine.service.begin_project_history_round(
        project.id,
        user_request="Owner 决定：按用户要求重新汇报",
        request_origin="project_owner",
        started_at=base + timedelta(seconds=2),
    )

    assert root_agent_module._newer_external_human_round(
        engine.service,
        project.id,
        after=reviewed_round.started_at,
        current_round_id=reviewed_round.id,
    ) is None


@pytest.mark.asyncio
async def test_report_owner_no_action_is_audited_without_card_or_followup(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")

    async def no_action(
        _owner, _project, *, title, content, user_request="", native_turn_id=None
    ):
        return ProjectOwnerResult(
            outcome="no_action",
            decision="无需新的项目决定",
            rationale="这只是完成汇报。",
            impact="",
            reversible=True,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", no_action
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-report-no-decision",
        title="本轮结果",
        content="任务已完成，测试通过。",
    )

    assert session.exec(select(ProjectDecision)).first() is None
    review = session.exec(select(ProjectOwnerReview)).one()
    assert review.outcome == "no_action"
    assert review.card_message_id is None
    assert scheduled == []


@pytest.mark.asyncio
async def test_report_owner_advice_appends_latest_actionable_choice_card(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    parent = engine.service.create_project_decision(
        project.id,
        request_item_id="report:advice-parent",
        native_turn_id="turn-advice-parent",
        question="完成已经获批的修复",
        options=[],
        decision="完成修复并汇报验证状态",
        rationale="用户已经要求修复。",
        impact="不自动执行明早的真实链路操作。",
        reversible=True,
        confidence="high",
        answer_payload={"continue_execution": True},
        context_refs=["human:repair"],
        actor_model="gpt-owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
        execution_rounds=1,
    )
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    report_message_id = await deliver_card(
        engine.service,
        bridge,
        dedupe_key="report-before-owner-advice",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("本轮结果", "修复已完成，但真实链路要等明早验证。"),
        project_id=project.id,
        projection_key="root-result:turn-report-advice:1",
    )
    review_job = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-report-advice",
        native_turn_id="turn-report-advice",
        title="本轮结果",
        content="修复已完成，但真实链路要等明早验证。",
        user_request="",
        report_card={
            "title": "本轮结果",
            "content": "修复已完成，但真实链路要等明早验证。",
            "message_id": report_message_id,
            "projection_key": "root-result:turn-report-advice:1",
        },
    )
    engine.service.mark_project_owner_review_running(review_job.id)
    monkeypatch.setattr(
        engine.service, "is_project_owner_review_current", lambda _review_id: True
    )

    async def advise(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="advise",
            decision="明早首轮运行后核对浏览器导航和新增职位数",
            rationale="修复尚未经过允许时段内的真实链路验证。",
            impact="只读核对，不自动发送简历。",
            reversible=False,
            confidence="medium",
            answers={},
            context_refs=("visible-report",),
            user_options=({
                "label": "明早核对",
                "description": "在允许时段核对真实链路，不自动发送简历。",
                "action": "execute",
            }, {
                "label": "暂不核对",
                "description": "保留目前修复结果，不启动后续验证。",
                "action": "record_only",
            }),
            model="gpt-owner-test",
            usage={"inputTokens": 60, "outputTokens": 20, "totalTokens": 80},
            duration_seconds=9.7,
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", advise
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-report-advice",
        title="本轮结果",
        content="修复已完成，但真实链路要等明早验证。",
        review_id=review_job.id,
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    assert len(bridge.cards) == 2
    assert len(bridge.updates) == 1
    card = bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "AI 建议"
    assert "明早首轮运行" in str(card)
    buttons = [
        node
        for node in card["body"]["elements"]
        if node.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == ["明早核对", "暂不核对"]
    decisions = session.exec(select(ProjectDecision)).all()
    decision = next(row for row in decisions if row.id != parent.id)
    assert decision.decision_type == "advice"
    assert decision.status == "requires_user"
    review = session.exec(select(ProjectOwnerReview)).one()
    assert review.outcome == "advise"
    assert review.card_message_id is None
    assert decision.card_message_id != report_message_id
    assert "embedded_report" not in decision.answer_payload
    assert decision.answer_payload["source_report"] == {
        "title": "本轮结果", "content": "修复已完成，但真实链路要等明早验证。",
    }
    assert "human_context" in decision.answer_payload
    completed_parent = engine.service.get_project_decision(parent.id)
    assert completed_parent.execution_status == "completed"
    assert completed_parent.result_refs == ["report:turn-report-advice"]
    assert engine.service.get_active_project_decision(project.id) is None
    assert scheduled == []


@pytest.mark.asyncio
async def test_report_owner_requires_user_schedules_native_question_followup(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    parent = engine.service.create_project_decision(
        project.id,
        request_item_id="report:parent",
        native_turn_id="turn-parent",
        question="执行已经获批的发布准备",
        options=[],
        decision="完成网站发布准备",
        rationale="用户已经要求完成准备工作。",
        impact="只准备，不公开发布。",
        reversible=True,
        confidence="high",
        answer_payload={"continue_execution": True},
        context_refs=["human:prepare"],
        actor_model="gpt-owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
        execution_rounds=1,
    )

    async def require_user(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="require_user",
            decision="是否将网站公开发布",
            rationale="公开发布属于用户授权范围。",
            impact="网站将可被公网访问。",
            reversible=False,
            confidence="high",
            answers={},
            context_refs=("visible-report",),
            user_options=({
                "label": "暂不发布",
                "description": "保持本地可用，不开放公网。",
                "action": "record_only",
            }, {
                "label": "公开发布",
                "description": "授权将网站开放到公网。",
                "action": "execute",
            }),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", require_user
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-report-requires-user",
        title="本轮结果",
        content="网站已经准备好，需要决定是否公开发布。",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    decisions = session.exec(select(ProjectDecision)).all()
    decision = next(row for row in decisions if row.id != parent.id)
    assert decision.status == "requires_user"
    assert decision.card_message_id is None
    assert decision.answer_payload["requires_user"] is True
    assert "embedded_report" not in decision.answer_payload
    assert [option["label"] for option in decision.options] == [
        "暂不发布",
        "公开发布",
    ]
    completed_parent = engine.service.get_project_decision(parent.id)
    assert completed_parent.execution_status == "completed"
    assert completed_parent.result_refs == ["report:turn-report-requires-user"]
    assert engine.service.get_active_project_decision(project.id) is None
    assert scheduled == []


@pytest.mark.asyncio
async def test_report_owner_uncertainty_renders_choices_without_extra_root_round(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    report_message_id = await deliver_card(
        engine.service,
        bridge,
        dedupe_key="report-before-owner-choice",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("本轮结果", "稳定性和视觉效果都可以继续优化。"),
        project_id=project.id,
        projection_key="root-result:turn-owner-choice:1",
    )
    review_job = engine.service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-owner-choice",
        native_turn_id="turn-owner-choice",
        title="本轮结果",
        content="稳定性和视觉效果都可以继续优化。",
        user_request="",
        report_card={
            "title": "本轮结果",
            "content": "稳定性和视觉效果都可以继续优化。",
            "message_id": report_message_id,
            "projection_key": "root-result:turn-owner-choice:1",
        },
    )
    engine.service.mark_project_owner_review_running(review_job.id)
    monkeypatch.setattr(
        engine.service, "is_project_owner_review_current", lambda _review_id: True
    )

    async def require_choice(_owner, _project, **_kwargs):
        return ProjectOwnerResult(
            outcome="require_user",
            decision="下一步优先稳定性还是视觉效果？",
            rationale="两项都合理，需要用户偏好。",
            impact="只影响下一步顺序。",
            reversible=False,
            confidence="low",
            answers={},
            context_refs=("visible-report",),
            user_options=(
                {"label": "优先稳定性", "description": "先修复已知故障"},
                {"label": "优先视觉效果", "description": "先完善页面体验"},
            ),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(
        "mac_system.core.project_owner.ProjectOwner.review_report", require_choice
    )
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_root_turn",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._review_delivered_report(
        project,
        turn_id="turn-owner-choice",
        title="本轮结果",
        content="稳定性和视觉效果都可以继续优化。",
        review_id=review_job.id,
    )

    decision = session.exec(select(ProjectDecision)).one()
    assert decision.status == "requires_user"
    assert [item["label"] for item in decision.options] == [
        "优先稳定性",
        "优先视觉效果",
    ]
    assert scheduled == []
    assert len(bridge.cards) == 2
    assert len(bridge.updates) == 1
    buttons = [
        node
        for node in bridge.cards[-1][2]["body"]["elements"]
        if node.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == [
        "优先稳定性",
        "优先视觉效果",
    ]


@pytest.mark.asyncio
async def test_disabled_owner_skips_report_review_queue(root_context, monkeypatch):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    engine.service.set_project_owner_enabled(project.id, False)
    scheduled = []
    monkeypatch.setattr(
        root_agent_module,
        "schedule_project_owner_review",
        lambda *args, **kwargs: scheduled.append((args, kwargs)),
    )

    await engine._queue_delivered_report_review(
        project,
        turn_id="turn-owner-disabled",
        title="本轮结果",
        content="任务已经完成。",
        user_request="完成任务",
    )

    assert session.exec(select(ProjectOwnerReview)).all() == []
    assert scheduled == []


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

    class FakeRoot(_FakeLeadBindings):
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
async def test_project_owner_answers_native_question_and_publishes_decision_card(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    await deliver_card(
        engine.service,
        bridge,
        dedupe_key="public-result-before-owner",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("上一轮结果", "现有界面采用蓝色简洁风格。"),
        project_id=project.id,
    )

    async def decide(_project, _questions):
        return ProjectOwnerResult(
            outcome="decide",
            decision="继续沿用蓝色简洁风格",
            rationale="上一轮结果已经明确采用该风格，延续它不会改变用户目标。",
            impact="保持界面一致性。",
            reversible=True,
            confidence="high",
            answers={"style": "现代简洁"},
            context_refs=("notification-public",),
            authority_refs=("policy-style",),
            answer_source_refs={"style": ("policy-style",)},
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", decide)
    response = await engine._request_user_input(
        project.id,
        {
            "itemId": "request-owner-style",
            "questions": [{
                "id": "style",
                "header": "界面风格",
                "question": "继续用哪种风格？",
                "isOther": False,
                "isSecret": False,
                "options": [
                    {"label": "现代简洁", "description": "延续现有设计"},
                    {"label": "未来科技", "description": "整体重做"},
                ],
            }],
        },
    )

    assert response == {"answers": {"style": {"answers": ["现代简洁"]}}}
    decision = session.exec(select(ProjectDecision)).one()
    assert decision.status == "applied"
    assert decision.card_message_id == "om-3"
    assert decision.answer_payload == response
    assert decision.authority_refs == ["policy-style"]
    answer = session.exec(select(QuestionAnswer)).one()
    assert answer.source_refs == ["policy-style"]
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.card_message_id == "om-2"
    assert interaction.requested_to == "user"
    assert interaction.escalated_to_user is True
    assert interaction.resolution_source == "project_owner"
    original_question = str(bridge.cards[1][2])
    assert "需要确认 1 个问题" in original_question
    assert "继续用哪种风格" in original_question
    rendered = str(bridge.cards[-1][2])
    assert "AI 决策记录" in rendered
    assert "继续沿用蓝色简洁风格" in rendered
    assert "调整决定" in rendered
    assert bridge.deleted == ["om-2"]
    assert bridge.updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_kind", ["commandExecution", "fileChange", "permissions"])
async def test_project_owner_can_approve_native_requests(root_context, monkeypatch, approval_kind):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    owner_started = asyncio.Event()
    owner_continue = asyncio.Event()
    requested = {"fileSystem": {"write": ["/workspace/.git"]}, "network": {"enabled": True}}
    params = {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "exec-git-add",
        "command": "git add -A", "cwd": project.workspace_path,
        "reason": "需要写入受保护的 .git/index",
        "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
    }
    expected_choice = "允许本次"
    expected_response = {"decision": "accept"}
    if approval_kind == "fileChange":
        params = {
            "threadId": "child-thread", "turnId": "child-turn", "itemId": "patch-1",
            "reason": None, "grantRoot": None,
        }
        engine.store.append_event(project.id, root_agent_module._durable_native_event(AgentEvent(
            kind="item/started", thread_id="child-thread", turn_id="child-turn",
            payload={"item": {
                "id": "patch-1", "type": "fileChange", "status": "inProgress",
                "changes": [{"path": "/private/tmp/dh-source-manifest.patch",
                             "kind": {"type": "add"}, "diff": "PRIVATE_SOURCE_CONTENT"}],
            }},
        )))
    elif approval_kind == "permissions":
        params["permissions"] = requested
        expected_choice = "本会话允许"
        expected_response = {"permissions": requested, "scope": "session"}
    else:
        params["additionalPermissions"] = requested
        params["networkApprovalContext"] = {"host": "packages.example.test", "protocol": "https"}
    engine.feishu_bridge = RecordingBridge()

    async def decide(_project, questions):
        reason = questions[0]["reason"]
        if approval_kind == "fileChange":
            assert "新增：/private/tmp/dh-source-manifest.patch" in reason
            assert "PRIVATE_SOURCE_CONTENT" not in str(questions)
        else:
            assert "/workspace/.git" in reason
            assert '"network": {"enabled": true}' in reason
            if approval_kind == "commandExecution":
                assert "git add -A" in questions[0]["question"]
                assert "packages.example.test" in reason
        assert reason in str(engine.feishu_bridge.cards[0][2]).replace("\\n", "\n")
        owner_started.set()
        await owner_continue.wait()
        return ProjectOwnerResult(
            outcome="decide",
            decision="允许本次 Git 暂存",
            rationale="这是当前项目任务内可撤销的常规 Git 操作。",
            impact="Codex 可以更新当前仓库索引。",
            reversible=True,
            confidence="high",
            answers={"approval_decision": expected_choice},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", decide)
    request_task = asyncio.create_task(
        engine._request_native_approval(
            project.id,
            f"item/{approval_kind}/requestApproval",
            params,
        )
    )
    await asyncio.wait_for(owner_started.wait(), timeout=1)
    session.expire_all()
    assert session.get(Project, project.id).execution_status == "owner_reviewing"
    assert session.exec(select(RootAgentSession)).one().status == "owner_reviewing"
    reviewing = session.exec(select(QuestionInteraction)).one()
    assert reviewing.requested_to == "project_owner"
    assert reviewing.escalated_to_user is False
    first_card = engine.feishu_bridge.cards[0][2]
    assert first_card["header"]["title"]["content"] == "Owner 审阅中"
    assert "提交答案" not in str(first_card)
    owner_continue.set()
    response = await asyncio.wait_for(request_task, timeout=1)

    assert response == expected_response
    decision = session.exec(select(ProjectDecision)).one()
    assert decision.answer_payload == response
    assert decision.status == "applied"
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.stage == "native_approval"
    assert interaction.resolution_source == "project_owner"
    assert len(engine.feishu_bridge.cards) == 2
    owner_decision = str(engine.feishu_bridge.cards[-1][2])
    assert "Project Owner" in owner_decision
    assert "允许本次 Git 暂存" in owner_decision
    assert engine.feishu_bridge.deleted == [interaction.card_message_id]
    assert engine.feishu_bridge.updates == []
    assert decision.card_message_id != interaction.card_message_id


@pytest.mark.asyncio
async def test_native_approval_resumes_after_outbox_sends_initially_failed_card(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    monkeypatch.setattr(
        root_agent_module, "_NATIVE_INTERACTION_CARD_RETRY_SECONDS", 0.01
    )
    first_send_failed = asyncio.Event()

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append(card)
            if len(self.cards) == 1:
                first_send_failed.set()
                raise httpx.ConnectError("temporary network outage")
            return f"om-{len(self.cards)}"

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    engine.feishu_bridge = RecordingBridge()
    owner_calls = []

    async def decide(_project, _questions):
        owner_calls.append(True)
        return ProjectOwnerResult(
            outcome="decide",
            decision="允许只读检查",
            rationale="只读取当前进程状态。",
            impact="Codex 可以继续当前检查。",
            reversible=True,
            confidence="high",
            answers={"approval_decision": "允许本次"},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", decide)
    request_task = asyncio.create_task(
        engine._request_native_approval(
            project.id,
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-retried-card",
                "command": "ps aux",
                "cwd": project.workspace_path,
                "availableDecisions": ["accept", "cancel"],
            },
        )
    )
    await asyncio.wait_for(first_send_failed.wait(), timeout=1)
    assert owner_calls == []
    notification = session.exec(
        select(NotificationOutbox).where(NotificationOutbox.intent_type == "question")
    ).one()
    assert notification.status == "failed"
    engine.service.finish_notification(
        notification.id, provider_message_id="om-outbox-retry"
    )

    assert await asyncio.wait_for(request_task, timeout=2) == {
        "decision": "accept"
    }
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.card_message_id == "om-outbox-retry"
    assert interaction.resolution_source == "project_owner"
    assert owner_calls == [True]
    assert len([card for card in engine.feishu_bridge.cards if card["header"]["title"]["content"] == "Owner 审阅中"]) == 1


@pytest.mark.asyncio
async def test_native_approval_stops_if_outbox_marks_card_delivery_terminal(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    monkeypatch.setattr(
        root_agent_module, "_NATIVE_INTERACTION_CARD_RETRY_SECONDS", 0.01
    )
    first_send_failed = asyncio.Event()

    class BrokenBridge:
        async def _send_interactive(self, *_args, **_kwargs):
            first_send_failed.set()
            raise httpx.ConnectError("temporary network outage")

    engine.feishu_bridge = BrokenBridge()
    request_task = asyncio.create_task(
        engine._request_native_approval(
            project.id,
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-terminal-card",
                "command": "ps aux",
                "cwd": project.workspace_path,
                "availableDecisions": ["accept", "cancel"],
            },
        )
    )
    await asyncio.wait_for(first_send_failed.wait(), timeout=1)
    notification = session.exec(
        select(NotificationOutbox).where(NotificationOutbox.intent_type == "question")
    ).one()
    engine.service.finish_notification(
        notification.id, error="provider rejected card", retryable=False
    )
    with pytest.raises(RuntimeError, match="permanently failed"):
        await asyncio.wait_for(request_task, timeout=2)
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.status == "expired"
    assert interaction.resolution_source == "card_delivery_interrupted"


@pytest.mark.asyncio
async def test_native_approval_becomes_user_task_only_after_owner_defers(
    root_context, monkeypatch
):
    session, engine = root_context
    card_updated = asyncio.Event()

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))
            card_updated.set()

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()

    async def defer(_project, _questions):
        return ProjectOwnerResult(
            outcome="defer",
            decision="需要你确认是否允许这次系统进程检查",
            rationale="当前历史没有这项主机权限授权。",
            impact="允许后 Codex 可以读取系统进程列表。",
            reversible=True,
            confidence="low",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", defer)
    request_task = asyncio.create_task(
        engine._request_native_approval(
            project.id,
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "exec-owner-defer",
                "command": "ps aux",
                "cwd": project.workspace_path,
                "reason": "需要查看系统进程",
                "availableDecisions": ["accept", "cancel"],
            },
        )
    )
    await asyncio.wait_for(card_updated.wait(), timeout=1)

    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.requested_to == "user"
    assert interaction.escalated_to_user is True
    assert engine.feishu_bridge.cards[0][2]["header"]["title"]["content"] == (
        "Owner 审阅中"
    )
    user_card = engine.feishu_bridge.updates[-1][1]
    assert user_card["header"]["title"]["content"] == "需要确认 1 个问题"
    assert "需要你确认是否允许这次系统进程检查" in str(user_card)
    assert "提交答案" in str(user_card)

    claim = "user:test"
    assert engine.service.claim_question_interaction(
        interaction.id, resolution_source=claim
    )
    question = engine.service.get_questions_for_interaction(interaction.id)[0]
    await engine.service.submit_question_answer(
        question.id,
        "允许本次",
        "允许本次",
        False,
        "test-user",
        answer_source="user",
        commit=False,
    )
    assert engine.service.complete_question_interaction_claim(
        interaction.id,
        claimed_by=claim,
        resolution_source="user",
        commit=False,
    )
    session.commit()
    assert resolve_root_user_input(engine.service, interaction.id)
    assert await asyncio.wait_for(request_task, timeout=1) == {"decision": "accept"}


@pytest.mark.asyncio
async def test_project_owner_can_apply_native_execpolicy_amendment(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    amendment = {
        "acceptWithExecpolicyAmendment": {
            "execpolicy_amendment": ["python", "-m", "pytest"],
        }
    }

    async def decide(_project, questions):
        assert any(
            option["label"] == "允许规则：python -m pytest"
            for option in questions[0]["options"]
        )
        return ProjectOwnerResult(
            outcome="decide",
            decision="本会话允许项目内同类测试命令",
            rationale="这是当前任务中会重复执行的低风险验证命令。",
            impact="后续匹配命令无需重复审批。",
            reversible=True,
            confidence="high",
            answers={"approval_decision": "允许规则：python -m pytest"},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", decide)
    response = await engine._request_native_approval(
        project.id,
        "item/commandExecution/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "exec-tests",
            "command": "python -m pytest -q",
            "cwd": project.workspace_path,
            "reason": "运行项目回归测试",
            "availableDecisions": ["accept", amendment, "cancel"],
        },
    )

    assert response == {"decision": amendment}
    decision = session.exec(select(ProjectDecision)).one()
    assert decision.answer_payload == response
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.resolution_source == "project_owner"
    assert "本会话允许项目内同类测试命令" in str(
        engine.feishu_bridge.cards[-1][2]
    )


@pytest.mark.asyncio
async def test_resolved_approval_starts_new_progress_card_below_decision(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []
            self.deleted = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

        async def delete_message(self, message_id):
            self.deleted.append(message_id)

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()
    engine._progress_min_delivery_interval = 0
    state = _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=100.0,
        last_event_at=datetime.now().astimezone(),
        label="正在检查项目",
    )
    _start_progress_big_step(state, "检查现有实现")
    engine._progress[project.id] = state
    await engine._publish_progress_snapshot(project.id, event_identity="initial")
    original_key = root_agent_module._progress_projection_key(project.id, state)

    async def decide(_project, _questions):
        return ProjectOwnerResult(
            outcome="decide",
            decision="本会话允许同类测试命令",
            rationale="当前持续任务会重复运行同类低风险检查。",
            impact="后续匹配命令不再重复询问。",
            reversible=True,
            confidence="high",
            answers={"approval_decision": "允许规则：python -m pytest"},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", decide)
    amendment = {
        "acceptWithExecpolicyAmendment": {
            "execpolicy_amendment": ["python", "-m", "pytest"],
        }
    }
    response = await engine._request_native_approval(
        project.id,
        "item/commandExecution/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "exec-tests-timeline",
            "command": "python -m pytest -q",
            "cwd": project.workspace_path,
            "reason": "运行回归测试",
            "availableDecisions": ["accept", amendment, "cancel"],
        },
    )

    assert response == {"decision": amendment}
    assert state.projection_segment == 1
    assert state.projection_suspended is False
    assert engine.service.get_card_projection(original_key).lifecycle_status == (
        "archived_for_interaction"
    )
    owner_decision = str(engine.feishu_bridge.cards[-1][2])
    assert "AI 决策记录" in owner_decision
    assert "本会话允许同类测试命令" in owner_decision
    assert engine.feishu_bridge.deleted == ["om-2"]
    progress_title = f"[{engine._project_display_name(project)}] 项目进展"
    assert [str(card[2]["header"]["title"]["content"]) for card in engine.feishu_bridge.cards] == [
        progress_title,
        "Owner 审阅中",
        "AI 决策记录",
    ]

    await engine._project_native_activity(
        project.id,
        AgentEvent(
            "item/started",
            "thread-1",
            "turn-1",
            {
                "item": {
                    "type": "commandExecution",
                    "id": "exec-after-approval",
                    "status": "inProgress",
                    "cwd": project.workspace_path,
                    "command": "python -m pytest -q",
                }
            },
        ),
    )
    await engine._flush_progress_snapshots(project.id)

    assert [str(card[2]["header"]["title"]["content"]) for card in engine.feishu_bridge.cards] == [
        progress_title,
        "Owner 审阅中",
        "AI 决策记录",
        progress_title,
    ]
    assert engine.feishu_bridge.updates == []


@pytest.mark.asyncio
async def test_project_owner_defer_preserves_native_user_question(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return f"om-{len(self.cards)}"

        async def update_card(self, _message_id, _card):
            return None

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    bridge = RecordingBridge()
    engine.feishu_bridge = bridge
    await deliver_card(
        engine.service,
        bridge,
        dedupe_key="public-result-before-owner-defer",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        card=build_content_card("上一轮结果", "项目已经准备完成。"),
        project_id=project.id,
    )

    async def defer(_project, _questions):
        return ProjectOwnerResult(
            outcome="defer",
            decision="需要用户选择发布位置",
            rationale="这是外部发布授权。",
            impact="会让项目公开可访问。",
            reversible=False,
            confidence="high",
            answers={},
            context_refs=(),
            model="gpt-owner-test",
        )

    monkeypatch.setattr(engine, "_project_owner_decide", defer)
    response = await engine._request_user_input(
        project.id,
        {
            "itemId": "request-owner-defer",
            "autoResolutionMs": 10,
            "questions": [{
                "id": "publish",
                "header": "发布位置",
                "question": "要发布到公网吗？",
                "isOther": False,
                "isSecret": False,
                "options": [
                    {"label": "仅本机", "description": "不公开"},
                    {"label": "公网", "description": "公开访问"},
                ],
            }],
        },
    )

    assert response == {"answers": {}}
    assert session.exec(select(ProjectDecision)).all() == []
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.requested_to == "user"
    assert interaction.escalated_to_user is True


@pytest.mark.asyncio
async def test_project_owner_card_is_marked_failed_when_native_answer_cannot_persist(
    root_context, monkeypatch
):
    session, engine = root_context

    class RecordingBridge:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
            self.cards.append((recipient, receive_id_type, card))
            return "om-decision"

        async def update_card(self, message_id, card):
            self.updates.append((message_id, card))

    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    engine.feishu_bridge = RecordingBridge()

    interaction = await engine.service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "root:request-owner-failed-apply",
        [{
            "question_id": "style",
            "question": "继续使用哪种风格？",
            "options": [{"key": "现代简洁", "label": "现代简洁"}],
            "sensitivity": "internal",
        }],
        request_item_id="request-owner-failed-apply",
    )
    interaction.card_message_id = "om-question"
    session.add(interaction)
    session.commit()

    async def fail_answer(*_args, **_kwargs):
        raise RuntimeError("persistence unavailable")

    monkeypatch.setattr(
        engine.service, "submit_question_answer", fail_answer
    )
    response = await engine._apply_project_owner_decision(
        project,
        "request-owner-failed-apply",
        [{
            "question_id": "style",
            "question": "继续使用哪种风格？",
            "options": [{"label": "现代简洁"}],
            "sensitivity": "internal",
        }],
        ProjectOwnerResult(
            outcome="decide",
            decision="继续沿用现代简洁风格",
            rationale="现有结果已经采用该风格。",
            impact="保持一致。",
            reversible=True,
            confidence="high",
            answers={"style": "现代简洁"},
            context_refs=("visible-result",),
            model="gpt-owner-test",
        ),
        interaction,
    )

    assert response is None
    decision = session.exec(select(ProjectDecision)).one()
    assert decision.status == "application_failed"
    assert session.get(QuestionInteraction, interaction.id).status == "pending"
    failed_card = engine.feishu_bridge.cards[-1][2]
    assert failed_card["header"]["template"] == "red"
    assert "已改为询问你" in str(failed_card)


@pytest.mark.asyncio
async def test_native_question_resumes_after_outbox_sends_initially_failed_card(
    root_context, monkeypatch
):
    session, engine = root_context
    project = session.get(Project, "proj-root")
    project.feishu_chat_id = "oc-chat"
    session.add(project)
    session.commit()
    monkeypatch.setattr(
        root_agent_module, "_NATIVE_INTERACTION_CARD_RETRY_SECONDS", 0.01
    )
    first_send_failed = asyncio.Event()

    class BrokenOnceBridge:
        def __init__(self):
            self.sends = 0

        async def _send_interactive(self, *_args, **_kwargs):
            self.sends += 1
            first_send_failed.set()
            raise httpx.ConnectError("temporary network outage")

    engine.feishu_bridge = BrokenOnceBridge()
    request_task = asyncio.create_task(
        engine._request_user_input(
            project.id,
            {
                "itemId": "question-retried-card",
                "questions": [{
                    "id": "color",
                    "question": "选择颜色",
                    "isSecret": True,
                    "options": [{"label": "blue", "description": "蓝色"}],
                }],
            },
        )
    )
    await asyncio.wait_for(first_send_failed.wait(), timeout=1)
    notification = session.exec(
        select(NotificationOutbox).where(NotificationOutbox.intent_type == "question")
    ).one()
    assert notification.status == "failed"
    engine.service.finish_notification(
        notification.id, provider_message_id="om-question-retry"
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        interaction = session.exec(select(QuestionInteraction)).one()
        if interaction.card_message_id:
            break
    else:
        raise AssertionError("retried native question card did not resume")
    question = engine.service.get_questions_for_interaction(interaction.id)[0]
    await engine.service.submit_question_answer(
        question.id, "blue", "blue", False, "ou-user"
    )
    await engine.service.resolve_question_interaction(interaction.id)
    assert resolve_root_user_input(engine.service, interaction.id)
    assert await asyncio.wait_for(request_task, timeout=1) == {
        "answers": {"color": {"answers": ["blue"]}}
    }
    assert engine.feishu_bridge.sends == 1


@pytest.mark.asyncio
async def test_terminal_question_card_delivery_failure_does_not_hang_codex(
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

    with pytest.raises(RuntimeError, match="card transport unavailable"):
        await engine._request_user_input(
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
    interaction = session.exec(select(QuestionInteraction)).one()
    assert interaction.status == "expired"
    assert interaction.resolution_source == "card_delivery_interrupted"
    assert session.exec(select(QuestionAnswer)).all() == []
    assert resolve_root_user_input(ProjectService(session), interaction.id) is False


def test_extension_tool_list_does_not_reimplement_codex_multi_agent(root_context):
    _, engine = root_context
    project = engine.service.require_project("proj-root")
    names = [tool.name for tool in engine._extension_tools(project)]
    assert names == [
        "read_project_contract",
        "list_external_agents",
        "run_external_agent",
        "run_external_agents",
    ]
    assert not ({"spawn_agent", "send_message", "wait", "reviewer"} & set(names))


@pytest.mark.asyncio
async def test_external_tool_exposes_live_provider_evidence_without_routing_rules(
    root_context,
):
    _, engine = root_context
    project = engine.service.require_project("proj-root")
    tools = {tool.name: tool for tool in engine._extension_tools(project)}
    catalog = tools["list_external_agents"]
    run = tools["run_external_agent"]
    run_parallel = tools["run_external_agents"]
    evidence = await catalog.handler(
        {}, ToolContext("thread-1", "turn-1", "call-list", catalog.name)
    )

    assert evidence["providers"][0]["id"] == "FakeRuntime"
    assert "no recent execution samples" in evidence["selection_guidance"]
    assert "inspect the returned evidence" in evidence["selection_guidance"]
    assert "blocker shared by every agent" in evidence["selection_guidance"]
    assert "minimal sufficient task package" in evidence["selection_guidance"]
    assert "Do not try every provider" in evidence["selection_guidance"]
    assert "selection_reason" in run.input_schema["required"]
    assert "timeout_seconds" in run.input_schema["required"]
    assert "enum" not in run.input_schema["properties"]["provider"]
    reason_description = run.input_schema["properties"]["selection_reason"][
        "description"
    ]
    assert "same language as the user's request" in reason_description
    assert "avoid internal routing or protocol jargon" in reason_description
    assert (
        run.input_schema["properties"]["timeout_seconds"]["maximum"]
        == 86400
    )
    assert run_parallel.input_schema["properties"]["agents"]["minItems"] == 2
    assert run_parallel.input_schema["properties"]["agents"]["maxItems"] == 4


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
    assert state.detail == "工具：bash"
    body = _progress_card_body(state)
    assert "当前执行：Pi" in body
    assert "… Pi · 后端未报告 · 运行中" in body
    assert "原因：需要宿主自动化完成部署" in body
    assert "交给 Pi 独立处理" in body
    assert "服务已重启，正在核对健康检查" in body
    assert len(state.big_steps) == 1
    assert body.count("需要宿主自动化完成部署") == 1

    await engine._project_external_agent_activity(
        "proj-root",
        "agent-1",
        "pi",
        "需要宿主自动化完成部署",
        "Pi 已完成外部任务，结果已返回 Codex",
        step_status="completed",
        usage={
            "input_tokens": 40,
            "cache_read_input_tokens": 30,
            "output_tokens": 10,
            "total_tokens": 80,
        },
        usage_final=True,
    )
    await engine._flush_progress_snapshots("proj-root")

    body = _progress_card_body(state)
    assert "Token · Pi：80（输入 40；缓存读取 30；输出 10）" in body
    assert "Token · Pi：后端未报告" not in body


@pytest.mark.asyncio
async def test_parallel_external_agents_keep_independent_steps_and_compact_summary(
    root_context,
):
    _session, engine = root_context

    await engine._project_external_agent_activity(
        "proj-root",
        "agent-pi",
        "pi",
        "独立检查接口",
        "Pi 正在验证接口",
        observed_detail="工具：bash",
    )
    await engine._project_external_agent_activity(
        "proj-root",
        "agent-kimi",
        "kimi_code",
        "并行检查界面",
        "Kimi Code 正在验证界面",
        observed_detail="工具：Read",
    )
    await engine._project_external_agent_activity(
        "proj-root",
        "agent-pi",
        "pi",
        "独立检查接口",
        "Pi 已完成外部任务，结果已返回 Codex",
        step_status="completed",
        usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        usage_final=True,
    )
    await engine._flush_progress_snapshots("proj-root")

    state = engine._progress["proj-root"]
    view = root_agent_module._progress_card_view(state)
    body = _progress_card_body(state)

    assert "执行链：Codex → 并行（Pi、Kimi Code）" in body
    assert "并行执行：2 个 Agent · 1 运行中 · 1 已完成" in body
    assert "✓ Pi" in body
    assert "… Kimi Code" in body
    assert view.details[0][0] == "Agent 与用量详情"
    assert view.details[0][2] is False
    pi_step = next(step for step in state.big_steps if step.agent_id == "external:agent-pi")
    kimi_step = next(
        step for step in state.big_steps if step.agent_id == "external:agent-kimi"
    )
    assert pi_step.status == "completed"
    assert kimi_step.status == "running"
    assert any("Pi 已完成" in step.label for step in pi_step.small_steps)
    assert all("Pi 已完成" not in step.label for step in kimi_step.small_steps)


@pytest.mark.asyncio
async def test_project_provider_pin_filters_and_enforces_external_calls(root_context):
    session, engine = root_context

    class ProviderRuntime(FakeRuntime):
        def available_providers(self):
            return {'codex', 'kimi_code'}

        def provider_profiles(self):
            return {'codex': {'model': 'codex-model'}, 'kimi_code': {'model': 'kimi-model'}}

    engine.runtime = ProviderRuntime({})
    project = engine.service.require_project('proj-root')
    project.context = {**(project.context or {}), 'execution_provider': 'codex'}
    session.add(project)
    session.commit()
    tools = {tool.name: tool for tool in engine._extension_tools(project)}
    catalog = await tools['list_external_agents'].handler(
        {}, ToolContext('thread', 'turn', 'catalog', 'list_external_agents')
    )
    assert [provider['id'] for provider in catalog['providers']] == ['codex']
    assert 'kimi' not in str(catalog).lower()
    arguments = {
        'provider': 'kimi_code', 'selection_reason': '检查接口',
        'task_name': 'check', 'message': '检查接口', 'access': 'read_only',
        'timeout_seconds': 60,
    }
    with pytest.raises(ValueError, match='not currently available'):
        await tools['run_external_agent'].handler(
            arguments, ToolContext('thread', 'turn', 'blocked', 'run_external_agent')
        )
    assert session.exec(select(AgentSession)).all() == []
    arguments['provider'] = 'codex'
    result = await tools['run_external_agent'].handler(
        arguments, ToolContext('thread', 'turn', 'allowed', 'run_external_agent')
    )
    assert result['success'] is True and result['provider'] == 'codex'


@pytest.mark.asyncio
async def test_external_provider_is_one_explicit_tool_call(root_context):
    session, engine = root_context
    project = engine.service.require_project("proj-root")
    tool = next(
        tool
        for tool in engine._extension_tools(project)
        if tool.name == "run_external_agent"
    )
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
    catalog = next(
        tool
        for tool in engine._extension_tools(project)
        if tool.name == "list_external_agents"
    )
    evidence = await catalog.handler(
        {}, ToolContext("thread-1", "turn-1", "call-list-after", catalog.name)
    )
    assert "recent CLI completion 1/1" in evidence["selection_guidance"]


@pytest.mark.asyncio
async def test_external_provider_batch_really_executes_concurrently(root_context):
    session, engine = root_context

    class ParallelRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({})
            self.active = 0
            self.max_active = 0
            self.both_started = asyncio.Event()

        async def execute(self, *args, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 2:
                self.both_started.set()
            await self.both_started.wait()
            await asyncio.sleep(0)
            self.active -= 1
            return AgentRuntimeResult(
                success=True,
                output_json=None,
                output_files=[],
                logs="",
                cost_info=None,
                tool_calls=[],
                raw_result=f"完成 {kwargs['task_prompt']}",
            )

    runtime = ParallelRuntime()
    engine.runtime = runtime
    project = engine.service.require_project("proj-root")
    tool = next(
        tool
        for tool in engine._extension_tools(project)
        if tool.name == "run_external_agents"
    )
    result = await asyncio.wait_for(
        tool.handler(
            {
                "agents": [
                    {
                        "provider": "ParallelRuntime",
                        "selection_reason": "并行检查接口",
                        "task_name": "parallel-api",
                        "message": "检查接口",
                        "access": "read_only",
                        "timeout_seconds": 60,
                    },
                    {
                        "provider": "ParallelRuntime",
                        "selection_reason": "并行检查界面",
                        "task_name": "parallel-ui",
                        "message": "检查界面",
                        "access": "read_only",
                        "timeout_seconds": 60,
                    },
                ]
            },
            ToolContext("thread-1", "turn-1", "call-batch", tool.name),
        ),
        timeout=2,
    )

    assert runtime.max_active == 2
    assert result["parallel"] is True
    assert result["success"] is True
    assert {item["task_name"] for item in result["results"]} == {
        "parallel-api",
        "parallel-ui",
    }
    agents = session.exec(select(AgentSession)).all()
    assert len(agents) == 2
    assert all(agent.status == "completed" for agent in agents)


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
    tool = next(
        tool
        for tool in engine._extension_tools(project)
        if tool.name == "run_external_agent"
    )
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
