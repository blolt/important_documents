from datetime import date, datetime, timedelta

from fares.models import Decision, Observation
from fares.notify import format_alert, publish

NOW = datetime(2026, 9, 11, 12)


def obs(price=214, carrier="DL", stops=0, lead=64, ret_offset=5,
        typical_low=180, typical_high=320, price_level="low"):
    depart = NOW.date() + timedelta(days=lead)
    return Observation(observed_at=NOW, depart=depart,
                       ret=depart + timedelta(days=ret_offset) if ret_offset else None,
                       price_usd=price, carrier=carrier, stops=stops,
                       price_level=price_level, typical_low=typical_low,
                       typical_high=typical_high)


class TestFormatting:
    def test_title_leads_with_price(self):
        title, _ = format_alert(obs(), Decision(True, "percentile:0.02<=0.1", 214, 0.02),
                                "DTW", "MIA")
        assert title.startswith("$214 DTW-MIA")

    def test_title_includes_nights(self):
        title, _ = format_alert(obs(ret_offset=5), Decision(True, "x", 214), "DTW", "MIA")
        assert "+5n" in title

    def test_one_way_omits_nights(self):
        title, body = format_alert(obs(ret_offset=0), Decision(True, "x", 214),
                                   "DTW", "MIA")
        assert "+" not in title
        assert "one way" in body

    def test_body_states_nonstop(self):
        _, body = format_alert(obs(stops=0), Decision(True, "x", 214), "DTW", "MIA")
        assert "nonstop" in body

    def test_body_states_stop_count(self):
        _, body = format_alert(obs(stops=1), Decision(True, "x", 214), "DTW", "MIA")
        assert "1 stop" in body

    def test_percentile_rendered_as_beat_percentage(self):
        _, body = format_alert(obs(), Decision(True, "x", 214, 0.05), "DTW", "MIA")
        assert "95%" in body

    def test_percentile_omitted_on_cold_start(self):
        _, body = format_alert(obs(), Decision(True, "cold_start:x", 214, None),
                               "DTW", "MIA")
        assert "Cheaper than" not in body

    def test_google_range_included_when_present(self):
        _, body = format_alert(obs(), Decision(True, "x", 214), "DTW", "MIA")
        assert "$180" in body and "$320" in body

    def test_google_range_omitted_when_absent(self):
        _, body = format_alert(obs(typical_low=None, typical_high=None),
                               Decision(True, "x", 214), "DTW", "MIA")
        assert "typical range" not in body

    def test_bag_fee_surfaced_when_it_changed_the_price(self):
        # The reader must not think the ticket itself costs the adjusted price.
        _, body = format_alert(obs(price=139, carrier="NK"),
                               Decision(True, "x", 214), "DTW", "MIA")
        assert "$139 fare + $75 bag" in body

    def test_no_bag_line_when_price_unadjusted(self):
        _, body = format_alert(obs(price=214), Decision(True, "x", 214), "DTW", "MIA")
        assert "bag)" not in body

    def test_reason_included_for_debuggability(self):
        _, body = format_alert(obs(), Decision(True, "percentile:0.02<=0.1", 214, 0.02),
                               "DTW", "MIA")
        assert "percentile:0.02<=0.1" in body


class TestPublish:
    def test_posts_to_the_topic_url_with_title_header(self):
        calls = []
        publish("my-topic", "T", "B", post=lambda u, d, h: calls.append((u, d, h)))
        url, data, headers = calls[0]
        assert url == "https://ntfy.sh/my-topic"
        assert data == b"B" and headers["Title"] == "T"
