# Marshmellow — Pre-Publish Release Audit, Verification Round 2 (v0.1.0, HEAD `c6dc40d`)

**Date:** 2026-11-07 · **Auditor:** independent adversarial reviewer (read-only pass)
**Verified commit:** `c6dc40d90655ed6b0891dd3fe84e82efd0edf04c` (HEAD of `main`, clean tree).
**Prior rounds:** round 0 at `4797dca` (both pre-fix-audit HIGH findings S1/S2 verified fixed;
0 open highs), round 1 at `746d201` (both re-confirmed; no regression). This round: re-confirm
the two high findings at the new HEAD, re-run every check, and hunt for regressions in the
delta since round 1.
**Baseline docs:** `docs/plan/RELEASE-AUDIT.md` (pre-fix audit), `01-architecture.md` §3.13/§3.15,
`docs/plan/CLEANROOM-VERIFY-final.md` (updated this round to the independent re-verification —
read the committed diff in full).

## Verdict

**Both high findings remain fixed at `c6dc40d`; no regression found; 0 open high findings.**
The delta since round 1 is one docs-only commit (`c6dc40d`: the committed round-1 report —
verified a faithful condensed derivative of my round-1 text, verdict/severity-table/citations
intact — and a replaced `CLEANROOM-VERIFY-final.md` recording an independent fresh-clone
re-verification PASS at `746d201` with all three gates exit 0; the earlier FAIL report remains
in git history). **`src/` and `tests/` are byte-identical to round 1** (`git diff 746d201..HEAD
-- src/ tests/ installer/ pyproject.toml .gitignore assets/` → empty), so every round-0/1 code
citation remains valid and every carry-over finding is unchanged. All checks re-run green.

| Severity | Count | Findings |
|---|---|---|
| **High** | 0 open | S1, S2 verified FIXED (see §2) |
| **Medium** | 6, all still open | S3 spoken secrets in plaintext logs · S4 unconfirmed LLM-chosen monitor egress · S5 raw exception text as a secret channel · P1 automatic screenshot egress + crash-persistent temp artifacts · A1 model-authored rationale spoken/logged (incl. gated launch target) · S6 tray pause does not abort TTS |
| **Low** | 8 | L1 dead `api_key_ref` · L2 dev-mode STT hub fetch (product fixed) · L3 clipboard gate inert · L5 63 MB dead blob in history · L6 internal pipeline docs tracked · L7 dead `.gitignore` entry · L8r pyproject lacks `license`/`readme` · L9r frozen-bundle layout unverified (clean-checkout half resolved at `219d1c6`) |

---

## 1. Delta since round 1 (`746d201..c6dc40d`) — the regression surface

`git diff --stat 746d201..HEAD` → exactly 2 files, both docs:

- `docs/plan/RELEASE-AUDIT-final.md` (178 lines changed) — my round-1 report committed in
  condensed form. **Fidelity check (run here):** the committed 113-line version's Verdict,
  severity table, §1–§6 and Verification notes match my round-1 text's findings and file:line
  citations section-by-section; prose only condensed. No distortion found.
- `docs/plan/CLEANROOM-VERIFY-final.md` (88 changed) — replaced by an independent
  round-1 re-verification: fresh clone of `746d201` (201 tracked files, clone pristine after
  runs), `uv sync` exit 0, and all three gates exit 0 un-piped — `uv run pytest -q` →
  **`492 passed, 5 skipped, 17 deselected in 80.93s` (exit 0)**, `ruff check` exit 0,
  `mypy` exit 0 (43 source files). The previous round's FAIL record (wake extractor missing
  from clean checkouts at `4797dca`) is preserved in git history and referenced by the new
  document itself. Consistent with this round's own main-tree suite result (§6.1: 494 passed —
  the same suite +2 dev-venv-only tests).
- **No product code, test, installer-spec, dependency, or asset change** — verified by
  `git diff 746d201..HEAD -- src/ tests/ installer/ pyproject.toml .gitignore assets/` → empty.

## 2. The two high findings — re-confirmed FIXED at `c6dc40d`

### S1 — shell/`open_app` gate bypass: still fixed, fix holds

Fresh re-read at HEAD (code byte-identical to rounds 0/1):

