from datetime import date, datetime, timedelta

import pytest

from fares.decide import effective_price, percentile_rank, should_alert
from fares.models import Observation, Policy

NOW = datetime(2026, 9, 11, 12, 0)


def obs(price=200, carrier="DL", stops=0, lead=30, price_level=None,
        typical_low=None, ret_offset=5):
    depart = NOW.date() + timedelta(days=lead)
    return Observation(
        observed_at=NOW, depart=depart, ret=depart + timedelta(days=ret_offset),
        price_usd=price, carrier=carrier, stops=stops,
        price_level=price_level, typical_low=typical_low,
    )


def policy(**kw):
    base = dict(ceiling_usd=300, percentile=0.10, min_history=5)
    base.update(kw)
    return Policy(**base)


CHEAP_HISTORY = list(range(200, 300))  # 100 prior readings, $200-$299


class TestPercentileRank:
    def test_cheapest_ever_ranks_at_floor(self):
        assert percentile_rank(100, [200, 300, 400]) == 0.0

    def test_most_expensive_ranks_at_ceiling(self):
        assert percentile_rank(500, [200, 300, 400]) == 1.0

    def test_empty_history_is_not_treated_as_cheap(self):
        # Must not return 0.0 -- that would make every cold-start fare "cheapest ever".
        assert percentile_rank(100, []) == 1.0

    def test_rank_is_inclusive_of_equal_prices(self):
        assert percentile_rank(200, [200, 300, 400]) == pytest.approx(1 / 3)


class TestEffectivePrice:
    def test_no_fee_configured_passes_through(self):
        assert effective_price(obs(price=59, carrier="NK"), policy()) == 59

    def test_bag_fee_is_added_for_matching_carrier(self):
        p = policy(bag_fee_usd={"NK": 75})
        assert effective_price(obs(price=59, carrier="NK"), p) == 134

    def test_bag_fee_does_not_leak_to_other_carriers(self):
        p = policy(bag_fee_usd={"NK": 75})
        assert effective_price(obs(price=59, carrier="DL"), p) == 59


class TestGates:
    def test_cheap_fare_with_history_alerts(self):
        d = should_alert(obs(price=180), CHEAP_HISTORY, policy(), [], NOW)
        assert d.alert and d.percentile_rank == 0.0

    def test_above_ceiling_is_rejected(self):
        d = should_alert(obs(price=400), CHEAP_HISTORY, policy(), [], NOW)
        assert not d.alert and "above_ceiling" in d.reason

    def test_bag_fee_can_push_a_fare_over_the_ceiling(self):
        # The whole point of normalizing: a $290 Spirit fare is not under a $300 ceiling.
        p = policy(bag_fee_usd={"NK": 75})
        d = should_alert(obs(price=290, carrier="NK"), CHEAP_HISTORY, p, [], NOW)
        assert not d.alert and "above_ceiling" in d.reason
        assert d.effective_price_usd == 365

    def test_excluded_carrier_is_rejected(self):
        p = policy(excluded_carriers=frozenset({"NK"}))
        d = should_alert(obs(price=100, carrier="NK"), CHEAP_HISTORY, p, [], NOW)
        assert not d.alert and "carrier_excluded" in d.reason

    def test_connection_rejected_when_nonstop_only(self):
        p = policy(nonstop_only=True)
        d = should_alert(obs(price=100, stops=1), CHEAP_HISTORY, p, [], NOW)
        assert not d.alert and "not_nonstop" in d.reason

    def test_connection_allowed_when_nonstop_not_required(self):
        d = should_alert(obs(price=100, stops=1), CHEAP_HISTORY, policy(), [], NOW)
        assert d.alert

    def test_google_high_price_level_vetoes_an_otherwise_cheap_fare(self):
        d = should_alert(obs(price=180, price_level="high"), CHEAP_HISTORY,
                         policy(), [], NOW)
        assert not d.alert and d.reason == "google_price_level_high"

    def test_expensive_relative_to_history_is_rejected(self):
        d = should_alert(obs(price=295), CHEAP_HISTORY, policy(), [], NOW)
        assert not d.alert and d.reason.startswith("percentile:")


