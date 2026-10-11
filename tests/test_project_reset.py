import asyncio
import sys
import threading
from pathlib import Path

import psutil
import pytest

import mac_system.core.root_agent as root
from mac_system.agents.root_runtime import CodexAppServerThread, ProjectLeadRuntimeError, ProjectLeadRuntimeRegistry, TurnResult
from mac_system.agents.kimi_runtime import KimiWireThread
from mac_system.agents.process_lifecycle import host_execution_has_exited
from mac_system.core.root_sessions import recover_interrupted_native_turns
from mac_system.core.notification_service import reconcile_retried_root_report
from mac_system.feishu.handlers import handle_text_message, _deliver_to_root
from mac_system.feishu.card import build_content_card
from mac_system.models import ProjectOwnerReview, QuestionInteraction, Approval, NotificationOutbox
from sqlmodel import select
from tests.test_root_agent import root_context
from tests.test_feishu import ctx, message


@pytest.fixture(autouse=True)
def reset_registries(monkeypatch):
    monkeypatch.setattr(root, "_PROJECT_WORK", {})
    monkeypatch.setattr(root, "_PROJECT_RESETTING", set())
    monkeypatch.setattr(root, "_PROJECT_RESET_EPOCH", {})
    monkeypatch.setattr(root, "_PROJECT_RESET_TASKS", {})
    monkeypatch.setattr(root, "_SERVICE_LOOP", None)


@pytest.mark.asyncio
async def test_reset_cancels_running_and_queued_work_preserves_history(root_context):
    session, engine = root_context
    engine.store.set_native_thread("proj-root", "existing-thread")
    engine.store.set_turn("proj-root", turn_id="stuck", status="running")
    calls = []

    class StuckRuntime:
        async def close(self):
            calls.append("closed")

    root._ROOT_THREADS["proj-root"] = root._RootHandle(StuckRuntime(), asyncio.get_running_loop(), "codex")
    started = asyncio.Event()

    async def blocked():
        async with root._project_work("proj-root"):
            started.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(blocked())
    queued = asyncio.create_task(blocked())
    await started.wait()
    await asyncio.sleep(0)
    await engine.reset_project_runtime("proj-root")
    session.expire_all()
    assert running.cancelled() and queued.cancelled()
    assert calls == ["closed"]
    row = engine.store.ensure("proj-root")
    assert (row.status, row.active_turn_id, row.native_thread_id) == ("idle", None, "existing-thread")
    assert not row.restart_recovery_pending
    assert "proj-root" not in root._ROOT_THREADS
    assert not engine.service.require_project("proj-root").context.get("runtime_reset_required")
    with pytest.raises(ProjectLeadRuntimeError):
        async with root._project_work("proj-root", 0):
            pytest.fail("pre-reset queued work must not execute")
    async with root._project_work("proj-root"):
        pass  # a genuinely new message is allowed


@pytest.mark.asyncio
async def test_reset_joins_foreign_handler_before_its_loop_closes(root_context):
    session, engine = root_context
    ready = threading.Event()
    exited = threading.Event()

    class Runtime:
        async def close(self):
            pass

    root._ROOT_THREADS["proj-root"] = root._RootHandle(Runtime(), asyncio.get_running_loop(), "codex")

    async def handler():
        async with root._project_work("proj-root"):
            ready.set()
            await asyncio.Event().wait()

    def run_handler():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handler())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()
            exited.set()

    thread = threading.Thread(target=run_handler, daemon=True)
    thread.start()
    assert await asyncio.to_thread(ready.wait, 2)
    try:
        await asyncio.wait_for(engine.reset_project_runtime("proj-root"), 2)
        assert await asyncio.to_thread(exited.wait, 2)
        session.expire_all()
        assert engine.store.ensure("proj-root").status == "idle"
        assert not root._PROJECT_WORK.get("proj-root")
    finally:
        for task in tuple(root._PROJECT_WORK.get("proj-root", ())):
            task.get_loop().call_soon_threadsafe(task.cancel)
        await asyncio.to_thread(thread.join, 2)


@pytest.mark.asyncio
async def test_failed_cleanup_fences_project_even_after_restart(root_context):
    session, engine = root_context

    class CannotStop:
        async def close(self):
            raise RuntimeError("writer still alive")

    root._ROOT_THREADS["proj-root"] = root._RootHandle(CannotStop(), asyncio.get_running_loop(), "codex")
    with pytest.raises(RuntimeError, match="writer still alive"):
        await engine.reset_project_runtime("proj-root")
    session.expire_all()
    assert engine.store.ensure("proj-root").status == "resetting"
    assert engine.service.require_project("proj-root").context["runtime_reset_required"]
    assert root._ROOT_THREADS["proj-root"].closing
    root._PROJECT_RESETTING.clear()  # simulate loss of process-local state
    assert "proj-root" not in recover_interrupted_native_turns(session)
    with pytest.raises(ProjectLeadRuntimeError, match="尚未确认"):
        await engine.run("proj-root", "must not run")


