"""ScreenPerception merge tests — UIA-first, vision fallback, D5 dedupe (headless).

Uses the real VisionPerceiver with a scripted provider + injected capture and
foreground-rect seams, so the merge/purge logic runs against the genuine code.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass

import pytest
from PIL import Image, ImageDraw

from marshmellow.core.contracts import (
    LLMRequest,
    LLMResponse,
    PerceptionHint,
    ProviderCaps,
    Rect,
    WindowInfo,
)
from marshmellow.packs.windows import (
    WindowsPack,
    close_window,
    top_level_windows,
    wait_new_window,
)
from marshmellow.perception.registry import ElementRegistry
from marshmellow.perception.snapshot import FALLBACK_THRESHOLD, ScreenPerception
from marshmellow.perception.uia_reader import UiaAutomationSource, UiaNode, UIAReader
from marshmellow.perception.vision import VisionPerceiver

WINDOW = Rect(x=100, y=50, w=600, h=400)


@dataclass
class FakeNode:
    """Minimal UiaNode stand-in for scripted walks (mirrors test_uia_reader)."""

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


class ScriptedVisionProvider:
    """Fake LLMProvider: records requests, replies with canned text or raises."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self.calls = 0
        self._text = text
        self._error = error

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(vision=True, tool_call=False)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        return LLMResponse(text=self._text)


