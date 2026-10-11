import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import MetaData, Table
from sqlmodel import Session, SQLModel, create_engine, select

from mac_system.core.project_service import ProjectService
from mac_system.feishu.card import build_content_card
from mac_system.models import (
    AgentSession,
    NotificationOutbox,
    Project,
    ProjectHistoryRound,
    ProjectOwnerReview,
    ProjectOwnerState,
    RootAgentSession,
)


source = Path(__file__).resolve().parents[1] / "scripts" / "deploy_long_conversation_once.py"
spec = importlib.util.spec_from_file_location("long_conversation_maintenance", source)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


@pytest.fixture
def maintenance_context(tmp_path):
    bind = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(bind)
    with Session(bind) as session:
        for project_id in ("implicit", "enabled", "disabled"):
            session.add(Project(id=project_id, name=project_id, task_type="feature",
                                description="test", workspace_path=str(tmp_path)))
        session.commit()
        session.add(ProjectOwnerState(project_id="enabled", enabled=True, version=8))
        session.add(ProjectOwnerState(project_id="disabled", enabled=False, version=5))
        session.add(RootAgentSession(id="root", project_id="implicit",
                                     native_thread_id="original-long-thread"))
        session.commit()
    return bind, tmp_path


def test_defer_snapshot_precedes_database_change(maintenance_context, monkeypatch):
    bind, root = maintenance_context

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "write_state", fail_write)
    with pytest.raises(OSError):
        maintenance.defer_owners(bind, root / "snapshot.json")
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(ProjectOwnerState, "enabled").enabled


def test_restore_preserves_owner_settings_threads_and_reviews(maintenance_context):
    bind, root = maintenance_context
    with Session(bind) as session:
        review = ProjectOwnerReview(id="review", project_id="implicit",
                                   request_item_id="report:old-turn", outcome="pending",
                                   status="running", input_payload={"content": "preserved"})
        session.add(review)
        session.commit()
        original = session.get(ProjectOwnerState, "enabled").model_dump(mode="json")
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    assert json.loads((root / "snapshot.json").read_text()) == snapshot
    assert snapshot["threads"] == {"implicit": "original-long-thread"}
    with Session(bind) as session:
        assert not session.get(ProjectOwnerState, "enabled").enabled
        assert not session.get(ProjectOwnerState, "implicit").enabled
        assert session.get(ProjectOwnerState, "disabled").version == 5
        assert session.get(ProjectOwnerReview, "review").status == "running"
        session.get(ProjectOwnerReview, "review").status = "superseded"
        session.commit()
    restored = maintenance.restore_owners(bind, snapshot)
    maintenance.restore_reviews(bind, snapshot, restored)
    maintenance.restore_reviews(bind, snapshot, maintenance.restore_owners(bind, snapshot))
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(ProjectOwnerState, "enabled").model_dump(mode="json") == original
        assert session.get(ProjectOwnerReview, "review").status == "pending"
        assert session.get(ProjectOwnerReview, "review").input_payload == {"content": "preserved"}
        assert session.get(RootAgentSession, "root").native_thread_id == "original-long-thread"


@pytest.mark.parametrize("legacy_fields", [
    ("goal",), ("status_summary",), ("next_direction",),
    ("goal", "status_summary", "next_direction"),
])
def test_temporary_owner_preserves_legacy_required_metadata(tmp_path, legacy_fields):
    bind = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(bind)
    with bind.begin() as connection:
        for field in legacy_fields:
            connection.exec_driver_sql(
                f"ALTER TABLE project_owner_states ADD COLUMN {field} VARCHAR NOT NULL"
            )
    owner_table = Table("project_owner_states", MetaData(), autoload_with=bind)
    with Session(bind) as session:
        for project_id in ("implicit", "enabled", "disabled"):
            session.add(Project(id=project_id, name=project_id, task_type="feature",
                                description="test", workspace_path=str(tmp_path)))
        session.commit()
        for project_id, enabled, version in (("enabled", True, 8), ("disabled", False, 5)):
            values = ProjectOwnerState(
                project_id=project_id, enabled=enabled, version=version
            ).model_dump()
            values.update({field: f"{project_id}:{field}" for field in legacy_fields})
            session.execute(owner_table.insert().values(**values))
        session.commit()
        original_rows = list(session.execute(
            owner_table.select().order_by(owner_table.c.project_id)
        ).mappings())
    snapshot = maintenance.defer_owners(bind, tmp_path / "snapshot.json")
    with Session(bind) as session:
        temporary = session.execute(owner_table.select().where(
            owner_table.c.project_id == "implicit"
        )).mappings().one()
        assert not temporary["enabled"]
        assert all(temporary[field] == "" for field in legacy_fields)
        assert not session.get(ProjectOwnerState, "enabled").enabled
        assert session.get(ProjectOwnerState, "disabled").version == 5
    assert maintenance.restore_owners(bind, snapshot) == {"implicit", "enabled"}
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert list(session.execute(
            owner_table.select().order_by(owner_table.c.project_id)
        ).mappings()) == original_rows


