"""Composition root — wires real vs fake implementations (01-architecture §1).

M1 ships with FakeSTT/FakeTTS/FakePlanner and the real WindowsPack; each fake
swaps for its real module in M2/M3 without changing this file's shape.
The engine never requires the UI: headless mode is how tests and the
simulator CLI run.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from marshmellow import __version__
from marshmellow.core.bus import EventBus
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    SpeakRequest,
    TaskEvent,
    TaskSpec,
)
from marshmellow.core.logging import setup_logging
from marshmellow.packs.base import DEShell, ShellResult
from marshmellow.packs.windows import WindowsPack
from marshmellow.planner.fake import FakePlanner
from marshmellow.stt.fake import FakeSTT
from marshmellow.tasks.manager import TaskHandle, TaskManager
from marshmellow.tts.fake import FakeTTS
from marshmellow.ui.tray import TrayAction

if TYPE_CHECKING:
    from marshmellow.ui.mascot import Mascot
    from marshmellow.ui.tray import Tray

logger = logging.getLogger(__name__)


def _default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "Marshmellow" / "logs") if base else Path.cwd() / "logs"


class App:
    def __init__(
        self,
        settings: Settings,
        headless: bool = False,
        log_dir: Path | None = None,
        pack: DEShell | None = None,
    ) -> None:
        self.settings = settings
        self.headless = headless
        self.bus = EventBus()
        setup_logging(log_dir or _default_log_dir())
        self.stt = FakeSTT()
        self.tts = FakeTTS()
        self.pack: DEShell = pack if pack is not None else WindowsPack()
        self.planner = FakePlanner()
        self.manager = TaskManager(
            bus=self.bus, planner=self.planner, execute=self._execute
        )
        self.mascot: Mascot | None = None
        self.tray: Tray | None = None
        self._quit_requested = False
        self._wire_voice_feedback()
        if not headless:
            from marshmellow.ui.mascot import Mascot
            from marshmellow.ui.tray import Tray

            self.mascot = Mascot(bus=self.bus)
            self.tray = Tray(bus=self.bus)
            self.bus.subscribe(TrayAction, self._on_tray_action)

    def start(self) -> None:
        if self.tray is not None:
            self.tray.show()
        if self.mascot is not None:
            self.mascot.show()
        logger.info("Marshmellow %s started (headless=%s)", __version__, self.headless)

    def stop(self) -> None:
        if self.mascot is not None:
            self.mascot.close()
        self.bus.stop()

    async def submit_text(self, text: str) -> TaskHandle:
        """M1 entry point: a scripted spoken instruction (FakeSTT seam)."""
        normalized = self.stt.transcribe_text(text).text
        spec_id = uuid.uuid4().hex[:8]
        logger.info("instruction: %s", normalized)
        return await self.manager.submit(TaskSpec(id=spec_id, instruction=normalized))

    def _execute(self, req: ActionRequest) -> ActionResult:
        if req.kind == "shell" and isinstance(req.payload.get("open_app"), str):
            result: ShellResult = self.pack.open_app(req.payload["open_app"])
            return ActionResult(ok=result.ok, observation=result.detail)
        return ActionResult(ok=False, observation=f"unsupported action: {req.kind}")

    def _wire_voice_feedback(self) -> None:
        def on_task_event(event: TaskEvent) -> None:
            if event.type == "started":
                self.tts.say(SpeakRequest(text="On it.", priority="ack"))
                if self.mascot is not None:
                    self.mascot.set_state("acting")
            elif event.type in ("result", "failed"):
                self.tts.say(SpeakRequest(text=event.payload, priority="result"))
                if self.mascot is not None:
                    self.mascot.set_state("speaking" if event.type == "result" else "error")

        self.bus.subscribe(TaskEvent, on_task_event)

    def _on_tray_action(self, action: TrayAction) -> None:
        if action.kind == "quit":
            self._quit_requested = True  # the app loop polls this; never join from the bus thread
        logger.info("tray action: %s", action.kind)