- `src/marshmellow/app.py:242` — `self._gated_shell = self.gate.guard(self._run_shell)`, built next to `_gated_execute` (`app.py:239`).
- `app.py:640-644` — `kind == "shell"` + string `open_app` payload → `self._gated_shell(req)`; `create_task` documented pre-gate (`app.py:645-646`); all else → `_gated_execute` (`app.py:647`).
- Caller census re-run: only `pack.open_app` callers are the gated `_run_shell` (`app.py:651`) and the scenario-fixture test harness (`testing/fixtures.py:79`).
- Gate chain unchanged: kill probe before classification and after any confirmation (`gate.py:150-151, 122-128`), decision+rationale log (`gate.py:160-169`), L1 announce (`gate.py:129-130`), `shell: L1` (`policy.py:57`), per-turn kill + lease in front of the gate (`manager.py:673-709`), non-string payload refused by the ActionLayer (`executor.py:58-60, 147-152`).
- **Tests, run this round:** `test_shell_action_passes_through_the_gate`, `test_shell_action_refused_when_gate_declines` — PASSED (§6.2).

### S2 — tray kill-switch/mute reachability: still fixed, fix holds

Fresh re-read at HEAD:

- `app.py:810-818` — tray `mute`/`unmute` → `audio_engine.set_muted()` (same flag as the voice intents, `loop.py:227-230, 256-260`; capture drops chunks only while set, `capture.py:145-157`): the mute one-way trap stays closed; tray Unmute recovers voice.
- `app.py:819-822` — tray `pause` → `manager.cancel_all()` (kill flags set instantly, loops cancelled, rows de-persisted, `manager.py:618-645`); single subscriber `_on_tray_action` (`app.py:225`).
- **Tests, run this round:** `test_tray_mute_reaches_engine_and_unmute_recovers`, `test_tray_pause_cancels_running_tasks` — PASSED.
- **S6 deviation still open (medium):** tray pause still lacks `tts.stop()` (`app.py:819-822` vs `loop.py:239-245`) — §3.13's kill switch "aborts actions and TTS instantly"; mid-speech TTS keeps playing.

## 3. Regression hunt — delta review

- **`c6dc40d` touches no code.** The two doc changes were reviewed in full (committed report
  fidelity + cleanroom re-verification, §1). Neither weakens the gate, the scrubber, the
  secrets path, or the packaging; the cleanroom PASS closes the round-0 round's only
  reproducibility concern from a clean checkout.
- All round-0/1 medium/low findings carry over byte-identically (§4). **No regression found.**

## 4. Carry-over findings (unchanged — `src/` byte-identical since round 0; diff-verified each round)

- **S3 — MEDIUM:** spoken secrets in plaintext JSONL; `_SECRET_RE` misses natural speech (`logging.py:20-23`); verbatim instructions (`loop.py:207`, `app.py:306`) and confirmation answers (`confirm.py:67-73`); no rotation/purge (`logging.py:69-84`).
- **S4 — MEDIUM:** LLM-created monitors fetch LLM-chosen URLs unconfirmed, ≥30 s, restart-surviving (`app.py:654-728`, `monitors.py:45-57`, `store.py:58-78`).
- **S5 — MEDIUM:** raw exception text into observations/logs/dashboard (`executor.py:132-138`, `orchestrator.py:559-565, 617-622`, `manager.py:444-445, 591`, `main_window.py:268-271`).
- **P1 — MEDIUM:** automatic threshold-triggered vision egress (<10 elements → cloud PNG, `snapshot.py:35, 79-86`, `vision.py:263-286`) + crash-persistent `%TEMP%/marshmellow-shot-*.png` (`snapshot.py:133-140`; purge only at task end, `orchestrator.py:453-461`).
- **A1 — MEDIUM:** model-authored rationale spoken/logged verbatim, including the gated launch target (`gate.py:57-59, 129-130`, `app.py:602-604`; act tool lacks anti-restatement guidance, `orchestrator.py:214-243`). Real-LLM reproducibility unverified.
- **L1** dead `api_key_ref` (`config.py:30`; fixed slot `secrets.py:13-14`, `app.py:499-504`) · **L2** dev-mode STT hub fetch (`app.py:362-382` frozen-fixed, `fasterwhisper.py:43-48`; hub behavior unverified) · **L3** clipboard gate inert (no reader; `prompt_builder.py:70-75` correct) · **L5** 63,201,294-byte sp4 voice blob still reachable in history (re-verified largest blob; 61.58 MiB loose) · **L6** internal pipeline docs tracked (`WORKFLOW-RULES.md:3`, build-ledger, pending-user-verifications, spikes, `.zcode/skills`) · **L7** dead `.gitignore:16` entry · **L8r** `pyproject.toml:1-13` lacks `license`/`readme` fields (MIT LICENSE file present) · **L9r** frozen-bundle on-disk layout unverified (spec read only; no PyInstaller build run in any round).

