"""Fixture-app harness (M3): launch, address, and close OUR OWN windows.

SP1 rule baked in: the harness only ever addresses windows that appeared after
the pre-launch snapshot — never "any window whose title matches". close() posts
WM_CLOSE to its own hwnd only; if the window refuses (e.g. a save prompt), the
owning process is force-terminated so teardown never leaks fixture windows.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
from types import TracebackType
from typing import Final

from marshmellow.core.contracts import Element
from marshmellow.packs.windows import (
    WindowsPack,
    close_window,
    top_level_windows,
    wait_new_window,
)
from marshmellow.perception.uia_reader import UiaAutomationSource, UIAReader

logger = logging.getLogger(__name__)

# alias -> case-insensitive title substring the launched window should show
_TITLE_HINTS: Final[dict[str, str]] = {
    "notepad": "notepad",
    "calculator": "calc",
    "explorer": "explorer",
    "settings": "settings",
}

_CLOSE_TIMEOUT_S: Final = 5.0
_POLL_S: Final = 0.25


class FixtureError(RuntimeError):
    """A fixture app failed to launch, appear, or close."""


class FixtureApp:
    """One launched fixture window (context manager, 01 §3.6 test harness).

    with FixtureApp("notepad") as app:
        app.hwnd        # the window that appeared for THIS launch only
        app.wait_element(app.hwnd, "Button", "Close")
        app.element_value(app.hwnd, ref) -> str
    """

    def __init__(
        self, alias: str, pack: WindowsPack | None = None, timeout: float = 15.0
    ) -> None:
        self._alias = alias
        self._pack = pack if pack is not None else WindowsPack()
        self._timeout = timeout
        self._before: set[int] = set()
        self._reader: UIAReader | None = None
        self.hwnd: int | None = None

    def __enter__(self) -> FixtureApp:
        self._before = set(top_level_windows())
        result = self._pack.open_app(self._alias)
        if not result.ok:
            raise FixtureError(f"could not launch {self._alias!r}: {result.detail}")
        hint = _TITLE_HINTS.get(self._alias, self._alias.lower())
        hwnd = wait_new_window(self._before, hint, self._timeout)
        if hwnd is None:
            raise FixtureError(
                f"{self._alias!r} window did not appear within {self._timeout:g}s"
            )
        self.hwnd = hwnd
        self._reader = UIAReader(source=UiaAutomationSource())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """WM_CLOSE only our own hwnd (SP1 rule); force-kill only if it refuses."""
        hwnd, self.hwnd = self.hwnd, None
        if hwnd is None:
            return
        close_window(hwnd)
        if not _wait_gone(hwnd, _CLOSE_TIMEOUT_S):
            pid = _owning_pid(hwnd)
            logger.warning(
                "fixture window %#x (%s) ignored WM_CLOSE; terminating pid %s",
                hwnd, self._alias, pid,
            )
            if pid:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid), "/T"],
                    check=False, capture_output=True,
                )
            if not _wait_gone(hwnd, _CLOSE_TIMEOUT_S):
                logger.error("fixture window %#x could not be closed", hwnd)

    # -- UIA-tree assertion helpers ---------------------------------------------

    def wait_element(
        self, hwnd: int, role: str, name_contains: str = "", timeout: float = 10.0
    ) -> Element:
        """Poll fresh UIA walks until an element with the role (and name
        substring) appears; FixtureError on timeout."""
        needle = name_contains.lower()
        deadline = time.monotonic() + timeout
        while True:
            for element in self._read(hwnd):
                if element.role.lower() == role.lower() and needle in element.name.lower():
                    return element
            if time.monotonic() >= deadline:
                raise FixtureError(
                    f"no {role} element matching {name_contains!r} within {timeout:g}s"
                )
            time.sleep(_POLL_S)

    def element_value(self, hwnd: int, ref: str) -> str:
        """Current UIA value of `ref` in a fresh walk ('' when valueless)."""
        for element in self._read(hwnd):
            if element.ref == ref:
                return element.value or ""
        raise FixtureError(f"ref {ref!r} not present in the current tree of {hwnd:#x}")

    def _read(self, hwnd: int) -> list[Element]:
        if self._reader is None or hwnd != self.hwnd:
            raise FixtureError("fixture access before open() or on a foreign hwnd")
        self._reader.invalidate(hwnd)  # assertion reads always need fresh state
        return self._reader.read(hwnd)


def move_window(hwnd: int, x: int, y: int) -> None:
    """SetWindowPos the window to (x, y), keeping size/z-order (race-test helper)."""
    if sys.platform != "win32":
        raise FixtureError("move_window is Windows-only")
    swp_nosize, swp_nozorder, swp_noactivate = 0x0001, 0x0004, 0x0010
    if not ctypes.windll.user32.SetWindowPos(
        hwnd, 0, x, y, 0, 0, swp_nosize | swp_nozorder | swp_noactivate
    ):
        raise FixtureError(f"SetWindowPos failed for hwnd {hwnd:#x}")


def focus_window(hwnd: int) -> bool:
    """Bring a window to the foreground (the focus-change race helper).

    UIA SetFocus first — it works for background windows where the calling
    process is not foreground-eligible; ctypes SetForegroundWindow as fallback.
    """
    if sys.platform != "win32":
        return False
    try:
        import uiautomation as uia

        control = uia.ControlFromHandle(hwnd)
        if control is not None and control.SetFocus():
            return True
    except Exception:
        logger.debug("UIA SetFocus failed for %#x; falling back", hwnd, exc_info=True)
    return bool(ctypes.windll.user32.SetForegroundWindow(hwnd))


def _owning_pid(hwnd: int) -> int:
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _wait_gone(hwnd: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hwnd not in top_level_windows():
            return True
        time.sleep(0.1)
    return False
