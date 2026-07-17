from typing import Any

from copy import deepcopy

from mac_system.config import load_state_machine, load_workflow_profile


# Compatibility aliases: old state names that were renamed.  The StateMachine
# accepts either the new or the old name when looking up a state, so projects
# in the database with old state names keep working after the rename.
_STATE_ALIASES: dict[str, str] = {
    # The former Product Spec stage was folded into the single Specify stage.
    # Re-run that unified stage for any unfinished test project left there.
    "PLANNING_PRODUCT_SPEC": "PLANNING_REQUIREMENTS",
    "PRODUCT_SPEC_INPUT_PENDING": "REQUIREMENT_INPUT_PENDING",
    "PRODUCT_SPEC_CONFIRM_PENDING": "REQUIREMENT_CONFIRM_PENDING",
    "PLANNING_SPECS": "PLANNING_IMPLEMENTATION_SPECS",
    "SPEC_INPUT_PENDING": "IMPLEMENTATION_SPEC_INPUT_PENDING",
    "SPEC_CONFIRM_PENDING": "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
}


class StateMachine:
    def __init__(self, task_type: str):
        self._template = load_state_machine()
        self._profile = load_workflow_profile(task_type)
        if self._profile.get("task_type") != task_type:
            raise ValueError(f"Workflow profile task_type mismatch: expected {task_type}")
        self.task_type = task_type
        self._config = self._compile()
        self.initial_state = self._config["initial_state"]
        self._states = self._config["states"]

    @property
    def profile(self) -> dict[str, Any]:
        return self._profile

    def _compile(self) -> dict[str, Any]:
        """Expand one lifecycle template with a task-specific stage profile.

        Profiles describe *what* stages exist.  Event semantics such as
        complete/ask_user/fail/confirm/reject live only in delivery.json.
        The generated concrete names preserve the public API during the v1
        migration and keep existing Feishu cards and persisted logs readable.
        """
        stages = self._profile.get("stages", [])
        if not stages:
            raise ValueError(f"Workflow profile {self.task_type} has no stages")

        states = deepcopy(self._template["common_states"])
        stage_by_id = {stage["id"]: stage for stage in stages}
        if len(stage_by_id) != len(stages):
            raise ValueError(f"Workflow profile {self.task_type} contains duplicate stage ids")

        policies = self._template["stage_policies"]
        for index, stage in enumerate(stages):
            kind = stage["kind"]
            if kind not in policies:
                raise ValueError(f"Unknown stage kind {kind!r} in profile {self.task_type}")
            policy = policies[kind]
            execution_name = stage["execution_state"]
            confirmation_name = stage["confirmation"]["state"]
            next_execution = (
                stages[index + 1]["execution_state"]
                if index + 1 < len(stages)
                else "ACCEPTANCE_PENDING"
            )

            execution = {
                "type": "agent_execution",
                "agent": stage["agent"],
                "display": stage["display"],
                "next_states": {
                    "complete": confirmation_name,
                    "fail": policy["fail_target"],
                },
            }
            recovery_name = f"{execution_name}_RECOVERY_PENDING"
            execution["next_states"]["runtime_exhausted"] = recovery_name
            recovery_events = {
                "retry": execution_name,
                "cancel": "CANCELLED",
            }
            if kind in ("implementation", "quality"):
                recovery_events["choose_rework"] = "REWORK"
            states[recovery_name] = {
                "type": "human_confirmation",
                "display": f"{stage['display']}失败，等待处理",
                "recovery_for": execution_name,
                "next_states": recovery_events,
            }
            if stage.get("stage"):
                execution["stage"] = stage["stage"]
            if stage.get("role"):
                execution["role"] = stage["role"]
            if policy.get("fail_max_target"):
                execution["next_states"]["fail_max_retries"] = policy["fail_max_target"]

            inputs = stage.get("inputs", [])
            if inputs:
                default_input = next((item for item in inputs if item.get("default")), inputs[0])
                execution["next_states"]["ask_user"] = default_input["state"]
                for input_spec in inputs:
                    event = input_spec.get("event")
                    if event:
                        execution["next_states"][event] = input_spec["state"]
                    input_transitions = {
                        event_name: (
                            execution_name
                            if target == "$self"
                            else self._resolve_target(target, stage_by_id)
                        )
                        for event_name, target in input_spec.get(
                            "return_events", {"answer": "$self"}
                        ).items()
                    }
                    states[input_spec["state"]] = {
                        "type": "human_input",
                        "input_purpose": input_spec.get("purpose", "clarification"),
                        "display": input_spec["display"],
                        "next_states": input_transitions,
                    }

            for event, target in stage.get("routes", {}).items():
                execution["next_states"][event] = self._resolve_target(target, stage_by_id)
            states[execution_name] = execution

            confirmation_events = {
                "confirm": next_execution,
                "reject": execution_name if policy["reject_target"] == "$self" else policy["reject_target"],
            }
            for event, target in stage["confirmation"].get("routes", {}).items():
                confirmation_events[event] = self._resolve_target(target, stage_by_id)
            states[confirmation_name] = {
                "type": "human_confirmation",
                "display": stage["confirmation"]["display"],
                "next_states": confirmation_events,
            }

        first_state = stages[0]["execution_state"]
        states["INIT"]["next_states"]["start"] = first_state

        states["REWORK"]["next_states"] = {
            item["action"]: stage_by_id[item["target_stage"]]["execution_state"]
            for item in self._profile.get("rework_targets", [])
        }
        acceptance = states["ACCEPTANCE_PENDING"]["next_states"]
        for item in self._profile.get("acceptance_targets", []):
            acceptance[item["action"]] = stage_by_id[item["target_stage"]]["execution_state"]

        return {
            "machine_type": self._template.get("machine_type", "delivery"),
            "task_type": self.task_type,
            "description": self._profile.get("description", ""),
            "initial_state": first_state,
            "states": states,
        }

    @staticmethod
    def _resolve_target(target: str, stage_by_id: dict[str, dict[str, Any]]) -> str:
        if target == "$self":
            raise ValueError("$self is only valid as a policy reject target")
        if target.startswith("$stage:"):
            stage_id = target.removeprefix("$stage:")
            try:
                return stage_by_id[stage_id]["execution_state"]
            except KeyError as exc:
                raise ValueError(f"Unknown workflow stage target {stage_id!r}") from exc
        return target

    def rework_options(self) -> list[dict[str, str]]:
        stage_by_id = {stage["id"]: stage for stage in self._profile["stages"]}
        return [
            {
                "action": item["action"],
                "label": item["label"],
                "next_state": stage_by_id[item["target_stage"]]["execution_state"],
            }
            for item in self._profile.get("rework_targets", [])
        ]

    def acceptance_rework_options(self) -> list[dict[str, str]]:
        stage_by_id = {stage["id"]: stage for stage in self._profile["stages"]}
        return [
            {
                "action": item["action"],
                "label": item["label"],
                "next_state": stage_by_id[item["target_stage"]]["execution_state"],
            }
            for item in self._profile.get("acceptance_targets", [])
        ]

    @property
    def states(self) -> dict[str, Any]:
        return self._states

    def validate(self) -> list[str]:
        """Return a list of consistency errors in the compiled state machine.

        Profiles are declarative and easy to break by typo.  This catches
        unresolved state references after compilation so tests can reject
        broken profiles early.
        """
        errors: list[str] = []
        for name, state in self._states.items():
            for event, target in state.get("next_states", {}).items():
                if target == "<previous_state>":
                    continue
                resolved = self._resolve_alias(target)
                if resolved not in self._states:
                    errors.append(
                        f"State {name!r} event {event!r} targets unknown state {target!r}"
                    )
        return errors

    def _resolve_alias(self, name: str) -> str:
        """Map a legacy state name to its current name, if renamed."""
        return _STATE_ALIASES.get(name, name)

    def get_state(self, name: str) -> dict[str, Any]:
        resolved = self._resolve_alias(name)
        if resolved not in self._states:
            raise ValueError(f"Unknown state '{name}' for task_type={self.task_type}")
        return self._states[resolved]

    def state_type(self, name: str) -> str:
        return self.get_state(name)["type"]

    def next_state(self, current: str, event: str) -> str | None:
        state = self.get_state(current)
        next_map = state.get("next_states", {})
        return next_map.get(event)

    def available_events(self, current: str) -> list[str]:
        state = self.get_state(current)
        return list(state.get("next_states", {}).keys())

    def is_terminal(self, name: str) -> bool:
        return self.state_type(name) == "terminal"

    def is_human_confirmation(self, name: str) -> bool:
        return self.state_type(name) == "human_confirmation"

    def is_human_input(self, name: str) -> bool:
        return self.state_type(name) == "human_input"

    def is_agent_execution(self, name: str) -> bool:
        return self.state_type(name) == "agent_execution"

    def is_system_action(self, name: str) -> bool:
        return self.state_type(name) == "system_action"

    def is_idle(self, name: str) -> bool:
        return self.state_type(name) == "idle"

    def display_name(self, name: str) -> str:
        return self.get_state(name).get("display", name)

    def agent_for(self, name: str) -> str | None:
        return self.get_state(name).get("agent")

    def stage_for(self, name: str) -> str | None:
        return self.get_state(name).get("stage")
