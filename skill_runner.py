"""Sequential skill execution for configurable workflows."""

from __future__ import annotations


class SkillRunner:
    """Tracks progress through a sequence of skill commands."""

    def __init__(
        self,
        session_name: str,
        commands: list[str],
        review_between: bool,
        placeholders: dict[str, str],
    ):
        self.session_name = session_name
        self.review_between = review_between
        self._commands = [self._resolve(cmd, placeholders) for cmd in commands]
        self._current_index = 0

    @staticmethod
    def _resolve(command: str, placeholders: dict[str, str]) -> str:
        result = command
        for key, value in placeholders.items():
            result = result.replace(f"{{{key}}}", value)
        return result

    @property
    def current_prompt(self) -> str | None:
        if self._current_index < len(self._commands):
            return self._commands[self._current_index]
        return None

    def advance(self) -> str | None:
        self._current_index += 1
        return self.current_prompt

    def start_from(self, index: int):
        self._current_index = max(0, min(index, len(self._commands) - 1))

    @property
    def is_complete(self) -> bool:
        return self._current_index >= len(self._commands)

    @property
    def remaining_labels(self) -> list[str]:
        return self._commands[self._current_index:]

    @property
    def current_step(self) -> int:
        return self._current_index + 1

    @property
    def total_steps(self) -> int:
        return len(self._commands)

    @property
    def next_label(self) -> str | None:
        idx = self._current_index + 1
        if idx < len(self._commands):
            return self._commands[idx]
        return None
