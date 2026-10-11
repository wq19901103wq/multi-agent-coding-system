"""Drain an authorized deployment without killing the turn arranging it."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import psutil
from sqlalchemy import MetaData, Table
from sqlmodel import Session, select

from mac_system.agents.process_lifecycle import host_execution_has_exited
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


def write_state(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def defer_owners(bind, snapshot_path):
    """Save recovery data before borrowing the existing Owner switch."""
    with Session(bind) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        marker = datetime.utcnow()
        snapshot = {
            "started_at": marker.isoformat(),
            "owners": {},
            "review_ids": [
                row.id for row in session.exec(select(ProjectOwnerReview).where(
                    ProjectOwnerReview.status.in_(["pending", "running"])
                )).all()
            ],
            "threads": {
                row.project_id: row.native_thread_id
                for row in session.exec(select(RootAgentSession)).all()
                if row.native_thread_id
            },
            "active_turns": {
                row.project_id: row.active_turn_id
                for row in session.exec(select(RootAgentSession)).all()
                if row.status not in {"idle", "failed"} and row.active_turn_id
            },
        }
        for project in session.exec(select(Project)).all():
            owner = session.get(ProjectOwnerState, project.id)
            if owner is not None and not owner.enabled:
                continue
            snapshot["owners"][project.id] = (
                owner.model_dump(mode="json") if owner is not None else None
            )
        write_state(snapshot_path, snapshot)
        owner_table = Table(
            "project_owner_states", MetaData(), autoload_with=session.connection()
        )
        for project_id, original in snapshot["owners"].items():
            owner = session.get(ProjectOwnerState, project_id)
            if owner is None:
                values = ProjectOwnerState(
                    project_id=project_id, enabled=False, version=1, updated_at=marker
                ).model_dump()
                values.update({
                    field: "" for field in ("goal", "status_summary", "next_direction")
                    if field in owner_table.c
                })
                session.execute(owner_table.insert().values(**values))
                continue
            owner.enabled = False
            owner.version = int(original["version"]) + 1 if original else 1
            owner.updated_at = marker
            session.add(owner)
        session.commit()
    return snapshot


def restore_owners(bind, snapshot):
    """Compare-and-restore: never overwrite a human Owner switch change."""
    restored = set()
    marker = datetime.fromisoformat(snapshot["started_at"])
    with Session(bind) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        for project_id, original in snapshot["owners"].items():
            owner = session.get(ProjectOwnerState, project_id)
            expected_version = int(original["version"]) + 1 if original else 1
            if owner is None:
                if original is None and session.get(Project, project_id) is not None:
                    restored.add(project_id)
                continue
            if (not owner.enabled and owner.updated_at == marker
                    and owner.version == expected_version):
                if original is None:
                    session.delete(owner)
                else:
                    for key, value in original.items():
                        if key in {"created_at", "updated_at"}:
                            value = datetime.fromisoformat(value)
                        setattr(owner, key, value)
                    session.add(owner)
                restored.add(project_id)
            elif original is not None and owner.model_dump(mode="json") == original:
                restored.add(project_id)
            elif owner.enabled:
                restored.add(project_id)
        session.commit()
    return restored


def restore_reviews(bind, snapshot, restored):
    """Requeue deferred reviews and rebuild only fully delivered reports."""
    marker = datetime.fromisoformat(snapshot["started_at"])
    with Session(bind) as session:
        service = ProjectService(session)
        reviews = session.exec(select(ProjectOwnerReview).where(
            (ProjectOwnerReview.id.in_(snapshot["review_ids"]))
            | (ProjectOwnerReview.created_at >= marker)
        )).all()
        candidates = session.exec(select(NotificationOutbox).where(
            NotificationOutbox.project_id.in_(restored),
            NotificationOutbox.intent_type == "report",
            NotificationOutbox.updated_at >= marker,
        )).all()
        turns = {
            (row.project_id, row.payload.get("subject_id")) for row in candidates
            if row.payload.get("subject_type") == "root_turn"
        }
        for project_id, turn_id in sorted(turns, key=lambda item: max(
            row.created_at for row in candidates
            if (row.project_id, row.payload.get("subject_id")) == item
        )):
            if not turn_id or not matches_latest_round(session, project_id, turn_id):
                continue
            rows = session.exec(select(NotificationOutbox).where(
                NotificationOutbox.project_id == project_id,
                NotificationOutbox.intent_type == "report",
                NotificationOutbox.payload["subject_id"].as_string() == turn_id,
            ).order_by(NotificationOutbox.created_at, NotificationOutbox.id)).all()
            if not report_delivered(rows):
                continue
            project = service.require_project(project_id)
            reset_at = (project.context or {}).get("runtime_reset_at")
            if reset_at and datetime.fromisoformat(reset_at) >= rows[0].created_at:
                continue
            if service.get_project_owner_review_by_request_item_id(
                project_id, f"report:{turn_id}"
            ) is not None:
                continue
            cards = [row.payload.get("card") or {} for row in rows]
            titles = [card.get("header", {}).get("title", {}).get("content", "")
                      for card in cards]
            pages = [re.search(r"（(\d+)/(\d+)）$", title) for title in titles]
            if any(pages):
                if not all(pages):
                    continue
                total = int(pages[0].group(2))
                if (any(int(page.group(2)) != total for page in pages)
                        or {int(page.group(1)) for page in pages} != set(range(1, total + 1))):
                    continue
                ordered = sorted(zip(rows, cards, titles, pages),
                                 key=lambda item: int(item[3].group(1)))
                rows, cards, titles, pages = map(list, zip(*ordered))
            parts = ["\n\n".join(
                str(element.get("content") or "")
                for element in card.get("body", {}).get("elements", [])
                if element.get("tag") == "markdown"
            ) for card in cards]
            if not all(titles) or not all(parts):
                continue
            title = re.sub(r"（\d+/\d+）$", "", titles[-1])
            history = service.record_project_history_report(
                project_id, native_turn_id=turn_id, title=title,
                content="\n\n".join(parts), report_ref=f"report:{turn_id}",
                report_status="failed" if rows[-1].payload.get("lifecycle_status") == "failed"
                else "completed",
            )
            service.ensure_project_owner_review_job(
                project_id, request_item_id=f"report:{turn_id}", native_turn_id=turn_id,
                title=title, content=history.report_content,
                user_request="\n".join(str(item.get("text") or "")
                                      for item in history.user_instructions),
                request_origin=history.request_origin,
                parent_decision_id=history.parent_decision_id,
                report_card={
                    "title": title, "content": parts[-1], "page": len(parts),
                    "total": len(parts), "color": cards[-1].get("header", {}).get("template", "blue"),
                    "projection_key": rows[-1].payload.get("projection_key"),
                    "message_id": rows[-1].provider_message_id,
                },
            )
        for review in reviews:
            if (review.project_id in restored and review.status == "superseded"
                    and service.is_project_owner_review_current(review.id)):
                project = service.get_project(review.project_id)
                reset_at = (project.context or {}).get("runtime_reset_at") if project else None
                if reset_at and datetime.fromisoformat(reset_at) >= review.created_at:
                    continue
                if service.get_project_decision_by_request_item_id(
                    review.project_id, review.request_item_id
                ) is not None:
                    continue
                if not matches_latest_round(session, review.project_id, review.native_turn_id):
                    continue
                review.status = "pending"
                review.last_error = None
                review.updated_at = datetime.utcnow()
                session.add(review)
        session.commit()


def matches_latest_round(session, project_id, turn_id):
    root = session.exec(select(RootAgentSession).where(
        RootAgentSession.project_id == project_id
    )).first()
    history = session.get(ProjectHistoryRound, root.active_history_round_id) if (
        root and root.active_history_round_id
    ) else None
    if history is None:
        history = session.exec(select(ProjectHistoryRound).where(
            ProjectHistoryRound.project_id == project_id
        ).order_by(ProjectHistoryRound.sequence.desc())).first()
    return history is None or history.native_turn_id == turn_id


def thread_identities(bind):
    with Session(bind) as session:
        return {row.project_id: row.native_thread_id
                for row in session.exec(select(RootAgentSession)).all()}


def report_delivered(rows):
    if not rows or any(row.status != "sent" or not row.provider_message_id for row in rows):
        return False
    pages = [re.search(r"（(\d+)/(\d+)）$", (row.payload.get("card") or {}).get(
        "header", {}
    ).get("title", {}).get("content", "")) for row in rows]
    if not any(pages):
        return len(rows) == 1
    if not all(pages):
        return False
    total = int(pages[0].group(2))
    return (
        len(rows) == total
        and all(int(page.group(2)) == total for page in pages)
        and {int(page.group(1)) for page in pages} == set(range(1, total + 1))
    )


def drain_blockers(bind, snapshot=None):
    with Session(bind) as session:
        roots = session.exec(select(RootAgentSession).where(
            RootAgentSession.status.notin_(["idle", "failed"])
        )).all()
        projects = session.exec(select(Project).where(
            Project.execution_status.notin_(["idle", "failed", "completed", "cancelled"])
        )).all()
        agents = session.exec(select(AgentSession).where(
            AgentSession.status.in_(["pending", "running"])
        )).all()
        reviews = session.exec(select(ProjectOwnerReview).where(
            ProjectOwnerReview.status.in_(["pending", "running"])
        )).all()
        sending = session.exec(select(NotificationOutbox).where(
            NotificationOutbox.status == "sending"
        )).all()
        blockers = {
            "roots": [row.project_id for row in roots],
            "projects": [row.id for row in projects],
            "agents": [row.id for row in agents],
            "reviews": [row.id for row in reviews],
            "sending": [row.id for row in sending],
        }
        if snapshot:
            witness = snapshot.get("background_execution")
            if witness is not None:
                blockers["background_execution"] = (
                    [] if not witness.get("unverifiable") and (
                        (witness.get("observers") and not witness["processes"]
                         and not witness["groups"])
                        or host_execution_has_exited(witness)
                    ) else ["still_running_or_unverifiable"]
                )
            marker = datetime.fromisoformat(snapshot["started_at"])
            notices = session.exec(select(NotificationOutbox).where(
                NotificationOutbox.created_at >= marker,
                NotificationOutbox.intent_type.in_(["report", "maintenance"]),
                NotificationOutbox.status != "sent",
            )).all()
            blockers["undelivered"] = [row.id for row in notices]
            blockers["active_turn_reports"] = []
            for project_id, turn_id in snapshot.get("active_turns", {}).items():
                rows = session.exec(select(NotificationOutbox).where(
                    NotificationOutbox.project_id == project_id,
                    NotificationOutbox.intent_type == "report",
                    NotificationOutbox.payload["subject_id"].as_string() == turn_id,
                )).all()
                if not report_delivered(rows):
                    blockers["active_turn_reports"].append(turn_id)
        return blockers


def wait_for_drain(bind, timeout, quiet_seconds=30, *, snapshot=None, snapshot_path=None):
    deadline = time.monotonic() + timeout
    quiet_since = None
    while time.monotonic() < deadline:
        if snapshot and snapshot.get("background_execution") is not None:
            snapshot["background_execution"] = refresh_execution_witness(
                snapshot["background_execution"]
            )
            if snapshot_path is not None:
                write_state(snapshot_path, snapshot)
        blockers = drain_blockers(bind, snapshot)
        if any(blockers.values()):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_seconds:
            return
        time.sleep(2)
    raise TimeoutError("Tasks did not drain; no service stop was requested")


def refresh_execution_witness(witness):
    if not witness or not witness.get("observers"):
        return witness
    identities = {(item["pid"], item["created_at"]) for item in witness["processes"]}
    groups = set(witness["groups"])
    processes = {}
    infrastructure = set()
    excluded = set()
    unverifiable = False
    for identity in [*witness["observers"], *witness["processes"]]:
        try:
            process = psutil.Process(identity["pid"])
            if process.create_time() != identity["created_at"]:
                unverifiable = True
                continue
            processes.update({child.pid: child for child in process.children(recursive=True)})
            if identity in witness["observers"]:
                infrastructure.add(process.pid)
            else:
                processes[process.pid] = process
        except psutil.NoSuchProcess:
            if identity in witness["observers"]:
                unverifiable = True
        except psutil.AccessDenied:
            unverifiable = True
    for process in list(processes.values()):
        try:
            name = process.name()
            arguments = process.cmdline()
            tokens = {Path(argument).name for argument in arguments if "\n" not in argument}
            if name == "node_repl" or tokens & {"node_repl", "cua-repl.mjs"}:
                excluded.update([process.pid, *(child.pid for child in process.children(recursive=True))])
            elif ("app-server" in arguments or "--multiprocessing-fork" in arguments
                  or any(argument.startswith("from multiprocessing.resource_tracker import ")
                         for argument in arguments)):
                infrastructure.add(process.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unverifiable = True
    infrastructure_groups = {os.getpgrp()}
    for process_id in infrastructure | excluded:
        try:
            infrastructure_groups.add(os.getpgid(process_id))
        except ProcessLookupError:
            continue
        except PermissionError:
            unverifiable = True
    for process_id, process in processes.items():
        if process_id in infrastructure | excluded:
            continue
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            identities.add((process.pid, process.create_time()))
            group = os.getpgid(process.pid)
            if group not in infrastructure_groups:
                groups.add(group)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError):
            unverifiable = True
    return {
        "processes": [{"pid": process_id, "created_at": created_at}
                      for process_id, created_at in sorted(identities)],
        "groups": sorted(groups), "observers": witness["observers"],
        "unverifiable": unverifiable,
    }


def wait_for_readiness(bind, status_path, readiness_path, execution_witness, quiet_seconds=30):
    with Session(bind) as session:
        readiness = {
            "started_at": datetime.utcnow().isoformat(),
            "active_turns": {
                row.project_id: row.active_turn_id
                for row in session.exec(select(RootAgentSession)).all()
                if row.status not in {"idle", "failed"} and row.active_turn_id
            },
            "background_execution": execution_witness,
        }
    quiet_since = None
    previous = None
    while True:
        refreshed = refresh_execution_witness(readiness["background_execution"])
        readiness["background_execution"] = refreshed
        write_state(readiness_path, readiness)
        blockers = drain_blockers(bind, readiness)
        counts = {key: len(value) for key, value in blockers.items()}
        if counts != previous:
            write_state(status_path, {
                "stage": "passive_waiting", "pid": os.getpid(), "blockers": counts,
                "owners_deferred": False, "restart_attempted": False,
                "updated_at": datetime.utcnow().isoformat(),
            })
            previous = counts
        if any(blockers.values()):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_seconds:
            return refreshed
        time.sleep(10 if quiet_since is not None else 30)


def notify(bind, project_id, operation_id, stage, content):
    if not project_id:
        return
    with Session(bind) as session:
        service = ProjectService(session)
        project = service.require_project(project_id)
        notice = service.enqueue_notification(
            dedupe_key=f"maintenance:{operation_id}:{stage}",
            recipient_id=project.feishu_chat_id or project.id,
            recipient_type="chat_id", intent_type="maintenance", project_id=project.id,
            bot_account_id=project.bot_account_id,
            payload={"card": build_content_card("MAC 长对话维护", content)},
        )
        return notice.id


def dispatch_acceptance(bind, project_id, notice_id, operation_id):
    while True:
        with Session(bind) as session:
            notice = session.get(NotificationOutbox, notice_id)
            if notice is None or notice.status == "terminal_failed":
                raise RuntimeError("Maintenance report was not delivered; acceptance not dispatched")
            if notice.status == "sent" and notice.provider_message_id:
                card = notice.payload["card"]
                title = card["header"]["title"]["content"]
                content = "\n\n".join(str(element.get("content") or "")
                                      for element in card["body"]["elements"]
                                      if element.get("tag") == "markdown")
                review = ProjectService(session).ensure_project_owner_review_job(
                    project_id, request_item_id=f"maintenance:{operation_id}:acceptance",
                    native_turn_id=None, request_origin="owner_audit", title=title,
                    content=content, user_request="", report_card={
                        "title": title, "content": content, "page": 1, "total": 1,
                        "message_id": notice.provider_message_id,
                    },
                )
                return review.id
        time.sleep(5)


def run(bind, root, operation_id, timeout, project_id=None, execution_witness=None,
        *, wait_ready=False):
    state_dir = root / ".mac" / "long_conversation_maintenance" / operation_id
    state_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(state_dir, 0o700)
    status_path = state_dir / "status.json"
    snapshot_path = state_dir / "snapshot.json"
    if (root / ".mac" / "restart_operations" / operation_id).exists():
        raise RuntimeError("Restart was already attempted; no retry permitted")
    write_state(status_path, {
        "stage": "passive_initializing" if wait_ready else "deferring_owners",
        "pid": os.getpid(), "owners_deferred": False, "restart_attempted": False,
    })
    snapshot = None
    restarted = False
    restart_attempted = False
    thread_changes = []
    error_type = None
    try:
        if wait_ready:
            execution_witness = wait_for_readiness(
                bind, status_path, state_dir / "readiness.json", execution_witness
            )
        snapshot = defer_owners(bind, snapshot_path)
        if execution_witness is not None:
            snapshot["background_execution"] = execution_witness
            write_state(snapshot_path, snapshot)
        write_state(status_path, {"stage": "waiting_for_natural_completion", "pid": os.getpid()})
        notify(bind, project_id, operation_id, "waiting",
               "已进入短暂维护窗口，请暂不提交新任务。Owner 自动续推暂缓。"
               "待全部执行及审阅结束后再重启；保留会话与待办，超时则不停止服务。")
        wait_for_drain(bind, timeout, snapshot=snapshot, snapshot_path=snapshot_path)
        if snapshot.get("background_execution") is not None:
            snapshot["background_execution"] = refresh_execution_witness(
                snapshot["background_execution"]
            )
        snapshot["threads_at_restart"] = thread_identities(bind)
        write_state(snapshot_path, snapshot)
        if any(drain_blockers(bind, snapshot).values()):
            raise RuntimeError("New work arrived; no service stop was requested")
        restart_attempted = True
        write_state(status_path, {"stage": "restart_attempted", "pid": os.getpid()})
        subprocess.run(["bash", "scripts/restart_server_once.sh", operation_id],
                       cwd=root, check=True)
        restarted = True
        actual = thread_identities(bind)
        thread_changes = [project_id for project_id, thread_id
                          in snapshot["threads_at_restart"].items()
                          if project_id not in actual or actual[project_id] != thread_id]
        if thread_changes:
            raise RuntimeError("Thread identity changed; inspect state without restarting again")
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        if snapshot is None and snapshot_path.exists():
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        restored = restore_owners(bind, snapshot) if snapshot is not None else set()
        if snapshot is not None:
            restore_reviews(bind, snapshot, restored)
        status = {
            "stage": "restarted_pending_acceptance" if restarted and not error_type else "not_completed",
            "owners_restored": sorted(restored), "pid": os.getpid(),
            "restart_attempted": restart_attempted, "restarted": restarted,
            "thread_changes": thread_changes, "error_type": error_type,
        }
        write_state(status_path, status)
        notice_id = notify(bind, project_id, operation_id, "restored",
               ("一次性重启完成，Owner 与待审阅结果已恢复。仍需验收原长对话及本轮结果卡，"
                "现有项目恢复推进；原长会话当前任务自然结束后，仅发送已授权的只读续接测试。"
                "本项目仍需真实飞书界面验收，尚未宣布修复完成。" if restarted and not error_type else
                "本次维护未完成，Owner 与待审阅结果已恢复。不会自动重试重启；"
                "请先核对维护记录与服务状态。")) if snapshot is not None else None
        if wait_ready and notice_id and error_type != "SystemExit":
            status["acceptance_dispatch"] = "waiting_for_report_delivery"
            write_state(status_path, status)
            try:
                status["acceptance_review_id"] = dispatch_acceptance(
                    bind, project_id, notice_id, operation_id
                )
                status["acceptance_dispatch"] = "queued"
            except BaseException as error:
                status["acceptance_dispatch"] = "not_dispatched"
                status["dispatch_error_type"] = type(error).__name__
                raise
            finally:
                write_state(status_path, status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "restore"])
    parser.add_argument("operation_id")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--notify-project")
    parser.add_argument("--wait-execution", type=Path)
    parser.add_argument("--wait-ready", action="store_true",
                        help="Wait passively without a deadline before the bounded maintenance window")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", args.operation_id):
        parser.error("Invalid operation ID")
    if args.timeout <= 0:
        parser.error("Timeout must be positive")
    if args.wait_ready and (not args.notify_project or not args.wait_execution):
        parser.error("Passive continuation requires a notification project and execution witness")
    from mac_system.database import _engine

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if args.action == "restore":
        state_dir = root / ".mac" / "long_conversation_maintenance" / args.operation_id
        status = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))
        try:
            os.kill(int(status["pid"]), 0)
        except ProcessLookupError:
            pass
        else:
            parser.error("Maintenance process still exists; inspect it before manual restoration")
        snapshot_path = state_dir / "snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        restore_reviews(_engine, snapshot, restore_owners(_engine, snapshot))
        return

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    execution_witness = (
        json.loads(args.wait_execution.read_text(encoding="utf-8"))
        if args.wait_execution is not None else None
    )
    run(_engine, root, args.operation_id, args.timeout, args.notify_project, execution_witness,
        wait_ready=args.wait_ready)


if __name__ == "__main__":
    main()
