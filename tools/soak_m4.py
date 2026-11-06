"""M4 soak harness (Task 7) — real-clock soak of the real monitor framework.

Drives the REAL composition (``App(headless=True)`` → ``TaskManager`` →
``monitors.make_check`` → jittered scheduler, M4 Tasks 1–6) with the real
clock against a file-backed fixture feed: the harness writes the fixture
file, mutates it a few known times (the *intended changes*), and a
``file_hash`` monitor polls it on the jittered schedule. Monitors are
ONE-SHOT (a trigger completes the task, M4 pinned semantics), so the harness
re-arms a fresh monitor after every trigger — each re-armed monitor captures
the current content as its baseline, so the change it was re-armed for is
never re-reported (ledger: "swallowed").

Every ``TaskEvent`` lands as one JSONL row ``{at, task, changed, ok, summary,
error}``; the final stdout summary counts:

- trigger count     — ``result`` events from the monitor tasks,
- error count       — ``waiting``/``failed`` events (check errors; the
                      shutdown ``cancelled`` rows are reported separately),
- false-positive
  candidates        — fixture-task triggers whose source content hash maps to
                      an already-credited version or the initial baseline,
                      i.e. a trigger with UNCHANGED source.

``--feed URL_OR_FILE`` adds ONE real (harness-uncontrolled) monitor: an
``http(s)://`` URL becomes an ``rss`` monitor; a local path a second
``file_hash`` monitor. It re-arms the same way; its triggers count toward the
trigger total but never toward the false-positive analysis (no ground truth).

Isolation: all state lives in ``--run-dir`` (default
``<temp>/marshmellow-soak-m4``) — the app's task store, logs, the fixture
file, ``mutations.jsonl`` and the report. The previous run's task store is
cleared so ``manager.restore()`` restores nothing stale; the product's
``%LOCALAPPDATA%/Marshmellow`` is never touched.

Verdict (M4 DoD, recorded in 02-milestones.md): PASS iff there are zero
false-positive candidates AND every intended change is accounted for —
credited to exactly one trigger or swallowed by a re-arm baseline; FAIL
otherwise; INCOMPLETE when the run ended early.

Own hygiene policy: ``tools/`` is ruff-excluded, but this file holds itself to
the product standard anyway — typed, ≤ 100 cols, no bare excepts, locks on
shared state, atomic fixture writes (temp + ``os.replace``).

Run:
    uv run python tools/soak_m4.py --hours 24 --interval-s 300 \\
        [--feed URL_OR_FILE] [--report-jsonl PATH] [--run-dir PATH]
Smoke:
    uv run python tools/soak_m4.py --hours 0.01 --interval-s 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from marshmellow.app import App
from marshmellow.core.config import Settings
from marshmellow.core.contracts import MonitorSpec, TaskEvent, TaskSpec
from marshmellow.tasks.manager import TaskHandle

FIXTURE_TASK = "soak-fixture"
FEED_TASK = "soak-feed"
#: Upper bound on intended fixture changes per run; the schedule spacesthem
#: ≥ 3 poll intervals apart (a check can never straddle two changes) and
#: keeps a ≥ 10 % unchanged tail after the last one.
MAX_MUTATIONS = 6
#: Stdout progress cadence (the JSONL is the per-event record).
PROGRESS_S = 900.0
#: Max wait for a re-armed monitor's first check (its baseline is sealed then).
REARM_WAIT_S = 20.0

#: Pinned row schema: {at, task, changed, ok, summary, error}.
Row = dict[str, str | bool | None]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


class FixtureLedger:
    """Ground truth for the fixture feed: which content versions were written,
    which a trigger credited, and which a re-arm consumed as its baseline
    (thread-safe: the main thread re-arms, the bus-handler thread classifies
    triggers)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}  # sha -> version (0 = initial baseline)
        self._credited: set[str] = set()
        self._swallowed: set[str] = set()

    def record_write(self, version: int, sha: str) -> None:
        with self._lock:
            self._versions[sha] = version

    def credit_or_swallow(self, sha: str) -> str:
        """Called on a fixture trigger: 'expected' (credits a new version),
        'swallowed' (the version a re-arm consumed as baseline — it can only
        appear here through a framework anomaly), 'false-positive' (a version
        a trigger already reported, or the unchanged initial baseline), or
        'unknown' (a hash the harness never wrote — also a candidate)."""
        with self._lock:
            if sha in self._credited:
                return "false-positive"
            if sha in self._swallowed:
                return "swallowed"
            version = self._versions.get(sha)
            if version is None:
                return "unknown"
            if version == 0:
                return "false-positive"
            self._credited.add(sha)
            return "expected"

    def mark_swallowed(self, sha: str) -> None:
        """Seal the re-arm baseline (main thread, after the first check):
        an uncredited intended version consumed as a baseline can never
        trigger, so it is accounted here instead of counting as a shortfall."""
        with self._lock:
            if self._versions.get(sha, 0) >= 1 and sha not in self._credited:
                self._swallowed.add(sha)


