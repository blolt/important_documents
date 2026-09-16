"""SerpApi google_flights JSON -> Observation[].

Written against SerpApi's documented response shape, NOT against a recorded
live response (SPEC.md §12). Every field access is therefore defensive: an
unexpected shape should drop an itinerary and keep going, never crash a sweep
or -- worse -- silently record a wrong price.

`tests/test_normalize.py::TestFixtureContract` runs over every fixture in
tests/fixtures/, so dropping a real recorded response into that directory
validates this parser against reality without writing a new test.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime

from .models import Observation

ITINERARY_KEYS = ("best_flights", "other_flights")
# "DL 1234" -> "DL". Prefer this over the airline display name: codes are
# stable and comparable, names are localized and get rebranded.
_FLIGHT_NUMBER = re.compile(r"^([A-Z0-9]{2})\s*\d")


class MalformedResponse(ValueError):
    pass


def _carrier(leg: dict) -> str | None:
    match = _FLIGHT_NUMBER.match(str(leg.get("flight_number", "")).strip())
    if match:
        return match.group(1)
    airline = leg.get("airline")
    return str(airline) if airline else None


def _price_insights(payload: dict) -> tuple[str | None, int | None, int | None]:
    """Google's own signal. Absent on some responses, so all three are optional."""
    insights = payload.get("price_insights") or {}
    level = insights.get("price_level")
    band = insights.get("typical_price_range") or []
    low = band[0] if len(band) >= 1 and isinstance(band[0], int) else None
    high = band[1] if len(band) >= 2 and isinstance(band[1], int) else None
    return (str(level) if level else None), low, high


def _leg_time(leg: dict, end: str) -> str | None:
    airport = leg.get(end) or {}
    t = airport.get("time") if isinstance(airport, dict) else None
    return str(t) if t else None


def _legs_detail(itin: dict) -> dict | None:
    """Airline, flight numbers, times, duration, routing for one itinerary's
    legs (outbound or return; the shape is the same)."""
    legs = itin.get("flights") or []
    if not legs or not all(isinstance(l, dict) for l in legs):
        return None
    carrier = _carrier(legs[0])
    if carrier is None:
        return None
    # Prefer the explicit layover count; fall back to leg count.
    layovers = itin.get("layovers")
    stops = len(layovers) if isinstance(layovers, list) else max(0, len(legs) - 1)
    airline = legs[0].get("airline")
    duration = itin.get("total_duration")
    return dict(
        carrier=carrier,
        airline=str(airline) if airline else None,
        numbers=tuple(str(l.get("flight_number")).strip() for l in legs if l.get("flight_number")),
        depart_time=_leg_time(legs[0], "departure_airport"),
        arrive_time=_leg_time(legs[-1], "arrival_airport"),
        duration=duration if isinstance(duration, int) else None,
        layovers=tuple(str(l.get("id")) for l in layovers
                       if isinstance(l, dict) and l.get("id")) if isinstance(layovers, list) else (),
        stops=stops,
    )


def _itinerary_to_observation(itin: dict, observed_at: datetime, depart: date,
                              ret: date | None, level: str | None,
                              low: int | None, high: int | None,
                              url: str | None = None) -> Observation | None:
    price = itin.get("price")
    if not isinstance(price, int) or price <= 0:
        return None
    d = _legs_detail(itin)
    if d is None:
        return None
    return Observation(
        observed_at=observed_at, depart=depart, ret=ret, price_usd=price,
        carrier=d["carrier"], stops=d["stops"],
        price_level=level, typical_low=low, typical_high=high,
        airline=d["airline"], flight_numbers=d["numbers"],
        depart_time=d["depart_time"], arrive_time=d["arrive_time"],
        duration_min=d["duration"], layovers=d["layovers"], url=url,
    )


def _checked(payload) -> str | None:
    if not isinstance(payload, dict):
        raise MalformedResponse(f"expected dict payload, got {type(payload).__name__}")
    if payload.get("error"):
        raise MalformedResponse(str(payload["error"]))
    meta = payload.get("search_metadata") or {}
    url = meta.get("google_flights_url") if isinstance(meta, dict) else None
    return str(url) if url else None


def normalize_with_tokens(payload: dict, observed_at: datetime, depart: date,
                          ret: date | None = None) -> list[tuple[Observation, str | None]]:
    """Every priced outbound option paired with its departure_token, which a
    second search spends to list that option's return flights."""
    url = _checked(payload)
    level, low, high = _price_insights(payload)
    out: list[tuple[Observation, str | None]] = []
    for key in ITINERARY_KEYS:
        for itin in payload.get(key) or []:
            if not isinstance(itin, dict):
                continue
            obs = _itinerary_to_observation(itin, observed_at, depart, ret,
                                            level, low, high, url)
            if obs is not None:
                token = itin.get("departure_token")
                out.append((obs, str(token) if token else None))
    return out


def normalize(payload: dict, observed_at: datetime, depart: date,
              ret: date | None = None) -> list[Observation]:
    """Extract every priced itinerary from one search response.

    `depart`/`ret` come from the Query we issued rather than from the payload:
    they are what we asked for, and are the keys the rest of the pipeline
    groups and debounces on.
    """
    return [obs for obs, _ in normalize_with_tokens(payload, observed_at, depart, ret)]


def normalize_returns(payload: dict, outbound: Observation) -> list[Observation]:
    """departure_token response -> one Observation per outbound+return
    combination. Each carries the combination's real round-trip total and the
    URL of the returns page with this outbound already selected."""
    url = _checked(payload)
    combos: list[Observation] = []
    for key in ITINERARY_KEYS:
        for itin in payload.get(key) or []:
            if not isinstance(itin, dict):
                continue
            price = itin.get("price")
            if not isinstance(price, int) or price <= 0:
                continue
            d = _legs_detail(itin)
            if d is None:
                continue
            combos.append(replace(
                outbound, price_usd=price, url=url or outbound.url,
                ret_airline=d["airline"], ret_flight_numbers=d["numbers"],
                ret_depart_time=d["depart_time"], ret_arrive_time=d["arrive_time"],
                ret_duration_min=d["duration"], ret_layovers=d["layovers"],
                ret_stops=d["stops"],
            ))
    return combos


def cheapest(observations: list[Observation]) -> Observation | None:
    return min(observations, key=lambda o: o.price_usd, default=None)
