"""The alert gate.

Description, not prediction (SPEC.md §5): we report that a fare is cheap
against its own record, and never that it is about to move.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Band, Decision, Observation, Policy
from .sweep import lead_bucket

Candidate = tuple[Observation, Decision]


def effective_price(obs: Observation, policy: Policy) -> int:
    """Price normalized for bag fees.

    A $59 Spirit fare with a $75 bag is not a $59 fare, and comparing it to a
    Delta fare that includes one is comparing different products.
    """
    return obs.price_usd + policy.bag_fee_usd.get(obs.carrier, 0)


def percentile_rank(price: int, history: list[int]) -> float:
    """Fraction of comparable history at or below `price`. 0.0 == cheapest seen."""
    if not history:
        return 1.0
    return sum(1 for h in history if h <= price) / len(history)


def _recently_alerted(obs: Observation, alerts: list[dict], policy: Policy,
                      now: datetime) -> bool:
    """Debounce, so a stable cheap fare does not page every sweep."""
    cutoff = now - timedelta(hours=policy.debounce_hours)
    depart, ret = obs.depart.isoformat(), obs.ret.isoformat() if obs.ret else None
    for alert in alerts:
        if alert["depart"] != depart or alert.get("ret") != ret:
            continue
        if datetime.fromisoformat(alert["sent_at"]) >= cutoff:
            return True
    return False


def should_alert(obs: Observation, history: dict[int, list[int]], policy: Policy,
                 alerts: list[dict], now: datetime,
                 bands: tuple[Band, ...]) -> Decision:
    """Gates are ordered cheapest-and-most-decisive first."""
    price = effective_price(obs, policy)

    if obs.carrier in policy.excluded_carriers:
        return Decision(False, f"carrier_excluded:{obs.carrier}", price)

    if policy.nonstop_only and not obs.is_nonstop:
        return Decision(False, f"not_nonstop:{obs.stops}_stops", price)

    if price > policy.ceiling_usd:
        return Decision(False, f"above_ceiling:{price}>{policy.ceiling_usd}", price)

    # Google's own read. We use it only to veto, never to justify.
    if obs.price_level == "high":
        return Decision(False, "google_price_level_high", price)

    if _recently_alerted(obs, alerts, policy, now):
        return Decision(False, f"debounced:{policy.debounce_hours}h", price)

    comparable = history.get(lead_bucket(obs.lead_days, bands), [])

    # Cold start: with too little history a percentile is noise, so fall back
    # to the typical range Google ships with the response. This is why
    # price_insights is worth collecting even having dropped forecasting.
    if len(comparable) < policy.min_history:
        if obs.typical_low is not None and price <= obs.typical_low:
            return Decision(True, "cold_start:below_google_typical_low", price)
        return Decision(False, f"insufficient_history:{len(comparable)}", price)

    rank = percentile_rank(price, comparable)
    if rank <= policy.percentile:
        return Decision(True, f"percentile:{rank:.3f}<={policy.percentile}", price, rank)
    return Decision(False, f"percentile:{rank:.3f}>{policy.percentile}", price, rank)


def select_alerts(candidates: list[Candidate], policy: Policy) -> list[Candidate]:
    """Cap a sweep's alerts, keeping the cheapest.

    Without this, the first run -- which has no history and so leans on the
    cold-start path -- would fire once per itinerary. 150 notifications is
    indistinguishable from spam, and trains you to ignore the channel.
    """
    firing = [c for c in candidates if c[1].alert]
    firing.sort(key=lambda c: c[1].effective_price_usd or c[0].price_usd)
    return firing[:policy.max_alerts_per_sweep]
