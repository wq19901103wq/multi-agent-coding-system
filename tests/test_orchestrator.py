from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mac_system.agents.orchestrator import AgentOrchestrator
from mac_system.agents.runtime import FakeRuntime


@pytest.fixture
def facade():
    project = SimpleNamespace(
        id="p1", description="目标", status="active", execution_status="idle"
    )
    service = SimpleNamespace(
        session=SimpleNamespace(get_bind=lambda: object()),
        require_project=Mock(return_value=project),
        _running_tasks=Mock(return_value=[]),
        cancel_project=AsyncMock(),
    )
    return AgentOrchestrator(
        service,
        FakeRuntime({}),
    )


@pytest.mark.asyncio
async def test_tick_is_one_native_turn(facade, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(facade, "run_root_agent", run)
    await facade.tick_until_stable("p1", max_ticks=99)
    run.assert_awaited_once_with("p1")


def test_facade_contains_no_workflow_methods(facade):
    for name in (
        "_execute_auxiliary_agent",
        "_send_confirmation_cards",
        "_build_confirmation_question",
        "run_chat_scoped_agent",
    ):
        assert not hasattr(facade, name)


def test_idle_active_project_has_immediate_work(facade):
    assert facade._has_immediate_work("p1") is True


@pytest.mark.asyncio
async def test_cancel_uses_native_interrupt(facade, monkeypatch):
    engine = SimpleNamespace(interrupt=AsyncMock(return_value=True))
    monkeypatch.setattr(facade, "_engine", lambda: engine)
    await facade.cancel_project("p1", "stop")
    facade.project_service.cancel_project.assert_awaited_once()
    engine.interrupt.assert_awaited_once_with("p1")


@pytest.mark.asyncio
async def test_stop_turn_preserves_project_and_uses_native_interrupt(
    facade, monkeypatch
):
    engine = SimpleNamespace(interrupt=AsyncMock(return_value=True))
    monkeypatch.setattr(facade, "_engine", lambda: engine)

    assert await facade.stop_root_turn("p1") is True

    facade.project_service.cancel_project.assert_not_awaited()
    engine.interrupt.assert_awaited_once_with("p1")
