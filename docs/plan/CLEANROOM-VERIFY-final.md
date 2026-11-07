# Cleanroom Verify — FINAL (Marshmellow v0.1.0 release candidate)

**Date:** 2026-11-07 · **Verified commit:** `4797dca846a971582495cea5f21b36802deef37a` (HEAD of `main` at clone time)
**Method:** fresh clone into an empty system-temp directory, `uv sync` from committed files only, then the three gates run **un-piped** inside the clone. Clone removed afterwards. This file is the only artifact written to the main tree; nothing was committed.

## Verdict

**CLEANROOM FAIL** — the pytest gate does not exit 0 from a clean checkout: `1 failed, 490 passed, 5 skipped, 17 deselected` → exit **1**, reproduced three times. `ruff` and `mypy` pass. The failure is a real clean-checkout reproducibility gap, not flakiness (see §4): `uv sync` does not produce an environment in which the openwakeword scorer can load, so the D8 acceptance test cannot pass anywhere except a dev venv where its model resources were fetched by hand.

| Gate (exact command, un-piped, in the clone) | Exit | Output |
|---|---|---|
| `uv run pytest -q` | **1** | `1 failed, 490 passed, 5 skipped, 17 deselected in 139.52s` (failed: `tests/audio/test_activation.py::test_self_trained_model_detects_piper_phrase`) |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run mypy` | **0** | `Success: no issues found in 43 source files` |

## 1. Clone

- Created `C:\Users\DELL\AppData\Local\Temp\marshmellow-cleanroom-1186` (empty: `ls -A` count 0), cloned `C:/Users/DELL/OneDrive/Documents/Projects/Marshmellow` into it → exit 0.
- Clone HEAD `git rev-parse` → `4797dca846a971582495cea5f21b36802deef37a` (identical to source); `git status --porcelain` empty; 197 tracked files (identical to source).
- After all runs below, the clone's tracked tree was still pristine (`git status --porcelain` empty, `git diff --stat` empty — only ignored `.venv/` and `.pytest_cache/` were added). Clone removed afterwards and verified absent.

## 2. uv sync

`uv sync` → **exit 0**. uv 0.13.6, CPython 3.14.0 (same interpreter as the main tree's venv — both `pyvenv.cfg` show `home = C:\Python314`, so interpreter version is not a variable here). Resolved 170 packages, built `marshmellow==0.1.0` from the clone itself, installed 86 packages. Everything came from the committed `pyproject.toml` + `uv.lock`; supplementary `uv lock --check` → **exit 0** (`Resolved 170 packages in 2ms`).

## 3. Gates — full account

### pytest — FAIL

Three full-suite invocations in the clone:

1. **Run A — the ask's exact command `uv run pytest -q`** (started 18:13:54): all tests executed (progress reached `[100%]`, one `F`), pytest's session-finish ran (`.pytest_cache/v/cache/lastfailed` written 18:16 with the single entry below) — **but the process never exited**: still alive at 18:35 with no summary printed and therefore no exit code. Killed after ~22 min. A stack dump was not obtainable: `uvx py-spy dump` failed with `os error 87 (The parameter is incorrect)` on every PID of the tree (Windows build 10.0.26300). **The hang was not reproduced**: Run B and Run C (below) both exited normally, including a re-run of this exact command, so it is reported as a one-off, unexplained anomaly — the first invocation's gate result is "no exit code delivered", and the failure was characterized by Runs B/C.
2. **Run B — diagnostic** (`uv run python -u -c "... pytest.main(['-q'])"` wrapped in `faulthandler.dump_traceback_later(420, exit=True)` so a hang would dump all thread stacks and hard-exit): completed normally in **102.21s** → `1 failed, 490 passed, 5 skipped, 17 deselected`, pytest exit **1**. The watchdog never fired (no hang).
3. **Run C — the exact command again** (`uv run pytest -q`): completed in **139.52s** → `1 failed, 490 passed, 5 skipped, 17 deselected`, **`PYTEST_EXIT=1`**.

The failure is byte-identical in all three runs:

```
onnxruntime.capi.onnxruntime_pybind11_state.NoSuchFile: [ONNXRuntimeError] : 3 : NO_SUCHFILE :
Load model from ...\repo\.venv\Lib\site-packages\openwakeword\resources\models\melspectrogram.onnx
failed: ... File doesn't exist
```

### ruff — PASS

`uv run ruff check .` → **exit 0**, `All checks passed!`

### mypy — PASS

`uv run mypy` → **exit 0**, `Success: no issues found in 43 source files`

## 4. Root cause of the pytest failure (verified on disk, with the mechanism)

- **Trigger:** `tests/audio/test_activation.py::test_self_trained_model_detects_piper_phrase` (the D8 acceptance test; `skipif not WAKE_MODEL_READY` — the self-trained model IS committed at this HEAD as `assets/wake/hey-marshmellow.onnx`, so the test runs). It starts a `WakeWordWatcher` and waits 30 s for `WakeDetected` (`tests/audio/test_activation.py:363` → `_wait` at `:38` → `Failed: condition not met within timeout`).
- **Mechanism:** the watcher's scorer factory `openwakeword_scorer` builds an `openwakeword.Model(...)` (`src/marshmellow/audio/activation.py:410`); openwakeword's `AudioFeatures.__init__` then loads `melspectrogram.onnx` from **its own package resources**. In the cleanroom venv that load raises `NoSuchFile` (traceback above); the watcher thread catches it, logs `wake scorer failed to load` (`src/marshmellow/audio/activation.py:468`), never publishes `WakeDetected`, and the test times out.
- **Why the file is missing:** the openwakeword **0.6.0 wheel ships no `resources/` directory at all** — verified: `.venv/Lib/site-packages/openwakeword/resources/` does not exist in the cleanroom venv, while the main tree's venv contains `melspectrogram.onnx` + `embedding_model.onnx` (manually materialized there at some point, e.g. via `openwakeword.utils.download_models()` — the repo's own tooling documents this: `tools/train_wake_model.py:57` "wheel ships no resources — see utils.download_models' .tflite→.onnx swap"; `tools/spikes/sp3_wakeword.py:70` calls it). **Nothing in `uv sync`, the package build, or the test suite fetches these resources** — they are an undeclared environment dependency.
- **Consequence for the release record:** the pre-publish audit's full-suite pass at this same HEAD (`493 passed, 3 skipped` — `docs/plan/RELEASE-AUDIT-final.md` §9.1) ran in the dev venv where the resources exist by accident of history; it did not verify the committed tree. The earlier cleanroom pass (`docs/plan/CLEANROOM-VERIFY.md`, at `40922bc`) also never exercised this path — the self-trained model was not committed yet and this test was skipped.
- **Fix direction (for the release owner, not applied here):** make the resources part of the committed environment — a session-scoped test fixture calling `openwakeword.utils.download_models()`, a `uv sync` post-install step, or vendor the two ONNX files — or gate the test with `skipif`-resources-missing like its siblings (`test_activation.py:293`).

## 5. Missing from git / clean-checkout gaps

- **Untracked file (the clean checkout lacks exactly one):** `docs/plan/RELEASE-AUDIT-final.md` — the only `??` in `git status --porcelain`, not gitignored (`git check-ignore -v` exit 1), simply uncommitted. Documentation only (the pre-publish audit report); no gate depends on it. It is also **not** in the clone used for this verification.
- **Gitignored dev-tree artifacts present in the main tree, absent from the cleanroom:** `build/`, `dist/`, `models/`, `voices/` (present per `ls -d`; ignored per `.gitignore`). Two committed tests silently **skip** without them, found via a piped supplementary run (`uv run pytest -q -rs` — piped, so reported as the skip census, not a gate): `tests/audio/test_activation.py:293` "bundled dev wake models / phrase clip not present on this machine" and `tests/stt/test_fasterwhisper.py:103` "SP2 fixtures not present on this machine". The release audit's main-tree run had only the 3 platform skips (`tests/core/test_autostart.py:111` ×3, "cannot run on a real Windows host") — so a clean checkout quietly loses 2 tests of coverage on top of the 1 that fails. Remaining 3 skips are platform-inherent (same everywhere).
- Nothing tracked-and-required is missing from git: `uv sync` and all non-wake tests built and ran purely from committed files.

## 6. Verdict rationale

The ask's standard is PASS only if every gate exits 0. `uv run ruff check .` and `uv run mypy` exited 0. `uv run pytest -q` exited **1** (twice measured: Runs B and C; Run A delivered no exit code before being killed). Verdict: **FAIL**, with the single deterministic blocker and its root cause documented in §4.

## 7. Addendum — the blocker fixed and re-verified (2026-11-07, release fixer)

Fix commit **`219d1c6`** (`fix(release): commit the wake feature extractors — clean checkouts could not score`): the §4 root cause is closed by tracking the two openwakeword feature extractors beside the wake model (`!assets/wake/melspectrogram.onnx` / `!assets/wake/embedding_model.onnx` negations — Apache-2.0, the license check was already recorded in `assets/wake/README.md`), so `openwakeword_scorer`'s explicit-path branch (`src/marshmellow/audio/activation.py:404`) engages everywhere; the guard test `tests/audio/test_activation.py::test_feature_extractors_are_committed` keeps a missing extractor a loud assert. The §5 untracked file is closed by committing this file and `RELEASE-AUDIT-final.md`. The §5 extra skips are NOT changed: both are by-design dev-gated tiers (bundled pretrained wake models are CC BY-NC-SA — D8 forbids shipping them; SP2 fixtures are gitignored spike data), each with a documented skip reason.

**Re-verification, same method as §1–§3**: fresh clone of committed `219d1c609642c6ef7c35b302ad3f431d4e07b176` into empty `C:\Users\DELL\AppData\Local\Temp\marshmellow-cleanroom-fix` (clone clean, HEAD identical to source, 199 tracked files = 197 + the two ONNX); `uv sync` → **exit 0** (uv 0.13.6, CPython 3.14.0, 86 packages, `marshmellow==0.1.0` built from the clone, `openwakeword==0.6.0`). The §4 precondition re-created exactly: the clone's `.venv/Lib/site-packages/openwakeword/resources` does **not** exist, while `assets/wake` carries both extractors from git. Gates un-piped in the clone:

| Gate (exact command, un-piped, in the clone) | Exit | Output |
|---|---|---|
| `uv run pytest -q` | **0** | `492 passed, 5 skipped, 17 deselected in 105.28s` — the D8 acceptance test now passes from a clean checkout |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run mypy` | **0** | `Success: no issues found in 43 source files` |

The 5 skips are the 3 platform-inherent plus the 2 dev-gated tiers from §5. No exit hang occurred. The clone's tracked tree was pristine after all runs (`git status --porcelain` and `git diff --stat` empty); the clone was removed and verified absent.

Main-tree gates at the same commit, also un-piped: `uv run pytest -q` → `494 passed, 3 skipped, 17 deselected in 138.10s`; `uv run ruff check .` → `All checks passed!`; `uv run mypy` → `Success: no issues found in 43 source files`.
