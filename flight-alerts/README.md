# DTW → MIA fare alerts

Sweeps Google Flights (via SerpApi) for Detroit→Miami round-trips, stores every
reading as an append-only panel in git, and pushes a phone notification when a
fare is cheap against its own history.

Design rationale and the decisions behind it: **[SPEC.md](SPEC.md)**.

---

## What's done and what's waiting on you

Everything except the two things that need your credentials and your
preferences. 112 tests pass; the full pipeline runs against fixtures.

### 1. SerpApi key — 5 minutes

Sign up for the **Developer** tier ($75/mo, 5,000 searches). The sweep is
budgeted at 4,500/month, validated at startup.

Add it as a repo secret named `SERPAPI_KEY`
(*Settings → Secrets and variables → Actions → New repository secret*).

### 2. ntfy topic — 2 minutes

Install [ntfy](https://ntfy.sh/) on your phone and subscribe to a topic.
**Pick something unguessable** — on ntfy.sh the topic name is the only access
control, so `dtw-mia-alerts` is world-readable and `dtw-mia-7f3a9c21` is not.

Add it as a repo secret named `NTFY_TOPIC`.

### 3. Record a real API response — 5 minutes, and please do this one

```bash
curl "https://serpapi.com/search?engine=google_flights&departure_id=DTW\
&arrival_id=MIA&outbound_date=2026-11-14&return_date=2026-11-19\
&currency=USD&type=1&api_key=$SERPAPI_KEY" \
  > tests/fixtures/real_dtw_mia_round_trip.json
```

`normalize.py` was written against SerpApi's *documented* response shape, not a
recorded one. `tests/test_normalize.py::TestFixtureContract` parametrizes over
every file in `tests/fixtures/`, so dropping that JSON in validates the parser
against reality with no new test to write. If it fails, the failure tells us
exactly which field assumption was wrong.

**Also check one thing in that response:** does `price` on a `type=1`
round-trip search represent the *round-trip total*, or does resolving a total
require a second call with `departure_token`? This is the one open cost
question (SPEC §12). If it needs two calls, the budget doubles to 9,000/month
and `validate_budget()` will refuse to run — by design. The fix is one line:
swap `PRODUCTION` for `FALLBACK_TWO_CALL` in `config.py`, which is pre-tuned
to 4,680/month at two calls per query.

### 4. Your alert policy — edit `policy.json`

Currently placeholders. The ones that need your judgment:

| Field | Now | What it means |
|---|---|---|
| `ceiling_usd` | 300 | Never alert above this, whatever the history says |
| `percentile` | 0.10 | Alert in the bottom decile for that lead-time bucket |
| `nonstop_only` | false | DTW–MIA has nonstops on both hub carriers |
| `excluded_carriers` | `[]` | IATA codes, e.g. `["NK"]` |
| `bag_fee_usd` | `{}` | **Per-carrier, added before comparing.** A $59 Spirit fare with a $75 bag is not a $59 fare — but whether that's true depends on how you pack |
| `max_alerts_per_sweep` | 5 | Day one has no history, so the cold-start path would otherwise fire on all 150 itineraries |

---

## Running it

```bash
pip install pytest

PYTHONPATH=src python -m fares plan            # today's queries + budget, no network
PYTHONPATH=src python -m fares plan -v         # ...and list every date
PYTHONPATH=src python -m fares sweep --dry-run # full pipeline on fixtures, no key needed
PYTHONPATH=src python -m fares status          # what's collected, per lead-time bucket
PYTHONPATH=src python -m pytest -q

PYTHONPATH=src python -m fares sweep --limit 5 # first live run: spend 5 searches, not 150
```

Once the secrets are set, `.github/workflows/fares-sweep.yml` runs daily at
13:00 UTC (~9am Detroit) and commits observations back to the branch. Trigger
it by hand from the Actions tab first — it takes a `dry_run` input.

## How it decides

An observation alerts only if **all** of: under your ceiling (bag-adjusted),
in the bottom `percentile` of comparable history, not rated `high` by Google,
not a repeat inside `debounce_hours`, and within `max_alerts_per_sweep` of the
cheapest in that sweep.

Comparable history means *same lead-time bucket*. A $180 fare 3 days out and a
$180 fare 80 days out are not the same event, and pooling them washes out the
signal.

With under `min_history` comparable readings, it falls back to Google's
`typical_price_range` low bound — which is why `price_insights` is worth
collecting even though forecasting is out of scope.

**There is no prediction here.** We report that a fare is cheap against its
record, never that it's about to move. SPEC §5 has the reasoning; short version
is that a credible single-route model needs a year-plus of panel and the
prior art (Farecast) got built, bought, and killed.

## Layout

```
src/fares/
  models.py     value types, dependency-free
  sweep.py      budget-aware date selection; validate_budget()
  config.py     production config, fallback config, policy loading
  normalize.py  SerpApi JSON -> Observation[]   (defensive; see §3 above)
  decide.py     the alert gate + ranked cap
  storage.py    append-only JSONL, monthly partitions
  notify.py     ntfy publishing
  serpapi.py    fetch shell; transport injected so tests never hit the network
  cli.py        plan / sweep / status
data/
  observations/YYYY-MM.jsonl   the panel
  alerts.jsonl                 debounce log
```

Storage is JSONL rather than SQLite on purpose: this data lives in git, and a
binary file makes every commit an unreadable diff. DuckDB reads JSONL directly
when you want to analyze the panel.

## Not built

- **Award availability.** The better version of this tool: your Capital One
  miles transfer 1:1 to Flying Blue (Delta out of DTW) and the Avios family
  (American into MIA), so the same seats are purchasable with points. seats.aero
  sells a Partner API at $9.99/mo Pro, 1,000 calls/day. That turns the alert
  from "$214" into "$214 cash, or 11k Avios + $11." SPEC §11 — including the
  caveat that access isn't guaranteed.
- **Anything reading your Capital One account.** There's no supported API.
  SPEC §11.
