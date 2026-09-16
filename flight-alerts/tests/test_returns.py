"""Return-flight resolution: the departure_token second call, its budget,
and how combos render. Fixtures are real responses recorded 2026-09-15."""
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fares.budget import returns_budget, sweeps_left_in_month
from fares.email_alert import format_digest
from fares.models import Decision
from fares.normalize import normalize_returns, normalize_with_tokens
from fares.serpapi import build_params
from fares.storage import from_record, to_record
from fares.models import Query

FIXTURES = Path(__file__).parent / "fixtures"
OUTBOUND = json.loads((FIXTURES / "real_dtw_mia_2026-12-28.json").read_text())
RETURNS = json.loads((FIXTURES / "returns" / "real_dtw_mia_2026-12-28_returns.json").read_text())
NOW = datetime(2026, 9, 15, 22, 0)
DEPART, RET = date(2026, 12, 28), date(2027, 1, 3)


def cheapest_outbound():
    outs = normalize_with_tokens(OUTBOUND, NOW, DEPART, RET)
    return min(outs, key=lambda p: p[0].price_usd)


class TestTokens:
    def test_every_outbound_carries_a_departure_token(self):
        outs = normalize_with_tokens(OUTBOUND, NOW, DEPART, RET)
        assert len(outs) == 13 and all(t for _, t in outs)

    def test_token_goes_into_the_second_request(self):
        params = build_params(Query(DEPART, RET), "k", "DTW", "MIA", departure_token="tok")
        assert params["departure_token"] == "tok" and params["type"] == "1"

    def test_token_is_omitted_by_default(self):
        assert "departure_token" not in build_params(Query(DEPART, RET), "k", "DTW", "MIA")


class TestNormalizeReturns:
    def test_combos_carry_the_real_round_trip_total(self):
        obs, _ = cheapest_outbound()
        combos = normalize_returns(RETURNS, obs)
        assert sorted(c.price_usd for c in combos) == [512, 626, 650, 660, 951]

    def test_the_512_is_american_out_frontier_back(self):
        # The headline "$512 American" is only $512 with a Frontier return;
        # American's own returns start at $650. This is why returns matter.
        obs, _ = cheapest_outbound()
        cheapest = min(normalize_returns(RETURNS, obs), key=lambda c: c.price_usd)
        assert cheapest.flight_numbers == ("AA 3542", "AA 817")
        assert cheapest.ret_airline == "Frontier"
        assert cheapest.ret_flight_numbers == ("F9 2483", "F9 1532")
        assert cheapest.ret_depart_time == "2027-01-03 06:15"
        assert cheapest.ret_arrive_time == "2027-01-03 16:18"
        assert cheapest.ret_duration_min == 603
        assert cheapest.ret_layovers == ("ATL",) and cheapest.ret_stops == 1
        assert cheapest.has_return

    def test_outbound_fields_are_preserved(self):
        obs, _ = cheapest_outbound()
        for c in normalize_returns(RETURNS, obs):
            assert (c.flight_numbers, c.depart_time, c.depart, c.ret) == \
                   (obs.flight_numbers, obs.depart_time, DEPART, RET)

    def test_url_is_the_returns_page_with_outbound_selected(self):
        obs, _ = cheapest_outbound()
        combos = normalize_returns(RETURNS, obs)
        assert combos[0].url != obs.url
        assert combos[0].url.startswith("https://www.google.com/travel/flights?")

    def test_combos_round_trip_through_storage(self):
        obs, _ = cheapest_outbound()
        for c in normalize_returns(RETURNS, obs):
            assert from_record(to_record(c)) == c

    def test_error_payload_raises(self):
        from fares.normalize import MalformedResponse
        with pytest.raises(MalformedResponse):
            normalize_returns({"error": "nope"}, cheapest_outbound()[0])


class TestBudget:
    def test_sweeps_left_counts_to_month_end(self):
        # Sep 15 22:00 -> Oct 1: 15.08 days x 6 = 91
        assert sweeps_left_in_month(NOW, 6) == 91

    def test_december_rolls_the_year(self):
        assert sweeps_left_in_month(datetime(2026, 12, 31, 12), 6) == 3

    def test_unknown_plan_resolves_nothing(self):
        assert returns_budget(None, NOW, 6, 1, 15) == 0

    def test_free_plan_leaves_about_one_per_sweep(self):
        # 238 left; reserve 91*1.1+20 = 120.1; spare 117.9 // 91 = 1
        assert returns_budget(238, NOW, 6, 1, 15) == 1

    def test_starter_plan_resolves_several(self):
        # 1000 left: spare 879.9 // 91 = 9
        assert returns_budget(1000, NOW, 6, 1, 15) == 9

    def test_developer_plan_hits_the_cap(self):
        assert returns_budget(5000, NOW, 6, 1, 15) == 15

    def test_exhausted_plan_resolves_nothing(self):
        assert returns_budget(50, NOW, 6, 1, 15) == 0
        assert returns_budget(0, NOW, 6, 1, 15) == 0


class TestComboDigest:
    def _digest(self):
        obs, _ = cheapest_outbound()
        combo = min(normalize_returns(RETURNS, obs), key=lambda c: c.price_usd)
        return format_digest([(combo, Decision(True, "x", combo.price_usd))], "DTW", "MIA", "NYE 2026")

    def test_both_legs_are_described(self):
        _, text, html = self._digest()
        for needle in ("American AA 3542 / AA 817 out", "Frontier F9 2483 / F9 1532 back",
                       "Out Mon Dec 28 4:34 PM → 10:25 PM · 5h 51m · 1 stop via ORD",
                       "Back Sun Jan 3 6:15 AM → 4:18 PM · 10h 03m · 1 stop via ATL (6 nights)"):
            assert needle in text, needle
        for needle in ("AA 3542 / AA 817", "F9 2483 / F9 1532", "6:15 AM", "10h 03m", "via ATL"):
            assert needle in html, needle

    def test_link_is_the_outbound_selected_page(self):
        _, text, html = self._digest()
        assert "outbound pre-selected" in html
        assert RETURNS["search_metadata"]["google_flights_url"] in text

    def test_unresolved_outbound_says_so(self):
        obs, _ = cheapest_outbound()
        _, text, html = format_digest([(obs, Decision(True, "x", obs.price_usd))], "DTW", "MIA", "NYE 2026")
        assert "return flight not resolved" in text and "return not resolved" in html