class SoakRun:
    """One soak run: owns the report/mutations files, the ledger and counters."""

    def __init__(self, run_dir: Path, report_path: Path, intended: int) -> None:
        self.run_dir = run_dir
        self.intended = intended
        self.fixture_path = run_dir / "fixture_feed.txt"
        self.ledger = FixtureLedger()
        self._file_lock = threading.Lock()  # one writer at a time, per file
        self._report = report_path.open("w", encoding="utf-8", newline="\n")
        self._mutations = (run_dir / "mutations.jsonl").open("w", encoding="utf-8")
        self._records_lock = threading.Lock()
        self._records: list[tuple[str, str, bool, bool]] = []  # (type, task, changed, ok)
        self._rearm_queue: list[str] = []  # task ids that triggered and need re-arming
        self.trigger_classes: list[str] = []  # per fixture trigger: ledger classification
        self.started_at = datetime.now()

    # -- event handler (runs on the resident task-loop thread) --------------------

    def on_task_event(self, event: TaskEvent) -> None:
        kind = event.type
        payload = event.payload
        if kind == "result":
            changed, ok, error = True, True, None
            if event.task_id == FIXTURE_TASK:
                try:
                    sha = _sha256(self.fixture_path.read_bytes())
                except OSError:
                    sha = ""
                classification = self.ledger.credit_or_swallow(sha)
                with self._records_lock:
                    self.trigger_classes.append(classification)
        elif kind in ("waiting", "failed"):
            changed, ok, error = False, False, payload
        elif kind == "cancelled":
            changed, ok, error = False, False, "cancelled"
        elif kind == "started":
            changed, ok, error = False, True, None
            payload = f"started: {payload}"
        else:  # step / lease_request — monitors never emit these; make it visible
            changed, ok, error = False, False, kind
            payload = f"unexpected {kind} event: {payload}"
        row: Row = {
            "at": _iso(event.at),
            "task": event.task_id,
            "changed": changed,
            "ok": ok,
            "summary": payload,
            "error": error,
        }
        with self._file_lock:
            self._report.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._report.flush()
        with self._records_lock:
            self._records.append((kind, event.task_id, changed, ok))
            if kind == "result":
                self._rearm_queue.append(event.task_id)

    # -- counters + re-arm queue (main thread) --------------------------------------

    def counts(self) -> dict[str, int]:
        with self._records_lock:
            records = list(self._records)
            classes = list(self.trigger_classes)
        return {
            "rows": len(records),
            "triggers": sum(1 for kind, _, changed, ok in records
                            if kind == "result" and changed and ok),
            "errors": sum(1 for kind, _, _, ok in records
                          if kind in ("waiting", "failed") and not ok),
            "cancelled": sum(1 for kind, _, _, _ in records if kind == "cancelled"),
            "expected": sum(1 for c in classes if c == "expected"),
            "swallowed": sum(1 for c in classes if c == "swallowed"),
            "fp": sum(1 for c in classes if c in ("false-positive", "unknown")),
        }

    def drain_rearm_queue(self) -> list[str]:
        with self._records_lock:
            queue, self._rearm_queue = self._rearm_queue, []
        return queue

    def write_mutation(self, version: int, content: bytes) -> None:
        """Atomic fixture write + ground-truth record (mutator thread)."""
        at = datetime.now()
        tmp = self.fixture_path.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, self.fixture_path)
        sha = _sha256(content)
        self.ledger.record_write(version, sha)
        with self._file_lock:
            self._mutations.write(
                json.dumps({"at": _iso(at), "version": version, "sha": sha}) + "\n"
            )
            self._mutations.flush()
        print(f"[mutator] v{version} written at {_iso(at)}", flush=True)

    def close_files(self) -> None:
        with self._file_lock:
            self._report.close()
            self._mutations.close()


