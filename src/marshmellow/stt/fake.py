"""Deterministic STT fake — text fixtures instead of audio (02-milestones testing strategy)."""

from __future__ import annotations

from marshmellow.core.contracts import Transcript


class FakeSTT:
    def transcribe_text(self, text: str) -> Transcript:
        return Transcript(text=text, source="fixture", confidence=1.0)
