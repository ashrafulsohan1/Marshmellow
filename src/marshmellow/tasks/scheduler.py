"""Jittered scheduler for monitor polls (M4 Task 1).

Pinned jitter convention (M4 plan): ``jitter_s = (rng() * 2 - 1) * interval_s *
0.1`` — uniform ±10 %. ``rng`` is injected so tests are deterministic. When
``last`` is None (first registration) the next due instant anchors on *now*;
otherwise it anchors on *last*, so an outage longer than the interval is due
immediately on the next tick instead of silently skipping. ``is_due`` is a
plain comparison whose boundary (``now == due``) counts as due.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta


def next_due(
    interval_s: int,
    *,
    now: datetime,
    last: datetime | None,
    rng: Callable[[], float] = random.random,
    jitter_fraction: float = 0.1,
) -> datetime:
    """Next poll instant: (last or now) + interval ± jitter.

    Raises ValueError when ``interval_s <= 0`` or ``jitter_fraction`` is
    outside ``[0, 1]``.
    """
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    if not 0.0 <= jitter_fraction <= 1.0:
        raise ValueError(f"jitter_fraction must be within [0, 1], got {jitter_fraction}")
    jitter_s = (rng() * 2 - 1) * interval_s * jitter_fraction
    anchor = last if last is not None else now
    return anchor + timedelta(seconds=interval_s + jitter_s)


def is_due(now: datetime, due: datetime) -> bool:
    """True when the poll instant has arrived; the exact boundary counts as due."""
    return now >= due
