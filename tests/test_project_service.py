from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.core.notification_service import (
    deliver_card,
    reconcile_retried_root_report,
)
from mac_system.feishu.card import build_content_card
from mac_system.core.project_service import ProjectService
from mac_system.core.root_sessions import RootSessionStore
from mac_system.models import (
    EventLog,
    FeishuEventLog,
    NotificationOutbox,
    Project,
    ProjectDecision,
    ProjectHistoryRound,
    ProjectPolicy,
    ProjectOwnerReview,
    UserOwnerMemory,
    Question,
    QuestionAnswer,
    QuestionInteraction,
)


class Bridge:
    def __init__(self):
        self.cards = []

    async def _send_interactive(self, recipient, card, receive_id_type="open_id"):
        self.cards.append((recipient, receive_id_type, card))
        return f"message-{len(self.cards)}"

    async def update_card(self, message_id, card):
        self.cards.append((message_id, "update", card))


@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        value = ProjectService(session)
        value.settings.projects_dir = tmp_path / "projects"
        value.settings.projects_dir.mkdir()
        value.settings.git_user_name = "MAC Test"
        value.settings.git_user_email = "mac-test@example.com"
        yield value


@pytest.mark.asyncio
async def test_new_project_is_native_root_and_idle(service):
    project = await service.create_project(
        "demo",
        "feature",
        "build it",
        initial_context={"anything": True},
    )
    assert project.current_state == "ACTIVE"
    assert project.execution_status == "idle"
    assert project.context == {"anything": True, "orchestration_mode": "root_agent"}
    assert (Path(project.workspace_path) / ".git").exists()
    assert (Path(project.workspace_path) / ".gitignore").read_text() == ".mac/\n"


@pytest.mark.asyncio
async def test_offline_notice_targets_only_recent_active_project_chats(service):
    project = await service.create_project(
        "demo", "feature", "build it", initial_context={}
    )
    now = datetime.utcnow()

    # Recent events in a chat with an active project are notified.
    service.set_chat_user_context("oc_recent", "ou_user1", project.id)
    service.update_last_event_at("oc_recent", "ou_user1", now)

    # The per-user global watermark row (chat_id == open_id) is not a chat.
    service.update_last_event_at("ou_user2", "ou_user2", now)

    # Recent events without an active project are not notified.
    service.update_last_event_at("oc_idle", "ou_user3", now)

    # Chats idle for longer than the window are not notified.
    service.set_chat_user_context("oc_old", "ou_user4", project.id)
    service.update_last_event_at("oc_old", "ou_user4", now - timedelta(days=30))

    assert service.list_offline_notice_targets() == ["oc_recent"]


@pytest.mark.asyncio
async def test_bot_accounts_isolate_chat_selection_owner_memory_and_outbox(service):
    second = ProjectService(service.session, bot_account_id="research")
    second.settings = service.settings
    default_project = await service.create_project(
        "default-project",
        "feature",
        "default work",
        creator_open_id="ou-same",
        feishu_chat_id="oc-same",
    )
    research_project = await second.create_project(
        "research-project",
        "feature",
        "research work",
        creator_open_id="ou-same",
        feishu_chat_id="oc-same",
    )

    assert service.get_chat_user_selected_project("oc-same", "ou-same").id == (
        default_project.id
    )
    assert second.get_chat_user_selected_project("oc-same", "ou-same").id == (
        research_project.id
    )

    service.add_owner_memories(
        "ou-same",
        [{"preference": "默认机器人偏好", "evidence": "默认机器人偏好"}],
        project_id=default_project.id,
        source_ref="default-source",
    )
    second.add_owner_memories(
        "ou-same",
        [{"preference": "研究机器人偏好", "evidence": "研究机器人偏好"}],
        project_id=research_project.id,
        source_ref="research-source",
    )
    assert [item.content for item in service.get_owner_memories("ou-same")] == [
        "默认机器人偏好"
    ]
    assert [item.content for item in second.get_owner_memories("ou-same")] == [
        "研究机器人偏好"
    ]

    notification = second.enqueue_notification(
        dedupe_key="research-notification",
        recipient_id="oc-same",
        recipient_type="chat_id",
        intent_type="report",
        payload={"card": {}},
        project_id=research_project.id,
    )
    assert notification.bot_account_id == "research"


@pytest.mark.asyncio
async def test_two_bot_accounts_can_use_same_directory_with_isolated_projects(
    service, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )
    repository = tmp_path / "shared-repository"
    repository.mkdir()
    default_project = await service.create_project(
        "default-project",
        "existing_project",
        "default work",
        bot_account_id="default",
        creator_open_id="ou-default",
        feishu_chat_id="oc-default",
        bound_directory=str(repository),
    )
    second = ProjectService(service.session, bot_account_id="research")
    second.settings = service.settings

    research_project = await second.create_project(
        "research-project",
        "existing_project",
        "research work",
        bot_account_id="research",
        creator_open_id="ou-research",
        feishu_chat_id="oc-research",
        bound_directory=str(repository),
    )

    assert default_project.id != research_project.id
    assert default_project.workspace_path == research_project.workspace_path
    assert default_project.bot_account_id == "default"
    assert research_project.bot_account_id == "research"
    assert service.get_chat_user_selected_project(
        "oc-default", "ou-default"
    ).id == default_project.id
    assert second.get_chat_user_selected_project(
        "oc-research", "ou-research"
    ).id == research_project.id


@pytest.mark.asyncio
async def test_retried_failed_root_report_closes_history_and_enqueues_owner_review(service):
    project = await service.create_project("retry-report", "feature", "完成项目")
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-retry")
    store.begin_turn(project.id, user_request="执行失败场景")
    store.set_turn(project.id, turn_id="turn-retry", status="running")
    pending_history = service.record_project_history_report(
        project.id,
        native_turn_id="turn-retry",
        title="本轮未完成",
        content="等待飞书恢复",
        report_ref="root-status:turn-retry:report",
        report_status="delivery_pending",
    )
    notification = service.enqueue_notification(
        dedupe_key="root-status:turn-retry:report",
        recipient_id="oc-chat",
        recipient_type="chat_id",
        intent_type="report",
        project_id=project.id,
        payload={
            "card": build_content_card("本轮未完成", "网络请求失败", color="red"),
            "projection_key": "root-status:turn-retry:report",
            "subject_type": "root_turn",
            "subject_id": "turn-retry",
            "lifecycle_status": "failed",
            "resulting_state": "idle",
        },
    )

    review = reconcile_retried_root_report(
        service,
        notification,
        message_id="om-retried-report",
    )

    service.session.refresh(pending_history)
    assert pending_history.report_status == "failed"
    assert pending_history.report_content == "网络请求失败"
    assert review is not None and review.status == "pending"
    assert review.request_item_id == "report:turn-retry"
    assert review.input_payload["history_round_id"] == pending_history.id


@pytest.mark.asyncio
async def test_existing_directory_shell_stays_idle_until_user_request(service, tmp_path, monkeypatch):
    repository = (tmp_path / "existing-shell").resolve()
    repository.mkdir()
    monkeypatch.setattr(
        "mac_system.core.project_service.path_within_allowed_bind_roots",
        lambda _path: True,
    )

    project = await service.create_project(
        "existing-shell",
        "existing_project",
        "已有目录 existing-shell",
        bound_directory=str(repository),
    )

    assert project.execution_status == "idle"


