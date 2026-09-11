"""Command-line entry points.

    python -m fares plan                # the itinerary grid + budget, no network
    python -m fares sweep --dry-run     # full pipeline on fixtures, no key, sends nothing
    python -m fares sweep               # the real thing
    python -m fares status              # what we've collected, per itinerary
    python -m fares test-email          # send one sample digest to the list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, email_alert, notify, serpapi, storage
from .decide import select_alerts, should_alert
from .normalize import MalformedResponse, cheapest, normalize
from .storage import itinerary_key
from .sweep import (
    select_target_queries,
    sweep_interval_hours,
    target_daily_estimate,
    target_monthly_estimate,
    validate_target_budget,
)

ROOT = Path(__file__).resolve().parents[2]
# Overridable so CI can point the panel at a worktree on a dedicated data
# branch, keeping ~1,300 automated data commits out of the code history.
DATA = Path(os.environ.get("FARES_DATA_DIR") or ROOT / "data")
FIXTURE = ROOT / "tests" / "fixtures" / "SYNTHETIC_dtw_mia_round_trip.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def cmd_plan(args) -> int:
    trip = config.ACTIVE
    validate_target_budget(trip)
    queries = select_target_queries(trip)
    print(f"{config.ORIGIN} -> {config.DESTINATION} · {config.TRIP_LABEL}")
    print(f"  {len(queries)} itineraries, swept every "
          f"{sweep_interval_hours(trip):.0f}h ({trip.sweeps_per_day}x/day)")
    print(f"  {target_daily_estimate(trip)} calls/day, "
          f"~{target_monthly_estimate(trip)}/month of {trip.monthly_budget} "
          f"({target_monthly_estimate(trip) / trip.monthly_budget:.0%})")
    if args.verbose:
        for q in queries:
            print(f"    {q.depart} -> {q.ret}  ({(q.ret - q.depart).days}n)")
    return 0


def cmd_status(args) -> int:
    observations = list(storage.read_all(DATA))
    alerts = storage.read_alerts(DATA)
    print(f"observations: {len(observations)}")
    if observations:
        print(f"  first: {min(o.observed_at for o in observations)}")
        print(f"  last:  {max(o.observed_at for o in observations)}")
        history = storage.history_by_itinerary(DATA)
        policy = config.load_policy(ROOT / "policy.json")
        for q in select_target_queries(config.ACTIVE):
            prices = history.get((q.depart.isoformat(), q.ret.isoformat()), [])
            if not prices:
                print(f"  {q.depart} -> {q.ret}: no readings")
                continue
            ready = "ready" if len(prices) >= policy.min_history else "cold start"
            print(f"  {q.depart} -> {q.ret}: {len(prices):>4} readings, "
                  f"${min(prices)}–${max(prices)} ({ready})")
    print(f"alerts sent: {len(alerts)}")
    return 0


def _deliver(selected, args) -> str | None:
    """Send the digest by whatever channel is configured. Returns a description."""
    if args.dry_run:
        subject, text, _ = email_alert.format_digest(
            selected, config.ORIGIN, config.DESTINATION, config.TRIP_LABEL)
        print(f"\n--- would email: {subject} ---\n{text}")
        return None

    try:
        smtp = email_alert.SmtpConfig.from_env()
    except email_alert.MissingEmailConfig as exc:
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            print(f"no delivery channel configured: {exc}", file=sys.stderr)
            return None
        # ntfy fallback: one message per fare, since it has no digest format.
        for obs, decision in selected:
            title, body = notify.format_alert(
                obs, decision, config.ORIGIN, config.DESTINATION)
            notify.publish(topic, title, body)
        return f"ntfy topic ({len(selected)} messages)"

    subject = email_alert.send_digest(
        smtp, selected, config.ORIGIN, config.DESTINATION, config.TRIP_LABEL)
    return f"email to {len(smtp.recipients)} recipient(s): {subject}"


def cmd_sweep(args) -> int:
    trip = config.ACTIVE
    validate_target_budget(trip)
    policy = config.load_policy(ROOT / "policy.json")
    api_key = os.environ.get("SERPAPI_KEY")

    if not args.dry_run and not api_key:
        print("SERPAPI_KEY not set. Use --dry-run to exercise the pipeline "
              "against a fixture without a key.", file=sys.stderr)
        return 2

    now = _now()
    queries = select_target_queries(trip)
    if args.limit:
        queries = queries[:args.limit]

    fixture = json.loads(FIXTURE.read_text()) if args.dry_run else None
    history = storage.history_by_itinerary(DATA)
    alerts = storage.read_alerts(DATA)

    collected, candidates, failed = [], [], 0
    for query in queries:
        try:
            payload = fixture if args.dry_run else serpapi.fetch(
                query, api_key, config.ORIGIN, config.DESTINATION)
            observations = normalize(payload, now, query.depart, query.ret)
        except serpapi.QuotaExceeded as exc:
            print(f"quota exhausted, stopping: {exc}", file=sys.stderr)
            break
        except (MalformedResponse, OSError, ValueError) as exc:
            failed += 1
            print(f"  {query.depart}->{query.ret}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue

        collected.extend(observations)
        best = cheapest(observations)
        if best is not None:
            # Comparable history is this itinerary's own record.
            comparable = history.get(itinerary_key(best), [])
            candidates.append((best, should_alert(best, comparable, policy, alerts, now)))

    selected = select_alerts(candidates, policy)
    suppressed = sum(1 for _, d in candidates if d.alert) - len(selected)

    if selected:
        sent = _deliver(selected, args)
        if sent:
            print(sent)
        if not args.dry_run:
            for obs, decision in selected:
                storage.record_alert(
                    obs, decision.effective_price_usd or obs.price_usd, now, DATA)
    if suppressed:
        print(f"({suppressed} further alerts held back by "
              f"max_alerts_per_sweep={policy.max_alerts_per_sweep})")

    if args.dry_run:
        print(f"\ndry run: {len(queries)} queries, {len(collected)} observations, "
              f"{len(selected)} alerts. Nothing written, nothing sent.")
        return 0

    written = storage.append(collected, DATA)
    print(f"{len(queries)} queries, {written} observations stored, "
          f"{len(selected)} alerts, {failed} failures")
    return 0


def cmd_test_email(args) -> int:
    """Send one digest built from the fixture, to prove delivery works."""
    try:
        smtp = email_alert.SmtpConfig.from_env()
    except email_alert.MissingEmailConfig as exc:
        print(str(exc), file=sys.stderr)
        return 2

    now = _now()
    query = select_target_queries(config.ACTIVE)[0]
    observations = normalize(json.loads(FIXTURE.read_text()), now,
                             query.depart, query.ret)
    policy = config.load_policy(ROOT / "policy.json")
    candidates = [(o, should_alert(o, [], policy, [], now)) for o in observations]
    selected = select_alerts(candidates, policy)
    if not selected:
        print("fixture produced no alerts; nothing to send", file=sys.stderr)
        return 1

    print(f"sending sample digest to: {', '.join(smtp.recipients)}")
    subject = email_alert.send_digest(smtp, selected, config.ORIGIN,
                                      config.DESTINATION, config.TRIP_LABEL)
    print(f"sent: {subject}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fares", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="show the itinerary grid and budget")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("sweep", help="fetch, store, and alert")
    p.add_argument("--dry-run", action="store_true",
                   help="use a fixture; writes nothing, sends nothing")
    p.add_argument("--limit", type=int, help="cap queries for a cheap first live test")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("status", help="summarize collected data")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("test-email", help="send one sample digest to the list")
    p.set_defaults(func=cmd_test_email)

    args = parser.parse_args(argv)
    return args.func(args)
