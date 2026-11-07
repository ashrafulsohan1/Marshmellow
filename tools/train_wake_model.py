"""Self-trained "hey marshmellow" wake model trainer (M5 Task 5, D8).

Two halves:

- **Pure** (headless-tested in tests/audio/test_wake_training.py):
  `build_manifest` — the synthesis manifest (utterances per voice, per-utterance
  text/gain/speed jitter, the negative list, the seed) — and `calibrate_threshold`
  — the smallest score cutoff whose false-accept rate <= target over a score list.

- **Machine job** (agent-run, unattended; no user interaction):

    uv run python tools/train_wake_model.py --plan            # manifest only
    uv sync --group wake-training                             # torch (training step only)
    uv run --group wake-training python tools/train_wake_model.py

  Full-run stages: download en_US piper voices (subprocess, never imported here) →
  synthesize positives + lookalike negatives through the EXISTING
  `marshmellow.tts.piper_worker` subprocess protocol (stdin lines -> framed PCM —
  this tool imports no piper, so no new GPL surface) -> SP3 pink-noise/silence
  background -> openwakeword's upstream custom-training flow (`openwakeword.train`,
  torch: uv group `wake-training`, imported inside the training step ONLY) ->
  export ``<out>/hey-marshmellow.onnx`` -> score every negative frame with the REAL
  scorer path and calibrate the wake threshold into ``<out>/calibration.json``
  (SP3's 0.5 stands unless the histogram says otherwise — recorded, not silently
  changed) -> write a held-out piper phrase fixture for the D8 acceptance test.
"""

from __future__ import annotations

import argparse
import json
import random
import struct
import subprocess
import sys
import urllib.request
import wave
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PHRASE = "hey marshmellow"
DEFAULT_VOICES = [
    "en_US-lessac-medium",
    "en_US-amy-medium",
    "en_US-ryan-medium",
]
MODEL_NAME = "hey-marshmellow"
FIXTURE_NAME = "fixture-hey-marshmellow.wav"
SAMPLE_RATE = 16000
CLIP_SAMPLES = 2 * SAMPLE_RATE  # upstream training clips are 2 s -> (16, 96) features
WAKE_THRESHOLD_DEFAULT = 0.5  # M0-SP3
FEATURE_MODEL_URLS = {
    # openwakeword's own feature-extractor models (v0.5.1 release assets; the
    # wheel ships no resources — see utils.download_models' .tflite→.onnx swap).
    "melspectrogram.onnx": (
        "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
        "melspectrogram.onnx"
    ),
    "embedding_model.onnx": (
        "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
        "embedding_model.onnx"
    ),
}
LOOKALIKE_NEGATIVES = [
    "hey marshmallow",
    "hey mellow",
    "hey marsh mellow",
    "hi marshmellow",
    "hey marshmellows",
    "marshmellow",
]
GENERIC_NEGATIVES = [
    "what time is it",
    "open the browser",
    "tell me a joke",
    "whats the weather today",
    "set a timer for ten minutes",
    "stop everything",
    "take a screenshot",
    "check my email",
    "play some music",
    "never mind",
]


# ------------------------------------------------------------- pure half


def _variant(phrase: str, rng: random.Random) -> str:
    """A natural spoken-text variant of the phrase (punctuation/casing only)."""
    capitalized = ", ".join(w.capitalize() for w in phrase.split())
    options = [
        phrase,
        capitalized,
        capitalized + "!",
        capitalized + ".",
        phrase.upper(),
        capitalized.replace(", ", ", ", 1) + "?",
    ]
    return rng.choice(options)


