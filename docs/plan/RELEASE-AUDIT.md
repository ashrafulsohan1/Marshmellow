# Marshmellow — Pre-Release Security / Privacy / Safety Audit (v0.1.0)

**Date:** 2026-11-07 · **Auditor:** independent adversarial reviewer (read-only pass)
**Scope:** full read of the safety layer, secrets, logging, LLM providers, action layer,
perception, audio, tasks, composition root, UI surfaces, git state; every finding cites
`file:line` from code actually read. No tests were run; nothing was committed.
**Method note:** findings are what the code does today, judged against the product's own
declared posture (01-architecture.md §3.13 Safety/Consent, §3.15 EventBus & logging,
WORKFLOW-RULES.md, packs' and modules' docstrings).

## Severity summary

| Severity | Count | Findings |
|---|---|---|
| **High** | 2 | S1 shell/open_app gate bypass · S2 kill-switch/mute reachability broken |
| **Medium** | 5 | S3 spoken secrets in plaintext logs · S4 unconfirmed LLM-chosen monitor egress · S5 exception text as a secret channel · P1 automatic screenshot egress + crash-persistent temp artifacts · A1 model-controlled rationale can speak/log typed text |
| **Low** | 8 | L1 dead `api_key_ref` · L2 STT model auto-download / dead bundled-model dir · L3 clipboard privacy gate is inert · L4 sensitive-name heuristic gaps · L5 63 MB voice blob in git history · L6 internal pipeline docs ship in the public repo · L7 dead `.gitignore` entry · L8 no LICENSE / no README |

**Single most important finding:** S1 — the LLM planner's app/URL launch path
(`shell` + `open_app`) reaches `WindowsPack.open_app` *without passing the SafetyGate*
(`src/marshmellow/app.py:622-624`), violating §3.13's "every ActionRequest passes
pre-execution": no L1 announcement, no gate decision log, no confirmation, for an
LLM-chosen launch surface that includes arbitrary URIs.

---

## 1. SECURITY

### S1 — HIGH: `shell`/`open_app` actions bypass the SafetyGate entirely

- Routing: `App._execute` handles `kind == "shell"` with an `open_app` string payload
  **before** the gated executor — `src/marshmellow/app.py:622-624` returns
  `self.pack.open_app(...)` directly; only the fall-through at `app.py:627` reaches
  `self._gated_execute` (the `gate.guard(...)` wrapper built at `app.py:238`).
- Producer: the LLM's `open_app` tool maps to exactly this request
  (`src/marshmellow/planner/orchestrator.py:668-677`), and its tool description invites
  URLs: *"a name or alias …, an ms-settings: URI, or a URL"* (`orchestrator.py:139-148`).
- What the gate would have done and does not (`src/marshmellow/safety/gate.py`):
  L1 announce before execution (`gate.py:129-130`), the §3.13 decision+rationale log
  (`gate.py:160-169`), and the gate-level kill probe (`gate.py:92-103`).
- Launch surface (`src/marshmellow/packs/windows.py`):
  fixed alias table (`windows.py:25-37`), any `*.exe` resolvable on PATH
  (`windows.py:119-120`), and **any string containing `://` or a `shell:`/`ms-settings`
  prefix is handed to `os.startfile`** (`windows.py:117-121, 128-130, 137-140`) — i.e.
  the registered protocol handler for an LLM-chosen URL launches with no user-visible
  announcement. No `shell=True` anywhere (verified: the only `Popen` calls are
  `windows.py:131` with a fixed argv, `tts/piper.py:267-271`, `core/autostart.py:40-41`)
  — the gap is authorization, not injection.
- Partial mitigations that DO still apply: the TaskManager per-turn executor checks the
  kill flag before every action including shell (`src/marshmellow/tasks/manager.py:655-663`)
  and background modes still need a foreground lease for `shell` (`manager.py:664-693`;
  `shell` ∉ `READ_KINDS`, `src/marshmellow/tasks/foreground.py:31`). Headless/test paths
  and the M1 keyword planner produce the same ungated shell request
  (`src/marshmellow/planner/fake.py:25-33`).
- Policy table declares `shell: L1` (`src/marshmellow/safety/policy.py:57`) — that
  classification is dead code for the only shell path that executes.