def _mutation_schedule(duration_s: float, interval_s: int) -> list[float]:
    """Intended-change offsets (s from start): ≥ 3 intervals apart, all at
    least ~10 % of the window before its end, at most MAX_MUTATIONS of them."""
    spacing = max(3.0 * interval_s, duration_s / (MAX_MUTATIONS + 1))
    limit = duration_s * 0.9
    return [spacing * i for i in range(1, MAX_MUTATIONS + 1) if spacing * i <= limit]


def _monitor_spec(task_id: str, run: SoakRun, interval_s: int, feed: str | None) -> TaskSpec:
    is_url = feed is not None and (feed.startswith("http://") or feed.startswith("https://"))
    if task_id == FIXTURE_TASK:
        monitor = MonitorSpec(kind="file_hash", target=str(run.fixture_path),
                              interval_s=interval_s, condition="content changes")
    elif task_id == FEED_TASK and feed is not None:
        monitor = MonitorSpec(kind="rss" if is_url else "file_hash", target=feed,
                              interval_s=interval_s, condition="content changes")
    else:  # pragma: no cover — internal caller error
        raise ValueError(f"unknown soak task id: {task_id}")
    return TaskSpec(
        id=task_id,
        instruction=f"soak: watch {task_id} and tell me when it changes",
        mode="background_monitor",
        monitor=monitor,
        created_by="soak",
    )


