"""Activation — hotkey (WH_KEYBOARD_LL) + wake word (openWakeWord) (01-architecture §3.1).

`HotkeyWatcher` runs a low-level keyboard hook on its own message-pump thread; the
combo state machine is pure and testable on injected key events. `WakeWordWatcher`
streams mic chunks through openWakeWord's native 80 ms frame. Both live off the
event loop and emit through `emit_threadsafe`; every start-up failure degrades to
a spoken `ActivationDegraded` instead of crashing (M2 plan Review Focus 4, D2/D8).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import (
    ActivationDegraded,
    HotkeyPressed,
    HotkeyReleased,
    WakeDetected,
)

logger = logging.getLogger(__name__)

HotkeyMode = Literal["toggle", "hold"]

# Virtual-key codes the tests and the combo machine share. KBDLLHOOKSTRUCT reports
# the L/R-specific vk for modifiers, so each logical modifier owns a variant set.
VK_CTRL = 0x11
VK_SHIFT = 0x10
VK_M = 0x4D
_MODIFIER_VKS: dict[str, frozenset[int]] = {
    "ctrl": frozenset({0x11, 0xA2, 0xA3}),  # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
    "shift": frozenset({0x10, 0xA0, 0xA1}),  # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
    "alt": frozenset({0x12, 0xA4, 0xA5}),  # VK_MENU, VK_LMENU, VK_RMENU
    "win": frozenset({0x5B, 0x5C}),  # VK_LWIN, VK_RWIN
}


def parse_combo(combo: str) -> tuple[tuple[frozenset[int], ...], int]:
    """`"ctrl+shift+m"` → (one vk-variant set per required modifier, trigger vk).

    Raises ValueError on unknown modifiers, a modifier trigger, or multi-char keys.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2 or any(p not in _MODIFIER_VKS for p in parts[:-1]):
        raise ValueError(f"unsupported hotkey combo: {combo!r}")
    trigger = parts[-1]
    if trigger in _MODIFIER_VKS or len(trigger) != 1:
        raise ValueError(f"unsupported hotkey trigger: {combo!r}")
    mods = tuple(_MODIFIER_VKS[name] for name in parts[:-1])
    return mods, ord(trigger.upper())


class ComboTracker:
    """Pure hold/toggle state machine over injected key events (the test seam).

    hold: pressed when the full combo goes down, released on trigger-up or any
    required modifier lifting (end-of-speech semantics, 01-architecture §2).
    toggle: each full combo press flips listening on/off.
    LL-hook key auto-repeat is ignored via trigger-down edge detection.
    """

    def __init__(self, combo: str, mode: HotkeyMode) -> None:
        if mode not in ("toggle", "hold"):
            raise ValueError(f"unsupported hotkey mode: {mode!r}")
        self._mods, self._trigger = parse_combo(combo)
        self._mode: str = mode
        self._mods_down: set[int] = set()
        self._trigger_down = False
        self._active = False

    def feed(self, vk: int, down: bool) -> Literal["pressed", "released", None]:
        if vk == self._trigger:
            return self._feed_trigger(down)
        if any(vk in mod for mod in self._mods):
            if down:
                self._mods_down.add(vk)
            else:
                self._mods_down.discard(vk)
                if self._mode == "hold" and self._active and not self._mods_satisfied():
                    self._active = False
                    return "released"
        return None

    def _mods_satisfied(self) -> bool:
        return all(any(vk in self._mods_down for vk in mod) for mod in self._mods)

    def _feed_trigger(self, down: bool) -> Literal["pressed", "released", None]:
        if down:
            if self._trigger_down:  # auto-repeat, not a new press
                return None
            self._trigger_down = True
            if self._mode == "toggle":
                if self._active:
                    self._active = False
                    return "released"
                if self._mods_satisfied():
                    self._active = True
                    return "pressed"
                return None
            if self._mods_satisfied() and not self._active:
                self._active = True
                return "pressed"
            return None
        self._trigger_down = False
        if self._mode == "hold" and self._active:
            self._active = False
            return "released"
        return None


class HookBackend(Protocol):
    """The one seam the LL-hook plumbing hangs on (counted by tests, ctypes for real)."""

    def install(self, on_key: Callable[[int, bool], None]) -> int:
        """Install the hook; returns its handle, or 0 on registration failure."""
        ...

    def run(self) -> None:
        """Pump until request_stop; key events arrive via the install() callback."""
        ...

    def request_stop(self) -> None: ...

    def uninstall(self, handle: int) -> None: ...


