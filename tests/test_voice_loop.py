"""Voice loop integration (M2 Task 8) — barge-in, kill switch, echo guard.

All headless: segments, wake and speaking events go straight onto the bus
(the FakeSTT fixture encoding carries the UTF-8 text in `pcm`); fake mascot /
audio-engine / hotkey-hook doubles make mascot, mute and activation effects
assertable without Qt or a real mic. SpeakingStarted/Stopped emulate what the
real PiperSpeaker emits around playback.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from marshmellow.app import App
from marshmellow.audio.activation import HookBackend
from marshmellow.audio.loop import VoiceLoop
from marshmellow.core import secrets
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    SpeakingStarted,
    SpeakingStopped,
    SpeechSegment,
    TaskEvent,
    Transcript,
    WakeDetected,
)
from marshmellow.packs.base import ShellResult
from marshmellow.planner.fake import FakePlanner
from marshmellow.planner.orchestrator import LLMPlanner

# -- doubles -------------------------------------------------------------------


class RecordingPack:
    """Stands in for WindowsPack: records open_app calls, opens nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class BlockingPack:
    """Records the call, then blocks — a task that is verifiably in flight."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = threading.Event()

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        self.release.wait(timeout=10.0)
        return ShellResult(ok=True, detail=f"opened {target}")


class FakeMascot:
    def __init__(self) -> None:
        self.state: str = "idle"

    def show(self) -> None:
        return None

    def set_state(self, state: str) -> None:
        self.state = state

    def close(self) -> None:
        return None


class FakeAudioEngine:
    def __init__(self) -> None:
        self.muted: bool | None = None
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_muted(self, muted: bool) -> None:
        self.muted = muted


class SlowSTT:
    """Transcriber that sleeps on the WORKER thread — the loop must not care."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.started_count = 0
        self.finished_count = 0

    def transcribe(self, segment: SpeechSegment) -> Transcript:
        self.started_count += 1
        time.sleep(self.delay)
        self.finished_count += 1
        text = segment.pcm.decode("utf-8", errors="ignore")
        return Transcript(text=text, source="fixture")


class ScriptedHotkeyBackend:
    """HookBackend double: install succeeds; tests inject key events directly."""

    def __init__(self) -> None:
        self.on_key: Callable[[int, bool], None] | None = None
        self._stop = threading.Event()

    def install(self, on_key: Callable[[int, bool], None]) -> int:
        self.on_key = on_key
        return 1

    def run(self) -> None:
        self._stop.wait(timeout=5.0)

    def request_stop(self) -> None:
        self._stop.set()

    def uninstall(self, handle: int) -> None:
        return None


# -- harness --------------------------------------------------------------------


def _segment(text: str | bytes) -> SpeechSegment:
    """FakeSTT fixture encoding: the UTF-8 text rides in `pcm`."""
    now = datetime.now()
    pcm = text if isinstance(text, bytes) else text.encode("utf-8")
    return SpeechSegment(pcm=pcm, started_at=now, ended_at=now)


def _app(
    tmp_path: Path,
    *,
    pack: object | None = None,
    audio_engine: FakeAudioEngine | None = None,
    hotkey_backend: HookBackend | None = None,
    settings: Settings | None = None,
    stt: SlowSTT | None = None,
) -> App:
    app = App(
        settings=settings if settings is not None else Settings(),
        headless=True,
        log_dir=tmp_path,
        pack=pack if pack is not None else RecordingPack(),
        audio_engine=audio_engine,  # type: ignore[arg-type]
        hotkey_backend=hotkey_backend,
        stt=stt,  # type: ignore[arg-type]
    )
    app.mascot = FakeMascot()  # headless has no Qt mascot; a double makes states assertable
    return app


def _task_events(app: App) -> list[TaskEvent]:
    events: list[TaskEvent] = []
    app.bus.subscribe(TaskEvent, events.append)
    return events


def _trace(events: list[TaskEvent]) -> dict[str, list[str]]:
    trace: dict[str, list[str]] = {}
    for event in events:
        trace.setdefault(event.task_id, []).append(event.type)
    return trace


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not met before timeout")


# -- the loop: SpeechSegment → spoken result -----------------------------------


