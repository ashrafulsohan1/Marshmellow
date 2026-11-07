"""Task 5 (D8) unit tier — ChunkQueue bridge, app tap/model-dir wiring, and the
headless-tested pure half of `tools/train_wake_model.py` (manifest determinism +
threshold calibration math). The real-model tier lives in test_activation.py."""

import itertools
import json
import time
from pathlib import Path

import numpy as np
from tools.train_wake_model import build_manifest, calibrate_threshold

from marshmellow.app import App
from marshmellow.audio.activation import (
    WAKE_THRESHOLD,
    ChunkQueue,
    wake_threshold_from_calibration,
)
from marshmellow.core.config import Settings

VOICES = ["en_US-lessac-medium", "en_US-amy-medium"]


# ------------------------------------------------------------------ ChunkQueue


def test_chunk_queue_drop_oldest() -> None:
    """Overflow drops the OLDEST chunk, `dropped` counts it, and the tap never
    blocks the capture thread (put_nowait + drop-oldest, 50 × 32 ms ≈ 1.6 s)."""
    tap = ChunkQueue(maxlen=4)
    chunks = [np.full(512, i, dtype=np.int16) for i in range(6)]
    start = time.perf_counter()
    for chunk in chunks:
        tap(chunk)  # this IS the AudioEngine tap call: must stay latency-bounded
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0  # 6 puts through a FULL queue: microseconds, not waits
    assert tap.dropped == 2
    got = list(itertools.islice(iter(tap), 4))
    assert [int(c[0]) for c in got] == [2, 3, 4, 5]  # the two oldest were dropped


def test_chunk_queue_close_ends_iteration() -> None:
    """close() is the clean watcher stop: items queued before the sentinel still
    drain, then the iterator ends (StopIteration). Idempotent."""
    tap = ChunkQueue()
    a, b = np.zeros(512, dtype=np.int16), np.ones(512, dtype=np.int16)
    tap(a)
    tap(b)
    it = iter(tap)
    assert next(it) is a and next(it) is b
    tap.close()
    assert next(it, None) is None  # sentinel → the watcher's chunk_source ends
    tap.close()  # second close must not raise


def test_chunk_queue_default_buffer_is_50_chunks() -> None:
    tap = ChunkQueue()
    for i in range(60):
        tap(np.full(512, i, dtype=np.int16))
    assert tap.dropped == 10
    got = list(itertools.islice(iter(tap), 50))
    assert int(got[0][0]) == 10 and int(got[-1][0]) == 59


# ------------------------------------------------------- app composition (D8)


def test_app_wires_tap_and_model_dir(tmp_path: Path) -> None:
    """Headless App with an injected wake_model_dir: the wake watcher's chunk
    source is the shared ChunkQueue the capture tap would feed (D8 mic sharing)."""
    model_dir = tmp_path / "wake"
    model_dir.mkdir()
    (model_dir / "hey-marshmellow.onnx").write_bytes(b"stub model bytes")
    app = App(
        Settings(activation="wake_word"),
        headless=True,
        log_dir=tmp_path,
        audio_engine=None,
        wake_model_dir=model_dir,
    )
    try:
        assert app.wake_watcher is not None
        assert app.wake_tap is not None
        chunk = np.zeros(512, dtype=np.int16)
        app.wake_tap(chunk)
        assert next(app.wake_watcher._source) is chunk  # the shared queue, drained
    finally:
        app.stop()


def test_app_hotkey_only_gets_no_tap(tmp_path: Path) -> None:
    app = App(
        Settings(activation="hotkey"),
        headless=True,
        log_dir=tmp_path,
        audio_engine=None,
    )
    try:
        assert app.wake_watcher is None
        assert app.wake_tap is None
    finally:
        app.stop()


