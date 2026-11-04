"""Activation tests — hotkey state machine on injected keys + hook-count seam, wake frame
loop on scripted scores. Real-model tier is dev-gated and skips cleanly when absent."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest

from marshmellow.audio.activation import (
    VK_CTRL,
    VK_M,
    VK_SHIFT,
    HotkeyWatcher,
    WakeWordWatcher,
)
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActivationDegraded,
    HotkeyPressed,
    HotkeyReleased,
    WakeDetected,
)

REPO = Path(__file__).resolve().parents[2]
DEV_MODELS = REPO / "docs" / "plan" / "spikes" / "data" / "sp3_dev_models"
DEV_CLIPS = REPO / "docs" / "plan" / "spikes" / "data" / "sp3_dev"


def _wait(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            pytest.fail("condition not met within timeout")
        time.sleep(0.01)


class Collector:
    """Bus collector that records matching events from the thread-safe drain."""

    def __init__(self, bus: EventBus, *etypes: type) -> None:
        self.events: list[object] = []
        self._lock = threading.Lock()
        for et in etypes:
            bus.subscribe(et, self._on)  # type: ignore[arg-type]

    def _on(self, ev: object) -> None:
        with self._lock:
            self.events.append(ev)

    def of(self, etype: type) -> list[object]:
        with self._lock:
            return [e for e in self.events if type(e) is etype]


class FakeHookBackend:
    """Hook seam double: counts installs/uninstalls instead of touching user32."""

    def __init__(self, handle: int = 42) -> None:
        self._handle = handle
        self.install_count = 0
        self.uninstall_count = 0
        self._stop = threading.Event()

    def install(self, on_key: Callable[[int, bool], None]) -> int:
        self.install_count += 1
        return self._handle

    def run(self) -> None:
        self._stop.wait(timeout=5.0)  # stand-in for the message pump

    def request_stop(self) -> None:
        self._stop.set()

    def uninstall(self, handle: int) -> None:
        self.uninstall_count += 1


# ---------------------------------------------------------------- hotkey watcher


def test_hold_mode_reacts_to_injected_keys() -> None:
    bus = EventBus()
    seen = Collector(bus, HotkeyPressed, HotkeyReleased)
    watcher = HotkeyWatcher(bus, combo="ctrl+shift+m", mode="hold",
                            backend=FakeHookBackend())
    watcher.on_key(VK_CTRL, True)
    watcher.on_key(VK_SHIFT, True)
    watcher.on_key(VK_M, True)
    watcher.on_key(VK_M, False)
    _wait(lambda: len(seen.of(HotkeyReleased)) == 1)
    pressed = seen.of(HotkeyPressed)
    released = seen.of(HotkeyReleased)
    assert len(pressed) == 1 and pressed[0].combo == "ctrl+shift+m"  # type: ignore[attr-defined]
    assert len(released) == 1 and released[0].combo == "ctrl+shift+m"  # type: ignore[attr-defined]


def test_hold_mode_ignores_key_repeat() -> None:
    bus = EventBus()
    seen = Collector(bus, HotkeyPressed, HotkeyReleased)
    watcher = HotkeyWatcher(bus, mode="hold", backend=FakeHookBackend())
    watcher.on_key(VK_CTRL, True)
    watcher.on_key(VK_SHIFT, True)
    watcher.on_key(VK_M, True)
    watcher.on_key(VK_M, True)  # LL-hook auto-repeat: second down while held
    watcher.on_key(VK_M, False)
    _wait(lambda: len(seen.of(HotkeyReleased)) == 1)
    assert len(seen.of(HotkeyPressed)) == 1


def test_hold_mode_released_when_modifier_lifted() -> None:
    bus = EventBus()
    seen = Collector(bus, HotkeyPressed, HotkeyReleased)
    watcher = HotkeyWatcher(bus, mode="hold", backend=FakeHookBackend())
    watcher.on_key(VK_CTRL, True)
    watcher.on_key(VK_SHIFT, True)
    watcher.on_key(VK_M, True)
    _wait(lambda: len(seen.of(HotkeyPressed)) == 1)
    watcher.on_key(VK_CTRL, False)  # combo broken mid-hold
    _wait(lambda: len(seen.of(HotkeyReleased)) == 1)


def test_toggle_mode_flips_on_each_press() -> None:
    bus = EventBus()
    seen = Collector(bus, HotkeyPressed, HotkeyReleased)
    watcher = HotkeyWatcher(bus, mode="toggle", backend=FakeHookBackend())
    watcher.on_key(VK_CTRL, True)
    watcher.on_key(VK_SHIFT, True)
    watcher.on_key(VK_M, True)
    _wait(lambda: len(seen.of(HotkeyPressed)) == 1)
    watcher.on_key(VK_M, False)
    watcher.on_key(VK_M, True)  # second full press toggles off
    _wait(lambda: len(seen.of(HotkeyReleased)) == 1)
    watcher.on_key(VK_M, False)
    watcher.on_key(VK_M, True)  # third press toggles on again
    _wait(lambda: len(seen.of(HotkeyPressed)) == 2)


def test_incomplete_combo_emits_nothing() -> None:
    bus = EventBus()
    seen = Collector(bus, HotkeyPressed, HotkeyReleased)
    watcher = HotkeyWatcher(bus, mode="hold", backend=FakeHookBackend())
    watcher.on_key(VK_M, True)  # no modifiers held
    watcher.on_key(VK_M, False)
    watcher.on_key(VK_SHIFT, True)
    watcher.on_key(VK_M, True)  # ctrl still missing
    watcher.on_key(VK_M, False)
    time.sleep(0.2)  # give the drain thread time to (not) deliver
    assert seen.events == []


def test_unknown_modifier_in_combo_rejected() -> None:
    with pytest.raises(ValueError):
        HotkeyWatcher(EventBus(), combo="ctrl+banana+m", backend=FakeHookBackend())


def test_stop_unregisters_hook_exactly_once() -> None:
    bus = EventBus()
    backend = FakeHookBackend()
    watcher = HotkeyWatcher(bus, mode="hold", backend=backend)
    watcher.start()
    _wait(lambda: backend.install_count == 1)
    watcher.stop()
    assert backend.uninstall_count == 1
    assert backend.install_count == 1
    assert not any(t.name == "hotkey-hook" and t.is_alive()
                   for t in threading.enumerate())


def test_registration_failure_emits_spoken_degradation() -> None:
    bus = EventBus()
    seen = Collector(bus, ActivationDegraded)
    backend = FakeHookBackend(handle=0)  # SetWindowsHookExW failure, injected
    watcher = HotkeyWatcher(bus, mode="hold", backend=backend)
    watcher.start()
    _wait(lambda: len(seen.of(ActivationDegraded)) == 1)
    event = seen.of(ActivationDegraded)[0]
    assert event.channel == "hotkey"  # type: ignore[attr-defined]
    assert event.message  # type: ignore[attr-defined]  # non-empty spoken text
    watcher.stop()
    assert backend.uninstall_count == 0  # never registered -> nothing to unhook


# ---------------------------------------------------------------- wake word watcher


class FakeScorer:
    """Scripted per-frame scores standing in for the openWakeWord model."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = iter(scores)
        self.frames = 0

    def predict(self, frame: np.ndarray) -> dict[str, float]:
        score = next(self._scores)  # raises StopIteration once scripted scores end
        self.frames += 1
        return {"fake": score}

    def reset(self) -> None:
        return None


