# Marshmellow — Final Pre-Publish Release Audit (v0.1.0, HEAD `4797dca`)

**Date:** 2026-11-07 · **Auditor:** independent adversarial reviewer (read-only pass)
**Scope:** the release candidate at HEAD on `main`, clean working tree, before publish to the
private repo `0xSpecters/Marshmellow`. Audited: secrets handling (BYOK key and every leak
surface), injection surfaces (LLM-driven shell/URI launches, UIA payloads), network egress
inventory, privacy (mic data, screenshots, transcripts), safety-gate integrity (including
verification that the pre-fix audit's S1/S2 fixes hold and a regression hunt through the
newest code), and release hygiene. Full test suite **was run** (the pre-fix audit's was not).
**Method:** every finding cites `file:line` from code actually read in this checkout at HEAD,
or an exact command run here. Claims that could not be verified are marked **unverified**.
Baseline: `docs/plan/RELEASE-AUDIT.md` (the pre-fix audit) and `docs/plan/01-architecture.md`
§3.13 (Safety/Consent) and §3.15 (EventBus & logging).

## Verdict

**No high-severity findings remain.** Both high findings of the pre-fix audit (S1 shell gate
bypass, S2 dead kill-switch/recovery channel) are genuinely fixed, carry regression tests that
pass in this run, and no regression the newest code introduced re-opens them. Five
medium findings of the pre-fix audit are still open (unchanged), plus one new medium finding
(tray pause does not stop TTS) and small hygiene items. Nothing found blocks a private-repo
publish on safety or secrecy grounds; the open mediums should be disclosed (they already are,
in part, in `docs/plan/RELEASE_NOTES.md` §Known limitations and `docs/privacy.md`) or fixed
before a public release.

| Severity | Count | Findings |
|---|---|---|
| **High** | 0 | — |
| **Medium** | 6 | S3 spoken secrets in plaintext logs · S4 unconfirmed LLM-chosen monitor egress · S5 raw exception text as a secret channel · P1 automatic screenshot egress + crash-persistent temp artifacts · A1 model-authored rationale spoken/logged (incl. the gated launch target) · **NEW S6** tray pause does not abort TTS (kill-switch spec deviation) |
| **Low** | 8 | L1 dead `api_key_ref` · L2 (residual) dev-mode STT hub fetch · L3 clipboard privacy gate still inert · L5 63 MB dead blob in published history · L6 internal pipeline docs in the repo · L7 dead `.gitignore` entry · L8 (residual) pyproject lacks `license`/`readme` metadata · L9 `WakeWordWatcher` init reads calibration relative to model dir (frozen-bundle assumption unverified) |

---

## 1. Pre-fix audit high findings — verified FIXED

### S1 — shell/`open_app` gate bypass: FIXED, fix holds

- The gated wrapper is built once at composition: `self._gated_execute = self.gate.guard(self._run_action_layer)` **and** `self._gated_shell = self.gate.guard(self._run_shell)` (`src/marshmellow/app.py:239-242`, comment cites the audit).
- Routing: `App._execute` sends `kind == "shell"` with a string `open_app` payload to `self._gated_shell(req)` (`app.py:640-644`) — the pre-fix direct `self.pack.open_app(...)` return is gone. `create_task` remains deliberately pre-gate (`app.py:645-646`, documented at `app.py:654-664`), everything else falls through to `self._gated_execute` (`app.py:647`).
- What the gate now does for a launch: kill probe checked in `_decide` before classification (`src/marshmellow/safety/gate.py:150-151`), the §3.13 decision+rationale log (`gate.py:160-169`), and the L1 announce — TTS ack-priority speech of the rationale before execution (`gate.py:129-130` → `app.py:602-604`), matching `shell: L1` in the kind table (`src/marshmellow/safety/policy.py:57`) which was dead code pre-fix.
- `_run_shell` (`app.py:649-652`) is the only product caller of `pack.open_app`; grep-verified. The other caller is the scenario-fixture harness (`src/marshmellow/testing/fixtures.py:79`), which launches its own scripted app in tests, not an action executor.
- No execution-side fallback: a `shell` request whose `open_app` payload is not a string falls to `_gated_execute` → gate (L1, announced) → `ActionLayer` refuses it as a non-element kind (`src/marshmellow/actions/executor.py:58-60, 147-152`). There is no ungated path that reaches `WindowsPack.open_app` or `os.startfile`.
- TaskManager layer in front of the gate is unchanged and applies to shell: the per-turn executor checks the kill flag before every action (`src/marshmellow/tasks/manager.py:671-679`) and background modes need a granted foreground lease for non-read kinds (`manager.py:680-709`; `shell` ∉ `READ_KINDS`).
- **Regression test, run here, passes:** `tests/test_release_fixes.py::test_shell_action_passes_through_the_gate` (asserts `gate._decide` sees the shell request) and `::test_shell_action_refused_when_gate_declines` (asserts the pack is never called on refusal) — `tests/test_release_fixes.py:47-78`. Command: `python -m pytest tests/test_release_fixes.py tests/core/test_privacy_audit.py tests/core/logging.py -v` → 20 passed.
- Residual (by design, not a bypass): the launch surface is still broad — any string containing `://` or a `shell:`/`ms-settings` prefix goes to `os.startfile` and any `*.exe` on PATH goes to `Popen` (`src/marshmellow/packs/windows.py:117-121, 123-134, 137-140`), and the LLM tool description still invites URLs (`src/marshmellow/planner/orchestrator.py:138-148`). §3.13 classifies L1 navigate/open as "autonomous + announced", so this is the documented policy — an LLM-chosen URI's registered protocol handler still launches after only a spoken announcement. Flagged as a product decision to revisit (see A1: the announce *speaks the target*).

### S2 — tray kill-switch/mute reachability: FIXED, fix holds

- `_on_tray_action` (`app.py:802-823`) now handles all five tray kinds:
  - `mute`/`unmute` → `self.audio_engine.set_muted(muted)` + mascot `muted`/`idle` + a log line (`app.py:810-818`). This is the **same flag** the voice `mute` intent sets (`src/marshmellow/audio/loop.py:227-230, 256-260` → `capture.py:142-143`), and the capture thread drops chunks only while it is set (`src/marshmellow/audio/capture.py:145-157`). The pre-fix one-way trap is therefore closed: after a voice "mute", the tray **Unmute** restores mic capture, VAD, wake and voice control — exactly the recovery channel the pre-fix audit demanded.
  - `pause` → `self.manager.cancel_all()` + spoken confirmation (`app.py:819-822`). `cancel_all` kills every running turn via the kill flags (visible to worker threads instantly), cancels long-lived monitor/scheduled loops on the resident loop, and eagerly drops their persisted rows (`manager.py:618-645`) — the tray half of the §3.13 kill switch now exists.
- The only subscriber is still `App._on_tray_action` (subscribed at `app.py:225`; kinds defined at `src/marshmellow/ui/tray.py:17-26`) — no double-handling.
- **Regression test, run here, passes:** `tests/test_release_fixes.py:81-97` (`test_tray_mute_reaches_engine_and_unmute_recovers`, `test_tray_pause_cancels_running_tasks`).
- **One deviation remains — new finding S6 below:** tray pause aborts actions but not TTS; §3.13 says the kill switch "aborts actions and TTS instantly" and the voice `kill_all` path does stop TTS (`loop.py:239-245`), the tray path does not.
- Minor (cosmetic, not a finding): tray unmute drops the mascot to `idle` (`app.py:817`) while voice unmute sets `listening` (`loop.py:260`).

---

## 2. Secrets handling — verified clean (for the record)

- **Key entry:** hidden input on every surface — `getpass` in the CLI (`src/marshmellow/tools/set_key.py:15-21`), `EchoMode.Password` + `clear()` after store in settings (`src/marshmellow/ui/settings_dialog.py:110-114, 151-159`) and onboarding (`src/marshmellow/ui/onboarding.py:172-175, 229-243`). Failure paths log only "storing the API key failed" (`settings_dialog.py:156-158`, `onboarding.py:237-239`) — and the key is registered with the scrubber *before* the store attempt (`src/marshmellow/core/secrets.py:24`), so even a key-bearing traceback is redacted on disk.
- **Storage:** Windows Credential Manager, fixed slot `Marshmellow`/`llm_api_key` (`secrets.py:13-14, 23-42`); registered with the log scrubber on both store and load (`secrets.py:24, 41`). Provider receives it via a getter into the SDK client only (`app.py:499-504`, `src/marshmellow/llm/openai_compat.py:41-45`).
- **Never in TOML:** `Settings` carries no key field and `save()` dumps only the non-secret pydantic model (`src/marshmellow/core/config.py:53-70`).
- **Never in subprocess args:** piper argv is the worker exe + model path only (`src/marshmellow/tts/piper.py:69-88`); spoken text goes over stdin (`piper.py:255-261`); autostart argv is a quoted interpreter + `-m marshmellow` (`src/marshmellow/core/autostart.py:92-105, 65-89` — fixed `reg.exe` argv, no shell).
- **Scrubber:** post-format redaction of `password|passwd|secret|api_key|apikey|token[=:]` shapes (`src/marshmellow/core/logging.py:20-23`), URL query credentials (`logging.py:33-35`, the M5-T6 review fix that stops before a JSON `\"` escape pair), and exact-string registered secrets — all applied to the fully formatted line *including* exception text (`logging.py:48-66`).
- **Committed secrets: none.** Ran `git grep -nIE "(sk-[A-Za-z0-9]{8,}|api_key\s*[=:]\s*['\"][A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._-]{10,}|password\s*[=:]\s*['\"][^'\"]{4,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-)" -- .` → only a fake test key assertion (`tests/llm/test_openai_compat.py:54`) and a commit-message quote in a plan doc. The same key patterns plus `BEGIN (RSA|OPENSSH) PRIVATE KEY` scanned over **every blob in history** (`git rev-list --objects --all | git cat-file --batch-check … grep`) → zero hits.

## 3. Injection surfaces — verified bounded

- **No `shell=True`, no `os.system`, no string-interpolated command lines anywhere in `src/`** (grep: `subprocess|startfile|Popen|os\.system|shell=True` → only the four known sites: `packs/windows.py` fixed-argv `Popen` + `os.startfile`, `tts/piper.py` worker spawn, `core/autostart.py` fixed `reg.exe` argv, `testing/fixtures.py:113` `taskkill` in the test harness). The S1 gap was authorization, and it is closed; the launch payloads remain LLM-chosen strings — that is now an announced, logged L1 decision (see A1 for the speech surface).
- **UIA payloads:** element values are suppressed from the planner listing for password fields (`src/marshmellow/planner/tools.py:85-95` — the value exists on the element, never rendered); `type_text`/`uia_set_value` payloads come only from the model's `act` call and are classified L2 (L3 in strict, L3 always on password/sensitive-name targets — `policy.py:108-127`, escalation via the gate's ref resolver `gate.py:91, 146-148`); elevated windows are refused, never force-acted (`executor.py:186-196`). Vision model output is parsed defensively into bounded Elements (`vision.py:159-199` — tile numbers range-checked, junk dropped).
- **Monitor targets:** validated only for shape (`app.py:674-691`) — see S4.

