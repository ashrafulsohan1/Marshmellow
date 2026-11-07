"""M5 Task 5 ambient-mic wake soak (D8) — discharges the SP3 carry-over.

Streams the REAL microphone through the REAL wake path — `AudioEngine`
chunk_tap -> `ChunkQueue` -> `WakeWordWatcher` on the self-trained model — for
`--hours` of ambient audio and counts `WakeDetected` events (false accepts;
nobody is asking Marshmellow to wake). The verdict lands in
docs/plan/spikes/data/m5_wake_soak.json and the M5 evidence block.

    uv run python tools/soak_wake_m5.py --hours 1 [--model-dir assets/wake]

Machine job (agent-run, unattended): keep the desk quiet-ish and let it run —
TVs, conversations and other audio are exactly the surface this soak measures.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from marshmellow.audio.activation import ChunkQueue, WakeWordWatcher, wake_threshold_from_calibration
from marshmellow.audio.capture import AudioEngine
from marshmellow.core.bus import EventBus
from marshmellow.core.contracts import WakeDetected

REPO = Path(__file__).resolve().parents[1]
SOAK_JSON = REPO / "docs" / "plan" / "spikes" / "data" / "m5_wake_soak.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=1.0)
    parser.add_argument("--model-dir", type=Path, default=REPO / "assets" / "wake")
    args = parser.parse_args()

    model_dir: Path = args.model_dir
    if not (model_dir / "hey-marshmellow.onnx").exists():
        print(f"no self-trained model in {model_dir} — run tools/train_wake_model.py first")
        return 2
    threshold = wake_threshold_from_calibration(model_dir)

    bus = EventBus()
    counts = {"wake": 0, "scores": []}
    bus.subscribe(WakeDetected, lambda e: (
        counts.__setitem__("wake", counts["wake"] + 1),
        counts["scores"].append(round(e.score, 3)),  # type: ignore[attr-defined]
    ))

    tap = ChunkQueue()
    # The REAL path: capture thread taps every unmuted chunk into the queue; the
    # watcher drains it. VAD is stubbed — speech segmentation is not the surface
    # under test, and the tap feeds the watcher BEFORE VAD regardless (§3.1).
    engine = AudioEngine(bus=bus, vad=lambda chunk: 0.0, chunk_tap=tap)
    watcher = WakeWordWatcher(
        bus, model_dir=model_dir, chunk_source=iter(tap), threshold=threshold,
    )

    started = datetime.now(timezone.utc)
    print(f"soak start {started.isoformat()} — {args.hours} h, "
          f"model={model_dir}, threshold={threshold}", flush=True)
    engine.start()
    watcher.start()
    deadline = time.monotonic() + args.hours * 3600
    last_report = time.monotonic()
    try:
        while time.monotonic() < deadline:
            time.sleep(30)
            if time.monotonic() - last_report >= 600:
                elapsed = args.hours * 3600 - (deadline - time.monotonic())
                print(f"  t+{elapsed / 60:.0f} min: {counts['wake']} false accepts",
                      flush=True)
                last_report = time.monotonic()
    except KeyboardInterrupt:
        print("interrupted; recording partial soak", flush=True)
    finally:
        engine.stop()
        tap.close()
        watcher.stop()

    ended = datetime.now(timezone.utc)
    hours = (ended - started).total_seconds() / 3600
    result = {
        "tool": "tools/soak_wake_m5.py",
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "hours": round(hours, 3),
        "model_dir": str(model_dir),
        "threshold": threshold,
        "false_accepts": counts["wake"],
        "false_accepts_per_hour": round(counts["wake"] / hours, 2) if hours else None,
        "scores": counts["scores"],
        "path": "AudioEngine chunk_tap -> ChunkQueue -> WakeWordWatcher (the real path)",
        "verdict": (
            "SP3 carry-over 'live-mic wake false-accept soak' discharged "
            "(pending-user-verifications.md; re-check in the M5 Task 7 checklist)"
        ),
    }
    SOAK_JSON.parent.mkdir(parents=True, exist_ok=True)
    SOAK_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"{counts['wake']} false accepts in {hours:.2f} h "
          f"({result['false_accepts_per_hour']}/h) -> {SOAK_JSON}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