def _chunk_source(n_chunks: int, chunk_samples: int = 512) -> Iterator[np.ndarray]:
    for _ in range(n_chunks):
        yield np.zeros(chunk_samples, dtype=np.int16)


def test_wake_threshold_crossing_emits_once_per_burst() -> None:
    bus = EventBus()
    seen = Collector(bus, WakeDetected)
    scores = [0.1, 0.9, 0.9, 0.1, 0.8]  # rise, plateau (refractory), drop, re-fire
    scorer = FakeScorer(scores)
    watcher = WakeWordWatcher(
        bus, model_dir=Path("models"), chunk_source=_chunk_source(200),
        scorer_factory=lambda _dir: scorer,
    )
    watcher.start()
    _wait(lambda: len(seen.of(WakeDetected)) == 2)
    watcher.stop()
    assert [e.score for e in seen.of(WakeDetected)] == [0.9, 0.8]  # type: ignore[attr-defined]
    assert scorer.frames == len(scores)


def test_wake_below_threshold_emits_nothing() -> None:
    bus = EventBus()
    seen = Collector(bus, WakeDetected)
    watcher = WakeWordWatcher(
        bus, model_dir=Path("models"), chunk_source=_chunk_source(200),
        scorer_factory=lambda _dir: FakeScorer([0.2] * 10),
    )
    watcher.start()
    watcher.stop()  # finite source drains fast; stop() joins
    _wait(lambda: not any(t.name == "wake-word" and t.is_alive()
                          for t in threading.enumerate()))
    assert seen.events == []


