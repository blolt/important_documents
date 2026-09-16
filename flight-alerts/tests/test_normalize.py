import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fares.normalize import MalformedResponse, cheapest, normalize

FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED = datetime(2026, 9, 11, 12, 0)
DEPART, RET = date(2026, 11, 14), date(2026, 11, 19)


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def payload():
    return load("SYNTHETIC_dtw_mia_round_trip.json")


class TestExtraction:
    def test_reads_both_itinerary_lists(self, payload):
        obs = normalize(payload, OBSERVED, DEPART, RET)
        assert {o.carrier for o in obs} == {"DL", "AA", "NK"}

    def test_carrier_comes_from_flight_number_not_display_name(self, payload):
        obs = normalize(payload, OBSERVED, DEPART, RET)
        assert all(len(o.carrier) == 2 for o in obs)

    def test_query_dates_are_used_not_payload_dates(self, payload):
        obs = normalize(payload, OBSERVED, DEPART, RET)
        assert all(o.depart == DEPART and o.ret == RET for o in obs)

    def test_nonstop_detected_from_empty_layovers(self, payload):
        dl = next(o for o in normalize(payload, OBSERVED, DEPART, RET) if o.carrier == "DL")
        assert dl.is_nonstop and dl.stops == 0

    def test_connection_counted_from_layovers(self, payload):
        aa = next(o for o in normalize(payload, OBSERVED, DEPART, RET) if o.carrier == "AA")
        assert aa.stops == 1 and not aa.is_nonstop

    def test_multi_leg_round_trip_does_not_overstate_stops(self, payload):
        # Two legs but one layover: leg-count inference would say 1 stop and
        # be right here, but on a round-trip payload it would say 3. Layovers win.
        aa = next(o for o in normalize(payload, OBSERVED, DEPART, RET) if o.carrier == "AA")
        assert aa.stops == len(payload["best_flights"][1]["layovers"])

    def test_cheapest_picks_the_minimum(self, payload):
        assert cheapest(normalize(payload, OBSERVED, DEPART, RET)).price_usd == 96

    def test_cheapest_of_nothing_is_none(self):
        assert cheapest([]) is None


class TestPriceInsights:
    def test_insights_attach_to_every_observation(self, payload):
        for o in normalize(payload, OBSERVED, DEPART, RET):
            assert o.price_level == "low"
            assert (o.typical_low, o.typical_high) == (180, 320)

    def test_absent_insights_yield_none_not_a_crash(self):
        obs = normalize(load("SYNTHETIC_no_price_insights.json"), OBSERVED, DEPART, RET)
        assert obs and all(o.price_level is None and o.typical_low is None for o in obs)


class TestDefensiveParsing:
    def test_error_payload_raises(self):
        with pytest.raises(MalformedResponse):
            normalize({"error": "Invalid API key"}, OBSERVED, DEPART, RET)

    def test_non_dict_payload_raises(self):
        with pytest.raises(MalformedResponse):
            normalize([], OBSERVED, DEPART, RET)

    def test_empty_payload_yields_no_observations(self):
        assert normalize({}, OBSERVED, DEPART, RET) == []

    @pytest.mark.parametrize("itin", [
        {},                                              # nothing at all
        {"price": 200},                                  # price but no legs
        {"flights": [{"flight_number": "DL 1"}]},        # legs but no price
        {"price": "cheap", "flights": [{"flight_number": "DL 1"}]},  # price not an int
        {"price": 0, "flights": [{"flight_number": "DL 1"}]},        # nonsense price
        {"price": 200, "flights": [{}]},                 # leg with no carrier
        "not even a dict",
    ])
    def test_unusable_itinerary_is_dropped_not_fatal(self, itin):
        # A bad itinerary must never crash a sweep or record a wrong price.
        assert normalize({"best_flights": [itin]}, OBSERVED, DEPART, RET) == []

    def test_good_itineraries_survive_alongside_bad_ones(self):
        payload = {"best_flights": [
            {"price": 200},
            {"price": 214, "flights": [{"flight_number": "DL 1421"}], "layovers": []},
        ]}
        obs = normalize(payload, OBSERVED, DEPART, RET)
        assert len(obs) == 1 and obs[0].price_usd == 214

    def test_falls_back_to_airline_name_when_flight_number_unparseable(self):
        payload = {"best_flights": [
            {"price": 214, "flights": [{"airline": "Delta", "flight_number": "???"}]},
        ]}
        assert normalize(payload, OBSERVED, DEPART, RET)[0].carrier == "Delta"

    def test_malformed_typical_price_range_is_ignored(self):
        payload = {"best_flights": [{"price": 214, "flights": [{"flight_number": "DL 1"}]}],
                   "price_insights": {"typical_price_range": ["low", "high"]}}
        o = normalize(payload, OBSERVED, DEPART, RET)[0]
        assert o.typical_low is None and o.typical_high is None


class TestFixtureContract:
    """Runs over every fixture present, so a real recorded response is
    validated by dropping it into tests/fixtures/ -- no new test needed.
    """

    @pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
    def test_fixture_parses_and_prices_are_sane(self, path):
        obs = normalize(json.loads(path.read_text()), OBSERVED, DEPART, RET)
        assert obs, f"{path.name} produced no observations"
        for o in obs:
            assert 20 < o.price_usd < 5000, f"implausible fare {o.price_usd}"
            assert o.carrier and 2 <= len(o.carrier) <= 20
            assert o.stops >= 0
            assert o.price_level in (None, "low", "typical", "high")

    @pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
    def test_fixture_is_storage_round_trippable(self, path):
        from fares.storage import from_record, to_record
        for o in normalize(json.loads(path.read_text()), OBSERVED, DEPART, RET):
            assert from_record(to_record(o)) == o


class TestFlightDetail:
    """Fields the digest shows, parsed from the recorded 2026-09-15 response."""

    def _real(self):
        path = FIXTURES / "real_dtw_mia_2026-12-28.json"
        return normalize(json.loads(path.read_text()), OBSERVED, DEPART, RET)

    def test_cheapest_real_option_carries_full_detail(self):
        o = min(self._real(), key=lambda o: o.price_usd)
        assert o.price_usd == 512
        assert o.airline == "American" and o.carrier == "AA"
        assert o.flight_numbers == ("AA 3542", "AA 817")
        assert o.depart_time == "2026-12-28 16:34"
        assert o.arrive_time == "2026-12-28 22:25"
        assert o.duration_min == 351
        assert o.layovers == ("ORD",) and o.stops == 1

    def test_google_flights_url_is_kept_from_search_metadata(self):
        urls = {o.url for o in self._real()}
        assert len(urls) == 1
        assert next(iter(urls)).startswith("https://www.google.com/travel/flights?")

    def test_synthetic_fixture_without_metadata_has_no_url(self):
        path = FIXTURES / "SYNTHETIC_dtw_mia_round_trip.json"
        for o in normalize(json.loads(path.read_text()), OBSERVED, DEPART, RET):
            assert o.url is None

    def test_old_records_without_detail_still_load(self):
        from fares.storage import from_record
        o = from_record({"observed_at": "2026-09-16T01:48:00", "depart": "2026-12-29",
                         "ret": "2027-01-01", "price_usd": 348, "carrier": "F9", "stops": 1})
        assert o.flight_numbers == () and o.url is None and o.duration_min is None
