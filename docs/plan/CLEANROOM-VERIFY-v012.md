# Marshmellow — v0.1.2 Cleanroom Verify

**Date:** 2026-11-14 · **Verification round 2** (supersedes the round-1 content of this file;
round 0 PASS at `a3cfb3f`, round 1 PASS at `fda062c`)
**Candidate:** HEAD `fda062c3798f3f8ca0bb07898cee1f200e93f6ea` (`fda062c` — `chore(release):
refresh uv.lock self-version to 0.1.2`) — **unchanged since round 1**; `git log fda062c..HEAD`
is empty, so this round re-verifies the same candidate on a fresh clone.

## Verdict

**PASS — every gate exited 0 from the fresh clean checkout; second consecutive hang-free full
pytest run.**

| Gate | Command (un-piped, run inside the clone) | Exit | Output |
|---|---|---|---|
| Sync | `uv sync` | 0 | CPython 3.14.0, 86 packages installed, `marshmellow==0.1.2` built from the clone; clone working tree **clean afterwards** — no lock rewrite |
| Tests | `uv run pytest -q` | 0 | `573 passed, 6 skipped, 17 deselected in 339.77s` — first attempt, **no shutdown hang** |
| Lint | `uv run ruff check .` | 0 | `All checks passed!` |
| Types | `uv run mypy` | 0 | `Success: no issues found in 43 source files` |

## 1. Method

1. Main-tree facts: `git rev-parse HEAD` → `fda062c3798f3f8ca0bb07898cee1f200e93f6ea`;
   `git status --porcelain=v1` → only the two untracked plan docs (`CLEANROOM-VERIFY-v012.md`,
   `RELEASE-AUDIT-v012.md`); no commits since round 1.
2. Cloned into an empty system-temp dir: `git clone --no-hardlinks <source>
   C:\Users\DELL\AppData\Local\Temp\tmp.Y6J5cI4hq5\Marshmellow`. Clone HEAD matched; clone
   porcelain empty — pure committed state.
3. `uv sync` inside the clone, un-piped, from committed `pyproject.toml` + committed `uv.lock`
   only (fresh `.venv`; uv 0.13.6; `SYNC_EXIT=0`). `git status --porcelain` afterwards: **empty**
   — the committed lock remains consistent with the committed pyproject (round-0's stale-lock
   finding stays fixed at `fda062c`).
4. Gates run un-piped, one at a time, in the clone. Exact commands as in the table.
5. Clone removed: `rm -rf /c/Users/DELL/AppData/Local/Temp/tmp.Y6J5cI4hq5` → verified `GONE`.
   Nothing committed; no main-tree file touched except this report.

## 2. The pytest gate — stability continues

- `uv run pytest -q` reached `[100%]` and **exited 0** on the first attempt:
  `573 passed, 6 skipped, 17 deselected in 339.77s`. Counts identical to round 1 (573/6/17);
  the longer wall clock vs round 1 (339.77s vs 164.94s) is ambient desktop load, not new code
  — nothing changed between the two rounds. Skip pattern identical (6 `s` markers, no `F`/`E`
  anywhere).
- The round-0 hang fix (commit `fda062c`'s `tests/conftest.py` `_close_lingering_event_loops()`
  sweep in a `tryfirst` `pytest_unconfigure` hookimpl, covered by
  `tests/test_shutdown_hygiene.py`) has now produced **two consecutive clean exits** (rounds 1
  and 2) versus one hang in three runs under round 0's code. The round-0 caveat stands in
  weaker form: two hang-free runs cannot prove absence of every possible hang, but the swept
  mechanism (unclosed proactor loops reaching `pytest_unconfigure`'s `gc_collect_harder` pass,
  which parked in `IocpProactor._poll`) is structurally removed before that pass.

## 3. Release-hygiene notes for this round

- **Stale committed `uv.lock` — stays RESOLVED** at `fda062c`: `uv sync` leaves the clone tree
  clean (verified both rounds since the fix).
- **Untracked files a clean checkout lacks** (gates ran without them; nothing in the suite
  reads them): `docs/plan/RELEASE-AUDIT-v012.md` and `docs/plan/CLEANROOM-VERIFY-v012.md`
  (this report, written after all clone gates ran). Ignored artifacts present only in the main
  tree (verified in round 1 via `git status --ignored --porcelain`; unchanged this round):
  `.venv/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.zcode/*`, `build/`, `dist/`,
  `Marshmellow-v0.1.0-win64.zip`, `Marshmellow-v0.1.1-win64.zip`, and `docs/plan/spikes/data/*`
  — the spike fixtures that keep three tests skipped in a clean checkout (571→574 passing on
  the dev machine) but not on it.
- Clone `git status --porcelain` after all gates: **empty**. HEAD unchanged throughout:
  `fda062c3798f3f8ca0bb07898cee1f200e93f6ea`.

## 4. What was and was not run

- **Run here, in the clone, un-piped:** `uv sync`; `uv run pytest -q`; `uv run ruff check .`;
  `uv run mypy`. All commands and outputs recorded above. The 6 skips are the same
  environment-conditional set recorded in round 1 (non-win32 autostart guard ×3, SP2 fixtures,
  dev wake models, piper voice).
- **Not run:** the frozen-bundle smoke (`dist/` is ignored, absent from a clean checkout; last
  verified in RELEASE-AUDIT-v012.md §6.6), `uv lock --check`, any GUI/manual pass, any
  live-provider call. Round-0 py-spy diagnostics not re-run — no hang occurred.
- **Environment:** Windows 10.0.26300 x64, Git Bash, uv 0.13.6, CPython 3.14.0
  (`C:\Python314`), pytest 9.2.0, ruff 0.17.3, mypy 2.4.1 (installed by `uv sync` from the
  committed lock into the clone's fresh venv).
