"""Production sweep configuration.

Tuned against the SerpApi Developer tier (5,000 searches/month). The band
cadences are not arbitrary: a 3-day mid-band cadence needs 5,100/month and
overruns the plan. See tests/test_sweep.py::TestBudget.
"""
from __future__ import annotations

from .models import Band, SweepConfig

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
)
