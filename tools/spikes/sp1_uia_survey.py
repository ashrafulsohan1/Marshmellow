"""SP1 - UIA coverage survey over a corpus of Windows apps.

Launches each target app, locates its NEW top-level window (never touches
pre-existing windows), waits for the UIA tree to settle (UWP apps load late),
walks the tree, and measures element counts, walk time, tree depth.
Writes docs/plan/spikes/data/sp1_survey.json (name overridable via SP1_OUT;
targets filterable via SP1_ONLY="notepad,calculator").

Run: tools/spikes/.venv/Scripts/python.exe tools/spikes/sp1_uia_survey.py
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import uiautomation as auto

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
DATA.mkdir(parents=True, exist_ok=True)
OUT = DATA / os.environ.get("SP1_OUT", "sp1_survey.json")

WM_CLOSE = 0x0010

INTERACTABLE = {
    "ButtonControl", "MenuItemControl", "ListItemControl", "ComboBoxControl",
    "EditControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl",
    "TabItemControl", "SliderControl", "SpinnerControl", "ToggleButtonControl",
    "SplitButtonControl",
}

TARGETS = [
    {"name": "notepad", "launch": ["notepad.exe"], "match": ["notepad"]},
    {"name": "calculator", "launch": ["calc.exe"], "match": ["calculator"]},
    {"name": "explorer", "launch": ["explorer.exe", "shell:Downloads"],
     "match": ["file explorer", "downloads"]},
    {"name": "edge", "launch": ["cmd", "/c", "start", "msedge", "about:blank"],
     "match": ["microsoft edge", "about:blank"]},
    {"name": "settings", "launch": ["cmd", "/c", "start", "ms-settings:"],
     "match": ["settings"]},
]


def top_windows() -> dict[int, str]:
    out = {}
    try:
        for w in auto.GetRootControl().GetChildren():
            out[w.NativeWindowHandle] = (w.Name or "")
    except Exception:
        pass
    return out


def find_new_window(match: list[str], before: set[int], timeout: float = 20.0):
    """Return (hwnd, title) of a window that appeared after `before` was taken."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, name in top_windows().items():
            if hwnd in before:
                continue
            if any(m in name.lower() for m in match):
                return hwnd, name
        time.sleep(0.5)
    return None


def walk(root, max_nodes: int = 8000):
    counts: dict[str, int] = {}
    samples: list[str] = []
    total = 0
    max_depth = 0
    t0 = time.perf_counter()
    stack = [(root, 0)]
    while stack and total < max_nodes:
        ctrl, depth = stack.pop()
        total += 1
        max_depth = max(max_depth, depth)
        try:
            ct = ctrl.ControlTypeName
            counts[ct] = counts.get(ct, 0) + 1
            if ct in INTERACTABLE and len(samples) < 12:
                label = (ctrl.Name or "").strip()[:40]
                samples.append(f"{ct[:-7]}:{label}")
        except Exception:
            counts["<error>"] = counts.get("<error>", 0) + 1
        if depth < 35:
            try:
                stack.extend((c, depth + 1) for c in ctrl.GetChildren())
            except Exception:
                pass
    walk_ms = int((time.perf_counter() - t0) * 1000)
    return counts, samples, total, max_depth, walk_ms


def settle_walk(hwnd, min_settle: float = 1.5, max_settle: float = 12.0,
                threshold: int = 15):
    """UWP/Store apps expose an empty frame first; poll until the tree is rich
    or stable. Returns the best (interactable, counts, samples, total, depth, ms)."""
    ctrl = auto.ControlFromHandle(hwnd)
    best = None
    prev, stable = -1, 0
    deadline = time.time() + max_settle
    while True:
        time.sleep(min_settle)
        counts, samples, total, depth, ms = walk(ctrl)
        inter = sum(n for t, n in counts.items() if t in INTERACTABLE)
        cand = (inter, counts, samples, total, depth, ms)
        if best is None or inter >= best[0]:
            best = cand
        if inter >= threshold or time.time() > deadline:
            break
        stable = stable + 1 if inter == prev else 0
        prev = inter
        if stable >= 2:
            break
    return best


def main() -> None:
    auto.SetGlobalSearchTimeout(15)
    only = [s for s in os.environ.get("SP1_ONLY", "").split(",") if s]
    results = []
    for tgt in TARGETS:
        if only and tgt["name"] not in only:
            continue
        rec: dict = {"name": tgt["name"], "launch": tgt["launch"]}
        pre = set(top_windows())
        try:
            subprocess.Popen(tgt["launch"])
        except OSError as exc:
            rec.update(found=False, error=f"launch failed: {exc}")
            results.append(rec)
            print(f"[{tgt['name']}] LAUNCH FAILED: {exc}")
            continue
        hit = find_new_window(tgt["match"], pre)
        if hit is None:
            rec.update(found=False, error="no NEW window matched within 20s "
                                          "(pre-existing windows are never touched)")
            results.append(rec)
            print(f"[{tgt['name']}] SKIPPED: no new window")
            continue
        hwnd, title = hit
        inter, counts, samples, total, depth, ms = settle_walk(hwnd)
        rec.update(
            found=True, title=title, hwnd=hwnd, total_elements=total,
            interactable=inter, walk_ms=ms, max_depth=depth,
            by_type=counts, sample_interactable=samples,
            verdict=("uia_usable" if inter >= 10 else "sparse" if inter >= 1 else "blind"),
        )
        results.append(rec)
        print(f"[{tgt['name']}] {rec['verdict']}: interactable={inter} "
              f"total={total} walk={ms}ms depth={depth} title={title!r}")
        try:  # close only the window we just opened
            ctypes.windll.user32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        time.sleep(1.0)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "interactable_types": sorted(INTERACTABLE),
        "method": "new-window-only; settle-poll for UWP content",
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