## 4. Network egress inventory (what actually leaves the machine)

Grep-verified: the only network-capable imports in `src/marshmellow/` are `httpx`+`openai` in `llm/openai_compat.py:16-17` and `llm/resilience.py:18-19`, `httpx` in `llm/ollama.py:18`, and `httpx`+`feedparser` in `tasks/monitors.py:24-25`. No telemetry, crash reporting, or update check exists anywhere.

1. **Planner LLM calls** → the configured `base_url`: instruction text, active window app+title (`orchestrator.py:739-754`), element listings with non-password values (`tools.py:77-95`), every tool result/observation (`orchestrator.py:726-737`), the system prompt.
2. **Vision LLM calls**: marked PNG of the foreground window as base64 (`vision.py:263-286`) — triggered automatically on sparse trees (P1 below) or planner-hinted (`snapshot.py:79-95`). `ollama` is localhost by default (`ollama.py:29`); a cloud endpoint is the user's BYOK choice.
3. **Monitor HTTP GETs**: LLM-chosen URLs, ≥30 s apart, persisted across restarts (S4 below).
4. **Build/dev-time only, not the product:** `installer/fetch_bundle_inputs.py:37-57` (piper voice + HuggingFace `Systran/faster-whisper-base`), `tools/train_wake_model.py:36` (`urllib.request` for voice downloads), and the dev-mode STT hub fetch (L2 residual below).
5. **Nothing else**: mic audio stays local (capture → VAD → faster-whisper in memory, `capture.py`, `stt/fasterwhisper.py:50-69` — the frozen build loads the packaged ct2 dir, `app.py:362-382`); wake scoring is local ONNX (`audio/activation.py:387-415`, `ChunkQueue` memory-only at `activation.py:308-356`); TTS is a local subprocess; task store/log/config are local files.