class TestColdStart:
    def test_insufficient_history_does_not_alert_without_google_signal(self):
        d = should_alert(obs(price=100), [200, 210], policy(), [], NOW)
        assert not d.alert and "insufficient_history" in d.reason

    def test_below_google_typical_low_alerts_despite_thin_history(self):
        d = should_alert(obs(price=150, typical_low=200), [200, 210],
                         policy(), [], NOW)
        assert d.alert and d.reason == "cold_start:below_google_typical_low"

    def test_above_google_typical_low_does_not_alert_on_thin_history(self):
        d = should_alert(obs(price=250, typical_low=200), [200, 210],
                         policy(), [], NOW)
        assert not d.alert



class TestDebounce:
    def _alert_log(self, o, hours_ago, price=180):
        return [{"sent_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
                 "depart": o.depart.isoformat(),
                 "ret": o.ret.isoformat() if o.ret else None,
                 "price_usd": price}]

    def test_recent_alert_suppresses_a_repeat(self):
        o = obs(price=180)
        d = should_alert(o, CHEAP_HISTORY, policy(), self._alert_log(o, 2), NOW)
        assert not d.alert and "debounced" in d.reason

    def test_stale_alert_does_not_suppress(self):
        o = obs(price=180)
        d = should_alert(o, CHEAP_HISTORY, policy(), self._alert_log(o, 48), NOW)
        assert d.alert

    def test_alert_for_a_different_departure_does_not_suppress(self):
        other = obs(price=180, lead=31)
        d = should_alert(obs(price=180, lead=30), CHEAP_HISTORY, policy(),
                         self._alert_log(other, 2), NOW)
        assert d.alert

    def test_alert_for_a_different_return_does_not_suppress(self):
        # Same departure, different trip length -- a distinct itinerary.
        other = obs(price=180, lead=30, ret_offset=3)
        d = should_alert(obs(price=180, lead=30, ret_offset=5), CHEAP_HISTORY,
                         policy(), self._alert_log(other, 2), NOW)
        assert d.alert


class TestAlertCap:
    def _candidates(self, prices):
        out = []
        for i, price in enumerate(prices):
            o = obs(price=price, lead=30 + i)
            out.append((o, should_alert(o, CHEAP_HISTORY, policy(), [], NOW)))
        return out

    def test_cap_keeps_only_the_allowed_count(self):
        from fares.decide import select_alerts
        selected = select_alerts(self._candidates([150, 160, 170, 180, 190, 200]),
                                 policy(max_alerts_per_sweep=3))
        assert len(selected) == 3

    def test_cap_keeps_the_cheapest_not_the_first_seen(self):
        from fares.decide import select_alerts
        selected = select_alerts(self._candidates([250, 100, 240, 110]),
                                 policy(max_alerts_per_sweep=2))
        assert [d.effective_price_usd for _, d in selected] == [100, 110]

    def test_non_firing_candidates_are_excluded(self):
        from fares.decide import select_alerts
        cands = self._candidates([150]) + self._candidates([999])
        selected = select_alerts(cands, policy(max_alerts_per_sweep=5))
        assert len(selected) == 1

    def test_cap_ranks_by_effective_price_including_bag_fees(self):
        from fares.decide import select_alerts
        p = policy(max_alerts_per_sweep=1, bag_fee_usd={"NK": 100})
        spirit = obs(price=120, carrier="NK", lead=30)
        delta = obs(price=150, carrier="DL", lead=31)
        cands = [(o, should_alert(o, CHEAP_HISTORY, p, [], NOW))
                 for o in (spirit, delta)]
        (kept, _), = select_alerts(cands, p)
        assert kept.carrier == "DL", "a $120 fare plus a $100 bag is not cheaper than $150"

    def test_empty_candidate_list(self):
        from fares.decide import select_alerts
        assert select_alerts([], policy()) == []


