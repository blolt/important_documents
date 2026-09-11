from datetime import date

import pytest

from fares.models import Band, SweepConfig
from fares.sweep import (
    BudgetExceeded,
    _is_due as _due,
    cadence_for_lead,
    lead_bucket,
    monthly_estimate,
    select_queries,
    validate_budget,
)

from fares.config import FALLBACK_TWO_CALL, PRODUCTION

BANDS = (Band(max_lead_days=14, cadence_days=1),
         Band(max_lead_days=60, cadence_days=4),
         Band(max_lead_days=90, cadence_days=7))

# The cadence we would reach for naively -- and which does not fit any tier
# we are willing to pay for. Kept as a regression guard.
NAIVE_BANDS = (Band(max_lead_days=14, cadence_days=1),
               Band(max_lead_days=60, cadence_days=3),
               Band(max_lead_days=90, cadence_days=7))

ONE_WAY = SweepConfig(horizon_days=90, bands=BANDS, monthly_budget=1000)
RT_5 = SweepConfig(horizon_days=90, bands=BANDS, monthly_budget=5000,
                   return_offsets=(3, 4, 5, 6, 7))
NAIVE_RT_5 = SweepConfig(horizon_days=90, bands=NAIVE_BANDS, monthly_budget=5000,
                         return_offsets=(3, 4, 5, 6, 7))


class TestCadence:
    def test_near_term_is_daily(self):
        assert cadence_for_lead(1, BANDS) == 1
        assert cadence_for_lead(14, BANDS) == 1

    def test_band_boundary_is_inclusive(self):
        # Off-by-one here silently halves near-term coverage.
        assert cadence_for_lead(14, BANDS) == 1
        assert cadence_for_lead(15, BANDS) == 4

    def test_far_term_is_weekly(self):
        assert cadence_for_lead(61, BANDS) == 7
        assert cadence_for_lead(90, BANDS) == 7

    def test_beyond_last_band_uses_widest_cadence(self):
        assert cadence_for_lead(999, BANDS) == 7

    def test_lead_bucket_is_stable_within_band(self):
        assert lead_bucket(1, BANDS) == lead_bucket(14, BANDS)
        assert lead_bucket(14, BANDS) != lead_bucket(15, BANDS)


class TestSelection:
    def test_respects_horizon(self):
        qs = select_queries(date(2026, 9, 11), ONE_WAY)
        leads = {(q.depart - date(2026, 9, 11)).days for q in qs}
        assert min(leads) >= 1          # never query today's departure
        assert max(leads) <= 90

    def test_near_term_dates_all_due_daily(self):
        qs = select_queries(date(2026, 9, 11), ONE_WAY)
        leads = {(q.depart - date(2026, 9, 11)).days for q in qs}
        assert {1, 2, 3, 14} <= leads

    def test_far_term_dates_are_sampled_not_swept(self):
        qs = select_queries(date(2026, 9, 11), ONE_WAY)
        far = {(q.depart - date(2026, 9, 11)).days for q in qs if (q.depart - date(2026, 9, 11)).days > 60}
        assert far, "some far-term coverage expected"
        assert len(far) < 30, "far-term should be sampled, not fully swept"

    def test_every_far_date_is_queried_before_it_leaves_the_band(self):
        # The real invariant: no departure date slips through the far band
        # unsampled. A date at lead L is due when L %% 7 == 0, and lead
        # decrements daily, so every date must hit a due day within a week.
        for start_lead in range(61, 91):
            assert any(_due(start_lead - d, BANDS) for d in range(7)), \
                f"lead {start_lead} never sampled within its cadence window"

    def test_round_trip_expands_each_date_by_offsets(self):
        ow = select_queries(date(2026, 9, 11), ONE_WAY)
        rt = select_queries(date(2026, 9, 11), RT_5)
        assert len(rt) == len(ow) * 5
        assert all(q.ret is not None for q in rt)

    def test_return_offset_applied_to_departure(self):
        cfg = SweepConfig(horizon_days=3, bands=BANDS, monthly_budget=1000, return_offsets=(5,))
        q = select_queries(date(2026, 9, 11), cfg)[0]
        assert (q.ret - q.depart).days == 5

    def test_one_way_has_no_return(self):
        assert all(q.ret is None for q in select_queries(date(2026, 9, 11), ONE_WAY))


class TestBudget:
    def test_production_config_fits_its_plan(self):
        # The guard that actually matters: what we ship must fit what we pay for.
        validate_budget(PRODUCTION)

    def test_production_leaves_headroom_for_retries(self):
        assert monthly_estimate(PRODUCTION) <= PRODUCTION.monthly_budget * 0.95

    def test_naive_cadence_overruns_the_75_dollar_tier(self):
        # 5,100/month against a 5,000 plan. Found by this test, not by a
        # quota exhaustion partway through a month.
        assert monthly_estimate(NAIVE_RT_5) == 5100
        with pytest.raises(BudgetExceeded):
            validate_budget(NAIVE_RT_5)

    def test_five_offset_round_trip_would_overrun_the_25_dollar_tier(self):
        downgraded = SweepConfig(horizon_days=90, bands=BANDS, monthly_budget=1000,
                                 return_offsets=(3, 4, 5, 6, 7))
        with pytest.raises(BudgetExceeded):
            validate_budget(downgraded)

    def test_one_way_still_fits_the_25_dollar_tier(self):
        assert monthly_estimate(ONE_WAY) <= 1000

    def test_estimate_scales_with_offsets(self):
        assert monthly_estimate(RT_5) == monthly_estimate(ONE_WAY) * 5

    def test_doubling_calls_per_query_doubles_the_estimate(self):
        doubled = SweepConfig(horizon_days=90, bands=BANDS, monthly_budget=99999,
                              return_offsets=(3, 4, 5, 6, 7), calls_per_query=2)
        assert monthly_estimate(doubled) == monthly_estimate(RT_5) * 2

    def test_production_overruns_if_round_trips_need_two_calls(self):
        # The unverified SerpApi behaviour in SPEC.md §12. If true, we must
        # fail at startup, not halfway through a billing month.
        two_call = SweepConfig(
            horizon_days=PRODUCTION.horizon_days, bands=PRODUCTION.bands,
            monthly_budget=PRODUCTION.monthly_budget,
            return_offsets=PRODUCTION.return_offsets, calls_per_query=2)
        assert monthly_estimate(two_call) == 9000
        with pytest.raises(BudgetExceeded):
            validate_budget(two_call)

    def test_fallback_config_fits_at_two_calls_per_query(self):
        assert FALLBACK_TWO_CALL.calls_per_query == 2
        validate_budget(FALLBACK_TWO_CALL)

    def test_error_reports_both_numbers(self):
        with pytest.raises(BudgetExceeded) as e:
            validate_budget(NAIVE_RT_5)
        assert "5000" in str(e.value) and "5100" in str(e.value)