def build_manifest(
    phrase: str,
    utterances: int,
    voices: Sequence[str],
    seed: int,
    lookalikes: Sequence[str] | None = None,
    negatives_per_text: int = 30,
) -> dict:
    """The deterministic synthesis plan: same seed -> byte-identical manifest."""
    rng = random.Random(seed)
    synthesis = []
    for i in range(utterances):
        # NOTE: the speed jitter is deliberately narrow (±3 %). A resample-based
        # time stretch shifts pitch with it — wide stretch makes "mellow"
        # acoustically overlap the "hey marshmallow" lookalike and destroys the
        # phoneme contrast the classifier needs (measured, first training run).
        synthesis.append(
            {
                "i": i,
                "voice": voices[i % len(voices)],
                "text": _variant(phrase, rng),
                "gain": round(rng.uniform(0.7, 1.3), 3),
                "speed": round(rng.uniform(0.97, 1.03), 3),
                "lead_s": round(rng.uniform(0.02, 0.15), 3),
            }
        )
    return {
        "phrase": phrase,
        "utterances": utterances,
        "seed": seed,
        "sample_rate": SAMPLE_RATE,
        "clip_seconds": CLIP_SAMPLES / SAMPLE_RATE,
        "voices": {v: sum(1 for r in synthesis if r["voice"] == v) for v in voices},
        "synthesis": synthesis,
        "negatives": {
            "lookalikes": list(lookalikes or LOOKALIKE_NEGATIVES),
            "generic": list(GENERIC_NEGATIVES),
            "background": ["pink_noise", "silence"],
            "utterances_per_text": negatives_per_text,
        },
    }


def calibrate_threshold(scores: Sequence[float], target_rate: float) -> float:
    """Smallest score cutoff whose false-accept rate (share of scores >= cutoff)
    is <= `target_rate`. When even the strictest observed cutoff exceeds the
    target, returns just above the maximum observed score (the histogram says
    "no observed negative may be accepted")."""
    if not scores:
        raise ValueError("no scores to calibrate from")
    n = len(scores)
    for cutoff in sorted(set(scores)):
        accepts = sum(1 for s in scores if s >= cutoff)
        if accepts / n <= target_rate:
            return float(cutoff)
    return float(max(scores)) + 0.01


# --------------------------------------------------------- synthesis machine