## 5. Privacy — data at rest and still-open gaps

- **Verified:** no audio is ever written to disk (capture → VAD → STT in memory; the wake `ChunkQueue` is memory-only). `config.toml` holds no secrets. `%LOCALAPPDATA%/Marshmellow/tasks.json` is written atomically now (temp file + `os.replace` with retry — `src/marshmellow/tasks/store.py:58-78`, the bd418c9 fix) and stores monitor targets verbatim **by design**, documented in `docs/privacy.md`. Machine-enforced privacy test `tests/core/test_privacy_audit.py::test_session_log_is_clean` (full headless session, row-by-row assertions) passes in this run.
- **S3 — MEDIUM (still open): spoken secrets reach the plaintext session logs.** Every instruction is logged verbatim (`loop.py:207`, `app.py:306`), confirmation answers too (`src/marshmellow/safety/confirm.py:67-73`). `_SECRET_RE` only matches `password|passwd|secret|api_key|apikey|token` followed by `=` or `:` (`logging.py:20-23`) — natural speech ("my password is hunter2") passes through unredacted; the enforced test only covers the `password=` shape. Logs are retained forever with no rotation/purge (`logging.py:69-84`).
- **P1 — MEDIUM (still open): automatic screenshot egress + crash-persistent temp artifacts.** The vision fallback fires whenever the foreground walk finds < 10 UIA elements, even with no hint (`src/marshmellow/perception/snapshot.py:35, 79-86`), base64-shipping the marked PNG to the provider (`vision.py:263-286`) on the plain L0 read path. Raw + marked PNGs go to `%TEMP%/marshmellow-shot-*.png` (`snapshot.py:133-140`), purged only at task end (`orchestrator.py:453-461`, `tools.py:169-176`); a killed process leaves them, and there is no startup sweep.
- **L3 — LOW/INFO (unchanged): the clipboard privacy gate is inert by absence.** Nothing reads the clipboard; live snapshots are always empty `ContextSnapshot`s (`loop.py:184`, `manager.py:658-664`). The gate itself is correct (`src/marshmellow/core/prompt_builder.py:70-75`) — the day a reader is wired, it must gain tests.

