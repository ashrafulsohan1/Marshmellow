# Marshmellow — Pre-Publish Release Audit, Verification Round 1 (v0.1.0, HEAD `746d201`)

**Date:** 2026-11-07 · **Auditor:** independent adversarial reviewer (read-only pass)
**Verified commit:** `746d2016fe41d7cd2a1331d6df9b83a7c4d37219` (HEAD of `main`, clean tree).
**Round-0 baseline:** this same file at audited HEAD `4797dca` (now committed, condensed, in
`746d201`). The round-0 report carried the two pre-fix-audit high findings (S1 shell gate
bypass, S2 dead kill-switch/recovery channel) as high-severity verification items and found
**0 open high findings**. This round: re-confirm both high items are fixed at the new HEAD,
re-run every check, and hunt for regressions in what changed since round 0.
**Baseline docs:** `docs/plan/RELEASE-AUDIT.md` (pre-fix audit), `01-architecture.md` §3.13/§3.15,
`docs/plan/CLEANROOM-VERIFY-final.md` (new since round 0 — read in full this round).

## Verdict

**Both high findings remain fixed at `746d201`; no regression found in the two new commits;
0 open high findings.** The delta since round 0 is two commits, one of which touches code
(only a test file), plus two committed Apache-2.0 ONNX feature extractors whose identity,
headers, and license record were verified. All round-0 medium/low findings carry over
unchanged (`src/` is byte-identical — proven by diff below); one round-0 low (L9, clean-checkout
wake assets) is resolved by the new commit, and the round-0 spec-doc-drift nit is fixed.

| Severity | Count | Findings |
|---|---|---|
| **High** | 0 open | S1, S2 verified FIXED (see §2) |
| **Medium** | 6, all still open from round 0 | S3 spoken secrets in plaintext logs · S4 unconfirmed LLM-chosen monitor egress · S5 raw exception text as a secret channel · P1 automatic screenshot egress + crash-persistent temp artifacts · A1 model-authored rationale spoken/logged (incl. gated launch target) · S6 tray pause does not abort TTS |
| **Low** | 8 | L1 dead `api_key_ref` · L2 dev-mode STT hub fetch (product fixed) · L3 clipboard gate inert · L5 63 MB dead blob in history · L6 internal pipeline docs tracked · L7 dead `.gitignore` entry · L8r pyproject lacks `license`/`readme` · L9r frozen-bundle layout still unverified (clean-checkout half resolved by `219d1c6`) |

---

## 1. What changed since round 0 (`4797dca..746d201`) — the regression surface

`git diff --stat 4797dca..HEAD` → 10 files, 256 insertions, 8 deletions:

- **`src/`: ZERO changes.** The only non-doc code change is `tests/audio/test_activation.py`
  (+15 lines: one new guard test, reviewed in full — a pure existence assert for the two
  extractor files, no new behavior, no new I/O). Every `src/marshmellow/` file cited in
  round 0 is byte-identical at this HEAD.
- `219d1c6` — commits `assets/wake/melspectrogram.onnx` (1,087,958 B) and
  `assets/wake/embedding_model.onnx` (1,326,578 B) with `.gitignore` negations, updates
  `assets/wake/README.md` and the `installer/marshmellow.spec` comment, adds the guard test.
  Root cause per `docs/plan/CLEANROOM-VERIFY-final.md`: the openwakeword 0.6.0 wheel ships no
  `resources/` and nothing fetches the extractors, so a clean checkout could not load the wake
  scorer (cleanroom FAIL at `4797dca`, repro'd 3×; re-verified PASS from a fresh clone of
  `219d1c6` — `uv run pytest -q` exit 0, 492 passed).
- `746d201` — docs only: the condensed round-0 audit report (compared against the round-0 text:
  verdict, findings, and file:line citations intact; prose condensed), the new cleanroom-final
  record, CHANGELOG and architecture-changelog entries.

**New-binary integrity check (run here):** `git hash-object assets/wake/{melspectrogram,embedding_model}.onnx`
→ `a3a60354…` / `afde53ef…` — identical to the committed blobs (`git ls-tree HEAD assets/wake/`),
i.e. the working-tree files audited in round 0 are exactly what is now tracked. Headers
(`head -c 8 | od -An -tx1`): `08 07 12 07 70 79 74 6f` ("pyto…") and `08 07 12 07 74 66 32 6f`
("tf2o…") — legitimate ONNX protobuf producer metadata (pytorch / tf2onnx exports), not
executables or archives. License: Apache-2.0, recorded check at `assets/wake/README.md:36-39`.

## 2. The two high findings — re-confirmed FIXED at `746d201`

### S1 — shell/`open_app` gate bypass: still fixed, fix holds

Re-read at HEAD (src unchanged, verified by diff and by fresh read):