class HotkeyWatcher:
    """Global hotkey watcher emitting HotkeyPressed/HotkeyReleased on the bus."""

    def __init__(
        self,
        bus: EventBus,
        combo: str = "ctrl+shift+m",
        mode: HotkeyMode = "hold",
        backend: HookBackend | None = None,
    ) -> None:
        self._bus = bus
        self._combo = combo
        self._tracker = ComboTracker(combo, mode)  # validates combo + mode eagerly
        self._backend = backend or WinLLHookBackend()
        self._thread = threading.Thread(target=self._run, name="hotkey-hook", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._thread.is_alive():
            self._backend.request_stop()
            self._thread.join(timeout=2.0)

    def on_key(self, vk: int, down: bool) -> None:
        """Key-event seam: the hook callback and tests both inject here."""
        transition = self._tracker.feed(vk, down)
        if transition == "pressed":
            self._bus.emit_threadsafe(HotkeyPressed(combo=self._combo))
        elif transition == "released":
            self._bus.emit_threadsafe(HotkeyReleased(combo=self._combo))

    def _run(self) -> None:
        handle = self._backend.install(self.on_key)
        if not handle:
            logger.error("hotkey %s could not be registered; degrading", self._combo)
            self._bus.emit_threadsafe(ActivationDegraded(
                channel="hotkey",
                message="I can't listen for the hotkey — another program may own it. "
                        "Try the wake word instead.",
            ))
            return
        try:
            self._backend.run()
        finally:
            self._backend.uninstall(handle)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
    _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0104, 0x0105
    _WM_QUIT = 0x0012

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    _LRESULT = wintypes.LPARAM
    _HOOKPROC = ctypes.WINFUNCTYPE(
        _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.SetWindowsHookExW.argtypes = (
        ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD,
    )
    _user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.CallNextHookEx.restype = _LRESULT
    _user32.CallNextHookEx.argtypes = (
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    )
    _user32.GetMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    )
    _user32.GetMessageW.restype = ctypes.c_int  # BOOL, but -1 on error
    _user32.PostThreadMessageW.argtypes = (
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    )
    _kernel32 = ctypes.WinDLL("kernel32")
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    class WinLLHookBackend:
        """Real LL-hook backend: ctypes only, no new dependency (M2 task interface).

        Must run on the watcher thread: WH_KEYBOARD_LL callbacks fire only while
        the installing thread pumps messages, so run() blocks on GetMessageW.
        """

        def __init__(self) -> None:
            self._proc: _HOOKPROC | None = None
            self._thread_id = 0

        def install(self, on_key: Callable[[int, bool], None]) -> int:
            user32 = _user32

            def proc(n_code: int, w_param: int, l_param: int) -> int:
                if n_code >= 0:  # HC_ACTION
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                    ).contents
                    down = w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                    try:
                        on_key(kb.vkCode, down)
                    except Exception:  # never swallow another app's keystrokes
                        logger.exception("hotkey handler raised")
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._proc = _HOOKPROC(proc)  # keep the trampoline alive for the hook
            handle = _user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, None, 0)
            self._thread_id = _kernel32.GetCurrentThreadId()
            if not handle:
                logger.error(
                    "SetWindowsHookExW failed: winerror=%s", ctypes.get_last_error()
                )
                return 0
            return int(handle)

        def run(self) -> None:
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass

        def request_stop(self) -> None:
            deadline = time.monotonic() + 1.0
            while self._thread_id == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            if self._thread_id:
                _user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)

        def uninstall(self, handle: int) -> None:
            if handle:
                _user32.UnhookWindowsHookEx(handle)
            self._proc = None

else:  # non-Windows: the product is win32-first; stub keeps imports safe off-Windows
    class WinLLHookBackend:  # type: ignore[no-redef]
        """Stub that always fails registration — same degradation path as reality."""

        def install(self, on_key: Callable[[int, bool], None]) -> int:
            return 0

        def run(self) -> None:
            return None

        def request_stop(self) -> None:
            return None

        def uninstall(self, handle: int) -> None:
            return None


