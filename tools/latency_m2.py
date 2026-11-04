"""M2 latency budget bench (Task 9 Step 1) — measured on THIS machine.

Measures the four M2 budget numbers against the real production components:

1. wake→ack            — real WakeWordWatcher thread + real VoiceLoop activation on a
                         headless App, with a scripted threshold-crossing scorer (the
                         self-trained "hey marshmellow" model does not exist yet, D8;
                         the detection-side per-frame cost is SP3's: ~4-5 ms per 80 ms
                         frame). Timestamps: crossing frame scored → "listening" state set.
2. end-speech→transcript — real silero VADSegmenter + faster-whisper (base/int8) over the
                         SP2 fixture WAVs, fed at REAL-TIME pacing so the 0.7 s designed
                         silence close is honest wall-clock.
3. per-step (engine)   — LLMPlanner tool loop through TaskManager with a scripted
                         provider: the engine-side overhead per step. The cloud RTT
                         itself needs the BYOK key (not stored on this machine yet —
                         verified via secrets.load_api_key()).
4. TTS first audio     — real piper resident subprocess (voices/ or the SP4 dev voice),
                         first PCM chunk after say(); cold (spawn+model load) and warm.
                         The player seam discards chunks (silent measurement); the
                         production sounddevice write is sub-ms (SP4).

Writes docs/plan/spikes/data/m2_latency.json and prints a summary table.
Run: uv run python tools/latency_m2.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
FIXTURES = DATA / "sp2_fixtures"
VOICE_CANDIDATES = [
    ROOT / "voices" / "en_US-lessac-medium.onnx",
    DATA / "sp4_voice" / "en_US-amy-medium.onnx",
]
BENCH_LOG_DIR = DATA / "_bench_tmp_logs"


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _read_wav_i16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return x, sr


def _to_16k(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return x
    n_out = int(round(len(x) * 16000 / sr))
    idx = np.linspace(0.0, len(x) - 1.0, n_out)
    return np.interp(idx, np.arange(len(x), dtype=np.float64), x.astype(np.float64)).astype(
        np.int16
    )


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --------------------------------------------------------------------------- 1


def measure_wake_to_ack() -> dict:
    from marshmellow.app import App
    from marshmellow.audio.activation import WakeWordWatcher
    from marshmellow.core.config import Settings
    from marshmellow.core.contracts import WakeDetected

    class RecorderMascot:
        def __init__(self) -> None:
            self.states: list[tuple[float, str]] = []

        def show(self) -> None:
            return None

        def close(self) -> None:
            return None

        def set_state(self, state: str) -> None:
            self.states.append((time.perf_counter(), state))

    class ScriptedScorer:
        """Crosses the threshold on frame 12 (~1 s in); timestamped at the crossing."""

        def __init__(self) -> None:
            self.calls = 0
            t_cross = [0.0]
            self._t_cross = t_cross

        @property
        def t_cross(self) -> float:
            return self._t_cross[0]

        def predict(self, frame: np.ndarray) -> dict[str, float]:
            self.calls += 1
            if self.calls == 12:
                self._t_cross[0] = time.perf_counter()
                return {"scripted": 0.99}
            return {"scripted": 0.01}

        def reset(self) -> None:
            return None

    app = App(Settings(), headless=True, log_dir=BENCH_LOG_DIR)
    recorder = RecorderMascot()
    app.mascot = recorder  # headless has none; the VoiceLoop drives this stub
    app.start()

    scorer = ScriptedScorer()
    frames = (np.zeros(1280, dtype=np.int16) for _ in range(30))
    watcher = WakeWordWatcher(
        app.bus, model_dir=DATA / "sp3_dev_models", chunk_source=frames,
        scorer_factory=lambda _dir: scorer,
    )
    wake_events: list[float] = []
    app.bus.subscribe(WakeDetected, lambda _e: wake_events.append(time.perf_counter()))

    t0 = time.perf_counter()
    watcher.start()
    ok = _wait_until(lambda: any(s == "listening" for _, s in recorder.states), 10.0)
    t_ack = next((t for t, s in recorder.states if s == "listening"), None)
    t_event = wake_events[0] if wake_events else None
    watcher.stop()
    app.stop()
    if not ok or t_ack is None or t_event is None or scorer.t_cross == 0.0:
        return {"ok": False, "error": "wake never crossed threshold within 10 s"}

    return {
        "ok": True,
        "note": (
            "scripted crossing scorer on the real watcher thread + real VoiceLoop; "
            "production detection adds one 80 ms frame + ~4-5 ms openWakeWord scoring (SP3)"
        ),
        "wall_s": round(time.perf_counter() - t0, 4),
        "scoring_frame_to_wake_event_s": round(t_event - scorer.t_cross, 4),
        "scoring_frame_to_listening_ack_s": round(t_ack - scorer.t_cross, 4),
    }


# --------------------------------------------------------------------------- 2


def _sapi_wav(text: str, dest: Path) -> Path | None:
    """Synthesize `text` with Windows SAPI (SP2's fixture method) → 22.05 kHz wav."""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.SetOutputToWaveFile('%s'); $s.Speak('%s'); $s.Dispose()"
        % (str(dest).replace("'", "''"), text.replace("'", "''"))
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], check=True,
            capture_output=True, timeout=60,
        )
        return dest if dest.exists() else None
    except Exception:
        return None