@pytest.mark.asyncio
async def test_reset_does_not_claim_success_for_unknown_writer(root_context):
    session, engine = root_context
    engine.store.set_turn("proj-root", turn_id="unknown", status="running")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="归属记录"):
            await engine.reset_project_runtime("proj-root")
        session.expire_all()
        assert engine.store.ensure("proj-root").status == "resetting"


@pytest.mark.asyncio
async def test_reset_timeout_and_retry_are_bounded(root_context, monkeypatch):
    session, engine = root_context
    monkeypatch.setattr(root, "_RESET_WAIT_SECONDS", 0.02)
    release = asyncio.Event()

    class SlowClose:
        async def close(self):
            await release.wait()

    runtime = SlowClose()
    root._ROOT_THREADS["proj-root"] = root._RootHandle(runtime, asyncio.get_running_loop(), "codex")
    with pytest.raises(RuntimeError, match="超时"):
        await asyncio.wait_for(engine.reset_project_runtime("proj-root"), 0.5)
    assert root._ROOT_THREADS["proj-root"].closing
    release.set()
    # Retry while ownership still exists, allowing it to confirm close.
    await engine.reset_project_runtime("proj-root")
    session.expire_all()
    assert engine.store.ensure("proj-root").status == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "kimi"])
async def test_reset_kills_real_wedged_provider_group_only(root_context, provider):
    session, engine = root_context
    script = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
    child_pid = int(await asyncio.wait_for(process.stdout.readline(), 2))
    runtime = (CodexAppServerThread(codex_bin="unused", cwd=Path.cwd(), tools=[])
               if provider == "codex" else KimiWireThread(kimi_bin="unused", cwd=Path.cwd(), tools=[]))
    runtime._process = process
    root._ROOT_THREADS["proj-root"] = root._RootHandle(runtime, asyncio.get_running_loop(), "codex")
    try:
        await asyncio.wait_for(engine.reset_project_runtime("proj-root"), 25)
        assert process.returncode is not None
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        assert unrelated.returncode is None
        session.expire_all()
        assert engine.store.ensure("proj-root").status == "idle"
    finally:
        await runtime.close()
        unrelated.kill()
        await unrelated.wait()


