"""UIAReader — UIA tree walks -> interactable Elements (01-architecture §3.6, SP1).

Settle-poll until two consecutive walks agree (or deadline — SP1 finding 2: UWP
apps report an empty frame before content loads), then cache per (hwnd, tick)
(SP1 finding 3: walk cost varies 30x). Unit tests inject UiaTreeSource fakes;
UiaAutomationSource is the real uiautomation-backed walker, guarded so this
module imports cleanly off-Windows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, NamedTuple, Protocol

from marshmellow.core.contracts import Element, ElementAction, Rect
from marshmellow.perception import PerceptionError

try:
    import uiautomation as _uia
except ImportError:  # non-Windows: comtypes/uiautomation are unavailable
    _uia = None  # type: ignore[assignment]

# UIA pattern name -> Element action (contracts.ElementAction), plus `read` for all.
_PATTERN_MAP: Final[dict[str, ElementAction]] = {
    "Invoke": "invoke",
    "Toggle": "toggle",
    "Value": "set_value",
    "SelectionItem": "select",
    "ExpandCollapse": "expand",
}

# Cache lifetime = the remainder of the current tick. Long enough to dedupe the
# repeated reads of one planner step, short enough that revalidation snapshots
# see recent window moves. Freshness-critical paths may call invalidate(hwnd).
_TICK_S: Final[float] = 0.1


class UiaNode(Protocol):
    """One walked UIA control — the seam fakes implement for unit tests.

    `rect` is (x, y, width, height) in screen pixels; `patterns` uses UIA
    pattern names (Invoke, Toggle, Value, SelectionItem, ExpandCollapse);
    `value` is None when the control carries no value at all.
    """

    runtime_id: str
    control_type: str
    name: str
    value: str | None
    enabled: bool
    offscreen: bool
    password: bool
    rect: tuple[int, int, int, int]
    patterns: frozenset[str]


class UiaTreeSource(Protocol):
    """Blocking UIA walk seam — planner worker thread only (M3 Review Focus 5)."""

    def walk(self, hwnd: int) -> list[UiaNode]: ...


class ElementWithNode(NamedTuple):
    """An Element plus the provenance ElementRegistry.register needs."""

    element: Element
    runtime_id: str
    node: UiaNode | None


class UIAReader:
    """Reads a window's UIA tree into SP1-interactable Elements (refs e1..eN)."""

    def __init__(
        self,
        source: UiaTreeSource | None = None,
        settle_deadline_s: float = 2.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source: UiaTreeSource = source if source is not None else UiaAutomationSource()
        self._settle_deadline_s = settle_deadline_s
        self._clock = clock
        self._cache: dict[tuple[int, int], list[ElementWithNode]] = {}

    def read(self, hwnd: int) -> list[Element]:
        return [item.element for item in self.read_with_nodes(hwnd)]

    def read_with_nodes(self, hwnd: int) -> list[ElementWithNode]:
        """Like read(), but also carries the runtime ids and nodes for the registry."""
        tick = int(self._clock() / _TICK_S)
        cached = self._cache.get((hwnd, tick))
        if cached is None:
            cached = self._settle(hwnd)
            self._cache[(hwnd, tick)] = cached
        return [
            ElementWithNode(item.element.model_copy(deep=True), item.runtime_id, item.node)
            for item in cached
        ]

    def invalidate(self, hwnd: int | None = None) -> None:
        """Drop cached walks — every window, or one hwnd before a freshness-critical read."""
        if hwnd is None:
            self._cache.clear()
        else:
            for key in [k for k in self._cache if k[0] == hwnd]:
                del self._cache[key]

    def _settle(self, hwnd: int) -> list[ElementWithNode]:
        start = self._clock()
        current = self._read_once(hwnd)
        while self._clock() - start <= self._settle_deadline_s:
            following = self._read_once(hwnd)
            if [i.element for i in following] == [i.element for i in current]:
                return following
            current = following
        return current

    def _read_once(self, hwnd: int) -> list[ElementWithNode]:
        out: list[ElementWithNode] = []
        for node in self._source.walk(hwnd):
            if node.offscreen:
                continue
            actions = {_PATTERN_MAP[p] for p in node.patterns if p in _PATTERN_MAP}
            if not actions and node.value is None:
                continue  # SP1 interactable filter: an action pattern or a value
            actions.add("read")
            out.append(
                ElementWithNode(
                    Element(
                        ref=f"e{len(out) + 1}",
                        role=node.control_type,
                        name=node.name,
                        value=node.value,
                        bbox=Rect(x=node.rect[0], y=node.rect[1], w=node.rect[2], h=node.rect[3]),
                        enabled=node.enabled,
                        actions=actions,
                        password=node.password,
                    ),
                    node.runtime_id,
                    node,
                )
            )
        return out


class UiaAutomationSource:
    """UiaTreeSource over the real UIA tree via `uiautomation` (Windows only).

    walk() is blocking COM work — planner worker thread only. Off-Windows the
    class constructs fine and walk() raises PerceptionError.
    """

    def walk(self, hwnd: int) -> list[UiaNode]:
        root = self._root_control(hwnd)
        nodes: list[UiaNode] = []
        for control, _depth in _uia.WalkControl(root, includeTop=True):
            node = _wrap_control(control)
            if node is not None:
                nodes.append(node)
        return nodes

    def _root_control(self, hwnd: int) -> Any:
        if _uia is None:
            raise PerceptionError("uiautomation is unavailable on this platform")
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception as exc:
            raise PerceptionError(f"no UIA control for hwnd {hwnd:#x}: {exc}") from exc
        if root is None:  # hwnd 0 / nothing resolvable
            raise PerceptionError(f"no UIA control for hwnd {hwnd:#x}")
        return root


@dataclass
class _AutoNode:
    """Snapshot of one UIA control's properties (satisfies UiaNode)."""

    runtime_id: str
    control_type: str
    name: str
    value: str | None
    enabled: bool
    offscreen: bool
    password: bool
    rect: tuple[int, int, int, int]
    patterns: frozenset[str]


def _wrap_control(control: Any) -> _AutoNode | None:
    """Snapshot one UIA control's properties; None when unreadable (dead control)."""
    try:
        patterns = _available_patterns(control)
        has_value = "Value" in patterns
        return _AutoNode(
            runtime_id="-".join(str(part) for part in control.GetRuntimeId() or ()),
            control_type=_control_type_name(control),
            name=control.Name or "",
            # Read the value only when the Value pattern is available: plain
            # GetPropertyValue returns the UIA default ('') for unsupported props.
            value=(
                control.GetPropertyValue(_uia.PropertyId.ValueValueProperty) if has_value else None
            ),
            enabled=bool(control.IsEnabled),
            offscreen=bool(control.IsOffscreen),
            password=bool(control.IsPassword),
            rect=_rect_tuple(control.BoundingRectangle),
            patterns=patterns,
        )
    except Exception:
        return None


def _available_patterns(control: Any) -> frozenset[str]:
    checks = (
        ("Invoke", "IsInvokePatternAvailableProperty"),
        ("Toggle", "IsTogglePatternAvailableProperty"),
        ("Value", "IsValuePatternAvailableProperty"),
        ("SelectionItem", "IsSelectionItemPatternAvailableProperty"),
        ("ExpandCollapse", "IsExpandCollapsePatternAvailableProperty"),
    )
    props = _uia.PropertyId
    return frozenset(
        name for name, prop in checks if control.GetPropertyValue(getattr(props, prop))
    )


def _control_type_name(control: Any) -> str:
    raw = _uia.ControlTypeNames.get(control.ControlType)
    if raw and raw.endswith("Control"):  # 'ButtonControl' -> 'Button'
        raw = raw[: -len("Control")]
    return raw or f"ControlType({control.ControlType})"


def _rect_tuple(rect: Any) -> tuple[int, int, int, int]:
    """uiautomation Rect (l, t, r, b) -> (x, y, w, h) screen pixels."""
    return (
        int(rect.left),
        int(rect.top),
        int(rect.right - rect.left),
        int(rect.bottom - rect.top),
    )
