# DTW → MIA fare alerts — New Year's Eve 2026

Tracks the 11 viable DTW→MIA itineraries around NYE, stores every reading as
an append-only panel, and emails the group one digest when a fare is cheap
against its own history.

Design rationale and decisions: **[SPEC.md](SPEC.md)**.

---

## What's done and what's waiting on you

Everything except credentials and your preferences. **162 tests pass**; the
full pipeline runs against fixtures.

```
DTW -> MIA · New Year's Eve 2026
  11 itineraries, swept every 2h (12x/day)
  132 calls/day, ~3960/month of 5000 (79%)
```

### 1. Confirm the dates — 30 seconds

NYE 2026 is a **Thursday**, so the grid is:

- **Departures:** Dec 29 (Tue), Dec 30 (Wed), Dec 31 (Thu)
- **Returns:** Jan 1 (Fri), Jan 2 (Sat), Jan 3 (Sun), Jan 4 (Mon)

11 itineraries, not 12 — Dec 31 → Jan 1 is filtered out as a one-night trip
(`min_nights`). Edit `NYE_TRIP` in `src/fares/config.py` if you want a
different spread. **If your pals are flying from other cities, tell me** —
that's a multi-origin change, not a config tweak.

### 2. SerpApi key

**Developer tier, $75/mo (5,000 searches).** Add as repo secret `SERPAPI_KEY`
(*Settings → Secrets and variables → Actions*).

> **You may not need the $75 tier.** A fixed-date trip is only 11 itineraries,
> so the Starter tier ($25/mo, 1,000 searches) covers **3 sweeps/day** — every
> 8 hours, 990/month. The $75 tier buys sweeps every 2 hours instead. For
> scarce holiday inventory I'd keep the faster cadence, but it's $50/mo for
> latency and that's your call. To downgrade: set `sweeps_per_day=3` and
> `monthly_budget=1000` in `config.py`; `validate_target_budget()` enforces it.

### 3. Email — a Gmail app password

The sweep sends through Gmail SMTP, so the digest arrives *from you* rather
than from a transactional service your friends' spam filters have never seen.

