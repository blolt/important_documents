"""Thin SerpApi fetch shell.

Kept deliberately thin: everything worth testing lives in normalize.py and
decide.py. The only logic here is parameter construction, which is unit
tested; the transport is injected so nothing in the suite touches the network.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from .models import Query

ENDPOINT = "https://serpapi.com/search"
ROUND_TRIP, ONE_WAY = "1", "2"


class QuotaExceeded(RuntimeError):
    pass


def build_params(query: Query, api_key: str, origin: str, destination: str,
                 currency: str = "USD") -> dict[str, str]:
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": query.depart.isoformat(),
        "currency": currency,
        "hl": "en",
        "gl": "us",
        "type": ROUND_TRIP if query.is_round_trip else ONE_WAY,
        "api_key": api_key,
    }
    if query.ret is not None:
        params["return_date"] = query.ret.isoformat()
    return params


def build_url(query: Query, api_key: str, origin: str, destination: str) -> str:
    return f"{ENDPOINT}?{urllib.parse.urlencode(build_params(query, api_key, origin, destination))}"


def _default_get(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch(query: Query, api_key: str, origin: str, destination: str,
          get: Callable[[str], str] = _default_get) -> dict:
    """One search. Raises QuotaExceeded so a sweep can stop rather than
    hammer an exhausted plan for the rest of the month."""
    payload = json.loads(get(build_url(query, api_key, origin, destination)))
    error = payload.get("error", "") if isinstance(payload, dict) else ""
    if error and "run out of searches" in str(error).lower():
        raise QuotaExceeded(str(error))
    return payload
