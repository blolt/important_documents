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

    @property
    def queries_per_date(self) -> int:
        return len(self.return_offsets) or 1


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


@dataclass(frozen=True)
class Decision:
    alert: bool
    reason: str
    effective_price_usd: int | None = None
    percentile_rank: float | None = None