1. Enable 2FA on the sending account if it isn't already.
2. Create an app password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   ([docs](https://support.google.com/accounts/answer/185833)).
3. Add these repo secrets:

| Secret | Value |
|---|---|
| `SMTP_USER` | the sending Gmail address |
| `SMTP_PASSWORD` | the 16-character app password (**not** your account password) |
| `ALERT_RECIPIENTS` | comma-separated list |
| `ALERT_FROM` | *optional* — defaults to `SMTP_USER`; Gmail rejects a From it hasn't authenticated |

**Keep the recipient list in the secret, not in a config file.** Your friends'
addresses should not be committed to a repo, and this one also holds your
resume.

Then verify delivery before it can surprise anyone:

```bash
PYTHONPATH=src python -m fares test-email    # one sample digest, built from the fixture
```

**Put only your own address in `ALERT_RECIPIENTS` first.** Confirm the digest
looks right in your inbox, *then* add your pals. Recipients go in `To:`, so
they'll see each other and can reply-all — that's intended for a group trip,
but it's worth knowing before you send.

`NTFY_TOPIC` still works as a fallback if email config is absent.

### 4. Record a real API response — please do this one

```bash
curl "https://serpapi.com/search?engine=google_flights&departure_id=DTW\
&arrival_id=MIA&outbound_date=2026-12-30&return_date=2027-01-03\
&currency=USD&type=1&api_key=$SERPAPI_KEY" \
  > tests/fixtures/real_dtw_mia_nye.json
```

`normalize.py` is written against SerpApi's *documented* response shape, not a
recorded one. `TestFixtureContract` parametrizes over every file in
`tests/fixtures/`, so dropping that JSON in validates the parser against
reality with no new test to write — and a failure names the wrong assumption.

**While you're in there, check one thing:** does `price` on a `type=1` search
give the *round-trip total*, or does a total need a second call with
`departure_token`? If it needs two, set `calls_per_query=2` in `NYE_TRIP` —
the budget becomes 7,920/month and `validate_target_budget()` will refuse to
run rather than exhaust the plan mid-month. Dropping to `sweeps_per_day=6`
brings it back to 3,960.

### 5. Your alert policy — edit `policy.json`

| Field | Now | What it means |
|---|---|---|
| `ceiling_usd` | 300 | Never alert above this. **NYE is peak season — this is probably too low.** |
| `percentile` | 0.10 | Alert in the cheapest decile for *that specific itinerary* |
| `nonstop_only` | false | Both hub carriers fly DTW–MIA nonstop |
| `excluded_carriers` | `[]` | IATA codes, e.g. `["NK"]` |
| `bag_fee_usd` | `{}` | Per-carrier, added before comparing. A $59 Spirit fare with a $75 bag isn't $59 — depends how you pack |
| `debounce_hours` | 24 | Don't re-mail the group about the same itinerary |
| `renotify_drop_usd` | 25 | ...unless it fell at least this much further |
| `max_alerts_per_sweep` | 5 | Caps the day-one cold-start burst |

---

## Running it

```bash
pip install pytest

PYTHONPATH=src python -m fares plan -v         # the grid + budget, no network
PYTHONPATH=src python -m fares sweep --dry-run # full pipeline on fixtures; sends nothing
PYTHONPATH=src python -m fares status          # readings per itinerary
PYTHONPATH=src python -m fares test-email      # prove delivery
PYTHONPATH=src python -m pytest -q

PYTHONPATH=src python -m fares sweep --limit 3 # first live run: spend 3 searches, not 11
```

Once secrets are set, `.github/workflows/fares-sweep.yml` runs every 2 hours.
Trigger it by hand from the Actions tab first — it takes a `dry_run` input.

### Where the data lives

Observations are committed to a dedicated **`fares-data`** branch, not to the
code branch. At 12 sweeps/day that's ~1,300 automated commits by NYE, and they
have no business in the history of a repo that holds your resume. The workflow
restores the panel from that branch each run via a git worktree, appends, and
pushes back. Both the fresh-start and accumulate paths were verified before
shipping.

## How it decides

An observation alerts only if **all** of: under your ceiling (bag-adjusted),
in the cheapest `percentile` of that itinerary's own history, not rated `high`
by Google, not a repeat inside `debounce_hours` *unless* it dropped
`renotify_drop_usd` further, and within `max_alerts_per_sweep` of the sweep's
cheapest.

Comparison is **per itinerary**, not per lead-time bucket. "Is Dec 30 → Jan 3
cheap against what that exact trip has been going for" is a sharper question
than "is this cheap for something ~100 days out," which pools unrelated dates.

Below `min_history` readings it falls back to Google's `typical_price_range`
low bound — which is why `price_insights` is worth collecting even with
forecasting out of scope.

**There is no prediction here.** We report that a fare is cheap against its
record, never that it's about to move. SPEC §5 has the reasoning.

## Layout

```
src/fares/
  models.py       value types, dependency-free
  sweep.py        fixed-date grid + rolling-horizon planner; budget validation
  config.py       NYE_TRIP (active), rolling config, policy loading
  normalize.py    SerpApi JSON -> Observation[]  (defensive; see §4)
  decide.py       the alert gate + ranked per-sweep cap
  storage.py      append-only JSONL, monthly partitions, itinerary history
  email_alert.py  SMTP digest, plain + HTML
  notify.py       ntfy fallback
  serpapi.py      fetch shell; transport injected so tests never hit the network
  cli.py          plan / sweep / status / test-email
```

`sweep.py` keeps the rolling-horizon planner alongside the fixed-date one —
it's tested and it's what you'd use to watch DTW–MIA generally after NYE.

## Not built

- **Award availability.** The better version of this: your Capital One miles
  transfer 1:1 to Flying Blue (Delta out of DTW) and the Avios family
  (American into MIA), so these exact seats are purchasable with points —
  and NYE is when cash fares are worst and award seats are most valuable.
  seats.aero sells a Partner API at $9.99/mo Pro. That turns the digest from
  "$214" into "$214 cash, or 11k Avios + $11." SPEC §11, including the caveat
  that access isn't guaranteed.
- **Anything reading your Capital One account.** No supported API. SPEC §11.