@pytest.mark.parametrize("enabled", [False, True])
def test_restore_respects_human_switch_during_maintenance(maintenance_context, enabled):
    bind, root = maintenance_context
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    with Session(bind) as session:
        ProjectService(session).set_project_owner_enabled("implicit", enabled)
        original = session.get(ProjectOwnerState, "implicit").model_dump(mode="json")
    restored = maintenance.restore_owners(bind, snapshot)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit").model_dump(mode="json") == original
    assert ("implicit" in restored) == enabled


def add_report(bind, page, total, *, status="sent"):
    with Session(bind) as session:
        session.add(NotificationOutbox(
            id=f"notice-{page}", project_id="implicit", recipient_id="chat",
            recipient_type="chat_id", intent_type="report",
            dedupe_key=f"root-result:turn:{page}", status=status,
            provider_message_id=f"message-{page}" if status == "sent" else None,
            payload={"subject_type": "root_turn", "subject_id": "turn",
                     "lifecycle_status": "completed", "projection_key": f"report-{page}",
                     "card": build_content_card("结果", f"第 {page} 部分", page, total)},
        ))
        session.commit()


@pytest.mark.parametrize("origin", ["human", "owner"])
def test_report_recovery_keeps_all_pages_and_origin_without_resending(maintenance_context, origin):
    bind, root = maintenance_context
    with Session(bind) as session:
        session.add(ProjectHistoryRound(
            id="round", project_id="implicit", sequence=1, native_turn_id="turn",
            native_thread_id="original-long-thread", request_origin=origin,
            parent_decision_id="decision" if origin == "owner" else None,
            user_instructions=[{"text": "原始任务"}],
        ))
        session.commit()
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    add_report(bind, 2, 2)
    add_report(bind, 1, 2)
    restored = maintenance.restore_owners(bind, snapshot)
    maintenance.restore_reviews(bind, snapshot, restored)
    maintenance.restore_reviews(bind, snapshot, restored)
    with Session(bind) as session:
        reviews = session.exec(select(ProjectOwnerReview)).all()
        assert len(reviews) == 1
        review = reviews[0]
        assert review.status == "pending"
        assert review.input_payload["content"] == "第 1 部分\n\n第 2 部分"
        assert review.request_origin == origin
        assert review.parent_decision_id == ("decision" if origin == "owner" else None)
        assert review.input_payload["user_request"] == "原始任务"
        assert review.input_payload["report_card"]["message_id"] == "message-2"
        assert review.input_payload["report_card"]["projection_key"] == "report-2"
        assert session.get(ProjectHistoryRound, "round").report_status == "completed"
        assert len(session.exec(select(NotificationOutbox)).all()) == 2


@pytest.mark.parametrize("missing_page", [True, False])
def test_report_recovery_does_not_review_incomplete_delivery(maintenance_context, missing_page):
    bind, root = maintenance_context
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    add_report(bind, 1, 2)
    if not missing_page:
        add_report(bind, 2, 2, status="pending")
    maintenance.restore_reviews(bind, snapshot, maintenance.restore_owners(bind, snapshot))
    with Session(bind) as session:
        assert session.exec(select(ProjectOwnerReview)).all() == []


