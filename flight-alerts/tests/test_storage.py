from datetime import date, datetime, timedelta

from fares.config import BANDS
from fares.models import Observation
from fares.storage import (
    append,
    from_record,
    history_by_bucket,
    partition_for,
    read_alerts,
    read_all,
    record_alert,
    to_record,
)


def obs(price=200, observed=datetime(2026, 9, 11, 12), lead=30, carrier="DL", ret=True):
    depart = observed.date() + timedelta(days=lead)
    return Observation(observed_at=observed, depart=depart,
                       ret=depart + timedelta(days=5) if ret else None,
                       price_usd=price, carrier=carrier, stops=0,
                       price_level="typical", typical_low=180, typical_high=320)


class TestRoundTrip:
    def test_record_round_trips_losslessly(self):
        o = obs()
        assert from_record(to_record(o)) == o

    def test_one_way_round_trips(self):
        o = obs(ret=False)
        assert from_record(to_record(o)) == o

    def test_missing_optional_fields_tolerated(self):
        rec = to_record(obs())
        for key in ("price_level", "typical_low", "typical_high"):
            rec.pop(key)
        assert from_record(rec).price_level is None


class TestPartitioning:
    def test_partition_is_monthly(self):
        assert partition_for(datetime(2026, 9, 11)) == "2026-09.jsonl"

    def test_observations_split_across_month_boundary(self, tmp_path):
        append([obs(observed=datetime(2026, 9, 30, 23)),
                obs(observed=datetime(2026, 10, 1, 1))], tmp_path)
        names = {p.name for p in (tmp_path / "observations").glob("*.jsonl")}
        assert names == {"2026-09.jsonl", "2026-10.jsonl"}


class TestAppendAndRead:
    def test_append_returns_rows_written(self, tmp_path):
        assert append([obs(), obs(price=210)], tmp_path) == 2

    def test_appends_accumulate_rather_than_truncate(self, tmp_path):
        append([obs(price=200)], tmp_path)
        append([obs(price=210)], tmp_path)
        assert [o.price_usd for o in read_all(tmp_path)] == [200, 210]

    def test_read_all_on_empty_root_yields_nothing(self, tmp_path):
        assert list(read_all(tmp_path)) == []

    def test_read_all_spans_partitions_in_order(self, tmp_path):
        append([obs(price=300, observed=datetime(2026, 10, 1, 1)),
                obs(price=200, observed=datetime(2026, 9, 30, 23))], tmp_path)
        assert [o.price_usd for o in read_all(tmp_path)] == [200, 300]


class TestHistoryBuckets:
    def test_groups_by_lead_bucket(self, tmp_path):
        append([obs(price=100, lead=7), obs(price=200, lead=30), obs(price=300, lead=80)],
               tmp_path)
        buckets = history_by_bucket(tmp_path, BANDS)
        assert buckets == {0: [100], 1: [200], 2: [300]}

    def test_same_bucket_accumulates(self, tmp_path):
        append([obs(price=100, lead=20), obs(price=150, lead=45)], tmp_path)
        assert history_by_bucket(tmp_path, BANDS)[1] == [100, 150]


class TestAlertLog:
    def test_alert_round_trips(self, tmp_path):
        o = obs()
        record_alert(o, 195, datetime(2026, 9, 11, 12), tmp_path)
        (entry,) = read_alerts(tmp_path)
        assert entry["price_usd"] == 195
        assert entry["depart"] == o.depart.isoformat()

    def test_no_alert_log_reads_as_empty(self, tmp_path):
        assert read_alerts(tmp_path) == []
