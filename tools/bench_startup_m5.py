"""M5 DoD bench: frozen-bundle cold start + idle memory (Task 7, OQ-9).

    uv run python tools/bench_startup_m5.py --exe dist/Marshmellow/Marshmellow.exe --runs 3

Per run: spawn the bundle exe (GUI mode — the honest boot: bundled ct2 STT,
wake watcher, hotkey, piper worker subprocess), tail the NEWEST
``%LOCALAPPDATA%/Marshmellow`` session JSONL for the "Marshmellow … started"
line (= cold start), wait the settle window, then sample psutil RSS of the app
process AND its children — the piper worker is a separate process, and the
≤ 400 MB DoD target is checked against the SUM. Writes
``docs/plan/spikes/data/m5_startup.json``.

Targets (M5 DoD): cold-start median ≤ 10 s, idle RSS sum ≤ 400 MB. A miss is
RECORDED with the tuning levers (lazy STT load, deferred piper spawn) — a
measured decision, never silently tuned.

The spawn seeds a minimal config through ``MARSHMELLOW_CONFIG`` (the app's
documented env override): there is no product config on a fresh account and
the onboarding wizard would otherwise block the boot. That seed mirrors the
post-onboarding state (activation = both → wake watcher + hotkey loaded).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(REPO_ROOT))) / "Marshmellow" / "logs"
OUT_PATH = REPO_ROOT / "docs" / "plan" / "spikes" / "data" / "m5_startup.json"

COLD_START_TARGET_S = 10.0
IDLE_RSS_TARGET_MB = 400.0

_SEED_CONFIG = """\
# bench seed: the post-onboarding state a clean account reaches after the
# wizard (activation chosen, so the boot loads wake + hotkey). Never installed.
activation = "both"
"""


def _started_line_since(spawn_wall: float) -> str | None:
    """First "Marshmellow … started" row in a session file created at/after spawn."""
    if not LOG_DIR.exists():
        return None
    for f in sorted(LOG_DIR.glob("session-*.jsonl"), key=lambda p: p.stat().st_mtime):
        if f.stat().st_mtime < spawn_wall - 2.0:
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if '"msg": "Marshmellow ' in line and " started (" in line:
                    return line
        except OSError:
            continue
    return None


def _rss_tree_mb(pid: int) -> tuple[float, float, list[int]]:
    import psutil

    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    app_mb = parent.memory_info().rss / (1024 * 1024)
    child_mb = sum(c.memory_info().rss for c in children) / (1024 * 1024)
    return app_mb, child_mb, [c.pid for c in children]


def _kill_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for child in parent.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        parent.terminate()
    except psutil.NoSuchProcess:
        pass
    _, alive = psutil.wait_procs([parent, *parent.children(recursive=True)], timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def run_bench(exe: Path, runs: int, settle_s: float, startup_timeout_s: float) -> dict:
    import psutil  # noqa: F401 — fail fast on the dev-only dep before any spawn

    cfg = REPO_ROOT / "build" / "bench-config.toml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(_SEED_CONFIG, encoding="utf-8")

    results: list[dict] = []
    for i in range(1, runs + 1):
        env = {**os.environ, "MARSHMELLOW_CONFIG": str(cfg)}
        spawn_wall = time.time()
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            [str(exe)], env=env, cwd=str(exe.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        started_line, cold_s = None, None
        deadline = time.monotonic() + startup_timeout_s
        while time.monotonic() < deadline:
            line = _started_line_since(spawn_wall)
            if line is not None:
                started_line = line
                cold_s = time.perf_counter() - t0
                break
            if proc.poll() is not None:
                break  # died before starting
            time.sleep(0.1)
        if started_line is None:
            _kill_tree(proc.pid)
            results.append({"run": i, "error": f"no started line within {startup_timeout_s}s",
                            "exit_code": proc.poll()})
            continue
        time.sleep(settle_s)  # idle settle: first-utterance piper spawn + model loads calm down
        app_mb, child_mb, child_pids = _rss_tree_mb(proc.pid)
        results.append({
            "run": i,
            "cold_start_s": round(cold_s, 2),
            "rss_app_mb": round(app_mb, 1),
            "rss_children_mb": round(child_mb, 1),
            "rss_sum_mb": round(app_mb + child_mb, 1),
            "child_pids": child_pids,
        })
        _kill_tree(proc.pid)
        time.sleep(2.0)  # let handles close before the next spawn

    good = [r for r in results if "cold_start_s" in r]
    summary = {
        "exe": str(exe),
        "runs": results,
        "median_cold_start_s": sorted(r["cold_start_s"] for r in good)[len(good) // 2] if good else None,
        "max_rss_sum_mb": max((r["rss_sum_mb"] for r in good), default=None),
        "targets": {"cold_start_s_max": COLD_START_TARGET_S, "idle_rss_sum_mb_max": IDLE_RSS_TARGET_MB},
    }
    if good:
        summary["cold_start_within_target"] = summary["median_cold_start_s"] <= COLD_START_TARGET_S
        summary["idle_rss_within_target"] = summary["max_rss_sum_mb"] <= IDLE_RSS_TARGET_MB
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 frozen-bundle startup/memory bench")
    parser.add_argument("--exe", type=Path, required=True, help="path to dist/Marshmellow/Marshmellow.exe")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--settle-s", type=float, default=60.0, help="idle settle before the RSS sample")
    parser.add_argument("--startup-timeout-s", type=float, default=90.0)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    if not args.exe.exists():
        raise SystemExit(f"exe not found: {args.exe} — build first (installer/marshmellow.spec)")

    summary = run_bench(args.exe, args.runs, args.settle_s, args.startup_timeout_s)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if summary.get("median_cold_start_s") is None:
        return 1
    verdict = "PASS" if summary["cold_start_within_target"] and summary["idle_rss_within_target"] else "MISS"
    print(f"\nverdict: {verdict} (cold {summary['median_cold_start_s']}s / rss {summary['max_rss_sum_mb']}MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