def test_explicit_reset_keeps_deferred_review_cancelled(maintenance_context):
    bind, root = maintenance_context
    with Session(bind) as session:
        session.add(ProjectOwnerReview(id="review", project_id="implicit",
                                       request_item_id="report:turn", outcome="pending",
                                       status="pending"))
        session.commit()
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    add_report(bind, 1, 1)
    with Session(bind) as session:
        project = session.get(Project, "implicit")
        project.context = {"runtime_reset_at": maintenance.datetime.utcnow().isoformat()}
        session.get(ProjectOwnerReview, "review").status = "superseded"
        session.commit()
    maintenance.restore_reviews(bind, snapshot, maintenance.restore_owners(bind, snapshot))
    with Session(bind) as session:
        assert session.get(ProjectOwnerReview, "review").status == "superseded"


@pytest.mark.parametrize("kind", ["root", "project", "review", "agent", "sending"])
def test_drain_checks_every_execution_and_delivery_kind(maintenance_context, kind):
    bind, root = maintenance_context
    with Session(bind) as session:
        if kind == "root":
            session.get(RootAgentSession, "root").status = "waiting_user"
        elif kind == "project":
            session.get(Project, "implicit").execution_status = "owner_reviewing"
        elif kind == "review":
            session.add(ProjectOwnerReview(id="review", project_id="implicit",
                                           request_item_id="report:turn", outcome="pending",
                                           status="running"))
        elif kind == "agent":
            session.add(AgentSession(id="agent", project_id="implicit",
                                     root_agent_session_id="root", task_name="cli",
                                     provider="external", objective="test", status="running"))
        else:
            session.add(NotificationOutbox(id="notice", project_id="implicit", recipient_id="chat",
                                           intent_type="report", dedupe_key="sending", status="sending"))
        session.commit()
    assert any(maintenance.drain_blockers(bind).values())
    with pytest.raises(TimeoutError):
        maintenance.wait_for_drain(bind, timeout=0)


@pytest.mark.parametrize("failure", ["timeout", "restart", "new_work", None])
def test_run_restores_state_on_success_or_failure_without_restart_retry(
    maintenance_context, monkeypatch, failure
):
    bind, root = maintenance_context
    calls = []

    def wait(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("busy")
        if failure == "new_work":
            with Session(bind) as session:
                session.get(RootAgentSession, "root").status = "running"
                session.commit()

    def restart(*args, **kwargs):
        calls.append(args[0])
        if failure == "restart":
            raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(maintenance, "wait_for_drain", wait)
    monkeypatch.setattr(maintenance.subprocess, "run", restart)
    if failure:
        with pytest.raises((TimeoutError, subprocess.CalledProcessError, RuntimeError)):
            maintenance.run(bind, root, "authorized-operation", 60)
    else:
        maintenance.run(bind, root, "authorized-operation", 60)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(ProjectOwnerState, "enabled").enabled
        assert not session.get(ProjectOwnerState, "disabled").enabled
    assert len(calls) == (1 if failure in {None, "restart"} else 0)
    with pytest.raises(FileExistsError):
        maintenance.run(bind, root, "authorized-operation", 60)
    assert len(calls) == (1 if failure in {None, "restart"} else 0)


def test_previously_attempted_restart_cannot_defer_owner_again(maintenance_context):
    bind, root = maintenance_context
    (root / ".mac" / "restart_operations" / "same").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="already attempted"):
        maintenance.run(bind, root, "same", 60)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None


@pytest.mark.parametrize("delivery", ["missing", "pending", "incomplete", "complete"])
def test_initial_active_turn_must_deliver_its_full_report(maintenance_context, delivery):
    bind, root = maintenance_context
    with Session(bind) as session:
        session.get(RootAgentSession, "root").status = "running"
        session.get(RootAgentSession, "root").active_turn_id = "turn"
        session.commit()
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    with Session(bind) as session:
        session.get(RootAgentSession, "root").status = "idle"
        session.commit()
    if delivery != "missing":
        add_report(bind, 1, 2, status="pending" if delivery == "pending" else "sent")
    if delivery == "complete":
        add_report(bind, 2, 2)
    blockers = maintenance.drain_blockers(bind, snapshot)
    assert bool(blockers["active_turn_reports"]) == (delivery != "complete")
    assert any(blockers.values()) == (delivery != "complete")


