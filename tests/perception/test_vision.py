"""VisionPerceiver unit tests — SoM annotation + defensive VLM parsing, no real calls.

The provider is scripted (canned JSON), the capture and foreground-rect seams
are injected; only the `manual` tier touches real mss/win32 (real Notepad).
"""

from __future__ import annotations

import base64
import io
import json
import sys

import pytest
from PIL import Image, ImageDraw

from marshmellow.core.contracts import (
    LLMRequest,
    LLMResponse,
    PerceptionHint,
    ProviderCaps,
    Rect,
)
from marshmellow.perception import PerceptionError
from marshmellow.perception.vision import (
    GRID,
    VISION_PROMPT_VERSION,
    VisionPerceiver,
    load_vision_prompt,
)


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


def _png(w: int = 600, h: int = 400) -> bytes:
    img = Image.new("RGB", (w, h), (40, 44, 52))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 80, 50], fill=(70, 130, 180))
    draw.ellipse([w - 100, h - 100, w - 30, h - 30], fill=(200, 90, 90))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _window() -> Rect:
    return Rect(x=100, y=50, w=600, h=400)


def _perceiver(provider: ScriptedVisionProvider) -> VisionPerceiver:
    return VisionPerceiver(
        provider=provider,
        capture=lambda rect: _png(rect.w, rect.h),
        foreground_rect=_window,
    )


# -- annotate: deterministic 6x6 SoM overlay (pure, no capture) -----------------


def test_annotate_returns_same_size_image_and_36_tiles() -> None:
    png = _png(600, 400)
    marked, tiles = VisionPerceiver(provider=ScriptedVisionProvider()).annotate(png)

    assert len(tiles) == GRID * GRID == 36
    img = Image.open(io.BytesIO(marked))
    assert img.size == (600, 400)
    assert marked != png  # visibly annotated


def test_tile_grid_partitions_the_image_deterministically() -> None:
    tiles = VisionPerceiver(provider=ScriptedVisionProvider()).annotate(_png(612, 400))[1]

    assert tiles[0] == Rect(x=0, y=0, w=102, h=66)
    assert tiles[35] == Rect(x=510, y=330, w=102, h=70)  # last row takes the remainder
    assert sum(t.w for t in tiles[:6]) == 612
    assert sum(t.h for t in tiles[::6]) == 400


def test_annotate_rejects_unreadable_png() -> None:
    with pytest.raises(PerceptionError):
        VisionPerceiver(provider=ScriptedVisionProvider()).annotate(b"not a png")


# -- perceive: capture -> annotate -> versioned prompt -> defensive parse -------


def test_perceive_maps_model_tiles_to_vision_elements() -> None:
    provider = ScriptedVisionProvider(
        json.dumps(
            {
                "elements": [
                    {"tile": 8, "role": "button", "name": "Play"},
                    {"tile": 15, "role": "text box", "name": "search", "value": "hello"},
                ]
            }
        )
    )
    elements = _perceiver(provider).perceive(PerceptionHint(query="find the play button"))

    assert provider.calls == 1
    assert [e.ref for e in elements] == ["v1", "v2"]
    assert all(e.source == "vision" for e in elements)
    assert all(e.actions == {"read"} for e in elements)
    _, tiles = VisionPerceiver(provider=ScriptedVisionProvider()).annotate(_png(600, 400))
    assert elements[0].bbox == Rect(
        x=100 + tiles[7].x, y=50 + tiles[7].y, w=tiles[7].w, h=tiles[7].h
    )
    assert elements[1].value == "hello"


def test_perceive_sends_versioned_prompt_and_marked_image() -> None:
    provider = ScriptedVisionProvider(json.dumps({"elements": []}))
    _perceiver(provider).perceive(PerceptionHint(query="where is save"))

    req = provider.requests[0]
    assert req.messages[0].role == "system"
    assert req.messages[0].content == load_vision_prompt()
    user = req.messages[1]
    assert "where is save" in user.content
    assert user.images and user.images[0].startswith("data:image/png;base64,")
    raw = base64.b64decode(user.images[0].split(",", 1)[1])
    Image.open(io.BytesIO(raw))  # the carried image is a decodable PNG


