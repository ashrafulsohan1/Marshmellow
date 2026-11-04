"""PromptBuilder — control-intent table vs Instruction routing (01-architecture §3.3).

Table-driven: every control intent, case/punctuation insensitivity, and the
strictness rule that only a whole utterance matches ("please stop the music"
is NOT a control intent).
"""

import pytest

from marshmellow.core.contracts import (
    ContextSnapshot,
    ControlIntent,
    Instruction,
    Transcript,
)
from marshmellow.core.prompt_builder import PromptBuilder


def _transcript(text: str) -> Transcript:
    return Transcript(text=text, source="fixture")


# (utterance, expected ControlIntent.kind) — covers each intent and case-insensitivity.
CONTROL_CASES: list[tuple[str, str]] = [
    ("stop", "stop"),
    ("STOP", "stop"),
    ("Stop.", "stop"),
    ("  stop  ", "stop"),
    ("cancel", "cancel"),
    ("Cancel!", "cancel"),
    ("CANCEL", "cancel"),
    ("mute", "mute"),
    ("Mute", "mute"),
    ("MUTE", "mute"),
    ("unmute", "unmute"),
    ("Unmute", "unmute"),
    ("UNMUTE", "unmute"),
    ("wake up", "wake"),
    ("Wake Up", "wake"),
    ("WAKE UP", "wake"),
    ("go to sleep", "sleep"),
    ("Go To Sleep", "sleep"),
    ("GO TO SLEEP", "sleep"),
    ("stop everything", "kill_all"),
    ("Stop Everything", "kill_all"),
    ("STOP EVERYTHING!!", "kill_all"),
]

# Utterances that must NOT match, even though they contain a control word.
NOT_CONTROL_CASES: list[str] = [
    "please stop the music",
    "stop the music",
    "stopping now",
    "cancel my subscription",
    "unmute the video please",
    "wake up at seven",
    "wakeup",
    "open notepad and type hello",
]


@pytest.mark.parametrize(("utterance", "kind"), CONTROL_CASES)
def test_control_intent_table(utterance: str, kind: str) -> None:
    result = PromptBuilder().build(_transcript(utterance), ContextSnapshot())
    assert isinstance(result, ControlIntent)
    assert result.kind == kind
    assert result.matched_text == utterance


@pytest.mark.parametrize("utterance", NOT_CONTROL_CASES)
def test_not_a_control_intent_becomes_instruction(utterance: str) -> None:
    context = ContextSnapshot()
    result = PromptBuilder().build(_transcript(utterance), context)
    assert isinstance(result, Instruction)
    assert result.text == utterance
    assert result.context == context


def test_instruction_carries_transcript_and_context() -> None:
    transcript = _transcript("open notepad and type hello")
    context = ContextSnapshot()
    result = PromptBuilder().build(transcript, context)
    assert isinstance(result, Instruction)
    assert result.text == "open notepad and type hello"
    assert result.transcript == transcript
    assert result.context == context


def test_clipboard_kept_only_when_instruction_references_it() -> None:
    context = ContextSnapshot(clipboard="copied secret")
    builder = PromptBuilder()

    referencing = builder.build(_transcript("paste what I copied"), context)
    assert isinstance(referencing, Instruction)
    assert referencing.context.clipboard == "copied secret"

    unrelated = builder.build(_transcript("open notepad"), context)
    assert isinstance(unrelated, Instruction)
    assert unrelated.context.clipboard is None
    # the caller's snapshot is never mutated
    assert context.clipboard == "copied secret"
