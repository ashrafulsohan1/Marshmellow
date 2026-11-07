# assets/wake — the self-trained "hey marshmellow" wake model (D8)

M5 Task 5. Trained by `tools/train_wake_model.py` (see that file and
`docs/plan/tasks/2026-11-04-M5-polish-release.md` Task 5 for the method); the
threshold decision lives in `calibration.json` next to the model and is read by
the app (`audio/activation.wake_threshold_from_calibration`).

## What ships (and what never ships)

| File | Tracked? | What it is |
|---|---|---|
| `hey-marshmellow.onnx` | **yes** (`.gitignore` negation) | the self-trained wake model — trained here, on fully synthetic piper speech |
| `calibration.json` | yes | threshold calibration from the negative-score histogram |
| `manifest.json` | yes | the synthesis manifest (utterances per voice, negative list, seed) |
| `fixture-hey-marshmellow.wav` | yes | held-out piper-synthesized phrase for the D8 acceptance test (`tests/audio/test_activation.py`) |
| `melspectrogram.onnx` | no (re-downloadable) | openWakeWord feature extractor — license check below |
| `embedding_model.onnx` | no (re-downloadable) | openWakeWord feature extractor — license check below |

Bundled **pretrained** openWakeWord models (`alexa`, `hey_jarvis`, …) NEVER ship:
they are CC BY-NC-SA 4.0 (non-commercial) and dev/test-only
(`MARSHMELLOW_DEV_MODELS=1` tier, `docs/plan/spikes/data/sp3_dev_models/`).

## License check (recorded, per Global Constraint D8)

Checked 2026-11-07 against the installed `openwakeword 0.6.0` wheel metadata
(`openwakeword-0.6.0.dist-info/METADATA`, "# License" section) and its stated
model provenance:

- **openwakeword code** — Apache-2.0.
- **`melspectrogram.onnx`** — Apache-2.0. The METADATA states the feature
  backbone "was manually re-implemented" by openWakeWord from Google's
  TFHub `speech_embedding` pipeline; the mel frontend is that Apache-2.0
  re-implementation (a deterministic transform, not a dataset-trained
  classifier — the CC BY-NC-SA clause covers the pretrained wake-word models'
  training data, not these extractors).
- **`embedding_model.onnx`** — Apache-2.0: Google's `speech_embedding` model
  ([tfhub.dev/google/speech_embedding/1](https://tfhub.dev/google/speech_embedding/1)),
  "provided by Google as a TFHub module under an Apache-2.0 license" (METADATA,
  feature-backbone paragraph).
- The CC BY-NC-SA 4.0 restriction applies to the **pretrained wake-word models**
  ("due to the inclusion of datasets with unknown or restrictive licensing as
  part of the training data") — none of which ship.
- The self-trained `hey-marshmellow.onnx` inherits only Apache-2.0 inputs
  (openwakeword code + the two extractors above; training audio synthesized by
  piper voices downloaded via `python -m piper.download_voices` — voice model
  licenses recorded with the voices, never bundled here).

**Result: SHIPPABLE** — the wake path (self-trained model + both feature
extractors) is Apache-2.0-clean. Task 7's installer checklist still makes the
ship call explicitly (D8: never a silent default).

## Calibration

Trained 2026-11-07 (`tools/train_wake_model.py --phrase "hey marshmellow"
--utterances 1000 --seed 42 --negatives-per-text 60 --steps 50000`; three
en_US piper voices, 1000 positives + 960 lookalike/generic phrase negatives +
1000 SP3 pink-noise/silence background clips, 2 s clips, openwakeword
`auto_train` 50000 steps). Method: score every negative frame through the real
scorer, take the smallest cutoff whose false-accept rate is within target
(`calibrate_threshold`), and keep SP3's 0.5 unless the histogram demands a
higher one — recorded, not silently changed. Full numbers in
`calibration.json`; the headline:

- **threshold = 0.99** (`sp3_default_kept: false`) — SP3's 0.5 has OBSERVED
  false accepts (the lookalike class, below), so the histogram said otherwise.
- **positives**: min clip score 0.9885, max 0.9996 (100 held-out clips) — the
  piper phrase fixture fires well inside the 1.5 s onset budget (D8 acceptance
  row in `tests/audio/test_activation.py`).
- **background (pink noise + silence)**: max frame score 0.0001 — SP3's
  noise-floor result holds for the self-trained model.
- **real human speech** (SP3's dev clips, real "alexa" / "hey mycroft"
  utterances): max frame score 0.0001 — the model transfers off TTS.
- **known limitation (recorded, not hidden)**: single-phoneme lookalikes
  ("hey marshmallow", "hey mellow") still reach 0.999 on some frames — at any
  usable threshold (the watcher rejects ≥ 1.0) a spoken lookalike can false-
  wake. Generic speech separates cleanly (p99 = 0.11). Ambient false accepts
  are exactly what the soak measures; the "trusted hands-free" call stays with
  the Task 7 checklist (wake-works-for-the-real-user is a user-physical check).

- **live ambient soak (2026-11-07, 1.00 h, threshold 0.99): 7 false accepts/hour**
  (`docs/plan/spikes/data/m5_wake_soak.json`, scores 0.991-0.999) — NOT ready to be
  trusted hands-free; the hotkey stays the trusted activation (D2) and the
  hands-free call is Task 7's checklist item. Discharges the SP3 carry-over
  soak MEASUREMENT (pending-user-verifications.md).
