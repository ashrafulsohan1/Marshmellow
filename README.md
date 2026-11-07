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

## Install & run (Windows, Python 3.12)

```powershell
uv sync
uv run python -m marshmellow
```

Configure your own LLM key (BYOK — stored in Windows Credential Manager,
never on disk):

```powershell
uv run python -m marshmellow.tools.set_key
```

Then point `[providers.llm]` at your OpenAI-compatible endpoint in
`%LOCALAPPDATA%/Marshmellow/config.toml`.

## Repository layout

- `src/marshmellow/` — the agent (audio, STT/TTS, planner, perception,
  actions, safety, tasks, UI).
- `docs/plan/` — the full planning + build record (architecture, milestones,
  spikes, audits).
- `tools/` — spike harnesses and training scripts.

## Development

```powershell
uv run pytest -q        # tests (manual tier deselected)
uv run ruff check .     # lint
uv run mypy             # type check (core)
```

License: [MIT](LICENSE).