## 6. Safety-gate integrity — verified sound

- **Every producer of an `ActionRequest` reaches an executor only through the intended chain.** Producers: the LLM orchestrator (`act` rebinds through the per-turn executor, failing closed for non-rebindable toolboxes — `orchestrator.py:567-622`, `tools.py:156-167`; M2 tools map via `_as_request` → `execute`, `orchestrator.py:537-565, 664-724`), the keyword `FakePlanner` (same manager path, `src/marshmellow/planner/fake.py:29-38`), and the TaskManager per-turn executor (`manager.py:666-713`) which checks the kill flag (`673-679`) and the D6 foreground lease (`680-709`) **before** `self._execute` (`711`). `App._execute` then routes: `ask_user` (read-only, spoken) direct; `shell` → `_gated_shell`; `create_task` → documented pre-gate registration (never executes actions — the monitor loop contains no executor calls, `manager.py:717-797`; scheduled turns run through the same gated turn path, `manager.py:799-814`); everything else → `_gated_execute`. Grep found no other caller of `pack.open_app` or an executor seam outside this chain.
- **Classification is authoritative and fail-closed:** declared risk ignored (`policy.py:102-107`), unknown kinds default L2 (`policy.py:108`), overrides only escalate (`policy.py:124-127`), settings dialog can only escalate the preset (`settings_dialog.py:100-107, 142-150`), password/sensitive-name → L3 via the resolver at both `check` and `guard` (`gate.py:91, 146-148`; resolver `app.py:611-616`), L3 confirm fail-closed end to end (`confirm.py:23-41, 62-78`; tray fallback defaults No/Escape, `tray.py:58-74`; headless has no fallback), kill re-probed after a "yes" (`gate.py:122-128`), every decision logged (`gate.py:160-169`).
- **A1 — MEDIUM (still open): the model-authored `rationale` is spoken and logged verbatim.** The L3 question embeds it (`gate.py:57-59`) and every L1 — including the newly gated launch — announces it (`gate.py:129-130`): for `open_app("https://host?token=…")` the token is **spoken aloud** (the equivalent log line is scrubbed by `_URL_SECRET_RE`; speech is not). The `act` tool description still has no anti-restatement instruction (`orchestrator.py:214-243`), so a rationale that restates a typed payload would be spoken/logged. Mechanism code-verified; real-LLM reproducibility **unverified** (no live LLM driven in this audit). Note this speech surface is a *consequence of the S1 fix* (pre-fix launches were silent) — keep the fix, constrain the rationale.

