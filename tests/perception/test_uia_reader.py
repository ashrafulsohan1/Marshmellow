"""UIAReader unit tests — scripted fake trees, no real windows (SP1 seam).

The manual tier below exercises the real UiaAutomationSource: Notepad is opened
via WindowsPack.open_app + wait_new_window and closed via its own hwnd (SP1
new-windows-only rule).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from marshmellow.core.contracts import Rect
from marshmellow.packs.windows import (
    WindowsPack,
    close_window,
    top_level_windows,
    wait_new_window,
)
from marshmellow.perception import PerceptionError
from marshmellow.perception.uia_reader import UiaAutomationSource, UiaNode, UIAReader


@dataclass
class FakeNode:
    """Minimal UiaNode stand-in for scripted walks."""

    control_type: str
    name: str = ""
    value: str | None = None
    enabled: bool = True
    offscreen: bool = False
    password: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    patterns: frozenset[str] = frozenset()
    runtime_id: str = ""


@dataclass
class FakeSource:
    """Scripted UiaTreeSource: serves walks in order, repeating the last one."""

    walks: list[list[FakeNode]]
    calls: int = 0

    def walk(self, hwnd: int) -> list[UiaNode]:
        self.calls += 1
        index = min(self.calls - 1, len(self.walks) - 1)
        return list(self.walks[index])


def test_walk_maps_nodes_into_elements_with_refs() -> None:
    source = FakeSource([[
        FakeNode("Button", name="Play", rect=(10, 20, 60, 24),
                 patterns=frozenset({"Invoke"})),
        FakeNode("Edit", name="doc", value="", rect=(0, 0, 100, 40),
                 patterns=frozenset({"Value"})),
    ]])
    elements = UIAReader(source=source).read(1234)

    assert [e.ref for e in elements] == ["e1", "e2"]
    assert elements[0].role == "Button"
    assert elements[0].name == "Play"
    assert elements[0].bbox == Rect(x=10, y=20, w=60, h=24)
    assert elements[0].enabled is True
    assert elements[0].source == "uia"
    assert elements[1].value == ""
    assert all(e.actions >= {"read"} for e in elements)


def test_pattern_map_and_password_propagate() -> None:
    all_patterns = FakeNode(
        "Button", name="all", rect=(0, 0, 1, 1),
        patterns=frozenset({"Invoke", "Toggle", "Value", "SelectionItem", "ExpandCollapse"}),
    )
    secret = FakeNode("Edit", name="pw", rect=(0, 0, 1, 1),
                      patterns=frozenset({"Value"}), password=True)
    elements = UIAReader(source=FakeSource([[all_patterns, secret]])).read(1)

    assert elements[0].actions == {"invoke", "toggle", "set_value", "select", "expand", "read"}
    assert elements[1].password is True


def test_interactable_filter_drops_offscreen_and_inert_nodes() -> None:
    offscreen = FakeNode("Button", name="hidden", rect=(0, 0, 1, 1),
                         patterns=frozenset({"Invoke"}), offscreen=True)
    inert = FakeNode("Text", name="label", rect=(0, 0, 1, 1))
    value_carrier = FakeNode("Edit", name="box", value="hi", rect=(0, 0, 1, 1))
    elements = UIAReader(source=FakeSource([[offscreen, inert, value_carrier]])).read(1)

    assert [e.name for e in elements] == ["box"]


def test_settle_poll_converges_when_second_walk_differs() -> None:
    first = [FakeNode("Button", name="A", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    second = [*first, FakeNode("Edit", name="B", value="", rect=(1, 1, 2, 2),
                               patterns=frozenset({"Value"}))]
    source = FakeSource([first, second])
    elements = UIAReader(source=source).read(1)

    assert [e.name for e in elements] == ["A", "B"]
    assert source.calls == 3  # walk1=A, walk2=B (differs), walk3=B (settled)


def test_settle_poll_stops_at_first_agreement() -> None:
    tree = [FakeNode("Button", name="A", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    source = FakeSource([tree, tree])
    UIAReader(source=source).read(1)

    assert source.calls == 2


def test_settle_poll_returns_last_walk_at_deadline() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0
        return now[0]

    a = [FakeNode("Button", name="A", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    b = [FakeNode("Button", name="B", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    source = FakeSource([a, b, a])  # never agrees: A, B, A, A, ...
    elements = UIAReader(source=source, settle_deadline_s=2.0, clock=clock).read(1)

    assert [e.name for e in elements] == ["A"]  # content of the third walk
    assert source.calls == 3


def test_cache_hits_within_tick_and_invalidate_forces_rewalk() -> None:
    tree = [FakeNode("Button", name="A", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    source = FakeSource([tree])
    reader = UIAReader(source=source, clock=lambda: 100.0)

    reader.read(1)
    assert source.calls == 2  # settle: two agreeing walks
    reader.read(1)
    assert source.calls == 2  # same tick -> cache hit, no walks

    reader.invalidate()
    reader.read(1)
    assert source.calls == 4

    reader.read(2)
    assert source.calls == 6  # another hwnd -> own cache entry
    reader.invalidate(2)
    reader.read(2)
    assert source.calls == 8
    reader.read(1)
    assert source.calls == 8  # invalidate(2) left hwnd 1's cache intact


def test_cached_reads_return_copies() -> None:
    tree = [FakeNode("Button", name="A", rect=(0, 0, 1, 1), patterns=frozenset({"Invoke"}))]
    reader = UIAReader(source=FakeSource([tree]), clock=lambda: 100.0)
    reader.read(1)[0].name = "mutated"

    assert reader.read(1)[0].name == "A"


def test_empty_tree_reads_empty() -> None:
    assert UIAReader(source=FakeSource([[]])).read(1) == []


@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_read_real_notepad_manual() -> None:
    pack = WindowsPack()
    before = set(top_level_windows())
    assert pack.open_app("notepad").ok
    hwnd = wait_new_window(before, "notepad", timeout=15.0)
    assert hwnd, "no new Notepad window appeared"
    try:
        elements = UIAReader(source=UiaAutomationSource()).read(hwnd)
        assert len(elements) >= 10, f"only {len(elements)} interactable elements"
        # Win11's new Notepad exposes the text area as a Document ('Text editor')
        # carrying the Value pattern; classic Notepad used an Edit control.
        assert any(e.role in {"Edit", "Document"} and "set_value" in e.actions for e in elements)
        assert any(e.role == "Button" for e in elements)
    finally:
        assert close_window(hwnd)


@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows UIA")
def test_invalid_hwnd_raises_perception_error_manual() -> None:
    with pytest.raises(PerceptionError):
        UiaAutomationSource().walk(0xDEADBEEF)
