"""Append-only JSONL persistence, partitioned by month.

Deliberately not SQLite: this data lives in git, and a binary file produces
unreadable diffs on every commit. At ~4,500 observations/month the JSONL is
kilobytes, and DuckDB reads it directly for later analysis.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

from .models import Band, Observation
from .sweep import lead_bucket

OBSERVATIONS_DIR = "observations"
ALERTS_FILE = "alerts.jsonl"


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def to_record(obs: Observation) -> dict:
    return {
        "observed_at": _iso(obs.observed_at),
        "depart": _iso(obs.depart),
        "ret": _iso(obs.ret),
        "price_usd": obs.price_usd,
        "carrier": obs.carrier,
        "stops": obs.stops,
        "price_level": obs.price_level,
        "typical_low": obs.typical_low,
        "typical_high": obs.typical_high,
        "airline": obs.airline,
        "flight_numbers": list(obs.flight_numbers),
        "depart_time": obs.depart_time,
        "arrive_time": obs.arrive_time,
        "duration_min": obs.duration_min,
        "layovers": list(obs.layovers),
        "url": obs.url,
        "ret_airline": obs.ret_airline,
        "ret_flight_numbers": list(obs.ret_flight_numbers),
        "ret_depart_time": obs.ret_depart_time,
        "ret_arrive_time": obs.ret_arrive_time,
        "ret_duration_min": obs.ret_duration_min,
        "ret_layovers": list(obs.ret_layovers),
        "ret_stops": obs.ret_stops,
    }


def from_record(rec: dict) -> Observation:
    return Observation(
        observed_at=datetime.fromisoformat(rec["observed_at"]),
        depart=date.fromisoformat(rec["depart"]),
        ret=date.fromisoformat(rec["ret"]) if rec.get("ret") else None,
        price_usd=rec["price_usd"],
        carrier=rec["carrier"],
        stops=rec["stops"],
        price_level=rec.get("price_level"),
        typical_low=rec.get("typical_low"),
        typical_high=rec.get("typical_high"),
        airline=rec.get("airline"),
        flight_numbers=tuple(rec.get("flight_numbers") or ()),
        depart_time=rec.get("depart_time"),
        arrive_time=rec.get("arrive_time"),
        duration_min=rec.get("duration_min"),
        layovers=tuple(rec.get("layovers") or ()),
        url=rec.get("url"),
        ret_airline=rec.get("ret_airline"),
        ret_flight_numbers=tuple(rec.get("ret_flight_numbers") or ()),
        ret_depart_time=rec.get("ret_depart_time"),
        ret_arrive_time=rec.get("ret_arrive_time"),
        ret_duration_min=rec.get("ret_duration_min"),
        ret_layovers=tuple(rec.get("ret_layovers") or ()),
        ret_stops=rec.get("ret_stops"),
    )


def partition_for(moment: datetime) -> str:
    return f"{moment:%Y-%m}.jsonl"


def append(observations: Iterable[Observation], root: Path) -> int:
    """Append observations to their month partitions. Returns rows written."""
    by_partition: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_partition[partition_for(obs.observed_at)].append(obs)

    written = 0
    target = root / OBSERVATIONS_DIR
    target.mkdir(parents=True, exist_ok=True)
    for name, rows in by_partition.items():
        with (target / name).open("a", encoding="utf-8") as fh:
            for obs in rows:
                fh.write(json.dumps(to_record(obs), separators=(",", ":")) + "\n")
                written += 1
    return written


def read_all(root: Path) -> Iterator[Observation]:
    """Stream every stored observation, oldest partition first."""
    target = root / OBSERVATIONS_DIR
    if not target.exists():
        return
    for path in sorted(target.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield from_record(json.loads(line))


def itinerary_key(obs: Observation) -> tuple[str, str | None]:
    """Identity of an itinerary, independent of when it was observed."""
    return (obs.depart.isoformat(), obs.ret.isoformat() if obs.ret else None)


def history_by_itinerary(root: Path) -> dict[tuple[str, str | None], list[int]]:
    """Prices grouped by exact itinerary -- the comparison set for a fixed trip.

    For a known trip this beats lead-time bucketing: "is Dec 30 cheap against
    what Dec 30 has been going for" is a sharper question than "is this cheap
    for something 40-ish days out", which pools unrelated travel dates.
    """
    buckets: dict[tuple[str, str | None], list[int]] = defaultdict(list)
    for obs in read_all(root):
        buckets[itinerary_key(obs)].append(obs.price_usd)
    return dict(buckets)


def history_by_bucket(root: Path, bands: tuple[Band, ...]) -> dict[int, list[int]]:
    """Prices grouped by lead-time bucket -- the comparison set for percentiles.

    Grouping matters: a $180 fare 3 days out and a $180 fare 80 days out are
    not comparable observations, and pooling them would wash out the signal.
    """
    buckets: dict[int, list[int]] = defaultdict(list)
    for obs in read_all(root):
        buckets[lead_bucket(obs.lead_days, bands)].append(obs.price_usd)
    return dict(buckets)


def record_alert(obs: Observation, price: int, sent_at: datetime, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ALERTS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "sent_at": _iso(sent_at),
            "depart": _iso(obs.depart),
            "ret": _iso(obs.ret),
            "price_usd": price,
        }, separators=(",", ":")) + "\n")


def read_alerts(root: Path) -> list[dict]:
    path = root / ALERTS_FILE
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