def test_perceive_defaults_query_without_hint() -> None:
    provider = ScriptedVisionProvider(json.dumps({"elements": []}))
    _perceiver(provider).perceive(None)

    user = provider.requests[0].messages[1]
    assert "interactive" in user.content
    assert provider.requests[0].messages[0].content == load_vision_prompt()


def test_perceive_malformed_json_returns_empty_list() -> None:
    provider = ScriptedVisionProvider("not json at all {oops")
    assert _perceiver(provider).perceive(None) == []


def test_perceive_strips_markdown_fences() -> None:
    text = 'Here you go:\n```json\n{"elements": [{"tile": 3, "role": "button", "name": "OK"}]}\n```'
    elements = _perceiver(ScriptedVisionProvider(text)).perceive(None)

    assert len(elements) == 1
    assert elements[0].name == "OK"


def test_perceive_drops_invalid_entries_and_keeps_refs_contiguous() -> None:
    text = json.dumps(
        {
            "elements": [
                "nope",
                {"tile": 99, "role": "button", "name": "out of range"},
                {"role": "button", "name": "no tile"},
                {"tile": True, "role": "button", "name": "bool tile"},
                {"tile": "12", "role": "link", "name": "docs"},
            ]
        }
    )
    elements = _perceiver(ScriptedVisionProvider(text)).perceive(None)

    assert [e.ref for e in elements] == ["v1"]
    assert elements[0].role == "link"
    assert elements[0].name == "docs"


def test_perceive_requires_a_foreground_window() -> None:
    perceiver = VisionPerceiver(
        provider=ScriptedVisionProvider("{}"),
        capture=lambda rect: _png(),
        foreground_rect=lambda: None,
    )
    with pytest.raises(PerceptionError):
        perceiver.perceive(None)


def test_perceive_wraps_provider_and_capture_failures() -> None:
    with pytest.raises(PerceptionError):
        _perceiver(ScriptedVisionProvider(error=RuntimeError("VLM down"))).perceive(None)

    def broken_capture(rect: Rect) -> bytes:
        raise ValueError("gdi blew up")

    with pytest.raises(PerceptionError):
        VisionPerceiver(
            provider=ScriptedVisionProvider("{}"),
            capture=broken_capture,
            foreground_rect=_window,
        ).perceive(None)


def test_perceive_keeps_raw_and_marked_bytes_for_snapshot() -> None:
    perceiver = _perceiver(ScriptedVisionProvider(json.dumps({"elements": []})))
    perceiver.perceive(None)

    assert perceiver.last_screenshot is not None
    assert perceiver.last_marked_image is not None
    assert perceiver.last_marked_image != perceiver.last_screenshot


def test_vision_prompt_is_versioned_and_json_contracted() -> None:
    assert VISION_PROMPT_VERSION == "v1"
    prompt = load_vision_prompt()
    assert "tile" in prompt and "JSON" in prompt
    assert load_vision_prompt(VISION_PROMPT_VERSION) == prompt


# -- manual tier: the real mss capture + win32 foreground rect ------------------


@pytest.mark.manual
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows capture")
def test_real_foreground_capture_smoke_manual() -> None:
    from marshmellow.perception.vision import _foreground_window_rect, _mss_capture

    rect = _foreground_window_rect()
    assert rect is not None and rect.w > 0 and rect.h > 0
    png = _mss_capture(rect)
    assert Image.open(io.BytesIO(png)).size == (rect.w, rect.h)

    provider = ScriptedVisionProvider(
        json.dumps({"elements": [{"tile": 36, "role": "button", "name": "anything"}]})
    )
    perceiver = VisionPerceiver(provider=provider)  # real capture + real foreground
    elements = perceiver.perceive(PerceptionHint(query="smoke check"))

    assert provider.calls == 1
    assert len(elements) == 1 and elements[0].source == "vision"
    assert perceiver.last_screenshot is not None
