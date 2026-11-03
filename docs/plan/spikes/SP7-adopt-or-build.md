# SP7 — Adopt / Build: library license & maintenance verification

**Date:** 2026-11-04 · **Status:** complete · Feeds: `docs/plan/04-tooling.md` §1/§2 (M0-SP7 license re-verification)

**Method:** every license claim below was checked against a primary source on 2026-11-04: the PyPI package metadata (license field + classifiers, via `pypi.org/pypi/<name>/json`) and/or the LICENSE file in the GitHub repo (fetched raw). Version numbers and dates come from the same metadata. Community/reliability claims link to the primary thread or issue.

**TL;DR verdict table**

| # | Component | Verified license | Latest release | Verdict |
|---|---|---|---|---|
| 1 | `uiautomation` | **Apache-2.0** (not MIT/BSD) | 2.0.29 · 2025-08-05 | AMEND (fix license) |
| 2 | `pywinauto` | **BSD-3-Clause** | 0.6.9 · 2025-01-06 (1.0.0b1 · 2026-10-15) | CONFIRM-ADOPT |
| 3 | `faster-whisper` | **MIT** | 1.2.1 · 2025-10-31 | CONFIRM-ADOPT |
| 4 | `openwakeword` | **Apache-2.0 code; bundled pretrained models CC BY-NC-SA 4.0** | 0.6.0 · 2024-02-11 | CONFIRM-ADOPT + AMEND (train own model) |
| 5 | Picovoice `Porcupine` | Proprietary (SDK code OSS, engines/models proprietary); **free tier ended 2026-06-30** | — (Console) | AVOID (for a free product) |
| 6 | `piper` / `piper-tts` | **GPL-3.0-or-later** (new line); 1.2.0 was the last MIT | 1.8.0 · 2026-09-04 | AMEND (subprocess + license note) |
| 7 | `pipecat` | **BSD-2-Clause** | 1.15.0 · 2026-10-29 | CONFIRM (patterns only) |
| 8 | Agent S (`simular-ai/Agent-S`) | **Apache-2.0** (not "research license") | Agent S4 announced 2026-10-20 | AMEND (license note) |
| 9 | OS-World (`xlang-ai/OSWorld`) | **Apache-2.0** | OSWorld-Verified · 2025-07-28 | CONFIRM-ADOPT (corpus shapes) |
| 10 | `edge-tts` | **LGPL-3.0** (one file MIT) | 7.3.1 · 2026-10-13 | CONFIRM (opt-in only; pin ≥7.3.1) |
| 11 | `mss` / `PySide6` / `pyautogui` / `silero-vad` | MIT / LGPL-3.0 / BSD / MIT | see §11 | CONFIRM ×3, AMEND pyautogui |

---

## 1. `uiautomation` (yinkaisheng) — license resolved

- **License: Apache-2.0.** LICENSE file in repo reads "Apache License, Version 2.0, January 2004" with "Copyright Yinkaisheng": <https://github.com/yinkaisheng/Python-UIAutomation-for-Windows/blob/master/LICENSE>. PyPI metadata agrees ("Apache 2.0" license field): <https://pypi.org/project/uiautomation/>. Our notes ("≈MIT/BSD-3") were wrong.
- **Latest:** 2.0.29, released 2025-08-05 (PyPI). 2.x requires Python 3 + `comtypes`; supports XP SP3 → Windows 11.
- **Maintenance:** single-maintainer project, ~3.6k stars, 495 commits, no formal GitHub releases (PyPI is the channel); 152 open issues. Slow but alive, and effectively the standard Python UIA wrapper. Windows 11-era controls mostly reached via `comtypes` passthrough.
- **Red flags:** bus factor 1; issue backlog large; no CI-driven releases.
- **Verdict: AMEND** — keep as adopt, but change the §1 "License" cell for the UIA row to **Apache-2.0** (row currently says "verify in SP7 (≈MIT/BSD-3)"). Apache-2.0 is fine for a closed app (keep NOTICE/attribution file).

## 2. `pywinauto` — BSD-3 confirmed, project revived