@pytest.mark.parametrize("invalid", ["missing_message", "duplicate_page", "conflicting_total"])
def test_sent_report_still_requires_valid_delivery_evidence(maintenance_context, invalid):
    bind, root = maintenance_context
    add_report(bind, 1, 2)
    add_report(bind, 2, 2)
    with Session(bind) as session:
        rows = session.exec(select(NotificationOutbox).order_by(NotificationOutbox.id)).all()
        if invalid == "missing_message":
            rows[0].provider_message_id = None
        else:
            rows[1].payload = {**rows[1].payload, "card": build_content_card(
                "结果", "第二部分", 1 if invalid == "duplicate_page" else 2,
                3 if invalid == "conflicting_total" else 2,
            )}
        assert not maintenance.report_delivered(rows)


@pytest.mark.parametrize("newer", ["active_round", "completed_round", "review"])
def test_restore_never_revives_review_overtaken_by_new_work(maintenance_context, newer):
    bind, root = maintenance_context
    with Session(bind) as session:
        session.add(ProjectOwnerReview(
            id="review", project_id="implicit", request_item_id="report:turn",
            native_turn_id="turn", outcome="pending", status="pending",
        ))
        session.commit()
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    with Session(bind) as session:
        session.get(ProjectOwnerReview, "review").status = "superseded"
        if newer == "review":
            session.add(ProjectOwnerReview(
                id="newer-review", project_id="implicit", request_item_id="report:new-turn",
                native_turn_id="new-turn", outcome="pending", status="pending",
            ))
        else:
            session.add(ProjectHistoryRound(
                id="new-round", project_id="implicit", sequence=2,
                native_turn_id="new-turn", user_instructions=[{"text": "新真人请求"}],
            ))
            if newer == "active_round":
                session.get(RootAgentSession, "root").active_history_round_id = "new-round"
        session.commit()
    maintenance.restore_reviews(bind, snapshot, maintenance.restore_owners(bind, snapshot))
    with Session(bind) as session:
        assert session.get(ProjectOwnerReview, "review").status == "superseded"


@pytest.mark.parametrize("active", [True, False])
def test_old_report_cannot_overwrite_a_new_human_round(maintenance_context, active):
    bind, root = maintenance_context
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    add_report(bind, 1, 1)
    with Session(bind) as session:
        session.add(ProjectHistoryRound(
            id="new-round", project_id="implicit", sequence=2,
            native_turn_id="new-turn", user_instructions=[{"text": "新真人请求"}],
        ))
        if active:
            session.get(RootAgentSession, "root").active_history_round_id = "new-round"
        session.commit()
    maintenance.restore_reviews(bind, snapshot, maintenance.restore_owners(bind, snapshot))
    with Session(bind) as session:
        assert session.exec(select(ProjectOwnerReview)).all() == []
        history = session.get(ProjectHistoryRound, "new-round")
        assert history.native_turn_id == "new-turn"
        assert history.user_instructions == [{"text": "新真人请求"}]
        assert not history.report_content