## 5. Standing verified-clean statements (re-checked this round where re-runnable)

- **Secrets:** key hidden on all entry surfaces, Credential Manager only, scrubber-registered on store+load, never in TOML (`config.py:53-70`), never in subprocess args (piper argv worker+model path, `piper.py:69-88`, text via stdin `:255-261`; fixed `reg.exe` argv, `autostart.py:65-105`).
- **Egress census re-run (unchanged):** only `httpx`/`openai` in `llm/{openai_compat,ollama,resilience}` and `httpx`/`feedparser` in `tasks/monitors.py`; no telemetry/crash/update code; no `shell=True`/`os.system` in `src/`.
- **Privacy:** no audio on disk; wake `ChunkQueue` memory-only; `tasks.json` atomic writes; the machine-enforced `test_session_log_is_clean` passes every run.

## 6. Checks run during THIS round (exact commands and outputs)

1. `python -m pytest tests/ -q` (repo venv, HEAD `c6dc40d`) → **`494 passed, 3 skipped, 17 deselected in 128.05s`** — full suite green (identical counts to rounds 0/1 + the round-1 guard test; consistent with the cleanroom's fresh-clone 492 passed + 2 dev-gated skips).
2. `python -m pytest tests/test_release_fixes.py tests/core/test_privacy_audit.py tests/core/test_logging.py tests/audio/test_activation.py::test_feature_extractors_are_committed -v` → **21 passed** — S1/S2 regression tests, the enforced privacy session, all 12 scrubber rows, the extractor guard.
3. Secret scans: `git grep -nIE "(sk-…|api_key…|Bearer…|password…|AKIA…|ghp_…|xox[baprs]-)" -- .` → only `tests/llm/test_openai_compat.py:54` (fake key) and a commit-message quote in a plan doc; same patterns + `BEGIN (RSA|OPENSSH) PRIVATE KEY` over **every blob in history** → **zero hits**.
4. `git rev-list --objects --all | git cat-file --batch-check` + `git count-objects -vH` → largest blob still 63,201,294 B (sp4 voice, history-only); then the three deliberate wake ONNX assets (1,326,578 / 1,087,958 / 869,652 B); 61.58 MiB loose.
5. `git check-ignore -v` spot checks → `voices/`, `models/`, `dist/`, `build/`, `.venv/`, `config.local.toml`, `logs/` ignored; the three wake ONNX files tracked (check-ignore exit 1 = none ignored — correct).
6. `git diff 746d201..HEAD -- src/ tests/ installer/ pyproject.toml .gitignore assets/` → **empty** — no code/asset change since round 1 (round-0/1 line citations remain valid).
7. Greps: network imports (unchanged census); `TODO|FIXME|XXX|HACK` over `src/`, `tools/`, `installer/` → clean; `open_app` caller census → only the gated executor + test harness.
8. Docs: LICENSE first line "MIT License"; `README.md`, `CHANGELOG.md`, `docs/user-guide.md`, `docs/privacy.md` all present.

## Verification notes

- **Unverified (unchanged):** faster-whisper's HuggingFace fetch in dev mode (library behavior); `os.startfile` per-scheme ShellExecute semantics; the frozen bundle's on-disk layout (spec read, bundle not built in any round); A1's real-LLM reproducibility; the cleanroom document's fresh-clone runs remain the release pipeline's own (my independent evidence at this HEAD: checks 1–8; the code-free delta makes its `746d201` PASS transferable in substance to `c6dc40d`).
- Read-only pass: this report is the only file written (overwritten in place per the ask); nothing committed; no network calls made.