def _png(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (40, 44, 52))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 60], fill=(70, 130, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _perception(
    source: FakeSource,
    provider: ScriptedVisionProvider,
    registry: ElementRegistry | None = None,
) -> ScreenPerception:
    vision = VisionPerceiver(
        provider=provider,
        capture=lambda rect: _png(rect.w, rect.h),
        foreground_rect=lambda: WINDOW,
    )
    return ScreenPerception(
        UIAReader(source=source),
        registry if registry is not None else ElementRegistry(),
        foreground=lambda: WindowInfo(hwnd=4321, title="Fake App", app="fake"),
        vision=vision,
    )


def _rich_tree(n: int = 50) -> list[FakeNode]:
    return [
        FakeNode(
            "Button",
            name=f"button {i}",
            rect=(100 + (i % 10) * 12, 50 + (i // 10) * 12, 8, 8),
            patterns=frozenset({"Invoke"}),
            runtime_id=f"rid-{i}",
        )
        for i in range(n)
    ]


def _sparse_tree() -> list[FakeNode]:
    return [
        FakeNode("Button", name="Play", rect=(100, 50, 100, 66),
                 patterns=frozenset({"Invoke"}), runtime_id="rid-1"),
        FakeNode("Button", name="Stop", rect=(250, 120, 80, 60),
                 patterns=frozenset({"Invoke"}), runtime_id="rid-2"),
        FakeNode("Edit", name="search", value="", rect=(400, 200, 120, 80),
                 patterns=frozenset({"Value"}), runtime_id="rid-3"),
    ]


def _canned(*tiles: int) -> str:
    return json.dumps(
        {"elements": [{"tile": t, "role": "button", "name": f"tile {t}"} for t in tiles]}
    )


# -- (c) D5: rich tree -> zero provider calls; sparse tree -> vision merged -----


def test_rich_tree_never_calls_the_vision_provider() -> None:
    provider = ScriptedVisionProvider(_canned(36))
    snap = _perception(FakeSource([_rich_tree()]), provider).snapshot()

    assert provider.calls == 0
    assert len(snap.elements) == 50
    assert all(e.source == "uia" for e in snap.elements)
    assert snap.active_window is not None and snap.active_window.hwnd == 4321
    assert snap.screenshot is None and snap.marked_image is None


def test_threshold_boundary_is_exclusive() -> None:
    provider = ScriptedVisionProvider(_canned(36))
    snap = _perception(FakeSource([_rich_tree(FALLBACK_THRESHOLD)]), provider).snapshot()

    assert provider.calls == 0  # 10 elements is NOT < 10: no vision
    assert len(snap.elements) == FALLBACK_THRESHOLD


def test_sparse_tree_merges_vision_elements_and_sets_images() -> None:
    provider = ScriptedVisionProvider(_canned(36))
    registry = ElementRegistry()
    snap = _perception(FakeSource([_sparse_tree()]), provider, registry).snapshot()

    assert provider.calls == 1
    assert [e.ref for e in snap.elements if e.source == "uia"] == ["e1", "e2", "e3"]
    vision = [e for e in snap.elements if e.source == "vision"]
    assert len(vision) == 1 and vision[0].ref == "v1"
    # tile 36 of the 600x400 window at (100, 50): col 5 / row 5, last row takes remainder
    assert vision[0].bbox == Rect(x=600, y=380, w=100, h=70)
    assert snap.screenshot is not None and snap.screenshot.exists()
    assert snap.marked_image is not None and snap.marked_image.exists()
    # both kinds are addressable via the registry; vision has no UIA provenance
    assert registry.resolve("e1") is not None
    handle = registry.resolve("v1")
    assert handle is not None
    assert handle.node is None and handle.runtime_id == ""


# -- (d) hint.prefer_vision forces vision on a rich tree -------------------------


def test_prefer_vision_hint_forces_vision_on_rich_tree() -> None:
    provider = ScriptedVisionProvider(_canned(36))
    snap = _perception(FakeSource([_rich_tree()]), provider).snapshot(
        PerceptionHint(prefer_vision=True)
    )

    assert provider.calls == 1
    assert len([e for e in snap.elements if e.source == "uia"]) == 50
    assert any(e.source == "vision" for e in snap.elements)
    assert snap.screenshot is not None and snap.marked_image is not None


# -- (e) dedupe: bbox IoU > 0.5 with UIA preferred -------------------------------


def test_uia_element_wins_over_overlapping_vision_tile() -> None:
    provider = ScriptedVisionProvider(_canned(1, 36))  # tile 1 == exactly the e1 bbox
    snap = _perception(FakeSource([_sparse_tree()]), provider).snapshot()

    at_e1 = [e for e in snap.elements if e.bbox == Rect(x=100, y=50, w=100, h=66)]
    assert len(at_e1) == 1
    assert at_e1[0].source == "uia" and at_e1[0].ref == "e1"
    # the non-overlapping vision tile is still merged
    assert any(e.source == "vision" and e.name == "tile 36" for e in snap.elements)


# -- defensive paths -------------------------------------------------------------


def test_no_foreground_window_returns_empty_snapshot() -> None:
    provider = ScriptedVisionProvider(_canned(1))
    perception = ScreenPerception(
        UIAReader(source=FakeSource([[]])),
        ElementRegistry(),
        foreground=lambda: None,
        vision=VisionPerceiver(
            provider=provider,
            capture=lambda rect: _png(rect.w, rect.h),
            foreground_rect=lambda: WINDOW,
        ),
    )
    snap = perception.snapshot()

    assert snap.active_window is None
    assert snap.elements == []
    assert provider.calls == 0
    assert snap.screenshot is None


def test_vision_failure_falls_back_to_uia_only() -> None:
    provider = ScriptedVisionProvider(error=RuntimeError("VLM down"))
    snap = _perception(FakeSource([_sparse_tree()]), provider).snapshot()

    assert provider.calls == 1
    assert [e.ref for e in snap.elements] == ["e1", "e2", "e3"]
    assert snap.screenshot is None and snap.marked_image is None


def test_without_vision_component_sparse_tree_stays_uia_only() -> None:
    snap = ScreenPerception(
        UIAReader(source=FakeSource([_sparse_tree()])),
        ElementRegistry(),
        foreground=lambda: WindowInfo(hwnd=1, title="Fake App", app="fake"),
    ).snapshot()

    assert [e.ref for e in snap.elements] == ["e1", "e2", "e3"]
    assert snap.screenshot is None and snap.marked_image is None


# -- manual tier: real Notepad + real mss capture (provider scripted) ------------


@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows apps")
def test_real_notepad_snapshot_manual() -> None:
    import ctypes

    pack = WindowsPack()
    before = set(top_level_windows())
    assert pack.open_app("notepad").ok
    hwnd = wait_new_window(before, "notepad", timeout=15.0)
    assert hwnd, "no new Notepad window appeared"
    ctypes.windll.user32.SetForegroundWindow(hwnd)  # best-effort: aim the capture
    provider = ScriptedVisionProvider(_canned(1, 36))
    artifacts: list = []
    try:
        perception = ScreenPerception(
            UIAReader(source=UiaAutomationSource()),
            ElementRegistry(),
            foreground=lambda: WindowInfo(hwnd=hwnd, title="Notepad", app="notepad"),
            vision=VisionPerceiver(provider=provider),  # real mss + real foreground rect
        )
        snap = perception.snapshot()
        assert len(snap.elements) >= FALLBACK_THRESHOLD
        assert provider.calls == 0  # rich real tree: zero VLM calls (D5)
        assert snap.screenshot is None and snap.marked_image is None

        marked = perception.snapshot(PerceptionHint(prefer_vision=True))
        assert provider.calls == 1
        assert marked.screenshot is not None and marked.screenshot.exists()
        assert marked.marked_image is not None and marked.marked_image.exists()
        artifacts = [marked.screenshot, marked.marked_image]

        # window-rect capture: the raw bytes decode to the foreground window's size
        from marshmellow.perception.vision import _foreground_window_rect

        rect = _foreground_window_rect()
        assert rect is not None
        assert Image.open(marked.screenshot.open("rb")).size == (rect.w, rect.h)
    finally:
        assert close_window(hwnd)
        for path in artifacts:
            path.unlink(missing_ok=True)
