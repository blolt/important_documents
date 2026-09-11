"""Core value types. Deliberately dependency-free so the pure layers stay testable."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class Query:
    """One search we intend to spend a quota unit on."""
    depart: date
    ret: date | None = None

    @property
    def is_round_trip(self) -> bool:
        return self.ret is not None


@dataclass(frozen=True)
class Band:
    """Sampling cadence for a lead-time band. `max_lead_days` is inclusive."""
    max_lead_days: int
    cadence_days: int


@dataclass(frozen=True)
class SweepConfig:
    horizon_days: int
    bands: tuple[Band, ...]
    monthly_budget: int
    return_offsets: tuple[int, ...] = ()  # empty => one-way
    # SerpApi may require a second call (departure_token) to resolve a
    # round-trip total. Unverified -- see SPEC.md §12. Modelled as a
    # parameter so the budget math is right either way rather than
    # discovering a 2x overrun in production.
    calls_per_query: int = 1

    @property
    def queries_per_date(self) -> int:
        return len(self.return_offsets) or 1

    @property
    def calls_per_date(self) -> int:
        return self.queries_per_date * self.calls_per_query


@dataclass(frozen=True)
class Observation:
    """A single normalized fare reading."""
    observed_at: datetime
    depart: date
    ret: date | None
    price_usd: int
    carrier: str
    stops: int
    # Google's own signal; absent on some responses, so all optional.
    price_level: str | None = None
    typical_low: int | None = None
    typical_high: int | None = None

    @property
    def lead_days(self) -> int:
        return (self.depart - self.observed_at.date()).days

    @property
    def is_nonstop(self) -> bool:
        return self.stops == 0


@dataclass(frozen=True)
class Policy:
    ceiling_usd: int
    percentile: float                 # alert if fare at/below this percentile of comparable history
    debounce_hours: int = 24
    nonstop_only: bool = False
    excluded_carriers: frozenset[str] = frozenset()
    bag_fee_usd: dict[str, int] = field(default_factory=dict)
    min_history: int = 5              # below this, percentile rank is not meaningful
    # Day one has no history, so the cold-start path can fire on every
    # itinerary in the sweep. Cap it, keeping the cheapest.
    max_alerts_per_sweep: int = 5
    # Debounce silences repeats, but a fare that keeps falling is news.
    # Re-alert inside the debounce window if it dropped at least this much
    # below what we last announced for the same itinerary.
    renotify_drop_usd: int = 25


@dataclass(frozen=True)
class Decision:
    alert: bool
    reason: str
    effective_price_usd: int | None = None
    percentile_rank: float | None = None


@dataclass(frozen=True)
class TargetTrip:
    """A fixed-date trip: an explicit grid of candidate itineraries.

    The rolling-horizon SweepConfig is the wrong shape for a trip with a
    known date. NYE is not "somewhere in the next 90 days" -- it is a dozen
    specific itineraries we want sampled often, so the budget buys temporal
    resolution on what we care about instead of breadth we don't.
    """
    departures: tuple[date, ...]
    returns: tuple[date, ...]
    monthly_budget: int
    sweeps_per_day: int = 1
    calls_per_query: int = 1
    # Sanity bounds: a Miami NYE trip is not 1 night or 3 weeks.
    min_nights: int = 2
    max_nights: int = 10
