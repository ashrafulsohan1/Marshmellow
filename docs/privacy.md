# Marshmellow privacy audit (§3.15)

What Marshmellow stores, where it stays, what never leaves the machine, and how
to purge everything. The living rules the engine enforces are in
`src/marshmellow/core/logging.py` (the log scrubber) and
`src/marshmellow/core/secrets.py` (key storage); this page is the audit's
written record. Machine-enforced by `tests/core/test_privacy_audit.py` — a full
headless session whose log is asserted clean on every test run.

## What is stored where

| Data | Location | Lifetime |
| --- | --- | --- |
| Session log (JSONL: transcripts, snapshot summaries, action traces, errors) | `%LOCALAPPDATA%\Marshmellow\logs\session-*.jsonl` (cwd-fallback `logs\`) | Until you delete it |
| Spoken/transcribed instruction text | Inside the session log rows only — never sent anywhere except the LLM provider you configured | Until you delete the log |
| Background tasks (monitors/scheduled specs, baselines) | `%LOCALAPPDATA%\Marshmellow\tasks.json` (cwd-fallback `tasks.json`) | While the tasks are live; terminal rows are dropped automatically |
| Monitor targets | Inside `tasks.json`, verbatim — a monitor must re-fetch its target, so a query credential embedded in a watched URL is stored there **by design** (operational state, never a log) | With the task |
| Your cloud API key (D7) | Windows **Credential Manager** (`Marshmellow` / `llm_api_key`) — never in `config.toml`, never in a log | Until you delete it (tray/settings or Credential Manager) |
| Settings | `%LOCALAPPDATA%\Marshmellow\config.toml` | Until you delete it |
| Screenshots / marked images (SoM vision path) | The system temp dir (`marshmellow-shot-*.png`), **active-task-only**: produced when a planner turn reads the screen, deleted by the planner at task end (success, failure, or timeout) | Seconds, within one task |
| Microphone audio | Held in memory only; nothing is written to disk | Ephemeral |

## What never leaves the machine

- **No telemetry** — Marshmellow ships no analytics, crash reporting, or
  phone-home of any kind (01-architecture §0.7, constraint 7).
- The only network traffic is what you configure: the LLM/vision provider you
  point at (cloud or local Ollama) and the monitor targets your tasks watch.
- Transcripts, logs, screenshots, and task state stay on this machine.

## The scrub rules (session log)

Every session-log row is scrubbed after formatting, so message arguments and
exception text are covered too (an API key only reaches a log as a redaction):

1. `password=`-style shapes — `password|passwd|secret|api_key|apikey|token`
   followed by `=` or `:` — become `<name>=[REDACTED]`.
2. URL query credentials — `?key=`, `&token=`, `&sig=`, `&signature=`,
   `&access_token=`, `&api_key=`, `&apikey=` (case-insensitive) — become
   `<param>=[REDACTED]` while the rest of the URL survives, so monitor targets
   stay diagnosable ("watch https://api.example.com/feed?key=abc&x=1" is
   logged as "…feed?key=[REDACTED]&x=1").
3. Exact strings registered with the log scrubber — the API key is registered
   the moment it is stored or loaded (`secrets.store_api_key` /
   `load_api_key`), so even an accidental direct log of the value is
   redacted before it hits disk.

Typed passwords are never logged at all: the UIA reader renders a password
field as `(password)`, never its value (§3.13).

## How to purge everything

- **Session logs** — delete `%LOCALAPPDATA%\Marshmellow\logs\`.
- **Background tasks + monitor targets** — delete the task in the dashboard, or
  delete `%LOCALAPPDATA%\Marshmellow\tasks.json` while Marshmellow is closed.
- **API key** — remove it in settings/tray, or delete the `Marshmellow` entry
  in Windows Credential Manager.
- **Settings** — delete `%LOCALAPPDATA%\Marshmellow\config.toml`.
- **Screenshots** — nothing to do: they are purged at task end; a crash can at
  worst leave one in the system temp dir, which Windows cleans on its own
  schedule (or clear the temp dir yourself).
- **Uninstall-everything** — close Marshmellow, then delete the
  `%LOCALAPPDATA%\Marshmellow` folder and the `Marshmellow` Credential Manager
  entry. After that, no Marshmellow data remains on the machine.
