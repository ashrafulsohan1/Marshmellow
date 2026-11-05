"""Real-app DoD proof (M3 Task 3 Step 4 / 02-milestones M3 DoD): Notepad and
Calculator via FixtureApp, including the window-move and focus-change
re-validation races. Every test launches and closes its own fixture windows.

Manual tier: launches real desktop apps — deselected from the default gate
(run explicitly with `uv run pytest -m manual tests/actions`).
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable, Sequence
from typing import Final

import pytest

from marshmellow.actions.executor import ActionLayer
from marshmellow.actions.input_synthesis import (
    InputEvent,
    InputSynthesizer,
    MouseEvent,
    send_input_native,
)
from marshmellow.actions.uia_actions import UiaAutomationPatterns
from marshmellow.core.contracts import ActionRequest, Element, PerceptionSnapshot, Rect, WindowInfo
from marshmellow.perception.registry import ElementRegistry
from marshmellow.perception.snapshot import ScreenPerception
from marshmellow.perception.uia_reader import UiaAutomationSource, UIAReader
from marshmellow.testing.fixtures import FixtureApp, focus_window, move_window

pytestmark = [
    pytest.mark.manual,
    pytest.mark.skipif(sys.platform != "win32", reason="real Windows desktop required"),
]

TEXT: Final = "hello marshmellow"


class SpySendInput:
    """Wraps the real SendInput: passes events through, records mouse moves."""

    def __init__(self) -> None:
        self.moves: list[tuple[int, int]] = []

    def __call__(self, batch: Sequence[InputEvent]) -> int:
        for event in batch:
            if isinstance(event, MouseEvent) and event.kind == "move":
                self.moves.append((event.x, event.y))
        return send_input_native(list(batch))


Stack = tuple[ActionLayer, ScreenPerception, ElementRegistry, UIAReader]


def _real_foreground() -> WindowInfo | None:
    """The production seam (01-architecture §3.6): perception follows the REAL
    foreground window. No pinning — the focus-change race must be real."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    return WindowInfo(hwnd=hwnd, title="", app="foreground")


def _stack(
    hwnd: int,
    send_input: Callable[[Sequence[InputEvent]], int] | None = None,
    jitter_px: int = 2,
) -> Stack:
    """Real perception + action stack; perception uses the real foreground seam
    (D6: the test focuses its fixture window before snapshotting)."""
    registry = ElementRegistry()
    reader = UIAReader(source=UiaAutomationSource())
    perception = ScreenPerception(reader, registry, foreground=_real_foreground)
    inputs = InputSynthesizer(
        send_input=send_input if send_input is not None else send_input_native,
        jitter_px=jitter_px,
    )
    layer = ActionLayer(perception, registry, UiaAutomationPatterns(), inputs)
    return layer, perception, registry, reader


def _pick(snapshot: PerceptionSnapshot, role: str) -> Element:
    for element in snapshot.elements:
        if element.role == role:
            return element
    raise AssertionError(f"no {role!r} element in snapshot ({len(snapshot.elements)} elements)")


def _click(layer: ActionLayer, ref: str) -> None:
    result = layer.execute(
        ActionRequest(kind="click", target_ref=ref, rationale="fixture click")
    )
    assert result.ok, f"click on {ref} failed: {result.observation}"


def _center(bbox: Rect) -> tuple[int, int]:
    return (bbox.x + bbox.w // 2, bbox.y + bbox.h // 2)


def _window_pos(hwnd: int) -> tuple[int, int]:
    import ctypes.wintypes  # Windows-only stdlib module; imported lazily

    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(rect)), "GetWindowRect failed"
    return rect.left, rect.top


def _wait_foreground(hwnd: int, timeout: float = 10.0) -> bool:
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
    return False


def test_notepad_type_text_lands_in_document() -> None:
    with FixtureApp("notepad") as app:
        layer, perception, _registry, _reader = _stack(app.hwnd)
        app.wait_element(app.hwnd, "Document", "Text editor", timeout=20.0)
        assert focus_window(app.hwnd) and _wait_foreground(app.hwnd)
        document = _pick(perception.snapshot(), "Document")
        original = app.element_value(app.hwnd, app.wait_element(app.hwnd, "Document").ref)
        try:
            result = layer.execute(
                ActionRequest(
                    kind="type_text",
                    target_ref=document.ref,
                    payload={"text": TEXT},
                    rationale="type into the notepad document",
                )
            )
            assert result.ok, f"type_text failed: {result.observation}"

            # the text must be readable back from the UIA tree (fresh walk)
            deadline = time.monotonic() + 10.0
            seen = ""
            while time.monotonic() < deadline:
                fresh = app.wait_element(app.hwnd, "Document", "Text editor", timeout=5.0)
                seen = app.element_value(app.hwnd, fresh.ref)
                if seen == TEXT:
                    break
                time.sleep(0.25)
            assert seen == TEXT, f"UIA value did not land: {seen!r}"
        finally:
            # restore the pre-test document so the fixture closes cleanly (SP1 politeness)
            layer.execute(
                ActionRequest(
                    kind="uia_set_value",
                    target_ref=document.ref,
                    payload={"text": original},
                    rationale="restore document",
                )
            )