def _read_exact(stream, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        piece = stream.read(n - len(buf))
        if not piece:
            raise EOFError("piper_worker stream ended mid-utterance")
        buf += piece
    return buf


class PiperSynthesizer:
    """One `marshmellow.tts.piper_worker` subprocess per voice.

    Speaks the EXISTING framing protocol (stdin lines -> 4-byte LE sample rate,
    then 4-byte-LE-length int16 PCM chunks, 0-terminator). piper is imported
    only inside that subprocess — this tool never imports it (SP7/D8).
    """

    def __init__(self, voice_path: Path) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "marshmellow.tts.piper_worker", str(voice_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def synthesize(self, text: str) -> np.ndarray:
        """16 kHz int16 PCM for `text` (piper voices vary in rate; resample here)."""
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write((text + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        (sample_rate,) = struct.unpack("<I", _read_exact(self._proc.stdout, 4))
        chunks: list[np.ndarray] = []
        while True:
            (length,) = struct.unpack("<I", _read_exact(self._proc.stdout, 4))
            if length == 0:
                break
            chunks.append(np.frombuffer(_read_exact(self._proc.stdout, length), dtype=np.int16))
        pcm = np.concatenate(chunks)
        if sample_rate == SAMPLE_RATE:
            return pcm
        n_out = int(round(len(pcm) * SAMPLE_RATE / sample_rate))
        idx = np.linspace(0.0, len(pcm) - 1.0, n_out)
        resampled = np.interp(idx, np.arange(len(pcm), dtype=np.float64),
                              pcm.astype(np.float64))
        return np.clip(resampled, -32768, 32767).astype(np.int16)

    def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        self._proc.wait(timeout=60)


def ensure_voice(voice: str, voices_dir: Path) -> Path:
    """The voice .onnx (+ .json config) in `voices_dir`, downloading via the
    piper downloader SUBPROCESS when missing (this tool imports no piper)."""
    onnx = voices_dir / f"{voice}.onnx"
    if not (voices_dir / f"{voice}.onnx.json").exists():
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "piper.download_voices", voice,
             "--download-dir", str(voices_dir)],
            check=True,
        )
    if not onnx.exists():
        raise FileNotFoundError(f"piper voice model not found after download: {onnx}")
    return onnx


def _speed_change(pcm: np.ndarray, speed: float) -> np.ndarray:
    """Linear-resample time stretch — the piper_worker protocol exposes no
    length_scale, so jitter comes from this plus gain (adds pitch variety too)."""
    if speed == 1.0:
        return pcm
    n_out = int(round(len(pcm) / speed))
    idx = np.linspace(0.0, len(pcm) - 1.0, n_out)
    stretched = np.interp(idx, np.arange(len(pcm), dtype=np.float64), pcm.astype(np.float64))
    return np.clip(stretched, -32768, 32767).astype(np.int16)


def pink_noise(n_samples: int, rng: np.random.Generator, amplitude: float = 0.3) -> np.ndarray:
    """SP3's noise stream: speech-band-shaped pink noise at a fixed amplitude."""
    white = rng.standard_normal(n_samples)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n_samples, d=1.0 / SAMPLE_RATE)
    shaped = spectrum / np.sqrt(np.maximum(freqs, 20.0))
    pink = np.fft.irfft(shaped, n=n_samples)
    peak = float(np.max(np.abs(pink))) or 1.0
    return (pink / peak * amplitude * 32767).astype(np.int16)


def _trim_silence(pcm: np.ndarray, floor: int = 120) -> np.ndarray:
    """Cut piper's padded edge silence (20 ms breath left on each side)."""
    loud = np.nonzero(np.abs(pcm.astype(np.int32)) > floor)[0]
    if len(loud) == 0:
        return pcm
    pad = int(0.02 * SAMPLE_RATE)
    start = max(0, int(loud[0]) - pad)
    end = min(len(pcm), int(loud[-1]) + 1 + pad)
    return pcm[start:end]


def _make_clip(utterance: np.ndarray, rng: np.random.Generator,
               background_amp: float = 0.02) -> np.ndarray:
    """A fixed 2 s training clip: quiet pink-noise bed + the phrase at a random
    offset (pos/neg clips share the layout, so the model learns the phrase)."""
    if len(utterance) > CLIP_SAMPLES:
        raise ValueError(f"utterance {len(utterance)} > clip size {CLIP_SAMPLES}")
    clip = pink_noise(CLIP_SAMPLES, rng, amplitude=background_amp).astype(np.int64)
    start = int(rng.integers(0, CLIP_SAMPLES - len(utterance) - 1))
    clip[start:start + len(utterance)] += utterance.astype(np.int64)
    return np.clip(clip, -32768, 32767).astype(np.int16)


def synthesize_dataset(
    manifest: dict,
    voices_dir: Path,
    work_dir: Path,
    log=print,
) -> tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]:
    """Positive/negative/background 2 s clips from the manifest.

    Positives follow `manifest["synthesis"]`; negatives are lookalike + generic
    phrases through the same voices plus SP3 pink-noise/silence background clips.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(manifest["seed"])
    voices = list(manifest["voices"])
    synths: dict[str, PiperSynthesizer] = {}
    for voice in voices:
        synths[voice] = PiperSynthesizer(ensure_voice(voice, voices_dir))

    def utterance(row: dict) -> np.ndarray:
        pcm = synths[row["voice"]].synthesize(row["text"])
        pcm = _speed_change(pcm, float(row["speed"]))
        pcm = _trim_silence(pcm)
        # Fit guarantee: a 0.9x-stretched long phrase can still exceed the 2 s
        # clip — compress (never truncate the phrase) to keep 50 ms jitter room.
        max_len = CLIP_SAMPLES - int(0.05 * SAMPLE_RATE)
        if len(pcm) > max_len:
            pcm = _speed_change(pcm, len(pcm) / max_len)
        return np.clip(pcm.astype(np.float64) * float(row["gain"]), -32768, 32767).astype(np.int16)

    positives: list[np.ndarray] = []
    negatives_by_class: dict[str, list[np.ndarray]] = {}
    try:
        for row in manifest["synthesis"]:
            positives.append(_make_clip(utterance(row), rng))
            if len(positives) % 100 == 0:
                log(f"  positives: {len(positives)}/{manifest['utterances']}")

        per_text = manifest["negatives"]["utterances_per_text"]
        for class_name, texts in (
            ("lookalikes", manifest["negatives"]["lookalikes"]),
            ("generic", manifest["negatives"]["generic"]),
        ):
            clips = negatives_by_class.setdefault(class_name, [])
            rows = [
                {"voice": voices[i % len(voices)], "text": text, "gain": 1.0, "speed": 1.0}
                for text in texts
                for i in range(per_text)
            ]
            for row in rows:
                clips.append(_make_clip(utterance(row), rng))
                if len(clips) % 100 == 0:
                    log(f"  {class_name} negatives: {len(clips)}")

        background = negatives_by_class.setdefault("background", [])
        for i in range(len(positives)):  # balance the phrase negatives with raw background
            if i % 2 == 0:
                background.append(
                    pink_noise(CLIP_SAMPLES, rng, amplitude=float(rng.uniform(0.1, 0.4)))
                )
            else:
                background.append(np.zeros(CLIP_SAMPLES, dtype=np.int16))
            if (i + 1) % 100 == 0:
                log(f"  background negatives: {i + 1}/{len(background)}")
    finally:
        for synth in synths.values():
            synth.close()
    return positives, negatives_by_class


def ensure_feature_models(out_dir: Path) -> dict[str, Path]:
    """The two feature-extractor ONNX deps in `out_dir` (download when missing) —
    the D8 license-check subjects — and a copy inside openwakeword's resources
    dir where `AudioFeatures` looks for them (the wheel ships no resources)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, url in FEATURE_MODEL_URLS.items():
        target = out_dir / name
        if not target.exists():
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(url, target)  # noqa: S310 - pinned https URL
        paths[name] = target
    import openwakeword

    resources = Path(openwakeword.__file__).parent / "resources" / "models"
    resources.mkdir(parents=True, exist_ok=True)
    for name, path in paths.items():
        if not (resources / name).exists():
            (resources / name).write_bytes(path.read_bytes())
    return paths