@pytest.mark.asyncio
async def test_reset_command_bypasses_reaction_and_progress_waits(ctx):
    project = await ctx.service.create_project("site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    calls = []

    async def blocked(*args, **kwargs):
        pytest.fail("reset must bypass ordinary presentation/steer path")

    async def reset(project_id):
        calls.append(project_id)

    ctx.bridge.add_reaction = blocked
    ctx.orchestrator.rotate_root_progress_for_timeline_event = blocked
    ctx.orchestrator.reset_root_runtime = reset
    await handle_text_message(ctx, message("/reset"))
    assert calls == [project.id]
    assert not ctx.orchestrator.scheduled and not ctx.orchestrator.steered
    assert ctx.bridge.cards[-1][2]["header"]["title"]["content"] == "当前项目执行已重置"


@pytest.mark.asyncio
async def test_reset_failure_returns_red_card(ctx):
    await ctx.service.create_project("site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat")

    async def fail(project_id):
        raise RuntimeError("writer still alive")

    ctx.orchestrator.reset_root_runtime = fail
    await handle_text_message(ctx, message("/reset"))
    card = ctx.bridge.cards[-1][2]
    assert card["header"]["title"]["content"] == "重置未确认成功"
    assert card["header"]["template"] == "red"


@pytest.mark.asyncio
async def test_reset_releases_real_project_lock_and_new_message_runs(root_context, monkeypatch):
    session, engine = root_context
    entered = asyncio.Event()
    seen = []

    async def run(project_id, text, **kwargs):
        lock = await root.get_default_lock_manager().acquire(project_id)
        async with lock:
            seen.append(text)
            if text == "old":
                entered.set()
                await asyncio.Event().wait()
            return "new-result"

    monkeypatch.setattr(engine, "_run", run)
    old = asyncio.create_task(engine.run("proj-root", "old"))
    await entered.wait()
    queued = asyncio.create_task(engine.run("proj-root", "old-queued"))
    await asyncio.sleep(0)
    await engine.reset_project_runtime("proj-root")
    assert old.cancelled() and queued.cancelled()
    assert await asyncio.wait_for(engine.run("proj-root", "new"), 1) == "new-result"
    assert seen == ["old", "new"]


@pytest.mark.asyncio
async def test_next_message_resumes_same_native_conversation(root_context):
    session, engine = root_context
    calls = []

    class Lead:
        thread_id = None
        active_turn_id = None
        active_model = None

        def __init__(self, context):
            pass

        async def resume_thread(self, thread_id):
            calls.append(("resume", thread_id))
            self.thread_id = thread_id
            return thread_id

        async def run_turn(self, text):
            calls.append(("run", text))
            return TurnResult(self.thread_id, "new-turn", "completed", None, ())

        async def close(self):
            calls.append(("close", self.thread_id))

    engine.store.set_native_thread("proj-root", "kept-thread")
    engine.store.set_turn("proj-root", turn_id=None, status="idle")
    engine.lead_runtime_registry = ProjectLeadRuntimeRegistry({"codex": Lead})
    await engine.reset_project_runtime("proj-root")
    session.expire_all()
    result = await engine.run("proj-root", "check existing work then continue")
    assert result.thread_id == "kept-thread"
    assert calls == [("resume", "kept-thread"), ("run", "check existing work then continue")]


@pytest.mark.asyncio
async def test_duplicate_reset_shares_one_cleanup(root_context):
    _, engine = root_context
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Runtime:
        async def close(self):
            calls.append("close")
            entered.set()
            await release.wait()

    root._ROOT_THREADS["proj-root"] = root._RootHandle(Runtime(), asyncio.get_running_loop(), "codex")
    one = asyncio.create_task(engine.reset_project_runtime("proj-root"))
    await entered.wait()
    two = asyncio.create_task(engine.reset_project_runtime("proj-root"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(one, two)
    assert calls == ["close"]


@pytest.mark.asyncio
async def test_reset_expires_prompts_reviews_and_late_report_cannot_restart(root_context):
    session, engine = root_context
    session.add(ProjectOwnerReview(id="review-reset", project_id="proj-root", request_item_id="report:old", outcome="pending", status="pending"))
    session.add(QuestionInteraction(id="question-reset", project_id="proj-root", batch_key="old", status="resolving"))
    session.add(QuestionInteraction(id="pending-question", project_id="proj-root", batch_key="pending", status="pending"))
    session.add(Approval(id="approval-reset", project_id="proj-root", state="ACTIVE", approval_type="command", status="pending"))
    session.add(ProjectOwnerReview(id="running-review", project_id="proj-root", request_item_id="report:running", outcome="pending", status="running"))
    session.commit()
    notification = engine.service.enqueue_notification(
        dedupe_key="old-late-report", recipient_id="oc-chat", recipient_type="chat_id", intent_type="report", project_id="proj-root",
        payload={"subject_type": "root_turn", "subject_id": "old-delayed-turn", "card": build_content_card("已完成结果", "保留已完成的工作")},
    )
    await engine.reset_project_runtime("proj-root")
    session.expire_all()
    assert session.get(ProjectOwnerReview, "review-reset").status == "superseded"
    assert session.get(QuestionInteraction, "question-reset").status == "expired"
    assert session.get(QuestionInteraction, "pending-question").status == "expired"
    assert session.get(Approval, "approval-reset").status == "expired"
    assert session.get(ProjectOwnerReview, "running-review").status == "superseded"
    assert reconcile_retried_root_report(engine.service, notification, message_id="om-late") is None
    assert engine.service.get_project_owner_review_by_request_item_id("proj-root", "report:old-delayed-turn") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "kimi"])
async def test_reset_cancels_blocked_stdin_without_leaking_pending_request(root_context, provider):
    _, engine = root_context
    entered = asyncio.Event()

    class BlockedInput:
        def write(self, value):
            pass

        async def drain(self):
            entered.set()
            await asyncio.Event().wait()

    class Process:
        stdin = BlockedInput()
        returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    runtime = (CodexAppServerThread(codex_bin="unused", cwd=Path.cwd(), tools=[])
               if provider == "codex" else KimiWireThread(kimi_bin="unused", cwd=Path.cwd(), tools=[]))
    runtime._process = Process()
    runtime.thread_id = "kept-thread"
    runtime.active_turn_id = "stuck-turn"
    root._ROOT_THREADS["proj-root"] = root._RootHandle(runtime, asyncio.get_running_loop(), provider)
    engine.store.set_turn("proj-root", turn_id="stuck-turn", status="running")
    task = asyncio.create_task(engine.steer("proj-root", "old-input"))
    await entered.wait()
    await asyncio.wait_for(engine.reset_project_runtime("proj-root"), 1)
    assert task.cancelled()
    assert not runtime._pending
    assert runtime._process is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["steer", "schedule"])
