"""Day report for the M5 dogfood DoD (Task 7): "zero crashes" becomes a grep.

    uv run python tools/dogfood_report.py --date 2026-11-08

Summarizes the session JSONLs of ONE day (``%LOCALAPPDATA%/Marshmellow/logs``,
sessions are selected by their start date — session-YYYYMMDD-*.jsonl):

- crash count        — rows with level=ERROR **and an exc payload** (a Python
  traceback; the DoD's "zero crashes"). Config ERRORs without a traceback
  (e.g. a hotkey degrade at startup) are counted separately, not as crashes.
- instructions       — "instruction:" (scripted submit) + "instruction
  submitted:" (voice-loop) rows.
- spoken results     — "spoken (<priority>, <type>):" rows (M5 T7 added the
  session-log line): acks = started, results = result/failed, monitor
  triggers = background priority (§3.12 unprompted reports).
- wake detections    — "wake detected (score=…)" rows.
- safety decisions   — gate verdicts ("safety decision: …").

Exit code 1 when crash count > 0, so the DoD check is scriptable. Rows older
than M5 (before the spoken/event log lines existed) simply contribute 0 to
those counters.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOGS = Path(os.environ.get("LOCALAPPDATA", str(REPO_ROOT))) / "Marshmellow" / "logs"

_SPOKEN_RE = re.compile(r"^spoken \((\w+), (\w+)\): ")
_WAKE_RE = re.compile(r"^wake detected \(score=([\d.]+)\)")
_MAX_EXCERPTS = 10


def _day_sessions(logs: Path, date: str) -> list[Path]:
    stamp = date.replace("-", "")  # session-YYYYMMDD-HHMMSS.jsonl
    return sorted(logs.glob(f"session-{stamp}-*.jsonl"))


def _rows(paths: list[Path]) -> Iterator[dict]:
    for f in paths:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        except OSError as e:
            print(f"warning: unreadable {f}: {e}")


def summarize(paths: list[Path]) -> dict:
    crashes: list[dict] = []
    counts = {
        "errors_without_traceback": 0,
        "warnings": 0,
        "instructions": 0,
        "spoken_acks": 0,
        "spoken_results": 0,
        "monitor_triggers": 0,
        "wake_detections": 0,
        "planner_turns": 0,
        "safety_decisions": 0,
    }
    sessions = 0
    session_minutes: list[float] = []
    for row in _rows(paths):
        level, msg = row.get("level", ""), row.get("msg", "")
        if level == "ERROR":
            exc = row.get("exc")
            if exc:
                crashes.append({"ts": row.get("ts"), "name": row.get("name"),
                                "msg": msg, "excerpt": exc.splitlines()[0][:160]})
            else:
                counts["errors_without_traceback"] += 1
        elif level == "WARNING":
            counts["warnings"] += 1
        if msg.startswith(("instruction:", "instruction submitted:")):
            counts["instructions"] += 1
        elif m := _SPOKEN_RE.match(msg):
            priority, kind = m.group(1), m.group(2)
            if priority == "background":
                counts["monitor_triggers"] += 1
            elif kind == "started":
                counts["spoken_acks"] += 1
            else:
                counts["spoken_results"] += 1
        elif _WAKE_RE.match(msg):
            counts["wake_detections"] += 1
        elif msg.startswith("task classified:"):
            counts["planner_turns"] += 1
        elif msg.startswith("safety decision:"):
            counts["safety_decisions"] += 1
        elif msg == "voice loop started":
            sessions += 1
    # session wall time: last − first ts per session file (ISO ms, same-day)
    for f in paths:
        stamps = [json.loads(l)["ts"] for l in f.read_text(encoding="utf-8").splitlines()
                  if l.strip() and "ts" in json.loads(l)]
        if len(stamps) >= 2:
            t0 = datetime.fromisoformat(stamps[0])
            t1 = datetime.fromisoformat(stamps[-1])
            session_minutes.append(round((t1 - t0).total_seconds() / 60.0, 1))

    return {
        "sessions": len(paths),
        "voice_loop_sessions": sessions,
        "session_minutes_total": round(sum(session_minutes), 1),
        "crash_count": len(crashes),
        "crashes": crashes[:_MAX_EXCERPTS],
        **counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a day's Marshmellow session logs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (session start date)")
    parser.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS)
    parser.add_argument("--json", type=Path, default=None, help="also write the summary as JSON")
    args = parser.parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")

    paths = _day_sessions(args.logs_dir, args.date)
    if not paths:
        print(f"no session logs for {args.date} in {args.logs_dir}")
        return 0
    summary = summarize(paths)
    print(f"dogfood report — {args.date} ({len(paths)} session file(s))")
    print(f"  crash count (ERROR + traceback): {summary['crash_count']}"
          + ("  ← DOD FAIL" if summary["crash_count"] else "  ← DoD PASS"))
    print(f"  errors without traceback:        {summary['errors_without_traceback']}")
    print(f"  warnings:                        {summary['warnings']}")
    print(f"  instructions:                    {summary['instructions']}")
    print(f"  spoken (ack / result / monitor): {summary['spoken_acks']} / "
          f"{summary['spoken_results']} / {summary['monitor_triggers']}")
    print(f"  wake detections:                 {summary['wake_detections']}")
    print(f"  planner turns / safety decisions: {summary['planner_turns']} / "
          f"{summary['safety_decisions']}")
    print(f"  session wall time (sum):         {summary['session_minutes_total']} min")
    for c in summary["crashes"]:
        print(f"    CRASH {c['ts']} {c['name']}: {c['msg']} — {c['excerpt']}")
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"  written: {args.json}")
    return 1 if summary["crash_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
