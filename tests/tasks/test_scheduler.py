"""Jittered scheduler — next_due computes the next poll instant, is_due the comparison.

Pinned jitter convention (M4 plan): jitter_s = (rng() * 2 - 1) * interval_s * 0.1,
so rng=0.0 → 0.9·interval and rng=1.0 → 1.1·interval. ``last=None`` (first
registration) schedules from now; a known ``last`` schedules from last, so an
outage longer than the interval is due immediately instead of silently skipping.
"""

from datetime import datetime, timedelta

import pytest

from marshmellow.tasks.scheduler import is_due, next_due

NOW = datetime(2026, 11, 6, 12, 0, 0)


def test_next_due_first_check_jitters_down_to_90_percent() -> None:
    assert next_due(600, now=NOW, last=None, rng=lambda: 0.0) == NOW + timedelta(seconds=540)


def test_next_due_first_check_jitters_up_to_110_percent() -> None:
    assert next_due(600, now=NOW, last=None, rng=lambda: 1.0) == NOW + timedelta(seconds=660)


def test_next_due_schedules_from_last_check_when_present() -> None:
    last = NOW - timedelta(seconds=120)
    assert next_due(600, now=NOW, last=last, rng=lambda: 0.5) == last + timedelta(seconds=600)


def test_next_due_overdue_last_check_is_due_immediately() -> None:
    last = NOW - timedelta(seconds=1200)  # outage: 2 intervals ago
    due = next_due(600, now=NOW, last=last, rng=lambda: 0.5)
    assert is_due(NOW, due)


def test_next_due_zero_jitter_fraction_yields_bare_interval() -> None:
    assert next_due(600, now=NOW, last=None, rng=lambda: 1.0, jitter_fraction=0.0) == (
        NOW + timedelta(seconds=600)
    )


def test_next_due_rejects_non_positive_interval_and_out_of_range_fraction() -> None:
    with pytest.raises(ValueError, match="interval"):
        next_due(0, now=NOW, last=None)
    with pytest.raises(ValueError, match="jitter"):
        next_due(600, now=NOW, last=None, jitter_fraction=-0.1)


def test_is_due_boundary_counts_exact_due_as_due() -> None:
    due = NOW + timedelta(seconds=540)
    assert not is_due(NOW, due)  # one tick before
    assert is_due(due, due)  # exactly at the boundary
    assert is_due(due + timedelta(seconds=1), due)
