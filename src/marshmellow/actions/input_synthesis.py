"""InputSynthesizer — the SendInput path (01-architecture §3.7).

Steps 2/3 of the action preference order: keyboard-equivalent input (type,
hotkey) and synthesized mouse, used only after the UIA pattern path declined.
Events are submitted one SendInput call each with jittered, human-plausible
delays between presses; mouse moves carry positional jitter. `send_input` is
injectable so tests record the exact event stream without touching hardware.

Typing is Unicode SendInput (KEYEVENTF_UNICODE) and surrogate-pair safe: a
non-BMP character is sent as its two UTF-16 code units in order, which Windows
recombines into a single character in the target's message stream.
"""

from __future__ import annotations

import ctypes
import random
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

WHEEL_DELTA: Final = 120

KEYEVENTF_KEYUP: Final = 0x0002
KEYEVENTF_UNICODE: Final = 0x0004
MOUSEEVENTF_MOVE: Final = 0x0001
MOUSEEVENTF_ABSOLUTE: Final = 0x8000
MOUSEEVENTF_LEFTDOWN: Final = 0x0002
MOUSEEVENTF_LEFTUP: Final = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
MOUSEEVENTF_RIGHTUP: Final = 0x0010
MOUSEEVENTF_WHEEL: Final = 0x0800

INPUT_MOUSE: Final = 0
INPUT_KEYBOARD: Final = 1


@dataclass(frozen=True)
class KeyboardEvent:
    """One keyboard INPUT: a virtual-key press, or one KEYEVENTF_UNICODE unit."""

    vk: int = 0
    scan: int = 0
    up: bool = False
    unicode: bool = False


@dataclass(frozen=True)
class MouseEvent:
    """One mouse INPUT (absolute screen-pixel coordinates for moves)."""

    kind: Literal["move", "down", "up", "wheel"]
    x: int = 0
    y: int = 0
    button: Literal["left", "right"] = "left"
    delta: int = 0


InputEvent = KeyboardEvent | MouseEvent
SendInputFn = Callable[[Sequence[InputEvent]], int]
SleepFn = Callable[[float], None]


class InputSynthesisError(RuntimeError):
    """Synthesizing real input failed (e.g. SendInput unavailable or refused)."""


# Virtual-key codes for hotkey combo tokens; single characters use ord() upper.
_VK: Final[dict[str, int]] = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21,
    "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
_VK.update({f"f{i}": 0x70 + i - 1 for i in range(1, 13)})


def _vk_for(token: str) -> int:
    key = token.strip().lower()
    if not key:
        raise ValueError("empty key token in combo")
    if key in _VK:
        return _VK[key]
    if len(key) == 1:
        code = ord(key.upper())
        if 0x01 <= code <= 0xFE:
            return code
    raise ValueError(f"unknown key {token!r} in combo")


def _parse_combo(combo: str) -> list[int]:
    tokens = combo.split("+")
    if not tokens:
        raise ValueError("empty combo")
    return [_vk_for(token) for token in tokens]


def _utf16_units(char: str) -> list[int]:
    """UTF-16 code units of one character (surrogate pairs split, in order)."""
    encoded = char.encode("utf-16-le")
    return [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]