- **License: BSD-3-Clause.** PyPI license field "BSD 3-clause" + classifier "License :: OSI Approved :: BSD License": <https://pypi.org/pypi/pywinauto/json>.
- **Latest:** stable **0.6.9** (2025-01-06); a **1.0.0b1** pre-release was published **2026-10-15** — "Linux, macOS, WPF and Qt/QML backends and refactoring", drops Python 2.7, default Windows backend becomes `uia`: <https://github.com/pywinauto/pywinauto/releases>.
- **Maintenance:** long-quiet project with fresh 2026 activity (1.0.0b1). 0.6.9 remains the tagged "Latest" stable.
- **Red flags:** 1.0.0b1 is explicitly beta-quality (new backends, heavy refactor); Windows backend in 1.0 changes defaults — don't jump early.
- **Verdict: CONFIRM-ADOPT** — pin `pywinauto==0.6.9` for M0–M4; revisit 1.0.0 when stable.

## 3. `faster-whisper` — MIT, current, CPU config

- **License: MIT** (license field + OSI classifier): <https://pypi.org/project/faster-whisper/>. Repo: <https://github.com/SYSTRAN/faster-whisper>.
- **Latest:** **1.2.1**, released **2025-10-31** (PyPI JSON). Deps: `ctranslate2>=4.0,<5`, `onnxruntime`, `av`, `tokenizers`; `requires_python >=3.9`. Classifiers list only 3.9–3.11 — classifiers lag; **add a 3.12 import/decode smoke test to M0** to be safe.
- **Recommended low-latency CPU config** (from the README benchmarks, small model on i7-12700K / 8 threads / 13 min audio): `WhisperModel(size, device="cpu", compute_type="int8")` → 1m42s (~7.6× realtime) at beam 5; `BatchedInferencePipeline(batch_size=8)` → 51s; `vad_filter=True` (Silero VAD, on by default in batched mode): <https://github.com/SYSTRAN/faster-whisper#benchmark>. For our interactive loop: start with `small` (or `base.en`) + `compute_type="int8"`, `beam_size=1`, `vad_filter=True`, no word timestamps; target a first-partial budget of ~1–2 s of audio per decode. Measure in M0; the README numbers are for offline files, not streaming.
- **Verdict: CONFIRM-ADOPT.**

## 4. `openwakeword` — custom "hey marshmellow" is feasible; watch the model license

- **License: split.** Code (inference + training tooling) is **Apache-2.0**; the **bundled pretrained models are CC BY-NC-SA 4.0** (non-commercial) because their training data had unknown/restrictive licensing. Both stated on PyPI and in the repo README: <https://pypi.org/project/openwakeword/>, <https://github.com/dscripka/openWakeWord>. The frozen feature-extraction backbones used for training (melspectrogram + a Google speech-embedding reimplementation) are Apache-2.0.
- **Latest:** 0.6.0, **2024-02-11** — stale in version terms (2.5+ years) but the API is stable, models download via `openwakeword.utils.download_models()`, and inference runs on onnxruntime/tflite on CPU (ms-scale per 80 ms chunk on a modern core). Active Discussions; issues tracked but slow-moving.
- **Custom phrase "hey marshmellow": supported and practical.** Pipeline: (1) generate ~1,000s of synthetic clips of the phrase with open-source TTS — helper repo `dscripka/synthetic_speech_dataset_generation` (Apache-2.0 code; bundled WaveGlow/VITS models carry their own licenses): <https://github.com/dscripka/synthetic_speech_dataset_generation>; (2) provide negative audio (the repo documents that bundled models used ~30k h of negative speech/noise/music); (3) train in a Google Colab notebook pitched at non-experts — "in <1 hour" with no dev experience, or the fuller `automatic_model_training.ipynb` for quality: notebooks linked from the README (<https://github.com/dscripka/openWakeWord>). Output is a `.tflite`/`.onnx` you ship yourself — and because you trained it on synthetic data over Apache-2.0 backbones, the non-commercial restriction on the *bundled* models does not attach to your custom model (their model restriction stems from their training data, not the pipeline).
- **Practical feasibility for a non-ML-expert in 2026:** high for v1 — it's data generation + one notebook, no ML theory needed. Budget for 1–2 days of false-accept tuning: train 2–3 model variants (different TTS voices/phrase renditions), threshold-tune against your room noise, and optionally train a per-user verifier (also documented in the repo). Community corroboration: after Porcupine's free-tier shutdown, Home Assistant users converged on exactly this route (custom openWakeWord models trained with Piper TTS), June 2026: <https://community.home-assistant.io/t/porcupine-free-tier-shutdown-alternatives-for-home-assistant-voice-users/1012382>.
- **Red flags:** single maintainer, slow releases; false-activation tuning is real work; don't ship any of their pretrained models in a commercial/monetized build (irrelevant if we only ship our own model + the Apache backbones' *code*).
- **Verdict: CONFIRM-ADOPT + AMEND** — §1 row stays, but add: "(a) code Apache-2.0, bundled pretrained models CC BY-NC-SA — we ship only self-trained models; (b) OQ-1 is answered: train 'hey marshmellow' via the synthetic-data + Colab pipeline; schedule it as an M0 spike."

