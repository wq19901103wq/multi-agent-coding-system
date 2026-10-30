"""Direct checks for progress presentation without a runtime or database."""

from datetime import datetime
import re

from mac_system.core.root_progress import (
    _ProgressState,
    _progress_card_view,
    _progress_projection_key,
    _progress_usage_lines,
    _record_progress_small_step,
    _render_progress_card,
    _start_progress_big_step,
    _usage_delta,
)
from mac_system.utils.identifiers import generate_id


def progress_state():
    return _ProgressState(
        started_monotonic=100.0,
        last_event_monotonic=100.0,
        last_event_at=datetime(2026, 10, 12),
        label="正在执行",
    )


def test_progress_identity_is_opaque_and_unique():
    first = generate_id("progress")
    assert re.fullmatch(r"progress_\d{8}_\d{6}_[0-9a-f]{16}", first)
    assert generate_id("progress") != first


def test_retry_countdown_and_terminal_summary_are_visible():
    state = progress_state()
    state.continuation_retry = {
        "phase": "waiting", "attempt": 2, "maxAttempts": 3,
        "delaySeconds": 60, "scheduledWaitSeconds": 90, "waitBudgetSeconds": 210,
    }
    state.continuation_retry_due_monotonic = 160
    waiting = _progress_card_view(state, now_monotonic=130)
    assert "第 2/3 次" in waiting.current
    assert "还有 30 秒" in waiting.current
    assert "/stop" in waiting.current
    assert "90 秒 / 预算 210 秒" in waiting.current
    assert "暂未收到" not in waiting.current
    assert "还有 0 秒" in _progress_card_view(state, now_monotonic=180).current
    state.status = "failed"
    state.continuation_retry.update(phase="exhausted", stopReason="wait_budget")
    terminal = _progress_card_view(state, now_monotonic=180)
    assert "超出额外等待预算" in terminal.current
    assert "任务未完成" in terminal.current
    assert "还有" not in terminal.current
    state.status = "completed"
    state.continuation_retry.update(phase="recovered")
    assert "共追加 2 次" in _progress_card_view(state, now_monotonic=180).current


def test_progress_projection_rotates_without_mutating_previous_key():
    state = progress_state()
    original = _progress_projection_key("project", state)
    state.projection_segment += 1
    assert _progress_projection_key("project", state) != original
    assert original.endswith(":0")


def test_progress_view_does_not_mutate_the_state():
    state = progress_state()
    _start_progress_big_step(state, "核对边界")
    _record_progress_small_step(state, key="read", label="读取源码", status="completed")
    assert _progress_card_view(state, now_monotonic=120.0) == _progress_card_view(
        state, now_monotonic=120.0
    )
    assert len(state.big_steps) == 1
    assert len(state.big_steps[0].small_steps) == 1
    assert state.big_steps[0].status == "running"


def test_usage_delta_never_charges_preexisting_tokens():
    assert _usage_delta(
        {"totalTokens": 120, "inputTokens": 90},
        {"totalTokens": 150, "inputTokens": 80},
    ) == {"totalTokens": 30, "inputTokens": 0}


def test_progress_retains_unknown_usage_instead_of_inventing_zero():
    state = progress_state()
    state.external_usage["external"] = ("Kimi", None)
    lines = _progress_usage_lines(state)
    assert any("Kimi：后端未报告" in line for line in lines)
    assert not any("可核验合计" in line for line in lines)


def test_progress_card_uses_existing_card_contract():
    card = _render_progress_card("项目", progress_state())
    assert card["header"]["title"]["content"] == "项目"
    assert card["header"]["template"] == "blue"
