"""M3 per-step latency bench (Task 8 Step 3) — measured on THIS machine.

Two measurements against the M2 per-agent-step budget of ≤ 3 s:

1. reference scenario per-step  — the scripted reference-check-channel scenario
   run headless through the REAL M3 stack (real ScreenPerception over a canned
   UIA tree, real SafetyGate + ConfirmFlow, real toolbox/orchestrator loop) with
   a timing wrapper on the scripted provider. Per-step wall = start of provider
   call k → start of call k+1 (the tools of step k execute in between); the
   last step's finish turn ends at the provider call's return. The scripted
   provider has ZERO RTT, so this measures the ENGINE-side per-step cost only:
   manager/prompt/dispatch overhead + UIA walk (settle-poll) + gate + act.
   The cloud RTT is additive per step and is NOT measured here — no BYOK key is
   stored on this machine (verified via secrets.load_api_key()); the first BYOK
   call doubles as the SP5 probe (OQ-2).

2. real Settings walk           — SP1 measured Settings walks up to 7.5 s vs
   Calculator 0.15 s; a cold get_elements runs the settle-poll (TWO agreeing
   walks), so a slow app can burst the 3 s per-step budget on its own. This
   probe launches ms-settings:about (the fixture_settings target), walks the
   REAL window 3× (invalidate between reads → median cold cost) and then reads
   back-to-back to show the (hwnd, 0.1 s tick) cache behavior. Read-only; the
   fixture context closes the window it opened. Requires Settings to be closed
   first (single-instance — it would navigate an existing window).

Writes docs/plan/spikes/data/m3_latency.json and prints a summary table.
Run: uv run python tools/latency_m3.py [--skip-settings]
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
SCENARIOS = ROOT / "tests" / "scenarios"

STEP_BUDGET_S = 3.0  # M2 latency budget: per agent step


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- 1


class _TimedProvider:
    """Wraps the scripted scenario provider, timestamping every complete()."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[tuple[float, float]] = []  # (start, end) per complete()

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def complete(self, req: object) -> object:
        start = time.perf_counter()
        response = await self._inner.complete(req)  # type: ignore[attr-defined]
        self.calls.append((start, time.perf_counter()))
        return response


