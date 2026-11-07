# Marshmellow — Pending User Verifications

Manual steps that need a human at the machine (speaking / listening / watching).
Each entry names the task that deferred it and what "done" looks like. Everything
automatable around these steps has already been implemented and gated.

## M2 Task 9 — assisted hands-free E2E: "hey marshmellow — open Notepad"

**Deferred from:** M2 plan Task 9 Step 2 (also discharges Task 2's "hear *On it.* from
the real speakers" and Task 4's real-device capture path, which this one session covers).

**Status:** not yet run — the wake word cannot lead this session yet: the self-trained
"hey marshmellow" model is still pending (D8/OQ-1), so the app degrades to
**hotkey activation** with a spoken hint at startup (expected: *"The wake word isn't
set up yet. Use the hotkey to talk to me."* — it may take a few seconds to start
speaking: the resident piper worker pays its model load on the first utterance).

**Run:**

1. `uv run python -m marshmellow` (tray + mascot appear; mic unmuted at the OS level).
2. Hold **ctrl+shift+m**, say **"open Notepad"**, release the keys.
3. Expected, hands-free end to end: mascot → listening → thinking → acting; *"On it."*
   spoken; a real Notepad window opens; result spoken; mascot → idle.
4. Barge-in: while the result is being spoken, just start talking — playback must stop
   immediately. Then say **"stop everything"** — any running task cancels, mascot → idle.
5. Repeat step 2 once more to confirm the loop still works after the barge-in.

**Done when:** steps 2–5 pass without touching mouse/keyboard (hotkey hold excepted
until the wake model ships). Record the date + any deviation in the M2 evidence block
in [02-milestones.md](02-milestones.md), then delete this entry.

**Optional (same session), cloud planner + the one open latency number:**

- Store the BYOK key: `uv run python -m marshmellow.tools.set_key`, then set
  `[providers.llm]` kind/base_url/model in `%LOCALAPPDATA%/Marshmellow/config.toml`.
  First cloud call doubles as the SP5 provider probe (OQ-2).
- With the key stored, re-run `uv run python tools/latency_m2.py` variant of the
  per-step measure (or just watch the log) and fill the pending "per agent step —
  cloud RTT" cell in the M2 budget table.

## M3 Task 8 Step 2 — assisted/manual E2E checklist (user-physical)

**Deferred from:** M3 plan Task 8 Step 2 — the whole checklist needs a human
speaking, listening, and watching; everything automatable around it is already
green and gated (headless scenario suite: `reference-check-channel`,
`l3-confirm-yes`/`l3-confirm-no` — ambiguity refuses fail-closed,
`vision-fallback` SoM path; kill switch covered headless in
`tests/test_voice_loop.py::test_stop_everything_cancels_running_task`).
Wake word caveat from the M2 entry still applies until the self-trained model
exists (D8/OQ-1): hands-free = after wake, or hotkey-held where noted.

**Run** (outcomes recorded verbatim below each step; assisted first, then the
hands-free repeat where the step names one):

1. **BYOK key (D7):** `uv run python -m marshmellow.tools.set_key`, then set
   `[providers.llm]` kind/base_url/model in `%LOCALAPPDATA%/Marshmellow/config.toml`
   so the cloud planner is live. Outcome: ____________ (date + provider model)
2. **Reference scenario, assisted:** "hey marshmellow — open the browser and
   check if *<your channel>* has new videos, tell me" → the agent must operate
   the browser generically (no channel integration exists) and speak a
   **correct** answer. Then the **hands-free** repeat (no keyboard). Outcome
   (verbatim spoken answers, both runs): ____________
3. **Live L3 confirmation:** trigger an L3 action — the password field on
   `tests/scenarios/fixtures/local_page.html`, or a delete in a scratch folder —
   and: hear the spoken confirmation; answer **"no"** → refused; **"yes"** →
   executed; ambiguous **"uh"** → refused (fail closed, live). Outcome (one
   line per answer): ____________
4. **Real vision fallback:** on a UIA-sparse surface (e.g. Paint canvas), with
   a vision-capable BYOK provider → the planner still perceives (SoM
   screenshot pass) and reports what it sees. Outcome: ____________
5. **Kill switch live:** say **"stop everything"** mid-UI-task → actions stop
   instantly, mascot idles. Outcome: ____________

**Done when:** all five outcomes are recorded verbatim in this block and then
copied into the M3 evidence block in [02-milestones.md](02-milestones.md);
then delete this entry. The headless proof of the same behaviors is the
`pytest -m scenario` suite (48 tests) — see the M3 evidence block for the
per-step latency FYI table (`tools/latency_m3.py`).

## M4 Task 7 Step 4 — assisted/manual M4 checklist (user-physical)

**Deferred from:** M4 plan Task 7 Step 4 — the whole checklist needs a human
speaking, listening, and watching the real screen, discharged in ONE session;
everything automatable around it is already implemented and gated (monitor
framework, one-shot triggers, baseline anti-re-report, foreground lease,
persistence, dashboard and `create_task` covered headless by `tests/tasks/` —
61 tests — plus the M4 soak harness `tools/soak_m4.py`). Wake word caveat from
the M2/M3 entries still applies until the self-trained model exists (D8/OQ-1):
"hey marshmellow" = after wake, or hotkey-held (hold **ctrl+shift+m**) where the
wake model isn't trained yet. Start the app with `uv run python -m marshmellow`.

**Run** (outcomes recorded verbatim below each step):

1. **BYOK stored (D7)** so the cloud planner is live: `uv run python -m
   marshmellow.tools.set_key`, then set `[providers.llm]` kind/base_url/model in
   `%LOCALAPPDATA%/Marshmellow/config.toml`. Then, assisted: **"hey marshmellow —
   watch this channel and tell me when they upload"** → monitor appears in the
   dashboard; later, **unprompted spoken report** "new upload: …" (the M4 E2E).
   Outcome: ____________
2. **Dashboard on the real screen:** tray → Open Marshmellow → task visible;
   **Pause** → no reports; **Resume**; **Delete**. Outcome: ____________
3. **Live lease consent:** scheduled task (**"in one minute, open Calculator"**)
   → spoken "I need the screen to … — OK?" → answer **"no"** → refused, nothing
   opens; repeat with **"yes"** → opens with a visible trace. Outcome: ____________
4. **Kill switch live with a monitor running:** **"stop everything"** → monitor
   cancelled, no further reports. Outcome: ____________
5. **Restart persistence live:** create a monitor → quit → relaunch → still
   listed, **no duplicate report**. Outcome: ____________

**Done when:** all five outcomes are recorded verbatim in this block and then
copied into the M4 evidence block in [02-milestones.md](02-milestones.md);
then delete this entry.

## M5 Task 7 — installer + milestone DoD checklist (user-physical, the milestone's ONLY user-required work)

**Deferred from:** M5 plan Task 7 Steps 6 — needs a fresh Windows local account, a
real mic/speaker and a full workday; everything automatable around it is already
implemented and gated: the bundle builds (`uv run pyinstaller
installer/marshmellow.spec --noconfirm`), `dist/Marshmellow/Marshmellow.exe --smoke`
exits 0 (headless install verification), the cold-start/idle-RSS bench ran
(`docs/plan/spikes/data/m5_startup.json`), and `tools/dogfood_report.py` turns the
"zero crashes" DoD into a command. Discharge ALL steps in ONE session.

**Run** (outcomes recorded verbatim below each step):

1. **Clean-account install (DoD):** on a fresh Windows local account, run the built bundle → onboarding wizard appears → mic check passes → BYOK key stored → wake word works hands-free (**"hey marshmellow"** with no hotkey — first session led by the self-trained model) → enable autostart → sign out/in → the app launches at login. Outcome: ____________
2. **Day-long hands-free dogfood (DoD):** use Marshmellow as the primary interface for a full workday, wake-word-led; afterwards `uv run python tools/dogfood_report.py --date <day>` → crash count **0**, friction list recorded. Outcome: ____________
3. **Wake false-accept feel:** during the dogfood day, note any unprompted wake (the subjective complement to Task 5's 1 h soak). Outcome: ____________
4. **D8 ship call (only if the feature-model license check is unresolved):** ship the wake path in the bundle, or release hotkey-only. Outcome: ____________

**Context for step 1:** the wake model ships in the bundle (`_internal/wake`, D8) —
but Task 5's ambient soak measured **7 false accepts/hour at threshold 0.99**, so
expect possible unprompted wakes; that observation IS step 3's data point. The
hands-free call was explicitly routed here (02-milestones M5 evidence block):
"saying 'hey marshmellow' aloud must wake the app — a human voice at threshold 0.99
is unverified until someone speaks." Step 4 is conditional: the feature-extractor
license check IS resolved (Apache-2.0, recorded in `assets/wake/README.md`), so
unless it is re-opened, the ship call defaults to **ship the wake path** (the spec
already bundles it) — record that decision, or re-open and choose hotkey-only.

**Done when:** the four outcomes are recorded verbatim in this block and copied
into the M5 evidence block in [02-milestones.md](02-milestones.md); then delete
this entry.

## M5 Task 4 — first-run onboarding + settings dialog (user-physical)

**Deferred from:** M5 plan Task 4 — the wizard and the modal dialog need a human
watching/listening; everything automatable is already gated (20 offscreen tests in
`tests/core/test_assets.py`, `tests/ui/test_onboarding.py`, `tests/ui/test_settings.py`:
should_onboard gating, injected mic-probe rendering, wake options disabled without a
model, key stored once and never echoed into TOML, autostart toggle through the
injected seam, cancel = no-op, invalid hotkey refuses Save, dashboard Settings…
button). Real seams the fakes stand in for: the sounddevice mic probe, Windows
Credential Manager, the HKCU Run key.

**Run** (outcomes recorded verbatim below each step):

1. **First-run wizard:** move `%LOCALAPPDATA%/Marshmellow/config.toml` aside →
   `uv run python -m marshmellow` → the "Welcome to Marshmellow" dialog appears
   BEFORE tray/mascot: the mic row shows a real probe result ("Microphone: OK — …"
   or a permission hint), the wake row says "not set up — the hotkey will be used;
   train later with tools/train_wake_model.py", and the wake-word/Both radios are
   disabled while the Hotkey radio is preselected. Outcome: ____________
2. **Wizard finish (D2/D3/D7):** pick Hotkey, paste the real API key, press
   "Save key" (field clears, "API key stored."), check autostart, Finish →
   `config.toml` appears containing `activation = "hotkey"` and `autostart = true`
   but NO trace of the key (`findstr /s sk- config.toml` finds nothing); the key
   answers `keyring`'s Credential Manager entry; the Run key exists. Relaunch →
   NO wizard (config exists now). Outcome: ____________
3. **Settings dialog live:** tray → Open Marshmellow → "Settings…" → set the hotkey
   to `notakey` → the error shows live and Save refuses; set a valid combo + strict
   preset → Save closes and updates config.toml; reopen → the lower preset items
   (relaxed/standard) are locked out — escalate only, never lower. With the dialog
   still open, run a task needing confirmation → the spoken/tray confirm fallback
   still fires (nested event loop). Restart → the new hotkey works. Outcome: ____________

**Done when:** all three outcomes are recorded verbatim in this block and copied
into the M5 evidence block in [02-milestones.md](02-milestones.md); then delete
this entry.

## M6 Task 6 — release-feedback feel checklist (user-physical)

**Deferred from:** M6 Tasks 1–5 — the art, the placement and the turn-taking are
implemented and gated offscreen (painter determinism + all-8-states rows, position
round-trip + unplugged-monitor clamp, window state-machine tests, the measured
0-ingestion echo-guard fixture, the quit census), but "does it FEEL right" needs a
human watching/listening at the machine. Run with the rebuilt bundle (M6 Task 7)
or `uv run python -m marshmellow`.

**Run** (outcomes recorded verbatim below each step):

1. **Mascot art approval + on-screen feel (OQ-3):** the white-marshmallow mascot
   appears bottom-right; walk it through states by talking to it (idle → listening
   → thinking → acting → speaking → idle) — the parametric art reads clearly, the
   state aura colors are distinguishable, nothing flickers or looks broken at rest.
   Outcome: ____________
2. **Drag + restart-persistence feel:** drag the mascot — it follows the cursor,
   never leaves the screen, and grabbing it does not click through to the window
   below; on release the position persists. Quit + relaunch → the mascot is where
   you left it (drag it, unplug/replug a monitor scenario aside, restore always
   lands on a visible screen). Outcome: ____________
3. **Turn-taking feel — one instruction with pauses:** wake (or hold **ctrl+shift+m**)
   and speak an instruction WITH pauses between phrases ("open notepad … then type
   hello world") — the phrases join into ONE instruction (no half-action fires after
   each phrase); watch the 3 s close: the window dispatches once, about 3 s after
   your last word. Outcome: ____________
4. **Media guard feel:** play a video/music while the assistant is idle (no wake) —
   the assistant ignores it: no wake, no instruction, nothing transcribed from the
   media audio. While a result is being spoken, media audio and your voice must NOT
   interrupt it — only the hotkey barge-in does (hold **ctrl+shift+m** mid-speech:
   playback stops at once, a fresh window opens). Outcome: ____________
5. **Tray icon look + full stop-from-tray:** the tray icon is the marshmallow art
   and visibly changes with state (idle/muted/attention distinct at a glance);
   menu = Mute / Unmute / Open Marshmellow / Stop tasks / Quit. With a task running,
   **Stop tasks** cancels it and stops any ongoing speech; **Quit** exits everything
   — Task Manager shows no orphaned Marshmellow/piper processes afterwards.
   Outcome: ____________

**Done when:** the five outcomes are recorded verbatim in this block and copied
into the M6 evidence block in [02-milestones.md](02-milestones.md); then delete
this entry.

## Carry-over (lower priority, not blocking M2 DoD)

- **Live-mic wake false-accept soak** (SP3 "Limitations", planned for M2): ≥ 1 h of
  ambient audio through the real watcher once the self-trained "hey marshmellow"
  model exists — before the wake word is trusted hands-free.
  → **Discharged by M5 Task 5** (`tools/soak_wake_m5.py --hours 1`: the REAL path —
  AudioEngine chunk_tap → ChunkQueue → WakeWordWatcher, self-trained model at the
  calibrated threshold — over ambient mic; verdict in
  [spikes/data/m5_wake_soak.json](spikes/data/m5_wake_soak.json) and the M5 evidence
  block). **Re-check in the M5 Task 7 checklist**: the other half of "trusted
  hands-free" is user-physical — saying "hey marshmellow" aloud must wake the app
  (the piper fixture proves the model fires; a human voice at threshold 0.99 is
  unverified until someone speaks), and a spoken lookalike ("hey marshmallow")
  must NOT wake it (see assets/wake/README.md's recorded lookalike limitation).
- **Mascot on-screen placement/feel** (M1 deferred offscreen verification): tray icon
  currently renders with the system default ("No Icon set" warning) — art is OQ-3.
  → **Discharged by M6** (Tasks 1/2/5): the parametric art shipped (`ui/mascot_art`,
  OQ-3 closed), the tray carries a per-state mascot still (no "No Icon set"), and
  placement/drag/persistence are implemented and offscreen-tested; the user-physical
  feel checks live in the M6 Task 6 checklist above.