def measure_end_speech_to_transcript() -> dict:
    from marshmellow.audio.capture import VADSegmenter, silero_vad
    from marshmellow.stt.fasterwhisper import FasterWhisperSTT

    if not FIXTURES.exists():
        return {"ok": False, "error": f"SP2 fixtures not present: {FIXTURES}"}

    stt = FasterWhisperSTT()
    real_vad = silero_vad()

    def measure_one(name: str, wav: Path) -> dict:
        pcm, sr = _read_wav_i16(wav)
        x = _to_16k(pcm, sr)
        pad = -len(x) % 512  # silero asserts 512-sample frames; zero-pad the tail
        if pad:
            x = np.concatenate([x, np.zeros(pad, dtype=np.int16)])
        last_speech = [0.0]

        def vad(chunk: np.ndarray) -> float:
            p = real_vad(chunk)
            if p >= 0.5:
                last_speech[0] = time.perf_counter()
            return p

        segmenter = VADSegmenter(vad)
        chunk_samples = 512
        chunk_s = chunk_samples / 16000.0
        segments = []
        t_start = time.perf_counter()
        for i in range(0, len(x), chunk_samples):
            segments.extend(segmenter.feed(x[i : i + chunk_samples]))
            target = t_start + (i // chunk_samples + 1) * chunk_s
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        if not segments:
            return {"wav": name, "error": "no segment closed"}
        segment = segments[0]
        t_stt0 = time.perf_counter()
        transcript = stt.transcribe(segment)
        t_stt1 = time.perf_counter()
        return {
            "wav": name,
            "audio_s": round(len(x) / 16000.0, 2),
            "end_speech_to_transcript_s": round(t_stt1 - last_speech[0], 3),
            "stt_s": round(t_stt1 - t_stt0, 3),
            "transcript": transcript.text,
        }

    rows: list[dict] = []
    for wav in sorted(FIXTURES.glob("phrase_*.wav")):
        rows.append(measure_one(wav.name, wav))
    # The budget's context is a SHORT command; the SP2 fixtures are long
    # dictation-length utterances. Add one SAPI-synthesized simple command
    # (SP2's own fixture method) as the representative row — not committed.
    short_wav = _sapi_wav("open notepad", FIXTURES.parent / "_bench_open_notepad.wav")
    if short_wav is not None:
        rows.append(measure_one("_bench_open_notepad (SAPI)", short_wav))
    else:
        rows.append({"wav": "_bench_open_notepad (SAPI)", "error": "SAPI synth failed"})
    totals = [r["end_speech_to_transcript_s"] for r in rows
              if "end_speech_to_transcript_s" in r]
    stts = [r["stt_s"] for r in rows if "stt_s" in r]
    short_rows = [r for r in rows if "open_notepad" in r.get("wav", "")]
    long_totals = [r["end_speech_to_transcript_s"] for r in rows
                   if "end_speech_to_transcript_s" in r
                   and "open_notepad" not in r.get("wav", "")]
    return {
        "ok": bool(totals),
        "note": (
            "real VAD close (0.7 s designed silence) + real STT at real-time feed "
            "pacing; 12 SP2 fixtures (long utterances) + one SAPI short command"
        ),
        "n": len(totals),
        "short_command": short_rows[0] if short_rows else None,
        "long_utterances_median_s": (
            round(statistics.median(long_totals), 3) if long_totals else None
        ),
        "median_s": round(statistics.median(totals), 3) if totals else None,
        "p95_s": round(float(np.percentile(totals, 95)), 3) if totals else None,
        "max_s": round(max(totals), 3) if totals else None,
        "stt_only_median_s": round(statistics.median(stts), 3) if stts else None,
        "rows": rows,
    }


# --------------------------------------------------------------------------- 3


def measure_per_step() -> dict:
    from marshmellow.core.bus import EventBus
    from marshmellow.core.contracts import (
        ActionResult,
        LLMResponse,
        LLMToolCall,
        TaskSpec,
    )
    from marshmellow.planner.orchestrator import LLMPlanner
    from marshmellow.tasks.manager import TaskManager

    class ScriptedProvider:
        """Returns pre-scripted turns instantly → measures ENGINE overhead only."""

        def __init__(self, turns: list[LLMResponse]) -> None:
            self.turns = list(turns)
            self.call_count = 0

        async def complete(self, _req: object) -> LLMResponse:
            self.call_count += 1
            return self.turns.pop(0)

    provider = ScriptedProvider([
        LLMResponse(tool_calls=[LLMToolCall(
            name="open_app", arguments={"target": "notepad"})]),
        LLMResponse(tool_calls=[LLMToolCall(
            name="finish",
            arguments={"ok": True, "summary_to_speak": "Opened notepad."})]),
    ])
    planner = LLMPlanner(provider)  # type: ignore[arg-type]
    manager = TaskManager(
        bus=EventBus(), planner=planner,
        execute=lambda _req: ActionResult(ok=True, observation="opened (bench)"),
    )

    async def run() -> tuple[float, object]:
        t0 = time.perf_counter()
        handle = await manager.submit(TaskSpec(id="bench", instruction="open notepad"))
        return time.perf_counter() - t0, handle

    total, handle = asyncio.run(run())
    result = getattr(handle, "result", None)
    return {
        "ok": bool(result and result.ok),
        "note": (
            "scripted provider (0 ms RTT): measures the engine-side per-step overhead "
            "(manager hop + prompt build + tool dispatch + finish). The cloud LLM RTT "
            "is NOT included — no BYOK key is stored on this machine yet (D7/OQ-2); "
            "the first BYOK call doubles as the SP5 provider probe."
        ),
        "provider_calls": provider.call_count,
        "turn_total_s": round(total, 4),
        "spoken": result.summary_to_speak if result else None,
    }


# --------------------------------------------------------------------------- 4


def measure_tts_first_audio() -> dict:
    from marshmellow.core.bus import EventBus
    from marshmellow.core.contracts import SpeakRequest, SpeakingStopped
    from marshmellow.tts.piper import PiperSpeaker

    model = next((p for p in VOICE_CANDIDATES if p.exists()), None)
    if model is None:
        return {"ok": False, "error": "no piper voice on this machine"}

    bus = EventBus()
    state = {"t_say": 0.0, "t_first": None, "chunks": 0}
    stopped = threading.Event()

    def player(_chunk: bytes) -> None:
        if state["t_first"] is None:
            state["t_first"] = time.perf_counter()
        state["chunks"] += 1

    bus.subscribe(SpeakingStopped, lambda _e: stopped.set())
    speaker = PiperSpeaker(bus=bus, model=model, player=player)
    speaker.start()

    def say_and_measure(text: str) -> float:
        state["t_first"] = None
        stopped.clear()
        state["t_say"] = time.perf_counter()
        speaker.say(SpeakRequest(text=text))
        if not _wait_until(lambda: state["t_first"] is not None, 60.0):
            return float("nan")
        first = state["t_first"] - state["t_say"]
        stopped.wait(timeout=60.0)  # let the utterance finish before the next one
        return first

    cold = say_and_measure("On it.")  # includes piper spawn + model load tail
    warm = [say_and_measure("On it.") for _ in range(3)]
    long_first = say_and_measure(
        "They posted a new video two days ago, titled Getting Started with Marshmellow."
    )
    speaker.quit()
    bus.stop()
    return {
        "ok": True,
        "note": (
            "real resident piper subprocess (python -m piper), first PCM chunk after "
            "say(); chunks discarded by the player seam — silent measurement"
        ),
        "model": model.name,
        "cold_first_audio_s": round(cold, 3),
        "warm_first_audio_median_s": round(statistics.median(warm), 3),
        "warm_first_audio_all_s": [round(w, 3) for w in warm],
        "long_utterance_first_audio_s": round(long_first, 3),
    }


def main() -> None:
    sections = {
        "wake_to_ack": measure_wake_to_ack,
        "end_speech_to_transcript": measure_end_speech_to_transcript,
        "per_step_engine": measure_per_step,
        "tts_first_audio": measure_tts_first_audio,
    }
    out: dict = {
        "generated": datetime.now(timezone.utc).isoformat(),
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
        for key, value in result.items():
            if key not in ("rows", "note"):
                print(f"  {key}: {value}", flush=True)

    dest = DATA / "m2_latency.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten: {dest}")


if __name__ == "__main__":
    main()
