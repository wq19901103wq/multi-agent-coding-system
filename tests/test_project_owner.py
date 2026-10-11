import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import mac_system.core.project_owner as project_owner_module
from mac_system.agents.root_runtime import AgentEvent, TurnResult
from mac_system.config import Settings
from mac_system.core.project_owner import (
    OwnerOutputValidationError,
    ProjectOwner,
    ProjectOwnerRuntimeContext,
    ProjectOwnerRuntimeRegistry,
    _select_relevant_decision_cases,
    _owner_runtime_dir,
    _turn_failure_message,
    _visible_card_text,
    _submitted_links,
    _webpage_text,
    public_project_context,
)
from mac_system.core.project_service import ProjectService
from mac_system.core.root_sessions import RootSessionStore
from mac_system.models import EventLog, Project, ProjectUser


@pytest.fixture
def owner_context(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Session(engine) as session:
        project = Project(
            id="proj-owner",
            name="测试项目",
            task_type="feature",
            description="test",
            workspace_path=str(workspace),
        )
        session.add(project)
        session.commit()
        yield ProjectService(session), project, workspace


def _question(*, secret: bool = False):
    return {
        "question_id": "style",
        "question": "继续使用哪种风格？",
        "options": [
            {"label": "现代简洁", "description": "延续现有设计"},
            {"label": "未来科技", "description": "整体重做"},
        ],
        "sensitivity": "secret" if secret else "internal",
    }


def _submission(**overrides):
    value = {
        "outcome": "decide",
        "decision": "沿用现代简洁风格",
        "rationale": "用户可见的上一轮结果已经采用该风格。",
        "impact": "保持界面一致。",
        "reversible": True,
        "confidence": "high",
        "answers": [{"question_id": "style", "answer": "现代简洁"}],
        "user_options": [],
    }
    value.update(overrides)
    return value


def test_owner_turn_failure_message_preserves_native_provider_error():
    result = TurnResult(
        thread_id="thread-owner",
        turn_id="turn-owner",
        status="failed",
        final_message=None,
        events=(
            AgentEvent(
                kind="turn/completed",
                thread_id="thread-owner",
                turn_id="turn-owner",
                payload={
                    "turn": {
                        "status": "failed",
                        "error": {"message": "model temporarily unavailable"},
                    }
                },
            ),
        ),
    )

    assert _turn_failure_message(result) == "model temporarily unavailable"


def test_owner_link_allowlist_extracts_only_submitted_http_links():
    links = _submitted_links([
        "参考 https://example.com/design。",
        "另一个 http://localhost:8000/path?x=1",
        "不是链接 example.com",
    ])

    assert links == {
        "https://example.com/design",
        "http://localhost:8000/path?x=1",
    }


def test_owner_retrieves_similar_decisions_and_marks_corrections_as_counterexamples():
    cases = [
        {
            "time": "2026-08-01T10:00:00",
            "project_id": "other",
            "question": "界面应该保持简洁还是增加更多装饰？",
            "options": [{"label": "保持简洁", "description": "减少视觉噪声"}],
            "user_choice": "保持简洁",
            "rejected_owner_decision": "增加复杂动画",
            "is_correction": True,
            "source_ref": "decision-corrected",
        },
        {
            "time": "2026-08-02T10:00:00",
            "project_id": "other",
            "question": "数据库选 SQLite 还是 Postgres？",
            "options": [{"label": "SQLite", "description": "本机使用"}],
            "user_choice": "SQLite",
            "rejected_owner_decision": "",
            "is_correction": False,
            "source_ref": "decision-unrelated",
        },
    ]

    selected = _select_relevant_decision_cases(
        cases,
        query="这次界面也要保持简洁，不要增加复杂动画",
        project_id="current",
    )

    assert [item["source_ref"] for item in selected] == ["decision-corrected"]
    assert selected[0]["evidence_role"] == "correction_counterexample"
    assert selected[0]["rejected_owner_decision"] == "增加复杂动画"


def test_owner_webpage_reader_strips_html_and_rejects_binary_content():
    import httpx

    response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><style>hidden</style><body><h1>设计原则</h1><p>保持简单</p></body></html>",
    )
    assert _webpage_text(response) == "设计原则 保持简单"

    binary = httpx.Response(
        200,
        headers={"content-type": "image/png"},
        content=b"png",
    )
    with pytest.raises(ValueError, match="文本"):
        _webpage_text(binary)


def test_owner_retrieves_choices_by_their_original_context():
    case = {
        "project_id": "current", "question": "接下来做什么？",
        "options": [{"label": "A", "description": ""}],
        "user_choice": "A", "is_correction": False,
        "source_ref": "context-choice",
        "context": {"rationale": "先保证手机布局可读，不增加动画干扰。"},
    }

    selected = _select_relevant_decision_cases(
        [case], query="手机布局是否清楚可读", project_id="current",
    )

    assert selected[0]["source_ref"] == "context-choice"
    assert selected[0]["evidence_role"] == "historical_choice_example"
    assert selected[0]["context"] == case["context"]


@pytest.mark.asyncio
async def test_next_owner_review_reads_saved_choice_context(owner_context, monkeypatch, tmp_path):
    service, project, _workspace = owner_context
    service.session.add(ProjectUser(project_id=project.id, open_id="ou-choice", role="owner"))
    service.session.commit()
    decision = service.create_project_decision(
        project.id,
        request_item_id="report:layout",
        native_turn_id="turn-layout",
        question="下一步怎么推进？",
        options=[
            {"label": "改善手机布局", "description": "让手机阅读更清楚", "action": "execute"},
            {"label": "增加动画", "description": "增强表现力", "action": "execute"},
        ],
        decision="改善手机布局",
        rationale="用户主要用手机阅读",
        impact="不增加新功能",
        reversible=True,
        confidence="high",
        answer_payload={
            "human_context": [{"text": "先让我选择下一步", "source_ref": "human:1"}],
            "source_report": {"title": "已可用", "content": "手机阅读还不够清楚"},
        },
        context_refs=[],
        actor_model="owner-model",
        decision_type="advice",
    )
    service.update_project_decision(
        decision.id, status="resolved_by_user", user_override="改善手机布局",
        user_choice_actor="ou-choice",
    )
    captured = {}

    class ChoiceOwnerThread:
        def __init__(self, **kwargs):
            self.tools = {tool.name: tool for tool in kwargs["tools"]}
            self.active_model = "owner-model"

        async def create_thread(self):
            return "choice-owner-thread"

        async def run_turn(self, prompt, *, output_schema=None):
            captured["payload"] = project_owner_module.json.loads(prompt.split("数据：\n", 1)[1])
            captured["preferences"] = self.tools["read_user_preferences"].handler({"limit": 10})
            return TurnResult(
                thread_id="choice-owner-thread", turn_id="choice-owner-turn",
                status="completed", events=(),
                final_message=project_owner_module.json.dumps({
                    "outcome": "advise", "decision": "优先保持手机阅读清楚",
                    "rationale": "历史选择支持先改善阅读体验，而不是增加动画。",
                    "impact": "选择前不继续修改", "reversible": True,
                    "confidence": "medium", "answers": [], "continue_execution": False,
                    "preference_observations": [], "project_policy_observations": [],
                    "user_options": [
                        {"label": "继续改善阅读", "description": "进一步整理信息层级", "action": "execute"},
                        {"label": "保持现状", "description": "保留当前可用结果", "action": "record_only"},
                    ],
                }),
            )

        async def close(self):
            return None

    monkeypatch.setattr(project_owner_module, "CodexAppServerThread", ChoiceOwnerThread)
    result = await ProjectOwner(service, Settings(PROJECTS_DIR=tmp_path / "projects")).review_report(
        project, title="手机阅读结果", content="手机布局已改善，阅读流程已通过。",
        user_request="手机阅读完成后让我选择下一步",
    )

    assert result.outcome == "advise"
    assert result.continue_execution is False
    case = captured["payload"]["authority"]["relevant_user_decision_cases"][0]
    assert case["evidence_role"] == "historical_choice_example"
    assert case["user_choice"] == "改善手机布局"
    assert case["rejected_owner_decision"] == ""
    assert case["context"]["source_report"]["content"] == "手机阅读还不够清楚"
    assert case["context"]["human_context"][0]["text"] == "先让我选择下一步"
    assert [option["label"] for option in case["options"]] == ["改善手机布局", "增加动画"]
    assert case["responded_by"] == "ou-choice"
    assert captured["preferences"]["choices"][0]["context"] == case["context"]
    assert service.get_project_policies(project.id) == []


