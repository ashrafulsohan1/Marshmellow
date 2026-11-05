---
name: add-scenario
description: Author a YAML scenario fixture for the Marshmellow M3 scenario runner (scripted or fixture-app mode) — structure, app pinning, UIA assertion rules, and how to run it.
---

# Add a scenario

Author an end-to-end scenario for the M3 runner (`src/marshmellow/testing/scenario.py`)
and its test. Two modes:

- **`mode: scripted`** — fully headless. Perception replays canned
  `PerceptionSnapshot` dumps (JSON list, refs exactly `e1..eN` in walk order,
  UIA-only elements); the action layer is a recording double. Use this for
  planner-protocol coverage (traces, L3 confirmation, vision fallback).
- **`mode: fixture:<alias>`** — real desktop. The runner launches the app via
  `testing.fixtures.FixtureApp`, runs the REAL perception (UIA walk of the real
  window) and the REAL ActionLayer behind the real SafetyGate, driven by the
  same scripted fake provider. Use this to prove the generic protocol against
  real apps (the M3 DoD 8-app corpus lives in `tests/scenarios/fixture_*.yaml`).

Zero real LLM calls are possible in both modes: the only provider in the loop
is the in-process `ScenarioProvider` built from the YAML `script`.

## YAML structure (all keys pinned; unknown keys rejected)

```yaml
name: fixture-notepad            # unique, kebab-case
instruction: type hello into notepad   # the "spoken" user instruction
mode: fixture:notepad            # 'scripted' or 'fixture:<alias>'
launch: fixtures/local_page.html # fixture only: open this instead of the bare alias
snapshots: fixtures/page.json    # scripted only: canned PerceptionSnapshot dumps
pin:                             # fixture only: app state that MUST exist before acting
  role: Document
  name_contains: text editor
script:                          # the fake provider's ordered tool calls
  - tool: get_elements
    args: {}
  - tool: act
    args: {kind: type_text, target_ref: e1, payload: {text: hello}, rationale: ..., risk: L2}
  - tool: finish                 # ALWAYS its own entry (never bundled with actions)
    args: {ok: true, summary_to_speak: Typed hello.}
trace:                           # the EXECUTED steps in order
  - get_elements
  - act:type_text
  - finish
spoken_contains: [hello]         # substrings the final spoken summary must include
assert_elements:                 # fixture only: UIA-tree assertions after the run
  - role: Document
    name_contains: text editor
    value_contains: hello
```

Script directives that configure the runner instead of the planner:
`ask_user` (`args.answer` — scripted confirm answers) and `vision`
(`args.reply` — the fake provider's SoM answer; wiring vision also requires a
screenshot on every canned state).

Trace semantics: a step is recorded when it EXECUTED. `get_elements` /
`screenshot` / `finish` always execute; `act:<kind>` only when the request
reached the action layer AND succeeded — gate refusals and unknown/stale refs
are attempts, not executions, and stay off the trace. Author the trace from
what actually ran (`report.trace` is in the failure message).

## App pinning (fixture mode — non-negotiable)

1. **The fixture owns its window.** Every run snapshots the pre-launch window
   set and addresses only windows that appeared after it (SP1 rule — never
   "any window whose title matches"). Consequences:
   - Single-instance apps (Settings, Clock, the default browser) must be
     **closed before the run** — otherwise the launch navigates an existing
     window and the fixture fails cleanly with `ScenarioError`.
   - The scenario closes only its own window at the end (WM_CLOSE, then a
     force-kill of its own process if the app shows a save prompt).
2. **Pin state before acting.** `pin` waits (polled fresh UIA walks, generous
   timeout — never a fixed sleep) for one element before the planner starts:
   the editor for Notepad, the display for Calculator, the page's input for
   the browser. A scenario that acts before its app is ready is flaky by
   construction.
3. **`launch` pins the exact surface**: `ms-settings:about` (not the Settings
   home), a `file:///` URI for a browser page, `conhost.exe` where the
   default terminal delegates to Windows Terminal (probed: WT routes all UIA
   focus to its tab strip, so generic typed input cannot reach the shell;
   conhost's provider accepts it). A relative `launch` that names an existing
   file is resolved to a `file:///` URI against the YAML's directory.
4. **Foreground is part of pinning.** The runner focuses the fixture window
   before pinning (D6: act only on windows you own) — this also wakes lazy
   accessibility trees (Chromium exposes page content only after activation).

## UIA assertion rules (`assert_elements`)

- Assert **UIA tree state, never pixels/screenshots** (determinism rule).
- Entries match on the fixture window's **raw UIA walk** — every control, not
  just the interactable subset the planner sees — so read-only results and
  status text are assertable:
  - `role` — control type minus the `Control` suffix (Edit, Button, Document,
    Text, Hyperlink, ListItem, Window, ...); same convention as reader roles.
  - `name_contains` — case-insensitive substring; use `""` for "any".
  - `value_contains` — UIA **Value** pattern (edits, documents); for what the
    planner typed/saw, prefer the planner-visible element.
  - `text_contains` — UIA **TextPattern** document text (conhost's
    `'Text Area'`, terminal scrollbacks). Slower; only when the text exists
    nowhere else.
- Every entry must match at least one node; failed matches fail the scenario
  (`report.ok is False`), they do not raise.
- Prefer assertions that survive legitimate UI churn: role + distinctive name
  over exact names, content substrings over positions. Refs (`e1..eN`) are
  walk-order artifacts — never assert them, only use them in `script` acts,
  and only from the LATEST `get_elements` before each act.
- The runner also reports `element_count` — the fresh **interactable** count
  the planner's lens saw (the SP1 `<10` vision-fallback-threshold measure).

## Authoring loop

1. **Probe first.** Launch the app once and walk its tree (the reader's
   interactable listing + a raw walk for non-interactive text). Refs, roles
   and names you author must come from that probe on the target desktop —
   element names are locale- and build-specific.
2. Write the YAML next to its siblings in `tests/scenarios/` and one test per
   YAML in `tests/test_fixture_scenarios.py` — marked
   `@pytest.mark.scenario @pytest.mark.manual` (+ `skipif` non-Windows).
   `manual` keeps the default gate free of real-app launches; run the suite
   explicitly with `uv run pytest -m scenario -k <name>`.
3. Iterate on failures using `report.trace` / `report.spoken` (in the assert
   message) — then close stray fixture windows before re-running.
4. Determinism: no fixed sleeps (the runner's waits poll real UIA state with
   generous timeouts); no live LLM calls; each run launches and closes its
   own fixture app.
