---
name: release-checklist
description: The repeatable M5 release gate for Marshmellow — build the installer bundle, headless smoke, startup/memory benches, privacy audit, and the dogfood day-report. Run this before publishing any release artifact.
---

# Release checklist (M5 Task 7)

The gate a release build must pass, in order. Every step is agent-runnable
except the dogfood day itself (user-physical, see
`docs/plan/pending-user-verifications.md`). Evidence goes into the milestone's
DoD block in `docs/plan/02-milestones.md` — a gate not recorded did not run.

## 1. Gates first (the tree must be clean and green)

```powershell
uv run pytest -q        # manual tier deselected
uv run ruff check .
uv run mypy
```

## 2. Fetch bundle inputs + build (OQ-9: pyinstaller one-dir, two exes)

```powershell
uv run python installer/fetch_bundle_inputs.py
uv run pyinstaller installer/marshmellow.spec --noconfirm
```

The spec hard-fails if `voices/<voice>.onnx(.json)` or
`models/faster-whisper-base/model.bin` is missing — binaries stay out of git
on purpose. Output: `dist/Marshmellow/` with `Marshmellow.exe` (GUI) and
`piper_worker.exe` (the SP7 GPL-isolated subprocess, sibling in the dist
root).

## 3. Headless smoke (the install verification)

```powershell
dist\Marshmellow\Marshmellow.exe --smoke
# expected: prints "smoke ok", exit 0 — no Qt windows, no mic, no wizard
```

Note: `--smoke` runs headless with FakeSTT/FakeTTS — it proves the bundle
imports, not the packaged-model paths. The bench (step 4) is the first REAL
GUI boot (bundled ct2 STT, wake watcher, mic) — that is where missing
package-data (silero VAD onnx, openwakeword feature models, piper espeak data)
shows up, as a traceback or a silent stall after "TTS voice:" in the newest
`%LOCALAPPDATA%/Marshmellow/logs/session-*.jsonl`.

## 4. Benches (real clock, targets recorded, never silently tuned)

```powershell
uv run python tools/bench_startup_m5.py --exe dist/Marshmellow/Marshmellow.exe --runs 3
```

Targets: cold-start median ≤ 10 s, idle RSS (app + piper worker sum) ≤ 400 MB.
A miss is recorded in the DoD block WITH the tuning levers (lazy STT load,
deferred piper spawn) and a measured decision.

## 5. Privacy audit (§3.15 — unchanged since M5 Task 6, but re-run on the tree)

Confirm the log-scrubbing suite still passes (`tests/core/test_logging.py`,
`tests/core/test_privacy_audit.py`) and that no new log call site leaks raw
secrets: secrets ride the registered-secret / pattern scrub in
`core/logging.py`, never raw `logger.info` of key material.

## 6. Dogfood day (user-physical) + report

The user runs the bundle as the primary interface for a full workday
(wake-word-led), then:

```powershell
uv run python tools/dogfood_report.py --date YYYY-MM-DD
```

DoD: crash count 0 (exit 0); friction list recorded in the M5 evidence block.
During the same day: note any unprompted wake (false-accept feel — the
subjective complement to `tools/soak_wake_m5.py`'s 1 h ambient soak).

## 7. Record

Append the DoD evidence block to `docs/plan/02-milestones.md` (gate outputs,
bench table, soak verdicts, installer smoke), update
`docs/plan/pending-user-verifications.md` (physical checklist outcomes), and
record the task in `docs/plan/build-ledger.jsonl`.
