"""Budget-aware sweep planning.

Which (departure, return) pairs do we spend quota on today? Fare volatility
concentrates near departure, so near-term dates are swept daily and far-term
dates are sampled. The monthly budget is a hard constraint, validated up
front rather than discovered when the quota runs dry mid-month.
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import Band, Query, SweepConfig, TargetTrip

DAYS_PER_MONTH = 30


class BudgetExceeded(ValueError):
    """Raised when a config would outrun its monthly search quota."""


def cadence_for_lead(lead_days: int, bands: tuple[Band, ...]) -> int:
    """Sweep interval for a date `lead_days` out. Bands are inclusive upper bounds."""
    for band in bands:
        if lead_days <= band.max_lead_days:
            return band.cadence_days
    return bands[-1].cadence_days


def lead_bucket(lead_days: int, bands: tuple[Band, ...]) -> int:
    """Index of the band a lead time falls in. Used to group comparable history."""
    for i, band in enumerate(bands):
        if lead_days <= band.max_lead_days:
            return i
    return len(bands) - 1


def _is_due(lead_days: int, bands: tuple[Band, ...]) -> bool:
    """A date is due when its lead time is a multiple of its cadence.

    Because lead time decrements by one each day, this naturally staggers
    which dates fire on which day instead of bursting the whole far-term
    band at once -- which matters, since every SerpApi plan throttles to
    20% of monthly volume per hour.
    """
    return lead_days % cadence_for_lead(lead_days, bands) == 0


def select_queries(today: date, config: SweepConfig) -> list[Query]:
    """The searches to run today. Deterministic given (today, config)."""
    queries: list[Query] = []
    for lead in range(1, config.horizon_days + 1):
        if not _is_due(lead, config.bands):
            continue
        depart = today + timedelta(days=lead)
        if config.return_offsets:
            queries.extend(Query(depart, depart + timedelta(days=o))
                           for o in config.return_offsets)
        else:
            queries.append(Query(depart))
    return queries


def due_dates_per_day(config: SweepConfig) -> int:
    return sum(1 for lead in range(1, config.horizon_days + 1)
               if _is_due(lead, config.bands))


def daily_estimate(config: SweepConfig) -> int:
    """API calls consumed on a typical day."""
    return due_dates_per_day(config) * config.calls_per_date


def monthly_estimate(config: SweepConfig) -> int:
    return daily_estimate(config) * DAYS_PER_MONTH


def validate_budget(config: SweepConfig) -> None:
    """Fail fast on a config that cannot fit its plan."""
    estimate = monthly_estimate(config)
    if estimate > config.monthly_budget:
        raise BudgetExceeded(
            f"config needs ~{estimate} searches/month but budget is "
            f"{config.monthly_budget}. Widen the band cadences, shorten the "
            f"horizon, drop return offsets, or raise the plan tier."
        )


# --- Fixed-date targeting ---------------------------------------------------

def select_target_queries(trip: TargetTrip) -> list[Query]:
    """Every viable itinerary in the trip's date grid.

    Unlike the rolling sweep this does not depend on today's date: a fixed
    trip is the same grid every day until it happens.
    """
    queries = []
    for depart in trip.departures:
        for ret in trip.returns:
            nights = (ret - depart).days
            if trip.min_nights <= nights <= trip.max_nights:
                queries.append(Query(depart, ret))
    return queries


def target_daily_estimate(trip: TargetTrip) -> int:
    return len(select_target_queries(trip)) * trip.sweeps_per_day * trip.calls_per_query


def target_monthly_estimate(trip: TargetTrip) -> int:
    return target_daily_estimate(trip) * DAYS_PER_MONTH


def validate_target_budget(trip: TargetTrip) -> None:
    estimate = target_monthly_estimate(trip)
    if estimate > trip.monthly_budget:
        raise BudgetExceeded(
            f"trip needs ~{estimate} searches/month but budget is "
            f"{trip.monthly_budget}. Reduce sweeps_per_day, trim the date "
            f"grid, or raise the plan tier."
        )


def sweep_interval_hours(trip: TargetTrip) -> float:
    """Hours between sweeps, for scheduling."""
    return 24 / trip.sweeps_per_day
