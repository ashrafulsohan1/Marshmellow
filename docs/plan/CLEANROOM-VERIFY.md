# Cleanroom Verify — Marshmellow build from a clean checkout

Date: 2026-11-07
Cleanroom clone: `C:\Users\DELL\AppData\Local\Temp\marshmellow-cleanroom` (removed after the run)
Verified commit: `40922bc2f4321096bf6378f35cd6dd74ae164738` (HEAD of the main tree at clone time; the main tree's uncommitted changes are NOT part of a clean checkout and were excluded by design)

## 1. Clone

| Step | Result |
|---|---|
| `git clone "C:\Users\DELL\OneDrive\Documents\Projects\Marshmellow" "%TEMP%\marshmellow-cleanroom"` | exit 0 |
| Clone HEAD | `40922bc2f4321096bf6378f35cd6dd74ae164738` |
| `git status` in clone | clean (no local modifications) |

## 2. uv sync

- `uv sync` — **exit 0**. Installed the full environment (84 packages) purely from the committed `pyproject.toml` + `uv.lock`. Notable installs: pyside6 6.12.0, sounddevice 0.5.6, scikit-learn 1.9.1, faster-whisper stack (tokenizers 0.23.3), pytest 9.2.0, ruff 0.17.3, uiautomation 2.0.29.
- `uv lock --check` — **exit 0** ("Resolved 84 packages in 3ms"): the committed lock is consistent with the committed `pyproject.toml`. No re-lock, no missing pins.

## 3. Gates (run un-piped, in the clone)

| Gate | Exit code | Output |
|---|---|---|
| `uv run pytest -q` | **0** | `453 passed, 5 skipped, 17 deselected in 83.48s` (manual-marked tests deselected by default, as designed) |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run mypy` | **0** | `Success: no issues found in 43 source files` |

## 4. Spot-check: `uv run python -m marshmellow.tools.simulate "open notepad"`

- The module exists in the clone and **imports cleanly**: `python -I -c "import marshmellow.tools.simulate"` → exit 0 (`IMPORT_OK`).
- **End-to-end run SKIPPED**, and why:
  - Keys/network: NOT the problem. No `%LOCALAPPDATA%/Marshmellow/config.toml` and no `config.local.toml` exists on this machine, so `load_settings()` falls back to defaults with LLM provider `kind: "fake"` — no API key, no network.
  - Real windows: IS the problem. `headless=True` only suppresses tray/mascot/mic (fake STT); `App._build_action_layer()` still wires the real `ActionLayer` (`UiaAutomationPatterns` + `InputSynthesizer`/SendInput), and `WindowsPack._ALIAS_ARGV` maps `"notepad"` → `notepad.exe` via subprocess. `simulate.py`'s own docstring states "With the real WindowsPack, the instruction really opens the app." Per instructions, the run was skipped to avoid opening real windows on the user's desktop.

## 5. Missing from git / dev-tree vs clone differences

All four untracked paths in the main tree are absent from the cleanroom (none are gitignored — `git check-ignore` exit 1 — they are simply not `git add`-ed yet):

| Untracked path | Size | Needed by the committed build? |
|---|---|---|
| `assets/` (contains `wake/`) | 1 dir | No — `core/assets.py` resolves wake/STT model paths to `REPO_ROOT/wake`, `REPO_ROOT/models`, and returns `None` gracefully when absent; all gates passed without it. Runtime-only, dev-tree state. |
| `tests/audio/test_wake_training.py` | 11 test functions | No — not part of committed test suite. **Coverage gap:** 11 tests exist only in the dev tree and were never exercised by this cleanroom run. |
| `tools/train_wake_model.py` | 718 lines | No — dev training tool, not imported by package or committed tests. |
| `tools/soak_wake_m5.py` | 108 lines | No — dev soak tool, not imported by package or committed tests. |

Additionally, 10 tracked files carry uncommitted modifications in the main tree (`.gitignore`, `pyproject.toml`, `uv.lock` (+1799 lines), `src/marshmellow/app.py`, `src/marshmellow/audio/activation.py`, `src/marshmellow/audio/capture.py`, the two corresponding test files, and two docs) — this is in-progress wake-training work. The cleanroom therefore verifies HEAD, not the dev tree's current working state.

Behavior diffs dev-tree vs clone:

- No gate failed in the clone, so there is no "passes only in the dev tree" divergence to report.
- Inverse caveat: the dev tree contains tests (11 wake-training tests) and source changes the cleanroom never ran; their health is unknown to this report. Gates were intentionally NOT run in the main tree (read-only constraint: pytest/ruff/mypy would write caches into the working tree).

## 6. Verdict

**CLEANROOM PASS** — clone, `uv sync`, and all three gates (pytest / ruff / mypy) succeeded from the committed tree alone with exit code 0. Nothing tracked-and-required is missing from git. The only findings are hygiene items: the four untracked paths above (notably `tests/audio/test_wake_training.py`, 11 tests invisible to a clean checkout) and ~2.1k lines of uncommitted wake-training work that a fresh checkout will not contain until committed.
