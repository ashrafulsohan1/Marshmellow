"""ConfirmFlow — the fail-closed spoken yes/no confirmation loop (§3.13).

"Yes" is an explicit whitelist regex, never substring-guessing ("yesterday"
must not confirm because it contains "yes"). An explicit "no" refuses without
a re-ask; an ambiguous answer ("uh", silence, garbage) is re-asked once, then
the tray-dialog fallback is consulted, then the action is REFUSED — ambiguity
never confirms (§3.13, M3 Review Focus 3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Final

logger = logging.getLogger(__name__)

AskFn = Callable[[str], str]
FallbackFn = Callable[[str], bool]

# Explicit whitelists, matched against the WHOLE (stripped) answer.
_YES_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|confirm|please do|affirmative)"
    r"[\s!,.?]*",
    re.IGNORECASE,
)
_NO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:no|nope|nah|stop|cancel|don't|dont|never mind)[\s!,.?]*",
    re.IGNORECASE,
)


def _verdict(answer: str) -> bool | None:
    """True (explicit yes) / False (explicit no) / None (ambiguous)."""
    text = answer.strip()
    if _YES_RE.fullmatch(text):
        return True
    if _NO_RE.fullmatch(text):
        return False
    return None


class ConfirmFlow:
    """Voice confirmation with one re-ask and a tray-dialog fallback.

    `ask` speaks a question and returns the transcript (AskUserChannel.ask at
    composition); `fallback` is the tray dialog (ui.tray.confirm_dialog). With
    no fallback, or after the fallback also declines, confirm() returns False:
    fail closed.
    """

    def __init__(
        self, ask: AskFn, fallback: FallbackFn | None = None, max_reasks: int = 1
    ) -> None:
        if max_reasks < 0:
            raise ValueError("max_reasks must be >= 0")
        self._ask = ask
        self._fallback = fallback
        self._max_reasks = max_reasks

    def confirm(self, question: str) -> bool:
        for attempt in range(1 + self._max_reasks):
            answer = self._ask(question)
            verdict = _verdict(answer)
            if verdict is not None:
                logger.info(
                    "confirmation answer %r → %s (attempt %d)", answer, verdict, attempt
                )
                return verdict
            logger.info(
                "ambiguous confirmation answer %r (attempt %d) — re-asking", answer, attempt
            )
        if self._fallback is not None:
            logger.info("voice confirmation ambiguous — consulting the tray dialog")
            return bool(self._fallback(question))
        logger.info("voice confirmation ambiguous, no fallback — refusing (fail closed)")
        return False
