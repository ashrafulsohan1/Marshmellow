# Marshmellow — v0.1.2 Pre-Publish Release Audit (verification round 2)

**Date:** 2026-11-14 · **Auditor:** fresh adversarial pass (read-only; the only file written is
this report, overwritten in place)
**Candidate:** HEAD `fda062c` — **unchanged since verification round 1** (`git log` + `git
rev-parse HEAD` re-run this round; `git diff fda062c..HEAD --stat` → **empty**: zero new
commits; `git status --porcelain` → only the two untracked plan reports).
**Round-1 report:** the previous version of this file found **0 OPEN high findings**. The only
HIGH-severity findings on record are the v0.1.0-era **S1 (shell/`open_app` gate bypass)** and
**S2 (tray kill-switch/mute reachability)** (`docs/plan/RELEASE-AUDIT-final.md` §2 origin,
round 0 and round 1 both verified FIXED). This round re-confirms **each of those two high
findings is fixed** at HEAD and hunts regressions — per the verification-round ask. Prior
baselines: [RELEASE-AUDIT-final.md](RELEASE-AUDIT-final.md), [RELEASE-AUDIT-m6.md](RELEASE-AUDIT-m6.md),
[CLEANROOM-VERIFY-v012.md](CLEANROOM-VERIFY-v012.md).
**Bounded fix rounds:** none used; no code, test, or doc changed this round except this report.

## Verdict

**PASS for the publish decision — both high findings (S1, S2) re-confirmed FIXED at HEAD
`fda062c`; the candidate is bit-for-bit the one verification round 1 passed; all gates green
again this round (second consecutive hang-free full suite since the shutdown-hygiene fix); no
regression possible and none found — the delta is empty.** Round-0 finding ledger stands
unchanged: 1 open medium (V1 — the settings-dialog probe egresses the stored API key to any
user-editable endpoint) and 5 open lows (V3–V7); V2 closed at `fda062c`.

| Severity | Count | Findings |
|---|---|---|
| **High** | 0 open | S1 and S2 re-verified FIXED at HEAD (§2) |
| **Medium** | 6 | V1 open (re-confirmed by read, §4) · S3, S4, S5, P1, A1 carry-over (unchanged) |
| **Low** | 12 | V2 closed · V3–V7 open (§4) · L1/L2/L3/L5/L6/L7/L8r carry-over + clean-checkout fixture note |

## 1. The candidate — stability check

- **HEAD pinned:** `git rev-parse HEAD` → `fda062c3798f3f8ca0bb07898cee1f200e93f6ea`; `git log
  --oneline -8` → identical history to round 1; `git diff fda062c..HEAD --stat` → empty. No
  fix round, no rebuild, no rebase happened between the two verification rounds.
- **Version sync:** `uv run python -I -c "import tomllib,pathlib; ...; import marshmellow;
  print(...)"` → `pyproject 0.1.2 __init__ 0.1.2`; `git show HEAD:uv.lock` self-version →
  `0.1.2`; `uv lock --check` → `Resolved 170 packages in 2ms`, exit 0.
- **Frozen bundle:** `./dist/Marshmellow/Marshmellow.exe --smoke` → **`smoke ok`, exit 0** (the
  bundle's product content matches HEAD — no `src/` change since its build; the round-1
  Analysis-TOC evidence still applies because HEAD is identical).

## 2. The two high findings — re-confirmed FIXED at HEAD `fda062c`

### S1 — shell/`open_app` gate bypass: fix holds

- Re-read at HEAD: `src/marshmellow/app.py:257-260` — `_gated_execute = self.gate.guard(...)`
  and `_gated_shell = self.gate.guard(self._run_shell)` (the audit comment names S1);
  `app.py:712-716` — `kind == "shell"` with a string `open_app` payload dispatches to
  `self._gated_shell(req)`; all else → `_gated_execute` (`app.py:717-719`); executor body
  `_run_shell` (`app.py:721-724`). Caller census: `open_app` reachable in product code only via
  the gated executor (pack + test harness).
- Gate chain unchanged: window-killer hotkeys → unconditional `"L3"` (`safety/policy.py:143-144`,
  before strict escalation `:146-147` and max-only overrides `:150-152`); kill probe precedes
  the decision; every decision+rationale logged (`safety/gate.py` `check`).
- **Tests run this round:** `test_shell_action_passes_through_the_gate`
  (`tests/test_release_fixes.py:47`) and `test_shell_action_refused_when_gate_declines` (`:66`)
  — passed (§3 item 2).

### S2 — tray kill-switch/mute reachability: fix holds

- Re-read at HEAD: `app.py:882-890` — tray `mute`/`unmute` → `audio_engine.set_muted(muted)`
  (same flag as the voice intents; Unmute recovers voice); `app.py:891-895` — tray `pause` →
  `manager.cancel_all()` **and** `self.tts.stop()` + spoken ack (S6 fix intact); single
  `TrayAction` subscriber (`app.py:243`); quit sets a polled flag (`app.py:875-876`).
- **Tests run this round:** `test_tray_mute_reaches_engine_and_unmute_recovers`
  (`tests/test_release_fixes.py:81`) and `test_tray_pause_cancels_running_tasks` (`:91`) —
  passed (§3 item 2).

## 3. Checks run during THIS round (exact commands and outputs)

