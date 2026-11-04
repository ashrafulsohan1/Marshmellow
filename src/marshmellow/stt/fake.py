"""Deterministic STT fake — text fixtures instead of audio (02-milestones testing strategy)."""

from __future__ import annotations

from marshmellow.core.contracts import SpeechSegment, Transcript


class FakeSTT:
    """The fixture encoding: a segment's `pcm` bytes carry the UTF-8 text."""

    def transcribe_text(self, text: str) -> Transcript:
        return Transcript(text=text, source="fixture", confidence=1.0)

    def transcribe(self, segment: SpeechSegment) -> Transcript:
        """Transcriber protocol (audio/loop.py): pcm → UTF-8 text; garbage → empty."""
        try:
            text = segment.pcm.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        return Transcript(text=text, source="fixture", confidence=1.0)
