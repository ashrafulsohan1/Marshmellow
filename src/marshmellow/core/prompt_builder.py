"""PromptBuilder — transcript → control intent or Instruction (01-architecture §3.3).

Control intents are matched against a phrase table of whole normalized
utterances — never an LLM call ("stop" must not cost a round trip). Anything
else becomes an `Instruction` carrying the transcript and the context
snapshot, with the clipboard privacy gate applied: clipboard text is kept only
if the instruction itself references "clipboard"/"copied".
"""

from __future__ import annotations

import re
from typing import Final, Literal

from marshmellow.core.contracts import (
    ContextSnapshot,
    ControlIntent,
    Instruction,
    Transcript,
)

# Mirrors ControlIntent.kind's Literal; pydantic validation at construction is
# the drift guard (exercised by the table tests for every phrase below).
IntentKind = Literal["stop", "cancel", "mute", "unmute", "sleep", "wake", "kill_all"]

# Whole-utterance phrase table: normalized utterance -> intent kind. Exact
# matching is what keeps "please stop the music" out of the control path.
_CONTROL_PHRASES: Final[dict[str, IntentKind]] = {
    "stop": "stop",
    "cancel": "cancel",
    "mute": "mute",
    "unmute": "unmute",
    "wake up": "wake",
    "go to sleep": "sleep",
    "stop everything": "kill_all",
}

# Punctuation/whitespace stripped from utterance edges before matching
# (STT often emits "Stop." / "STOP EVERYTHING!").
_EDGE_NOISE: Final[str] = " \t\r\n.,!?;:'\"…—"

_CLIPBOARD_RE: Final[re.Pattern[str]] = re.compile(r"\b(clipboard|copied)\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Casefold, drop edge punctuation, collapse whitespace: "STOP!!" -> "stop"."""
    return " ".join(text.casefold().strip(_EDGE_NOISE).split())


class PromptBuilder:
    """Routes a transcript to a control intent (local handling) or an Instruction.

    Pure table lookup plus context assembly — synchronous, side-effect free,
    and no LLM call, ever.
    """

    def build(
        self, transcript: Transcript, context: ContextSnapshot
    ) -> ControlIntent | Instruction:
        phrase = _normalize(transcript.text)
        kind = _CONTROL_PHRASES.get(phrase)
        if kind is not None:
            return ControlIntent(kind=kind, matched_text=transcript.text)
        return Instruction(
            text=transcript.text,
            transcript=transcript,
            context=self._gated_context(transcript.text, context),
        )

    @staticmethod
    def _gated_context(text: str, context: ContextSnapshot) -> ContextSnapshot:
        """Privacy gate (§3.3): clipboard survives only if the text references it."""
        if context.clipboard is None or _CLIPBOARD_RE.search(text):
            return context
        return context.model_copy(update={"clipboard": None})