1. `uv run pytest -q` (main tree, HEAD `fda062c`) → **`576 passed, 3 skipped, 17 deselected in
   413.41s`, exit 0** — identical pass/skip counts to rounds 0 and 1; summary reached, no
   shutdown hang (second consecutive hang-free full suite since `dbd18c0`'s sweep; the wall
   clock is inflated by concurrent targeted runs on this machine — the recorded number is what
   this round observed).
2. Targeted verification batch: `uv run pytest tests/test_release_fixes.py
   tests/test_shutdown_hygiene.py tests/core/test_privacy_audit.py tests/core/test_logging.py
   tests/ui/test_providers_catalog.py tests/ui/test_settings.py tests/test_smoke.py
   tests/safety/test_policy.py tests/llm/test_ollama.py -q` → **`93 passed in 93.39s`** — the
   S1/S2 regression tests, both shutdown-hygiene guards, the enforced privacy session + scrubber
   suite, catalog/dialog tests, version sync, window-killer policy, ollama tests.
3. `uv run ruff check .` → **`All checks passed!`, exit 0**; `uv run mypy` → **`Success: no
   issues found in 43 source files`, exit 0**; `uv lock --check` → exit 0.
4. Egress census re-run: `git grep -nE "import (httpx|openai|feedparser|requests|urllib)|from
   (httpx|openai|feedparser|requests)" -- src/` → exactly the round-0 set; `git grep -nE
   "shell=True|os\.system" -- src/` → zero hits.
5. Secret scan: `git grep -nIE "(sk-[A-Za-z0-9]{16,}|api_key\s*=\s*['"][A-Za-z0-9]{16,}|
   Bearer [A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-...)" -- .` → zero
   hits (history scan clean in rounds 0–1; no new blobs — no new commits).
6. Version/lock: runtime check → `pyproject 0.1.2 __init__ 0.1.2`; `git show HEAD:uv.lock` →
   `version = "0.1.2"`.
7. Frozen bundle: `./dist/Marshmellow/Marshmellow.exe --smoke` → **`smoke ok`, exit 0**.
8. S1/S2 spot-reads: `sed -n '257,260p;712,716p;891,895p' src/marshmellow/app.py` — the gated
   shell dispatch and the tray pause handler, byte-identical to rounds 0/1 (§2).
9. Round-0 finding spot-checks: `settings_dialog.py:226-233` probe still loads the stored key
   (V1 open); `docs/plan/RELEASE_NOTES.md:1` still "v0.1.1" and `CHANGELOG.md:8` still dated
   2026-11-09 (V3 open); `git grep -c api_key_ref -- src/marshmellow/core/config.py` → 1
   (definition only — L1 still dead).

## 4. Finding ledger — re-checked at HEAD (unchanged candidate)

Every round-0/1 citation transfers: HEAD is identical to the round-1 audited commit, so no
file changed since any citation was written. Statuses re-confirmed by the §3 spot-checks:

- **V1 (MEDIUM) — OPEN.** Probe sends `key_edit.text() or _load_key()` as Bearer to the
  editable base-URL combo (`ui/settings_dialog.py:226-242`, `ui/providers_catalog.py:150-153`,
  single key slot `core/secrets.py:13-14`). Recommend the round-0 mitigation before or promptly
  after publish (warn when the endpoint is outside the selected provider's catalog base_urls,
  or probe only with a session-typed key).
- **V2 (LOW) — CLOSED** (`fda062c`; re-verified §3 item 6).
- **V3–V7 (LOW) — OPEN** (§3 item 9): v0.1.2 notes missing / CHANGELOG date wrong; startup
  notice untested; probe-thread emit into a possibly-destroyed dialog (not runtime-reproduced);
  `_on_save` partial mutation + unguarded disk write escaping via the mascot-drag save path
  (`app.py:429-432`); egress-census statement needs the `ui/providers_catalog.py` amendment.
- **Carry-overs unchanged:** S3/S4/S5/P1/A1 (medium); L1 (dead `api_key_ref`, re-counted),
  L2, L3, L5 (63 MB history blob), L6 (internal docs tracked incl. `M6-FEEDBACK-PROMPT.md`),
  L7 (`.gitignore:16` dead entry), L8r (no `license`/`readme` in `pyproject.toml`); clean-
  checkout fixture-dependency note (3 fewer tests on a fresh clone; untracked spike data).

## 5. Regression hunt — result

**No regression possible and none found:** the delta since the round-1 audited HEAD is empty
(`git diff fda062c..HEAD --stat` → no output). The regression hunt therefore consisted of
re-executing every gate and scan against the unchanged candidate, all of which reproduced
round 1's results exactly (same pass/skip counts, same censuses, same smoke).

## Verification notes

- **Not run:** GUI/manual session with the settings dialog (user-physical); live-provider probe
  (only MockTransport-tested; V1 is a code-read finding); startup/memory bench (user-physical,
  M5 baseline stands); no fresh-clone run this round (cleanroom evidence cross-referenced; its
  two remediations were verified in the main tree in round 1 and HEAD is unchanged since).
- **Unverified:** that the intermittent shutdown hang cannot ever recur — three consecutive
  hang-free full-suite runs since the `dbd18c0` fix (round 0's pre-fix run 2/3 were clean too;
  the hang appeared once in the cleanroom) — the root cause is Windows loop-finalization
  timing, and the sweep's unit guards are the standing defense.
- Read-only pass: this report is the only file written; nothing committed; no network calls
  made by the audit itself.