@pytest.mark.asyncio
async def test_project_owner_review_job_is_durable_idempotent_and_recoverable(service):
    project = await service.create_project("demo", "feature", "build it")
    first = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-1",
        native_turn_id="turn-1",
        title="本轮结果",
        content="已经交付",
        user_request="完成项目",
        request_origin="project_owner",
        parent_decision_id="decision-parent",
    )
    replay = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-1",
        native_turn_id="turn-1",
        title="重复结果",
        content="不应覆盖",
        user_request="重复请求",
    )

    assert replay.id == first.id
    assert replay.input_payload["content"] == "已经交付"
    history = service.get_project_history_rounds(project.id)
    assert len(history) == 1
    assert history[0].user_instructions[0]["text"] == "完成项目"
    assert history[0].report_content == "已经交付"
    assert replay.input_payload["history_round_id"] == history[0].id
    assert replay.request_origin == "project_owner"
    assert replay.parent_decision_id == "decision-parent"
    assert [row.id for row in service.list_recoverable_project_owner_reviews()] == [
        first.id
    ]
    running = service.mark_project_owner_review_running(first.id)
    assert running.status == "running"
    completed = service.create_project_owner_review(
        project.id,
        review_id=first.id,
        request_item_id="report:turn-1",
        native_turn_id="turn-1",
        outcome="no_action",
        decision="无需决定",
        rationale="任务已经完成",
        impact="",
        confidence="high",
        context_refs=[],
        actor_model="owner-model",
        actor_usage={"totalTokens": 12},
        actor_duration_seconds=1.2,
        continue_execution=False,
        memory_updates=[{"preference": "减少用户介入"}],
        status="completed",
    )
    assert completed.status == "completed"
    assert completed.request_origin == "project_owner"
    assert completed.parent_decision_id == "decision-parent"
    assert completed.memory_updates == [{"preference": "减少用户介入"}]
    assert service.list_recoverable_project_owner_reviews() == []
    assert service.session.exec(select(ProjectOwnerReview)).one().decision == "无需决定"


@pytest.mark.asyncio
async def test_recovered_report_review_preserves_original_human_authority(service):
    project = await service.create_project("recovered", "feature", "继续完成")
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-recovered")
    root = store.begin_turn(
        project.id,
        user_request="继续完成当前项目",
        request_origin="human",
    )
    history_round_id = root.active_history_round_id

    recovered = store.begin_turn(
        project.id,
        user_request="继续完成当前项目",
        request_origin="system_recovery",
    )
    assert recovered.active_history_round_id == history_round_id

    review = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:recovered-turn",
        native_turn_id="recovered-turn",
        title="恢复后的本轮结果",
        content="当前任务已经完成。",
        user_request="继续完成当前项目",
        request_origin="system_recovery",
    )

    history = service.session.get(ProjectHistoryRound, history_round_id)
    assert history.request_origin == "human"
    assert review.request_origin == "human"
    assert review.input_payload["request_origin"] == "human"


@pytest.mark.asyncio
async def test_existing_recovery_review_is_reconciled_to_history_authority(service):
    project = await service.create_project("existing", "feature", "继续完成")
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-existing")
    root = store.begin_turn(
        project.id,
        user_request="继续原始用户任务",
        request_origin="human",
    )
    history_round_id = root.active_history_round_id
    review = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:existing-recovery",
        native_turn_id=None,
        title="本轮结果",
        content="已完成",
        user_request="继续原始用户任务",
        request_origin="system_recovery",
    )
    assert review.request_origin == "system_recovery"
    review.input_payload = {
        **review.input_payload,
        "history_round_id": history_round_id,
    }
    service.session.add(review)
    service.session.commit()

    reconciled = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:existing-recovery",
        native_turn_id="turn-existing",
        title="本轮结果",
        content="已完成",
        user_request="继续原始用户任务",
        request_origin="system_recovery",
    )

    assert reconciled.request_origin == "human"
    assert reconciled.input_payload["request_origin"] == "human"


@pytest.mark.asyncio
async def test_project_history_keeps_instruction_steer_and_delivered_report(service):
    project = await service.create_project(
        "history", "feature", "完成第一版", creator_open_id="ou-history"
    )
    store = RootSessionStore(service.session)
    store.set_native_thread(project.id, "thread-before-new")
    store.begin_turn(
        project.id,
        user_request="先完成移动端入口",
        request_origin="human",
    )
    store.append_history_instruction(project.id, "补充：保留桌面入口")

    review = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-history",
        native_turn_id="turn-history",
        title="本轮结果",
        content="移动端和桌面入口均已完成。",
        user_request="先完成移动端入口",
        report_card={"color": "blue"},
    )

    rounds = service.get_project_history_rounds(project.id)
    assert len(rounds) == 1
    round_row = rounds[0]
    assert [item["text"] for item in round_row.user_instructions] == [
        "先完成移动端入口",
        "补充：保留桌面入口",
    ]
    assert round_row.native_thread_id == "thread-before-new"
    assert round_row.native_turn_id == "turn-history"
    assert round_row.report_content == "移动端和桌面入口均已完成。"
    assert round_row.report_status == "completed"
    assert review.input_payload["history_round_id"] == round_row.id
    assert store.ensure(project.id).active_history_round_id is None

    # Replacing the provider thread is a conversation operation, not a project
    # history reset.
    store.clear_native_thread(project.id)
    assert service.get_project_history_rounds(project.id)[0].id == round_row.id

    other = await service.create_project("other", "feature", "另一个项目")
    assert service.get_project_history_rounds(other.id) == []


@pytest.mark.asyncio
async def test_project_history_backfills_legacy_owner_review_without_execution_detail(service):
    project = await service.create_project("legacy-history", "feature", "历史项目")
    service.session.add(
        ProjectOwnerReview(
            id="legacy-review",
            project_id=project.id,
            request_item_id="report:legacy-turn",
            native_turn_id="legacy-turn",
            request_origin="human",
            outcome="completed",
            status="completed",
            input_payload={
                "title": "历史汇报",
                "content": "历史结果已展示。",
                "user_request": "完成历史任务",
                "report_card": {"color": "blue"},
                "hidden_tool_output": "不得进入历史",
            },
        )
    )
    service.session.commit()

    rounds = service.get_project_history_rounds(project.id)

    assert len(rounds) == 1
    assert rounds[0].source == "owner_review_backfill"
    assert rounds[0].user_instructions[0]["text"] == "完成历史任务"
    assert rounds[0].report_content == "历史结果已展示。"
    assert "hidden_tool_output" not in str(rounds[0].model_dump())
    assert len(service.session.exec(select(ProjectHistoryRound)).all()) == 1


@pytest.mark.asyncio
async def test_legacy_history_is_backfilled_before_a_new_round_gets_its_sequence(service):
    project = await service.create_project("ordered-history", "feature", "历史项目")
    service.session.add(
        ProjectOwnerReview(
            id="ordered-legacy-review",
            project_id=project.id,
            request_item_id="report:ordered-legacy",
            native_turn_id="ordered-legacy",
            request_origin="human",
            outcome="completed",
            status="completed",
            input_payload={
                "title": "旧汇报",
                "content": "旧结果",
                "user_request": "旧指令",
                "report_card": {"color": "blue"},
            },
        )
    )
    service.session.commit()

    RootSessionStore(service.session).begin_turn(
        project.id, user_request="新指令", request_origin="human"
    )
    rounds = service.get_project_history_rounds(project.id)

    assert [(row.sequence, row.source) for row in rounds] == [
        (1, "owner_review_backfill"),
        (2, "native_turn"),
    ]