def _rearm(app: App, run: SoakRun, task_id: str, interval_s: int, feed: str | None) -> None:
    """Re-arm a one-shot monitor that just triggered: a fresh monitor whose
    first check captures the current content as its baseline — the change that
    fired is never re-reported. Once the first check has landed (state leaves
    pending/running) the captured baseline is sealed in the ledger, so an
    intended version consumed this way counts as accounted, not missing."""
    handle: TaskHandle = app.manager.register(_monitor_spec(task_id, run, interval_s, feed))
    deadline = time.monotonic() + REARM_WAIT_S
    while handle.state in ("pending", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
    if task_id == FIXTURE_TASK and handle.baseline:
        run.ledger.mark_swallowed(handle.baseline)
    print(f"[soak] re-armed {task_id} (baseline sealed: {handle.baseline is not None})",
          flush=True)


def _summary(run: SoakRun, hours: float, interval_s: int, feed: str | None,
             interrupted: bool) -> str:
    c = run.counts()
    accounted = c["expected"] + c["swallowed"]
    fixture_triggers = c["expected"] + c["swallowed"] + c["fp"]
    feed_triggers = c["triggers"] - fixture_triggers
    if interrupted:
        verdict = "INCOMPLETE — the run ended early; criteria not evaluated"
    elif c["fp"] == 0 and accounted == run.intended:
        verdict = "PASS — trigger count equals intended changes; zero triggers on unchanged content"
    else:
        verdict = "FAIL — triggers must equal the intended changes with zero false positives"
    feed_kind = ("rss" if feed.startswith("http") else "file_hash") if feed else None
    trigger_detail = f"[fixture: {fixture_triggers} (credited {c['expected']}, " \
                     f"swallowed {c['swallowed']}, fp {c['fp']})"
    if feed is not None:
        trigger_detail += f"; {FEED_TASK} ({feed_kind}): {feed_triggers} — no ground truth]"
    else:
        trigger_detail += "]"
    return "\n".join([
        "=== M4 soak summary (tools/soak_m4.py) ===",
        f"run dir:          {run.run_dir}",
        f"window:           {_iso(run.started_at)} -> {_iso(datetime.now())} "
        f"(requested {hours:g} h; ended early: {'yes' if interrupted else 'no'})",
        f"tasks:            {FIXTURE_TASK} (file_hash, {interval_s} s, fixture — "
        "harness-controlled ground truth)"
        + (f"; {FEED_TASK} ({feed_kind}) — real, no ground truth" if feed else ""),
        f"intended changes: {run.intended} (mutations.jsonl)",
        f"trigger count:    {c['triggers']}  {trigger_detail}",
        f"error count:      {c['errors']}  [waiting/failed; shutdown-cancel rows: "
        f"{c['cancelled']}]",
        f"false-positive candidates: {c['fp']}  "
        "[fixture-task triggers with unchanged source]",
        f"VERDICT: {verdict}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=24.0, help="soak window (default 24)")
    parser.add_argument("--interval-s", type=int, default=300, help="monitor poll interval")
    parser.add_argument("--feed", default=None,
                        help="one real feed: http(s) URL (rss) or local file path (file_hash)")
    parser.add_argument("--report-jsonl", type=Path, default=None,
                        help="JSONL report path (default <run-dir>/soak_report.jsonl)")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="state dir (default <temp>/marshmellow-soak-m4)")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours must be positive")
    if args.interval_s <= 0:
        parser.error("--interval-s must be positive")

    duration_s = args.hours * 3600.0
    schedule = _mutation_schedule(duration_s, args.interval_s)
    if not schedule:
        parser.error(
            f"the window ({args.hours:g} h) is too short for the poll interval "
            f"({args.interval_s} s) — decrease --interval-s or increase --hours"
        )

    run_dir = args.run_dir or Path(tempfile.gettempdir()) / "marshmellow-soak-m4"
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_jsonl or run_dir / "soak_report.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Fresh run: no stale restore() rows, fixture back to the v0 baseline.
    store = run_dir / "tasks.json"
    if store.exists():
        store.unlink()
        print(f"[soak] cleared previous task store {store}", flush=True)

    run = SoakRun(run_dir, report_path, intended=len(schedule))
    run.fixture_path.write_bytes(b"marshmellow soak fixture v0\n")
    run.ledger.record_write(0, _sha256(b"marshmellow soak fixture v0\n"))

    app = App(
        Settings(activation="hotkey"),
        headless=True,
        log_dir=run_dir / "logs",
        task_store_path=store,
    )
    app.bus.subscribe(TaskEvent, run.on_task_event)
    try:
        app.start()  # manager.start() + restore() (fresh store -> restores nothing)
        app.manager.register(_monitor_spec(FIXTURE_TASK, run, args.interval_s, args.feed))
        if args.feed is not None:
            app.manager.register(_monitor_spec(FEED_TASK, run, args.interval_s, args.feed))
    except ValueError as exc:
        print(f"[soak] fatal: {exc}", flush=True)
        app.stop()
        run.close_files()
        return 2
    print(f"[soak] started {_iso(run.started_at)} for {args.hours:g} h "
          f"(interval {args.interval_s} s, {len(schedule)} intended changes); "
          f"report {report_path}", flush=True)

    stop = threading.Event()
    mutator = threading.Thread(target=_mutator, args=(run, schedule, stop),
                               name="soak-mutator", daemon=True)
    mutator.start()

    interrupted = False
    end = run.started_at + timedelta(seconds=duration_s)
    next_progress = run.started_at + timedelta(seconds=PROGRESS_S)
    try:
        while datetime.now() < end:
            for task_id in run.drain_rearm_queue():
                _rearm(app, run, task_id, args.interval_s, args.feed)
            if stop.wait(timeout=1.0):
                break
            if datetime.now() >= next_progress:
                c = run.counts()
                elapsed_h = (datetime.now() - run.started_at).total_seconds() / 3600
                states = ", ".join(f"{t.spec.id}={t.state}" for t in app.manager.list_tasks())
                print(f"[progress] elapsed {elapsed_h:.2f} h rows={c['rows']} "
                      f"triggers={c['triggers']} errors={c['errors']} fp={c['fp']} "
                      f"({states})", flush=True)
                next_progress = datetime.now() + timedelta(seconds=PROGRESS_S)
    except KeyboardInterrupt:
        interrupted = True
    stop.set()
    mutator.join(timeout=5.0)
    try:
        app.stop()  # graceful: monitor loops cancelled -> their rows land first
    except KeyboardInterrupt:
        interrupted = True
    run.close_files()
    print(_summary(run, args.hours, args.interval_s, args.feed, interrupted), flush=True)
    return 0


def _mutator(run: SoakRun, schedule: list[float], stop: threading.Event) -> None:
    """Write the scheduled fixture mutations; exits before the window ends
    (the schedule keeps a ≥ 10 % unchanged tail)."""
    for i, offset_s in enumerate(schedule, start=1):
        due = run.started_at + timedelta(seconds=offset_s)
        delay = (due - datetime.now()).total_seconds()
        while delay > 0 and not stop.wait(timeout=min(delay, 5.0)):
            delay = (due - datetime.now()).total_seconds()
        if stop.is_set():
            return
        content = f"marshmellow soak fixture v{i} — written {_iso(datetime.now())}\n".encode()
        run.write_mutation(i, content)


if __name__ == "__main__":
    raise SystemExit(main())