class InputSynthesizer:
    """Jittered, human-plausible input streams (§3.7).

    `send_input` defaults to the real ctypes SendInput; tests inject a recorder.
    `delay_ms` is the jitter band slept between presses; `jitter_px` fuzzes mouse
    targets; `sleep`/`rng` are injectable for deterministic tests.
    """

    def __init__(
        self,
        send_input: SendInputFn | None = None,
        delay_ms: tuple[int, int] = (20, 60),
        *,
        jitter_px: int = 2,
        sleep: SleepFn = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._send: SendInputFn = send_input if send_input is not None else send_input_native
        self._delay_ms = delay_ms
        self._jitter_px = jitter_px
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()

    def click(self, point: tuple[int, int], *, right: bool = False, double: bool = False) -> None:
        """Move (jittered) to the point, then one down/up pair (two if double)."""
        button: Literal["left", "right"] = "right" if right else "left"
        x, y = self._jitter(point)
        self._emit(MouseEvent(kind="move", x=x, y=y))
        self._pause()
        for pair in range(2 if double else 1):
            if pair:
                self._pause()
            self._emit(MouseEvent(kind="down", button=button))
            self._emit(MouseEvent(kind="up", button=button))

    def type_text(self, text: str) -> None:
        """Unicode SendInput per UTF-16 unit with inter-key delays."""
        for char in text:
            for unit in _utf16_units(char):
                self._pause()
                self._emit(KeyboardEvent(scan=unit, unicode=True))
                self._emit(KeyboardEvent(scan=unit, unicode=True, up=True))

    def hotkey(self, combo: str) -> None:
        """Hold the combo in listed order, then release in reverse ("ctrl+shift+s")."""
        keys = _parse_combo(combo)
        for vk in keys:
            self._pause()
            self._emit(KeyboardEvent(vk=vk))
        for vk in reversed(keys):
            self._pause()
            self._emit(KeyboardEvent(vk=vk, up=True))

    def scroll(self, point: tuple[int, int], lines: int) -> None:
        """Move to the point, then one wheel notch per line (120 per notch)."""
        x, y = self._jitter(point)
        self._emit(MouseEvent(kind="move", x=x, y=y))
        direction = 1 if lines >= 0 else -1
        for _ in range(abs(lines)):
            self._pause()
            self._emit(MouseEvent(kind="wheel", delta=direction * WHEEL_DELTA))

    def _emit(self, event: InputEvent) -> None:
        self._send([event])

    def _pause(self) -> None:
        low, high = self._delay_ms
        self._sleep(self._rng.uniform(low, high) / 1000.0)

    def _jitter(self, point: tuple[int, int]) -> tuple[int, int]:
        if self._jitter_px <= 0:
            return point
        span = self._jitter_px
        return point[0] + self._rng.randint(-span, span), point[1] + self._rng.randint(-span, span)


# -- real SendInput adapter -----------------------------------------------------
#
# Pure ctypes (no wintypes import) so the module imports cleanly off-Windows;
# send_input_native raises InputSynthesisError when actually used off-Windows.

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _normalized(x: int, y: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Absolute screen pixels -> 0..65535 normalized (primary monitor, v1)."""
    nx = round(x * 65535 / (screen_w - 1)) if screen_w > 1 else 0
    ny = round(y * 65535 / (screen_h - 1)) if screen_h > 1 else 0
    return nx, ny


def send_input_native(events: Sequence[InputEvent]) -> int:
    """Submit events through the real SendInput; returns the accepted count."""
    if sys.platform != "win32":
        raise InputSynthesisError("SendInput is only available on Windows")
    user32 = ctypes.windll.user32
    native = (_INPUT * len(events))()
    screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    for i, event in enumerate(events):
        entry = native[i]
        if isinstance(event, KeyboardEvent):
            entry.type = INPUT_KEYBOARD
            entry.ki.wVk = event.vk
            entry.ki.wScan = event.scan
            flags = 0
            if event.unicode:
                flags |= KEYEVENTF_UNICODE
            if event.up:
                flags |= KEYEVENTF_KEYUP
            entry.ki.dwFlags = flags
        else:
            entry.type = INPUT_MOUSE
            if event.kind == "move":
                entry.mi.dx, entry.mi.dy = _normalized(event.x, event.y, screen_w, screen_h)
                entry.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
            elif event.kind == "down":
                entry.mi.dwFlags = (
                    MOUSEEVENTF_RIGHTDOWN if event.button == "right" else MOUSEEVENTF_LEFTDOWN
                )
            elif event.kind == "up":
                entry.mi.dwFlags = (
                    MOUSEEVENTF_RIGHTUP if event.button == "right" else MOUSEEVENTF_LEFTUP
                )
            else:
                entry.mi.dwFlags = MOUSEEVENTF_WHEEL
                entry.mi.mouseData = event.delta & 0xFFFFFFFF
    accepted = int(user32.SendInput(len(events), native, ctypes.sizeof(_INPUT)))
    if accepted != len(events):
        raise InputSynthesisError(f"SendInput accepted {accepted}/{len(events)} events")
    return accepted
