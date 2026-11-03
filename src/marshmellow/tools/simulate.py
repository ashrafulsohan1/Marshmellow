"""Run one spoken-instruction fixture through the M1 loop (no mic, no UI).

Usage: uv run python -m marshmellow.tools.simulate "open notepad"

Headless by design — this is the deterministic dev entry point (project skill
`simulate-spoken-instruction` once authored). With the real WindowsPack, the
instruction really opens the app.
"""

from __future__ import annotations

import argparse
import asyncio

from marshmellow.app import App
from marshmellow.core.config import load_settings
from marshmellow.core.contracts import TaskEvent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instruction", help="the simulated spoken instruction")
    args = parser.parse_args()

    app = App(load_settings(), headless=True)
    app.bus.subscribe(
        TaskEvent,
        lambda ev: print(f"[task {ev.task_id}] {ev.type}: {ev.payload}", flush=True),
    )

    async def run() -> None:
        app.start()
        handle = await app.submit_text(args.instruction)
        result = handle.result
        print(f"spoken: {result.summary_to_speak if result else '<no result>'}")

    try:
        asyncio.run(run())
    finally:
        app.stop()


if __name__ == "__main__":
    main()
