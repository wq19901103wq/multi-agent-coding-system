from typing import Any

from mac_system.config import load_state_machine


class StateMachine:
    def __init__(self, task_type: str):
        self._config = load_state_machine(task_type)
        self.task_type = self._config["task_type"]
        self.initial_state = self._config["initial_state"]
        self._states = self._config["states"]

    @property
    def states(self) -> dict[str, Any]:
        return self._states

    def get_state(self, name: str) -> dict[str, Any]:
        if name not in self._states:
            raise ValueError(f"Unknown state '{name}' for task_type={self.task_type}")
        return self._states[name]

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