@pytest.mark.asyncio
async def test_reconcile_owner_assessments_follows_native_question_lifecycle(service):
    project = await service.create_project("assessment", "feature", "choose")
    answered = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "answered",
        [{"question_id": "direction", "question": "方向？"}],
        request_item_id="request-answered",
    )
    question = service.get_questions_for_interaction(answered.id)[0]
    await service.submit_question_answer(
        question.id, "稳健", "优先稳健", False, "ou-user"
    )
    await service.resolve_question_interaction(
        answered.id, resolution_source="user"
    )
    expired = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "expired",
        [{"question_id": "scope", "question": "范围？"}],
        request_item_id="request-expired",
    )
    expired.status = "expired"
    service.session.add(expired)
    service.session.commit()
    pending = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "pending",
        [{"question_id": "cost", "question": "成本？"}],
        request_item_id="request-pending",
    )

    decisions = []
    for request_item_id in (
        "request-answered",
        "request-expired",
        "request-pending",
    ):
        decisions.append(
            service.create_project_decision(
                project.id,
                request_item_id=f"assessment:{request_item_id}",
                native_turn_id="turn",
                question="需要选择",
                options=[],
                decision="请用户决定",
                rationale="缺少依据",
                impact="",
                reversible=False,
                confidence="low",
                answer_payload={"requires_user": True},
                context_refs=[],
                actor_model="owner-test",
                status="requires_user",
                decision_type="assessment",
            )
        )

    repaired = service.reconcile_project_owner_assessments()

    assert [row.id for row in repaired] == [decisions[0].id, decisions[1].id]
    assert service.get_project_decision(decisions[0].id).status == "resolved_by_user"
    assert service.get_project_decision(decisions[0].id).user_override == "优先稳健"
    assert service.get_project_decision(decisions[1].id).status == "expired"
    assert service.get_project_decision(decisions[2].id).status == "requires_user"
    assert service.get_question_interaction(pending.id).status == "pending"


@pytest.mark.asyncio
async def test_project_owner_review_freshness_tracks_newer_work(service):
    project = await service.create_project("demo", "feature", "build it")
    root_store = RootSessionStore(service.session)
    root_store.ensure(project.id)
    first = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-1",
        native_turn_id="turn-1",
        title="本轮结果",
        content="第一轮",
        user_request="完成第一轮",
    )
    assert service.is_project_owner_review_current(first.id) is True

    second = service.ensure_project_owner_review_job(
        project.id,
        request_item_id="report:turn-2",
        native_turn_id="turn-2",
        title="本轮结果",
        content="第二轮",
        user_request="完成第二轮",
    )
    assert service.is_project_owner_review_current(first.id) is False
    assert service.is_project_owner_review_current(second.id) is True

    # The durable Root session is authoritative even when the denormalized
    # Project projection briefly disagrees with it.
    project.execution_status = "running"
    service.session.add(project)
    service.session.commit()
    assert service.is_project_owner_review_current(second.id) is True

    root_store.set_turn(project.id, turn_id="turn-3", status="running")
    assert service.is_project_owner_review_current(second.id) is False
    superseded = service.supersede_project_owner_review(first.id)
    assert superseded.status == "superseded"


@pytest.mark.asyncio
async def test_chat_project_is_selected_for_creator(service):
    project = await service.create_project(
        "demo",
        "feature",
        "build it",
        creator_open_id="ou-1",
        feishu_chat_id="oc-1",
    )
    assert service.get_chat_user_active_project("oc-1", "ou-1").id == project.id
    assert [item.id for item in service.list_projects_for_chat("oc-1")] == [project.id]


