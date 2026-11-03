# SP1 — UIA coverage survey

**Date:** 2026-11-04 · **Machine:** user's Windows 11 desktop (10.0.26300, x64)
**Resolves:** perception fallback thresholds (01-architecture §3.6) — is UIA-first (D5) viable, and when do we fall back to vision?

## Results

| App | Interactable | Total elements | Walk time | Depth | Verdict |
|---|---|---|---|---|---|
| Notepad (content-bearing doc) | 25 | 70 | 359 ms | 6 | **uia_usable** |
| Calculator (UWP) | 53 | 126 | 146 ms | 7 | **uia_usable** |
| File Explorer (Downloads) | 161 | 259 | 949 ms | 9 | **uia_usable** |
| Microsoft Edge (about:blank) | 104 | 283 | 1590 ms | 11 | **uia_usable** |
| Windows Settings (UWP) | 49 | 167 | 7537 ms | 10 | **uia_usable** |

Data: `data/sp1_survey_run1.json` (explorer/edge/settings/notepad) + `data/sp1_survey_rerun.json` (calculator, fixed method).

## Findings

1. **UIA-first is viable across the whole mainstream corpus — 5/5 usable.** Chromium/Electron (Edge), UWP (Calculator, Settings), Win32 (Explorer, Notepad) all expose rich trees with semantic names (`Button:Copy`, `Edit:Search box`…). This is the cheap, precise, low-token channel the architecture bet on.
2. **UWP apps report an empty frame before content loads.** Calculator measured 4 interactable on first pass (settle artifact) and 53 after a settle-poll. → `ScreenPerception.snapshot()` must **poll until the tree stabilizes** (short deadline), never take one fixed-speed shot.
3. **Walk cost varies 30×.** Settings took 7.5 s vs Calculator 0.15 s. → cache the UIA tree per `(hwnd, tick)` and walk lazily/query-scoped (the planned `get_elements(query)` tool), not full-tree every step.
4. Vision fallback (D5) stays mandatory for custom-drawn surfaces (games, canvas apps) — not needed anywhere in this corpus, exactly as designed.

## Decisions fed into the architecture
- Fallback threshold confirmed: snapshot with **< 10 interactable elements** (or planner hint) triggers vision fallback.
- `snapshot()` = settle-poll + per-window tree cache (01-architecture §3.6 amended by this report).
- **Safety rule (hardened by incident below):** automation must only ever address windows it positively identified as its own launch targets — never "any window whose title matches". Goes into ActionLayer + test harness rules (01 §3.7, 02-milestones M3 DoD).

## Methodology notes (honest record)

- **Incident:** run 1's window matcher fell back to a *pre-existing* Notepad (the user's unsaved doc) and sent it WM_CLOSE. Windows 11 Notepad session-restores unsaved tabs, so no dialog appeared and the document was restored intact and left open immediately after (`RESTORED_USER_DOC: True`). The matcher was rewritten to **new-window-only** and the survey now skips a target rather than touch anything pre-existing.
- Calculator run-1 numbers were a UWP settle artifact; re-run with settle-poll produced the real counts.
- SP2's first-launch `uv pip install` is still running concurrently (separate spike venv; no interference with survey results).