@pytest.mark.asyncio
async def test_owner_retries_one_fresh_ephemeral_turn_after_empty_failure(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    attempts = 0

    class FlakyOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner"

        async def run_turn(self, _prompt, *, output_schema=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return TurnResult(
                    thread_id="thread-owner-1",
                    turn_id="turn-owner-1",
                    status="failed",
                    final_message=None,
                    events=(
                        AgentEvent(
                            kind="error",
                            thread_id="thread-owner-1",
                            turn_id="turn-owner-1",
                            payload={"error": {"message": "temporary capacity"}},
                        ),
                    ),
                )
            return TurnResult(
                thread_id="thread-owner-2",
                turn_id="turn-owner-2",
                status="completed",
                final_message=project_owner_module.json.dumps(
                    {
                        **_submission(),
                        "outcome": "defer",
                        "decision": "",
                        "rationale": "没有可回查的人类依据，保留原问题给用户。",
                        "impact": "原问题继续等待用户回答。",
                        "reversible": False,
                        "confidence": "low",
                        "answers": [],
                    }
                ),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", FlakyOwnerThread
    )

    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).decide(project, [_question()])

    assert attempts == 2
    # Retrying is transport recovery only. The second model response still
    # cannot answer a business choice without traceable evidence and sources.
    assert result.outcome == "defer"
    assert result.answers == {}


@pytest.mark.asyncio
async def test_owner_retries_invalid_semantics_without_rewriting_the_outcome(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    prompts = []

    class CorrectingOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner"

        async def run_turn(self, prompt, *, output_schema=None):
            prompts.append(prompt)
            payload = {
                "outcome": "advise",
                "decision": "下一步核对真实用户体验",
                "rationale": "当前结果只覆盖了内部验证。",
                "impact": "用户可以选择是否继续核对。",
                "reversible": True,
                "confidence": "medium",
                "answers": [],
                "authority_refs": [],
                "continue_execution": False,
                "user_options": (
                    []
                    if len(prompts) == 1
                    else [{
                        "label": "继续核对",
                        "description": "验证真实用户可见的完整体验。",
                        "action": "execute",
                    }]
                ),
                "preference_observations": [],
                "project_policy_observations": [],
            }
            return TurnResult(
                thread_id="thread-owner",
                turn_id=f"turn-owner-{len(prompts)}",
                status="completed",
                final_message=project_owner_module.json.dumps(payload),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", CorrectingOwnerThread
    )

    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).review_report(
        project,
        title="本轮结果",
        content="内部验证已经通过。",
        user_request="完成这一轮并给出下一步建议。",
    )

    assert result.outcome == "advise"
    assert [option["label"] for option in result.user_options] == ["继续核对"]
    assert len(prompts) == 2
    assert "上次输出未通过确定性边界校验" in prompts[1]


def test_visible_card_text_excludes_callback_values_and_internal_fields():
    card = {
        "header": {"title": {"tag": "plain_text", "content": "用户可见标题"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "用户可见结果"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "调整"},
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "action": "internal_action",
                            "token": "hidden-secret",
                        },
                    }],
                },
            ]
        },
        "internal_trace": "must-not-leak",
    }

    rendered = _visible_card_text(card)

    assert "用户可见标题" in rendered
    assert "用户可见结果" in rendered
    assert "调整" in rendered
    assert "internal_action" not in rendered
    assert "hidden-secret" not in rendered
    assert "must-not-leak" not in rendered


def test_public_context_only_contains_delivered_report_and_decision_cards(
    owner_context,
):
    service, project, _workspace = owner_context
    rows = []
    for key, intent, status, text in [
        ("sent-report", "report", "sent", "可见结果"),
        ("sent-decision", "decision", "sent", "可见决定"),
        ("sent-progress", "progress", "sent", "内部进度"),
        ("pending-report", "report", "pending", "未发送结果"),
        ("sent-question", "question", "sent", "用户问题"),
    ]:
        row = service.enqueue_notification(
            dedupe_key=key,
            recipient_id="oc-chat",
            recipient_type="chat_id",
            intent_type=intent,
            payload={"card": {"body": {"elements": [{"tag": "markdown", "content": text}]}}},
            project_id=project.id,
        )
        if status == "sent":
            row = service.claim_notification(row.id)
            row = service.finish_notification(row.id, provider_message_id=f"om-{key}")
        rows.append(row)

    context, refs = public_project_context(service, project)
    rendered = str(context)

    assert "可见结果" in rendered
    assert "可见决定" in rendered
    assert "内部进度" not in rendered
    assert "未发送结果" not in rendered
    assert "用户问题" not in rendered
    assert set(refs) == {rows[0].id, rows[1].id}


@pytest.mark.parametrize(
    "submitted,questions",
    [
        (_submission(reversible=False), [_question()]),
        (_submission(confidence="low"), [_question()]),
        (
            _submission(answers=[{"question_id": "style", "answer": "不存在的选项"}]),
            [_question()],
        ),
        (_submission(answers=[]), [_question()]),
        (_submission(), [_question(secret=True)]),
    ],
)
def test_project_owner_validation_rejects_unsafe_or_invalid_decisions(
    submitted, questions
):
    with pytest.raises(OwnerOutputValidationError):
        ProjectOwner._validated_result(
            submitted,
            questions,
            context_refs=("visible-card",),
            model="test-model",
        )


def test_project_owner_validation_accepts_complete_reversible_option():
    result = ProjectOwner._validated_result(
        _submission(),
        [_question()],
        context_refs=("visible-card",),
        model="test-model",
    )

    assert result.outcome == "decide"
    assert result.answers == {"style": "现代简洁"}
    assert result.context_refs == ("visible-card",)


def test_project_owner_question_answer_accepts_traceable_owner_reasoning():
    result = ProjectOwner._validated_result(
        _submission(
            answers=[{
                "question_id": "style",
                "answer": "现代简洁",
                "source_refs": ["current_request"],
                "evidence": "请帮我选一个",
            }]
        ),
        [_question()],
        context_refs=("visible-card",),
        model="test-model",
        authority_evidence={"current_request": "请帮我选一个"},
        decision_authority_evidence={"current_request": "请帮我选一个"},
        require_answer_authority=True,
    )

    assert result.outcome == "decide"
    assert result.answers == {"style": "现代简洁"}
    assert result.answer_source_refs == {"style": ("current_request",)}


def test_project_owner_question_answer_accepts_non_verbatim_evidence():
    result = ProjectOwner._validated_result(
        _submission(
            answers=[{
                "question_id": "style",
                "answer": "现代简洁",
                "source_refs": ["current_request"],
                "evidence": "现代简洁",
            }]
        ),
        [_question()],
        context_refs=("visible-card",),
        model="test-model",
        authority_evidence={"current_request": "请帮我选一个"},
        decision_authority_evidence={"current_request": "请帮我选一个"},
        require_answer_authority=True,
    )

    assert result.outcome == "decide"
    assert result.answers == {"style": "现代简洁"}
    assert result.answer_source_refs == {"style": ("current_request",)}