# ------------------------------------------------------------------ wake word

WAKE_FRAME_SAMPLES = 1280  # 80 ms @ 16 kHz — openWakeWord's native streaming frame
WAKE_THRESHOLD = 0.5  # M0-SP3


class WakeScorer(Protocol):
    """Minimal scorer surface; frames are int16 (openWakeWord expects raw PCM)."""

    def predict(self, frame: np.ndarray) -> dict[str, float]: ...

    def reset(self) -> None: ...


def openwakeword_scorer(model_dir: Path) -> WakeScorer:
    """Real scorer: openWakeWord ONNX models from `model_dir`.

    Product rule (D8): ship only self-trained "hey marshmellow" models; the bundled
    pretrained ones are dev/test-only (MARSHMELLOW_DEV_MODELS=1 in the test tier).
    """
    from openwakeword.model import Model

    wake_models = sorted(
        p for p in model_dir.glob("*.onnx")
        if p.name not in ("melspectrogram.onnx", "embedding_model.onnx")
    )
    if not wake_models:
        raise FileNotFoundError(f"no wake model .onnx files in {model_dir}")
    kwargs: dict[str, str] = {}
    melspec = model_dir / "melspectrogram.onnx"
    embedding = model_dir / "embedding_model.onnx"
    if melspec.exists() and embedding.exists():
        # explicit preprocessor paths — the openwakeword wheel ships no resources
        kwargs = {
            "melspec_model_path": str(melspec),
            "embedding_model_path": str(embedding),
        }
    model = Model(
        wakeword_models=[str(p) for p in wake_models],
        inference_framework="onnx",
        **kwargs,
    )
    return model


class WakeWordWatcher:
    """openWakeWord streaming loop: mic chunks → 80 ms frames → WakeDetected.

    Own thread, never blocks the loop. `model_dir=None` (or a failed load) degrades
    to a spoken ActivationDegraded — the hotkey path is the trusted one until the
    self-trained model exists (M2 plan, D8). A score >= threshold fires once, then
    stays quiet until the score drops back below (refractory against long phrases).
    """

    def __init__(
        self,
        bus: EventBus,
        model_dir: Path | None,
        chunk_source: Iterator[np.ndarray],
        threshold: float = WAKE_THRESHOLD,
        scorer_factory: Callable[[Path], WakeScorer] = openwakeword_scorer,
        frame_samples: int = WAKE_FRAME_SAMPLES,
    ) -> None:
        self._bus = bus
        self._model_dir = model_dir
        self._source = chunk_source
        self._threshold = threshold
        self._scorer_factory = scorer_factory
        self._frame_samples = frame_samples
        self._alive = False
        self._thread = threading.Thread(target=self._loop, name="wake-word", daemon=True)

    def start(self) -> None:
        if self._model_dir is None:
            logger.info("no wake model configured; degrading to hotkey-only")
            self._bus.emit_threadsafe(ActivationDegraded(
                channel="wake_word",
                message="The wake word isn't set up yet. Use the hotkey to talk to me.",
            ))
            return
        self._alive = True
        self._thread.start()

    def stop(self) -> None:
        self._alive = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        model_dir = self._model_dir
        if model_dir is None:  # defensive; start() already routed this path
            return
        try:
            scorer = self._scorer_factory(model_dir)
        except Exception:
            logger.exception("wake scorer failed to load from %s", model_dir)
            self._bus.emit_threadsafe(ActivationDegraded(
                channel="wake_word",
                message="I couldn't load the wake word model. Use the hotkey instead.",
            ))
            return
        buffer = np.zeros(0, dtype=np.int16)
        armed = True
        for chunk in self._source:
            if not self._alive:
                return
            buffer = np.concatenate([buffer, chunk.astype(np.int16, copy=False)])
            while len(buffer) >= self._frame_samples:
                frame = buffer[: self._frame_samples]
                buffer = buffer[self._frame_samples:]
                try:
                    scores = scorer.predict(frame)
                except StopIteration:  # scripted scorers end the loop
                    return
                score = max(scores.values(), default=0.0)
                if score >= self._threshold:
                    if armed:
                        armed = False
                        logger.info("wake detected (score=%.3f)", score)
                        self._bus.emit_threadsafe(WakeDetected(score=float(score)))
                else:
                    armed = True