def test_calculator_clicks_compute_42() -> None:
    with FixtureApp("calculator") as app:
        layer, perception, _registry, _reader = _stack(app.hwnd)
        app.wait_element(app.hwnd, "Button", "Zero", timeout=30.0)
        assert focus_window(app.hwnd) and _wait_foreground(app.hwnd)
        snapshot = perception.snapshot()
        buttons = {e.name: e for e in snapshot.elements if e.role == "Button"}
        for name in ("Four", "Two", "Equals"):
            assert name in buttons, f"missing {name!r} button in calculator tree"
            _click(layer, buttons[name].ref)

        display = app.wait_element(app.hwnd, "Text", "Display is", timeout=10.0)
        assert "42" in display.name, f"display shows {display.name!r}"


def test_window_move_race_revalidates_to_new_bbox() -> None:
    with FixtureApp("notepad") as app:
        spy = SpySendInput()
        layer, perception, _registry, reader = _stack(
            app.hwnd, send_input=spy, jitter_px=0  # exact-center assertion
        )
        app.wait_element(app.hwnd, "Document", "Text editor", timeout=20.0)
        assert focus_window(app.hwnd) and _wait_foreground(app.hwnd)
        document = _pick(perception.snapshot(), "Document")
        old_center = _center(document.bbox)

        # move the fixture window (staying on-screen), then wait until a fresh
        # UIA walk reports the document at its NEW bbox
        x, y = _window_pos(app.hwnd)
        move_window(app.hwnd, x + 120, y + 80)
        deadline = time.monotonic() + 15.0
        new_center: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            reader.invalidate(app.hwnd)
            fresh = [e for e in reader.read(app.hwnd) if e.role == "Document"]
            if fresh and _center(fresh[0].bbox) != old_center:
                new_center = _center(fresh[0].bbox)
                break
            time.sleep(0.25)
        assert new_center is not None, "document bbox never moved in the UIA walk"

        # act on the OLD ref: revalidation must match by runtime id at the new
        # bbox and the click must land at the revalidated center (not the old one)
        result = layer.execute(
            ActionRequest(kind="click", target_ref=document.ref, rationale="post-move click")
        )
        assert result.ok, f"post-move click failed: {result.observation}"
        assert spy.moves, "click produced no mouse events"
        assert spy.moves[0] == new_center
        assert new_center != old_center


def test_focus_change_race_types_into_original_notepad() -> None:
    with FixtureApp("notepad") as app1, FixtureApp("notepad") as app2:
        layer, perception, _registry, _reader = _stack(app1.hwnd)
        app1.wait_element(app1.hwnd, "Document", "Text editor", timeout=20.0)
        assert focus_window(app1.hwnd) and _wait_foreground(app1.hwnd)
        document = _pick(perception.snapshot(), "Document")
        original = app1.element_value(app1.hwnd, app1.wait_element(app1.hwnd, "Document").ref)
        try:
            # steal the focus to the OTHER fixture window (the race). Guards both
            # failure modes: (a) refusing "element gone" instead of re-focus-and-
            # succeed, (b) matching the other window's identical document and
            # typing THERE (ok=True, wrong target).
            assert focus_window(app2.hwnd), "could not focus the second notepad"
            assert _wait_foreground(app2.hwnd), "second notepad never got the foreground"

            result = layer.execute(
                ActionRequest(
                    kind="type_text",
                    target_ref=document.ref,
                    payload={"text": TEXT},
                    rationale="type into notepad despite focus change",
                )
            )
            assert result.ok, f"type_text failed: {result.observation}"

            fresh = app1.wait_element(app1.hwnd, "Document", "Text editor", timeout=5.0)
            assert app1.element_value(app1.hwnd, fresh.ref) == TEXT, "text missed notepad 1"
            other = app2.wait_element(app2.hwnd, "Document", "Text editor", timeout=5.0)
            assert app2.element_value(app2.hwnd, other.ref) != TEXT, "text landed in notepad 2"
        finally:
            layer.execute(
                ActionRequest(
                    kind="uia_set_value",
                    target_ref=document.ref,
                    payload={"text": original},
                    rationale="restore document",
                )
            )
