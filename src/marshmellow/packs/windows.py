"""Windows shell pack (v1): open-app resolution + window helpers.

SP1 rule baked in here: automation only ever addresses windows that appeared
AFTER our own launch — never "any window whose title matches". The ctypes
window helpers are stdlib-only so tests and the M3 perception layer can reuse
them without the uiautomation dependency.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from marshmellow.packs.base import ShellResult

WM_CLOSE = 0x0010

# alias -> ordered argv candidates (first one found on PATH wins)
_ALIAS_ARGV: dict[str, list[list[str]]] = {
    "notepad": [["notepad.exe"]],
    "calculator": [["calc.exe"]],
    "explorer": [["explorer.exe"]],
    "terminal": [["wt.exe"], ["cmd.exe"]],
    "browser": [["cmd", "/c", "start", "", "about:blank"]],
}
# alias -> URI opened via os.startfile
_ALIAS_URI: dict[str, str] = {
    "settings": "ms-settings:",
}


class LaunchPlan(BaseModel):
    argv: list[str] = Field(default_factory=list)
    uri: str | None = None


def top_level_windows() -> dict[int, str]:
    """Visible top-level windows: {hwnd: title} (stdlib ctypes)."""
    user32 = ctypes.windll.user32
    out: dict[int, str] = {}
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: Any, _lparam: Any) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                out[int(hwnd) & 0xFFFFFFFF] = buf.value
        return True

    user32.EnumWindows(proto(callback), 0)
    return out


def wait_new_window(
    before: set[int], title_substring: str, timeout: float
) -> int | None:
    """First visible window appearing after `before` whose title matches."""
    deadline = time.monotonic() + timeout
    needle = title_substring.lower()
    while time.monotonic() < deadline:
        for hwnd, title in top_level_windows().items():
            if hwnd not in before and needle in title.lower():
                return hwnd
        time.sleep(0.25)
    return None


def close_window(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


class WindowsPack:
    """Windows shell operations. Launcher seams are injectable for tests."""

    def __init__(
        self,
        launcher: Callable[[list[str]], Any] | None = None,
        startfile: Callable[[str], Any] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self._launcher = launcher or subprocess.Popen
        self._startfile = startfile or _os_startfile
        self._which = which or shutil.which

    def resolve(self, target: str) -> LaunchPlan | None:
        key = target.strip().lower()
        if key in _ALIAS_URI:
            return LaunchPlan(uri=_ALIAS_URI[key])
        if key in _ALIAS_ARGV:
            candidates = _ALIAS_ARGV[key]
            for argv in candidates:
                if argv[0].endswith(".exe") and self._which(argv[0]):
                    return LaunchPlan(argv=argv)
            return LaunchPlan(argv=candidates[-1])
        if "://" in key or key.startswith(("shell:", "ms-settings")):
            return LaunchPlan(uri=target.strip())
        if key.endswith(".exe") and self._which(key):
            return LaunchPlan(argv=[key])
        return None

    def open_app(self, target: str) -> ShellResult:
        plan = self.resolve(target)
        if plan is None:
            return ShellResult(ok=False, detail=f"unknown app: {target!r}")
        try:
            if plan.uri is not None:
                self._startfile(plan.uri)
                return ShellResult(ok=True, detail=f"startfile {plan.uri}")
            self._launcher(plan.argv)
            return ShellResult(ok=True, detail=" ".join(plan.argv))
        except OSError as exc:
            return ShellResult(ok=False, detail=f"launch failed: {exc}")


def _os_startfile(target: str) -> None:
    import os

    os.startfile(target)  # type: ignore[attr-defined]  # Windows-only