@pytest.mark.scenario
async def test_voice_loop_is_the_app_loop(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert isinstance(app.voice, VoiceLoop)
    app.stop()


@pytest.mark.scenario
async def test_stop_shuts_down_the_loop_thread(tmp_path: Path) -> None:
    """Regression: stop() must actually stop the resident loop (no 2s stall, no leak)."""
    app = _app(tmp_path)
    app.start()
    loop = app.voice._loop
    thread = app.voice._thread
    assert loop is not None and thread is not None

    began = time.monotonic()
    app.stop()
    elapsed = time.monotonic() - began

    assert not thread.is_alive(), "the resident loop thread must not survive stop()"
    assert loop.is_closed(), "the loop must be closed after stop()"
    assert elapsed < 1.0, f"stop() must not stall on the join timeout (took {elapsed:.2f}s)"


@pytest.mark.scenario
async def test_speech_segment_flows_to_spoken_result(tmp_path: Path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack=pack)
    events = _task_events(app)
    app.start()
    try:
        app.bus.emit(_segment("open notepad"))

        await _wait_for(lambda: any(e.type == "result" for e in events))

        assert pack.calls == ["notepad"]
        spoken = [s.text for s in app.tts.spoken]
        assert any(t.strip().lower() == "on it." for t in spoken), "ack must be spoken"
        assert any("notepad" in t.lower() for t in spoken), "result must be spoken"
        assert app.mascot is not None and app.mascot.state == "speaking"
    finally:
        app.stop()


@pytest.mark.scenario
async def test_unrecognizable_audio_gets_the_repeat_prompt(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events = _task_events(app)
    app.start()
    try:
        app.bus.emit(_segment(b"\xff\xfe not utf-8"))  # FakeSTT yields an empty transcript

        await _wait_for(
            lambda: any(s.text == "Sorry, I didn't catch that." for s in app.tts.spoken)
        )
        assert events == [], "an empty transcript must never spawn a task"
    finally:
        app.stop()


# -- kill switch -----------------------------------------------------------------


@pytest.mark.scenario
async def test_stop_everything_cancels_running_task(tmp_path: Path) -> None:
    pack = BlockingPack()
    app = _app(tmp_path, pack=pack)
    events = _task_events(app)
    app.start()
    try:
        app.bus.emit(_segment("open notepad"))
        await _wait_for(lambda: pack.calls == ["notepad"])  # task is mid-flight

        app.bus.emit(_segment("stop everything"))
        await _wait_for(lambda: any(e.type == "cancelled" for e in events))

        assert app.tts.stop_calls >= 1, "kill switch must stop TTS"
        assert app.mascot is not None and app.mascot.state == "idle"
        killed = [seq for seq in _trace(events).values() if "cancelled" in seq]
        assert killed and killed[0][-1] == "cancelled"
        assert all("result" not in seq and "failed" not in seq for seq in killed), (
            "a killed task must never report a result"
        )

        pack.release.set()  # unblock the abandoned worker thread
        app.bus.emit(_segment("open calc"))
        await _wait_for(
            lambda: any(e.type == "result" and "calc" in e.payload.lower() for e in events)
        )
        assert pack.calls == ["notepad", "calc"], "manager state survives the kill"
    finally:
        pack.release.set()
        app.stop()


# -- barge-in --------------------------------------------------------------------


@pytest.mark.scenario
async def test_speech_during_speaking_barges_in_and_next_segment_processes(
    tmp_path: Path,
) -> None:
    pack = BlockingPack()
    app = _app(tmp_path, pack=pack)
    events = _task_events(app)
    app.start()
    try:
        app.bus.emit(_segment("open notepad"))
        await _wait_for(lambda: pack.calls == ["notepad"])  # turn in flight

        app.bus.emit(SpeakingStarted())  # the "On it." ack is playing
        app.bus.emit(_segment("open calc"))  # user talks over the agent
        await _wait_for(lambda: any(e.type == "cancelled" for e in events))

        assert app.tts.stop_calls >= 1, "barge-in must stop TTS playback"
        pack.release.set()
        await _wait_for(
            lambda: any(e.type == "result" and "calc" in e.payload.lower() for e in events)
        )
        assert pack.calls == ["notepad", "calc"], "the barge-in segment still processes"
        killed = [seq for seq in _trace(events).values() if "cancelled" in seq]
        assert killed and killed[0][-1] == "cancelled"

        app.bus.emit(SpeakingStopped())  # playback over (what PiperSpeaker emits)
        app.bus.emit(_segment("open explorer"))
        await _wait_for(lambda: pack.calls == ["notepad", "calc", "explorer"])
    finally:
        pack.release.set()
        app.stop()


@pytest.mark.scenario
async def test_loop_stays_responsive_during_slow_transcribe(tmp_path: Path) -> None:
    """Review Focus 2: STT runs on a worker thread; the loop never waits on it."""
    stt = SlowSTT(delay=1.0)
    app = _app(tmp_path, stt=stt)
    app.start()
    try:
        app.bus.emit(SpeakingStarted())
        app.bus.emit(_segment("first"))
        await _wait_for(lambda: stt.started_count == 1)

        began = time.monotonic()
        app.bus.emit(_segment("second"))  # barge-in while the first still transcribes
        await _wait_for(lambda: app.tts.stop_calls >= 1)
        elapsed = time.monotonic() - began

        assert elapsed < stt.delay, "barge-in must not wait for the in-flight transcribe"
        assert stt.finished_count == 0, "the first transcribe was still running"
    finally:
        app.stop()


# -- echo guard ------------------------------------------------------------------


@pytest.mark.scenario
async def test_wake_during_tts_is_suppressed(tmp_path: Path) -> None:
    """Review Focus 1: the agent must not hear itself wake (infinite self-talk)."""
    app = _app(tmp_path)
    mascot = app.mascot
    assert mascot is not None
    app.start()
    try:
        app.bus.emit(SpeakingStarted())
        app.bus.emit(WakeDetected(score=0.9))
        assert mascot.state != "listening", "wake during TTS playback is the echo loop"

        app.bus.emit(SpeakingStopped())
        app.bus.emit(WakeDetected(score=0.9))
        assert mascot.state == "listening", "wake works again after playback"
    finally:
        app.stop()


# -- control intents -------------------------------------------------------------


@pytest.mark.scenario
async def test_mute_unmute_drive_the_audio_engine(tmp_path: Path) -> None:
    engine = FakeAudioEngine()
    app = _app(tmp_path, audio_engine=engine)
    assert app.mascot is not None
    app.start()
    try:
        app.bus.emit(_segment("mute"))
        await _wait_for(lambda: engine.muted is True)
        assert app.mascot.state == "muted"

        app.bus.emit(_segment("unmute"))
        await _wait_for(lambda: engine.muted is False)
        assert app.mascot.state == "listening"
    finally:
        app.stop()


@pytest.mark.scenario
async def test_stop_intent_stops_tts_without_spawning_a_task(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events = _task_events(app)
    app.start()
    try:
        app.bus.emit(_segment("stop"))
        await _wait_for(lambda: app.tts.stop_calls >= 1)
        assert events == [], '"stop" is handled locally, never via the LLM'
    finally:
        app.stop()


@pytest.mark.scenario
async def test_sleep_gates_instructions_until_wake(tmp_path: Path) -> None:
    pack = RecordingPack()
    app = _app(tmp_path, pack=pack)
    assert app.mascot is not None
    app.start()
    try:
        app.bus.emit(_segment("go to sleep"))
        await _wait_for(lambda: app.mascot.state == "idle")

        app.bus.emit(_segment("open notepad"))  # asleep: dropped
        await asyncio.sleep(0.1)
        assert pack.calls == []

        app.bus.emit(_segment("wake up"))
        await _wait_for(lambda: app.mascot.state == "listening")
        app.bus.emit(_segment("open notepad"))
        await _wait_for(lambda: pack.calls == ["notepad"])
    finally:
        app.stop()


# -- settings-driven wiring (D2 activation, D7 provider) -------------------------


@pytest.mark.parametrize(
    ("mode", "hotkey_expected", "wake_expected"),
    [("hotkey", True, False), ("wake_word", False, True), ("both", True, True)],
)
def test_activation_setting_wires_channels(
    tmp_path: Path, mode: str, hotkey_expected: bool, wake_expected: bool
) -> None:
    app = _app(tmp_path, settings=Settings(activation=mode))  # type: ignore[arg-type]
    assert (app.hotkey_watcher is not None) is hotkey_expected
    assert (app.wake_watcher is not None) is wake_expected
    app.stop()


@pytest.mark.scenario
async def test_hotkey_press_activates_listening(tmp_path: Path) -> None:
    backend = ScriptedHotkeyBackend()
    app = _app(tmp_path, hotkey_backend=backend)
    assert app.mascot is not None
    app.start()
    try:
        await _wait_for(lambda: backend.on_key is not None)  # the hook installed
        assert backend.on_key is not None
        backend.on_key(0x11, True)  # ctrl down
        backend.on_key(0x10, True)  # shift down
        backend.on_key(0x4D, True)  # m down → combo complete
        await _wait_for(lambda: app.mascot.state == "listening")
    finally:
        app.stop()


def test_default_provider_is_the_fake(tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert isinstance(app.planner, FakePlanner)
    app.stop()


def test_openai_compat_provider_setting_builds_llm_planner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(secrets, "load_api_key", lambda: "test-key")
    settings = Settings()
    settings.providers.llm.kind = "openai_compat"
    settings.providers.llm.base_url = "https://llm.example.com/v1"
    settings.providers.llm.model = "test-model"

    app = _app(tmp_path, settings=settings)

    assert isinstance(app.planner, LLMPlanner)
    app.stop()