# ----------------------------------------------------------- training machine


def _batched(clips: list[np.ndarray], batch_size: int) -> Iterator[np.ndarray]:
    usable = len(clips) - len(clips) % batch_size
    for i in range(0, usable, batch_size):
        yield np.stack(clips[i:i + batch_size])


def _compute_features(clips: list[np.ndarray], output_file: Path, batch_size: int = 64) -> None:
    """(N, 16, 96) float32 feature arrays via openwakeword's `AudioFeatures` —
    the same features `compute_features_from_generator` writes, minus its
    memmap plumbing (which deadlocks on Windows: trim_mmap renames the file
    while the w+ memmap is still open). Our datasets fit in RAM."""
    from openwakeword.utils import AudioFeatures

    F = AudioFeatures(device="cpu")
    batch_size = min(batch_size, len(clips))  # small val sets must still fill one batch
    chunks = [
        F.embed_clips(np.stack(clips[i:i + batch_size]), batch_size=batch_size)
        for i in range(0, len(clips) - len(clips) % batch_size, batch_size)
    ]
    np.save(output_file, np.vstack(chunks).astype(np.float32))


def _import_openwakeword_data():
    """`openwakeword.data` imports the stale `acoustics` package at module level;
    `acoustics` 0.2.4 cannot load under scipy >= 1.17 (sph_harm was removed).
    Our flow only uses `mmap_batch_generator`, which never touches acoustics
    (it lives inside generate_adversarial_texts) — shim the failed import rather
    than downgrading scipy for the whole environment."""
    try:
        import acoustics  # noqa: F401
    except ImportError:
        import types

        def _unavailable(*_args, **_kwargs) -> None:
            raise RuntimeError(
                "acoustics is shimmed; openwakeword's generate_adversarial_texts "
                "is not part of this training flow"
            )

        shim = types.ModuleType("acoustics")
        shim.generator = types.SimpleNamespace(noise=_unavailable)  # type: ignore[attr-defined]
        sys.modules["acoustics"] = shim
    from openwakeword.data import mmap_batch_generator

    return mmap_batch_generator


