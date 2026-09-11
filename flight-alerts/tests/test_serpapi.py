import json
from datetime import date

import pytest

from fares.models import Query
from fares.serpapi import QuotaExceeded, build_params, build_url, fetch

RT = Query(date(2026, 11, 14), date(2026, 11, 19))
OW = Query(date(2026, 11, 14))


class TestParams:
    def test_route_and_dates(self):
        p = build_params(RT, "KEY", "DTW", "MIA")
        assert p["departure_id"] == "DTW" and p["arrival_id"] == "MIA"
        assert p["outbound_date"] == "2026-11-14" and p["return_date"] == "2026-11-19"

    def test_round_trip_type(self):
        assert build_params(RT, "KEY", "DTW", "MIA")["type"] == "1"

    def test_one_way_type_and_no_return_date(self):
        p = build_params(OW, "KEY", "DTW", "MIA")
        assert p["type"] == "2" and "return_date" not in p

    def test_currency_is_explicit(self):
        # Without this the endpoint localizes and we silently log non-USD fares.
        assert build_params(RT, "KEY", "DTW", "MIA")["currency"] == "USD"

    def test_url_encodes_params(self):
        url = build_url(RT, "KEY", "DTW", "MIA")
        assert url.startswith("https://serpapi.com/search?")
        assert "engine=google_flights" in url and "api_key=KEY" in url


class TestFetch:
    def test_returns_parsed_payload(self):
        payload = {"best_flights": []}
        assert fetch(RT, "KEY", "DTW", "MIA", get=lambda u: json.dumps(payload)) == payload

    def test_quota_message_raises_quota_exceeded(self):
        # Must be distinguishable: a sweep should stop, not retry 149 more times.
        body = json.dumps({"error": "Your account has run out of searches."})
        with pytest.raises(QuotaExceeded):
            fetch(RT, "KEY", "DTW", "MIA", get=lambda u: body)

    def test_other_errors_pass_through_for_normalize_to_reject(self):
        body = json.dumps({"error": "Invalid API key."})
        assert "error" in fetch(RT, "KEY", "DTW", "MIA", get=lambda u: body)

    def test_api_key_is_sent_but_not_in_the_payload(self):
        seen = {}

        def get(url):
            seen["url"] = url
            return json.dumps({})

        fetch(RT, "SECRET", "DTW", "MIA", get=get)
        assert "SECRET" in seen["url"]
