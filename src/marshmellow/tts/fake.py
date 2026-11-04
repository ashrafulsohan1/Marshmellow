"""Deterministic TTS fake — records SpeakRequests for assertions."""

from __future__ import annotations

from marshmellow.core.contracts import SpeakRequest


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[SpeakRequest] = []
        self.stop_calls = 0

    def say(self, request: SpeakRequest) -> None:
        self.spoken.append(request)

    def stop(self) -> None:
        """Barge-in kill; the fake records the call so tests can assert it."""
        self.stop_calls += 1

    def quit(self) -> None:
        """Full teardown seam (PiperSpeaker terminates its subprocess); no-op here."""