@pytest.mark.asyncio
async def test_user_choice_history_is_cross_project_but_only_explicit_user_answers(service):
    first = await service.create_project(
        "first", "feature", "build", creator_open_id="ou-1"
    )
    second = await service.create_project(
        "second", "feature", "build", creator_open_id="ou-1"
    )
    interaction = await service.create_question_interaction(
        first.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "preference-style",
        [{
            "question_id": "style",
            "question": "更喜欢哪种界面？",
            "options": [{"key": "简洁", "label": "简洁"}],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    await service.submit_question_answer(
        question.id,
        "简洁",
        "简洁",
        False,
        "ou-1",
        answer_source="user",
    )

    assert service.get_project_owner_open_id(second.id) == "ou-1"
    history = service.get_user_choice_history("ou-1")
    assert history == [{
        "time": history[0]["time"],
        "project": "first",
        "kind": "user_choice",
        "question": "更喜欢哪种界面？",
        "choice": "简洁",
        "source_ref": history[0]["source_ref"],
    }]
    project_choices = service.get_project_user_choice_history(
        first.id, open_id="ou-1"
    )
    assert len(project_choices) == 1
    assert project_choices[0]["kind"] == "user_choice"
    assert project_choices[0]["question"] == "更喜欢哪种界面？"
    assert project_choices[0]["choice"] == "简洁"
    assert project_choices[0]["options"] == [{"label": "简洁", "description": ""}]
    assert project_choices[0]["source"] == "native_question"
    assert project_choices[0]["responded_by"] == "ou-1"
    assert service.get_project_user_choice_history(
        second.id, open_id="ou-1"
    ) == []
    choice_log = service.session.exec(
        select(EventLog).where(EventLog.message == "User choice recorded")
    ).one()
    assert choice_log.project_id == first.id
    assert choice_log.meta == {
        "kind": "user_choice",
        "source": "native_question",
        "interaction_id": interaction.id,
        "question_id": question.id,
        "question": "更喜欢哪种界面？",
        "options": [{"label": "简洁", "description": ""}],
        "context": {
            "reason": question.reason,
            "default_assumption": question.default_assumption,
            "recommended_option": question.recommended_option,
            "stage": "conversation",
            "request_item_id": interaction.request_item_id,
        },
        "choice": "简洁",
        "skipped": False,
        "responded_by": "ou-1",
        "answer_ref": history[0]["source_ref"],
    }


@pytest.mark.asyncio
async def test_user_decision_cases_keep_choice_context_and_owner_correction(service):
    project = await service.create_project(
        "decision-cases", "feature", "build", creator_open_id="ou-cases"
    )
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "style-choice",
        [{
            "question_id": "style",
            "question": "界面保持简洁还是增加动画？",
            "options": [
                {"key": "simple", "label": "保持简洁", "description": "减少干扰"},
                {"key": "motion", "label": "增加动画", "description": "更有表现力"},
            ],
        }],
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    answer = await service.submit_question_answer(
        question.id,
        "保持简洁",
        "保持简洁",
        False,
        "ou-cases",
        answer_source="user",
    )
    decision = service.create_project_decision(
        project.id,
        request_item_id="report:style",
        native_turn_id="turn-style",
        question="下一轮是否增加复杂动画？",
        options=[
            {"label": "增加复杂动画", "description": "强化表现"},
            {"label": "保持简洁", "description": "避免复杂化"},
        ],
        decision="增加复杂动画",
        rationale="Owner 推断用户想提升表现力",
        impact="页面会更复杂",
        reversible=True,
        confidence="medium",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
    )
    service.update_project_decision(
        decision.id,
        status="adjusted",
        user_override="保持简洁，不增加复杂动画",
    )

    cases = service.get_user_decision_cases("ou-cases")

    assert [item["kind"] for item in cases] == [
        "owner_correction",
        "user_choice",
    ]
    assert cases[0]["rejected_owner_decision"] == "增加复杂动画"
    assert cases[0]["user_choice"] == "保持简洁，不增加复杂动画"
    assert cases[0]["authority_class"] == "explicit_human_correction"
    assert [item["label"] for item in cases[1]["options"]] == [
        "保持简洁",
        "增加动画",
    ]
    assert service.get_user_decision_cases(
        "ou-cases", before=answer.responded_at
    ) == []


@pytest.mark.asyncio
async def test_owner_choices_persist_visible_context_and_are_not_corrections(service):
    project = await service.create_project(
        "saved-choices", "feature", "完善手机体验", creator_open_id="ou-choices"
    )
    options = [
        {"label": "  改善手机布局  ", "description": "说明" * 150, "action": "execute"},
        {"label": "保持现状", "description": "记录意见，不继续修改", "action": "record_only"},
    ]
    public_context = {
        "human_context": [{"text": "下一步给我选，不要自己换方向", "source_ref": "human:1"}],
        "source_report": {"title": "本轮结果", "content": "手机访问已通过，布局仍可改善。"},
        "hidden_trace": "not-public",
    }
    decision = service.create_project_decision(
        project.id,
        request_item_id="report:choice",
        native_turn_id="turn-choice",
        question="下一步怎么推进？",
        options=options,
        decision="优先改善手机布局",
        rationale="核心功能已经可用，手机布局最影响阅读",
        impact="不增加新功能",
        reversible=True,
        confidence="high",
        answer_payload=public_context,
        context_refs=["human:1"],
        actor_model="owner-model",
        status="requires_user",
        decision_type="advice",
    )
    service.update_project_decision(
        decision.id,
        status="resolved_by_user",
        card_message_id="om-choice",
        user_override="保持现状",
        user_choice_actor="ou-choices",
    )
    service.session.expire_all()
    reopened = ProjectService(service.session)
    case = reopened.get_user_decision_cases("ou-choices", project_id=project.id)[0]
    event = service.session.exec(
        select(EventLog).where(EventLog.message == "User choice recorded")
    ).one()

    assert case["kind"] == "user_choice"
    assert case["authority_class"] == "explicit_human_choice"
    assert case["is_correction"] is False
    assert case["rejected_owner_decision"] == ""
    assert case["responded_by"] == "ou-choices"
    assert case["source"] == "project_owner_decision"
    assert case["choice_action"] == "record_only"
    assert case["user_choice"] == "保持现状"
    assert case["context"]["human_context"] == public_context["human_context"]
    assert case["context"]["source_report"] == public_context["source_report"]
    assert case["context"]["recommendation"] == "优先改善手机布局"
    assert "hidden_trace" not in case["context"]
    assert case["options"] == [
        {"label": "改善手机布局", "description": "说明" * 120},
        {"label": "保持现状", "description": "记录意见，不继续修改"},
    ]
    assert event.meta["context"] == case["context"]
    assert event.meta["options"] == case["options"]
    assert event.meta["choice"] == case["user_choice"]
    assert event.meta["card_message_id"] == "om-choice"
    project_choice = reopened.get_project_user_choice_history(project.id, open_id="ou-choices")[0]
    assert project_choice["kind"] == "user_choice"
    assert project_choice["context"] == case["context"]
    assert project_choice["options"] == case["options"]
    cross_project_case = reopened.get_user_decision_cases("ou-choices")[0]
    assert "source_report" not in cross_project_case["context"]
    assert "human_context" not in cross_project_case["context"]
    assert reopened.get_project_policies(project.id) == []


@pytest.mark.asyncio
async def test_owner_choice_learning_excludes_another_users_selection(service):
    project = await service.create_project(
        "shared-choice", "feature", "build", creator_open_id="ou-owner"
    )
    decision = service.create_project_decision(
        project.id,
        request_item_id="report:shared-choice",
        native_turn_id="turn-shared-choice",
        question="接下来做什么？",
        options=[{"label": "保持现状", "description": "不继续修改", "action": "record_only"}],
        decision="保持现状",
        rationale="功能已可用",
        impact="不修改",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        actor_model="owner-model",
    )
    service.update_project_decision(
        decision.id, status="resolved_by_user", user_override="保持现状",
        user_choice_actor="ou-collaborator",
    )

    assert service.get_user_decision_cases("ou-owner", project_id=project.id) == []
    assert service.get_project_user_choice_history(project.id, open_id="ou-owner") == []


@pytest.mark.asyncio
async def test_project_owner_switch_is_project_scoped_and_defaults_on(service):
    first = await service.create_project(
        "first-owner-switch", "feature", "完成第一个项目", creator_open_id="ou-1"
    )
    second = await service.create_project(
        "second-owner-switch", "feature", "完成第二个项目", creator_open_id="ou-1"
    )
    assert service.is_project_owner_enabled(first.id) is True
    assert service.is_project_owner_enabled(second.id) is True
    review = service.ensure_project_owner_review_job(
        first.id,
        request_item_id="report:owner-switch",
        native_turn_id="turn-owner-switch",
        title="本轮结果",
        content="等待 Owner 审阅",
        user_request="完成项目",
    )
    service.mark_project_owner_review_running(review.id)

    state = service.set_project_owner_enabled(first.id, False)

    assert state.enabled is False
    assert service.is_project_owner_enabled(first.id) is False
    assert service.is_project_owner_enabled(second.id) is True
    assert service.get_project_owner_review_by_request_item_id(
        first.id, "report:owner-switch"
    ).status == "superseded"
    assert service.mark_project_owner_review_running(review.id) is None

    state = service.set_project_owner_enabled(first.id, True)
    assert state.enabled is True
    assert service.is_project_owner_enabled(first.id) is True


@pytest.mark.asyncio
async def test_open_owner_commitments_are_not_lost_by_recent_history_limit(service):
    project = await service.create_project("commitments", "feature", "完成项目")
    active = service.create_project_decision(
        project.id,
        request_item_id="report:active",
        native_turn_id="turn-active",
        question="下一步",
        options=[],
        decision="完成全部验收",
        rationale="目标尚未完成",
        impact="继续执行",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        actor_model="owner-test",
        status="applied",
        decision_type="governance",
        execution_status="active",
    )
    for index in range(7):
        service.create_project_decision(
            project.id,
            request_item_id=f"report:done-{index}",
            native_turn_id=f"turn-done-{index}",
            question="历史事项",
            options=[],
            decision=f"已完成事项 {index}",
            rationale="历史",
            impact="无",
            reversible=True,
            confidence="high",
            answer_payload={},
            context_refs=[],
            actor_model="owner-test",
            status="applied",
            decision_type="governance",
            execution_status="completed",
        )

    assert active.id not in {
        item.id for item in service.get_project_decisions(project.id, limit=5)
    }
    assert [
        item.id for item in service.get_open_project_commitments(project.id)
    ] == [active.id]


@pytest.mark.asyncio
async def test_owner_memory_separates_global_and_project_scopes(service):
    first = await service.create_project(
        "first-memory", "feature", "first", creator_open_id="ou-memory"
    )
    second = await service.create_project(
        "second-memory", "feature", "second", creator_open_id="ou-memory"
    )
    service.add_owner_memories(
        "ou-memory",
        [
            {
                "preference": "低风险可撤销决定由 AI 代办",
                "evidence": "低风险可撤销决定由 AI 代办",
                "category": "autonomy",
                "scope": "global",
                "strength": "hard",
            },
            {
                "preference": "这个项目保持 research-only",
                "evidence": "这个项目保持 research-only",
                "category": "risk",
                "scope": "project",
                "strength": "hard",
            },
        ],
        project_id=first.id,
        source_ref="report:1",
    )

    first_memory = service.get_owner_memories(
        "ou-memory", project_id=first.id, limit=10
    )
    second_memory = service.get_owner_memories(
        "ou-memory", project_id=second.id, limit=10
    )
    assert {item.content for item in first_memory} == {
        "低风险可撤销决定由 AI 代办",
        "这个项目保持 research-only",
    }
    assert {item.content for item in second_memory} == {
        "低风险可撤销决定由 AI 代办"
    }
    assert {item.source_project_id for item in first_memory} == {first.id}
    assert second_memory[0].source_project_id == first.id
    assert {
        item.content
        for item in service.get_effective_owner_memories(
            "ou-memory", project_id=first.id
        )
    } == {
        "低风险可撤销决定由 AI 代办",
        "这个项目保持 research-only",
    }
    assert len(service.session.exec(select(UserOwnerMemory)).all()) == 2


@pytest.mark.asyncio
async def test_owner_global_memory_follows_human_across_bot_accounts(service):
    primary = service
    secondary = ProjectService(service.session, bot_account_id="research")
    primary.register_user_identity(
        "ou-primary", user_id="tenant-user-1", union_id="union-1"
    )
    secondary.register_user_identity(
        "ou-secondary", user_id="tenant-user-1", union_id="union-1"
    )
    first = await primary.create_project(
        "primary", "feature", "first", creator_open_id="ou-primary"
    )
    second = await secondary.create_project(
        "secondary", "feature", "second", creator_open_id="ou-secondary"
    )
    primary.add_owner_sources(
        "ou-primary",
        [{
            "provider": "mac",
            "source_ref": "human:shared:1",
            "content": "所有项目都保持架构简单。",
        }],
    )
    added = primary.add_owner_memories(
        "ou-primary",
        [
            {
                "preference": "所有项目都保持架构简单",
                "evidence": "所有项目都保持架构简单",
                "scope": "global",
                "source_refs": ["human:shared:1"],
            },
            {
                "preference": "仅主项目保持只读",
                "evidence": "仅主项目保持只读",
                "scope": "project",
                "source_refs": ["human:project:1"],
            },
        ],
        project_id=first.id,
        source_ref="human:shared:1",
    )

    assert {item.content for item in secondary.get_owner_memories(
        "ou-secondary", project_id=second.id
    )} == {"所有项目都保持架构简单"}
    assert [
        item.source_ref for item in secondary.get_owner_sources("ou-secondary")
    ] == ["human:shared:1"]
    assert [
        item.content
        for item in secondary.get_effective_owner_memories(
            "ou-secondary", project_id=second.id
        )
    ] == ["所有项目都保持架构简单"]
    assert all(item.human_id == "feishu-user:tenant-user-1" for item in added)


def test_effective_owner_memory_requires_durable_human_evidence(service):
    service.add_owner_sources(
        "ou-provenance",
        [
            {
                "provider": "codex",
                "kind": "human_input",
                "source_ref": "codex:human:1",
                "content": "架构保持简单，避免没有必要的流程。",
            },
            {
                "provider": "agent_doc",
                "kind": "user_document",
                "source_ref": "agent_doc:repo",
                "content": "所有修改前必须创建备份。",
            },
        ],
    )
    service.add_owner_memories(
        "ou-provenance",
        [
            {
                "preference": "架构保持简单",
                "evidence": "架构保持简单",
                "scope": "global",
                "source_refs": ["legacy:candidate:human"],
            },
            {
                "preference": "所有修改前必须创建备份",
                "evidence": "所有修改前必须创建备份",
                "scope": "global",
                "source_refs": ["legacy:candidate:agent-doc"],
            },
        ],
        project_id=None,
        source_ref="legacy-import",
    )

    assert {
        item.content
        for item in service.get_effective_owner_memories("ou-provenance")
    } == {"架构保持简单"}
    assert len(service.get_owner_memories("ou-provenance")) == 2


def test_effective_owner_memory_accepts_legacy_feishu_human_evidence(service):
    event = FeishuEventLog(
        open_id="ou-feishu-memory",
        event_type="message",
        text="把证据和未解决项通过卡片汇报。",
        status="received",
    )
    service.session.add(event)
    service.session.commit()
    service.session.refresh(event)
    memory = UserOwnerMemory(
        id="memory-feishu-legacy",
        open_id="ou-feishu-memory",
        scope="global",
        category="reporting",
        content="结果卡包含证据和未解决项",
        strength="hard",
        evidence="把证据和未解决项通过卡片汇报",
        source_ref=f"feishu_event:{event.id}",
        source_refs=[],
    )
    service.session.add(memory)
    service.session.commit()

    assert [
        item.id
        for item in service.get_effective_owner_memories("ou-feishu-memory")
    ] == [memory.id]


def test_owner_raw_sources_are_deduplicated_and_searchable(service):
    added = service.add_owner_sources(
        "ou-history",
        [
            {
                "provider": "codex",
                "source_ref": "codex:1",
                "timestamp": "2026-01-01T00:00:00",
                "session_ref": "session-1",
                "turn_ref": "turn-1",
                "project_ref": "project-1",
                "context_before": "方案 A 还是 B？",
                "context_after": "已采用 A",
                "content": "架构尽量简单，参考 https://example.com/design",
            },
            {
                "provider": "codex",
                "source_ref": "codex:1",
                "content": "duplicate",
            },
            {
                "provider": "kimi",
                "source_ref": "kimi:2",
                "content": "重要信息完整展示",
            },
        ],
    )

    assert added == 2
    matches = service.get_owner_sources("ou-history", query="架构尽量简单")
    assert [row.source_ref for row in matches] == ["codex:1"]
    assert "https://example.com/design" in matches[0].content
    contextual = service.get_contextual_user_history(
        "ou-history", query="架构尽量简单"
    )
    assert contextual[0]["human_input"].startswith("架构尽量简单")
    assert contextual[0]["context_before"] == "方案 A 还是 B？"
    assert contextual[0]["context_after"] == "已采用 A"


@pytest.mark.asyncio
async def test_mac_history_archive_keeps_turn_context_and_legacy_messages(service):
    project = await service.create_project(
        "demo", "feature", "build it", creator_open_id="ou-context"
    )
    first = service.begin_project_history_round(
        project.id, user_request="先实现基础版本", request_origin="human"
    )
    first.native_turn_id = "turn-1"
    first.report_title = "结果"
    first.report_content = "基础版本已经完成"
    first.report_ref = "report-1"
    first.report_status = "completed"
    service.session.add(first)
    service.session.commit()
    second = service.begin_project_history_round(
        project.id,
        user_request="同意",
        request_origin="human",
        reply_context={
            "kind": "decision",
            "subject_type": "project_decision",
            "subject_ref": "decision-next-step",
            "summary": "是否继续完成下一步？",
            "request": "基础版本完成后是否继续下一步",
            "result": "基础版本已经完成",
        },
    )
    second.native_turn_id = "turn-2"
    second.report_title = "结果"
    second.report_content = "已经完成下一步"
    second.report_ref = "report-2"
    second.report_status = "completed"
    service.session.add(second)
    service.session.commit()
    legacy_reply = service.begin_project_history_round(
        project.id,
        user_request="继续",
        request_origin="human",
        reply_context={
            "kind": "report",
            "subject_type": "root_turn",
            "subject_ref": "turn-1",
            "summary": "基础版本结果",
        },
    )
    legacy_reply.report_content = "已经继续处理"
    legacy_reply.report_status = "completed"
    service.session.add(legacy_reply)
    service.session.commit()
    service.session.add(
        FeishuEventLog(
            open_id="ou-context",
            chat_type="p2p",
            event_type="message",
            message_id="legacy-1",
            text="迁移前用户纠正",
            status="received",
        )
    )
    service.session.add(
        FeishuEventLog(
            open_id="ou-context",
            chat_type="p2p",
            event_type="message",
            message_id="command-1",
            text="保持架构简洁",
            status="received",
        )
    )
    interaction = QuestionInteraction(
        id="interaction-1",
        project_id=project.id,
        batch_key="batch-1",
        request_item_id="request-1",
        status="answered",
    )
    question = Question(
        id="question-1",
        interaction_id=interaction.id,
        question_id="q1",
        question="你更希望先保证速度还是覆盖率？",
        options=[],
    )
    answer = QuestionAnswer(
        id="answer-1",
        question_id=question.id,
        answer_label="优先覆盖率",
        answer_source="user",
        responded_by="ou-context",
    )
    service.session.add(interaction)
    service.session.add(question)
    service.session.add(answer)
    service.session.commit()

    rows = service.contextual_mac_history_sources("ou-context")
    by_content = {row["content"]: row for row in rows}

    assert by_content["同意"]["context_before"] == (
        "原请求：基础版本完成后是否继续下一步；当轮结果：基础版本已经完成"
    )
    assert by_content["同意"]["context_after"] == "已经完成下一步"
    assert by_content["继续"]["context_before"] == (
        "原请求：先实现基础版本；当轮结果：基础版本已经完成"
    )
    assert by_content["迁移前用户纠正"]["source_ref"] == "mac:feishu:legacy-1"
    assert by_content["保持架构简洁"]["source_ref"] == "mac:feishu:command-1"
    assert by_content["优先覆盖率"]["context_before"] == "你更希望先保证速度还是覆盖率？"
    assert by_content["优先覆盖率"]["kind"] == "human_choice"
    assert first.id != second.id


def test_owner_raw_sources_sync_removes_stale_scanned_records_only(service):
    service.add_owner_sources(
        "ou-history",
        [
            {"provider": "kimi", "source_ref": "kimi:generated", "content": "old"},
            {"provider": "codex", "source_ref": "codex:keep", "content": "keep"},
            {"provider": "manual", "source_ref": "manual:keep", "content": "manual"},
        ],
    )

    added, removed = service.sync_owner_sources(
        "ou-history",
        [
            {"provider": "kimi", "source_ref": "kimi:human", "content": "human"},
            {"provider": "codex", "source_ref": "codex:keep", "content": "keep"},
        ],
        providers={"kimi", "codex"},
    )

    assert (added, removed) == (1, 1)
    rows = service.get_owner_sources("ou-history", limit=10)
    assert {row.source_ref for row in rows} == {
        "kimi:human",
        "codex:keep",
        "manual:keep",
    }


def test_owner_memory_reinforces_repeated_evidence(service):
    service.add_owner_memories(
        "ou-reinforce",
        [{
            "preference": "同等效果优先简单方案",
            "evidence": "第一次选择简单方案",
            "scope": "global",
            "confidence": "medium",
            "learning_type": "repeated_choice",
            "source_refs": ["choice-1"],
        }],
        project_id=None,
        source_ref="choice-1",
    )
    service.add_owner_memories(
        "ou-reinforce",
        [{
            "preference": "同等效果优先简单方案",
            "evidence": "第二次选择简单方案",
            "scope": "global",
            "confidence": "medium",
            "learning_type": "repeated_choice",
            "source_refs": ["choice-2"],
        }],
        project_id=None,
        source_ref="choice-2",
    )

    memory = service.get_owner_memories("ou-reinforce")[0]
    assert memory.reinforcement_count == 2
    assert memory.confidence == "high"
    assert memory.learning_type == "repeated_choice"
    assert memory.source_refs == ["choice-1", "choice-2"]


def test_owner_memory_import_accepts_global_memory_without_project(service):
    added = service.add_owner_memories(
        "ou-import",
        [
            {
                "preference": "架构尽量简单",
                "evidence": "架构尽量简单",
                "scope": "global",
                "category": "architecture",
                "strength": "hard",
            },
            {
                "preference": "这个项目只读",
                "evidence": "这个项目只读",
                "scope": "project",
            },
        ],
        project_id=None,
        source_ref="history-import:test",
    )

    assert [row.content for row in added] == ["架构尽量简单"]
    assert added[0].source_project_id is None
    assert added[0].project_id is None


@pytest.mark.asyncio
async def test_owner_memory_correction_supersedes_only_exact_same_scope(service):
    project = await service.create_project(
        "memory-correction", "feature", "demo", creator_open_id="ou-correction"
    )
    service.add_owner_memories(
        "ou-correction",
        [{
            "preference": "所有变更都先问我",
            "evidence": "所有变更都先问我",
            "category": "autonomy",
            "scope": "global",
            "strength": "hard",
        }],
        project_id=project.id,
        source_ref="message-old",
    )
    added = service.add_owner_memories(
        "ou-correction",
        [{
            "preference": "低风险可撤销变更由 AI 决定",
            "evidence": "低风险可撤销变更由 AI 决定",
            "category": "autonomy",
            "scope": "global",
            "strength": "hard",
            "replaces": "所有变更都先问我",
        }],
        project_id=project.id,
        source_ref="message-new",
    )

    active = service.get_owner_memories(
        "ou-correction", project_id=project.id, limit=10
    )
    rows = service.session.exec(select(UserOwnerMemory)).all()
    old = next(row for row in rows if row.content == "所有变更都先问我")
    assert [row.content for row in active] == ["低风险可撤销变更由 AI 决定"]
    assert old.status == "superseded"
    assert added[0].supersedes_id == old.id


@pytest.mark.asyncio
async def test_owner_memory_correction_accepts_stable_memory_reference(service):
    project = await service.create_project(
        "memory-ref-correction", "feature", "demo", creator_open_id="ou-ref"
    )
    old = service.add_owner_memories(
        "ou-ref",
        [{
            "preference": "任何选择都必须问用户",
            "evidence": "任何选择都必须问用户",
            "category": "autonomy",
            "scope": "global",
            "strength": "hard",
        }],
        project_id=project.id,
        source_ref="message-old-ref",
    )[0]

    replacement = service.add_owner_memories(
        "ou-ref",
        [{
            "preference": "低风险可撤销选择由 AI 决定",
            "evidence": "低风险可撤销选择由 AI 决定",
            "category": "autonomy",
            "scope": "global",
            "strength": "hard",
            "replaces": old.id,
        }],
        project_id=project.id,
        source_ref="message-new-ref",
    )[0]

    service.session.refresh(old)
    assert old.status == "superseded"
    assert replacement.supersedes_id == old.id


@pytest.mark.asyncio
async def test_project_policy_is_project_scoped_and_exactly_superseded(service):
    first = await service.create_project("policy-a", "feature", "first")
    second = await service.create_project("policy-b", "feature", "second")
    old = service.add_project_policies(
        first.id,
        [{
            "policy": "历史样本完全不可访问",
            "evidence": "历史样本完全不可访问",
            "source_ref": "round-1:instruction:1",
            "kind": "constraint",
            "category": "data_usage",
            "strength": "hard",
        }],
        source_ref="report:1",
    )[0]
    replacement = service.add_project_policies(
        first.id,
        [{
            "policy": "历史样本可作为已暴露样本使用，但不能反复调参",
            "evidence": "可以使用，但不能反复调参",
            "source_ref": "round-2:instruction:1",
            "kind": "definition",
            "category": "data_usage",
            "strength": "hard",
            "replaces": "历史样本完全不可访问",
        }],
        source_ref="report:2",
    )[0]

    service.session.refresh(old)
    assert old.status == "superseded"
    assert replacement.supersedes_id == old.id
    assert [row.content for row in service.get_project_policies(first.id)] == [
        "历史样本可作为已暴露样本使用，但不能反复调参"
    ]
    assert service.get_project_policies(second.id) == []
    assert len(service.session.exec(select(ProjectPolicy)).all()) == 2


@pytest.mark.asyncio
async def test_project_policy_does_not_default_to_hard(service):
    project = await service.create_project("policy-default", "feature", "demo")

    policy = service.add_project_policies(
        project.id,
        [{
            "policy": "后续汇报优先说明主指标",
            "evidence": "汇报时优先说明主指标",
            "source_ref": "round-default:instruction:1",
            "kind": "principle",
            "category": "presentation",
        }],
        source_ref="round-default",
    )[0]

    assert policy.strength == "default"


@pytest.mark.asyncio
async def test_project_policy_keeps_all_generalization_sources(service):
    project = await service.create_project("policy-sources", "feature", "demo")
    supporting_sources = [
        {
            "source_ref": "round-1:instruction:1",
            "source_time": "2026-08-20T10:00:00",
            "original_text": "之前的结果可以复用",
        },
        {
            "source_ref": "round-2:instruction:1",
            "source_time": "2026-08-21T10:00:00",
            "original_text": "不要重复做同一个页面",
        },
    ]

    policy = service.add_project_policies(
        project.id,
        [{
            "policy": "已有产物仍满足目标时优先复用",
            "evidence": "之前的结果可以复用 | 不要重复做同一个页面",
            "source_ref": "round-1:instruction:1",
            "source_refs": [item["source_ref"] for item in supporting_sources],
            "source_context": {
                **supporting_sources[0],
                "supporting_sources": supporting_sources,
            },
            "kind": "inferred",
            "category": "reuse",
        }],
        source_ref="round-1",
    )[0]

    assert policy.kind == "inferred"
    assert policy.strength == "default"
    assert policy.source_context["supporting_sources"] == supporting_sources


@pytest.mark.asyncio
async def test_project_policy_correction_accepts_stable_policy_reference(service):
    project = await service.create_project("policy-ref", "feature", "demo")
    old = service.add_project_policies(
        project.id,
        [{
            "policy": "封箱期完全不能访问历史数据",
            "evidence": "封箱期完全不能访问历史数据",
            "source_ref": "round-old:instruction:1",
            "kind": "constraint",
            "category": "data_usage",
            "strength": "hard",
        }],
        source_ref="round-old",
    )[0]

    replacement = service.add_project_policies(
        project.id,
        [{
            "policy": "已暴露历史数据可以使用，但不能再称为未触碰样本",
            "evidence": "已暴露历史数据可以使用",
            "source_ref": "round-new:instruction:1",
            "kind": "definition",
            "category": "data_usage",
            "strength": "hard",
            "replaces": old.id,
        }],
        source_ref="round-new",
    )[0]

    service.session.refresh(old)
    assert old.status == "superseded"
    assert replacement.supersedes_id == old.id


@pytest.mark.asyncio
async def test_project_policy_correction_keeps_unfinished_ai_state_visible_for_reconciliation(
    service,
):
    project = await service.create_project("policy-lineage", "feature", "demo")
    old_rules = service.add_project_policies(
        project.id,
        [
            {
                "policy": "基准使用固定名单",
                "evidence": "基准使用固定名单",
                "source_ref": "round-old:instruction:1",
                "kind": "definition",
                "category": "benchmark",
                "strength": "hard",
            },
            {
                "policy": "成本按单边千一计算",
                "evidence": "成本按单边千一计算",
                "source_ref": "round-old:instruction:2",
                "kind": "definition",
                "category": "cost",
                "strength": "hard",
            },
        ],
        source_ref="round-old",
    )
    decision = service.create_project_decision(
        project.id,
        request_item_id="report-old",
        native_turn_id="turn-old",
        question="下一步",
        options=[],
        decision="按旧口径继续",
        rationale="旧规则支持",
        impact="继续回测",
        reversible=True,
        confidence="high",
        answer_payload={},
        context_refs=[],
        authority_refs=[row.id for row in old_rules],
        actor_model="owner-test",
        decision_type="governance",
        execution_status="active",
    )

    replacement = service.add_project_policies(
        project.id,
        [{
            "policy": "基准与成本都以本轮用户给出的新口径为准",
            "evidence": "固定名单和千一成本都不要再用",
            "source_ref": "round-new:instruction:1",
            "source_time": "2026-08-15T12:30:00",
            "source_context": {
                "original_text": "固定名单和千一成本都不要再用",
                "context_before": "上一轮仍沿用旧口径",
            },
            "kind": "definition",
            "category": "backtest_contract",
            "strength": "hard",
            "replaces": [row.id for row in old_rules],
        }],
        source_ref="round-new",
    )[0]

    for old in old_rules:
        service.session.refresh(old)
        assert old.status == "superseded"
    service.session.refresh(decision)
    assert decision.execution_status == "active"
    assert [
        row.id for row in service.get_open_project_commitments(project.id)
    ] == [decision.id]
    assert replacement.supersedes_refs == [row.id for row in old_rules]
    assert replacement.source_context["original_text"] == "固定名单和千一成本都不要再用"
    assert replacement.source_time.isoformat(timespec="seconds") == "2026-08-15T12:30:00"
    assert service.session.exec(select(ProjectDecision)).one().id == decision.id


@pytest.mark.asyncio
async def test_owner_memories_are_not_truncated_by_default(service):
    project = await service.create_project(
        "memory-unbounded", "feature", "demo", creator_open_id="ou-unbounded"
    )
    for index in range(35):
        service.add_owner_memories(
            "ou-unbounded",
            [{
                "preference": f"用户偏好 {index}",
                "evidence": f"用户偏好 {index}",
                "category": "general",
                "scope": "global",
                "strength": "default",
            }],
            project_id=project.id,
            source_ref=f"message:{index}",
        )

    memories = service.get_owner_memories(
        "ou-unbounded", project_id=project.id
    )

    assert len(memories) == 35
    assert {item.content for item in memories} == {
        f"用户偏好 {index}" for index in range(35)
    }


@pytest.mark.asyncio
async def test_legacy_owner_profile_bootstrap_preserves_project_scope(service):
    first = await service.create_project(
        "legacy-first", "feature", "first", creator_open_id="ou-legacy"
    )
    second = await service.create_project(
        "legacy-second", "feature", "second", creator_open_id="ou-legacy"
    )
    service.add_explicit_user_preferences(
        "ou-legacy",
        [{
            "preference": "仅这个项目保持只读",
            "evidence": "仅这个项目保持只读",
            "category": "scope",
            "scope": "project",
            "strength": "hard",
        }],
        project_id=first.id,
        source_ref="legacy-message",
    )

    first_memory = service.get_owner_memories(
        "ou-legacy", project_id=first.id, limit=10
    )
    second_memory = service.get_owner_memories(
        "ou-legacy", project_id=second.id, limit=10
    )
    assert [item.content for item in first_memory] == ["仅这个项目保持只读"]
    assert first_memory[0].scope == "project"
    assert first_memory[0].strength == "hard"
    assert second_memory == []


@pytest.mark.asyncio
async def test_question_interaction_claim_is_first_responder_wins(service):
    project = await service.create_project("demo", "feature", "build")
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "owner-race",
        [{"question_id": "choice", "question": "继续吗？", "options": []}],
    )

    assert service.claim_question_interaction(
        interaction.id, resolution_source="project_owner"
    )
    assert not service.claim_question_interaction(
        interaction.id, resolution_source="user"
    )
    claimed = service.get_question_interaction(interaction.id)
    assert claimed.status == "resolving"
    assert claimed.resolution_source == "project_owner"

    service.release_question_interaction(
        interaction.id, resolution_source="project_owner"
    )
    released = service.get_question_interaction(interaction.id)
    assert released.status == "pending"
    assert released.resolution_source is None
    assert service.claim_question_interaction(
        interaction.id, resolution_source="user"
    )


@pytest.mark.asyncio
async def test_pause_resume_only_changes_project_shell_state(service):
    project = await service.create_project("demo", "feature", "build")
    project.execution_status = "running"
    project.waiting_reason = "provider turn is still active"
    service.session.add(project)
    service.session.commit()
    await service.pause_project(project.id)
    assert service.require_project(project.id).status == "paused"
    await service.resume_project(project.id)
    assert service.require_project(project.id).status == "active"
    await service.cancel_project(project.id)
    assert service.require_project(project.id).status == "cancelled"
    assert service.require_project(project.id).execution_status == "running"
    assert service.require_project(project.id).waiting_reason == "provider turn is still active"
    assert service.require_project(project.id).current_state == "ACTIVE"


@pytest.mark.asyncio
async def test_cancel_expires_pending_question_interactions(service):
    project = await service.create_project("demo", "feature", "build")
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "cancel-pending",
        [{"id": "choice", "question": "Continue?", "options": []}],
        request_item_id="request-1",
    )

    await service.cancel_project(project.id)

    refreshed = service.get_question_interaction(interaction.id)
    assert refreshed is not None
    assert refreshed.status == "expired"
    assert refreshed.resolution_source == "project_cancelled"


@pytest.mark.asyncio
async def test_question_batch_round_trip(service):
    project = await service.create_project("demo", "feature", "build")
    approval = await service.create_approval(
        project.id, None, "ROOT_AGENT_WAITING_USER", "input", "style?", []
    )
    interaction = await service.create_question_interaction(
        project.id,
        None,
        1,
        "ACTIVE",
        "root_agent",
        "conversation",
        "batch-1",
        [
            {
                "question_id": "style",
                "kind": "decision",
                "question": "style?",
                "options": [
                    {"key": "simple", "label": "Simple"},
                    {"key": "bold", "label": "Bold"},
                ],
            }
        ],
        approval_id=approval.id,
    )
    question = service.get_questions_for_interaction(interaction.id)[0]
    assert question.recommended_option == "simple"
    assert not service.interaction_is_resolvable(interaction.id)
    await service.submit_question_answer(
        question.id, "bold", "Bold", False, "ou-1"
    )
    assert service.interaction_is_resolvable(interaction.id)
    await service.resolve_question_interaction(interaction.id)
    await service.respond_approval(approval.id, "answer", "ou-1")
    assert service.get_question_interaction(interaction.id).status == "answered"
    assert service.get_approval(approval.id).status == "responded"


@pytest.mark.asyncio
async def test_notification_dedupes_provider_post(service):
    project = await service.create_project("demo", "feature", "build")
    bridge = Bridge()
    kwargs = dict(
        service=service,
        bridge=bridge,
        dedupe_key="same-card",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        card={"schema": "2.0"},
        project_id=project.id,
    )
    first = await deliver_card(**kwargs)
    second = await deliver_card(**kwargs)
    assert first == second == "message-1"
    assert len(bridge.cards) == 1


@pytest.mark.asyncio
async def test_sent_progress_snapshots_are_coalesced_but_reports_are_retained(service):
    project = await service.create_project("demo", "feature", "build")
    bridge = Bridge()
    projection_key = f"root-agent:{project.id}:activity"
    first_message = await deliver_card(
        service,
        bridge,
        dedupe_key="progress-1",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        card={"step": 1},
        project_id=project.id,
        projection_key=projection_key,
    )
    await deliver_card(
        service,
        bridge,
        dedupe_key="progress-2",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        card={"step": 2},
        project_id=project.id,
        update_message_id=first_message,
        projection_key=projection_key,
    )
    await deliver_card(
        service,
        bridge,
        dedupe_key="report-1",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        card={"result": 1},
        project_id=project.id,
        projection_key="root-result:turn-1",
    )
    await deliver_card(
        service,
        bridge,
        dedupe_key="report-2",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        card={"result": 2},
        project_id=project.id,
        projection_key="root-result:turn-2",
    )

    rows = list(service.session.exec(select(NotificationOutbox)).all())
    assert [row.dedupe_key for row in rows if row.intent_type == "progress"] == [
        "progress-2"
    ]
    assert {row.dedupe_key for row in rows if row.intent_type == "report"} == {
        "report-1",
        "report-2",
    }


def test_restart_recovers_safe_card_updates_without_reposting_ambiguous_cards(service):
    update = service.enqueue_notification(
        dedupe_key="progress-update",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        payload={
            "projection_key": "root-agent:p:activity",
            "update_message_id": "message-existing",
        },
    )
    post = service.enqueue_notification(
        dedupe_key="new-report",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        payload={"projection_key": "root-result:turn-1"},
    )
    assert service.claim_notification(update.id) is not None
    assert service.claim_notification(post.id) is not None

    assert service.recover_inflight_notifications() == {
        "retryable_updates": 1,
        "ambiguous_posts": 1,
    }
    service.session.refresh(update)
    service.session.refresh(post)
    assert update.status == "failed"
    assert update.payload["update_message_id"] == "message-existing"
    assert post.status == "terminal_failed"


def test_live_recovery_only_expires_stale_notification_claims(service):
    stale = service.enqueue_notification(
        dedupe_key="stale-progress-update",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        payload={"update_message_id": "message-existing"},
    )
    fresh = service.enqueue_notification(
        dedupe_key="fresh-progress-update",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        payload={"update_message_id": "message-current"},
    )
    stale = service.claim_notification(stale.id)
    fresh = service.claim_notification(fresh.id)
    stale.updated_at -= timedelta(seconds=120)
    service.session.add(stale)
    service.session.commit()

    assert service.recover_inflight_notifications(older_than_seconds=60) == {
        "retryable_updates": 1,
        "ambiguous_posts": 0,
    }
    service.session.refresh(stale)
    service.session.refresh(fresh)
    assert stale.status == "failed"
    assert stale.last_error.startswith("delivery claim expired")
    assert fresh.status == "sending"


def test_user_owner_profile_keeps_bounded_explicit_cross_project_evidence(service):
    profile = service.add_explicit_user_preferences(
        "ou-owner",
        [
            {
                "preference": "优先使用选择题",
                "evidence": "能选择就不要让我长篇回答",
                "category": "interaction",
            }
        ],
        project_id="project-a",
        source_ref="report:turn-1",
    )
    service.add_explicit_user_preferences(
        "ou-owner",
        [
            {
                "preference": "优先使用选择题",
                "evidence": "重复证据",
                "category": "interaction",
            }
        ],
        project_id="project-b",
        source_ref="report:turn-2",
    )

    assert len(profile.preferences) == 1
    stored = service.get_user_owner_profile("ou-owner")
    assert len(stored.preferences) == 1
    assert stored.preferences[0]["preference"] == "优先使用选择题"
    assert stored.preferences[0]["project_id"] == "project-a"
    assert stored.preferences[0]["source_ref"] == "report:turn-1"


def test_scan_candidate_repositories_is_bounded(service, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    too_deep = root / "a" / "b" / "c"
    too_deep.mkdir(parents=True)
    (too_deep / ".git").mkdir()
    assert ProjectService.scan_candidate_repositories([root], max_depth=2) == [
        {"path": str(repo), "name": "repo"}
    ]


def test_project_status_has_no_workflow_projection(service):
    project = Project(
        id="p",
        name="p",
        task_type="feature",
        description="x",
        workspace_path="/tmp/p",
        current_state="ACTIVE",
    )
    service.session.add(project)
    service.session.commit()
    status = service.get_project_status("p")
    assert "current_state" not in status
    assert "pending_approval" not in status
    assert "workflow_steps" not in status


def test_restart_recovery_supersedes_only_stale_projection_updates(service):
    stale = service.enqueue_notification(
        dedupe_key="stale-progress",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="progress",
        payload={"projection_key": "root-agent:p:activity"},
        project_id=None,
    )
    unrelated = service.enqueue_notification(
        dedupe_key="unrelated-report",
        recipient_id="chat",
        recipient_type="chat_id",
        intent_type="report",
        payload={"projection_key": "root-result:turn-1"},
        project_id=None,
    )

    assert service.supersede_projection_notifications(
        {"root-agent:p:activity"}, reason="restart recovery"
    ) == 1
    service.session.refresh(stale)
    service.session.refresh(unrelated)
    assert stale.status == "terminal_failed"
    assert stale.last_error == "restart recovery"
    assert unrelated.status == "pending"


def test_processed_event_claim_commits_before_async_handler_boundary(service):
    service.mark_event_processed("evt-1", "message")

    assert not service.session.in_transaction()
    assert service.is_event_processed("evt-1") is True