## 7. Newest-code regression hunt (M5 T5–T7)

- **S6 — MEDIUM (new): tray "Pause tasks" does not abort TTS.** §3.13 defines the kill switch as "'stop everything' control intent **+ tray pause** aborts actions and TTS instantly." The voice `kill_all` stops TTS, cancels the inflight turn, cancels all tasks (`loop.py:239-245`); the tray pause only calls `manager.cancel_all()` and queues a new utterance (`app.py:819-822`) — mid-speech TTS (e.g. a background report) keeps playing after the pause. The dangerous half (action abort) works; fix is one `self.tts.stop()` in the branch.
- **S6b — LOW (new, same branch): the menu label says "Pause tasks" but the behavior is the kill switch** — it cancels (and de-persists) monitors and scheduled tasks, not a resumable pause. Consistent with §3.13's kill-switch reading; the label misleads.
- **L9 — LOW (new): frozen-bundle wake/STT asset assumptions unverified.** The PyInstaller spec bundles `assets/wake` (including the tracked `hey-marshmellow.onnx`, 869,652 bytes) and `collect_data_files("faster_whisper"/"openwakeword")` (`installer/marshmellow.spec:54-73`), and `_build_stt` loads the packaged ct2 dir when frozen (`app.py:375-379`). Verified by reading the spec only — **the bundle was not built in this audit**, so the on-disk layout claim is **unverified**. (Doc-drift nit: `marshmellow.spec:70-71` says the `_internal/wake` melspec/embedding copies are "not what openwakeword looks for", but `activation.py:401-409` explicitly prefers exactly those copies when present.)
- Everything else in the newest commits checks out: the URL-scrub escape-pair fix is pinned by tests (all pass, including the payload-echo and quoted-URL rows, `tests/core/test_logging.py`); the task-store atomic write is in (`store.py:58-78`); the `_persist_terminal` before-flip/after-flip double drop matches its documented rationale (`manager.py:356-381, 582-584, 609-611`); the M5 T7 lazy STT load is race-guarded (`stt/fasterwhisper.py:39-54`).

## 8. Release hygiene

