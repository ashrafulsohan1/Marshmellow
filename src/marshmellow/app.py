"""Composition root — wires real vs fake implementations (01-architecture §1).

The voice loop lives here too (M2 Task 8): settings drive the real-vs-fake
wiring — D7 provider choice builds the planner, D2 activation mode wires the
hotkey/wake channels — and every piece stays swappable by injection for the
headless test runs. The engine never requires the UI: headless mode is how
tests and the simulator CLI run, and headless never installs a real keyboard
hook or opens the mic.

M3 composition (§3.5–3.7, §3.13): `_build_perception` / `_build_action_layer` /
`_build_gate` assemble the real UIA stack + safety gate + AskUserChannel; each
is injectable (`perception=`, `action_layer=`, `gate=`, `ask_channel=`,
`planner=`) for headless tests. The executor seam routes: `ask_user` → the
spoken ask channel, `shell`+open_app → the DE pack, everything else →
`gate.guard(action_layer.execute)`. The planner's toolbox (planner.tools.
ScreenToolbox) wraps perception + registry + that same executor seam.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from marshmellow import __version__
from marshmellow.audio.activation import (
    HookBackend,
    HotkeyWatcher,
    WakeWordWatcher,
    WinLLHookBackend,
)
from marshmellow.audio.ask import AskUserChannel
from marshmellow.audio.capture import AudioEngine
from marshmellow.audio.loop import Transcriber, VoiceLoop
from marshmellow.core import secrets
from marshmellow.core.bus import EventBus
from marshmellow.core.config import Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ActivationDegraded,
    Element,
    PerceptionHint,
    PerceptionSnapshot,
    SpeakRequest,
    TaskEvent,
    TaskSpec,
    WindowInfo,
)
from marshmellow.core.logging import setup_logging
from marshmellow.packs.base import DEShell, ShellResult
from marshmellow.packs.windows import WindowsPack, window_title
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.fake import FakePlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.stt.fake import FakeSTT
from marshmellow.tasks.manager import Planner, TaskHandle, TaskManager
from marshmellow.tts.fake import FakeTTS
from marshmellow.ui.tray import TrayAction, confirm_dialog

if TYPE_CHECKING:
    from marshmellow.tts.piper import PiperSpeaker
    from marshmellow.ui.mascot import Mascot
    from marshmellow.ui.tray import Tray

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]


class ScreenPerceptionLike(Protocol):
    """The perception seam (ScreenPerception satisfies it)."""

    def snapshot(
        self, hint: PerceptionHint | None = None, hwnd: int | None = None
    ) -> PerceptionSnapshot: ...


class ActionLayerLike(Protocol):
    """The executor seam of the action layer (ActionLayer satisfies it)."""

    def execute(
        self, req: ActionRequest, snapshot: PerceptionSnapshot | None = None
    ) -> ActionResult: ...


class GateLike(Protocol):
    """The safety-gate seam: wrap an executor so every request is gated (§3.13)."""

    def guard(self, execute: Executor) -> Executor: ...


class AskChannelLike(Protocol):
    """The spoken ask seam (AskUserChannel satisfies it)."""

    def ask(self, question: str, cancelled: Callable[[], bool] | None = None) -> str: ...


def _default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "Marshmellow" / "logs") if base else Path.cwd() / "logs"


class _HeadlessHookBackend:
    """Activation channel stand-in for headless runs: registration always fails.

    That drives the documented degradation path (a spoken ActivationDegraded)
    instead of installing a global WH_KEYBOARD_LL hook from tests/simulator
    processes. Tests that need a working hotkey inject their own backend.
    """

    def install(self, on_key: object) -> int:
        return 0

    def run(self) -> None:
        return None

    def request_stop(self) -> None:
        return None

    def uninstall(self, handle: int) -> None:
        return None


class App:
    def __init__(
        self,
        settings: Settings,
        headless: bool = False,
        log_dir: Path | None = None,
        pack: DEShell | None = None,
        *,
        stt: Transcriber | None = None,
        audio_engine: AudioEngine | None = None,
        hotkey_backend: HookBackend | None = None,
        wake_model_dir: Path | None = None,
        perception: ScreenPerceptionLike | None = None,
        action_layer: ActionLayerLike | None = None,
        gate: GateLike | None = None,
        ask_channel: AskChannelLike | None = None,
        planner: Planner | None = None,
    ) -> None:
        self.settings = settings
        self.headless = headless
        self.bus = EventBus()
        setup_logging(log_dir or _default_log_dir())
        self.stt = stt if stt is not None else self._build_stt()
        self.tts = self._build_tts()
        self.pack: DEShell = pack if pack is not None else WindowsPack()
        self.registry = ElementRegistry()
        # The mascot is built before the ask channel so L3 questions can drop it
        # to "listening" (§3.13); the engine stays UI-free in headless mode.
        self.mascot: Mascot | None = None
        self.tray: Tray | None = None
        if not headless:
            from marshmellow.ui.mascot import Mascot
            from marshmellow.ui.tray import Tray

            self.mascot = Mascot(bus=self.bus)
            self.tray = Tray(bus=self.bus)
            self.bus.subscribe(TrayAction, self._on_tray_action)
        self.perception = perception if perception is not None else self._build_perception()
        self.action_layer = (
            action_layer if action_layer is not None else self._build_action_layer()
        )
        self.ask_channel = (
            ask_channel
            if ask_channel is not None
            else AskUserChannel(tts=self.tts, stt=self.stt, bus=self.bus, mascot=self.mascot)
        )
        self.gate = gate if gate is not None else self._build_gate()
        self._gated_execute = self.gate.guard(self._run_action_layer)
        self.toolbox = ScreenToolbox(self.perception, self.registry, self._execute)
        self.planner = planner if planner is not None else self._build_planner(settings)
        self.manager = TaskManager(
            bus=self.bus, planner=self.planner, execute=self._execute
        )
        self.audio_engine: AudioEngine | None = audio_engine
        if self.audio_engine is None and not headless:
            self.audio_engine = AudioEngine(bus=self.bus)  # real mic; headless stays silent
        self._hotkey_backend = hotkey_backend
        self._wake_model_dir = wake_model_dir
        self._build_activation(settings)
        self._quit_requested = False
        self.voice = VoiceLoop(self)
        self.bus.subscribe(ActivationDegraded, self._on_activation_degraded)
        self._wire_voice_feedback()

    def start(self) -> None:
        if self.tray is not None:
            self.tray.show()
        if self.mascot is not None:
            self.mascot.show()
        self.tts.start()
        self.voice.start()
        if self.audio_engine is not None:
            self.audio_engine.start()
        if self.hotkey_watcher is not None:
            self.hotkey_watcher.start()
        if self.wake_watcher is not None:
            self.wake_watcher.start()
        logger.info("Marshmellow %s started (headless=%s)", __version__, self.headless)

    def stop(self) -> None:
        self.voice.stop()
        if self.audio_engine is not None:
            self.audio_engine.stop()
        if self.hotkey_watcher is not None:
            self.hotkey_watcher.stop()
        if self.wake_watcher is not None:
            self.wake_watcher.stop()
        self.tts.quit()  # full teardown (piper: terminate subprocess); stop() is only barge-in
        if self.mascot is not None:
            self.mascot.close()
        self.bus.stop()

    async def submit_text(self, text: str) -> TaskHandle:
        """M1 entry point: a scripted spoken instruction (FakeSTT seam)."""
        normalized = self.stt.transcribe_text(text).text  # type: ignore[attr-defined]
        spec_id = uuid.uuid4().hex[:8]
        logger.info("instruction: %s", normalized)
        return await self.manager.submit(TaskSpec(id=spec_id, instruction=normalized))

    @property
    def quit_requested(self) -> bool:
        """Tray quit sets this; the app entry loop polls it (never joins from the bus)."""
        return self._quit_requested

    def _build_stt(self) -> Transcriber:
        """Real faster-whisper for live runs; the fake keeps headless runs silent-fast."""
        if self.headless:
            return FakeSTT()
        from marshmellow.stt.fasterwhisper import FasterWhisperSTT

        return FasterWhisperSTT(model=self.settings.stt_model)

    def _build_tts(self) -> FakeTTS | PiperSpeaker:
        """Real piper subprocess for live runs; degrade to silent when no voice exists."""
        if self.headless:
            return FakeTTS()
        from marshmellow.tts.piper import PiperSpeaker, default_voice_model

        model = default_voice_model(self.settings.tts_voice)
        if model is None:
            logger.error(
                "no piper voice %r on this machine (set MARSHMELLOW_VOICE_DIR or run "
                "`uv run python -m piper.download_voices %s`); TTS stays silent",
                self.settings.tts_voice,
                self.settings.tts_voice,
            )
            return FakeTTS()
        logger.info("TTS voice: %s (%s)", self.settings.tts_voice, model)
        return PiperSpeaker(bus=self.bus, model=model)

    def _build_perception(self) -> ScreenPerceptionLike:
        """Real UIA stack: settle-poll reader + the shared registry (§3.6).

        Vision stays unwired until a vision-capable provider is configured
        (D5): `snapshot` then degrades to a UIA-only walk, never an error.
        """
        from marshmellow.perception.snapshot import ScreenPerception
        from marshmellow.perception.uia_reader import UIAReader

        return ScreenPerception(
            reader=UIAReader(),
            registry=self.registry,
            foreground=self._foreground_window,
            vision=None,
        )

    def _build_action_layer(self) -> ActionLayerLike:
        """Real ActionLayer: UIA patterns → keyboard → SendInput (§3.7)."""
        from marshmellow.actions.executor import ActionLayer
        from marshmellow.actions.input_synthesis import InputSynthesizer
        from marshmellow.actions.uia_actions import UiaAutomationPatterns

        return ActionLayer(
            perception=self.perception,
            registry=self.registry,
            patterns=UiaAutomationPatterns(),
            inputs=InputSynthesizer(),
            focus_window=self._focus_hwnd,
        )

    def _build_gate(self) -> GateLike:
        """Real SafetyGate: authoritative classification + fail-closed confirm (§3.13)."""
        from marshmellow.safety.confirm import ConfirmFlow
        from marshmellow.safety.gate import SafetyGate
        from marshmellow.safety.policy import RiskPolicy

        confirm = ConfirmFlow(
            ask=self.ask_channel.ask,
            # the tray dialog needs a Qt app; headless runs fail closed on
            # ambiguous voice instead of popping a modal no one can see
            fallback=None if self.headless else confirm_dialog,
        )
        return SafetyGate(
            policy=RiskPolicy(
                preset=self.settings.safety.preset,
                overrides=dict(self.settings.safety.overrides),
            ),
            confirm=confirm,
            announce=self._announce,
            kill_probe=self._kill_engaged,
            resolve=self._resolve_element,
        )

    def _build_planner(self, settings: Settings) -> Planner:
        """D7: the provider setting picks the planner behind TaskManager.

        The LLM planner gets the screen toolbox (perceive/act/verify, M3);
        the keyword planner stays the fake-provider default unless a planner
        was injected.
        """
        cfg = settings.providers.llm
        if cfg.kind == "openai_compat":
            from marshmellow.llm.openai_compat import OpenAICompatProvider
            from marshmellow.planner.orchestrator import LLMPlanner

            provider = OpenAICompatProvider(
                base_url=cfg.base_url, model=cfg.model, get_key=secrets.load_api_key
            )
            logger.info("LLM provider: openai_compat (model=%s)", cfg.model)
            return LLMPlanner(provider, toolbox=self.toolbox)
        if cfg.kind != "fake":
            logger.warning(
                "LLM provider kind %r is not implemented yet; using the keyword planner",
                cfg.kind,
            )
        return FakePlanner()

    # -- executor seams ---------------------------------------------------------

    def _foreground_window(self) -> WindowInfo | None:
        """The current foreground window for perception (None when none)."""
        try:
            import ctypes

            hwnd = int(ctypes.windll.user32.GetForegroundWindow())  # type: ignore[attr-defined]
        except Exception:  # off-Windows / no session — perception degrades
            return None
        if not hwnd:
            return None
        return WindowInfo(hwnd=hwnd & 0xFFFFFFFF, title=window_title(hwnd), app="")

    def _focus_hwnd(self, hwnd: int) -> bool:
        """ActionLayer seam: bring one window to the foreground before hotkeys/scroll.

        SetForegroundWindow is best-effort — Windows restricts focus stealing
        for background processes, and a failure is logged, never fatal.
        """
        try:
            import ctypes

            return bool(ctypes.windll.user32.SetForegroundWindow(hwnd))  # type: ignore[attr-defined]
        except Exception:
            logger.warning("could not focus window %#x", hwnd, exc_info=True)
            return False

    def _run_action_layer(self, req: ActionRequest) -> ActionResult:
        return self.action_layer.execute(req)

    def _announce(self, text: str) -> None:
        """L1 announcement: ack-priority TTS before the action runs (§3.13)."""
        self.tts.say(SpeakRequest(text=text, priority="ack"))

    def _kill_engaged(self) -> bool:
        """The gate/ask kill probe: the ACTIVE turn's kill event (§3.13)."""
        kill = self.manager.current_kill()
        return kill is not None and kill.is_set()

    def _resolve_element(self, ref: str | None) -> Element | None:
        """Gate seam: ref → the element the planner saw (password escalation)."""
        if ref is None:
            return None
        handle = self.registry.resolve(ref)
        return handle.element if handle is not None else None

    def _execute(self, req: ActionRequest) -> ActionResult:
        """The executor seam (§3.12): route one action to its handler.

        `ask_user` → the spoken ask channel; `shell`+open_app → the DE pack;
        everything else → the safety gate in front of the action layer (§3.13).
        """
        if req.kind == "ask_user":
            question = req.payload.get("question")
            text = question.strip() if isinstance(question, str) else ""
            if not text:
                return ActionResult(
                    ok=False,
                    observation="ask_user needs a 'question' payload",
                    error="missing_question",
                )
            answer = self.ask_channel.ask(text, cancelled=self._kill_engaged)
            return ActionResult(
                ok=True, observation=answer if answer.strip() else "no answer (silence or timeout)"
            )
        if req.kind == "shell" and isinstance(req.payload.get("open_app"), str):
            result: ShellResult = self.pack.open_app(req.payload["open_app"])
            return ActionResult(ok=result.ok, observation=result.detail)
        return self._gated_execute(req)

    def _build_activation(self, settings: Settings) -> None:
        """D2: the activation setting decides which channels get wired."""
        mode = settings.activation
        self.hotkey_watcher: HotkeyWatcher | None = None
        self.wake_watcher: WakeWordWatcher | None = None
        if mode in ("hotkey", "both"):
            backend = self._hotkey_backend
            if backend is None:
                backend = (
                    WinLLHookBackend() if not self.headless else _HeadlessHookBackend()
                )
            self.hotkey_watcher = HotkeyWatcher(
                self.bus, combo=settings.hotkey, mode="hold", backend=backend
            )
        if mode in ("wake_word", "both"):
            # No self-trained wake model yet (D8): model_dir=None degrades to a
            # spoken hint and the hotkey stays the trusted channel. The chunk
            # source arrives with the model (mic sharing is wired there).
            self.wake_watcher = WakeWordWatcher(
                self.bus, model_dir=self._wake_model_dir, chunk_source=iter(())
            )

    def _on_activation_degraded(self, event: ActivationDegraded) -> None:
        self.tts.say(SpeakRequest(text=event.message, priority="background"))

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