def test_project_owner_question_answer_accepts_combined_multi_source_evidence():
    approval_question = {
        "question_id": "approval_decision",
        "question": "是否允许执行本轮检查？",
        "options": [
            {"label": "允许本次", "description": "仅允许这一次执行"},
            {
                "label": "本会话允许同类命令",
                "description": "后续同类命令不再重复询问",
            },
            {"label": "拒绝并停止", "description": "不执行并停止当前轮次"},
        ],
        "sensitivity": "internal",
    }
    result = ProjectOwner._validated_result(
        _submission(
            answers=[{
                "question_id": "approval_decision",
                "answer": "本会话允许同类命令",
                "source_refs": ["current_request", "previous_choice"],
                "evidence": (
                    "用户要求持续推进，并在此前同类问题中选择了会话级允许。"
                ),
            }]
        ),
        [approval_question],
        context_refs=("visible-card",),
        model="test-model",
        authority_evidence={
            "current_request": "持续推进，需要的权限都通过。",
            "previous_choice": "我选择：本会话允许同类命令",
        },
        decision_authority_evidence={
            "current_request": "持续推进，需要的权限都通过。",
            "previous_choice": "我选择：本会话允许同类命令",
        },
        require_answer_authority=True,
    )

    assert result.outcome == "decide"
    assert result.answers == {"approval_decision": "本会话允许同类命令"}
    assert result.answer_source_refs == {
        "approval_decision": ("current_request", "previous_choice")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_length", [300, 13_000])
async def test_owner_treats_current_task_as_authority_for_ordinary_approval(
    owner_context, monkeypatch, tmp_path, reason_length
):
    service, project, _workspace = owner_context
    store = RootSessionStore(service.session)
    store.begin_turn(
        project.id,
        user_request="修复登录故障并确认结果可用。",
        request_origin="human",
    )
    captured = {}

    class ApprovalOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner-approval"

        async def run_turn(self, prompt, *, output_schema=None):
            payload = project_owner_module.json.loads(
                prompt.split("数据：\n", 1)[1]
            )
            captured.update(payload)
            source_ref = payload["authority"]["current_human_instructions"][0][
                "source_ref"
            ]
            return TurnResult(
                thread_id="thread-owner-approval",
                turn_id="turn-owner-approval",
                status="completed",
                final_message=project_owner_module.json.dumps({
                    **_submission(),
                    "decision": "允许运行当前项目的回归测试",
                    "rationale": "这是确认本轮修复可用的普通项目内步骤。",
                    "impact": "可以继续完成用户要求的结果确认。",
                    "answers": [{
                        "question_id": "question_1",
                        "answer": "允许本次",
                        "source_refs": [source_ref],
                        "evidence": "当前修复请求授权了确认结果所需的项目内测试。",
                    }],
                    "authority_refs": [source_ref],
                }),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", ApprovalOwnerThread
    )
    question = {
        "question_id": "approval_decision",
        "question": "是否允许执行：python -m pytest -q",
        "options": [
            {"label": "允许本次", "description": "仅允许这一次执行"},
            {"label": "拒绝并停止", "description": "停止当前轮次"},
        ],
        "sensitivity": "internal",
        "request_kind": "approval",
        "reason": "审批说明。" * (reason_length // 5) + " 新增：/private/tmp/result.patch token=private-value",
    }

    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).decide(project, [question])

    assert captured["task"]["kind"] == "answer_approval"
    owner_question = captured["task"]["current_questions"][0]
    assert len(owner_question["header"]) == 120
    assert "private-value" not in owner_question["approval_context"]
    if reason_length < 12_000:
        assert "新增：/private/tmp/result.patch" in owner_question["approval_context"]
        assert "[REDACTED]" in owner_question["approval_context"]
    else:
        assert owner_question["approval_context"].endswith("[审批上下文已截断，操作范围不完整]")
    assert captured["authority"]["current_human_instructions"][0]["text"] == (
        "修复登录故障并确认结果可用。"
    )
    assert result.outcome == "decide"
    assert result.answers == {"approval_decision": "允许本次"}
    assert result.answer_source_refs["approval_decision"]


@pytest.mark.asyncio
async def test_owner_continuation_keeps_parent_human_context_for_approval_scope(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    parent = service.create_project_decision(
        project.id,
        request_item_id="report:parent",
        native_turn_id="turn-parent",
        question="本轮汇报后的项目决定",
        options=[],
        decision="继续完成已经确认的数据评估",
        rationale="当前样本已经确认，可以继续形成结果。",
        impact="完成当前评估结果。",
        reversible=True,
        confidence="high",
        answer_payload={
            "continue_execution": True,
            "human_context": [{
                "text": "先完成已经确认的数据评估，不要换研究方向。",
                "origin": "human",
                "time": "2026-08-20T22:27:00",
                "source_ref": "human-source",
                "reply_to": {"request": "下一步研究哪个方向？"},
            }],
        },
        context_refs=[],
        authority_refs=[],
        actor_model="owner-model",
        decision_type="governance",
        execution_status="active",
    )
    store = RootSessionStore(service.session)
    store.begin_turn(
        project.id,
        user_request="继续 Owner 决定：完成已经确认的数据评估",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )
    captured = {}

    class ApprovalOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner-approval-parent"

        async def run_turn(self, prompt, *, output_schema=None):
            payload = project_owner_module.json.loads(
                prompt.split("数据：\n", 1)[1]
            )
            captured.update(payload)
            parent_ref = payload["task"]["technical_continuation_context"][
                "source_ref"
            ]
            return TurnResult(
                thread_id="thread-owner-approval-parent",
                turn_id="turn-owner-approval-parent",
                status="completed",
                final_message=project_owner_module.json.dumps({
                    **_submission(),
                    "decision": "允许运行当前评估所需的本机服务",
                    "rationale": "这是完成当前结果所需的低风险可撤销步骤。",
                    "impact": "评估可以继续，不需要用户处理技术审批。",
                    "answers": [{
                        "question_id": "question_1",
                        "answer": "允许本次",
                        "source_refs": [parent_ref],
                        "evidence": "父决定限定了当前评估范围，本步骤只服务于该结果。",
                    }],
                }),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", ApprovalOwnerThread
    )
    question = {
        "question_id": "approval_decision",
        "question": "是否允许重启当前项目的本机评估服务？",
        "options": [
            {"label": "允许本次", "description": "仅允许这一次执行"},
            {"label": "拒绝并停止", "description": "停止当前轮次"},
        ],
        "sensitivity": "internal",
        "request_kind": "approval",
    }

    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).decide(project, [question])

    assert captured["authority"]["current_human_instructions"] == [{
        "text": "先完成已经确认的数据评估，不要换研究方向。",
        "origin": "human",
        "time": "2026-08-20T22:27:00",
        "source_ref": "human-source",
        "reply_to": {"request": "下一步研究哪个方向？"},
    }]
    assert captured["task"]["technical_continuation_context"] == {
        "id": parent.id,
        "source_ref": parent.id,
        "decision": "继续完成已经确认的数据评估",
        "impact": "完成当前评估结果。",
        "acceptance_criteria": [],
        "execution_status": "active",
        "execution_rounds": 0,
        "human_authority_refs": [],
        "human_context": [{
            "text": "先完成已经确认的数据评估，不要换研究方向。",
            "origin": "human",
            "time": "2026-08-20T22:27:00",
            "source_ref": "human-source",
            "reply_to": {"request": "下一步研究哪个方向？"},
        }],
        "authority_rank": "project_owner_below_human",
    }
    assert result.outcome == "decide"
    assert result.answers == {"approval_decision": "允许本次"}
    assert result.answer_source_refs["approval_decision"] == (parent.id,)


@pytest.mark.asyncio
async def test_owner_review_treats_same_turn_human_steer_as_current_authority(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    parent = service.create_project_decision(
        project.id,
        request_item_id="report:old-direction",
        native_turn_id="turn-old-direction",
        question="本轮汇报后的项目决定",
        options=[],
        decision="继续扩大实验样本",
        rationale="原方向需要更多样本。",
        impact="继续原实验。",
        reversible=True,
        confidence="high",
        answer_payload={"continue_execution": True},
        context_refs=[],
        actor_model="owner-model",
        decision_type="governance",
        execution_status="active",
    )
    store = RootSessionStore(service.session)
    store.begin_turn(
        project.id,
        user_request="继续 Owner 决定：扩大实验样本",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )
    history = store.append_history_instruction(
        project.id,
        "先不要扩大样本，只总结现在的结论。",
        actor_open_id="ou-owner",
    )
    assert history is not None
    service.record_project_history_report(
        project.id,
        native_turn_id="turn-current",
        title="本轮结果",
        content="已停止扩大样本，并总结了当前结论。",
        report_ref="report:turn-current",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )
    captured = {}

    class ReviewOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner-steer"

        async def run_turn(self, prompt, *, output_schema=None):
            captured.update(project_owner_module.json.loads(
                prompt.split("数据：\n", 1)[1]
            ))
            return TurnResult(
                thread_id="thread-owner-steer",
                turn_id="turn-owner-steer",
                status="completed",
                final_message=project_owner_module.json.dumps({
                    **_submission(),
                    "outcome": "decide",
                    "decision": "保持当前结论，不再扩大样本",
                    "rationale": "最新真人指令要求只总结，当前结果已经满足。",
                    "impact": "不会恢复旧的扩样方向。",
                    "answers": [],
                    "continue_execution": False,
                }),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", ReviewOwnerThread
    )
    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).review_report(
        project,
        title="本轮结果",
        content="已停止扩大样本，并总结了当前结论。",
        user_request="继续 Owner 决定：扩大实验样本",
        native_turn_id="turn-current",
        history_round_id=history.id,
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    assert captured["authority"]["current_human_instructions"] == [{
        "text": "先不要扩大样本，只总结现在的结论。",
        "origin": "human_steer",
        "time": history.user_instructions[1]["time"],
        "source_ref": f"{history.id}:instruction:2",
    }]
    assert captured["human_mainline"]["latest_instruction"] == (
        captured["authority"]["current_human_instructions"][0]
    )
    assert "parent_decision" not in captured["task"]
    assert result.outcome == "decide"
    assert result.continue_execution is False


@pytest.mark.asyncio
async def test_owner_continuation_keeps_later_human_correction_ahead_of_parent(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    parent = service.create_project_decision(
        project.id,
        request_item_id="report:old-mainline",
        native_turn_id="turn-old-mainline",
        question="本轮汇报后的项目决定",
        options=[],
        decision="继续扩大实验样本",
        rationale="原方向需要更多样本。",
        impact="继续原实验。",
        reversible=True,
        confidence="high",
        answer_payload={
            "continue_execution": True,
            "human_context": [{
                "text": "继续扩大样本。",
                "origin": "human",
                "time": "2026-08-20T20:00:00",
                "source_ref": "human:old-mainline",
            }],
        },
        context_refs=[],
        actor_model="owner-model",
        decision_type="governance",
        execution_status="active",
    )
    correction = service.begin_project_history_round(
        project.id,
        user_request="不要再扩大样本，回到交付当前结论。",
        request_origin="human",
    )
    current = service.begin_project_history_round(
        project.id,
        user_request="Owner 决定：继续扩大实验样本",
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )
    current.native_turn_id = "turn-after-correction"
    current.report_title = "本轮结果"
    current.report_content = "仍在扩大样本。"
    current.report_status = "completed"
    current.report_ref = "report:after-correction"
    service.session.add(current)
    service.session.commit()
    captured = {}

    class ReviewOwnerThread:
        def __init__(self, **_kwargs):
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner-later-correction"

        async def run_turn(self, prompt, *, output_schema=None):
            captured.update(project_owner_module.json.loads(
                prompt.split("数据：\n", 1)[1]
            ))
            return TurnResult(
                thread_id="thread-owner-later-correction",
                turn_id="turn-owner-later-correction",
                status="completed",
                final_message=project_owner_module.json.dumps({
                    **_submission(),
                    "outcome": "decide",
                    "decision": "停止扩大样本，回到交付当前结论",
                    "rationale": "最新真人纠正已经终止扩样主线。",
                    "impact": "不会继续旧方向。",
                    "answers": [],
                    "continue_execution": False,
                }),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", ReviewOwnerThread
    )
    await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).review_report(
        project,
        title="本轮结果",
        content="仍在扩大样本。",
        user_request="Owner 决定：继续扩大实验样本",
        native_turn_id="turn-after-correction",
        history_round_id=current.id,
        request_origin="project_owner",
        parent_decision_id=parent.id,
    )

    latest = captured["authority"]["current_human_instructions"]
    assert [item["text"] for item in latest] == [
        "不要再扩大样本，回到交付当前结论。"
    ]
    assert latest[0]["source_ref"] == f"{correction.id}:instruction:1"
    assert captured["authority"]["recent_human_instructions"][0] == latest[0]
    assert captured["human_mainline"]["latest_instruction"] == latest[0]
    assert "parent_decision" not in captured["task"]


@pytest.mark.parametrize(
    "source_refs,evidence",
    [
        ([], "用户选择了现代简洁"),
        (["missing_source"], "用户选择了现代简洁"),
        (["current_request"], ""),
    ],
)
def test_project_owner_question_answer_requires_traceable_evidence(
    source_refs, evidence
):
    with pytest.raises(OwnerOutputValidationError):
        ProjectOwner._validated_result(
            _submission(
                answers=[{
                    "question_id": "style",
                    "answer": "现代简洁",
                    "source_refs": source_refs,
                    "evidence": evidence,
                }]
            ),
            [_question()],
            context_refs=("visible-card",),
            model="test-model",
            authority_evidence={"current_request": "请帮我选一个"},
            decision_authority_evidence={"current_request": "请帮我选一个"},
            require_answer_authority=True,
        )


def test_project_owner_question_answer_keeps_exact_authority_lineage():
    result = ProjectOwner._validated_result(
        _submission(
            answers=[{
                "question_id": "style",
                "answer": "现代简洁",
                "source_refs": ["current_request"],
                "evidence": "继续沿用现代简洁风格",
            }],
            authority_refs=["current_request"],
        ),
        [_question()],
        context_refs=("visible-card",),
        model="test-model",
        authority_evidence={
            "current_request": "继续沿用现代简洁风格，不要重新设计。"
        },
        decision_authority_evidence={
            "current_request": "继续沿用现代简洁风格，不要重新设计。"
        },
        require_answer_authority=True,
    )

    assert result.outcome == "decide"
    assert result.answers == {"style": "现代简洁"}
    assert result.answer_source_refs == {"style": ("current_request",)}
    assert result.authority_refs == ("current_request",)


def test_project_owner_validation_accepts_report_decision_without_answers():
    result = ProjectOwner._validated_result(
        _submission(answers=[], continue_execution=True),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "decide"
    assert result.answers == {}
    assert result.continue_execution is True


def test_project_owner_validation_preserves_human_turn_decision_without_followup():
    result = ProjectOwner._validated_result(
        _submission(answers=[], continue_execution=False),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "decide"
    assert result.answers == {}
    assert result.continue_execution is False


def test_owner_output_does_not_duplicate_parent_decision_state():
    result = ProjectOwner._validated_result(
        _submission(
            answers=[],
            continue_execution=True,
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
        request_origin="project_owner",
        has_parent_decision=False,
    )

    assert result.outcome == "decide"
    assert result.continue_execution is True
    assert "parent_decision_status" not in project_owner_module._owner_output_schema(
        report_review=True
    )["properties"]


def test_owner_generated_turn_may_continue_the_linked_commitment():
    result = ProjectOwner._validated_result(
        _submission(
            answers=[],
            continue_execution=True,
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
        request_origin="project_owner",
        has_parent_decision=True,
    )

    assert result.outcome == "decide"
    assert result.continue_execution is True


def test_project_owner_validation_rejects_no_action_for_report_review():
    with pytest.raises(OwnerOutputValidationError, match="outcome"):
        ProjectOwner._validated_result(
            _submission(outcome="no_action", answers=[]),
            [],
            context_refs=("visible-report",),
            model="test-model",
        )


def test_project_owner_native_approval_uses_answer_evidence_not_report_fields():
    question = {
        **_question(),
        "request_kind": "approval",
    }
    result = ProjectOwner._validated_result(
        _submission(
            reversible=False,
            authority_refs=["legacy-candidate-ref"],
            continue_execution=True,
            answers=[{
                "question_id": "style",
                "answer": "现代简洁",
                "source_refs": ["current_request"],
                "evidence": "当前用户请求需要完成这个项目内步骤。",
            }],
        ),
        [question],
        context_refs=("visible-card",),
        model="test-model",
        decision_authority_evidence={"current_request": "请完成当前任务"},
        require_answer_authority=True,
    )

    assert result.outcome == "decide"
    assert result.reversible is True
    assert result.authority_refs == ("current_request",)
    assert result.continue_execution is False


def test_project_owner_validation_preserves_report_that_requires_user():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="require_user",
            decision="是否将网站公开发布",
            answers=[],
            reversible=False,
            user_options=[{
                "label": "暂不发布",
                "description": "保持当前仅本地可用的状态。",
                "action": "record_only",
            }, {
                "label": "公开发布",
                "description": "授权将网站开放到公网。",
                "action": "execute",
            }],
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "require_user"
    assert result.decision == "是否将网站公开发布"
    assert result.answers == {}


def test_project_owner_validation_does_not_fabricate_advice_option():
    with pytest.raises(OwnerOutputValidationError, match="user_options"):
        ProjectOwner._validated_result(
            _submission(
                outcome="advise",
                decision="明早首轮运行后核对浏览器导航和新增职位数",
                rationale="修复尚未经过允许时段内的真实链路验证。",
                answers=[],
                confidence="medium",
            ),
            [],
            context_refs=("visible-report",),
            model="test-model",
        )


def test_project_owner_validation_keeps_uncertain_advice_as_actionable_advice():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="advise",
            decision="接下来优先稳定性还是视觉效果？",
            answers=[],
            confidence="low",
            user_options=[
                {"label": "优先稳定性", "description": "先修复已知故障", "action": "execute"},
                {"label": "优先视觉效果", "description": "先完善页面体验", "action": "execute"},
                {"label": "优先性能", "description": "先降低页面加载时间", "action": "execute"},
                {"label": "优先内容", "description": "先补齐对外信息", "action": "execute"},
            ],
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "advise"
    assert [item["label"] for item in result.user_options] == [
        "优先稳定性",
        "优先视觉效果",
        "优先性能",
        "优先内容",
    ]


@pytest.mark.parametrize("continue_execution", [True, False])
def test_project_owner_validation_allows_decide_to_carry_user_options(
    continue_execution,
):
    result = ProjectOwner._validated_result(
        _submission(
            outcome="decide",
            decision="继续完成已批准的 Wiki 整批交付",
            answers=[],
            confidence="high",
            reversible=True,
            continue_execution=continue_execution,
            user_options=[{
                "label": "采纳提速方案",
                "description": "用已验证的设计改造检索，缩短后续等待。",
                "action": "execute",
            }, {
                "label": "暂不改造",
                "description": "保持现状，只记录该方向。",
                "action": "record_only",
            }],
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "decide"
    assert result.continue_execution is continue_execution
    assert [item["label"] for item in result.user_options] == [
        "采纳提速方案",
        "暂不改造",
    ]


def test_project_owner_neutral_report_still_gives_non_executing_advice():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="decide",
            decision="保持当前结果，下一次优先核对真实使用效果",
            rationale="当前汇报没有给出可以安全自动继续的动作。",
            impact="本轮不启动新工作，但保留下一次最值得关注的方向。",
            answers=[],
            continue_execution=False,
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
    )

    assert result.outcome == "decide"
    assert result.continue_execution is False


def test_owner_prompt_keeps_scope_boundary_while_proactively_advising():
    assert "current_human_instructions 优先决定本轮范围" in project_owner_module._OWNER_INSTRUCTIONS
    assert "查询本身不是暂停或撤销授权" in project_owner_module._OWNER_INSTRUCTIONS
    assert "查询已经答复不等于此前已授权的工作已经完成" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "应使用 decide 且 continue_execution=true" in project_owner_module._OWNER_INSTRUCTIONS
    assert "不从旧 Owner 决定继承授权" in project_owner_module._OWNER_INSTRUCTIONS
    assert "后续普通查询不能恢复已暂停的工作或验收" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不指定文件、命令、协议字段" in project_owner_module._OWNER_INSTRUCTIONS
    assert "所有输入只分为两类" in project_owner_module._OWNER_INSTRUCTIONS
    assert "不维护另一套长期状态" in project_owner_module._OWNER_INSTRUCTIONS
    assert "必须在 user_options 中至少" in project_owner_module._OWNER_INSTRUCTIONS
    assert "不要把“遵守用户本轮限制”本身包装成新的项目决定" in project_owner_module._OWNER_INSTRUCTIONS
    assert "使用 decide、continue_execution=false" in project_owner_module._OWNER_INSTRUCTIONS
    assert "给出至少一条具体意见" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "尚未选定的真实下一步方向" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "使用 advise 且 continue_execution=false" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "没有真实取舍或无需继续时，明确建议保持现状或关注点" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "主动判断下一步怎样最符合用户的原则和做事风格" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "纵向比较最近几轮是否在反复重做同一流程" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "主动寻找最有价值的下一步不等于每轮重新设计流程" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "一个决定只向 Root 推动一个当前最高价值的主要结果" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "非主线问题先保留为意见" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "对齐程度取决于决定对项目的影响层级" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "实现细节和普通执行取舍" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "影响后续多轮的大方向、核心流程、成功标准" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "先给出你的明确推荐和关键取舍" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不能因为“主动代理”而替用户首次定义重大方向" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "只授权你消除模糊表述，不授权你创造固定总次数" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不能把一次纠正扩写成整套新制度" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "已有结果、基线或中间产物能够继续使用时" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "只有当前真人原话或现行项目规则直接支持同一个选项" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "repeated_choice 只能支持" in project_owner_module._OWNER_INSTRUCTIONS
    assert "不复制项目历史" in project_owner_module._OWNER_INSTRUCTIONS
    assert "至少两处不同真人消息或选择" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不得写入 project_policy_observations" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不要为了显得考虑全面而批量重读旧汇报" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "被测对象就是已对齐的当前候选" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "对象跑错、条件不可比或数据泄漏" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "选择下一个新假设不属于普通实现细节" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "用户没有逐字提到该步骤，本身不构成依据不足" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "不得建立命令关键词白名单" in project_owner_module._OWNER_INSTRUCTIONS
    assert "名称本身不是唯一身份证据" in project_owner_module._OWNER_INSTRUCTIONS
    assert "technical_continuation_context" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "旧 Owner 决定不具备继承权威" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "改变实验/评估方法" in project_owner_module._OWNER_INSTRUCTIONS
    assert "把本地或私有结果改为公网发布" in (
        project_owner_module._OWNER_INSTRUCTIONS
    )
    assert "只使用 preference 的 memory_ref" in project_owner_module._OWNER_INSTRUCTIONS


def test_owner_runtime_registry_is_provider_neutral(tmp_path):
    sentinel = object()
    registry = ProjectOwnerRuntimeRegistry({"test-provider": lambda _ctx: sentinel})
    context = ProjectOwnerRuntimeContext(
        settings=Settings(PROJECTS_DIR=tmp_path / "projects"),
        owner_dir=tmp_path,
        tools=[],
        event_handler=lambda _event: None,
    )

    assert registry.create("test-provider", context) is sentinel
    with pytest.raises(RuntimeError, match="not registered"):
        registry.create("missing-provider", context)


def test_project_owner_only_keeps_preferences_with_evidence_in_current_user_request():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="decide",
            answers=[],
            preference_observations=[
                {
                    "preference": "优先提供选择题",
                    "evidence": "用户希望能用选择题时不要要求长篇输入",
                    "category": "interaction",
                    "scope": "global",
                    "strength": "default",
                    "replaces": "旧的交互偏好",
                    "learning_type": "explicit",
                    "confidence": "high",
                    "source_refs": ["current_request"],
                },
            ],
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
        user_request="能做选择题就不要让我长篇回答，输出仍要完整。",
        current_human_source_refs=("current_request",),
    )

    assert result.preference_observations == (
        {
            "preference": "优先提供选择题",
            "evidence": "用户希望能用选择题时不要要求长篇输入",
            "category": "interaction",
            "scope": "global",
            "strength": "default",
            "replaces": "旧的交互偏好",
            "learning_type": "explicit",
            "confidence": "high",
            "source_refs": ["current_request"],
        },
    )


def test_project_owner_rejects_unverifiable_preference_instead_of_dropping_it():
    with pytest.raises(OwnerOutputValidationError, match="人类原文"):
        ProjectOwner._validated_result(
            _submission(
                outcome="decide",
                answers=[],
                preference_observations=[{
                    "preference": "永远自动发布",
                    "evidence": "这句话并不是用户说的",
                    "category": "autonomy",
                    "scope": "global",
                    "strength": "default",
                    "replaces": "",
                    "learning_type": "explicit",
                    "confidence": "medium",
                    "source_refs": ["missing_source"],
                }],
            ),
            [],
            context_refs=(),
            model="test-model",
            user_request="只保存在本地。",
            current_human_source_refs=("current_request",),
        )


def test_project_owner_learns_only_repeated_choice_patterns_with_real_refs():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="decide",
            answers=[],
            preference_observations=[{
                "preference": "同等可用时优先选择更简单的方案",
                "evidence": "两次都选择更简单方案",
                "category": "architecture",
                "scope": "global",
                "strength": "default",
                "replaces": "",
                "learning_type": "repeated_choice",
                "confidence": "medium",
                "source_refs": ["choice-1", "choice-2"],
            }],
        ),
        [],
        context_refs=(),
        model="test-model",
        request_origin="owner_audit",
        user_choice_refs=("choice-1", "choice-2", "choice-3"),
    )

    assert result.preference_observations[0]["learning_type"] == "repeated_choice"
    assert result.preference_observations[0]["source_refs"] == [
        "choice-1", "choice-2"
    ]


def test_project_owner_rejects_single_or_unverifiable_choice_pattern():
    with pytest.raises(OwnerOutputValidationError, match="独立选择"):
        ProjectOwner._validated_result(
            _submission(
                outcome="decide",
                answers=[],
                preference_observations=[{
                    "preference": "总是选择方案 A",
                    "evidence": "一次选择",
                    "category": "general",
                    "scope": "global",
                    "strength": "default",
                    "replaces": "",
                    "learning_type": "repeated_choice",
                    "confidence": "medium",
                    "source_refs": ["choice-1"],
                }],
            ),
            [],
            context_refs=(),
            model="test-model",
            user_choice_refs=("choice-1",),
        )


def test_question_review_cannot_write_cross_project_preferences():
    result = ProjectOwner._validated_result(
        _submission(
            preference_observations=[
                {
                    "preference": "擅自推断的偏好",
                    "evidence": "继续使用哪种风格",
                    "category": "quality",
                }
            ]
        ),
        [_question()],
        context_refs=(),
        model="test-model",
        user_request="继续使用哪种风格",
    )

    assert result.preference_observations == ()


def test_project_policy_preserves_all_traceable_human_sources():
    result = ProjectOwner._validated_result(
        _submission(
            outcome="decide",
            answers=[],
            project_policy_observations=[
                {
                    "policy": "旧封箱数据只能作为已暴露的历史样本使用",
                    "source_refs": [
                        "round-42:instruction:1",
                        "round-43:instruction:1",
                    ],
                    "kind": "inferred",
                    "category": "data_usage",
                    "replaces": ["policy-old"],
                },
            ],
        ),
        [],
        context_refs=("visible-report",),
        model="test-model",
        authority_evidence={
            "round-42:instruction:1": "旧封箱数据可以使用，但不能再叫未触碰验证集",
            "round-43:instruction:1": "已经看过的数据可以复用，结论里说清楚就行",
        },
        authority_contexts={
            "round-42:instruction:1": {
                "source_time": "2026-08-24T12:00:00",
                "original_text": "旧封箱数据可以使用，但不能再叫未触碰验证集",
            },
            "round-43:instruction:1": {
                "source_time": "2026-08-25T12:00:00",
                "original_text": "已经看过的数据可以复用，结论里说清楚就行",
            },
        },
        active_project_policies=("封箱数据完全不能访问",),
        active_project_policy_refs=("policy-old",),
    )

    assert result.project_policy_observations == (
        {
            "policy": "旧封箱数据只能作为已暴露的历史样本使用",
            "evidence": "旧封箱数据可以使用，但不能再叫未触碰验证集 | 已经看过的数据可以复用，结论里说清楚就行",
            "source_ref": "round-42:instruction:1",
            "source_refs": [
                "round-42:instruction:1",
                "round-43:instruction:1",
            ],
            "kind": "inferred",
            "category": "data_usage",
            "strength": "default",
            "replaces": ["policy-old"],
            "source_time": "2026-08-24T12:00:00",
            "source_context": {
                "source_ref": "round-42:instruction:1",
                "source_time": "2026-08-24T12:00:00",
                "original_text": "旧封箱数据可以使用，但不能再叫未触碰验证集",
                "supporting_sources": [
                    {
                        "source_ref": "round-42:instruction:1",
                        "source_time": "2026-08-24T12:00:00",
                        "original_text": "旧封箱数据可以使用，但不能再叫未触碰验证集",
                    },
                    {
                        "source_ref": "round-43:instruction:1",
                        "source_time": "2026-08-25T12:00:00",
                        "original_text": "已经看过的数据可以复用，结论里说清楚就行",
                    },
                ],
            },
        },
    )
    assert "round-42:instruction:1" in result.context_refs
    assert "round-43:instruction:1" in result.context_refs


def test_inferred_project_principle_requires_multiple_human_sources():
    with pytest.raises(OwnerOutputValidationError, match="至少需要两处"):
        ProjectOwner._validated_result(
            _submission(
                outcome="decide",
                answers=[],
                project_policy_observations=[{
                    "policy": "优先复用已有产物",
                    "source_refs": ["current_request"],
                    "kind": "inferred",
                    "category": "reuse",
                    "replaces": [],
                }],
            ),
            [],
            context_refs=(),
            model="test-model",
            authority_evidence={"current_request": "为什么不复用之前的 HTML"},
        )


def test_project_policy_cannot_fuzzily_supersede_an_existing_rule():
    with pytest.raises(OwnerOutputValidationError, match="replaces"):
        ProjectOwner._validated_result(
            _submission(
                outcome="decide",
                answers=[],
                project_policy_observations=[{
                    "policy": "采用新的项目范围",
                    "source_refs": ["current_request"],
                    "kind": "explicit",
                    "category": "scope",
                    "replaces": "大概是旧范围",
                }],
            ),
            [],
            context_refs=(),
            model="test-model",
            authority_evidence={"current_request": "采用新的项目范围"},
            active_project_policies=("严格保持旧范围",),
        )


def test_project_owner_runtime_is_outside_the_project_tree(owner_context):
    _service, project, workspace = owner_context

    runtime_dir = _owner_runtime_dir(project.id).resolve()

    assert runtime_dir != workspace.resolve()
    assert workspace.resolve() not in runtime_dir.parents


@pytest.mark.asyncio
async def test_project_owner_runtime_receives_public_aliases_not_backend_ids(
    owner_context, monkeypatch, tmp_path
):
    service, project, workspace = owner_context
    monkeypatch.setattr(service, "get_project_owner_open_id", lambda _project_id: "ou-1")
    monkeypatch.setattr(
        service,
        "get_user_decision_cases",
        lambda _open_id, project_id=None, limit=100, before=None: [{
            "time": "2026-08-01T12:00:00",
            "project": "另一个项目",
            "project_id": "other-project",
            "kind": "owner_correction",
            "question": "偏好什么界面？",
            "options": [
                {"label": "现代简洁", "description": "保持简单"},
                {"label": "未来科技", "description": "整体重做"},
            ],
            "user_choice": "现代简洁",
            "rejected_owner_decision": "未来科技",
            "is_correction": True,
            "source_ref": "answer-1",
            "authority_class": "explicit_human_correction",
        }],
    )
    row = service.enqueue_notification(
        dedupe_key="visible-owner-context",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        payload={"card": {"body": {"elements": [{
            "tag": "markdown",
            "content": "现有界面采用现代简洁风格。",
        }]}}},
        project_id=project.id,
    )
    service.claim_notification(row.id)
    service.finish_notification(row.id, provider_message_id="om-visible")
    captured = {}

    class FakeOwnerThread:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.tools = {tool.name: tool for tool in kwargs["tools"]}
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner"

        async def run_turn(self, prompt, *, output_schema=None):
            captured["prompt"] = prompt
            captured["output_schema"] = output_schema
            self_event_handler = captured["kwargs"]["event_handler"]
            self_event_handler(
                AgentEvent(
                    kind="thread/tokenUsage/updated",
                    thread_id="thread-owner",
                    turn_id="turn-owner",
                    payload={
                        "tokenUsage": {
                            "total": {
                                "inputTokens": 120,
                                "cachedInputTokens": 80,
                                "outputTokens": 30,
                                "totalTokens": 150,
                            }
                        }
                    },
                )
            )
            payload = {
                "outcome": "decide",
                "decision": "沿用现代简洁风格",
                "rationale": "已有用户可见结果支持该选择。",
                "impact": "保持一致。",
                "reversible": True,
                "confidence": "high",
                "answers": [{
                    "question_id": "question_1",
                    "answer": "现代简洁",
                    "source_refs": ["current_request"],
                    "evidence": "继续沿用现代简洁风格",
                }],
                "authority_refs": ["current_request"],
                "continue_execution": False,
            }
            return TurnResult(
                thread_id="thread-owner",
                turn_id="turn-owner",
                status="completed",
                final_message=project_owner_module.json.dumps(payload),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", FakeOwnerThread
    )
    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).decide(
        project,
        [{
            "question_id": "backend-internal-style-id",
            "reason": "界面风格",
            "question": "继续使用哪种风格？",
            "options": [
                {"label": "现代简洁", "description": "延续现有设计"},
                {"label": "未来科技", "description": "整体重做"},
            ],
            "allow_free_text": True,
            "sensitivity": "internal",
        }],
        request_context={
            "text": "继续沿用现代简洁风格，不要重新设计。",
            "origin": "human",
            "parent_decision_id": None,
        },
    )

    assert result.outcome == "decide"
    assert result.answers == {"backend-internal-style-id": "现代简洁"}
    assert result.usage == {
        "inputTokens": 120,
        "cachedInputTokens": 80,
        "outputTokens": 30,
        "totalTokens": 150,
    }
    assert result.duration_seconds is not None
    assert "question_1" in captured["prompt"]
    assert "backend-internal-style-id" not in captured["prompt"]
    assert "另一个项目" in captured["prompt"]
    assert "现代简洁" in captured["prompt"]
    assert "correction_counterexample" in captured["prompt"]
    assert '"rejected_owner_decision": "未来科技"' in captured["prompt"]
    assert "继续沿用现代简洁风格，不要重新设计" in captured["prompt"]
    assert '"origin": "human"' in captured["prompt"]
    assert str(workspace) not in captured["prompt"]
    assert captured["kwargs"]["cwd"] == _owner_runtime_dir(project.id)
    assert captured["kwargs"]["environments"] == []
    assert captured["kwargs"]["ephemeral"] is True
    assert captured["kwargs"]["sandbox"] == "read-only"
    assert captured["kwargs"]["approval_policy"] == "never"
    assert captured["kwargs"]["codex_home"] == (
        _owner_runtime_dir(project.id) / "codex-home"
    )
    assert {tool.name for tool in captured["kwargs"]["tools"]} == {
        "read_public_project_results",
        "read_project_history",
        "read_user_authority_history",
        "read_user_preferences",
        "search_user_history",
        "read_user_link",
    }
    assert captured["output_schema"]["properties"]["outcome"]["enum"] == [
        "decide",
        "defer",
    ]


@pytest.mark.asyncio
async def test_project_owner_reviews_current_report_in_isolated_public_context(
    owner_context, monkeypatch, tmp_path
):
    service, project, workspace = owner_context
    service.session.add(
        ProjectUser(project_id=project.id, open_id="ou-owner", role="owner")
    )
    service.session.commit()
    service.add_project_policies(
        project.id,
        [{
            "policy": "已有产物仍满足目标时优先复用",
            "evidence": "复用已有结果 | 不要重复建设",
            "source_ref": "round-1:instruction:1",
            "source_refs": [
                "round-1:instruction:1",
                "round-2:instruction:1",
            ],
            "source_context": {
                "source_ref": "round-1:instruction:1",
                "original_text": "复用已有结果",
                "supporting_sources": [
                    {
                        "source_ref": "round-1:instruction:1",
                        "original_text": "复用已有结果",
                    },
                    {
                        "source_ref": "round-2:instruction:1",
                        "original_text": "不要重复建设",
                    },
                ],
            },
            "kind": "inferred",
            "category": "reuse",
        }, {
            "policy": "旧格式具体规则",
            "evidence": "旧格式具体规则",
            "source_ref": "legacy-policy",
            "kind": "constraint",
            "category": "legacy",
        }],
        source_ref="round-1",
    )
    service.add_owner_sources(
        "ou-owner",
        [{
            "provider": "kimi",
            "kind": "human_input",
            "source_ref": "kimi:session:human:1",
            "timestamp": "2026-08-01T10:00:00",
            "content": "低风险且可撤销时由 AI 继续",
            "context_before": "用户正在说明自动推进边界。",
            "context_after": "不可逆的重要事项需要先问。",
        }],
    )
    service.add_owner_memories(
        "ou-owner",
        [{
            "preference": "低风险且可撤销时由 AI 继续",
            "evidence": "低风险且可撤销时由 AI 继续",
            "category": "autonomy",
            "scope": "global",
            "strength": "hard",
        }],
        project_id=project.id,
        source_ref="visible-user-policy",
    )
    cross_project_memory = next(
        item
        for item in service.get_effective_owner_memories(
            "ou-owner", project_id=project.id
        )
        if item.content == "低风险且可撤销时由 AI 继续"
    )
    cross_project_memory.source_project_id = None
    service.session.add(cross_project_memory)
    service.session.commit()
    for index in range(12):
        service.add_owner_memories(
            "ou-owner",
            [{
                "preference": f"历史用户偏好 {index}",
                "evidence": f"历史用户偏好 {index}",
                "category": "history",
                "scope": "global",
                "strength": "default",
            }],
            project_id=project.id,
            source_ref=f"visible-user-policy:{index}",
        )
    report_card = {
        "header": {"title": {"tag": "plain_text", "content": "本轮结果"}},
        "body": {"elements": [{
            "tag": "markdown",
            "content": "两个低风险实现都已验证，需要决定采用 A 方案还是 B 方案。",
        }]},
    }
    row = service.enqueue_notification(
        dedupe_key="visible-report-review",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        payload={"card": report_card},
        project_id=project.id,
    )
    service.claim_notification(row.id)
    service.finish_notification(row.id, provider_message_id="om-report")
    prior = service.create_project_decision(
        project.id,
        request_item_id="question-before-report",
        native_turn_id="turn-owner-review",
        question="采用哪个方案？",
        options=[],
        decision="采用 A 方案",
        rationale="A 方案风险更低。",
        impact="继续执行 A。",
        reversible=True,
        confidence="high",
        answer_payload={"answers": {"choice": "A"}},
        context_refs=[],
        actor_model="owner-model",
        status="applied",
    )
    service.update_project_decision(
        prior.id, status="applied", card_message_id="om-owner-decision"
    )
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "project-choice",
        [{
            "question_id": "delivery",
            "question": "是否保持移动端优先？",
            "options": [{"key": "是", "label": "是"}],
        }],
    )
    project_question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        project_question.id,
        "是",
        "是",
        False,
        "ou-owner",
        answer_source="user",
    )
    captured = {}

    class FakeOwnerThread:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.tools = {tool.name: tool for tool in kwargs["tools"]}
            self.active_model = "owner-model"

        async def create_thread(self):
            return "thread-owner-review"

        async def run_turn(self, prompt, *, output_schema=None):
            captured["prompt"] = prompt
            captured["output_schema"] = output_schema
            payload = project_owner_module.json.loads(prompt.split("数据：\n", 1)[1])
            memory = next(
                item for item in payload["authority"]["user_preferences"]
                if item["content"] == "低风险且可撤销时由 AI 继续"
            )
            captured["memory_detail"] = self.tools[
                "read_user_preferences"
            ].handler({"memory_ref": memory["memory_ref"], "limit": 1})
            payload = {
                "outcome": "decide",
                "decision": "采用 A 方案",
                "rationale": "汇报显示 A 方案风险更低且已经验证。",
                "impact": "执行 Agent 将按 A 方案继续。",
                "reversible": True,
                "confidence": "high",
                "answers": [],
                "continue_execution": True,
                "preference_observations": [],
            }
            return TurnResult(
                thread_id="thread-owner-review",
                turn_id="turn-owner-review",
                status="completed",
                final_message=project_owner_module.json.dumps(payload),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", FakeOwnerThread
    )

    result = await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).review_report(
        project,
        title="本轮结果",
        content="两个低风险实现都已验证，需要决定采用 A 方案还是 B 方案。",
        user_request="请只比较两个方案，不要开始实现。",
        native_turn_id="turn-owner-review",
    )

    assert result.outcome == "decide"
    assert result.decision == "采用 A 方案"
    assert result.answers == {}
    assert result.continue_execution is True
    context_log = service.session.exec(
        select(EventLog).where(
            EventLog.message == "Project Owner context snapshot"
        )
    ).one()
    assert context_log.meta["kind"] == "project_owner_context"
    assert context_log.meta["context"]["human_mainline"] == (
        result.context_snapshot["human_mainline"]
    )
    assert context_log.meta["context"]["task"]["kind"] == "review_report"
    assert '"kind":"review_report"' in captured["prompt"].replace(" ", "")
    assert '"authority":{' in captured["prompt"].replace(" ", "")
    assert '"current_human_instructions":[' in captured["prompt"].replace(" ", "")
    assert '"recent_project_outcomes":[' in captured["prompt"].replace(" ", "")
    assert '"current_project_outcome":{' in captured["prompt"].replace(" ", "")
    assert '"project_goal"' not in captured["prompt"]
    assert '"hold_reason":"no_safe_step"' not in captured["prompt"].replace(" ", "")
    assert '"project_owner_view"' not in captured["prompt"]
    assert '"user_preferences"' in captured["prompt"]
    assert '"project_policies"' in captured["prompt"]
    assert "已有产物仍满足目标时优先复用" in captured["prompt"]
    assert "round-1:instruction:1" in captured["prompt"]
    assert "round-2:instruction:1" in captured["prompt"]
    assert "旧格式具体规则" not in captured["prompt"]
    assert '"project_user_choices"' in captured["prompt"]
    assert "是否保持移动端优先" in captured["prompt"]
    assert "历史用户偏好 0" in captured["prompt"]
    assert "历史用户偏好 11" in captured["prompt"]
    assert '"source_project_id":"proj-owner"' in captured["prompt"].replace(" ", "")
    prompt_payload = project_owner_module.json.loads(
        captured["prompt"].split("数据：\n", 1)[1]
    )
    preferences = prompt_payload["authority"]["user_preferences"]
    assert all("source_ref" not in item for item in preferences)
    autonomy = next(
        item for item in preferences if item["content"] == "低风险且可撤销时由 AI 继续"
    )
    assert "historical_contexts" not in autonomy
    assert captured["memory_detail"]["policies"][0]["historical_contexts"][0]["original_text"] == (
        "低风险且可撤销时由 AI 继续"
    )
    assert '"recent_owner_decisions"' in captured["prompt"]
    assert '"decisions_already_shown_this_turn"' in captured["prompt"]
    assert "采用哪个方案" in captured["prompt"]
    assert prior.id in result.context_refs
    assert "两个低风险实现都已验证" in captured["prompt"]
    assert "请只比较两个方案，不要开始实现" in captured["prompt"]
    assert str(workspace) not in captured["prompt"]
    assert captured["kwargs"]["cwd"] == _owner_runtime_dir(project.id)
    assert captured["kwargs"]["thread_config"]["model"] == "gpt-6.1-sol"
    assert captured["kwargs"]["thread_config"]["model_reasoning_effort"] == "xhigh"
    assert captured["kwargs"]["thread_config"]["model_reasoning_summary"] == "none"
    assert captured["kwargs"]["thread_config"]["model_providers.mac_openai_https"][
        "stream_max_retries"
    ] == 10
    assert captured["output_schema"]["properties"]["outcome"]["enum"] == [
        "decide",
        "advise",
        "require_user",
    ]
    assert {
        "goal_assessment",
        "unmet_criteria",
        "conflicts",
        "goal_update",
        "hold_reason",
        "hold_details",
        "resume_at",
    }.isdisjoint(captured["output_schema"]["properties"])
    assert {tool.name for tool in captured["kwargs"]["tools"]}.issuperset(
        {"search_user_history", "read_user_link"}
    )


