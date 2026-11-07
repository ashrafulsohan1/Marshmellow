# Marshmellow — User Guide

A hands-free, voice-controlled Windows desktop agent: say what you want, watch it
happen on screen, hear the result spoken back. It lives in the system tray, wears
a marshmallow mascot, and operates the computer the way a person would — through
UI Automation first, vision as a fallback.

This guide covers **using** Marshmellow (installer, onboarding, settings,
safety). For development and the build record see [README.md](../README.md) and
[docs/plan/](plan/PLAN.md).

## Install (Windows 10/11, 64-bit)

Two ways to get Marshmellow:

- **Frozen bundle (recommended)** — `dist/Marshmellow/` from a release build.
  Copy the folder anywhere (it is self-contained: interpreter, the faster-whisper
  STT model, the piper voice, and the self-trained wake model are all bundled in
  `_internal\`), then run `Marshmellow.exe`. No Python install is required; the
  only thing you supply is your own LLM API key.
- **From source (developers)** — Python 3.12 + [uv](https://docs.astral.sh/uv/):
  `uv sync`, then `uv run python -m marshmellow`. Models/voices are fetched at
  build time by `installer/fetch_bundle_inputs.py`.

Quick health check for the bundle: `Marshmellow.exe --smoke` starts and stops the
app headlessly and prints `smoke ok` (exit 0) — no windows, no microphone.

## First run (onboarding wizard)

On the first start (no config found), a one-page wizard collects everything the
engine needs before it boots:

1. **Microphone** — the row probes your default input device live ("Microphone:
   OK — …" or a permission hint). Windows Settings → Privacy → Microphone must
   allow desktop apps.
2. **Activation mode (D2)** — **wake word** ("hey marshmellow", hands-free),
   **hotkey** (hold the combo, talk, release), or **both**. The wake model is
   bundled and self-trained; if it were missing, the wake options grey out and
   the hotkey is used with a spoken hint.
3. **API key (BYOK/D7)** — paste your key for any OpenAI-compatible endpoint and
   press **Save key**. The key is stored in **Windows Credential Manager**, never
   on disk; the field clears and confirms.
4. **Autostart (D3)** — tick to launch Marshmellow at login (writes the HKCU Run
   key; no admin rights needed).

Finish → the tray icon and mascot appear and the engine starts. Settings persist
in `%LOCALAPPDATA%\Marshmellow\config.toml` (contains no secrets).

## Talking to Marshmellow

- **Wake word**: say **"hey marshmellow"**, wait for the mascot to switch to
  listening, then speak your instruction. No keyboard involved.
- **Hotkey**: hold the hotkey (default **ctrl+shift+m**), speak, release.
- **Ack**: every understood instruction is acknowledged aloud ("On it.") before
  actions start.
- **Result**: the outcome is spoken back. While it is being spoken, just start
  talking — that is a barge-in and playback stops immediately.
- **Stop everything**: cancels all running tasks instantly (also in the tray
  menu as Pause).

### What you can ask

Anything describable on screen — "open Notepad and write hello", "what did I
leave unfinished in this document", "check my channel page and tell me how many
videos I posted this week". There are no per-app integrations on purpose: one
perceive → act → verify protocol works across applications.

### Background tasks (M4)

- **Monitors**: "hey marshmellow — watch this channel and tell me when they
  upload" → the task appears in the dashboard and reports **unprompted, by
  voice** when the condition fires.
- **Scheduled**: "in one minute, open Calculator".
- **Dashboard**: tray → Open Marshmellow lists tasks with Pause / Resume /
  Run now / Delete, plus Settings and the trace of each turn.

Anything that needs the foreground screen asks first: *"I need the screen to …
— OK?"* — "no" refuses (fail-closed), "yes" grants a consented foreground lease.

## Safety levels (L0–L3)

Every action the planner takes is classified and passes a safety gate
(configurable preset; the dialog can only escalate it, never lower):

| Level | Meaning | Behavior |
|---|---|---|
| **L0** | read-only / nothing changes | autonomous |
| **L1** | reversible UI actions (clicks, typing into an app) | autonomous |
| **L2** | opens apps, shell, system surfaces | gated per policy |
| **L3** | destructive / sensitive (deletes, password fields, sending) | **spoken confirmation required** — "yes" executes, "no"/ambiguous refuses |

- **Kill switch**: "stop everything" — mid-action, no menu navigation needed.
- **Tray recovery**: mute/unmute the mic and pause all tasks from the tray even
  when voice itself is not working.
- **Consent (§3.12)**: background tasks never take the foreground without asking;
  UIA reads of background windows are allowed, private-window screenshots are not
  (OQ-11).

## Privacy posture

- **BYOK**: your API key lives in Windows Credential Manager; config files never
  contain it and logs never echo it.
- **Log scrubbing (§3.15)**: the session JSONL
  (`%LOCALAPPDATA%\Marshmellow\logs\session-*.jsonl`) redacts `password=…`-style
  secrets, URL query credentials (`?token=…`, `&api_key=…`), and any registered
  secret (your stored key is registered automatically at load).
- **No telemetry**: no SDKs, no crash reporting, no network calls except the LLM
  endpoint you configure.
- **Day report**: `uv run python tools/dogfood_report.py --date YYYY-MM-DD`
  summarizes a day's sessions — crash count, instructions, spoken results,
  monitor triggers — locally, from the local logs.

## Troubleshooting

| Symptom | Check |
|---|---|
| No sound | Tray → unmute; does a monitor/ack ever speak? First utterance may lag a few seconds (the piper worker loads its voice on first use). |
| "The wake word isn't set up yet…" spoken at start | Wake model missing → hotkey activation is used; train with `tools/train_wake_model.py`. |
| Hotkey does nothing | Another app owns the combo — change it in Settings. |
| Wizard reappears | Config deleted → re-run it; settings live in `%LOCALAPPDATA%\Marshmellow\config.toml`. |
| Autostart not firing | Task Manager → Startup apps → Marshmellow must be enabled (HKCU Run key). |
| Actions land on the wrong window | Foreground lease refused? The spoken ask needs an answer; check the dashboard trace. |
| Crashes / odd behavior | `uv run python tools/dogfood_report.py --date <today>` — crash rows carry tracebacks; file them with the friction list. |
