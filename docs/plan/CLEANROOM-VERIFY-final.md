# Cleanroom Verify — FINAL (Marshmellow v0.1.0 release candidate)

**Date:** 2026-11-07 · **Round:** independent re-verification 1 (previous verdict **FAIL** at `4797dca`, same session)
**Verified commit:** `746d2016fe41d7cd2a1331d6df9b83a7c4d37219` (HEAD of `main` at clone time)
**Method:** fresh clone into an empty system-temp directory, `uv sync` from committed files only, then the three gates run **un-piped** inside the clone. Clone removed afterwards. This file is the only artifact written to the main tree; nothing was committed. (This round's report replaces the previous content, which carried the `4797dca` FAIL report plus the release fixer's own fix-verification addendum — both were committed in `746d201` and remain in git history.)

## Verdict

**CLEANROOM PASS** — clone, `uv sync`, and all three gates (pytest / ruff / mypy) succeeded from the committed tree alone, each with exit code 0. The single blocker of the previous round (`tests/audio/test_activation.py::test_self_trained_model_detects_piper_phrase` → exit 1) is fixed by `219d1c6` committing the two openwakeword feature extractors; the wake-scorer path now genuinely runs in a clean checkout, and a guard test pins the fix.

| Gate (exact command, un-piped, in the clone) | Exit | Output |
|---|---|---|
| `uv run pytest -q` | **0** | `492 passed, 5 skipped, 17 deselected in 80.93s (0:01:20)` |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run mypy` | **0** | `Success: no issues found in 43 source files` |

## 0. What changed since the previous FAIL

Two commits on top of `4797dca` (the previously verified head):

- **`219d1c6` "fix(release): commit the wake feature extractors — clean checkouts could not score"** — the fix for the previous round's root cause. Commits `assets/wake/melspectrogram.onnx` (1,087,958 bytes) and `assets/wake/embedding_model.onnx` (1,326,578 bytes) (verified via `git ls-files assets/wake/` → 7 entries; `ls -la` sizes), negates them in `.gitignore` (+7/−1), adds a guard test to `tests/audio/test_activation.py` (+15), updates `installer/marshmellow.spec` and `assets/wake/README.md`. `uv.lock`/`pyproject.toml` untouched since `4797dca` (`git diff --stat 4797dca..746d201 -- uv.lock pyproject.toml` → empty).
- **`746d201` "docs(release): audit + cleanroom-verify reports; changelog records the extractor fix"** — docs only (committed the previously untracked `docs/plan/RELEASE-AUDIT-final.md` and the previous round's cleanroom report, closing the previous round's §5 finding).

## 1. Clone

- Created `C:\Users\DELL\AppData\Local\Temp\marshmellow-cleanroom-r1-1648` (empty: `ls -A` count 0), cloned `C:/Users/DELL/OneDrive/Documents/Projects/Marshmellow` into it → exit 0.
- Clone HEAD `git rev-parse` → `746d2016fe41d7cd2a1331d6df9b83a7c4d37219` (identical to source); `git status --porcelain` empty; 201 tracked files.
- After all runs below the clone's tracked tree was still pristine (`git status --porcelain` and `git diff --stat` both empty — only ignored `.venv/` + `.pytest_cache/` were added). Clone removed afterwards; absence verified (`ls` → "No such file or directory").

## 2. uv sync

`uv sync` → **exit 0** (uv 0.13.6). Installed purely from the committed `pyproject.toml` + `uv.lock` (`uv pip list` → 88 lines incl. headers ≈ 86 packages + the local `marshmellow==0.1.0` built from the clone). Since the committed lock and `pyproject.toml` are byte-identical to `4797dca` (§0), the resolved dependency set is the same as the previous round. **Not directly captured this round:** the interpreter line of `uv sync`'s output was truncated to its tail before the clone was removed; the previous round's identical lock under the same uv resolved to CPython 3.14.0 (`C:\Python314`, read from both `pyvenv.cfg` files last round) — treat the interpreter version as inferred from that, not observed this round. Supplementary `uv lock --check` → **exit 0** (`Resolved 170 packages in 3ms`). As before, `openwakeword/resources/` does **not** exist in the fresh site-packages (`ls` → "No such file or directory") — the wheel ships none — and that no longer matters: the scorer points at the committed files (§4).

## 3. Gates — full account

- **pytest** — `uv run pytest -q` → **exit 0** in 80.93 s: `492 passed, 5 skipped, 17 deselected`. First and only invocation this round exited normally (previous round's one-off post-suite hang — ~22 min, killed, never reproduced there — did not recur).
- **ruff** — `uv run ruff check .` → **exit 0**: `All checks passed!`
- **mypy** — `uv run mypy` → **exit 0**: `Success: no issues found in 43 source files`

## 4. Fix mechanism — verified in the clone, not just trusted from the commit message

- `src/marshmellow/audio/activation.py:399-409` (`openwakeword_scorer`): resolves `melspec = model_dir / "melspectrogram.onnx"` and `embedding = model_dir / "embedding_model.onnx"`; when both exist it passes them as explicit `"melspec_model_path"` / `"embedding_model_path"` kwargs to `openwakeword.Model` (comment at `:405`: "explicit preprocessor paths — the openwakeword wheel ships no resources"). Both files are now committed in `assets/wake/` — the `model_dir` the test uses — so the scorer loads with zero dependence on package resources. The wake-model glob at `:392-395` correctly excludes the two extractors from `wakeword_models`.
- New guard test `tests/audio/test_activation.py:327` `test_feature_extractors_are_committed` asserts both extractor files exist and, on failure, instructs restoring the `.gitignore` negation — a loud one-line failure instead of the old 30 s wake timeout. It is among the 492 passing.
- Test arithmetic reconciles with the previous round: 490 passed + 1 failed = 491 ran at `4797dca`; +1 new guard test = 492 ran, all passing now. No test was removed or skipped away.

## 5. Missing from git / clean-checkout gaps

- **Untracked files: none.** `git status --porcelain` in the source tree at clone time is empty — the clean checkout lacks nothing that exists in the dev tree (the previous round's single untracked file was committed in `746d201`).
- **Remaining skips — by-design, dev-gated** (verified identical to the previous round's census via a piped supplementary run `uv run pytest -q -rs` → same `492 passed, 5 skipped, 17 deselected`; piped, so reported as the census, not a gate):
  - `SKIPPED [3] tests\core\test_autostart.py:111` — "exercises the non-win32 guard — cannot run on a real Windows host" (platform-inherent).
  - `SKIPPED [1] tests\audio\test_activation.py:293` — "bundled dev wake models / phrase clip not present on this machine" (per `219d1c6`'s message: CC BY-NC-SA dev models the D8 ship-rule forbids bundling — deliberate dev-tier gating).
  - `SKIPPED [1] tests\stt\test_fasterwhisper.py:103` — "SP2 fixtures not present on this machine" (dev-gated tier).
  - The gitignored dev-artifact dirs (`build/`, `dist/`, `models/`, `voices/`) remain absent from the cleanroom; the two tests that need them skip with a documented reason rather than fail. 17 further tests are `manual`-marked and deselected by design (`pyproject.toml` `addopts`).
- Nothing tracked-and-required is missing from git: `uv sync` and the full suite built and ran purely from committed files.

## 6. Verdict rationale

The ask's standard is PASS only if every gate exits 0. This round: pytest 0, ruff 0, mypy 0 — all from the committed tree alone, in a clone removed after the run, with the fix mechanism independently read in the clone (§4). Verdict: **PASS**. (Supplementary, not gates: `uv lock --check` exit 0; skip census; clone pristine after all runs.)
