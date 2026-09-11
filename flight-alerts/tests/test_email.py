from datetime import date, datetime, timedelta

import pytest

from fares.email_alert import (
    MissingEmailConfig,
    SmtpConfig,
    build_message,
    format_digest,
    send_digest,
)
from fares.models import Decision, Observation

NOW = datetime(2026, 9, 11, 12)


def obs(price=214, carrier="DL", stops=0, typical_low=180, typical_high=320,
        depart=date(2026, 12, 30), ret=date(2027, 1, 3)):
    return Observation(observed_at=NOW, depart=depart, ret=ret, price_usd=price,
                       carrier=carrier, stops=stops, price_level="low",
                       typical_low=typical_low, typical_high=typical_high)


def cand(price=214, rank=0.03, **kw):
    return (obs(price=price, **kw), Decision(True, "percentile", price, rank))


CONFIG = SmtpConfig(host="smtp.gmail.com", port=587, user="me@gmail.com",
                    password="pw", sender="me@gmail.com",
                    recipients=("a@x.com", "b@y.com"))


class TestSmtpConfigFromEnv:
    def test_reads_a_complete_env(self):
        c = SmtpConfig.from_env({"SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
                                 "ALERT_RECIPIENTS": "a@x.com, b@y.com"})
        assert c.recipients == ("a@x.com", "b@y.com")
        assert (c.host, c.port) == ("smtp.gmail.com", 587)

    def test_sender_defaults_to_the_authenticated_user(self):
        # Gmail rejects a From that isn't the logged-in account.
        c = SmtpConfig.from_env({"SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
                                 "ALERT_RECIPIENTS": "a@x.com"})
        assert c.sender == "me@gmail.com"

    def test_explicit_sender_is_respected(self):
        c = SmtpConfig.from_env({"SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "pw",
                                 "ALERT_RECIPIENTS": "a@x.com",
                                 "ALERT_FROM": "trips@example.com"})
        assert c.sender == "trips@example.com"

    def test_blank_and_whitespace_recipients_are_dropped(self):
        c = SmtpConfig.from_env({"SMTP_USER": "u", "SMTP_PASSWORD": "p",
                                 "ALERT_RECIPIENTS": "a@x.com, ,  , b@y.com,"})
        assert c.recipients == ("a@x.com", "b@y.com")

    @pytest.mark.parametrize("env", [
        {},
        {"SMTP_USER": "u", "SMTP_PASSWORD": "p"},                 # no recipients
        {"SMTP_USER": "u", "ALERT_RECIPIENTS": "a@x.com"},        # no password
        {"SMTP_PASSWORD": "p", "ALERT_RECIPIENTS": "a@x.com"},    # no user
        {"SMTP_USER": "u", "SMTP_PASSWORD": "p", "ALERT_RECIPIENTS": " , "},
    ])
    def test_incomplete_env_raises(self, env):
        with pytest.raises(MissingEmailConfig):
            SmtpConfig.from_env(env)

    def test_error_names_what_is_missing(self):
        with pytest.raises(MissingEmailConfig) as e:
            SmtpConfig.from_env({"SMTP_USER": "u"})
        assert "SMTP_PASSWORD" in str(e.value) and "ALERT_RECIPIENTS" in str(e.value)


class TestDigest:
    def test_subject_leads_with_the_cheapest_fare(self):
        subject, _, _ = format_digest([cand(280), cand(198), cand(240)],
                                      "DTW", "MIA", "NYE 2026")
        assert subject.startswith("$198 DTW→MIA")

    def test_subject_counts_the_extras(self):
        subject, _, _ = format_digest([cand(198), cand(240), cand(280)],
                                      "DTW", "MIA", "NYE 2026")
        assert "(+2 more)" in subject

    def test_single_fare_subject_has_no_counter(self):
        subject, _, _ = format_digest([cand(198)], "DTW", "MIA", "NYE 2026")
        assert "more" not in subject

    def test_trip_label_appears_in_subject_and_body(self):
        subject, text, html = format_digest([cand()], "DTW", "MIA", "NYE 2026")
        assert "NYE 2026" in subject and "NYE 2026" in text and "NYE 2026" in html

    def test_every_fare_appears_in_both_parts(self):
        _, text, html = format_digest([cand(198), cand(240), cand(280)],
                                      "DTW", "MIA", "NYE 2026")
        for price in ("198", "240", "280"):
            assert f"${price}" in text and f"${price}" in html

    def test_nights_are_computed_and_shown(self):
        _, text, _ = format_digest([cand()], "DTW", "MIA", "NYE 2026")
        assert "4 nights" in text  # Dec 30 -> Jan 3

    def test_bag_adjusted_price_is_explained(self):
        # The group must not think the ticket itself costs the adjusted price.
        c = (obs(price=139, carrier="NK"), Decision(True, "x", 214, 0.02))
        _, text, _ = format_digest([c], "DTW", "MIA", "NYE 2026")
        assert "$139 fare + $75 bag" in text

    def test_nonstop_and_connecting_are_distinguished(self):
        _, text, _ = format_digest([cand(stops=0), cand(price=180, stops=1)],
                                   "DTW", "MIA", "NYE 2026")
        assert "nonstop" in text and "1 stop" in text

    def test_google_range_omitted_when_absent(self):
        _, text, _ = format_digest([cand(typical_low=None, typical_high=None)],
                                   "DTW", "MIA", "NYE 2026")
        assert "typical range" not in text

    def test_empty_digest_is_an_error_not_an_empty_email(self):
        with pytest.raises(ValueError):
            format_digest([], "DTW", "MIA", "NYE 2026")

    def test_html_is_self_contained(self):
        # Email clients block external CSS; styles must be inline.
        _, _, html = format_digest([cand()], "DTW", "MIA", "NYE 2026")
        assert "<link" not in html and "</html>" in html


class TestMessage:
    def test_recipients_all_land_in_the_to_header(self):
        msg = build_message(CONFIG, "S", "T", "<p>H</p>")
        assert msg["To"] == "a@x.com, b@y.com"

    def test_from_is_the_configured_sender(self):
        assert build_message(CONFIG, "S", "T", "<p>H</p>")["From"] == "me@gmail.com"

    def test_message_is_multipart_with_a_plain_text_fallback(self):
        msg = build_message(CONFIG, "S", "T", "<p>H</p>")
        types = {part.get_content_type() for part in msg.walk()}
        assert "text/plain" in types and "text/html" in types

    def test_password_never_appears_in_the_message(self):
        assert "pw" not in build_message(CONFIG, "S", "T", "<p>H</p>").as_string()


class TestSendDigest:
    def test_sends_once_for_the_whole_sweep(self):
        # One digest, not one email per fare.
        sent = []
        send_digest(CONFIG, [cand(198), cand(240), cand(280)], "DTW", "MIA",
                    "NYE 2026", send=lambda c, m: sent.append(m))
        assert len(sent) == 1

    def test_returns_the_subject_it_sent(self):
        subject = send_digest(CONFIG, [cand(198)], "DTW", "MIA", "NYE 2026",
                              send=lambda c, m: None)
        assert "$198" in subject