- `src/marshmellow/app.py:242` — `self._gated_shell = self.gate.guard(self._run_shell)` built at composition next to `_gated_execute` (`app.py:239`).
- `app.py:640-644` — `kind == "shell"` with a string `open_app` payload routes to `self._gated_shell(req)`; `create_task` stays documented pre-gate (`app.py:645-646`); everything else → `_gated_execute` (`app.py:647`); `_run_shell` (`app.py:649-652`) is the gated DE-pack executor.
- Caller census re-run: the only `open_app` callers are `_run_shell` (behind the gate) and the scenario-fixture test harness (`src/marshmellow/testing/fixtures.py:79`).
- Gate behavior unchanged (`src/marshmellow/safety/gate.py`): kill probe before classification (`:150-151`), decision+rationale log (`:160-169`), L1 announce (`:129-130`), fail-closed refusals; `shell: L1` in the kind table (`policy.py:57`); per-turn kill check + foreground lease still in front of the gate for every producer (`manager.py:673-709`); a non-string payload falls to the gate and is refused by the ActionLayer's non-element-kind check (`executor.py:58-60, 147-152`).
- **Tests, run this round:** `tests/test_release_fixes.py::test_shell_action_passes_through_the_gate` and `::test_shell_action_refused_when_gate_declines` — PASSED (see §6).

### S2 — tray kill-switch/mute reachability: still fixed, fix holds

Re-read at HEAD:

- `app.py:810-818` — tray `mute`/`unmute` → `audio_engine.set_muted()` (the same flag the voice intents set, `loop.py:227-230, 256-260`; capture drops chunks only while it is set, `capture.py:145-157`) + mascot state + log. The mute one-way trap stays closed: tray Unmute recovers voice after a voice mute.
- `app.py:819-822` — tray `pause` → `manager.cancel_all()` (kill flags set instantly, loops cancelled, rows de-persisted, `manager.py:618-645`); the only subscriber remains `_on_tray_action` (subscribed `app.py:225`).
- **Tests, run this round:** `test_tray_mute_reaches_engine_and_unmute_recovers`, `test_tray_pause_cancels_running_tasks` — PASSED.
- **S6 deviation still open (medium):** tray pause still does not call `tts.stop()` (`app.py:819-822` vs the voice kill switch's `loop.py:239-245`) — §3.13 says the kill switch "aborts actions and TTS instantly"; mid-speech TTS keeps playing. Unchanged since round 0.

## 3. Regression hunt — new commits

- **`219d1c6` (extractors):** no product code touched; binary identity/headers/license verified (§1); the `.gitignore` negations (`!assets/wake/melspectrogram.onnx`, `!assets/wake/embedding_model.onnx`) confirmed effective via `git check-ignore -v` (the three wake ONNX files are NOT ignored; everything else correctly ignored — voices/ now at `.gitignore:48`, models/ `:55` after the insertion; the dead `%LOCALAPPDATA%/` entry is still line 16). The round-0 spec-comment drift nit is FIXED: `installer/marshmellow.spec:69-72` now correctly describes `collect_data_files("openwakeword", subdir="resources")` as a belt-and-braces no-op on the bare wheel, with the explicit-path copies in `_internal/wake` (matching `activation.py:401-409`). The new guard test passes.
- **`746d201` (docs):** changelog entries accurately describe the fix and the cleanroom FAIL→PASS; no code, no secrets (history scan re-run, §6.3). Note: the committed `RELEASE-AUDIT-final.md` is a **condensed derivative** of the round-0 report — verdict, all findings, and citations intact (compared section-by-section); this file is now overwritten in the worktree with the round-1 report per the ask and not committed.
- **`docs/plan/CLEANROOM-VERIFY-final.md`** (read in full): documents a real clean-checkout FAIL at round-0's HEAD that round 0's main-tree suite could not see (dev-venv resources by accident of history) — an honest, properly root-caused record; its re-verification numbers at `219d1c6` (fresh clone: 492 passed, ruff 0, mypy 0; main tree: 494 passed) are consistent with this round's own suite run (§6.1). The two extra clean-checkout skips it lists (dev-gated CC-BY-NC wake models, SP2 fixtures) are by-design.
- **No regression found.** The medium/low findings from round 0 all carry over byte-identically; nothing in the delta weakens the gate, the scrubber, or the secrets path.

## 4. Carry-over findings (unchanged — `src/` identical, citations from round 0 verified still present)

- **S3 — MEDIUM:** spoken secrets in plaintext JSONL logs; `_SECRET_RE` misses natural speech (`logging.py:20-23`); verbatim instruction logging (`loop.py:207`, `app.py:306`), confirmation answers (`confirm.py:67-73`); no rotation/purge (`logging.py:69-84`).
- **S4 — MEDIUM:** LLM-created monitors fetch LLM-chosen URLs unconfirmed, ≥30 s, restart-surviving (`app.py:654-728`, `monitors.py:45-57`, `store.py:58-78`).
- **S5 — MEDIUM:** raw exception text into observations/logs/dashboard (`executor.py:132-138`, `orchestrator.py:559-565, 617-622`, `manager.py:444-445, 591`, `main_window.py:268-271`).
- **P1 — MEDIUM:** automatic threshold-triggered vision egress (<10 elements → cloud PNG, `snapshot.py:35, 79-86`, `vision.py:263-286`) + crash-persistent `%TEMP%/marshmellow-shot-*.png` (`snapshot.py:133-140`; purge only at task end, `orchestrator.py:453-461`).
- **A1 — MEDIUM:** model-authored rationale spoken/logged verbatim, including the gated launch target — a credentialed URL is spoken even though the log line is scrubbed (`gate.py:57-59, 129-130`, `app.py:602-604`; act tool lacks anti-restatement guidance, `orchestrator.py:214-243`). Real-LLM reproducibility unverified.
- **L1** dead `api_key_ref` (`config.py:30`; fixed slot `secrets.py:13-14`, `app.py:499-504`) · **L2** dev-mode STT hub fetch (`app.py:362-382` frozen-fixed, `fasterwhisper.py:43-48`; hub behavior unverified) · **L3** clipboard gate inert (no reader; `prompt_builder.py:70-75` correct) · **L5** 63,201,294-byte sp4 voice blob still reachable in history (re-verified: largest blob; `git count-objects`: 61.57 MiB loose, +1.8 MiB from the extractors) · **L6** internal pipeline docs tracked (`WORKFLOW-RULES.md:3` session id, build-ledger, pending-user-verifications, spikes, `.zcode/skills`) · **L7** dead `.gitignore:16` entry · **L8r** `pyproject.toml:1-13` lacks `license`/`readme` fields (MIT LICENSE file present) · **L9r** frozen-bundle on-disk layout still unverified (spec read only; no PyInstaller build run this round either).

## 5. Standing verified-clean statements (re-checked this round where re-runnable)

- **Secrets:** key hidden on all entry surfaces, Credential Manager only, scrubber-registered on store+load, never in TOML (`config.py:53-70` has no key field), never in subprocess args (piper argv = worker exe + model path `piper.py:69-88`, text via stdin `:255-261`; autostart fixed `reg.exe` argv `autostart.py:65-105`). Network/subprocess import census re-run: unchanged (`httpx`/`openai`/`feedparser` only in `llm/*` + `tasks/monitors.py`; no `shell=True`/`os.system` anywhere).
- **Egress inventory:** unchanged — planner LLM calls, vision LLM calls (P1), monitor GETs (S4), build/dev-time fetchers only.
- **Privacy:** no audio on disk; wake `ChunkQueue` memory-only; `tasks.json` atomic writes; `docs/privacy.md` accurate; the machine-enforced `test_session_log_is_clean` passes every run.

## 6. Checks run during THIS round (exact commands and outputs)

1. `python -m pytest tests/ -q` (repo venv, HEAD `746d201`) → **`494 passed, 3 skipped, 17 deselected in 130.88s`** — full suite green (one more test than round 0's 493: the new extractor guard test; matches the cleanroom record).
2. `python -m pytest tests/test_release_fixes.py tests/core/test_privacy_audit.py tests/core/test_logging.py tests/audio/test_activation.py::test_feature_extractors_are_committed -v` → **21 passed** — S1/S2 regression tests, the enforced privacy session, all 12 scrubber rows, the extractor guard.
3. Secret scans: `git grep -nIE "(sk-…|api_key…|Bearer…|password…|AKIA…|ghp_…|xox[baprs]-)" -- .` → only `tests/llm/test_openai_compat.py:54` (fake key `test-key-123`) and a commit-message quote in a plan doc; same patterns + `BEGIN (RSA|OPENSSH) PRIVATE KEY` over **every blob in history** → **zero hits**.
4. `git rev-list --objects --all | git cat-file --batch-check` → largest blob still 63,201,294 B (sp4 voice, history-only); then the two wake extractors (1,326,578 / 1,087,958 B) and `hey-marshmellow.onnx` (869,652 B) — all deliberate product assets; `git count-objects -vH` → 61.57 MiB loose.
5. `git hash-object` + `git ls-tree HEAD assets/wake/` + 8-byte headers → committed extractor blobs byte-identical to the audited working-tree files; legitimate ONNX protobufs.
6. `git check-ignore -v` spot checks (incl. the new negations) → `voices/`, `models/`, `dist/`, `build/`, `.venv/`, `config.local.toml`, `logs/` ignored; the three wake ONNX files tracked (not ignored).
7. `git diff --stat 4797dca..HEAD -- src/ tests/` → only `tests/audio/test_activation.py` +15 — **no product-code change since round 0** (cited round-0 line numbers remain valid).
8. Greps: network imports; `subprocess|os.startfile|shell=True` census (same four product sites as round 0); `TODO|FIXME|XXX|HACK` over `src/`, `tools/`, `installer/`, `docs/plan/` → clean.

## Verification notes

- **Unverified (unchanged from round 0):** faster-whisper's HuggingFace fetch in dev mode (library behavior); `os.startfile` per-scheme ShellExecute semantics; the frozen bundle's on-disk layout (spec read, bundle not built in either round); A1's real-LLM reproducibility; the cleanroom doc's own claims beyond what this round independently reproduced (its fresh-clone runs were the release fixer's, not mine — my independent evidence at this HEAD is check 1 above plus the byte-identical-diff proof).
- Read-only pass: this report is the only file written (overwritten in place per the ask); nothing committed; no network calls made.