def measure_reference_per_step() -> dict:
    """Per-step walls for the scripted reference scenario (engine-side only)."""
    from marshmellow.packs.base import ShellResult
    from marshmellow.testing.scenario import (
        load_scenario,
        load_snapshots,
        run_scripted,
        scripted_provider,
    )
    from marshmellow.tts.fake import FakeTTS

    class RecordingPack:
        """DEShell double: the reference script opens no apps; records anyway."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def open_app(self, target: str) -> ShellResult:
            self.calls.append(target)
            return ShellResult(ok=True, detail=f"would open {target}")

    spec = load_scenario(SCENARIOS / "reference-check-channel.yaml")
    inner = scripted_provider(spec)
    provider = _TimedProvider(inner)
    report = run_scripted(
        spec,
        snapshots=load_snapshots(spec.snapshots),
        provider=provider,  # type: ignore[arg-type]  — duck-typed timing wrapper
        pack=RecordingPack(),  # type: ignore[arg-type]
        tts=FakeTTS(),
    )
    if not report.ok:
        return {"ok": False, "error": f"reference scenario failed: {report}"}

    calls = provider.calls
    steps: list[dict] = []
    names = [call.name for call in inner.emitted]
    for i, (start, end) in enumerate(calls):
        if i + 1 < len(calls):
            wall = calls[i + 1][0] - start  # next provider call starts after this step's tools
            end_note = "→ next provider call"
        else:
            wall = end - start  # the finish turn: the task ends at this return
            end_note = "→ task end"
        steps.append({
            "step": i + 1,
            "tool": names[i] if i < len(names) else "?",
            "provider_dispatch_s": round(end - start, 4),
            "step_wall_s": round(wall, 4),
            "note": end_note,
        })
    worst = max(steps, key=lambda s: s["step_wall_s"])
    return {
        "ok": True,
        "note": (
            "scripted provider (0 ms RTT) → ENGINE-side per-step cost: "
            "dispatch + prompt build + tool execution (UIA settle-poll walk / "
            "gate / act) over the real stack, canned page. Cloud RTT is "
            "additive per step and unmeasured (no BYOK key stored — D7/OQ-2). "
            "Budget: per agent step ≤ 3 s (M2)."
        ),
        "steps": steps,
        "planner_window_total_s": round(calls[-1][1] - calls[0][0], 4),
        "worst_step_s": worst["step_wall_s"],
        "worst_step_tool": worst["tool"],
        "budget_s": STEP_BUDGET_S,
        "worst_step_within_budget": worst["step_wall_s"] <= STEP_BUDGET_S,
        "spoken": report.spoken,
        "trace": report.trace,
    }


# --------------------------------------------------------------------------- 2


def measure_settings_walk(cold_runs: int = 3) -> dict:
    """Cold + cached UIA walk cost of the REAL Settings About window.

    The settle-poll needs TWO agreeing walks, so `cold` here is the honest
    get_elements walk cost; the tick cache then dedupes repeats within one
    0.1 s tick (production: repeated reads of a single planner step). The pin
    wait exists for the same reason fixture_settings.yaml pins app state: SP1
    finding 2 — UWP apps report an agreeing EMPTY frame before content loads,
    and a settle-poll without the pin would happily measure that frame.
    """
    from marshmellow.packs.windows import WindowsPack
    from marshmellow.perception.uia_reader import UIAReader
    from marshmellow.testing.fixtures import FixtureApp, focus_window

    reader = UIAReader()
    colds: list[float] = []
    counts: list[int] = []
    with FixtureApp("settings", pack=WindowsPack(), launch="ms-settings:about") as fx:
        hwnd = fx.hwnd
        if hwnd is None:  # __enter__ guarantees it; narrows for the checker
            return {"ok": False, "error": "settings window vanished after launch"}
        focus_window(hwnd)
        fx.wait_element(hwnd, "Hyperlink", "system protection", timeout=20.0)
        for _ in range(cold_runs):
            reader.invalidate()
            start = time.perf_counter()
            elements = reader.read(hwnd)
            colds.append(time.perf_counter() - start)
            counts.append(len(elements))
            time.sleep(0.2)  # land the next cold read in a fresh tick
        repeats: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            reader.read(hwnd)
            repeats.append(time.perf_counter() - start)
    worst_cold = max(colds)
    return {
        "ok": True,
        "note": (
            "REAL ms-settings:about window, pinned on the 'system protection' "
            "hyperlink BEFORE walking (an unpinned settle-poll agrees on the "
            "pre-content empty frame — measured 0.04 s here before the fix). "
            "cold = invalidate + settle-poll (two agreeing walks); repeats = "
            "back-to-back reads — each spans several 0.1 s ticks, so they are "
            "new-tick re-walks, NOT cache hits (on slow apps the tick cache "
            "dedupes nothing across steps). SP1 corpus: Settings walks up to "
            "7.5 s (vs Calculator 0.15 s). Read-only; the fixture closes its "
            "window. Settings must be closed before running (single-instance)."
        ),
        "cold_walks_s": [round(c, 3) for c in colds],
        "cold_median_s": round(statistics.median(colds), 3),
        "cold_worst_s": round(worst_cold, 3),
        "element_count": counts[0] if counts else None,
        "repeat_reads_s": [round(r, 4) for r in repeats[1:]],
        "budget_s": STEP_BUDGET_S,
        "cold_median_within_budget": statistics.median(colds) <= STEP_BUDGET_S,
    }


def main() -> None:
    skip_settings = "--skip-settings" in sys.argv
    sections: dict[str, object] = {
        "reference_per_step": measure_reference_per_step,
    }
    if not skip_settings:
        sections["settings_walk"] = measure_settings_walk

    out: dict = {
        "generated": datetime.now(UTC).isoformat(),
        "machine_note": "user's Windows 11 desktop (the dev machine), CPU-only inference",
        "git_head": _git_head(),
    }
    for name, fn in sections.items():
        print(f"\n=== {name} ===", flush=True)
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — a bench must report, not crash
            result = {"ok": False, "error": repr(exc)}
        out[name] = result
        for key, value in result.items():  # type: ignore[union-attr]
            if key not in ("note",):
                print(f"  {key}: {value}", flush=True)

    dest = DATA / "m3_latency.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten: {dest}")


if __name__ == "__main__":
    main()