- **Present at HEAD:** `LICENSE` (MIT, © 2026 0xSpecters), `README.md`, `CHANGELOG.md`, `docs/user-guide.md`, `docs/privacy.md`, `docs/plan/RELEASE_NOTES.md` (with known-limitations sections). L8 is closed except:
- **L8r — LOW (residual): `pyproject.toml` `[project]` declares no `license` or `readme` field** (`pyproject.toml:1-13`) — packaging metadata gap; the LICENSE file itself is present.
- **L5 — LOW (still open, publish decision): 63,201,294-byte dead blob reachable in history** (`docs/plan/spikes/data/sp4_voice/en_US-amy-medium.onnx`, now untracked; verified largest blob via `git rev-list --objects --all | git cat-file --batch-check`; `git count-objects -vH`: 59.74 MiB loose). Publishing this history to GitHub ships it to every clone. `git filter-repo` (history rewrite) or a squash-fresh publish is the decision to make **before** the first push.
- **L6 — LOW (still open, disclosure decision): internal pipeline docs are tracked** and would be visible even in a private repo's collaborator view: `docs/plan/WORKFLOW-RULES.md` (names the autonomous builder session `dwfrun-192a5372…`, line 3), `docs/plan/build-ledger.jsonl`, `docs/plan/pending-user-verifications.md`, `docs/plan/spikes/*` (incl. bench JSON), `.zcode/skills/*`. No secrets in them (history scan clean) — but exposure of the autonomous-build internals is a decision to make explicitly.
- **L7 — LOW (still open): dead `.gitignore` entry `%LOCALAPPDATA%/`** (`.gitignore:16`) — git never env-expands it; `logs/`/`*.log`/`config.local.toml` do the real work.
- **L1 — LOW (still open): `[providers.llm].api_key_ref` is dead config** (`config.py:30`) — the provider always loads the fixed Credential Manager slot (`secrets.py:13-14`; `app.py:499-504`); a second/fallback provider cannot have its own key and the field misleads TOML editors.
- **L2 — LOW (residual, product fixed):** the frozen build loads the bundled ct2 model dir and never touches the network (`app.py:362-382`); dev checkouts still construct `WhisperModel("base")` and (library behavior, **unverified**) fetch from HuggingFace on first use (`stt/fasterwhisper.py:43-48`).
- **Committed binaries:** none beyond `tests/scenarios/fixtures/canvas.png` (test fixture); no tracked file exceeds 1 MB (`git ls-files` + size scan). `dist/`, `build/`, `models/`, `voices/`, `*.onnx` (with the deliberate `!assets/wake/hey-marshmellow.onnx` negation), `.venv/`, spike data dirs — all confirmed ignored via `git check-ignore -v` spot checks. Working tree clean (`git status --short` empty).
- **TODO/FIXME landmines — none:** `grep -rInE "TODO|FIXME|XXX|HACK"` over `src/`, `tools/`, `installer/`, `docs/plan/` returns only the pre-fix audit's own heading.

## 9. Checks run during this audit (exact commands)

1. `python -m pytest tests/ -q` (repo venv) → **493 passed, 3 skipped, 17 deselected** in 141.35 s — full offline suite, green.
2. `python -m pytest tests/test_release_fixes.py tests/core/test_privacy_audit.py tests/core/test_logging.py -v` → **20 passed** — the S1/S2 regression tests and the machine-enforced privacy audit.
3. `git grep` secret-pattern scan over tracked files → only a fake test key + a docs quote; same patterns (plus private-key headers) over **all history blobs** → zero hits.
4. `git rev-list --objects --all | git cat-file --batch-check` → largest blob 63,201,294 bytes (sp4 voice, history-only); `git count-objects -vH` → 59.74 MiB loose.
5. `git check-ignore -v` spot checks → all model/voice/build/log/config-local paths correctly ignored.
6. `git ls-files` + per-file size scan → no committed binaries beyond the test-fixture PNG, no tracked file > 1 MB.
7. Greps: network imports; `subprocess|startfile|Popen|os.system|shell=True`; `TODO|FIXME|XXX|HACK`; executor/`open_app` caller census.

## Verification notes

- **Unverified:** faster-whisper's HuggingFace fetch in dev mode (library behavior, not exercised); `os.startfile`'s per-scheme ShellExecute semantics (standard behavior, not run); the frozen bundle's on-disk layout (spec read, bundle not built); A1's real-LLM reproducibility (no live LLM driven); tray-pause thread-timing beyond code read (cancel paths are unit-tested, the GUI tray path itself was not driven end-to-end here).
- Read-only pass: the only file created is this report; nothing committed; the test suite ran against the existing venv.
