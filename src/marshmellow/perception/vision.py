"""VisionPerceiver — SoM vision fallback for UIA-blind surfaces (01 §3.6, D5).

Set-of-Marks, adopted as an *idea* from Agent S (04-tooling §2, not code): the
foreground window is captured (mss), overlaid with a deterministic GRID×GRID
numbered tile grid (Pillow, pure), and the marked PNG + a versioned JSON prompt
go to the vision-capable LLM. The model answers "which tile holds which
interactive element"; tile rects translate into screen coordinates, so vision
Elements merge with UIA Elements and dedupe by bbox IoU (snapshot.py).

Vision is FALLBACK-only (D5): snapshot.py decides when to call perceive() —
never the other way round. Screenshot bytes ride along as `last_screenshot` /
`last_marked_image` for the snapshot to persist on the vision path (§3.15:
active-task-only, purged at task end).

The provider call is async (LLMProvider contract) while perceive() is sync on
the planner worker thread; a dedicated resident loop bridges the two. Same
rationale as planner.orchestrator: providers hold pooled HTTP clients bound to
the loop they first run on, so a fresh loop per call would hand the pool a dead
loop on the second call.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import importlib.resources
import io
import json
import re
import sys
import threading
from collections.abc import Callable
from typing import Final

import mss
from PIL import Image, ImageDraw

from marshmellow.core.contracts import (
    Element,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    PerceptionHint,
    Rect,
)
from marshmellow.llm.base import LLMProvider
from marshmellow.perception import PerceptionError

GRID: Final = 6  # SoM: deterministic 6x6 numbered tiles over the window rect
VISION_PROMPT_VERSION: Final = "v1"
VISION_TIMEOUT_S: Final = 30.0
DEFAULT_QUERY: Final = "Identify the interactive UI elements in this screenshot."

_GRID_COLOR: Final[tuple[int, int, int]] = (255, 64, 64)
_LABEL_BG: Final[tuple[int, int, int]] = (255, 214, 0)
_LABEL_FG: Final[tuple[int, int, int]] = (0, 0, 0)


def load_vision_prompt(version: str = VISION_PROMPT_VERSION) -> str:
    """Load a versioned vision prompt from ``perception/prompts/`` (§3.6)."""
    ref = (
        importlib.resources.files("marshmellow")
        / "perception"
        / "prompts"
        / f"vision_{version}.md"
    )
    return ref.read_text(encoding="utf-8")


def _tile_grid(width: int, height: int) -> list[Rect]:
    """The GRID×GRID tiles of a (width, height) image, numbered row-major 1..36."""
    tile_w, tile_h = max(1, width // GRID), max(1, height // GRID)
    tiles: list[Rect] = []
    for row in range(GRID):
        for col in range(GRID):
            x0, y0 = col * tile_w, row * tile_h
            x1 = width if col == GRID - 1 else (col + 1) * tile_w
            y1 = height if row == GRID - 1 else (row + 1) * tile_h
            tiles.append(Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0))
    return tiles


def _label_tile(draw: ImageDraw.ImageDraw, tile: Rect, text: str) -> None:
    """Draw the tile number on a small filled badge (skipped on degenerate tiles)."""
    box = draw.textbbox((0, 0), text)
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    pad = 1
    x1, y1 = tile.x + 1, tile.y + 1
    x2 = min(tile.x + tile.w - 1, x1 + text_w + 2 * pad)
    y2 = min(tile.y + tile.h - 1, y1 + text_h + 2 * pad)
    if x2 <= x1 or y2 <= y1:
        return
    draw.rectangle([x1, y1, x2, y2], fill=_LABEL_BG)
    draw.text((x1 + pad - box[0], y1 + pad - box[1]), text, fill=_LABEL_FG)


def _mss_capture(rect: Rect) -> bytes:
    """Default capture seam: mss grab of the screen rect -> PNG bytes.

    Blocking GDI work — planner worker thread only (M3 Review Focus 5). A fresh
    mss instance per call keeps the object thread-bound-safe; captures are rare
    (fallback path only, D5).
    """
    with mss.mss() as sct:
        shot = sct.grab({"left": rect.x, "top": rect.y, "width": rect.w, "height": rect.h})
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_DPI_ENSURED = False


def _ensure_dpi_awareness() -> None:
    """Per-monitor-v2 DPI awareness so GetWindowRect and mss agree on physical pixels."""
    global _DPI_ENSURED
    if _DPI_ENSURED:
        return
    _DPI_ENSURED = True
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001 — best effort; pre-1703 Windows or already set
        pass


def _foreground_window_rect() -> Rect | None:
    """Screen-pixel rect of the foreground window; None off-Windows or on failure."""
    if sys.platform != "win32":
        return None
    import ctypes.wintypes  # Windows-only module (stdlib); imported lazily

    _ensure_dpi_awareness()
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return Rect(x=rect.left, y=rect.top, w=width, h=height)


def _extract_json(text: str) -> dict[str, object] | None:
    """Pull the first {...} object out of a reply (tolerates fences and prose)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = match.group(0) if match else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _as_tile_number(raw: object, count: int) -> int | None:
    """Model tile -> 1..count int, or None (bools, junk, out-of-range dropped)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        tile = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        tile = int(raw.strip())
    else:
        return None
    return tile if 1 <= tile <= count else None


def _parse_elements(text: str, screen_tiles: list[Rect]) -> list[Element]:
    """Defensive parse: malformed JSON or entries -> fewer Elements, never a crash."""
    data = _extract_json(text)
    if data is None:
        return []
    raw = data.get("elements")
    if not isinstance(raw, list):
        return []
    out: list[Element] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tile = _as_tile_number(item.get("tile"), len(screen_tiles))
        if tile is None:
            continue
        value = item.get("value")
        out.append(
            Element(
                ref=f"v{len(out) + 1}",
                role=str(item.get("role", "")).strip() or "other",
                name=str(item.get("name", "")).strip(),
                value=None if value in (None, "") else str(value),
                bbox=screen_tiles[tile - 1],
                actions={"read"},
                source="vision",
            )
        )
    return out


class VisionPerceiver:
    """Capture -> SoM annotate -> vision LLM -> Element[] with tile bboxes."""

    def __init__(
        self,
        provider: LLMProvider,
        capture: Callable[[Rect], bytes] | None = None,
        *,
        foreground_rect: Callable[[], Rect | None] | None = None,
    ) -> None:
        self._provider = provider
        self._capture = capture if capture is not None else _mss_capture
        self._foreground_rect = (
            foreground_rect if foreground_rect is not None else _foreground_window_rect
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        # Raw + annotated bytes of the LAST perceive() call; snapshot.py persists
        # them on the vision path (§3.15) — never written by this class itself.
        self.last_screenshot: bytes | None = None
        self.last_marked_image: bytes | None = None

    def annotate(self, png: bytes) -> tuple[bytes, list[Rect]]:
        """Pure SoM overlay: same-size PNG with the GRID×GRID grid + tile numbers.

        Returns the annotated PNG bytes and the GRID² tile rects in IMAGE
        coordinates (0,0-based); perceive() translates them onto the screen.
        """
        try:
            # Image.open returns ImageFile; convert() below rebinds to Image —
            # annotate the variable at the wider type so the reassignment type-checks.
            img: Image.Image = Image.open(io.BytesIO(png))
            img.load()
        except Exception as exc:
            raise PerceptionError(f"unreadable screenshot: {exc}") from exc
        img = img.convert("RGB")
        width, height = img.size
        tiles = _tile_grid(width, height)
        draw = ImageDraw.Draw(img)
        tile_w, tile_h = max(1, width // GRID), max(1, height // GRID)
        for i in range(1, GRID):
            draw.line([(i * tile_w, 0), (i * tile_w, height)], fill=_GRID_COLOR, width=1)
            draw.line([(0, i * tile_h), (width, i * tile_h)], fill=_GRID_COLOR, width=1)
        for number, tile in enumerate(tiles, start=1):
            _label_tile(draw, tile, str(number))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), tiles

    def perceive(self, hint: PerceptionHint | None) -> list[Element]:
        """One vision pass over the foreground window; D5 fallback contract above."""
        rect = self._foreground_rect()
        if rect is None:
            raise PerceptionError("no foreground window to capture")
        try:
            png = self._capture(rect)
        except PerceptionError:
            raise
        except Exception as exc:
            raise PerceptionError(f"window capture failed: {exc}") from exc
        marked, tiles = self.annotate(png)  # PerceptionError on unreadable capture
        self.last_screenshot = png
        self.last_marked_image = marked
        request = self._request(hint, marked)
        try:
            response = self._complete(request)
        except PerceptionError:
            raise
        except Exception as exc:
            raise PerceptionError(f"vision provider failed: {exc!r}") from exc
        screen_tiles = [
            Rect(x=rect.x + t.x, y=rect.y + t.y, w=t.w, h=t.h) for t in tiles
        ]
        return _parse_elements(response.text, screen_tiles)

    def _request(self, hint: PerceptionHint | None, marked: bytes) -> LLMRequest:
        query = hint.query if hint is not None and hint.query else DEFAULT_QUERY
        data_url = "data:image/png;base64," + base64.b64encode(marked).decode("ascii")
        return LLMRequest(
            messages=[
                LLMMessage(role="system", content=load_vision_prompt()),
                LLMMessage(role="user", content=query, images=[data_url]),
            ]
        )

    def _complete(self, request: LLMRequest) -> LLMResponse:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._provider.complete(request), loop)
        try:
            return future.result(VISION_TIMEOUT_S)
        except TimeoutError as exc:  # concurrent.futures.TimeoutError (3.11+ alias)
            future.cancel()
            raise PerceptionError(
                f"vision provider timed out after {VISION_TIMEOUT_S:.0f}s"
            ) from exc

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """The one loop this perceiver ever uses (see module docstring)."""
        with self._loop_lock:
            if (
                self._loop is None
                or self._loop.is_closed()
                or (self._loop_thread is not None and not self._loop_thread.is_alive())
            ):
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="vision-provider-loop", daemon=True
                )
                thread.start()
                self._loop = loop
                self._loop_thread = thread
            assert self._loop is not None
            return self._loop
