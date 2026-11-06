"""M4 Task 4 integration — the LLM creates a monitor; it reports by voice.

Headless App over the scripted ``ScenarioProvider`` (zero real provider calls):
"watch this channel" → the model calls ``create_task`` → the monitor is
registered and visible via ``list_tasks()`` → the real file_hash check changes
on tick 2 (the test rewrites the watched file) → FakeTTS receives
``SpeakRequest(priority="background")`` whose text is the monitor summary → the
task completes. A structurally bad payload is a refusal observation
("invalid create_task payload: …") and registers NOTHING.

The monitor waits use the M4 seam set (FakeClock + recording sleeper,
``advance_ratio=0`` — the test drives the clock between checks), so no
wall-clock waits and no real network: ``file_hash`` is local-only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    LLMToolCall,
    PerceptionSnapshot,
    TaskEvent,
)
from marshmellow.packs.base import ShellResult
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.orchestrator import LLMPlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.tasks.manager import TaskManager
from marshmellow.testing.scenario import ScenarioProvider

NOW = datetime(2026, 11, 6, 12, 0, 0)

INSTRUCTION = "watch this channel and tell me when they upload"


# -- seams (the test_manager.py pattern; tests/ is not a package) ---------------


class FakeClock:
    def __init__(self) -> None:
        self._now = NOW

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingSleeper:
    """Instant sleeper seam: only the test's advance() moves the clock, so
    nothing becomes due on its own (advance_ratio=0)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []
        self.advance_ratio = 0.0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds * self.advance_ratio)


class RecordingPack:
    """WindowsPack double — the create_task scripts never open anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_app(self, target: str) -> ShellResult:
        self.calls.append(target)
        return ShellResult(ok=True, detail=f"would open {target}")


class _IdlePerception:
    """The create_task scripts never read the screen; a call fails loudly."""

    def snapshot(
        self, hint: object = None, hwnd: int | None = None
    ) -> PerceptionSnapshot:
        raise AssertionError("the create_task script must not read the screen")


def _refuse_acts(req: ActionRequest) -> ActionResult:
    raise AssertionError("the create_task script must not act on the screen")


def _create_task_app(
    tmp_path: Path, provider: ScenarioProvider, clock: FakeClock, sleeper: RecordingSleeper
) -> App:
    """Headless App on the scripted provider; the manager swapped for the
    seamed one (the voice-feedback closure reads ``self.manager`` dynamically,
    so background reports still flow through it)."""
    toolbox = ScreenToolbox(_IdlePerception(), ElementRegistry(), _refuse_acts)
    planner = LLMPlanner(provider, toolbox=toolbox)
    app = App(
        Settings(),
        headless=True,
        log_dir=tmp_path,
        pack=RecordingPack(),
        planner=planner,
    )
    app.manager = TaskManager(
        bus=app.bus,
        planner=app.planner,
        execute=app._execute,
        clock=clock,
        rng=lambda: 0.0,
        sleeper=sleeper,
        lease=app.lease,
    )
    return app


async def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("condition was not reached in time")
        await asyncio.sleep(0.01)


# -- the happy path: instruction → create_task → monitor → spoken report --------


async def test_instruction_creates_monitor_that_reports_by_voice(tmp_path: Path) -> None:
    watched = tmp_path / "feed.xml"
    watched.write_bytes(b"v1")
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    provider = ScenarioProvider(
        [
            LLMToolCall(
                name="create_task",
                arguments={
                    "instruction": INSTRUCTION,
                    "mode": "background_monitor",
                    "monitor": {
                        "kind": "file_hash",
                        "target": str(watched),
                        "interval_s": 60,
                        "condition": "the file changes",
                    },
                },
            ),
            LLMToolCall(
                name="finish",
                arguments={"ok": True, "summary_to_speak": "Watching it — I'll report."},
            ),
        ]
    )
    app = _create_task_app(tmp_path, provider, clock, sleeper)
    events: list[TaskEvent] = []
    app.bus.subscribe(TaskEvent, events.append)
    try:
        handle = await app.submit_text(INSTRUCTION)

        assert handle.result is not None and handle.result.ok, str(handle.result)
        assert provider.emitted[0].name == "create_task"  # the model chose the new tool

        # the monitor is registered, visible, and waiting for its next check
        await _until(lambda: bool(app.manager.list_tasks("waiting")))
        registered = app.manager.list_tasks("waiting")
        assert len(registered) == 1
        monitor = registered[0]
        assert monitor is not handle  # a separate long-lived task, not the turn
        assert monitor.spec.instruction == INSTRUCTION
        assert monitor.spec.created_by == "llm"
        assert monitor.spec.monitor is not None and monitor.spec.monitor.kind == "file_hash"
        assert app.manager.task_mode(monitor.spec.id) == "background_monitor"

        # tick 2: the watched file changes → the one-shot monitor completes
        watched.write_bytes(b"v2")
        clock.advance(3600)
        await _until(lambda: monitor.state == "completed")

        assert monitor.result is not None and monitor.result.ok
        assert monitor.result.summary_to_speak == "the watched file changed"
        # monitors never emit step (no executor actions) — trace is started, result
        assert [e.type for e in events if e.task_id == monitor.spec.id] == [
            "started",
            "result",
        ]

        # the report is spoken UNPROMPTED at background priority, verbatim summary
        background = [
            s
            for s in app.tts.spoken
            if s.priority == "background" and s.text == "the watched file changed"
        ]
        assert background, f"no background report spoken: {app.tts.spoken}"
        # the creating turn's own result keeps the foreground priority
        foreground = [
            s
            for s in app.tts.spoken
            if s.priority == "result" and "i'll report" in s.text.lower()
        ]
        assert foreground, f"foreground result priority changed: {app.tts.spoken}"
    finally:
        app.stop()


# -- invalid payloads: refusal observation, nothing registered -------------------


async def _refused(tmp_path: Path, monitor_args: dict[str, object] | None) -> str:
    """Run one create_task script with a bad payload; returns the fed-back text."""
    arguments: dict[str, object] = {"instruction": "watch this file too closely",
                                    "mode": "background_monitor"}
    if monitor_args is not None:
        arguments["monitor"] = monitor_args
    provider = ScenarioProvider(
        [
            LLMToolCall(name="create_task", arguments=arguments),
            LLMToolCall(
                name="finish",
                arguments={
                    "ok": False,
                    "summary_to_speak": "I couldn't set that up.",
                },
            ),
        ]
    )
    clock = FakeClock()
    app = _create_task_app(tmp_path, provider, clock, RecordingSleeper(clock))
    try:
        handle = await app.submit_text("watch this file")

        assert handle.result is not None and not handle.result.ok
        assert app.manager.list_tasks() == [], "a refused payload must not register"
        return provider.planner_requests[1].messages[-1].content
    finally:
        app.stop()


async def test_interval_below_the_floor_is_refused(tmp_path: Path) -> None:
    watched = tmp_path / "feed.xml"
    watched.write_bytes(b"v1")

    fed_back = await _refused(
        tmp_path,
        {
            "kind": "file_hash",
            "target": str(watched),
            "interval_s": 5,  # below the 30 s LLM floor — no hot loops from the model
            "condition": "changes",
        },
    )

    assert "ERROR" in fed_back
    assert "invalid create_task payload" in fed_back
    assert "interval" in fed_back


async def test_missing_monitor_is_refused(tmp_path: Path) -> None:
    fed_back = await _refused(tmp_path, None)  # background_monitor without a monitor

    assert "ERROR" in fed_back
    assert "invalid create_task payload" in fed_back
    assert "monitor" in fed_back