def test_restart_checks_threads_without_rewriting_or_restarting_again(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    calls = []
    monkeypatch.setattr(maintenance, "wait_for_drain", lambda *args, **kwargs: None)

    def restart(*args, **kwargs):
        calls.append(args[0])
        with Session(bind) as session:
            session.get(RootAgentSession, "root").native_thread_id = "unexpected-thread"
            session.commit()

    monkeypatch.setattr(maintenance.subprocess, "run", restart)
    with pytest.raises(RuntimeError, match="Thread identity changed"):
        maintenance.run(bind, root, "thread-verification", 60)
    status = json.loads((root / ".mac/long_conversation_maintenance/thread-verification/status.json").read_text())
    assert status["stage"] == "not_completed"
    assert status["restarted"]
    assert status["thread_changes"] == ["implicit"]
    assert len(calls) == 1
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(RootAgentSession, "root").native_thread_id == "unexpected-thread"


@pytest.mark.parametrize("error", [OSError("commit failed"), SystemExit(143)])
def test_owner_defer_interruption_restores_persisted_snapshot(maintenance_context, monkeypatch, error):
    bind, root = maintenance_context
    defer_owners = maintenance.defer_owners

    def interrupted_defer(*args):
        defer_owners(*args)
        raise error

    monkeypatch.setattr(maintenance, "defer_owners", interrupted_defer)
    with pytest.raises(type(error)):
        maintenance.run(bind, root, "interrupted-defer", 1)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(ProjectOwnerState, "enabled").enabled
        assert not session.get(ProjectOwnerState, "disabled").enabled
    status = json.loads((root / ".mac/long_conversation_maintenance/interrupted-defer/status.json").read_text())
    assert not status["restart_attempted"]
    assert status["error_type"] == type(error).__name__


def test_background_execution_blocks_maintenance_after_turn_is_idle(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    snapshot = maintenance.defer_owners(bind, root / "snapshot.json")
    snapshot["background_execution"] = {"processes": [{"pid": 123, "created_at": 1}], "groups": [123]}
    monkeypatch.setattr(maintenance, "host_execution_has_exited", lambda witness: False)
    assert maintenance.drain_blockers(bind, snapshot)["background_execution"]
    monkeypatch.setattr(maintenance, "host_execution_has_exited", lambda witness: True)
    assert not maintenance.drain_blockers(bind, snapshot)["background_execution"]


def test_run_persists_background_witness_before_drain(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    witness = {"processes": [{"pid": 123, "created_at": 1}], "groups": [123]}

    def drain(*args, **kwargs):
        assert kwargs["snapshot"]["background_execution"] == witness
        saved = json.loads((root / ".mac/long_conversation_maintenance/background/snapshot.json").read_text())
        assert saved["background_execution"] == witness
        raise TimeoutError("still working")

    monkeypatch.setattr(maintenance, "wait_for_drain", drain)
    with pytest.raises(TimeoutError):
        maintenance.run(bind, root, "background", 1, execution_witness=witness)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.get(ProjectOwnerState, "enabled").enabled


def test_drain_requires_a_continuous_quiet_interval(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    clock = [0]
    checks = []

    def blockers(*args):
        checks.append(clock[0])
        return {"roots": ["new-work"] if clock[0] == 4 else []}

    monkeypatch.setattr(maintenance, "drain_blockers", blockers)
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    maintenance.wait_for_drain(bind, timeout=20, quiet_seconds=4)
    assert checks == [0, 2, 4, 6, 8, 10]


@pytest.fixture
def process_observer(monkeypatch):
    class ObservedProcess:
        def __init__(self, process_id, arguments, group):
            self.pid = process_id
            self.arguments = arguments
            self.group = group
            self.created_at = float(process_id)
            self.descendants = []

        def create_time(self):
            return self.created_at

        def children(self, recursive=True):
            return self.descendants

        def name(self):
            return self.arguments[0]

        def cmdline(self):
            return self.arguments

        def status(self):
            return "running"

    processes = {
        10: ObservedProcess(10, ["python", "-m", "uvicorn"], 10),
        20: ObservedProcess(20, ["python", "--multiprocessing-fork"], 10),
        30: ObservedProcess(30, ["codex", "app-server"], 30),
        40: ObservedProcess(40, ["node", "/tools/cua-repl.mjs"], 40),
        50: ObservedProcess(50, ["python", "ui-helper.py"], 50),
        60: ObservedProcess(60, ["python", "batch_wiki.py"], 60),
        61: ObservedProcess(61, ["python", "batch-child.py"], 60),
    }
    processes[10].descendants = list(processes.values())[1:]
    processes[30].descendants = [processes[40], processes[50], processes[60], processes[61]]
    processes[40].descendants = [processes[50]]
    processes[60].descendants = [processes[61]]

    def lookup(process_id):
        if process_id not in processes:
            raise maintenance.psutil.NoSuchProcess(process_id)
        return processes[process_id]

    monkeypatch.setattr(maintenance.psutil, "Process", lookup)
    monkeypatch.setattr(maintenance.os, "getpgrp", lambda: 100)
    monkeypatch.setattr(maintenance.os, "getpgid", lambda process_id: lookup(process_id).group)
    return processes, {"processes": [], "groups": [],
                       "observers": [{"pid": 10, "created_at": 10.0}]}


def test_observer_captures_new_execution_without_counting_infrastructure(process_observer):
    processes, witness = process_observer
    processes[10].descendants = list(processes.values())[1:5]
    initial = maintenance.refresh_execution_witness(witness)
    assert initial["processes"] == []
    assert initial["groups"] == []
    processes[10].descendants.extend([processes[60], processes[61]])
    refreshed = maintenance.refresh_execution_witness(initial)
    assert refreshed["processes"] == [{"pid": 60, "created_at": 60.0},
                                       {"pid": 61, "created_at": 61.0}]
    assert refreshed["groups"] == [60]
    assert not refreshed["unverifiable"]
    assert set(refreshed) == {"processes", "groups", "observers", "unverifiable"}


@pytest.mark.parametrize("condition", ["observer_reused", "observer_gone", "denied", "execution_reused"])
def test_uncertain_process_identity_blocks_maintenance(process_observer, maintenance_context,
                                                      monkeypatch, condition):
    processes, witness = process_observer
    if condition == "observer_reused":
        processes[10].created_at = 11.0
    elif condition == "observer_gone":
        del processes[10]
    elif condition == "execution_reused":
        witness["processes"] = [{"pid": 60, "created_at": 59.0}]
    else:
        def denied(process_id):
            raise maintenance.psutil.AccessDenied(process_id)
        monkeypatch.setattr(maintenance.psutil, "Process", denied)
    refreshed = maintenance.refresh_execution_witness(witness)
    assert refreshed["unverifiable"]
    bind, root = maintenance_context
    snapshot = {"started_at": maintenance.datetime.utcnow().isoformat(),
                "background_execution": refreshed}
    assert maintenance.drain_blockers(bind, snapshot)["background_execution"]


def test_passive_readiness_wait_has_no_deadline_or_owner_mutation(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    clock = [0]
    checks = []
    witness = {"processes": [], "groups": [], "observers": [{"pid": 10, "created_at": 10}]}
    monkeypatch.setattr(maintenance, "refresh_execution_witness", lambda current: current)
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def blockers(*args):
        checks.append(clock[0])
        with Session(bind) as session:
            assert session.get(ProjectOwnerState, "enabled").enabled
            assert session.get(ProjectOwnerState, "implicit") is None
            assert not session.get(ProjectOwnerState, "disabled").enabled
        return {"roots": ["work"] if clock[0] < 90 else []}

    monkeypatch.setattr(maintenance, "drain_blockers", blockers)
    assert maintenance.wait_for_readiness(bind, root / "status.json", root / "readiness.json", witness) == witness
    assert checks == [0, 30, 60, 90, 100, 110, 120]
    status = json.loads((root / "status.json").read_text())
    assert status["stage"] == "passive_waiting"
    assert not status["owners_deferred"]
    assert not status["restart_attempted"]
    assert json.loads((root / "readiness.json").read_text())["background_execution"] == witness


def test_short_maintenance_refreshes_witness_on_every_check(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    clock = [0]
    checks = []
    snapshot = {"background_execution": {"generation": 0}}

    def refresh(witness):
        return {"generation": witness["generation"] + 1}

    def blockers(*args):
        checks.append(snapshot["background_execution"]["generation"])
        return {"background_execution": ["new-task"] if clock[0] == 2 else []}

    monkeypatch.setattr(maintenance, "refresh_execution_witness", refresh)
    monkeypatch.setattr(maintenance, "drain_blockers", blockers)
    monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(maintenance.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    maintenance.wait_for_drain(bind, 20, quiet_seconds=2, snapshot=snapshot, snapshot_path=root / "snapshot.json")
    assert checks == [1, 2, 3, 4]
    assert json.loads((root / "snapshot.json").read_text()) == snapshot


def test_acceptance_waits_for_real_delivery_and_is_idempotent(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    notice_id = maintenance.notify(bind, "implicit", "resume", "restored", "继续真实验收")
    waits = []

    def deliver(seconds):
        with Session(bind) as session:
            assert session.exec(select(ProjectOwnerReview)).all() == []
            notice = session.get(NotificationOutbox, notice_id)
            notice.status = "sent"
            if waits:
                notice.provider_message_id = "om-real-delivery"
            session.add(notice)
            session.commit()
        waits.append(seconds)

    monkeypatch.setattr(maintenance.time, "sleep", deliver)
    review_id = maintenance.dispatch_acceptance(bind, "implicit", notice_id, "resume")
    assert maintenance.dispatch_acceptance(bind, "implicit", notice_id, "resume") == review_id
    assert waits == [5, 5]
    with Session(bind) as session:
        review = session.exec(select(ProjectOwnerReview)).one()
        assert review.request_item_id == "maintenance:resume:acceptance"
        assert review.request_origin == "owner_audit"
        assert review.native_turn_id is None
        assert review.input_payload["report_card"]["message_id"] == "om-real-delivery"
        assert review.input_payload["content"] == "继续真实验收"
        assert session.exec(select(ProjectHistoryRound)).all() == []
        assert session.get(RootAgentSession, "root").native_thread_id == "original-long-thread"


@pytest.mark.parametrize("missing", [True, False])
def test_failed_report_never_dispatches_acceptance(maintenance_context, missing):
    bind, root = maintenance_context
    notice_id = "missing"
    if not missing:
        notice_id = maintenance.notify(bind, "implicit", "failed", "restored", "未完成")
        with Session(bind) as session:
            session.get(NotificationOutbox, notice_id).status = "terminal_failed"
            session.commit()
    with pytest.raises(RuntimeError, match="not delivered"):
        maintenance.dispatch_acceptance(bind, "implicit", notice_id, "failed")
    with Session(bind) as session:
        assert session.exec(select(ProjectOwnerReview)).all() == []


def test_wait_ready_rechecks_before_single_restart_and_restores_owners(maintenance_context, monkeypatch):
    bind, root = maintenance_context
    calls = []

    def ready(*args):
        with Session(bind) as session:
            assert session.get(ProjectOwnerState, "enabled").enabled
            assert session.get(ProjectOwnerState, "implicit") is None
        return {"generation": 1}

    def restart(*args, **kwargs):
        calls.append(args[0])
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(maintenance, "wait_for_readiness", ready)
    monkeypatch.setattr(maintenance, "wait_for_drain", lambda *args, **kwargs: None)
    monkeypatch.setattr(maintenance, "refresh_execution_witness", lambda witness: {"generation": 2})
    monkeypatch.setattr(maintenance, "drain_blockers", lambda bind, snapshot: {})
    monkeypatch.setattr(maintenance.subprocess, "run", restart)
    monkeypatch.setattr(maintenance, "dispatch_acceptance", lambda *args: "acceptance-review")
    with pytest.raises(subprocess.CalledProcessError):
        maintenance.run(bind, root, "single-resume", 300, "implicit", {}, wait_ready=True)
    assert len(calls) == 1
    state_dir = root / ".mac/long_conversation_maintenance/single-resume"
    assert json.loads((state_dir / "snapshot.json").read_text())["background_execution"] == {"generation": 2}
    status = json.loads((state_dir / "status.json").read_text())
    assert status["stage"] == "not_completed"
    assert status["restart_attempted"]
    assert status["acceptance_dispatch"] == "queued"
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "enabled").enabled
        assert session.get(ProjectOwnerState, "implicit") is None
    with pytest.raises(FileExistsError):
        maintenance.run(bind, root, "single-resume", 300, "implicit", {}, wait_ready=True)
    assert len(calls) == 1


def test_passive_interruption_does_not_defer_or_dispatch(maintenance_context, monkeypatch):
    bind, root = maintenance_context

    def interrupted(*args):
        raise SystemExit(143)

    def unexpected(*args, **kwargs):
        pytest.fail("Passive interruption must not change Owner or dispatch acceptance")

    monkeypatch.setattr(maintenance, "wait_for_readiness", interrupted)
    monkeypatch.setattr(maintenance, "defer_owners", unexpected)
    monkeypatch.setattr(maintenance, "dispatch_acceptance", unexpected)
    with pytest.raises(SystemExit):
        maintenance.run(bind, root, "passive-interrupted", 300, "implicit", {}, wait_ready=True)
    with Session(bind) as session:
        assert session.get(ProjectOwnerState, "enabled").enabled
        assert session.get(ProjectOwnerState, "implicit") is None
        assert session.exec(select(NotificationOutbox)).all() == []