def test_app_wake_channel_without_model_keeps_spoken_degrade(tmp_path: Path) -> None:
    """No injected dir and no default model → model_dir=None: the existing
    ActivationDegraded path (audio/activation.py start()) is preserved."""
    app = App(
        Settings(activation="wake_word"),
        headless=True,
        log_dir=tmp_path,
        audio_engine=None,
    )
    try:
        assert app.wake_watcher is not None
        assert app.wake_watcher._model_dir is None  # degrade decided at start()
    finally:
        app.stop()


def test_wake_threshold_from_calibration(tmp_path: Path) -> None:
    """calibration.json (written by the trainer) is the app's wake threshold;
    absent/malformed/out-of-range falls back to SP3's 0.5."""
    assert wake_threshold_from_calibration(tmp_path) == WAKE_THRESHOLD
    (tmp_path / "calibration.json").write_text(json.dumps({"threshold": 0.72}))
    assert wake_threshold_from_calibration(tmp_path) == 0.72
    (tmp_path / "calibration.json").write_text(json.dumps({"threshold": 9.9}))
    assert wake_threshold_from_calibration(tmp_path) == WAKE_THRESHOLD
    (tmp_path / "calibration.json").write_text("not json")
    assert wake_threshold_from_calibration(tmp_path) == WAKE_THRESHOLD


def test_app_uses_calibrated_threshold(tmp_path: Path) -> None:
    model_dir = tmp_path / "wake"
    model_dir.mkdir()
    (model_dir / "hey-marshmellow.onnx").write_bytes(b"stub model bytes")
    (model_dir / "calibration.json").write_text(json.dumps({"threshold": 0.65}))
    app = App(
        Settings(activation="wake_word"),
        headless=True,
        log_dir=tmp_path,
        audio_engine=None,
        wake_model_dir=model_dir,
    )
    try:
        assert app.wake_watcher is not None
        assert app.wake_watcher._threshold == 0.65
    finally:
        app.stop()


# -------------------------------------------- trainer pure half (headless tier)


def test_manifest_is_deterministic_given_seed() -> None:
    a = build_manifest("hey marshmellow", 12, VOICES, seed=42)
    b = build_manifest("hey marshmellow", 12, VOICES, seed=42)
    assert a == b  # same seed → byte-identical synthesis manifest
    c = build_manifest("hey marshmellow", 12, VOICES, seed=7)
    assert c["synthesis"] != a["synthesis"]  # different seed → different jitter


def test_manifest_shapes_the_synthesis_run() -> None:
    manifest = build_manifest("hey marshmellow", 12, VOICES, seed=42)
    assert manifest["phrase"] == "hey marshmellow" and manifest["seed"] == 42
    assert manifest["voices"] == {VOICES[0]: 6, VOICES[1]: 6}  # round-robin split
    rows = manifest["synthesis"]
    assert len(rows) == 12
    assert all("marshmellow" in row["text"].lower() for row in rows)
    assert all(row["voice"] in VOICES for row in rows)
    # augmentation jitter the synthesizer applies per row — the speed band is
    # deliberately narrow (a wide resample stretch shifts pitch and blurs the
    # marshmellow/marshmallow phoneme contrast the classifier must learn)
    assert all(0.7 <= row["gain"] <= 1.3 for row in rows)
    assert all(0.97 <= row["speed"] <= 1.03 for row in rows)
    # the negative list is recorded in the manifest (--plan shows it)
    assert "hey marshmallow" in manifest["negatives"]["lookalikes"]
    assert manifest["negatives"]["background"] == ["pink_noise", "silence"]


def test_calibration_threshold_is_smallest_qualifying_cutoff() -> None:
    """threshold = smallest score cutoff whose false-accept rate ≤ target."""
    scores = [0.1, 0.2, 0.3, 0.9]
    assert calibrate_threshold(scores, 0.25) == 0.9  # 1/4 = 0.25 ≤ 0.25; smaller fail
    assert calibrate_threshold(scores, 0.6) == 0.3  # 2/4 = 0.5 ≤ 0.6
    assert calibrate_threshold(scores, 1.0) == 0.1  # everything qualifies → smallest
    assert calibrate_threshold([0.2, 0.4], 0.01) > 0.4  # none qualifies → above all
