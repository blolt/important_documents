from datetime import date

import pytest

from fares.config import ACTIVE, NYE_TRIP
from fares.models import TargetTrip
from fares.sweep import (
    BudgetExceeded,
    select_target_queries,
    sweep_interval_hours,
    target_daily_estimate,
    target_monthly_estimate,
    validate_target_budget,
)


def trip(**kw):
    base = dict(
        departures=(date(2026, 12, 30), date(2026, 12, 31)),
        returns=(date(2027, 1, 2), date(2027, 1, 3)),
        monthly_budget=5000,
    )
    base.update(kw)
    return TargetTrip(**base)


class TestGrid:
    def test_full_cartesian_product_when_all_viable(self):
        assert len(select_target_queries(trip())) == 4

    def test_returns_before_departure_are_excluded(self):
        t = trip(departures=(date(2027, 1, 5),), returns=(date(2027, 1, 2),))
        assert select_target_queries(t) == []

    def test_min_nights_filters_same_day_turnarounds(self):
        t = trip(departures=(date(2026, 12, 31),), returns=(date(2027, 1, 1),),
                 min_nights=2)
        assert select_target_queries(t) == []

    def test_max_nights_filters_overlong_trips(self):
        t = trip(departures=(date(2026, 12, 30),), returns=(date(2027, 1, 20),),
                 max_nights=10)
        assert select_target_queries(t) == []

    def test_grid_is_independent_of_today(self):
        # Unlike the rolling sweep, a fixed trip is the same grid every day.
        assert select_target_queries(trip()) == select_target_queries(trip())

    def test_every_query_is_a_round_trip(self):
        assert all(q.is_round_trip for q in select_target_queries(trip()))


class TestBudget:
    def test_estimate_scales_with_sweep_frequency(self):
        once = target_monthly_estimate(trip(sweeps_per_day=1))
        twelve = target_monthly_estimate(trip(sweeps_per_day=12))
        assert twelve == once * 12

    def test_estimate_scales_with_calls_per_query(self):
        assert (target_monthly_estimate(trip(calls_per_query=2))
                == target_monthly_estimate(trip(calls_per_query=1)) * 2)

    def test_too_frequent_sweeping_is_rejected(self):
        with pytest.raises(BudgetExceeded):
            validate_target_budget(trip(sweeps_per_day=100))

    def test_interval_derives_from_frequency(self):
        assert sweep_interval_hours(trip(sweeps_per_day=12)) == 2
        assert sweep_interval_hours(trip(sweeps_per_day=1)) == 24


class TestNyeTrip:
    def test_the_trip_is_the_active_config(self):
        assert ACTIVE is NYE_TRIP

    def test_fits_the_developer_tier(self):
        validate_target_budget(NYE_TRIP)
        assert target_monthly_estimate(NYE_TRIP) == 3960

    def test_eleven_viable_itineraries(self):
        # 3 departures x 4 returns = 12, less Dec 31 -> Jan 1 at one night.
        assert len(select_target_queries(NYE_TRIP)) == 11

    def test_new_years_eve_departure_is_covered(self):
        departures = {q.depart for q in select_target_queries(NYE_TRIP)}
        assert date(2026, 12, 31) in departures

    def test_one_night_new_years_itinerary_is_excluded(self):
        pairs = {(q.depart, q.ret) for q in select_target_queries(NYE_TRIP)}
        assert (date(2026, 12, 31), date(2027, 1, 1)) not in pairs

    def test_sweeps_every_two_hours(self):
        assert sweep_interval_hours(NYE_TRIP) == 2

    def test_trip_is_beyond_the_rolling_horizon(self):
        # The reason fixed-date targeting exists: on 2026-09-11 the trip is
        # 111 days out and the 90-day rolling sweep cannot see it.
        from fares.config import PRODUCTION
        assert (date(2026, 12, 31) - date(2026, 9, 11)).days > PRODUCTION.horizon_days
