"""M1 keyword planner — proves the loop shape; replaced by the LLM planner in M2/M3.

"open X" → one shell action through the safety/executor callback, then a spoken
result. Anything else is an immediate reply that never touches the screen.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    Instruction,
    TaskResult,
)

_OPEN_RE = re.compile(
    r"^\s*open\s+(?P<target>[a-z0-9][a-z0-9 _-]*?)(?:\s+please)?\s*$", re.IGNORECASE
)


class FakePlanner:
    def run(
        self, instruction: Instruction, execute: Callable[[ActionRequest], ActionResult]
    ) -> TaskResult:
        text = instruction.text.strip()
        match = _OPEN_RE.match(text)
        if match:
            target = match.group("target").strip()
            request = ActionRequest(
                kind="shell",
                payload={"open_app": target},
                rationale=f"instruction: open {target}",
                risk="L1",
            )
            result = execute(request)
            if result.ok:
                return TaskResult(
                    ok=True, summary_to_speak=f"Opened {target}.",
                    details=result.observation,
                )
            return TaskResult(
                ok=False, summary_to_speak=f"I could not open {target}.",
                details=result.observation,
            )
        return TaskResult(ok=True, summary_to_speak=f"You said: {text}")
