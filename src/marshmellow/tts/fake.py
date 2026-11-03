"""Deterministic TTS fake — records SpeakRequests for assertions."""

from __future__ import annotations

from marshmellow.core.contracts import SpeakRequest


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[SpeakRequest] = []

    def say(self, request: SpeakRequest) -> None:
        self.spoken.append(request)

    def stop(self) -> None:
        """Barge-in kill; nothing to stop for the fake."""