@pytest.mark.asyncio
async def test_owner_context_marks_current_recent_and_paginated_older_rounds(
    owner_context, monkeypatch, tmp_path
):
    service, project, _workspace = owner_context
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-history")
    current_round_id = None
    for sequence in range(1, 9):
        root = store.begin_turn(
            project.id,
            user_request=f"用户指令 {sequence}",
            request_origin="human",
            reply_context=(
                {
                    "kind": "report",
                    "subject_type": "root_turn",
                    "subject_ref": "turn-6",
                    "summary": "第 6 轮交付结果",
                    "request": "用户指令 6",
                    "result": "汇报结果 6",
                }
                if sequence == 7
                else None
            ),
        )
        current_round_id = root.active_history_round_id
        service.ensure_project_owner_review_job(
            project.id,
            request_item_id=f"report:turn-{sequence}",
            native_turn_id=f"turn-{sequence}",
            title=f"第 {sequence} 轮汇报",
            content=f"汇报结果 {sequence}",
            user_request=f"用户指令 {sequence}",
            report_card={"color": "blue"},
        )

    captured = {}

    class HistoryOwnerThread:
        def __init__(self, **kwargs):
            captured["tools"] = {tool.name: tool for tool in kwargs["tools"]}
            self.active_model = "owner-model"

        async def create_thread(self):
            return "owner-history-thread"

        async def run_turn(self, prompt, *, output_schema=None):
            captured["prompt"] = prompt
            captured["history_tool"] = captured["tools"][
                "read_project_history"
            ].handler({"limit": 50})
            captured["authority_tool"] = captured["tools"][
                "read_user_authority_history"
            ].handler({"limit": 50})
            return TurnResult(
                thread_id="owner-history-thread",
                turn_id="owner-history-turn",
                status="completed",
                final_message=project_owner_module.json.dumps(
                    {
                        "outcome": "advise",
                        "decision": "继续核对历史",
                        "rationale": "历史轮次可读。",
                        "impact": "无自动执行。",
                        "reversible": True,
                        "confidence": "high",
                        "answers": [],
                        "continue_execution": False,
                        "preference_observations": [],
                        "user_options": [{
                            "label": "继续核对历史",
                            "description": "读取更早的人类指令以确认上下文。",
                            "action": "execute",
                        }],
                    }
                ),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", HistoryOwnerThread
    )

    await ProjectOwner(
        service, Settings(PROJECTS_DIR=tmp_path / "projects")
    ).review_report(
        project,
        title="第 8 轮汇报",
        content="汇报结果 8",
        user_request="用户指令 8",
        native_turn_id="turn-8",
        history_round_id=current_round_id,
    )

    payload = project_owner_module.json.loads(
        captured["prompt"].split("数据：\n", 1)[1]
    )
    authority = payload["authority"]
    evidence = payload["evidence"]
    assert authority["current_human_instructions"][0]["text"] == "用户指令 8"
    assert [
        item["text"] for item in authority["recent_human_instructions"]
    ] == [
        "用户指令 8",
        "用户指令 7",
        "用户指令 6",
        "用户指令 5",
        "用户指令 4",
        "用户指令 3",
    ]
    assert authority["attention_order"][:2] == [
        "current_human_instructions",
        "recent_human_instructions",
    ]
    assert (
        evidence["current_project_outcome"]["result"]["outcome_excerpt"]
        == "汇报结果 8"
    )
    assert [row["sequence"] for row in evidence["recent_project_outcomes"]] == [7, 6, 5, 4, 3]
    assert evidence["recent_project_outcomes"][0]["user_instructions"][0][
        "reply_to"
    ] == {
        "kind": "report",
        "subject_type": "root_turn",
        "subject_ref": "turn-6",
        "summary": "第 6 轮交付结果",
        "request": "用户指令 6",
        "result": "汇报结果 6",
    }
    assert evidence["recent_project_outcomes"][0]["turn_ref"] == "turn-7"
    assert evidence["older_rounds_overview"]["round_count"] == 2
    assert evidence["older_rounds_overview"]["sequence_range"] == [1, 2]
    assert authority["project_policy_bootstrap_required"] is True
    assert len(authority["historical_human_authority_for_bootstrap"]) == 7
    assert [
        row["sequence"] for row in captured["history_tool"]["rounds"]
    ] == [8, 7, 6, 5, 4, 3, 2, 1]
    assert [
        row["sequence"]
        for row in captured["authority_tool"]["human_instructions"]
    ] == [8, 7, 6, 5, 4, 3, 2, 1]
    assert "汇报结果" not in str(captured["authority_tool"])
    assert captured["authority_tool"]["human_instructions"][1]["instructions"][0][
        "reply_ref"
    ] == {
        "kind": "report",
        "subject_type": "root_turn",
        "subject_ref": "turn-6",
    }
    assert "tool_output" not in str(evidence)
