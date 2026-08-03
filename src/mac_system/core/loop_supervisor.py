"""Project-level no-progress detection independent of model prose."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mac_system.core.failure_router import FailureRouter


@dataclass(frozen=True)
class LoopAssessment:
    repeated: bool
    signature: str
    occurrences: int
    reason: str


class LoopSupervisor:
    def __init__(self, threshold: int = 3):
        self.threshold = threshold

    def assess(self, current: Any, history: list[Any]) -> LoopAssessment:
        signature = FailureRouter.signature(current)
        occurrences = sum(
            1 for item in history if FailureRouter.signature(item) == signature
        )
        repeated = occurrences >= self.threshold
        return LoopAssessment(
            repeated=repeated,
            signature=signature,
            occurrences=occurrences,
            reason=(
                "相同责任域、回退目标和 AC 集合已重复，禁止继续确定性走原路径；"
                "重复只触发 Coordinator 因果分诊，不证明根因位于更上游"
                if repeated
                else "尚未达到无进展循环阈值"
            ),
        )