async def test_message_racing_reset_has_visible_rejection(ctx, phase):
    project = await ctx.service.create_project("site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    if phase == "steer":
        project.execution_status = "running"
        ctx.service.session.add(project)
        ctx.service.session.commit()

        async def reject(*args, **kwargs):
            raise root.ProjectResetError("reset race")

        ctx.orchestrator.steer_root_agent = reject
    else:
        def reject(*args, **kwargs):
            raise root.ProjectResetError("reset race")

        ctx.orchestrator.schedule_root_agent = reject
    await handle_text_message(ctx, message("新的要求"))
    assert ctx.bridge.cards[-1][2]["header"]["title"]["content"] == "消息未执行"


@pytest.mark.asyncio
async def test_queued_old_epoch_message_gets_outbox_notice(root_context):
    session, engine = root_context
    root.schedule_root_turn(engine.bind, engine.runtime, engine.settings, "proj-root", "old-queued", actor_open_id="ou-user")
    root._PROJECT_RESET_EPOCH["proj-root"] = 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    notices = session.exec(select(NotificationOutbox).where(NotificationOutbox.project_id == "proj-root")).all()
    assert len(notices) == 1
    assert notices[0].payload["card"]["header"]["title"]["content"] == "消息未执行"


@pytest.mark.asyncio
async def test_reset_fenced_message_without_actor_uses_chat(ctx):
    project = await ctx.service.create_project("site", "feature", "initial", creator_open_id="ou-user", feishu_chat_id="oc-chat")
    project.context = {"runtime_reset_required": True}
    await _deliver_to_root(ctx, project, "message")
    assert ctx.bridge.cards[-1][0] == "oc-chat"
    assert ctx.bridge.cards[-1][2]["header"]["title"]["content"] == "项目尚未恢复"


@pytest.mark.asyncio
async def test_reset_after_restart_requires_old_execution_exit_then_recovers(root_context):
    session, engine = root_context
    old_host = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True,
    )
    witness = {"processes": [{"pid": old_host.pid, "created_at": psutil.Process(old_host.pid).create_time()}], "groups": [old_host.pid]}
    project = engine.service.require_project("proj-root")
    project.context = {**project.context, "runtime_reset_required": True, "runtime_reset_host_witness": witness}
    session.add(project)
    session.commit()
    engine.store.set_native_thread("proj-root", "preserved-thread")
    engine.store.set_turn("proj-root", turn_id=None, status="resetting")
    try:
        assert "proj-root" not in recover_interrupted_native_turns(session)
        with pytest.raises(RuntimeError, match="旧宿主"):
            await engine.reset_project_runtime("proj-root")
        assert old_host.returncode is None  # witnesses never kill other projects
        session.expire_all()
        assert engine.service.require_project("proj-root").context["runtime_reset_host_witness"] == witness
        old_host.kill()
        await old_host.wait()
        root._PROJECT_RESETTING.clear()  # another new worker, same durable fence
        await engine.reset_project_runtime("proj-root")
        session.expire_all()
        assert engine.store.ensure("proj-root").status == "idle"
        assert engine.store.ensure("proj-root").native_thread_id == "preserved-thread"
        assert not engine.service.require_project("proj-root").context.get("runtime_reset_required")
    finally:
        if old_host.returncode is None:
            old_host.kill()
            await old_host.wait()


def test_reset_witness_does_not_mistake_reused_pid_for_old_process(monkeypatch):
    class Reused:
        def create_time(self):
            return 200.0

        def status(self):
            return psutil.STATUS_RUNNING

    monkeypatch.setattr(psutil, "Process", lambda pid: Reused())
    assert host_execution_has_exited({"processes": [{"pid": 123, "created_at": 100.0}], "groups": []})
    assert not host_execution_has_exited({})


@pytest.mark.asyncio
async def test_reset_snapshot_failure_also_keeps_fence(root_context, monkeypatch):
    session, engine = root_context

    def denied():
        raise psutil.AccessDenied()

    monkeypatch.setattr(root, "snapshot_host_execution", denied)
    with pytest.raises(psutil.AccessDenied):
        await engine.reset_project_runtime("proj-root")
    session.expire_all()
    assert engine.service.require_project("proj-root").context["runtime_reset_required"]
    assert "proj-root" in root._PROJECT_RESETTING
