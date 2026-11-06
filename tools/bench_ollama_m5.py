"""M5 local-LLM bench (Task 1 Step 5) — the SP5-style probe, LOCAL leg.

Measures, per candidate Ollama model on THIS machine's daemon:

1. planner-tool-turn  — one /api/chat round trip carrying the planner's tool
                        spec set ("open Notepad" + 3 tools). Latency per turn,
                        tool-call rate, and tool-call JSON validity (arguments
                        parse to an object carrying the required key).
2. vision-SoM-turn    — one full VisionPerceiver.perceive() over a synthetic
                        PNG (capture/foreground seams injected): SoM annotate +
                        provider call + defensive parse. Needs a vision-capable
                        model; models without one are recorded as failing (the
                        Ollama daemon refuses images), which DISQUALIFIES a
                        model from serving the D5 fallback.

Writes docs/plan/spikes/data/m5_ollama.json and docs/plan/spikes/M5-local-llm.md
naming the recommended local default (valid tool calls first, then latency).

NOT part of the test gates — needs the local Ollama daemon (`ollama serve`).
Run: uv run python tools/bench_ollama_m5.py --models <comma,separated> [--runs 5]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw

from marshmellow.core.contracts import LLMMessage, LLMRequest, PerceptionHint, Rect
from marshmellow.llm.ollama import DEFAULT_OLLAMA_URL, OllamaProvider
from marshmellow.perception.vision import VisionPerceiver

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "plan" / "spikes" / "data"
REPORT = ROOT / "docs" / "plan" / "spikes" / "M5-local-llm.md"

PLANNER_SYSTEM = (
    "You are Marshmellow, a desktop agent. Pick exactly ONE tool call that "
    "advances the user's instruction."
)
PLANNER_USER = "Open Notepad for me."
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application by name.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "app name"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for milliseconds.",
            "parameters": {
                "type": "object",
                "properties": {"ms": {"type": "integer"}},
                "required": ["ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Report the final result to the user.",
            "parameters": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
        },
    },
]

WINDOW_RECT = Rect(x=100, y=80, w=1024, h=640)


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _synthetic_png(w: int = 1024, h: int = 640) -> bytes:
    """A fake app window: five buttons + five panels for the SoM grid to label."""
    img = Image.new("RGB", (w, h), (30, 33, 40))
    draw = ImageDraw.Draw(img)
    for i in range(5):
        draw.rectangle([40 + i * 180, 60, 200 + i * 180, 110], fill=(70, 130, 180))
        draw.rectangle([40 + i * 180, 180, 200 + i * 180, 400], fill=(90, 90, 110))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _installed(url: str) -> dict[str, list[str]]:
    """name -> capability list from the daemon (raises when it is down)."""
    response = httpx.Client(timeout=30).get(f"{url}/api/tags")
    response.raise_for_status()
    return {
        # /api/tags puts "capabilities" on the model entry (details carries size/quant).
        m["name"]: list(m.get("capabilities") or m.get("details", {}).get("capabilities") or [])
        for m in response.json().get("models", [])
    }


async def _bench_planner(provider: OllamaProvider, runs: int) -> dict[str, Any]:
    req = LLMRequest(
        messages=[
            LLMMessage(role="system", content=PLANNER_SYSTEM),
            LLMMessage(role="user", content=PLANNER_USER),
        ],
        tools=TOOLS,
        max_tokens=256,
    )
    latencies: list[float] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for _ in range(runs):
        start = time.perf_counter()
        try:
            response = await provider.complete(req)
        except Exception as exc:
            errors.append(repr(exc))
            continue
        latencies.append((time.perf_counter() - start) * 1000.0)
        call = response.tool_calls[0] if response.tool_calls else None
        calls.append(
            {
                "name": call.name if call else None,
                "argument_keys": sorted(call.arguments) if call else [],
                "valid": bool(
                    call
                    and call.name == "open_app"
                    and isinstance(call.arguments.get("target"), str)
                ),
            }
        )
    valid = [c["valid"] for c in calls]
    return {
        "latency_ms": [round(v, 1) for v in latencies],
        "median_ms": round(statistics.median(latencies), 1) if latencies else None,
        "tool_call_rate": round(sum(1 for c in calls if c["name"]) / runs, 2),
        "arg_valid_rate": round(sum(valid) / runs, 2) if valid else 0.0,
        "calls": calls,
        "errors": errors,
    }


async def _bench_vision(provider: OllamaProvider, runs: int, png: bytes) -> dict[str, Any]:
    perceiver = VisionPerceiver(
        provider=provider,
        capture=lambda rect: png,
        foreground_rect=lambda: WINDOW_RECT,
    )
    hint = PerceptionHint(query="buttons")
    latencies: list[float] = []
    counts: list[int] = []
    error: str | None = None
    for _ in range(runs):
        start = time.perf_counter()
        try:
            elements = perceiver.perceive(hint)
        except Exception as exc:
            error = repr(exc)  # daemon refuses images on non-vision models, timeouts, ...
            break
        latencies.append((time.perf_counter() - start) * 1000.0)
        counts.append(len(elements))
    return {
        "ok": bool(latencies) and not error,
        "latency_ms": [round(v, 1) for v in latencies],
        "median_ms": round(statistics.median(latencies), 1) if latencies else None,
        "elements": counts,
        "error": error,
    }


def _recommend(models: dict[str, Any]) -> str | None:
    """Valid tool calls first, then latency — the local-default criterion."""
    eligible = [
        name
        for name, r in models.items()
        if r["planner"]["median_ms"] is not None
        and r["planner"]["tool_call_rate"] >= 0.5
        and r["planner"]["arg_valid_rate"] >= 0.5
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda n: models[n]["planner"]["median_ms"])


def _write_report(payload: dict[str, Any]) -> None:
    lines = [
        "# M5 local LLM probe (SP5-style, LOCAL leg) — Ollama on this machine",
        "",
        f"Generated: {payload['generated']} · daemon `{payload['url']}` · "
        f"{payload['runs']} runs per leg (after one untimed warmup) · git `{payload['git']}`",
        "",
        "| model | planner median (ms) | tool-call rate | arg-valid rate | vision turn | vision median (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for name, r in payload["models"].items():
        planner, vision = r["planner"], r["vision"]
        lines.append(
            f"| `{name}` | {planner['median_ms']} | {planner['tool_call_rate']} "
            f"| {planner['arg_valid_rate']} | {'ok' if vision['ok'] else 'FAILED'} "
            f"| {vision['median_ms']} |"
        )
    rec = payload["recommended_default"]
    lines += ["", "## Recommendation", ""]
    if rec is None:
        lines.append(
            "**No local default.** No measured model produced reliably valid tool "
            "calls — do not wire a local planner default from this leg. For the D5 "
            "vision fallback a vision-capable model is required on top "
            "(e.g. `ollama pull qwen2.5vl:3b`), then re-run this bench."
        )
    else:
        r = payload["models"][rec]
        lines.append(f"**Recommended local default: `{rec}`** "
                     f"(median planner turn {r['planner']['median_ms']} ms, "
                     f"arg-valid rate {r['planner']['arg_valid_rate']}, "
                     f"vision turn {'available' if r['vision']['ok'] else 'NOT available — cannot serve D5'}).")
        if not r["vision"]["ok"]:
            lines.append(
                "It plans locally, but the D5 SoM fallback has no local model on this "
                "machine yet — pull a vision-capable candidate and re-run this bench."
            )
    lines += [
        "",
        "## Raw data",
        "",
        "`data/m5_ollama.json` (per-run latencies, tool-call arguments, errors).",
        "",
        "Bench: `tools/bench_ollama_m5.py` — not part of the test gates; needs `ollama serve`.",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", required=True, help="comma-separated model names")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--url", default=DEFAULT_OLLAMA_URL)
    args = parser.parse_args()
    requested = [m.strip() for m in args.models.split(",") if m.strip()]

    try:
        installed = _installed(args.url)
    except Exception as exc:
        print(f"Ollama daemon at {args.url} is unreachable ({exc}). Start it: `ollama serve`")
        return 1

    png = _synthetic_png()
    models: dict[str, Any] = {}
    for name in requested:
        if name not in installed:
            print(f"SKIP {name}: not pulled on this daemon (`ollama pull {name}`)")
            continue
        print(f"== {name} (capabilities: {installed[name]})")
        provider = OllamaProvider(base_url=args.url, model=name)
        try:
            await provider.complete(
                LLMRequest(messages=[LLMMessage(role="user", content="warmup: reply ok")])
            )
        except Exception as exc:
            print(f"  warmup failed: {exc!r} — model skipped")
            models[name] = {
                "capabilities": installed[name],
                "planner": {"error": repr(exc)},
                "vision": {"ok": False, "error": repr(exc)},
            }
            continue
        planner = await _bench_planner(provider, args.runs)
        print(f"  planner: median {planner['median_ms']} ms, "
              f"tool-call {planner['tool_call_rate']}, valid {planner['arg_valid_rate']}")
        vision = await _bench_vision(provider, args.runs, png)
        print(f"  vision: {'ok' if vision['ok'] else 'FAILED'}"
              f"{'' if vision['ok'] else ' — ' + str(vision['error'])[:160]}")
        models[name] = {"capabilities": installed[name], "planner": planner, "vision": vision}

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": _git_head(),
        "url": args.url,
        "runs": args.runs,
        "models": models,
        "recommended_default": _recommend(models),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "m5_ollama.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    _write_report(payload)
    print(f"wrote {DATA / 'm5_ollama.json'} and {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