def test_wake_missing_model_emits_degradation() -> None:
    bus = EventBus()
    seen = Collector(bus, ActivationDegraded)
    watcher = WakeWordWatcher(
        bus, model_dir=None, chunk_source=_chunk_source(1),
        scorer_factory=FakeScorer,  # type: ignore[arg-type]
    )
    watcher.start()  # must not raise and must not start the loop
    _wait(lambda: len(seen.of(ActivationDegraded)) == 1)
    event = seen.of(ActivationDegraded)[0]
    assert event.channel == "wake_word"  # type: ignore[attr-defined]
    assert event.message  # type: ignore[attr-defined]
    watcher.stop()
    assert not any(t.name == "wake-word" and t.is_alive()
                   for t in threading.enumerate())


def test_wake_scorer_failure_emits_degradation() -> None:
    bus = EventBus()
    seen = Collector(bus, ActivationDegraded)

    def _boom(_dir: Path) -> FakeScorer:
        raise RuntimeError("model load failed")

    watcher = WakeWordWatcher(
        bus, model_dir=Path("models"), chunk_source=_chunk_source(1),
        scorer_factory=_boom,
    )
    watcher.start()
    _wait(lambda: len(seen.of(ActivationDegraded)) == 1)
    assert seen.of(ActivationDegraded)[0].channel == "wake_word"  # type: ignore[attr-defined]
    watcher.stop()


def test_wake_thread_exits_when_source_ends() -> None:
    bus = EventBus()
    seen = Collector(bus, WakeDetected)
    watcher = WakeWordWatcher(
        bus, model_dir=Path("models"), chunk_source=_chunk_source(4),
        scorer_factory=lambda _dir: FakeScorer([0.2] * 4),
    )
    watcher.start()
    watcher.stop()
    _wait(lambda: not any(t.name == "wake-word" and t.is_alive()
                          for t in threading.enumerate()))
    assert seen.events == []


# ------------------------------------------------- real model tier (dev-gated)


@pytest.mark.skipif(
    not DEV_MODELS.exists() or not (DEV_CLIPS / "alexa_test.wav").exists(),
    reason="bundled dev wake models / phrase clip not present on this machine",
)
def test_openwakeword_detects_phrase_on_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real openWakeWord scoring end to end: 512-sample mic chunks -> 80 ms frames ->
    WakeDetected at score >= 0.5 (SP3 threshold). Bundled pretrained models are
    CC BY-NC-SA — dev/test-only, gated by MARSHMELLOW_DEV_MODELS (D8 constraint)."""
    monkeypatch.setenv("MARSHMELLOW_DEV_MODELS", "1")
    pytest.importorskip("openwakeword")

    from marshmellow.audio.fixtures import FixtureMic

    bus = EventBus()
    seen = Collector(bus, WakeDetected)
    watcher = WakeWordWatcher(
        bus, model_dir=DEV_MODELS,
        chunk_source=FixtureMic([DEV_CLIPS / "alexa_test.wav"]).chunks(),
    )
    watcher.start()
    _wait(lambda: bool(seen.of(WakeDetected)), timeout=15.0)  # model load ~seconds
    watcher.stop()
    assert seen.of(ActivationDegraded) == [], "real-model tier degraded unexpectedly"
    detections = seen.of(WakeDetected)
    assert detections, "phrase clip never crossed the wake threshold"
    assert all(e.score >= 0.5 for e in detections)  # type: ignore[attr-defined]
