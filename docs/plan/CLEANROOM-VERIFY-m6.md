# Cleanroom Verify — M6 rebuilt-bundle candidate

**Date:** 2026-11-08 · **Rounds:** round 0 at `864e4629b483597ba67e672ef53f28b175401bb7`
(HEAD of `main`, pre-fix) → **FAIL (1 test)** → fix round 1 (test-only) → **round 2
re-verification: PASS**. The round-2 clone = HEAD `864e462` + the Task 7 working-tree diff
(`tests/ui/test_identity.py`, +14/−2 — the only uncommitted change in the source tree at clone
time; `git status --porcelain` showed exactly ` M tests/ui/test_identity.py`). The Task 7
commit's code content is therefore identical to what round 2 verified.
**Method:** fresh clone into an empty system-temp directory, gates run **un-piped** inside the
clone, clone removed afterwards. This file is the only artifact written to the main tree
(besides the fix itself and [RELEASE-AUDIT-m6.md](RELEASE-AUDIT-m6.md)).

## Verdict

**CLEANROOM PASS (round 2)** — clone, `uv sync`, and all three gates (pytest / ruff / mypy)
succeeded from the committed tree plus the one test-only fix, each with exit code 0.
Round 0's single failure is the clean-checkout quit-census gap (F1 in the audit report): the
census needs a piper worker, a clean checkout has no `voices/` (model binaries stay out of git),
so the composition root degrades to FakeTTS and spawns nothing to census. Fix round 1 makes the
census skip with a documented reason when no voice exists — the repo's established dev-gated
tier (wake dev-models, SP2 fixtures) — keeping it at full strength wherever a voice exists
(dev tree: still runs and passes; the frozen bundle ships a voice).

| Gate (exact command, un-piped, in the clone) | Round 0 (`864e462`) | Round 2 (`864e462` + fix) |
|---|---|---|
| `uv sync` | **0** | **0** |
| `uv run pytest -q` | **1** — `1 failed, 556 passed, 5 skipped, 17 deselected in 179.26s` | **0** — `556 passed, 6 skipped, 17 deselected in 144.39s` |
| `uv run ruff check .` | not reached (`&&` chain broke at pytest) | **0** — `All checks passed!` |
| `uv run mypy` | not reached | **0** — `Success: no issues found in 43 source files` |

Round 0's failed test → round 2's 6th skip: `tests/ui/test_identity.py::test_quit_via_tray_action_leaves_no_processes`
("no piper voice on this machine — the app spawns no worker to census"); the passing set is
byte-identical between rounds (556).

## Round 0 — clone at `864e462` (pre-fix)

- Temp dir `…\Temp\marshmellow-cleanroom-m6-1478` created empty; clone exit 0; HEAD
  `864e4629b483597ba67e672ef53f28b175401bb7` (identical to source `main`); `git status
  --porcelain` empty; **221 tracked files**.
- `uv sync` → **exit 0** — `Using CPython 3.14.0 interpreter at: C:\Python314\python.exe`,
  resolved 170 packages, built `marshmellow==0.1.0` from the clone itself, purely from the
  committed `pyproject.toml` + `uv.lock`.
- `uv run pytest -q` → **exit 1**: `1 failed, 556 passed, 5 skipped, 17 deselected in 179.26s`.
  The one failure: the quit census — captured log shows the composition root's documented
  degradation (`ERROR marshmellow.app:app.py:445 no piper voice 'en_US-lessac-medium' on this
  machine … TTS stays silent`) → no worker spawned → the census assertion failed
  (`tests/ui/test_identity.py:333` "the app spawned no piper worker to census — TTS build
  changed?"). Not a product regression: the same test passed in the dev tree both gate runs
  (voice present).
- ruff/mypy not reached (`&&` chain). Clone removed afterwards (verified absent).

## Fix round 1 (of ≤ 2 allowed)

Test-only change to `tests/ui/test_identity.py`: before building the app, the census mirrors the
composition root's own decision — `default_voice_model(Settings(activation="hotkey").tts_voice)`
— and `pytest.skip(...)` with a documented reason when no voice exists (same tier as
`tests/audio/test_activation.py:289` "bundled dev wake models … not present" and
`tests/stt/test_fasterwhisper.py:99` "SP2 fixtures not present"). No product code touched
(`git diff --stat` = 1 file, +14/−2). Dev-tree verification: full suite still
**559 passed, 3 skipped, 17 deselected, exit 0** (the census runs where a voice exists).

## Round 2 — clone at `864e462` + the fix diff applied

- Temp dir created empty; clone of `main` exit 0 (HEAD `864e462…`); the source tree's uncommitted
  diff exported (`git diff` → 1 file) and applied inside the clone (`git apply` exit 0;
  `git diff --stat` in the clone: `1 file changed, 14 insertions(+), 2 deletions(-)`).
- `uv sync` → **exit 0** (same interpreter/resolution as round 0).
- **`uv run pytest -q` → exit 0**: `556 passed, 6 skipped, 17 deselected in 144.39s` — the
  failed census is now the 6th skip; all other results identical to round 0.
- **`uv run ruff check .` → exit 0** (`All checks passed!`) · **`uv run mypy` → exit 0**
  (`Success: no issues found in 43 source files`).
- Tree pristine after all runs: `git status --porcelain` → exactly 1 entry (the applied,
  intentionally-uncommitted test fix); only ignored `.venv/`/`.pytest_cache/` added. Clone
  removed afterwards.

## Skip census (6, all by-design, dev-gated)

- `SKIPPED [3] tests\core\test_autostart.py:111` — non-win32 guard platform-inherent.
- `SKIPPED [1] tests\audio\test_activation.py:293` — CC BY-NC-SA dev wake models deliberately
  uncommitted (D8).
- `SKIPPED [1] tests\stt\test_fasterwhisper.py:103` — SP2 fixtures not present (dev-gated tier).
- `SKIPPED [1] tests\ui\test_identity.py` — the round-1 fix: no piper voice in a clean checkout
  (voices/ gitignored by design); full census strength wherever a voice exists.
- 17 `manual`-marked tests deselected by `pyproject.toml` addopts, by design.

## Verdict rationale

The ask's standard is PASS only if every gate exits 0 from the clean tree. Round 2: pytest 0,
ruff 0, mypy 0 — all three from the committed tree plus the one test-only fix that the Task 7
commit carries verbatim. Round 0's failure is understood, fixed within the bounded budget
(1 of 2 rounds), and the fix is itself cleanroom-verified. Verdict: **PASS**.
