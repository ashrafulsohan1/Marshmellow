"""Scripted LLM fake — replays a fixed ActionRequest sequence (golden-trace tests)."""

from __future__ import annotations

from marshmellow.core.contracts import ActionRequest


class FakeLLM:
    def __init__(self, script: list[ActionRequest]) -> None:
        self._script = list(script)

    def next_action(self) -> ActionRequest | None:
        if not self._script:
            return None
        return self._script.pop(0)
