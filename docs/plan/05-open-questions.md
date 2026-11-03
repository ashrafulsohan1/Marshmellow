# Marshmellow — Open Questions Register

Every unresolved question lives here with options + a recommendation. Nothing critical is silently assumed
(brief §6). Decided items move to [PLAN.md §1](../../PLAN.md#1-decisions-locked-with-user-2026-11-04).

**Decided this session (traceability):** D1 Python core · D2 activation = user-selectable (wake word / hotkey / both) ·
D3 autostart = user setting · D4 cloud-first LLM · D5 hybrid UIA+vision perception · D6 foreground-default mode policy.

| # | Question | Options | Recommendation | Decided by |
|---|---|---|---|---|
| OQ-1 | Wake-word phrase & engine | **Resolved by SP7**: openWakeWord self-trained model on **"hey marshmellow"** — synthetic-TTS pipeline, <1 h training + 1–2 d false-accept tuning; Porcupine is dead (no free tier since 2026-06-30); must ship **self-trained** models only (bundled openWakeWord models are CC BY-NC-SA 4.0) | engine + approach decided; model training happens during M2 prep | user: confirm the exact phrase ("hey marshmellow"?) |
| OQ-2 | Which cloud provider + API key is the first-party default? | (a) OpenAI-compat endpoint w/ GLM/Z.AI · (b) OpenAI · (c) Anthropic | (a) — OpenAI-compatible base URL keeps it provider-agnostic; user supplies key in settings | **user** (which key) |
| OQ-3 | Mascot art & animation pipeline | (a) PNG sprite sequence / APNG (zero deps) · (b) Lottie (small dep, slick) · (c) live-drawn vector (QPainter) | (a) for v1 + Lottie later; art source: user provides or simple commissioned/pixel style | user (style) |
| OQ-4 | STT default size | **Resolved by SP2**: `base/int8` — only config inside the latency budget (p95 1.21 s, RTF 0.31); `small` disqualified (RTF 1.87, p95 14.3 s); `distil-small.en` best WER but p95 3.8 s → accuracy-upgrade candidate, not default | closed | M0-SP2 |
| OQ-5 | Language scope for v1 | (a) English only · (b) EN + user's other language from M2 | (a) English v1 (Whisper transcribes multilingual anyway; planner prompts EN) | **user** |
| OQ-6 | Autostart mechanism (D3 setting) | (a) Task Scheduler on-logon · (b) shell:startup shortcut · (c) registry Run key | (a) Task Scheduler — robust, toggleable, no UAC | M5 (impl detail) |
| OQ-7 | Optional online TTS voice (edge-tts) as extra | include opt-in / omit v1 | omit unless user wants it; default stays piper offline | user (optional) |
| OQ-8 | Multi-monitor policy | (a) foreground window's monitor (v1) · (b) full multi-monitor awareness | (a) for v1; (b) post-v1 | plan (revisit M5) |
| OQ-9 | Distribution form | (a) pyinstaller single-exe · (b) MSIX · (c) uv-managed env + launcher | (a) for dogfood; decide properly in M5 | M5 |
| OQ-10 | L3 confirmation phrasing & fallback UX (voice yes/no loop vs tray dialog) | (a) voice loop w/ dialog fallback · (b) dialog-first | (a) voice-first, dialog when mic ambiguous | user (feel) |
| OQ-11 | Should background monitors poll via UIA reads when APIs are absent (screen-free but reads pixels/tree)? | allow quiet reads / forbid | allow UIA reads of background windows (no input synthesis), forbid screenshots of private windows | user (privacy) |
| OQ-12 | Main-UI look & feel (theme, branding) | light/dark/marshmallow-palette | soft marshmallow palette, dark default; mock in M1, polish M5 | user (taste) |

**Waiting on the user's broader requirements** (per brief §6): detailed product requirements arrive in later messages —
they may add features per milestone, reshape M2+ scope, and close OQ-2/3/5/7/10/12 directly.
