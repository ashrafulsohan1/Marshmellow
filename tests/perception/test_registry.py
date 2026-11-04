"""ElementRegistry unit tests — register/resolve round trip + revalidate matching."""

from __future__ import annotations

import pytest

from marshmellow.core.contracts import Element, Rect
from marshmellow.perception.registry import ElementRegistry


def _el(ref: str, role: str = "Button", name: str = "OK",
        bbox: tuple[int, int, int, int] = (0, 0, 100, 50)) -> Element:
    x, y, w, h = bbox
    return Element(ref=ref, role=role, name=name, bbox=Rect(x=x, y=y, w=w, h=h),
                   actions={"invoke", "read"})


class FakeNode:
    """Stand-in for a live UIA node handle (identity matters, behavior doesn't)."""


def test_register_resolve_round_trip() -> None:
    element = _el("e1")
    node = FakeNode()
    registry = ElementRegistry()
    registry.register([element], ["rid-1"], [node])

    handle = registry.resolve("e1")
    assert handle is not None
    assert handle.ref == "e1"
    assert handle.element is element
    assert handle.runtime_id == "rid-1"
    assert handle.node is node
    assert registry.resolve("e9") is None


def test_register_without_nodes_sets_node_none() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1")], ["rid-1"])

    handle = registry.resolve("e1")
    assert handle is not None
    assert handle.node is None


def test_register_replaces_previous_snapshot() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1")], ["rid-1"])
    registry.register([_el("e1")], ["rid-2"])

    handle = registry.resolve("e1")
    assert handle is not None
    assert handle.runtime_id == "rid-2"


def test_register_rejects_mismatched_lengths() -> None:
    registry = ElementRegistry()
    with pytest.raises(ValueError):
        registry.register([_el("e1")], [])
    with pytest.raises(ValueError):
        registry.register([_el("e1")], ["rid-1"], [])


def test_revalidate_by_runtime_id_carries_new_bbox() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1", bbox=(0, 0, 100, 50))], ["rid-7"])

    moved = _el("e3", bbox=(500, 300, 100, 50))
    handle = registry.revalidate("e1", [moved], ["rid-7"])

    assert handle is not None
    assert handle.ref == "e1"
    assert handle.element.bbox == Rect(x=500, y=300, w=100, h=50)
    assert handle.runtime_id == "rid-7"
    # a successful revalidate also refreshes the stored entry
    stored = registry.resolve("e1")
    assert stored is not None
    assert stored.element.bbox == Rect(x=500, y=300, w=100, h=50)


def test_revalidate_falls_back_to_role_name_and_iou() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1", role="Edit", name="doc", bbox=(0, 0, 100, 50))], ["rid-1"])

    # runtime id gone; same role+name and IoU((0,0,100,50),(20,0,100,50)) = 0.667 >= 0.5
    moved = _el("e2", role="Edit", name="doc", bbox=(20, 0, 100, 50))
    handle = registry.revalidate("e1", [moved], ["rid-CHANGED"], [FakeNode()])

    assert handle is not None
    assert handle.element.bbox == Rect(x=20, y=0, w=100, h=50)
    assert handle.runtime_id == "rid-CHANGED"
    assert handle.node is not None


def test_revalidate_rejects_name_change_or_small_iou() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1", role="Edit", name="doc", bbox=(0, 0, 100, 50))], ["rid-1"])

    # IoU((0,0,100,50),(80,0,100,50)) = 0.111 < 0.5; same bbox but renamed
    shifted = _el("e2", role="Edit", name="doc", bbox=(80, 0, 100, 50))
    renamed = _el("e3", role="Edit", name="other", bbox=(0, 0, 100, 50))
    assert registry.revalidate("e1", [shifted, renamed], ["x", "y"]) is None


def test_revalidate_prefers_runtime_id_over_iou() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1", role="Button", name="A", bbox=(0, 0, 100, 50))], ["rid-1"])

    twin = _el("e2", role="Button", name="A", bbox=(0, 0, 100, 50))       # IoU 1.0, other rid
    moved = _el("e3", role="Button", name="B", bbox=(900, 900, 100, 50))  # rid match, no IoU
    handle = registry.revalidate("e1", [twin, moved], ["rid-x", "rid-1"])

    assert handle is not None
    assert handle.element.ref == "e3"


def test_revalidate_gone_element_returns_none() -> None:
    registry = ElementRegistry()
    registry.register([_el("e1")], ["rid-1"])

    assert registry.revalidate("e1", [], []) is None


def test_revalidate_unknown_ref_returns_none() -> None:
    registry = ElementRegistry()
    assert registry.revalidate("e1", [_el("e1")], ["rid-1"]) is None