class TestRenotifyOnFurtherDrop:
    def _log(self, o, hours_ago, price):
        return [{"sent_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
                 "depart": o.depart.isoformat(),
                 "ret": o.ret.isoformat() if o.ret else None,
                 "price_usd": price}]

    def test_same_price_inside_window_stays_suppressed(self):
        o = obs(price=200)
        d = should_alert(o, CHEAP_HISTORY, policy(), self._log(o, 2, 200), NOW)
        assert not d.alert and "debounced" in d.reason

    def test_trivial_drop_stays_suppressed(self):
        o = obs(price=190)
        d = should_alert(o, CHEAP_HISTORY, policy(renotify_drop_usd=25),
                         self._log(o, 2, 200), NOW)
        assert not d.alert, "a $10 drop should not re-mail the group"

    def test_material_drop_re_alerts_inside_the_window(self):
        o = obs(price=140)
        d = should_alert(o, CHEAP_HISTORY, policy(renotify_drop_usd=25),
                         self._log(o, 2, 200), NOW)
        assert d.alert, "a $60 drop is news even two hours later"

    def test_drop_exactly_at_the_threshold_fires(self):
        # renotify_drop_usd is "at least this much below", so $25 qualifies.
        o = obs(price=175)
        d = should_alert(o, CHEAP_HISTORY, policy(renotify_drop_usd=25),
                         self._log(o, 2, 200), NOW)
        assert d.alert

    def test_one_dollar_short_of_the_threshold_does_not_fire(self):
        o = obs(price=176)
        d = should_alert(o, CHEAP_HISTORY, policy(renotify_drop_usd=25),
                         self._log(o, 2, 200), NOW)
        assert not d.alert

    def test_threshold_measured_against_the_lowest_recent_alert(self):
        # Two alerts in the window; the $150 one is the bar to beat, not $200.
        o = obs(price=140)
        log = self._log(o, 4, 200) + self._log(o, 2, 150)
        d = should_alert(o, CHEAP_HISTORY, policy(renotify_drop_usd=25), log, NOW)
        assert not d.alert

    def test_price_rise_inside_the_window_stays_suppressed(self):
        o = obs(price=260)
        d = should_alert(o, CHEAP_HISTORY, policy(), self._log(o, 2, 200), NOW)
        assert not d.alert


class TestGatesDisabled:
    """policy.json can switch off Google's veto and the history gate, leaving
    ceiling + debounce + cap as the whole policy."""

    OFF = dict(percentile=None, veto_google_high=False)

    def test_google_high_is_ignored_when_veto_off(self):
        d = should_alert(obs(price=180, price_level="high"), CHEAP_HISTORY,
                         policy(**self.OFF), [], NOW)
        assert d.alert and d.reason == "under_ceiling"

    def test_no_history_gate_alerts_on_thin_history(self):
        d = should_alert(obs(price=250), [200, 210], policy(**self.OFF), [], NOW)
        assert d.alert and d.reason == "under_ceiling"

    def test_expensive_relative_to_history_still_alerts(self):
        d = should_alert(obs(price=295), CHEAP_HISTORY, policy(**self.OFF), [], NOW)
        assert d.alert and d.reason == "under_ceiling"

    def test_ceiling_still_applies(self):
        d = should_alert(obs(price=301), [], policy(**self.OFF), [], NOW)
        assert not d.alert and d.reason.startswith("above_ceiling")

    def test_debounce_still_applies(self):
        o = obs(price=250)
        recent = [dict(depart=o.depart.isoformat(), ret=o.ret.isoformat(),
                       price_usd=250, sent_at=(NOW - timedelta(hours=2)).isoformat())]
        d = should_alert(o, [], policy(**self.OFF), recent, NOW)
        assert not d.alert and d.reason.startswith("debounced")

    def test_veto_alone_can_stay_on_with_history_gate_off(self):
        d = should_alert(obs(price=180, price_level="high"), [],
                         policy(percentile=None, veto_google_high=True), [], NOW)
        assert not d.alert and d.reason == "google_price_level_high"
