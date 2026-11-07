# Marshmellow

A hands-free, voice-controlled Windows desktop agent: say what you want,
watch it happen on screen, hear the result spoken back. Not a chat window —
an ambient assistant that lives in the tray, wears a marshmallow mascot, and
operates the computer the way a person would.

> **Status:** v0.1.0 (private preview). Built per the plan in
> [`PLAN.md`](docs/plan/PLAN.md); see
> [`docs/plan/BUILD-LOG.md`](docs/plan/BUILD-LOG.md) for build history.

## What it does

- **Voice loop** — wake word / hotkey → speech-to-text → an LLM planner →
  live on-screen actions (UI Automation first, vision fallback) → spoken
  result.
- **Generic app control** — one perceive → act → verify protocol for any
  application; no per-app integrations.
- **Background tasks** — monitors ("tell me when they upload"), scheduled
  actions, and long-running jobs, reported by voice and managed from the UI;
  anything needing the screen asks for a consented foreground lease first.
- **Safety gate** — every LLM-driven action passes a risk gate (L0–L3);
  sensitive actions require spoken confirmation; "stop everything" kills all
  tasks; fail-closed confirmations.

## Install & run (Windows)

**Frozen bundle (no Python needed)** — build it once, run it anywhere:

```powershell
uv run python installer/fetch_bundle_inputs.py            # voice + ct2 model (re-downloadable, untracked)
uv run pyinstaller installer/marshmellow.spec --noconfirm
dist\Marshmellow\Marshmellow.exe                          # tray + mascot; first run opens the onboarding wizard
dist\Marshmellow\Marshmellow.exe --smoke                  # headless verification: prints "smoke ok", exit 0
```

The bundle ships the faster-whisper STT model, the piper voice, and the
self-trained wake model inside `_internal\` — a clean account installs
offline-capable; the only thing you supply is your own LLM API key (BYOK,
stored in Windows Credential Manager, never on disk). The bundle contains two
exes: `Marshmellow.exe` (GUI) and `piper_worker.exe` — the isolated subprocess
that keeps piper's GPL code out of the GUI process. See
[docs/user-guide.md](docs/user-guide.md) for the full walkthrough
(onboarding, activation modes, safety levels, privacy).

**From source (Python 3.12 + uv):**

```powershell
uv sync
uv run python -m marshmellow
```

Configure your own LLM key (BYOK):

```powershell
uv run python -m marshmellow.tools.set_key
```

Then point `[providers.llm]` at your OpenAI-compatible endpoint in
`%LOCALAPPDATA%/Marshmellow/config.toml`.

## Repository layout

- `src/marshmellow/` — the agent (audio, STT/TTS, planner, perception,
  actions, safety, tasks, UI).
- `docs/plan/` — the full planning + build record (architecture, milestones,
  spikes, audits); `docs/user-guide.md` — the end-user walkthrough.
- `installer/` — the pyinstaller spec + build-time model fetcher (OQ-9).
- `tools/` — spike harnesses, benches, the wake-model trainer, and the
  `dogfood_report.py` day report.

## Development

```powershell
uv run pytest -q        # tests (manual tier deselected)
uv run ruff check .     # lint
uv run mypy             # type check (core)
```

License: [MIT](LICENSE).
