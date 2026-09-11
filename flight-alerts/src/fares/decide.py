"""The alert gate.

Description, not prediction (SPEC.md §5): we report that a fare is cheap
against its own record, and never that it is about to move.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Decision, Observation, Policy

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


def _last_alert_price(obs: Observation, alerts: list[dict], policy: Policy,
                      now: datetime) -> int | None:
    """Price we last announced for this itinerary inside the debounce window."""
    cutoff = now - timedelta(hours=policy.debounce_hours)
    depart, ret = obs.depart.isoformat(), obs.ret.isoformat() if obs.ret else None
    recent = [a for a in alerts
              if a["depart"] == depart and a.get("ret") == ret
              and datetime.fromisoformat(a["sent_at"]) >= cutoff]
    if not recent:
        return None
    return min(a["price_usd"] for a in recent)


def _is_debounced(obs: Observation, price: int, alerts: list[dict],
                  policy: Policy, now: datetime) -> bool:
    """Suppress a repeat, unless the fare has fallen materially since.

    A stable cheap fare should not mail the group every two hours. A fare
    that drops another $60 should.
    """
    last = _last_alert_price(obs, alerts, policy, now)
    if last is None:
        return False
    return price > last - policy.renotify_drop_usd


def should_alert(obs: Observation, comparable: list[int], policy: Policy,
                 alerts: list[dict], now: datetime) -> Decision:
    """Gates are ordered cheapest-and-most-decisive first.

    `comparable` is the price history this observation should be judged
    against, chosen by the caller: same itinerary for a fixed-date trip,
    same lead-time bucket for a rolling sweep. Keeping that decision out
    here means the gate logic does not care which mode we are in.
    """
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

    if _is_debounced(obs, price, alerts, policy, now):
        return Decision(False, f"debounced:{policy.debounce_hours}h", price)

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
