"""Production sweep configuration.

Tuned against the SerpApi Developer tier (5,000 searches/month). The band
cadences are not arbitrary: a 3-day mid-band cadence needs 5,100/month and
overruns the plan. See tests/test_sweep.py::TestBudget.
"""
from __future__ import annotations

from datetime import date

from .models import Band, SweepConfig, TargetTrip

ORIGIN = "DTW"
DESTINATION = "MIA"

# Inclusive upper bounds. Volatility concentrates near departure, so the
# near band is swept daily and the far band is sampled.
BANDS = (
    Band(max_lead_days=14, cadence_days=1),
    Band(max_lead_days=60, cadence_days=4),
    Band(max_lead_days=90, cadence_days=7),
)

# Nights in Miami worth pricing. Five offsets => 5x the query cost.
RETURN_OFFSETS = (3, 4, 5, 6, 7)

# SerpApi Developer tier. ~4,500/month used, leaving headroom for retries
# and ad-hoc lookups. Plans do not roll over, so headroom is use-it-or-lose-it.
SERPAPI_DEVELOPER_TIER = 5000

PRODUCTION = SweepConfig(
    horizon_days=90,
    bands=BANDS,
    monthly_budget=SERPAPI_DEVELOPER_TIER,
    return_offsets=RETURN_OFFSETS,
    calls_per_query=1,
)

# If a round-trip total turns out to need a second call per itinerary
# (departure_token), PRODUCTION needs 9,000/month and fails validation at
# startup. This is the pre-tuned replacement: narrower near band, 5-day
# mid cadence, three return offsets => 4,680/month at two calls each.
# Swap PRODUCTION for this and nothing else changes.
NARROW_BANDS = (
    Band(max_lead_days=12, cadence_days=1),
    Band(max_lead_days=60, cadence_days=5),
    Band(max_lead_days=90, cadence_days=7),
)

FALLBACK_TWO_CALL = SweepConfig(
    horizon_days=90,
    bands=NARROW_BANDS,
    monthly_budget=SERPAPI_DEVELOPER_TIER,
    return_offsets=(4, 5, 6),
    calls_per_query=2,
)


# --- The actual trip -------------------------------------------------------
# NYE 2026 falls on a Thursday, so Dec 31 -> Jan 3/4 is the natural long
# weekend. The trip is 111 days out as of 2026-09-11, which is why the
# rolling 90-day SweepConfig above cannot see it at all.
#
# One itinerary: the group settled on Dec 28 -> Jan 3 (2026-09-15), so the
# date grid collapsed to a single query. The key is on SerpApi's Free plan
# (250 searches/month, checked 2026-09-15), so 6 sweeps/day = 180/month
# leaves room for manual runs. 12/day would overrun it.
SERPAPI_FREE_PLAN = 250
TRIP_LABEL = "New Year's Eve 2026"

NYE_TRIP = TargetTrip(
    departures=(date(2026, 12, 28),),
    returns=(date(2027, 1, 3),),
    monthly_budget=SERPAPI_FREE_PLAN,
    sweeps_per_day=6,
    calls_per_query=1,
    min_nights=2,
    max_nights=10,
)

# What the CLI and the workflow actually run.
ACTIVE = NYE_TRIP

# --- Alert policy -----------------------------------------------------------
# These are placeholders awaiting John's actual travel preferences (SPEC.md §7).
# Edit policy.json rather than this file; these are the fallbacks if it is absent.
DEFAULT_POLICY = {
    "ceiling_usd": 300,
    "percentile": 0.10,
    "debounce_hours": 24,
    "nonstop_only": False,
    "excluded_carriers": [],
    # A $59 Spirit fare with a $75 bag is not a $59 fare. Zero here means
    # "carry-on included or I don't check one" -- set per carrier as needed.
    "bag_fee_usd": {},
    "min_history": 5,
    "max_alerts_per_sweep": 5,
    "renotify_drop_usd": 25,
    "veto_google_high": True,
}


def load_policy(path=None):
    """Policy from JSON, falling back to DEFAULT_POLICY."""
    import json
    from pathlib import Path

    from .models import Policy

    raw = dict(DEFAULT_POLICY)
    if path is not None and Path(path).exists():
        raw.update(json.loads(Path(path).read_text()))
    return Policy(
        ceiling_usd=int(raw["ceiling_usd"]),
        percentile=None if raw["percentile"] is None else float(raw["percentile"]),
        debounce_hours=int(raw["debounce_hours"]),
        nonstop_only=bool(raw["nonstop_only"]),
        excluded_carriers=frozenset(raw["excluded_carriers"]),
        bag_fee_usd={k: int(v) for k, v in raw["bag_fee_usd"].items()},
        min_history=int(raw["min_history"]),
        max_alerts_per_sweep=int(raw["max_alerts_per_sweep"]),
        renotify_drop_usd=int(raw["renotify_drop_usd"]),
        veto_google_high=bool(raw["veto_google_high"]),
    )