def train_and_export(
    work_dir: Path,
    out_dir: Path,
    clip_sets: dict[str, list[np.ndarray]],
    steps: int,
    log=print,
) -> Path:
    """The upstream custom-training flow: features -> `openwakeword.train.Model`
    -> `auto_train` -> `export_model` -> `<out_dir>/hey-marshmellow.onnx`.
    torch is imported HERE only (uv group `wake-training`)."""
    mmap_batch_generator = _import_openwakeword_data()
    from openwakeword.train import Model
    from openwakeword.utils import AudioFeatures

    features_dir = work_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)

    def features(name: str, clips: list[np.ndarray]) -> Path:
        path = features_dir / name
        if not path.exists():
            log(f"computing features: {name}")
            _compute_features(clips, path)
        return path

    pos_train_f = features("positive_features_train.npy", clip_sets["pos_train"])
    neg_train_f = features("negative_features_train.npy", clip_sets["neg_train"])
    pos_test_f = features("positive_features_test.npy", clip_sets["pos_test"])
    neg_test_f = features("negative_features_test.npy", clip_sets["neg_test"])
    background_f = features("background_features.npy", clip_sets["background_val"])

    audio_features = AudioFeatures(device="cpu")
    input_shape = audio_features.get_embedding_shape(CLIP_SAMPLES / SAMPLE_RATE)
    log(f"feature input shape: {input_shape}")
    oww = Model(
        n_classes=1, input_shape=input_shape, model_type="dnn", layer_dim=128,
        seconds_per_example=1280 * input_shape[0] / SAMPLE_RATE,
    )

    feature_data_files = {"positive": str(pos_train_f), "negative": str(neg_train_f)}

    def _identity(x, n: int = 16):  # noqa: ARG001 - upstream transform signature
        return x

    data_transforms = {key: _identity for key in feature_data_files}
    label_transforms = {"positive": lambda x: [1 for _ in x],
                        "negative": lambda x: [0 for _ in x]}
    batch_generator = mmap_batch_generator(
        feature_data_files,
        n_per_class={"positive": 64, "negative": 64},
        data_transform_funcs=data_transforms,
        label_transform_funcs=label_transforms,
    )

    import torch

    class _IterDataset(torch.utils.data.IterableDataset):
        def __init__(self, generator) -> None:
            self.generator = generator

        def __iter__(self):
            return self.generator

    # num_workers=0: the batch generator holds a live memmap iterator, which
    # cannot pickle through Windows' spawn-based DataLoader workers.
    x_train = torch.utils.data.DataLoader(_IterDataset(batch_generator), batch_size=None)

    pos_test_feats, neg_test_feats = np.load(pos_test_f), np.load(neg_test_f)
    val_labels = np.hstack(
        (np.ones(pos_test_feats.shape[0]), np.zeros(neg_test_feats.shape[0]))
    ).astype(np.float32)
    x_val = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(
            torch.from_numpy(np.vstack((pos_test_feats, neg_test_feats)).astype(np.float32)),
            torch.from_numpy(val_labels),
        ),
        batch_size=len(val_labels),
    )

    flat_background = np.load(background_f).reshape(-1, input_shape[1]).astype(np.float32)
    windows = np.array(
        [flat_background[i:i + input_shape[0]]
         for i in range(0, flat_background.shape[0] - input_shape[0], 1)]
    )
    fp_labels = np.zeros(windows.shape[0]).astype(np.float32)
    fp_data = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(torch.from_numpy(windows), torch.from_numpy(fp_labels)),
        batch_size=len(fp_labels),
    )

    log(f"training ({steps} steps, 3 auto sequences, CPU)...")
    best_model = oww.auto_train(
        X_train=x_train, X_val=x_val, false_positive_val_data=fp_data,
        steps=steps, max_negative_weight=1000, target_fp_per_hour=0.2,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    oww.export_model(model=best_model, model_name=MODEL_NAME, output_dir=str(out_dir))
    model_path = out_dir / f"{MODEL_NAME}.onnx"
    if not model_path.exists():
        raise RuntimeError("export_model did not produce the onnx model")
    return model_path


def _embed_onnx_external_data(model_path: Path) -> None:
    """torch 2.9+'s dynamo exporter writes weights as an external `.data` file;
    the shipped asset must be ONE self-contained .onnx — inline them back."""
    data_file = model_path.with_name(model_path.name + ".data")
    if not data_file.exists():
        return
    import onnx
    from onnx import external_data_helper

    model = onnx.load(str(model_path), load_external_data=True)
    external_data_helper.convert_model_from_external_data(model)
    onnx.save(model, str(model_path))
    data_file.unlink()
    print(f"embedded external weights into {model_path.name} (removed {data_file.name})",
          flush=True)


# ------------------------------------------------------------- calibration


def _frame_scores(scorer, clips: list[np.ndarray]) -> list[float]:
    """Per-80-ms-frame scores through the REAL scorer path (the watcher's
    semantics: one score per frame; the wake fires when one crosses)."""
    scores: list[float] = []
    for clip in clips:
        scorer.reset()
        for i in range(0, len(clip) - 1280 + 1, 1280):
            frame_scores = scorer.predict(clip[i:i + 1280])
            scores.append(max(frame_scores.values(), default=0.0))
    return scores


def calibrate_and_write(
    model_path: Path,
    positives: list[np.ndarray],
    negatives_by_class: dict[str, list[np.ndarray]],
    out_dir: Path,
    log=print,
) -> dict:
    """Threshold calibration from the negative-score histogram (D8): SP3's 0.5
    stands unless the histogram says otherwise — recorded, not silently changed.
    Per-class negative scores are recorded (lookalike confusions are visible)."""
    from marshmellow.audio.activation import openwakeword_scorer

    scorer = openwakeword_scorer(out_dir)
    class_frames = {
        name: _frame_scores(scorer, clips) for name, clips in negatives_by_class.items()
    }
    neg_frames = [s for frames in class_frames.values() for s in frames]
    target_rate = 1.0 / (len(neg_frames) + 1)  # zero observed false accepts
    smallest_ok = calibrate_threshold(neg_frames, target_rate)
    chosen = max(WAKE_THRESHOLD_DEFAULT, smallest_ok)
    chosen = min(chosen, 0.99)  # the watcher rejects thresholds >= 1.0
    positive_clip_scores = [
        max(_frame_scores(scorer, [clip]), default=0.0) for clip in positives
    ]
    calibration = {
        "model": model_path.name,
        "method": (
            "smallest cutoff with false-accept rate <= target over all negative "
            "frame scores (tools/train_wake_model.py calibrate_threshold); "
            "per-class maxima recorded to make lookalike confusions visible"
        ),
        "target_far": target_rate,
        "sp3_default": WAKE_THRESHOLD_DEFAULT,
        # "threshold" is the app-contract key (wake_threshold_from_calibration
        # reads exactly this); "sp3_default_kept" records the decision.
        "threshold": round(chosen, 4),
        "sp3_default_kept": chosen == WAKE_THRESHOLD_DEFAULT,
        "negative_frames": len(neg_frames),
        "negative_scores": {
            "max": round(float(np.max(neg_frames)), 4),
            "p99": round(float(np.percentile(neg_frames, 99)), 4),
            "p95": round(float(np.percentile(neg_frames, 95)), 4),
        },
        "negative_scores_by_class": {
            name: {
                "clips": len(negatives_by_class[name]),
                "max": round(float(np.max(frames)), 4) if frames else None,
                "p99": round(float(np.percentile(frames, 99)), 4) if frames else None,
            }
            for name, frames in class_frames.items()
        },
        "positive_clips": len(positive_clip_scores),
        "positive_min_clip_max_score": round(float(np.min(positive_clip_scores)), 4),
        "positive_max_clip_max_score": round(float(np.max(positive_clip_scores)), 4),
        "recall_margin": round(
            float(np.min(positive_clip_scores)) - chosen, 4
        ),
    }
    (out_dir / "calibration.json").write_text(
        json.dumps(calibration, indent=2), encoding="utf-8"
    )
    log(f"calibration: threshold={calibration['threshold']} "
        f"(SP3 default kept: {calibration['sp3_default_kept']}, "
        f"neg max={calibration['negative_scores']['max']}, "
        f"pos min={calibration['positive_min_clip_max_score']})")
    return calibration


def write_phrase_fixture(
    manifest: dict,
    voices_dir: Path,
    out_dir: Path,
) -> Path:
    """A held-out, UNaugmented piper phrase for the D8 acceptance test row
    (test_activation.py): the real synthesized phrase, edge-silence trimmed,
    padded to 1.55 s so it stays inside the 1.5 s onset budget."""
    voice = ensure_voice(list(manifest["voices"])[0], voices_dir)
    synth = PiperSynthesizer(voice)
    pcm = _trim_silence(synth.synthesize("Hey, Marshmellow!"))
    synth.close()
    total = int(1.55 * SAMPLE_RATE)
    if len(pcm) > total:
        raise ValueError(f"phrase fixture speech too long: {len(pcm) / SAMPLE_RATE:.2f} s")
    fixture = np.zeros(total, dtype=np.int16)
    fixture[int(0.05 * SAMPLE_RATE):int(0.05 * SAMPLE_RATE) + len(pcm)] = pcm
    path = out_dir / FIXTURE_NAME
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(fixture.tobytes())
    return path


# ------------------------------------------------------------------ CLI


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phrase", default=DEFAULT_PHRASE)
    parser.add_argument("--utterances", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=REPO / "assets" / "wake")
    parser.add_argument("--voices-dir", type=Path, default=REPO / "voices")
    parser.add_argument("--work-dir", type=Path,
                        default=REPO / "docs" / "plan" / "spikes" / "data" / "m5_wake_training")
    parser.add_argument("--voices", nargs="*", default=DEFAULT_VOICES)
    parser.add_argument("--steps", type=int, default=20000,
                        help="auto_train steps (3 sequences: steps, steps/10, steps/10)")
    parser.add_argument("--negatives-per-text", type=int, default=30,
                        help="synthesized negatives per lookalike/generic text")
    parser.add_argument("--plan", action="store_true",
                        help="print/save the synthesis manifest without training")
    args = parser.parse_args()

    manifest = build_manifest(args.phrase, args.utterances, args.voices, args.seed,
                              negatives_per_text=args.negatives_per_text)
    out_dir: Path = args.out
    if args.plan:
        print(json.dumps(manifest, indent=2))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        print(f"manifest saved to {out_dir / 'manifest.json'} (no training run)")
        return 0

    log = lambda msg: print(msg, flush=True)  # noqa: E731 - progress printer
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    ensure_feature_models(out_dir)
    positives, negatives_by_class = synthesize_dataset(manifest, args.voices_dir,
                                                       args.work_dir, log=log)
    # 90/10 train/test split per class at clip granularity (deterministic stride);
    # training merges all negative classes, calibration keeps them separate, and
    # the FP-per-hour validation uses the HELD-OUT background only (never clips
    # the model trained on).
    clip_sets = {
        "pos_train": [c for i, c in enumerate(positives) if i % 10 != 0],
        "pos_test": [c for i, c in enumerate(positives) if i % 10 == 0],
        "neg_train": [c for clips in negatives_by_class.values()
                      for i, c in enumerate(clips) if i % 10 != 0],
        "neg_test": [c for clips in negatives_by_class.values()
                     for i, c in enumerate(clips) if i % 10 == 0],
        "background_val": [c for i, c in enumerate(negatives_by_class["background"])
                           if i % 10 == 0],
    }
    model_path = train_and_export(args.work_dir, out_dir, clip_sets, args.steps, log=log)
    _embed_onnx_external_data(model_path)
    calibration = calibrate_and_write(
        model_path, clip_sets["pos_test"],
        {name: [c for i, c in enumerate(clips) if i % 10 == 0]
         for name, clips in negatives_by_class.items()},
        out_dir, log=log,
    )
    fixture = write_phrase_fixture(manifest, args.voices_dir, out_dir)
    log(f"wrote {model_path}")
    log(f"wrote {out_dir / 'calibration.json'} (threshold {calibration['threshold']})")
    log(f"wrote {fixture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