- **Violates** 01-architecture.md §3.13 ("`safety/gate.py` — every `ActionRequest` passes
  pre-execution") and gate.py's own docstring (`gate.py:1-13`).
- **Fix direction:** route `shell`+`open_app` through `self._gated_execute` (announce =
  rationale), or gate it with a dedicated launcher classifier; add a regression test
  asserting the L1 announce fires for `open_app`.

### S2 — HIGH: the tray's "Pause tasks" / "Mute" / "Unmute" are no-ops — and voice mute is a one-way trap, so the documented kill switch is unreachable in the muted state

- The tray publishes `TrayAction(kind="pause"|"mute"|"unmute"|...)`
  (`src/marshmellow/ui/tray.py:20-26, 43-48`). The **only** subscriber is
  `App._on_tray_action` (`src/marshmellow/app.py:224`), which handles just `quit` and
  `open_ui` (`app.py:772-780`). No other component consumes these kinds (grep-verified).
- Consequences:
  - Tray **"Pause tasks"** does nothing. §3.13 defines the kill switch as
    *"'stop everything' control intent **+ tray pause** aborts actions and TTS
    instantly"* (01-architecture.md §3.13) — the tray half of the kill switch does not
    exist. The voice half works (`src/marshmellow/audio/loop.py:236-245` →
    `manager.cancel_all`, `manager.py:602-629`).
  - Tray **Mute/Unmute** do nothing (voice `mute`/`unmute` intents work,
    `loop.py:227-230`).
  - **The trap:** muting via voice drops every mic chunk *before* VAD
    (`src/marshmellow/audio/capture.py:145-150` — `if not self._alive or self._muted:
    continue`), so **no SpeechSegments are emitted at all** while muted: the voice
    "unmute" intent (`loop.py:229-230`) and even the voice kill switch
    ("stop everything", a control intent that must "never sleep",
    `loop.py:186-189`) can never be recognized again. The documented recovery surface —
    the tray — is dead. The only functioning exit is Quit (`app.py:773-774`).
- Hotkey does not restore control either: `HotkeyPressed` only activates the mascot
  (`loop.py:163-168`); it does not unmute.
- **Fix direction:** wire `mute`/`unmute`/`pause` TrayActions in `App._on_tray_action`
  (mute → `AudioEngine.set_muted`, pause → `manager.cancel_all` or a real pause), and
  consider letting the wake/hotkey activation channel unmute.

### S3 — MEDIUM: spoken credentials reach the plaintext session logs, and logs are retained forever

- The verbatim transcript of every instruction is logged:
  `src/marshmellow/audio/loop.py:207` (`logger.info("instruction submitted: %r", text)`)
  and `src/marshmellow/app.py:306`. §3.15's posture is "no typed secrets ever logged" —
  typed secrets are indeed suppressed (`src/marshmellow/planner/tools.py:85-95` keeps a
  password field's value out of the planner listing), but a **spoken** secret ("my
  password is hunter2 — log me in") lands in the JSONL on disk.
- The scrubber cannot catch natural speech: `_SECRET_RE` only matches
  `password|passwd|secret|api_key|apikey|token` followed by `=` or `:`
  (`src/marshmellow/core/logging.py:17-20`); registered exact-string redaction covers
  only the BYOK key (`logging.py:27-30, 45-47`).
- Confirmation answers are also logged verbatim (`src/marshmellow/safety/confirm.py:67-73`).
- Retention: one `session-*.jsonl` per run under
  `%LOCALAPPDATA%/Marshmellow/logs/` with **no rotation or purge**
  (`logging.py:51-66`); nothing in the codebase ever deletes old sessions.
- **Fix direction:** extend the scrub patterns (e.g. redact text after
  `password (is|:)`-style phrases is hard — more realistic: keep the log but document
  that spoken secrets are stored locally, or hash/omit instruction bodies at INFO and
  keep them at DEBUG; add a retention cap).

### S4 — MEDIUM: LLM-created monitors make unconfirmed, perpetual, LLM-chosen HTTP requests

- `create_task` is deliberately routed before the gate (`app.py:625-626`, documented at
  `app.py:600-608`) and `App._create_task` (`app.py:629-703`) validates *shape* only —
  the monitor **target URL is never confirmed with the user**. Once registered, the
  check GETs the URL every ≥30 s (`src/marshmellow/tasks/monitors.py:45-57`,
  `httpx.get(url, follow_redirects=True)`; floor at `app.py:97, 662-666`), survives
  restarts (`src/marshmellow/tasks/store.py:56-69` + `manager.py:371-405`), and runs
  forever until triggered or 5 consecutive errors (`manager.py:733-767`).
- Exfil shape: everything the planner can see (instruction text, window titles, element
  names, non-password element values) can be embedded by a prompt-injected page into
  the monitor target — a periodic GET is then a covert channel that never crosses the
  gate. (Bounds: the clipboard never reaches the model today — see L3 — and password
  values are suppressed, so the exposure is limited to screen-derived text.)
- Minor: requests carry httpx's default `python-httpx` User-Agent — monitor targets can
  fingerprint the client (`monitors.py:51`).
- **Fix direction:** spoken/tray confirmation of a monitor target at registration (the
  same ConfirmFlow the lease uses), or restrict first-party confirmations to a
  user-approved allowlist.

### S5 — MEDIUM: raw exception text is a secret-bearing channel into observations and logs

- `ActionLayer.execute` converts any crash into
  `observation=f"action {req.kind} crashed: {exc}"` (`src/marshmellow/actions/executor.py:132-138`)
  and logs it with `exc_info=True` (`executor.py:133`). The observation travels back to
  the LLM (`orchestrator.py:483-485, 726-737`) and the exception text into the JSONL log.
  COM/UIA failures during `SetValue` can embed the value being set — i.e. typed text —
  in the exception message; the scrubber would not catch it (see S3).
- Same pattern in the toolbox (`tools.py:148-152`, `error=repr(exc)`) and the planner
  (`orchestrator.py:559-565, 447-449`); `TaskManager` stores `repr(exc)` in
  `handle.error` shown in the dashboard (`manager.py:572-580`, `ui/main_window.py`).
- Residual risk only (a crash mid-`SetValue` on a secret payload), but the channel is
  structural: **fix direction** — log the exception class + kind, keep the full text at
  DEBUG, and never echo raw exception text into planner-visible observations.

### L1 — LOW: `[providers.llm].api_key_ref` is dead config; one fixed credential slot

- `config.py:30` declares `api_key_ref` as the Credential Manager reference
  ("never the key itself"), but nothing reads it: the provider always loads the single
  fixed slot `service="Marshmellow"` / `account="llm_api_key"`
  (`src/marshmellow/core/secrets.py:13-14, 33-42`;
  `app.py:481-486` passes `get_key=secrets.load_api_key`). A second provider (e.g. the
  §3.4 fallback) cannot have its own key, and the field misleads users editing TOML.

### Secrets handling — verified clean (for the record)

- Key entry is hidden and never echoed: `getpass` in the CLI
  (`src/marshmellow/tools/set_key.py:15-20`), `EchoMode.Password` + `clear()` after
  store in settings (`src/marshmellow/ui/settings_dialog.py:110-114, 151-159`), same in
  onboarding (`src/marshmellow/ui/onboarding.py:5-9`).
- Key is registered with the scrubber on both store and load
  (`secrets.py:24, 41`), the scrubber runs on the fully formatted line *including
  exception text* (`logging.py:43-47`), the key never enters TOML
  (`config.py:64-70` saves a pydantic dump of non-secret settings;
  `Settings` carries no key field), and never enters subprocess args (piper argv is the
  model path only, `tts/piper.py:261-265`; autostart argv is a quoted interpreter path,
  `core/autostart.py:78-89`). Spoken text reaches TTS via stdin, not argv
  (`piper.py:223-229`).
- No committed secrets: pattern scan over tracked files (`sk-…`, `api_key=`,
  `Bearer …`, `password=…`) found nothing; git history contains no key-shaped blobs
  (only the voice ONNX, see L5).

---

## 2. PRIVACY

### P1 — MEDIUM: screenshots are shipped to the cloud LLM automatically, and crash leaves them in %TEMP%

- The vision fallback triggers whenever a foreground snapshot finds fewer than 10 UIA
  elements — even when the planner did not ask for vision
  (`src/marshmellow/perception/snapshot.py:79-86`, `FALLBACK_THRESHOLD = 10` at
  `snapshot.py:35`). The marked PNG of the user's window is base64-sent to the
  configured vision provider (`src/marshmellow/perception/vision.py:263-286`), on the
  plain `get_elements` read path (L0, autonomous).
- Config-level opt-in exists (`[providers.llm].vision`, `app.py:46-48, 485`), but the
  per-turn decision is automatic; a user dictating to a sparse window (e.g. a bare
  terminal) ships that window's pixels to the cloud without any consent step.
- Artifacts: raw + marked PNGs are written to the system temp dir
  (`snapshot.py:133-140`, `tempfile.mkstemp`), tracked and purged at task end
  (`orchestrator.py:450-461`, `tools.py:169-176`) — but a killed/crashed process leaves
  them behind; there is no startup sweep.
- **Fix direction:** gate the threshold-triggered (non-hinted) vision call behind the
  same announce/confirm machinery, and sweep stale `marshmellow-shot-*.png` at startup.

### P2 — LOW/INFO: egress inventory (what actually leaves the machine)

1. **Planner LLM calls** (configured provider): instruction text, active window
   app+title (`orchestrator.py:745-754`), element listings — names and values of
   non-password fields (`tools.py:77-95`), all tool results/observations, system prompt.
2. **Vision LLM calls**: foreground-window PNGs (P1).
3. **Monitor HTTP GETs**: LLM-chosen URLs (S4).
4. **faster-whisper model fetch**: `WhisperModel("base")` resolves a model name and
   (library behavior, unverified at runtime) downloads from HuggingFace Hub on first
   use unless cached (`src/marshmellow/stt/fasterwhisper.py:27`). Undocumented in the
   §3.15 posture, and the bundled-model resolver exists but is never called (see L2).
5. Nothing else: mic audio stays local (VAD + local STT, `audio/capture.py`,
   `stt/fasterwhisper.py`), TTS is a local subprocess (`tts/piper.py`), wake scoring is
   local (`audio/activation.py:461-494`), there is no telemetry, crash reporting, or
   update check anywhere (grep-verified: the only network imports are httpx in
   `llm/ollama.py`, `llm/openai_compat.py` (via SDK), `tasks/monitors.py`).

### L2 — LOW: STT downloads from the network while the bundled-model seam is dead code

- `default_stt_model_dir()` / `default_voice_dir()` (`src/marshmellow/core/assets.py:46-55`)
  have **zero callers** (grep-verified); `_build_stt` always constructs
  `FasterWhisperSTT(model=settings.stt_model)` with the bare name "base"
  (`app.py:358-364`) — so the installer's planned bundled STT model would be ignored
  and every fresh machine fetches the model from the network at first start. Both a
  privacy-posture gap (undocumented egress) and a functional bug for the offline
  `offline` profile promised in §3.14.

### L3 — LOW: the clipboard privacy gate exists but the clipboard is never read

- `PromptBuilder._gated_context` only keeps `context.clipboard` when the instruction
  mentions "clipboard"/"copied" (`src/marshmellow/core/prompt_builder.py:42, 70-75`)
  — a good design — but the live
  voice loop and the task layer always construct **empty** `ContextSnapshot`s
  (`audio/loop.py:184`, `tasks/manager.py:643-648`), and nothing in the codebase ever
  reads the clipboard. So today there is no clipboard egress at all, and the gate is
  untested against a real reader. Note for future contributors: the moment clipboard
  reading is wired, the §3.3 gate must be exercised by tests — currently nothing would
  catch a regression that reads it ungated.

### Data-at-rest inventory (verified)

- `%LOCALAPPDATA%/Marshmellow/logs/session-*.jsonl` — transcripts, action traces, gate
  decisions; plaintext, unbounded (S3).
- `%LOCALAPPDATA%/Marshmellow/tasks.json` — task specs (instruction text) + monitor
  baselines, plaintext (`tasks/store.py:56-69`); cancels drop rows eagerly
  (`manager.py:501-517`), graceful stop keeps them by design.
- `%LOCALAPPDATA%/Marshmellow/config.toml` — settings only, no secrets.
- Credential Manager — the BYOK key (single slot, L1).
- `%TEMP%/marshmellow-shot-*.png` — screen captures, purged per task, crash-leaky (P1).
- No audio is ever written to disk (capture → VAD → STT in memory,
  `audio/capture.py:96-107`; the wake `ChunkQueue` is memory-only,
  `audio/activation.py:308-356`).

---

## 3. SAFETY (L0–L3 gates, password fields, kill switch)

### Gate architecture — verified sound (for the record)

- Classification is authoritative and recomputed server-side of the LLM: declared
  `req.risk` is ignored (`policy.py:102-107`); unknown kinds default to **L2**
  ("never safe", `policy.py:46, 108`); overrides only escalate via `max`
  (`policy.py:124-127`); the settings dialog can escalate but never lower the preset
  (`ui/settings_dialog.py:100-107, 143-150`).
- Password fields: `IsPassword` is read from live UIA (`perception/uia_reader.py:214`),
  carried on `Element.password`, suppressed from the planner listing
  (`tools.py:85-95`), and escalated to **L3 in every preset/override**
  (`policy.py:110-113`). The gate resolves `target_ref` → element at both the `check`
  and `guard` seams (`gate.py:91, 146-148`; resolver wired at `app.py:440, 593-598`),
  so password escalation applies even when the caller passes no element. The executor
  refuses ref-less element actions (`executor.py:154-165`). The typed text itself is
  never placed in the confirm question (only kind + element name + rationale,
  `gate.py:57-59`) — a deliberate TOCTOU review (element re-registration between
  `get_elements` and `act`) found no bypass: gate and executor resolve the *same*
  registry mapping, and within a turn the planner is single-threaded (one turn runs at
  a time, `manager.py:534-536`).
- L3 confirm is fail-closed end to end: whole-utterance yes/no whitelists
  ("yesterday" cannot confirm, `confirm.py:23-41`), one re-ask → tray dialog → refuse
  (`confirm.py:62-78`), tray defaults to No / Escape refuses (`tray.py:58-74`),
  headless has no fallback at all (`app.py:456-465`), and the kill probe is re-checked
  after a "yes" (`gate.py:122-128`).
- Background modes: foreground-lease enforcement sits **before** the executor seam
  (`manager.py:664-693`), fails closed with `lease_required`/`lease_denied`
  (`manager.py:669-693`), one consent attempt per turn, grants tombstoned when the
  turn dies mid-ask (`foreground.py:82-86, 108-118`), and the lease auto-releases in
  the turn's `finally` (`manager.py:582-587`). Monitors never execute actions at all
  (`manager.py:701-769` — no executor calls in the monitor loop).
- The M4 constructor-time-executor bypass is closed: the orchestrator rebinds
  `act` through the per-turn executor and fails closed for non-rebindable toolboxes
  (`orchestrator.py:567-617`, `tools.py:156-167`).
- Elevated (admin) targets are refused, never force-acted (`executor.py:186-196`,
  `actions/uia_actions.py:38-57`).
- Step budget + wall-clock timeout bound a runaway turn
  (`orchestrator.py:85-86, 426-437`).

### A1 — MEDIUM: the model-authored `rationale` is spoken aloud and logged, and can carry the text about to be typed

- The L3 confirm question embeds the LLM's rationale verbatim and is **spoken**:
  `gate.py:57-59` → `ConfirmFlow` → `AskUserChannel.ask` (`app.py:421-425`). For L2
  acts (standard preset) the rationale lands in the `step` event shown in the
  dashboard (`manager.py:694`). Nothing sanitizes or constrains the rationale
  (schema: "Why this action, one phrase", `orchestrator.py:233`).
- Failure shape: if the model restates the payload ("type hunter2 into the password
  field") — plausible when the secret came from the instruction — the secret is spoken
  through the speakers and written into the trace. **Unverified as reproducible** (no
  live LLM was driven in this audit); the mechanism is code-verified.
- **Fix direction:** instruct against payload restatement in the act-tool description,
  and/or filter the question to kind + target only for password-classified elements.

### A2 — MEDIUM (folded into S4): scheduled tasks re-run LLM instructions later without a fresh consent

- `create_task(mode="scheduled", schedule=…, instruction=…)` registers a turn that
  executes autonomously at the scheduled time (`manager.py:774-789` → the same gated
  turn path). Per-action gates still apply (kill/lease/gate), so this is not a gate
  bypass — but nothing re-confirms the *instruction* at fire time, and the monitor/
  scheduled rows survive restarts (`manager.py:371-405`). Combined with S4 this is the
  persistent-autonomy surface a release note should disclose.

### L4 — LOW: the sensitive-name heuristic and the batch-delete payload are model-trusting

- `SENSITIVE_NAME_RE` matches element *names* only
  (`policy.py:33-36`): a confirmation dialog's `OK`/`Yes` button inside a
  "Delete everything?" dialog is not caught (the destructive verb usually lives in the
  dialog title or another element), and non-English UIs are out of scope. The batch
  rule (delete > 5 → L3) trusts the planner to declare `delete_items`/`delete_count`
  in the payload (`policy.py:76-88`) — nothing enforces that a destructive action
  carries the payload, so an "invoke on the Empty-Trash button" path relies entirely
  on the name match. By-design heuristic (§3.13 says "fields detected via UIA /
  sensitive names"), documented here as a known limitation, not a bypass.

### Kill switch reachability — see S2 (high)

Voice "stop everything" and "cancel" work while the mic is unmuted
(`loop.py:220-245`, `manager.py:602-629`); the ask channel and the gate poll the kill
flag with ~100 ms granularity (`audio/ask.py:32, 83-95`; `gate.py:122-128`; executor
flag check `manager.py:655-663`). After a voice "mute", **no** voice control intent can
ever be recognized (capture drops all chunks before VAD, `capture.py:145-150`) and the
tray recovery buttons are unwired — the kill switch is unreachable in exactly the state
(a muted mic) a user might want it.

---

## 4. RELEASE HYGIENE

### L5 — LOW: 63 MB of dead binary sits in the published git history

- `docs/plan/spikes/data/sp4_voice/en_US-amy-medium.onnx` (63,201,294 bytes) was
  untracked in commit `4c6f236` but remains reachable in history; every clone pays
  ~58 MiB for it (`git count-objects`: 58.13 MiB loose). If the public release reuses
  this history, consider `git filter-repo` (history rewrite — coordinate with the
  running pipeline first) or accept the bloat consciously.

### L6 — LOW: internal pipeline documents ship in the public repo

- Tracked and public on release: `docs/plan/WORKFLOW-RULES.md` (names the autonomous
  builder session id `dwfrun-192a5372…` and publishes token-consumption telemetry of
  the build), `docs/plan/build-ledger.jsonl`, `docs/plan/pending-user-verifications.md`
  (personal-machine checklists), `docs/plan/spikes/*` (benchmarks), `.zcode/skills/…`.
  No secrets — but a public v0.1.0 that exposes the autonomous-build pipeline internals
  is a disclosure decision to make explicitly, not by default.

### L7 — LOW: `.gitignore` dead entry

- `%LOCALAPPDATA%/` (`.gitignore:16`) is a literal directory name that git on Windows
  never matches (it is not env-expanded). Harmless — `logs/` and `*.log` (lines 14-15)
  plus `config.local.toml` (line 21) do the real work — but it suggests coverage that
  does not exist; remove it to avoid false confidence.
- Verified otherwise correct: `voices/`, `*.onnx` (with the deliberate
  `!assets/wake/hey-marshmellow.onnx` negation), `.venv/`, spike data dirs. The wake
  assets that will be committed are fully synthetic piper speech with a recorded
  Apache-2.0 license check (`assets/wake/README.md`, `assets/wake/manifest.json` —
  1000 synthesized utterances, no user recordings).

### L8 — LOW (release blockers): no LICENSE, no README, no user guide

- No `LICENSE`/`COPYING` file exists, and `pyproject.toml` declares no `license` field
  — yet the tree embeds a GPL-3.0 boundary decision (piper imported only inside a
  subprocess, `tts/piper_worker.py:15-16`) and Apache-2.0 wake assets. Before
  publishing, the license of Marshmellow itself must be chosen and stated.
- No `README.md` / `docs/user-guide.md` (planned for M5 Task 7,
  `docs/plan/tasks/2026-11-04-M5-polish-release.md:330`). Publishing v0.1.0 without
  them would ship a repo whose only docs are internal build plans.
- Untracked-in-worktree files the wake feature needs (parallel builder owns them):
  `tools/train_wake_model.py`, `tools/soak_wake_m5.py`, `tests/audio/test_wake_training.py`,
  and the whole `assets/` directory — all must land before the release tag or the wake
  story references files that do not exist in the published tree.

### TODO/FIXME landmines — none

- `grep -rInE "TODO|FIXME|XXX|HACK"` over `src/`, `tools/*.py`, and the plan/task docs
  returned clean. No commented-out code blocks of consequence were observed.

### Git state at audit time

- Branch `main`, 30 commits, HEAD `40922bc`. Working tree has **uncommitted modifications**
  owned by the parallel builder (`.gitignore`, `app.py`, `audio/activation.py`,
  `audio/capture.py`, `pyproject.toml`, `uv.lock`, tests) and untracked wake-training
  files — this audit read the worktree state; findings are pinned to line numbers as of
  2026-11-07 and may shift if the builder lands M5 Task 5/6.

---

## Verification notes

- Every claim above was read from the cited file in this checkout; nothing is inferred
  from architecture prose alone. Items marked **unverified**: faster-whisper's
  HuggingFace download at runtime (library behavior, not read), A1's real-LLM
  reproducibility, and `os.startfile`'s handling of specific URI schemes (standard
  ShellExecute semantics, not exercised here).
- Not run, per constraints: the test suite, the app itself, any network call, any
  commit. The repo was treated as read-only except this single report file.
