# Cleanroom Verify — FINAL (Marshmellow v0.1.0 release candidate)

**Date:** 2026-11-07 · **Round:** independent re-verification 2 (previous round 1 verdict **PASS** at `746d201`, same session)
**Verified commit:** `c6dc40d90655ed6b0891dd3fe84e82efd0edf04c` (HEAD of `main` at clone time)
**Method:** fresh clone into an empty system-temp directory, `uv sync` from committed files only, then the three gates run **un-piped** inside the clone. Clone removed afterwards. This file is the only artifact written to the main tree; nothing was committed.

## Verdict

**CLEANROOM PASS** — clone, `uv sync`, and all three gates (pytest / ruff / mypy) succeeded from the committed tree alone, each with exit code 0, re-confirming round 1 at a head whose code is byte-identical to round 1's (the one new commit is docs-only).

| Gate (exact command, un-piped, in the clone) | Exit | Output |
|---|---|---|
| `uv run pytest -q` | **0** | `492 passed, 5 skipped, 17 deselected in 99.32s (0:01:39)` |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run mypy` | **0** | `Success: no issues found in 43 source files` |

## 0. What changed since round 1

One commit on top of `746d201` (round 1's verified head):

- **`c6dc40d` "docs(release): verification round 1 — both HIGH items re-confirmed fixed, cleanroom PASS"** — touches only `docs/plan/CLEANROOM-VERIFY-final.md` (+88/− lines) and `docs/plan/RELEASE-AUDIT-final.md`; verified zero code impact: `git diff --stat 746d201..c6dc40d -- src tests pyproject.toml uv.lock assets installer tools` → **empty**. The code, tests, lock, and assets under test are therefore byte-identical to round 1, where the fix mechanism was read in the clone (`src/marshmellow/audio/activation.py:399-409` — explicit `melspec_model_path`/`embedding_model_path` from the committed `assets/wake/` extractors; guard test `tests/audio/test_activation.py:327`).

## 1. Clone

- Created `C:\Users\DELL\AppData\Local\Temp\marshmellow-cleanroom-r2-2016` (empty: `ls -A` count 0), cloned `C:/Users/DELL/OneDrive/Documents/Projects/Marshmellow` into it → exit 0.
- Clone HEAD `git rev-parse` → `c6dc40d90655ed6b0891dd3fe84e82efd0edf04c` (identical to source); `git status --porcelain` empty; 201 tracked files; `git ls-files assets/wake/` → 7 entries (both feature extractors still tracked).
- After all runs below the clone's tracked tree was still pristine (`git status --porcelain` and `git diff --stat` both empty — only ignored `.venv/` + `.pytest_cache/` were added). Clone removed afterwards; absence verified (`ls` → "No such file or directory").

## 2. uv sync

`uv sync` → **exit 0** (uv 0.13.6). **Interpreter captured this round** (round 1's truncated-output caveat): `Using CPython 3.14.0 interpreter at: C:\Python314\python.exe`. Resolved 170 packages, built `marshmellow==0.1.0` from the clone itself, installed **86 packages** — purely from the committed `pyproject.toml` + `uv.lock`. Supplementary `uv lock --check` → **exit 0** (`Resolved 170 packages in 3ms`). As in both prior rounds, `openwakeword/resources/` does not exist in the fresh site-packages — the wheel ships none; the scorer's explicit-path branch (§0) is what keeps the wake tests green.

## 3. Gates — full account

- **pytest** — `uv run pytest -q` → **exit 0** in 99.32 s: `492 passed, 5 skipped, 17 deselected`. First and only invocation, no hang, no failure (matches round 1's 492/5/17 exactly — test census unchanged).
- **ruff** — `uv run ruff check .` → **exit 0**: `All checks passed!`
- **mypy** — `uv run mypy` → **exit 0**: `Success: no issues found in 43 source files`

## 4. Missing from git / clean-checkout gaps

- **Untracked files: none.** `git status --porcelain` in the source tree at clone time is empty — the clean checkout lacks nothing that exists in the dev tree.
- **Remaining skips — by-design, dev-gated**, verified identical to rounds 0/1 via a piped supplementary run (`uv run pytest -q -rs` → same `492 passed, 5 skipped, 17 deselected`; piped, so reported as the census, not a gate):
  - `SKIPPED [3] tests\core\test_autostart.py:111` — "exercises the non-win32 guard — cannot run on a real Windows host" (platform-inherent).
  - `SKIPPED [1] tests\audio\test_activation.py:293` — "bundled dev wake models / phrase clip not present on this machine" (CC BY-NC-SA dev models the D8 ship-rule forbids bundling — deliberate dev-tier gating).
  - `SKIPPED [1] tests\stt\test_fasterwhisper.py:103` — "SP2 fixtures not present on this machine" (dev-gated tier).
  - The gitignored dev-artifact dirs (`build/`, `dist/`, `models/`, `voices/`) remain absent from the cleanroom; the two tests that need them skip with a documented reason rather than fail. 17 further tests are `manual`-marked and deselected by design (`pyproject.toml` `addopts`).
- Nothing tracked-and-required is missing from git: `uv sync` and the full suite built and ran purely from committed files.

## 5. Verdict rationale

The ask's standard is PASS only if every gate exits 0. This round: pytest 0, ruff 0, mypy 0 — all from the committed tree alone, in a clone removed after the run, against code byte-identical to the round-1-verified head (§0). Verdict: **PASS**. (Supplementary, not gates: `uv lock --check` exit 0; skip census; clone pristine after all runs.)