## 5. Picovoice `Porcupine` — free tier is gone (2026)

- **License: proprietary.** "Picovoice on-device AI SDKs, voice recorders, and benchmarks are open-source. Picovoice models and inference engines are proprietary" — official FAQ: <https://picovoice.ai/docs/faq/general>. On-device, AccessKey-gated: <https://picovoice.ai/docs/porcupine/>.
- **Current free-tier terms (Nov 2026):** the FAQ states there are **"no dedicated free or paid plans for personal or non-commercial use"**; the Free Trial is a **one-time offer for enterprise developers** ("contact sales during your trial to avoid service interruptions"). Picovoice confirmed **Free-Tier AccessKeys stopped working after 2026-06-30** (Home Assistant community thread, June 2026: <https://community.home-assistant.io/t/fyi-picovoice-confirmed-free-tier-accesskeys-will-stop-working-after-june-30-2026/1012744>). Reported commercial pricing from the HA discussion: ~$899/yr entry tier, ~$6k professional — not officially confirmed here, treat as anecdote.
- **Custom phrase support:** yes — Picovoice Console trains a `.ppn` for an arbitrary phrase in seconds (transfer learning), but under the paid/licensed terms above: <https://picovoice.ai/docs/porcupine/>.
- **Consequence:** a free Marshmellow cannot legally/practically ship Porcupine at all in Nov 2026 — not even as a fallback. This makes openWakeWord self-trained models load-bearing (see §4) with no open fallback of equal quality for arbitrary phrases (alternatives = microWakeWord-class or a second openWakeWord variant).
- **Verdict: AVOID** for v1 (any build that ships to other people). Remove it as the §1 fallback in `04-tooling.md`; the fallback becomes "train additional openWakeWord variants / add phrase variants". Revisit only if the product ever takes paid licensing.

## 6. `piper` / `piper-tts` — alive but RELICENSED to GPL-3.0; run as subprocess

- **Project health:** the original `rhasspy/piper` repo was **archived 2025-10-06** (read-only) and development moved to the Open Home Foundation fork **`OHF-Voice/piper1-gpl`**: <https://github.com/rhasspy/piper>, <https://github.com/OHF-Voice/piper1-gpl>. The fork is active (328 commits, 101 open issues, 51 PRs; OHF is recruiting maintainers).
- **License: GPL-3.0-or-later** for the current line. PyPI `piper-tts` **1.8.0** (2026-09-04) declares `"License: GPL-3.0-or-later"` with a `COPYING` file: <https://pypi.org/pypi/piper-tts/json>. The repo carries GPL-3.0 (`COPYING`). The **last MIT release was 1.2.0** (2023-08-17, license field "MIT"): <https://pypi.org/pypi/piper-tts/1.2.0/json>. (Note: piper has always embedded `espeak-ng`, itself GPL-3.0.)
- **Voices:** download from HuggingFace `rhasspy/piper-voices` (one `.onnx` + one `.onnx.json` per voice): <https://huggingface.co/rhasspy/piper-voices>. The fork's voice doc (`docs/VOICES.md`) uses **`en_US-lessac-medium`** as its en_US example — that remains the safe default US voice recommendation; sample page: <https://rhasspy.github.io/piper-samples/>.
- **Impact for a closed app:** importing/GPL-linking piper-tts inside our proprietary distribution would impose GPL-3 obligations on the distribution. The standard, widely-used pattern is to run **piper as a separate subprocess** (stdin text → stdout WAV), which keeps the works separate ("mere aggregation"); this is common practice (Home Assistant does the same) though not formal legal advice. Alternative escape hatch: pin MIT `piper-tts==1.2.0` (stale 2023, no 3.12-era wheels guaranteed).
- **Verdict: AMEND** — keep piper as the TTS pick, but record: current line is **GPL-3.0-or-later**, adopt via **subprocess isolation** (`piper-tts>=1.8.0` as an external engine process, not an in-process import), voices from `rhasspy/piper-voices`, default voice `en_US-lessac-medium`, license cell changes from "MIT" to "GPL-3.0 (subprocess) / MIT (1.2.0 legacy)".

## 7. `pipecat` — BSD-2 confirmed; patterns, not the framework

- **License: BSD-2-Clause** (SPDX license expression in PyPI metadata), latest **pipecat-ai 1.15.0** (2026-10-29), Production/Stable, actively maintained by the Pipecat org: <https://pypi.org/project/pipecat-ai/>.
- **Framework vs patterns (one paragraph):** pipecat's value is its transport/pipeline/frame abstraction and its catalog of cloud-service adapters (Daily/WebRTC, Twilio, hosted LLM/TTS/STT) — machinery aimed at multi-service, networked voice agents. Marshmellow is a single-process asyncio app whose pipeline is already fully local (silero-vad → openWakeWord → faster-whisper → piper) with a hand-rolled EventBus and hard latency budgets; importing pipecat would add a second pipeline abstraction to fight, a large dependency surface, and zero components we don't already have. Its BSD-2 license makes borrowing *specific implementations* (barge-in/echo handling, VAD-to-STT handoff, turn-taking state machines) legal with attribution. **Verdict: CONFIRM** — the §2 posture ("pattern adoption only") is correct; keep.

## 8. Agent S (`simular-ai/Agent-S`) — Apache-2.0, not a research license

- **License: Apache-2.0** (repo license + badge + LICENSE file): <https://github.com/simular-ai/Agent-S>. Our §2 note ("research license, AC by default") is **wrong for the code**.
- **Status:** very active — **Agent S4** announced 2026-10-20 ("Evolving Agent Interfaces", SOTA on OSWorld-V2 with Qwen3.8-27B); prior versions (S3/S2) in `gui_agents`; published as the `gui-agents` PyPI package. **Caveat:** the repo licenses the *code*; the **model weights** it downloads (GUI grounding etc.) have their own terms (documented in the repo's `models.md`, not re-verified here) — we don't need them.
- **Safely adoptable:** all of it is now on the table, not just "ideas" — the ACI (Agent-Computer Interface) action-space design, Set-of-Marks perception prompting, and the act→verify→replan loop are Apache-2.0 code and docs we can adapt with attribution (keep the NOTICE file). Still no runtime dependency — we take patterns + selected code, not the agent runtime or any model weights.
- **Verdict: AMEND** — §2 bullet: change "(research license, AC by default)" to "Apache-2.0 code; patterns and code excerpts adoptable with attribution; no weights, no runtime dep".

## 9. OS-World (`xlang-ai/OSWorld`) — Apache-2.0

- **License: Apache-2.0** (repo license + badge): <https://github.com/xlang-ai/OSWorld>. NeurIPS 2024 benchmark evaluating multimodal agents on real-computer tasks; notable recent update "OSWorld-Verified" (2025-07-28).
- **Verdict: CONFIRM-ADOPT** for what we said we'd take: scenario/benchmark *shapes* (task phrasing, setup/eval structure) for our UI-test corpus. Since it's Apache-2.0, even copying scenario YAML structure with attribution is fine.

## 10. `edge-tts` — alive, LGPL-3.0, and it broke in Oct 2026 (then was fixed)

- **License: LGPL-3.0** for all files except `src/edge_tts/srt_composer.py` (MIT) — repo LICENSE, verified raw: <https://github.com/rany2/edge-tts/blob/master/LICENSE>; PyPI classifier agrees ("GNU Lesser General Public License v3 (LGPLv3)"): <https://pypi.org/project/edge-tts/>. (Relicensed from the earlier GPL-3.0.) Fine for an *optional* extra in a closed app; even simpler, don't bundle it — let the user `pip install edge-tts` as an opt-in extra.
- **Latest:** **7.3.1**, released **2026-10-13** — actively maintained.
- **2026 endpoint risk — confirmed real:** on **~2026-10-10 Microsoft began accepting only Sec-MS-GEC v2 tokens** on the free Edge read-aloud endpoint; every client ≤7.2.8 got hard **403s** on synthesis (not rate-limiting — a protocol/token change): <https://github.com/rany2/edge-tts/issues/490>, corroborated by <https://github.com/hasscc/hass-edge-tts/issues/78>. Fixed in 7.3.x (v2 token, <https://github.com/rany2/edge-tts/issues/510>), with **TTS latency up ~50%** after the fix. This is the second such breakage wave (the 2024 Sec-MS-GEC v1 saga: <https://github.com/rany2/edge-tts/issues/290>).
- **Verdict: CONFIRM** the §2 posture — opt-in extra voice only, never default; if adopted, floor-pin `edge-tts>=7.3.1` and treat outages as expected lifecycle events of an unofficial endpoint. The v1 posture survives contact with 2026 reality.

## 11. Quick confirmations

- **`mss` — MIT, CONFIRM.** 10.2.0 (2026-04-23), license field + classifier MIT, repo `BoboTiG/python-mss`, Python ≥3.9: <https://pypi.org/project/mss/>.
- **`PySide6` — LGPL-3.0, CONFIRM.** 6.12.0 (2026-10-08), license field `"LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"` (+ Qt commercial dual-license), Python ≥3.10,<3.16: <https://pypi.org/pypi/PySide6/json>. One line for the closed app: LGPL-3.0 permits dynamic-linking Qt in a proprietary app provided we ship the LGPL license text, keep Qt an unmodified, user-replaceable library (no static linking / no patched Qt), and mark any Qt modifications as such.
- **`pyautogui` — BSD, but stale: AMEND.** 0.9.54 released **2023-05-24** (3.5 years without a release), license field "BSD", repo `asweigart/pyautogui`: <https://pypi.org/pypi/PyAutoGUI/json>. Known Windows DPI/multi-monitor quirks have never been cut into a release. Prefer **raw SendInput via ctypes** as the primary input-synthesis layer (as the §1 fallback already hints); keep pyautogui only as a dev/test convenience, not a product dependency.
- **`silero-vad` — MIT, CONFIRM.** 6.2.3 (2026-09-23), PyPI classifier MIT, repo MIT ("Published under permissive license (MIT)... no telemetry, no keys, no registration"): <https://pypi.org/pypi/silero-vad/json>, <https://github.com/snakers4/silero-vad>. Minor flag: the repo's README badge/license presentation is inconsistent in places, but the LICENSE file and PyPI classifier are MIT; active releases into 2026.

---

## Recommended changes to 04-tooling.md

Six amendments fall out of this spike. (1) §1 UIA row: set `uiautomation`'s license to **Apache-2.0** and pin `pywinauto==0.6.9` (BSD-3) with a note that 1.0.0b1 (2026-10) exists but is beta. (2) §1 TTS row: piper's license cell becomes **"GPL-3.0-or-later (subprocess) / MIT (1.2.0 legacy)"** — rhasspy/piper is archived (2025-10-06) and the maintained line is `OHF-Voice/piper1-gpl` (`piper-tts>=1.8.0`), consumed as an **isolated subprocess**, voices from `rhasspy/piper-voices`, default voice `en_US-lessac-medium`. (3) §1 wake-word row: openWakeWord stays the pick, with the note that bundled pretrained models are CC BY-NC-SA (we ship only self-trained models) and OQ-1 is answered — training "hey marshmellow" via the synthetic-TTS-data + Colab pipeline is realistic for a non-expert (~1–2 days incl. false-accept tuning), so make it an M0 spike. (4) §1/§2 Porcupine: mark **AVOID** — Picovoice's free tier died 2026-06-30 and its FAQ now states there are no free/personal plans at all, so a free product cannot ship it; the wake-word fallback becomes "additional openWakeWord variants". (5) §2 Agent S: correct the license to Apache-2.0 (patterns *and* code excerpts adoptable with attribution; no weights, no runtime dep). (6) §1 input row: make raw SendInput (ctypes) primary and pyautogui optional/dev-only (last release 2023-05). Additionally: keep edge-tts strictly opt-in with `edge-tts>=7.3.1` (Oct 2026 v2-token 403 wave confirmed the risk; fixed but ~50% slower), keep pipecat as pattern-adoption-only (BSD-2), and add a Python 3.12 smoke test for faster-whisper 1.2.1 (classifiers list only ≤3.11) plus the M0 low-latency baseline: `small` int8 CPU, `beam_size=1`, `vad_filter=True`.
