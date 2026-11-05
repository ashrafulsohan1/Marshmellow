"""UIA pattern invocation — action preference order step 1 (01-architecture §3.7).

`UiaPatternInvoker` is the seam; `UiaAutomationPatterns` drives the live
uiautomation controls carried by the registry's node handles (perception's
walker wraps each UIA control it visits). Every call returns bool: False means
the pattern was unavailable or failed (stale node, COM error) — the ActionLayer
turns that into a planner-visible ActionResult, never an exception.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import Any, Protocol

from marshmellow.perception.uia_reader import UiaNode


class UiaPatternInvoker(Protocol):
    """Pattern calls on a live UIA node; False = unavailable or failed."""

    def invoke(self, node: UiaNode) -> bool: ...
    def toggle(self, node: UiaNode) -> bool: ...
    def set_value(self, node: UiaNode, text: str) -> bool: ...
    def select(self, node: UiaNode) -> bool: ...
    def expand(self, node: UiaNode, expand: bool) -> bool: ...


def live_control(node: UiaNode) -> Any:
    """The live uiautomation control behind a walked node, if it carries one.

    Perception's real walker attaches the control it visited (so patterns act
    on the live element, not a property snapshot); fakes may omit it.
    """
    return getattr(node, "control", None)


def uia_elevation_required(node: UiaNode) -> bool:
    """Default elevation seam for the ActionLayer: the target runs elevated while
    we do not (§3.7 "elevated windows are untouchable without elevation").

    There is no public UIA_IsElevationRequired element property (checked the
    Windows 10/11 SDK headers and learn.microsoft.com uiauto-automation-element-
    propids — the constant does not exist), so this goes to the source: the
    owning process's TokenElevation, queried through the live control's
    ProcessId. Anything it cannot prove (no live control, probe refused,
    ourselves elevated too) is reported as NOT elevated — pattern calls on
    genuinely untouchable windows fail afterwards and surface as uia_failed.
    """
    control = live_control(node)
    if control is None:
        return False
    try:
        pid = int(control.ProcessId)
    except Exception:  # stale control / COM error
        return False
    return _process_elevated(pid) and not _self_elevated()


def _process_elevated(pid: int) -> bool:
    """True when process `pid` runs with an elevated token (best effort)."""
    process_query_limited_information = 0x1000
    token_query = 0x0008
    token_elevation = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(handle, token_query, ctypes.byref(token)):
            return False
        try:
            elevation = ctypes.c_ulong()
            returned = ctypes.c_ulong()
            ok = advapi32.GetTokenInformation(
                token, token_elevation, ctypes.byref(elevation),
                ctypes.sizeof(elevation), ctypes.byref(returned),
            )
            return bool(ok and elevation.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def _self_elevated() -> bool:
    """True when OUR process runs elevated (then elevated targets are touchable)."""
    token_query = 0x0008
    token_elevation = 20
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query,
                                     ctypes.byref(token)):
        return False
    try:
        elevation = ctypes.c_ulong()
        returned = ctypes.c_ulong()
        ok = advapi32.GetTokenInformation(
            token, token_elevation, ctypes.byref(elevation),
            ctypes.sizeof(elevation), ctypes.byref(returned),
        )
        return bool(ok and elevation.value)
    finally:
        kernel32.CloseHandle(token)


class UiaAutomationPatterns:
    """UiaPatternInvoker over uiautomation pattern objects on the live control.

    Each call resolves the pattern freshly from the live control (a stale
    control raises COMError -> False). `wait_time` is uiautomation's post-call
    settle sleep, kept short so multi-step plans stay responsive; the
    ActionLayer's post-state walk does the real settling.
    """

    def __init__(self, wait_time: float = 0.1) -> None:
        self._wait_time = wait_time

    def invoke(self, node: UiaNode) -> bool:
        return self._call(node, "GetInvokePattern", lambda p: p.Invoke(self._wait_time))

    def toggle(self, node: UiaNode) -> bool:
        return self._call(node, "GetTogglePattern", lambda p: p.Toggle(self._wait_time))

    def set_value(self, node: UiaNode, text: str) -> bool:
        return self._call(node, "GetValuePattern", lambda p: p.SetValue(text))

    def select(self, node: UiaNode) -> bool:
        return self._call(node, "GetSelectionItemPattern", lambda p: p.Select(self._wait_time))

    def expand(self, node: UiaNode, expand: bool) -> bool:
        def act(pattern: Any) -> Any:
            return pattern.Expand(self._wait_time) if expand else pattern.Collapse(self._wait_time)

        return self._call(node, "GetExpandCollapsePattern", act)

    @staticmethod
    def _call(node: UiaNode, getter: str, action: Callable[[Any], Any]) -> bool:
        control = live_control(node)
        if control is None:
            return False
        try:
            return bool(action(getattr(control, getter)()))
        except Exception:  # stale node / pattern unavailable / COM error
            return False
