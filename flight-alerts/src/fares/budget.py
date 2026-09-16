"""How many return-flight lookups a sweep can afford.

The base sweep (one search per itinerary) is guaranteed by the static check in
sweep.validate_target_budget. Return resolution is the variable part: it spends
whatever the plan has left this month beyond what the remaining base sweeps
need, spread evenly, so a Starter key resolves a few returns per sweep and a
Developer key resolves all of them -- with no config change between tiers.
"""
from __future__ import annotations

import math
from datetime import datetime


def sweeps_left_in_month(now: datetime, sweeps_per_day: int) -> int:
    nxt = datetime(now.year + (now.month == 12), (now.month % 12) + 1, 1)
    days = (nxt - now).total_seconds() / 86400
    return max(1, math.ceil(days * sweeps_per_day))


def returns_budget(plan_searches_left: int | None, now: datetime, sweeps_per_day: int,
                   base_calls_per_sweep: int, cap: int,
                   headroom: int = 20, margin: float = 1.1) -> int:
    """Return lookups this sweep may spend. 0 when the plan is unknown or
    the rest of the month's base sweeps would not fit."""
    if plan_searches_left is None or plan_searches_left <= 0:
        return 0
    remaining = sweeps_left_in_month(now, sweeps_per_day)
    reserve = remaining * base_calls_per_sweep * margin + headroom
    spare = plan_searches_left - reserve
    if spare <= 0:
        return 0
    return int(min(cap, spare // remaining))
