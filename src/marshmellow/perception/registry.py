"""Element registry — refs -> live handles with re-validation (01-architecture §3.6).

Refs are valid for the task lifetime and refreshed each snapshot (register).
Before acting, Task 3's ActionLayer revalidates a stale ref against a fresh
walk: runtime-id match first (stable across window moves), then role + name +
bbox IoU >= 0.5 as the fallback. Element gone -> None -> "re-snapshot", never a
coordinate guess on a stale bbox (M3 Review Focus 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from marshmellow.core.contracts import Element, Rect
from marshmellow.perception.uia_reader import UiaNode

IOU_MATCH_THRESHOLD: Final[float] = 0.5


@dataclass
class ElementHandle:
    """A resolved ref: the Element as walked plus its live UIA provenance.

    `hwnd` is the window the walk came from — the window-identity anchor for
    the ActionLayer's revalidation (M3 focus-change race): re-walking the
    element's OWN window keeps a focus change to another app or another
    instance of the same app from diverting or mis-matching the action.
    """

    ref: str
    element: Element
    runtime_id: str
    node: UiaNode | None = None
    hwnd: int | None = None


def bbox_iou(a: Rect, b: Rect) -> float:
    """Intersection-over-union of two (x, y, w, h) rects; 0.0 for empty unions."""
    inter_w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    inter_h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    inter = max(0, inter_w) * max(0, inter_h)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


class ElementRegistry:
    """ref -> element/runtime-id/node, replaced per snapshot, refreshable per walk."""

    def __init__(self) -> None:
        self._entries: dict[str, ElementHandle] = {}

    def register(
        self,
        elements: Sequence[Element],
        runtime_ids: Sequence[str],
        nodes: Sequence[UiaNode | None] | None = None,
        hwnd: int | None = None,
    ) -> None:
        """Replace the registry with one snapshot's elements.

        UIA refs carry a runtime id + live node per walk; vision refs (M3 Task 2)
        register with runtime_id="" and node=None — they revalidate by bbox only.
        `hwnd` stamps the window the walk came from (window-scoped revalidation).
        """
        self._entries = {
            h.ref: h for h in _combine(elements, runtime_ids, nodes, hwnd)
        }

    def resolve(self, ref: str) -> ElementHandle | None:
        return self._entries.get(ref)

    def revalidate(
        self,
        ref: str,
        elements: Sequence[Element],
        runtime_ids: Sequence[str],
        nodes: Sequence[UiaNode] | None = None,
    ) -> ElementHandle | None:
        """Match ref's registered element in the current walk and return it refreshed.

        Matching: runtime-id first (any bbox — a moved window keeps its runtime
        id), else role + name + bbox IoU >= IOU_MATCH_THRESHOLD. Returns a handle
        carrying the CURRENT walk's element/runtime-id/node (or None when the
        element is gone); the requested `ref` is kept so callers stay consistent.
        A successful match also refreshes the stored entry.
        """
        stored = self._entries.get(ref)
        if stored is None:
            return None
        fresh = _combine(elements, runtime_ids, nodes)
        matched = self._match(stored, fresh)
        if matched is None:
            return None
        refreshed = ElementHandle(
            ref=ref,
            element=matched.element,
            runtime_id=matched.runtime_id,
            node=matched.node,
            hwnd=stored.hwnd,
        )
        self._entries[ref] = refreshed
        return refreshed

    def rematch(self, handle: ElementHandle) -> ElementHandle | None:
        """Match a previously resolved handle against the CURRENT entries.

        Task 3's ActionLayer calls this right after a fresh snapshot() has
        re-registered the current walk (elements + runtime ids + nodes): the
        stale handle is matched by runtime id first (stable across window
        moves), then role + name + bbox IoU >= IOU_MATCH_THRESHOLD. Window
        identity is part of the match: candidates registered from a DIFFERENT
        window than the handle's never match, so another instance of the same
        app (identical names/bboxes) cannot satisfy a ref (M3 focus-change
        race). Returns the refreshed handle (current walk's element/runtime-id/
        node under the requested ref) or None (element gone). Unlike
        revalidate(), stored entries stay untouched: the fresh registration
        remains authoritative.
        """
        candidates = [
            entry
            for entry in self._entries.values()
            if handle.hwnd is None or entry.hwnd is None or entry.hwnd == handle.hwnd
        ]
        matched = self._match(handle, candidates)
        if matched is None:
            return None
        return ElementHandle(
            ref=handle.ref,
            element=matched.element,
            runtime_id=matched.runtime_id,
            node=matched.node,
            hwnd=matched.hwnd if matched.hwnd is not None else handle.hwnd,
        )

    def _match(self, stored: ElementHandle, fresh: list[ElementHandle]) -> ElementHandle | None:
        if stored.runtime_id:
            for candidate in fresh:
                if candidate.runtime_id == stored.runtime_id:
                    return candidate
        for candidate in fresh:
            element = candidate.element
            if (
                element.role == stored.element.role
                and element.name == stored.element.name
                and bbox_iou(stored.element.bbox, element.bbox) >= IOU_MATCH_THRESHOLD
            ):
                return candidate
        return None


def _combine(
    elements: Sequence[Element],
    runtime_ids: Sequence[str],
    nodes: Sequence[UiaNode | None] | None,
    hwnd: int | None = None,
) -> list[ElementHandle]:
    if len(elements) != len(runtime_ids):
        raise ValueError(
            f"elements/runtime_ids length mismatch: {len(elements)} != {len(runtime_ids)}"
        )
    if nodes is not None and len(nodes) != len(elements):
        raise ValueError(f"elements/nodes length mismatch: {len(elements)} != {len(nodes)}")
    return [
        ElementHandle(
            ref=element.ref,
            element=element,
            runtime_id=runtime_id,
            node=None if nodes is None else nodes[i],
            hwnd=hwnd,
        )
        for i, (element, runtime_id) in enumerate(zip(elements, runtime_ids, strict=True))
    ]
