"""InputSynthesizer tests — event shaping via a recording send_input (no real input).

The synthesizer is the §3.7 SendInput path: jittered, human-plausible event
streams. All tests inject `send_input` (records events), `sleep` (records
delays) and a seeded rng (or jitter_px=0) so nothing real moves and everything
is assertable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from marshmellow.actions.input_synthesis import (
    InputSynthesizer,
    KeyboardEvent,
    MouseEvent,
)


@dataclass
class RecorderSendInput:
    """Injectable send_input seam: records every event batch."""

    events: list[KeyboardEvent | MouseEvent] = field(default_factory=list)

    def __call__(self, batch: list[KeyboardEvent | MouseEvent]) -> int:
        self.events.extend(batch)
        return len(batch)


@dataclass
class RecordingSleep:
    """Injectable sleep seam: records durations (seconds)."""

    durations: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


def _synth(
    recorder: RecorderSendInput,
    sleep: RecordingSleep,
    **kwargs: object,
) -> InputSynthesizer:
    return InputSynthesizer(
        send_input=recorder,
        delay_ms=(20, 60),
        sleep=sleep,
        **kwargs,  # type: ignore[arg-type]
    )


def test_click_moves_then_left_down_up_at_exact_point() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep, jitter_px=0).click((550, 325))

    assert recorder.events == [
        MouseEvent(kind="move", x=550, y=325),
        MouseEvent(kind="down", button="left"),
        MouseEvent(kind="up", button="left"),
    ]


def test_click_right_uses_right_button_events() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep, jitter_px=0).click((10, 10), right=True)

    assert [e for e in recorder.events if isinstance(e, MouseEvent)] == [
        MouseEvent(kind="move", x=10, y=10),
        MouseEvent(kind="down", button="right"),
        MouseEvent(kind="up", button="right"),
    ]


def test_double_click_is_two_down_up_pairs() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep, jitter_px=0).click((7, 9), double=True)

    assert recorder.events == [
        MouseEvent(kind="move", x=7, y=9),
        MouseEvent(kind="down", button="left"),
        MouseEvent(kind="up", button="left"),
        MouseEvent(kind="down", button="left"),
        MouseEvent(kind="up", button="left"),
    ]


def test_click_jitter_stays_within_budget() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    synth = InputSynthesizer(
        send_input=recorder, delay_ms=(20, 60), jitter_px=5, sleep=sleep,
        rng=random.Random(42),
    )
    for _ in range(50):
        synth.click((1000, 500))

    moves = [e for e in recorder.events if isinstance(e, MouseEvent) and e.kind == "move"]
    assert len(moves) == 50
    assert all(abs(move.x - 1000) <= 5 and abs(move.y - 500) <= 5 for move in moves)


def test_type_text_emits_unicode_down_up_per_char_with_inter_key_delays() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep).type_text("hi")

    assert [(e.scan, e.vk, e.unicode, e.up) for e in recorder.events] == [
        (ord("h"), 0, True, False),
        (ord("h"), 0, True, True),
        (ord("i"), 0, True, False),
        (ord("i"), 0, True, True),
    ]
    # inter-key delays: recorded, jittered inside the configured band
    assert sleep.durations
    assert all(0.020 <= d <= 0.060 for d in sleep.durations)


def test_type_text_splits_astral_chars_into_surrogate_units() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep).type_text("😀")  # U+1F600 -> UTF-16 D83D DE00

    assert [(e.scan, e.up) for e in recorder.events] == [
        (0xD83D, False),
        (0xD83D, True),
        (0xDE00, False),
        (0xDE00, True),
    ]


def test_type_text_empty_emits_nothing() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep).type_text("")

    assert recorder.events == []
    assert sleep.durations == []


def test_hotkey_downs_then_reverse_ups() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep).hotkey("ctrl+shift+s")

    assert [(e.vk, e.scan, e.up) for e in recorder.events] == [
        (0x11, 0, False),  # ctrl down
        (0x10, 0, False),  # shift down
        (0x53, 0, False),  # s down
        (0x53, 0, True),   # reverse: s up
        (0x10, 0, True),
        (0x11, 0, True),
    ]


def test_hotkey_named_keys_and_unknown_tokens() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep).hotkey("enter")
    assert [(e.vk, e.up) for e in recorder.events] == [(0x0D, False), (0x0D, True)]

    with pytest.raises(ValueError, match="unknown key"):
        _synth(RecorderSendInput(), RecordingSleep()).hotkey("ctrl+banana")


def test_scroll_moves_to_point_then_wheel_notches() -> None:
    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep, jitter_px=0).scroll((100, 200), 3)

    assert recorder.events == [
        MouseEvent(kind="move", x=100, y=200),
        MouseEvent(kind="wheel", delta=120),
        MouseEvent(kind="wheel", delta=120),
        MouseEvent(kind="wheel", delta=120),
    ]

    recorder, sleep = RecorderSendInput(), RecordingSleep()
    _synth(recorder, sleep, jitter_px=0).scroll((100, 200), -2)
    wheel = [e for e in recorder.events if isinstance(e, MouseEvent) and e.kind == "wheel"]
    assert [e.delta for e in wheel] == [-120, -120]
