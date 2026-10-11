import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import mac_system.core.project_owner as project_owner_module
from mac_system.agents.root_runtime import TurnResult
from mac_system.config import Settings
from mac_system.core.project_service import ProjectService
from mac_system.models import EventLog
from scripts.evaluate_project_owner import (
    SCENARIOS,
    add_scenario_project,
    evaluate_result,
    run_scenario,
    summarize_historical_holdouts,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario.human_history],
    ids=lambda scenario: scenario.id,
)
async def test_authorization_boundary_context_preserves_original_human_history(
    scenario, tmp_path, monkeypatch
):
    captured = {}

    class ContextOwnerThread:
        def __init__(self, **kwargs):
            captured["instructions"] = kwargs["developer_instructions"]
            self.active_model = "owner-context-test"

        async def create_thread(self):
            return "owner-context-thread"

        async def run_turn(self, prompt, *, output_schema=None):
            return TurnResult(
                thread_id="owner-context-thread",
                turn_id="owner-context-turn",
                status="completed",
                final_message=json.dumps({
                    "outcome": "decide",
                    "decision": "继续原目标" if scenario.expect_continue else "保持当前边界",
                    "rationale": "依据真人原话及当前结果。",
                    "impact": "不扩大执行范围。",
                    "reversible": True,
                    "confidence": "high",
                    "answers": [],
                    "user_options": [],
                    "continue_execution": scenario.expect_continue,
                    "preference_observations": [],
                    "authority_refs": [f"history_{scenario.id}_1:instruction:1"],
                }),
                events=(),
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        project_owner_module, "CodexAppServerThread", ContextOwnerThread
    )
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = add_scenario_project(session, scenario, "ou-eval", tmp_path)
        service = ProjectService(session)
        row = await run_scenario(service, Settings(PROJECTS_DIR=tmp_path), scenario, project)
        snapshot = session.exec(
            select(EventLog).where(EventLog.message == "Project Owner context snapshot")
        ).one().meta["context"]

        assert row["evaluation"]["passed"] is True
        assert snapshot["human_mainline"]["latest_instruction"]["text"] == scenario.instruction
        assert [
            instruction["text"]
            for instruction in snapshot["authority"]["current_human_instructions"]
        ] == [scenario.instruction]
        assert [
            instruction["text"]
            for instruction in snapshot["authority"]["recent_human_instructions"]
        ] == [scenario.instruction, *[
            instruction for instruction, _report in reversed(scenario.human_history)
        ]]
        assert all(
            instruction["source_ref"].startswith(f"history_{scenario.id}_")
            for instruction in snapshot["human_mainline"]["recent_context"]
        )
        assert "普通查询不撤销已授权的未完成工作" in snapshot["human_mainline"]["context_role"]
        assert "查询已经答复不等于此前已授权的工作已经完成" in captured["instructions"]
        assert "后续普通查询不能恢复已暂停的工作或验收" in captured["instructions"]
    engine.dispose()


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in SCENARIOS if scenario.human_history],
    ids=lambda scenario: scenario.id,
)
def test_authorization_boundary_evaluation_rejects_wrong_continuation(scenario):
    result = SimpleNamespace(
        outcome="decide",
        decision="保持原范围",
        rationale="依据真人原话。",
        impact="不扩大范围。",
        user_options=[],
        continue_execution=not scenario.expect_continue,
    )

    evaluation = evaluate_result(scenario, result)

    assert evaluation["passed"] is False
    assert evaluation["checks"]["execution_boundary"] is False


def test_historical_holdout_summary_reports_accuracy_abstention_and_recurrence():
    summary = summarize_historical_holdouts(
        [
            {
                "kind": "user_choice",
                "outcome": "decide",
                "exact_match": True,
                "repeated_rejected_choice": False,
            },
            {
                "kind": "user_choice",
                "outcome": "defer",
                "exact_match": False,
                "repeated_rejected_choice": False,
            },
            {
                "kind": "owner_correction",
                "outcome": "decide",
                "exact_match": False,
                "repeated_rejected_choice": True,
            },
            {
                "kind": "owner_correction",
                "outcome": "defer",
                "exact_match": False,
                "repeated_rejected_choice": False,
            },
        ]
    )

    assert summary == {
        "total": 4,
        "completed": 4,
        "coverage": 0.5,
        "choice_accuracy_when_autonomous": 0.5,
        "overall_exact_match": 0.25,
        "wrong_autonomous_decision_rate": 0.25,
        "abstention_rate": 0.5,
        "correction_cases": 2,
        "correction_recurrence_rate": 0.5,
    }


def test_historical_holdout_summary_excludes_provider_failures_from_rates():
    summary = summarize_historical_holdouts(
        [
            {"kind": "user_choice", "error": "provider unavailable"},
            {
                "kind": "user_choice",
                "outcome": "defer",
                "exact_match": False,
                "repeated_rejected_choice": False,
            },
        ]
    )

    assert summary["total"] == 2
    assert summary["completed"] == 1
    assert summary["abstention_rate"] == 1.0
