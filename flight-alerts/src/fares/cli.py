"""Command-line entry points.

    python -m fares plan              # today's queries + budget, no network
    python -m fares sweep --dry-run   # full pipeline against a fixture, no key
    python -m fares sweep             # the real thing
    python -m fares status            # what we've collected so far
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, notify, serpapi, storage
from .decide import select_alerts, should_alert
from .normalize import MalformedResponse, cheapest, normalize
from .sweep import daily_estimate, due_dates_per_day, monthly_estimate, select_queries, validate_budget

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
FIXTURE = ROOT / "tests" / "fixtures" / "SYNTHETIC_dtw_mia_round_trip.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def cmd_plan(args) -> int:
    cfg = config.PRODUCTION
    validate_budget(cfg)
    queries = select_queries(_now().date(), cfg)
    print(f"{config.ORIGIN} -> {config.DESTINATION}  ({_now().date()})")
    print(f"  {due_dates_per_day(cfg)} departure dates x {cfg.queries_per_date} return "
          f"offsets x {cfg.calls_per_query} call(s) = {len(queries) * cfg.calls_per_query} calls today")
    print(f"  ~{monthly_estimate(cfg)}/month of {cfg.monthly_budget} budgeted "
          f"({monthly_estimate(cfg) / cfg.monthly_budget:.0%})")
    if args.verbose:
        for q in queries:
            nights = f" +{(q.ret - q.depart).days}n" if q.ret else ""
            print(f"    {q.depart}{nights}")
    return 0


def cmd_status(args) -> int:
    observations = list(storage.read_all(DATA))
    alerts = storage.read_alerts(DATA)
    print(f"observations: {len(observations)}")
    if observations:
        prices = [o.price_usd for o in observations]
        print(f"  price range: ${min(prices)}–${max(prices)}")
        print(f"  first: {min(o.observed_at for o in observations)}")
        print(f"  last:  {max(o.observed_at for o in observations)}")
        buckets = storage.history_by_bucket(DATA, config.BANDS)
        for idx, band in enumerate(config.BANDS):
            n = len(buckets.get(idx, []))
            ready = "ready" if n >= 5 else "cold start"
            print(f"  <={band.max_lead_days}d lead: {n} obs ({ready})")
    print(f"alerts sent: {len(alerts)}")
    return 0


def cmd_sweep(args) -> int:
    cfg = config.PRODUCTION
    validate_budget(cfg)
    policy = config.load_policy(ROOT / "policy.json")
    topic = os.environ.get("NTFY_TOPIC")
    api_key = os.environ.get("SERPAPI_KEY")

    if not args.dry_run and not api_key:
        print("SERPAPI_KEY not set. Use --dry-run to exercise the pipeline "
              "against a fixture without a key.", file=sys.stderr)
        return 2

    now = _now()
    queries = select_queries(now.date(), cfg)
    if args.limit:
        queries = queries[:args.limit]

    fixture = json.loads(FIXTURE.read_text()) if args.dry_run else None
    history = storage.history_by_bucket(DATA, config.BANDS)
    alerts = storage.read_alerts(DATA)

    collected, candidates, failed = [], [], 0
    for query in queries:
        try:
            payload = fixture if args.dry_run else serpapi.fetch(
                query, api_key, config.ORIGIN, config.DESTINATION)
            observations = normalize(payload, now, query.depart, query.ret)
        except serpapi.QuotaExceeded as exc:
            # Stop the sweep rather than burn the rest of the month on errors.
            print(f"quota exhausted, stopping: {exc}", file=sys.stderr)
            break
        except (MalformedResponse, OSError, ValueError) as exc:
            failed += 1
            print(f"  {query.depart}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        collected.extend(observations)

        best = cheapest(observations)
        if best is not None:
            candidates.append(
                (best, should_alert(best, history, policy, alerts, now, config.BANDS)))

    # Decide across the whole sweep so the cap keeps the cheapest fares,
    # not whichever happened to be fetched first.
    selected = select_alerts(candidates, policy)
    suppressed = sum(1 for _, d in candidates if d.alert) - len(selected)

    for obs, decision in selected:
        title, body = notify.format_alert(obs, decision, config.ORIGIN, config.DESTINATION)
        print(f"ALERT {title}")
        if args.dry_run or not topic:
            print(body)
        else:
            notify.publish(topic, title, body)
        if not args.dry_run:
            storage.record_alert(obs, decision.effective_price_usd or obs.price_usd,
                                 now, DATA)
    fired = len(selected)
    if suppressed:
        print(f"({suppressed} further alerts suppressed by "
              f"max_alerts_per_sweep={policy.max_alerts_per_sweep})")

    if args.dry_run:
        print(f"\ndry run: {len(queries)} queries, {len(collected)} observations, "
              f"{fired} alerts, {failed} failures. Nothing written.")
        return 0

    written = storage.append(collected, DATA)
    print(f"{len(queries)} queries, {written} observations stored, "
          f"{fired} alerts, {failed} failures")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fares", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="show today's planned queries and budget")
    p.add_argument("-v", "--verbose", action="store_true", help="list every date")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("sweep", help="fetch, store, and alert")
    p.add_argument("--dry-run", action="store_true",
                   help="use a fixture instead of the API; writes nothing")
    p.add_argument("--limit", type=int, help="cap queries (useful for a first live test)")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("status", help="summarize collected data")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)
