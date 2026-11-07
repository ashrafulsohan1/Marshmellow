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
M4: the TaskManager carries the D6 foreground lease (`self.lease`, same
fail-closed ConfirmFlow as the gate), so background modes must win spoken
consent before any non-read action reaches the executor.
M5: `_build_providers` builds the D7 LLM provider and the D5 vision provider
(a dedicated instance when the planner provider's caps claim vision) BEFORE
perception — `_build_perception` wires the SoM `VisionPerceiver` from it, or
runs UIA-only.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, Protocol, cast

from pydantic import ValidationError

from marshmellow import __version__
from marshmellow.audio.activation import (
    ChunkQueue,
    HookBackend,
    HotkeyWatcher,
    WakeWordWatcher,
    WinLLHookBackend,
    wake_threshold_from_calibration,
)
from marshmellow.audio.ask import AskUserChannel
from marshmellow.audio.capture import AudioEngine
from marshmellow.audio.loop import Transcriber, VoiceLoop
from marshmellow.core import secrets
from marshmellow.core.assets import default_wake_model_dir, default_wake_model_path
from marshmellow.core.bus import EventBus
from marshmellow.core.config import LLMProviderConfig, Settings
from marshmellow.core.contracts import (
    ActionRequest,
    ActionResult,
    ActivationDegraded,
    Element,
    MonitorSpec,
    PerceptionHint,
    PerceptionSnapshot,
    SpeakRequest,
    TaskEvent,
    TaskMode,
    TaskSpec,
    TaskState,
    WindowInfo,
)
from marshmellow.core.logging import setup_logging
from marshmellow.llm.base import LLMProvider
from marshmellow.llm.resilience import ProviderChain
from marshmellow.packs.base import DEShell, ShellResult
from marshmellow.packs.windows import WindowsPack, window_title
from marshmellow.perception.registry import ElementRegistry
from marshmellow.planner.fake import FakePlanner
from marshmellow.planner.tools import ScreenToolbox
from marshmellow.stt.fake import FakeSTT
from marshmellow.tasks.manager import BACKGROUND_MODES, Planner, TaskHandle, TaskManager
from marshmellow.tasks.store import TaskStore, default_task_store_path
from marshmellow.tts.fake import FakeTTS
from marshmellow.ui.tray import GuiMarshaledConfirm, TrayAction

if TYPE_CHECKING:
    from marshmellow.safety.confirm import ConfirmFlow
    from marshmellow.tasks.foreground import ForegroundLease
    from marshmellow.tts.piper import PiperSpeaker
    from marshmellow.ui.main_window import GuiMarshal, TasksDashboard
    from marshmellow.ui.mascot import Mascot
    from marshmellow.ui.tray import Tray

logger = logging.getLogger(__name__)

Executor = Callable[[ActionRequest], ActionResult]

#: M4 pinned convention: LLM-created monitors poll at most every 30 s — no hot
#: loops from the model (``MonitorSpec`` itself stays gt=0).
MONITOR_MIN_INTERVAL_S: Final = 30


def _invalid_create_task(reason: str) -> ActionResult:
    """The create_task refusal: an observation the model sees and can fix."""
    return ActionResult(
        ok=False,
        observation=f"invalid create_task payload: {reason}",
        error="invalid_create_task",
    )


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


class _Providers(NamedTuple):
    """The LLM providers built once in `__init__` (§3.4, D4/D5).

    `planner` is the LLM behind the planner — None for `kind="fake"` (and for
    an ollama config without a model), which falls back to FakePlanner.
    `vision` is the D5 SoM fallback provider — a DEDICATED instance (own HTTP
    client, same model config) whenever the planner provider's caps claim
    vision, never the planner instance itself: the planner loop and the
    vision-provider loop are two resident loops and HTTP pools are loop-bound
    (M5 T1 review fix).
    """

    planner: LLMProvider | None
    vision: LLMProvider | None


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
        task_store_path: Path | None = None,
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
        # The tasks dashboard (§3.11, M4 T6) is built LAZILY on the GUI thread
        # the first time the tray's open_ui arrives — never headless.
        self._dashboard: TasksDashboard | None = None
        self._open_ui_marshal: GuiMarshal | None = None
        if not headless:
            from marshmellow.ui.main_window import GuiMarshal
            from marshmellow.ui.mascot import Mascot
            from marshmellow.ui.tray import Tray

            self.mascot = Mascot(bus=self.bus)
            self.tray = Tray(bus=self.bus)
            # The bus dispatches this handler on its drain thread; GuiMarshal
            # hops the request to the GUI thread before any widget is touched.
            self._open_ui_marshal = GuiMarshal(self._open_dashboard)
            self.bus.subscribe(TrayAction, self._on_tray_action)
        # The providers come first: perception needs the D5 vision provider,
        # and the planner is built from _Providers.planner below (§3.4, M5 T1).
        self._providers = self._build_providers()
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
        # Release-audit S1 (§3.13): the LLM-driven shell/launch path goes
        # through the same gate as every other action — no executor bypass.
        self._gated_shell = self.gate.guard(self._run_shell)
        self.toolbox = ScreenToolbox(self.perception, self.registry, self._execute)
        self.planner = planner if planner is not None else self._build_planner(settings)
        self.lease = self._build_lease()  # D6: consented screen access (§3.12)
        # M4 T5 (§3.15): background tasks + baselines persist across restart.
        self.task_store_path = (
            task_store_path if task_store_path is not None else default_task_store_path()
        )
        self.task_store = TaskStore(self.task_store_path)
        self.manager = TaskManager(
            bus=self.bus,
            planner=self.planner,
            execute=self._execute,
            lease=self.lease,
            store=self.task_store,
        )
        self._hotkey_backend = hotkey_backend
        self._wake_model_dir = wake_model_dir
        self._build_activation(settings)  # creates self.wake_tap when the wake channel is on
        self.audio_engine: AudioEngine | None = audio_engine
        if self.audio_engine is None and not headless:
            # real mic; headless stays silent. The D8 tap (when the wake channel
            # is on) bridges every unmuted chunk to the wake watcher's queue.
            self.audio_engine = AudioEngine(bus=self.bus, chunk_tap=self.wake_tap)
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
        self.manager.start()  # resident loop for background tasks (§3.12, M4)
        self.manager.restore()  # baselines travel with tasks across restart (§3.15, M4 T5)
        logger.info("Marshmellow %s started (headless=%s)", __version__, self.headless)

    def stop(self) -> None:
        self.voice.stop()
        if self.audio_engine is not None:
            self.audio_engine.stop()
        if self.hotkey_watcher is not None:
            self.hotkey_watcher.stop()
        if self.wake_tap is not None:
            self.wake_tap.close()  # ends the watcher's chunk_source for a clean join
        if self.wake_watcher is not None:
            self.wake_watcher.stop()
        self.tts.quit()  # full teardown (piper: terminate subprocess); stop() is only barge-in
        if self._dashboard is not None:  # runs on the GUI thread (entry-point poller)
            self._dashboard.close()
        if self.mascot is not None:
            self.mascot.close()
        self.manager.stop()  # before bus.stop(): loop teardown events still dispatch
        self.bus.stop()

    async def submit_text(self, text: str) -> TaskHandle:
        """M1 entry point: a scripted spoken instruction (FakeSTT seam)."""
        normalized = self.stt.transcribe_text(text).text  # type: ignore[attr-defined]
        spec_id = uuid.uuid4().hex[:8]
        logger.info("instruction: %s", normalized)
        return await self.manager.submit(TaskSpec(id=spec_id, instruction=normalized))

    @property
    def quit_requested(self) -> bool:
        """Tray quit sets this; the app entry loop polls it (never joins from the bus thread)."""
        return self._quit_requested

    @property
    def dashboard(self) -> TasksDashboard | None:
        """The tasks dashboard once summoned (None until the first open_ui)."""
        return self._dashboard

    # -- TaskDirectory seam (§3.11 dashboard; the manager's view, verbatim) ------

    def list_tasks(self, state: TaskState | None = None) -> list[TaskHandle]:
        return self.manager.list_tasks(state)

    def control(
        self, task_id: str, cmd: Literal["pause", "resume", "cancel", "run_now"]
    ) -> bool:
        return self.manager.control(task_id, cmd)

    def _open_dashboard(self) -> None:
        """GUI thread only (via GuiMarshal): lazily build + summon the dashboard.

        The dashboard is created here — never on a bus thread — and stays the
        ONE main window for the app's lifetime: dismissed = hidden, re-summoned
        with its trace history intact (§3.11 "summoned, not resident")."""
        if self._dashboard is None:
            from marshmellow.ui.main_window import TasksDashboard

            self._dashboard = TasksDashboard(
                bus=self.bus, tasks=self, open_settings=self._open_settings
            )
        self._dashboard.show_dashboard()

    def _open_settings(self) -> None:
        """GUI thread only (the dashboard's "Settings…" button): the modal
        settings dialog.

        Lazy-built per summons; Save persists to ``product_config_path()`` and
        the changes apply on the NEXT start (M5 Task 4 pinned — no live watcher
        rebuild). The modal ``exec()`` runs a nested event loop, so the
        engine's tray-confirm marshal (BlockingQueuedConnection) stays live
        while the dialog is open."""
        from marshmellow.core import autostart
        from marshmellow.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.settings, apply_autostart=autostart.set_autostart)
        dialog.exec()

    def _build_stt(self) -> Transcriber:
        """Real faster-whisper for live runs; the fake keeps headless runs silent-fast.

        M5 Task 7 (OQ-9): a frozen build loads the PACKAGED ct2 model dir
        (`_internal/models/faster-whisper-base`) — the hub name would try a
        HuggingFace download at first start and the clean-account install must
        be offline-capable except the BYOK call. Dev keeps the hub name.
        """
        if self.headless:
            return FakeSTT()
        from marshmellow.core.assets import default_stt_model_dir
        from marshmellow.stt.fasterwhisper import FasterWhisperSTT

        model: str = self.settings.stt_model
        if getattr(sys, "frozen", False):
            bundled = default_stt_model_dir()
            if bundled is not None:
                model = str(bundled)
        return FasterWhisperSTT(model=model)

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

        The D5 SoM vision fallback is wired from `_Providers.vision` — a
        dedicated provider instance for the vision loop (M5 T1); without one,
        `snapshot` degrades to a UIA-only walk, never an error.
        """
        from marshmellow.perception.snapshot import ScreenPerception
        from marshmellow.perception.uia_reader import UIAReader
        from marshmellow.perception.vision import VisionPerceiver

        vision = (
            VisionPerceiver(provider=self._providers.vision)
            if self._providers.vision is not None
            else None
        )
        return ScreenPerception(
            reader=UIAReader(),
            registry=self.registry,
            foreground=self._foreground_window,
            vision=vision,
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

    def _build_confirm(self) -> ConfirmFlow:
        """The §3.13 spoken yes/no loop, shared by the L3 gate and the D6 lease."""
        from marshmellow.safety.confirm import ConfirmFlow

        return ConfirmFlow(ask=self.ask_channel.ask, fallback=self._confirm_fallback())

    def _build_gate(self) -> GateLike:
        """Real SafetyGate: authoritative classification + fail-closed confirm (§3.13)."""
        from marshmellow.safety.gate import SafetyGate
        from marshmellow.safety.policy import RiskPolicy

        return SafetyGate(
            policy=RiskPolicy(
                preset=self.settings.safety.preset,
                overrides=dict(self.settings.safety.overrides),
            ),
            confirm=self._build_confirm(),
            announce=self._announce,
            kill_probe=self._kill_engaged,
            resolve=self._resolve_element,
        )

    def _build_lease(self) -> ForegroundLease:
        """D6 (§3.12): the foreground lease behind every background-mode screen action.

        The confirmer is the same fail-closed ConfirmFlow as the L3 gate —
        spoken yes/no with the tray fallback in GUI mode; headless has no
        fallback, so ambiguity refuses. Refusals never reach the executor
        (the lease is enforced in the TaskManager seam, before the gate)."""
        from marshmellow.tasks.foreground import ForegroundLease

        return ForegroundLease(
            bus=self.bus, confirm=self._build_confirm(), mascot=self.mascot
        )

    def _confirm_fallback(self) -> Callable[[str], bool] | None:
        """The §3.13 tray-dialog fallback — or None headless (fail closed instead).

        The QMessageBox must be created and exec()'d on the GUI thread while the
        gate calls the fallback from a planner worker thread: composition wraps
        ``confirm_dialog`` in the GUI-thread marshal. Headless runs have no GUI
        thread at all, so there is no fallback and ambiguity refuses."""
        if self.headless:
            return None
        return GuiMarshaledConfirm()

    def _build_provider(self, cfg: LLMProviderConfig) -> LLMProvider | None:
        """One provider instance from its config — every call builds a FRESH HTTP
        client (the dedicated-instance rule: pools are loop-bound, M5 T1).

        Returns None for the kinds that mean "no LLM": `fake` and an ollama
        config without a model.
        """
        if cfg.kind == "ollama":
            from marshmellow.llm.ollama import DEFAULT_OLLAMA_URL, OllamaProvider

            return OllamaProvider(base_url=cfg.base_url or DEFAULT_OLLAMA_URL, model=cfg.model)
        if cfg.kind == "openai_compat":
            from marshmellow.llm.openai_compat import OpenAICompatProvider

            return OpenAICompatProvider(
                base_url=cfg.base_url,
                model=cfg.model,
                get_key=secrets.load_api_key,
                vision=cfg.vision,
            )
        return None  # kind == "fake"

    def _build_providers(self) -> _Providers:
        """D4/D5 (§3.4): build the planner LLM + the vision provider once.

        When `[providers].fallback` is configured (M5 T2), the planner provider
        is a `ProviderChain(primary, fallback)` — per provider, silent
        transient-failure retries, then the next (§3.4 fallback order, the
        local adapter behind the cloud); exhaustion everywhere raises
        ProviderUnavailableError, which the planner speaks as the distinct
        unavailable summary.

        `vision` is a DEDICATED provider instance (same model config, own HTTP
        client) whenever the planner provider's caps claim vision — a
        vision-capable cloud model (`[providers.llm].vision`, D5) or the local
        ollama model (vision-capable by contract, D4) — and None otherwise
        (`kind="fake"`, ollama without a model): the fake planner never serves
        vision. The instance must be dedicated, never the planner's: the
        planner drives its provider from the resident llm-planner-loop
        (orchestrator `_ensure_loop`) while VisionPerceiver drives its own
        from the vision-provider-loop, and HTTP pools are loop-bound — a
        shared instance deadloops/fails its second user's first pooled call
        (M5 T1 review fix). Vision selection is unchanged by the chain (M5
        T2): the dedicated instance comes from the primary model config, while
        the CHAIN's caps OR the fallback's vision in for anyone that reads
        them. Providers are built BEFORE perception so the SoM fallback is
        wired from `_Providers.vision` (see `__init__` ordering).
        """
        cfg = self.settings.providers.llm
        if cfg.kind == "ollama" and not cfg.model:
            logger.warning(
                "LLM provider kind 'ollama' needs a model "
                "(set [providers.llm].model); using the keyword planner"
            )
            return _Providers(planner=None, vision=None)
        provider = self._build_provider(cfg)
        if provider is None:
            return _Providers(planner=None, vision=None)  # kind == "fake"
        logger.info("LLM provider: %s (model=%s)", cfg.kind, cfg.model)
        planner: LLMProvider = provider
        fb = self.settings.providers.fallback
        if fb is not None:
            fallback = self._build_provider(fb)
            if fallback is not None:
                planner = ProviderChain(primary=provider, fallback=fallback)
                logger.info("LLM fallback chain: %s → %s", cfg.kind, fb.kind)
        return _Providers(
            planner=planner,
            vision=self._build_provider(cfg) if provider.capabilities.vision else None,
        )

    def _build_planner(self, settings: Settings) -> Planner:
        """D7: the provider setting picks the planner behind TaskManager.

        The provider decision itself was made in `_build_providers` (the D5
        vision fallback shares that LLM, so it must exist before perception):
        here it is merely wrapped with the screen toolbox (perceive/act/
        verify, M3); a None provider falls back to the keyword planner.
        """
        provider = self._providers.planner
        if provider is None:
            return FakePlanner()
        from marshmellow.planner.orchestrator import LLMPlanner

        return LLMPlanner(provider, toolbox=self.toolbox)

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
        `create_task` → the TaskManager (a task-layer operation, leaseless per
        READ_KINDS — it never touches the screen, so the gate is not in its
        path); everything else → the safety gate in front of the action layer
        (§3.13).
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
            # Release-audit S1: gated — the L1 announce/log/confirm policy
            # applies to DE-pack launches exactly as to on-screen actions;
            # the pack sees only requests the gate allowed.
            return self._gated_shell(req)
        if req.kind == "create_task":
            return self._create_task(req)
        return self._gated_execute(req)

    def _run_shell(self, req: ActionRequest) -> ActionResult:
        """The gated shell executor: DE-pack launches (app names and URIs)."""
        result: ShellResult = self.pack.open_app(str(req.payload.get("open_app", "")))
        return ActionResult(ok=result.ok, observation=result.detail)

    def _create_task(self, req: ActionRequest) -> ActionResult:
        """M4 (§3.12): the LLM registers a background task through this seam.

        The payload is validated HERE — pydantic for the monitor, the 30 s
        interval floor for LLM-created monitors, monitor/schedule presence per
        mode — and the task is registered with the TaskManager, which starts
        its long-lived loop and reports by voice later. Every refusal is an
        ActionResult(ok=False) observation (the model reacts; nothing is
        registered); register()'s own ValueError (e.g. an unparseable monitor
        target) lands in the same place.
        """
        payload = req.payload
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return _invalid_create_task("instruction must be a non-empty string")
        mode = payload.get("mode")
        if not isinstance(mode, str) or mode not in BACKGROUND_MODES:
            return _invalid_create_task(
                f"mode must be one of {', '.join(sorted(BACKGROUND_MODES))}"
            )
        monitor: MonitorSpec | None = None
        raw_monitor = payload.get("monitor")
        if raw_monitor is not None:
            if not isinstance(raw_monitor, dict):
                return _invalid_create_task("monitor must be an object")
            try:
                monitor = MonitorSpec.model_validate(raw_monitor)
            except ValidationError as exc:
                errors = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                )
                return _invalid_create_task(f"monitor is invalid ({errors})")
            if monitor.interval_s < MONITOR_MIN_INTERVAL_S:
                return _invalid_create_task(
                    f"interval_s must be at least {MONITOR_MIN_INTERVAL_S} seconds "
                    f"for LLM-created monitors (got {monitor.interval_s})"
                )
        schedule: datetime | None = None
        raw_schedule = payload.get("schedule")
        if raw_schedule is not None:
            if not isinstance(raw_schedule, str) or not raw_schedule.strip():
                return _invalid_create_task("schedule must be an ISO-8601 string")
            try:
                schedule = datetime.fromisoformat(raw_schedule)
            except ValueError:
                return _invalid_create_task(
                    f"schedule {raw_schedule!r} is not an ISO-8601 date-time"
                )
        if mode == "background_monitor" and monitor is None:
            return _invalid_create_task(
                "background_monitor requires a monitor (kind, target, interval_s, condition)"
            )
        if mode == "scheduled" and schedule is None:
            return _invalid_create_task("scheduled requires a schedule (ISO-8601)")
        spec = TaskSpec(
            id=uuid.uuid4().hex[:8],
            instruction=instruction.strip(),
            mode=cast(TaskMode, mode),
            monitor=monitor,
            schedule=schedule,
            created_by="llm",
        )
        try:
            self.manager.register(spec)
        except ValueError as exc:
            return _invalid_create_task(str(exc))
        logger.info("create_task: %s registered (%s)", spec.id, mode)
        return ActionResult(
            ok=True,
            observation=(
                f"background task {spec.id} created ({mode}) — it runs on its own and "
                "reports by voice; finish and tell the user what will happen"
            ),
        )

    def _build_activation(self, settings: Settings) -> None:
        """D2: the activation setting decides which channels get wired.

        M5 Task 5 (D8): the wake channel shares ONE `ChunkQueue` between the
        capture tap and the watcher — the engine feeds it per unmuted chunk,
        the watcher drains it, and a stalled scorer never stalls capture. The
        model dir is the injected `wake_model_dir`, else the packaged default
        when it holds a trained model (GUI mode only: headless runs — tests and
        the simulator — never load real models or open the mic; they inject
        explicitly). Without a model, model_dir=None keeps the existing spoken
        degrade and the hotkey stays the trusted channel.
        """
        mode = settings.activation
        self.hotkey_watcher: HotkeyWatcher | None = None
        self.wake_watcher: WakeWordWatcher | None = None
        self.wake_tap: ChunkQueue | None = None
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
            model_dir = self._wake_model_dir
            if (
                model_dir is None
                and not self.headless
                and default_wake_model_path() is not None
            ):
                model_dir = default_wake_model_dir()
            # D8 mic sharing: one queue is the engine's tap AND the watcher's source.
            self.wake_tap = ChunkQueue()
            self.wake_watcher = WakeWordWatcher(
                self.bus,
                model_dir=model_dir,
                chunk_source=iter(self.wake_tap),
                threshold=wake_threshold_from_calibration(model_dir),
            )

    def _on_activation_degraded(self, event: ActivationDegraded) -> None:
        self.tts.say(SpeakRequest(text=event.message, priority="background"))

    def _wire_voice_feedback(self) -> None:
        def on_task_event(event: TaskEvent) -> None:
            if event.type == "started":
                self.tts.say(SpeakRequest(text="On it.", priority="ack"))
                logger.info("spoken (ack, started): On it.")
                if self.mascot is not None:
                    self.mascot.set_state("acting")
            elif event.type in ("result", "failed"):
                # M4 (§3.12): background tasks report UNPROMPTED — the user is
                # possibly mid-sentence, so those utterances go out at the
                # background priority and never barge in. One-shot submits
                # have no registered mode (task_mode → None) → foreground
                # turns keep the "result" priority.
                mode = self.manager.task_mode(event.task_id)
                priority: Literal["ack", "result", "background"] = (
                    "background" if mode in BACKGROUND_MODES else "result"
                )
                self.tts.say(SpeakRequest(text=event.payload, priority=priority))
                # M5 T7 (dogfood report): the spoken payload reaches the session
                # JSONL so the day-report counts spoken results and (background
                # priority) monitor triggers by grep, not memory.
                logger.info("spoken (%s, %s): %s", priority, event.type, event.payload)
                if self.mascot is not None:
                    self.mascot.set_state("speaking" if event.type == "result" else "error")

        self.bus.subscribe(TaskEvent, on_task_event)

    def _on_tray_action(self, action: TrayAction) -> None:
        if action.kind == "quit":
            self._quit_requested = True  # the app loop polls this; never join from the bus thread
        elif action.kind == "open_ui":
            # This handler runs on the bus drain thread; widgets must live on
            # the GUI thread — hop over (non-blocking; §3.11, M4 T6).
            if self._open_ui_marshal is not None:  # subscribed only in GUI mode
                self._open_ui_marshal.request()
        elif action.kind in ("mute", "unmute"):
            # Release-audit S2: the tray is the recovery channel while the mic
            # is muted (voice intents cannot be heard) — it must actually work.
            muted = action.kind == "mute"
            if self.audio_engine is not None:
                self.audio_engine.set_muted(muted)
            if self.mascot is not None:
                self.mascot.set_state("muted" if muted else "idle")
            logger.info("microphone muted: %s (tray)", muted)
        elif action.kind == "pause":
            # Tray kill-switch equivalent: cancel every running task now.
            self.manager.cancel_all()
            self.tts.say(SpeakRequest(text="All tasks cancelled.", priority="result"))
        logger.info("tray action: %s", action.kind)
