# DTW → MIA fare alerts — New Year's Eve 2026

Tracks DTW→MIA for the agreed dates (Dec 28 → Jan 3), stores every reading as
an append-only panel, and emails the group one digest listing every flight
under the price ceiling — airline, flight numbers, times, duration, and a
link to the exact date search on Google Flights.

Design rationale and decisions: **[SPEC.md](SPEC.md)**.

---

## What's done and what's waiting on you

Everything except credentials and your preferences. **162 tests pass**; the
full pipeline runs against fixtures.

```
DTW -> MIA · New Year's Eve 2026
  1 itinerary, swept every 4h (6x/day)
  6 calls/day, ~180/month of 250 (72%)
```

### 1. The dates

One itinerary: **Dec 28 (Mon) → Jan 3 (Sun)**, 6 nights. Edit `NYE_TRIP` in
`src/fares/config.py` to change it; add more departures/returns and it becomes a
grid again (each extra pair costs one search per sweep, so re-check the budget
with `fares plan`).

### 2. SerpApi key

Add as repo secret `SERPAPI_KEY` (*Settings → Secrets and variables → Actions*).

The key is on the **Free plan (250 searches/month)**. One itinerary at 6 sweeps/day
is 180/month, leaving ~70 for manual runs. `validate_target_budget()` refuses to
run a config that would overrun the plan. A paid tier would buy either more
frequent sweeps or a second call per option to resolve the *return* flight
(`departure_token`), which the digest currently leaves for you to pick on Google
Flights.

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

Then verify delivery before it can surprise anyone (or trigger the workflow
with `test_email` checked):

```bash
PYTHONPATH=src python -m fares test-email    # one sample digest from the recorded response
```

Test digests are tagged `[TEST - recorded data, not live]` in the subject.

**Put only your own address in `ALERT_RECIPIENTS` first.** Confirm the digest
looks right in your inbox, *then* add your pals. Recipients go in `To:`, so
they'll see each other and can reply-all — that's intended for a group trip,
but it's worth knowing before you send.

`NTFY_TOPIC` still works as a fallback if email config is absent.

### 4. Recorded response

`tests/fixtures/real_dtw_mia_2026-12-28.json` is a real response recorded on
2026-09-15 (trigger the workflow with `record` checked to take a fresh one; it
lands as an artifact). Findings: a round-trip (`type=1`) search returns
**outbound options only**, each priced as the **round-trip total**, with a
`departure_token` for a second call that lists returns. `TestFixtureContract`
runs over every fixture, so the parser is validated against this shape.

### 5. Your alert policy — edit `policy.json`

| Field | Now | What it means |
|---|---|---|
| `ceiling_usd` | 500 | Never alert above this (bag-adjusted) |
| `percentile` | `null` | `null` = no history gate, anything under the ceiling alerts. Set e.g. `0.10` to alert only in the cheapest decile for *that specific itinerary* |
| `veto_google_high` | false | `true` = Google's own "high" price rating blocks an alert |
| `nonstop_only` | false | Both hub carriers fly DTW–MIA nonstop |
| `excluded_carriers` | `[]` | IATA codes, e.g. `["NK"]` |
| `bag_fee_usd` | `{}` | Per-carrier, added before comparing. A $59 Spirit fare with a $75 bag isn't $59 — depends how you pack |
| `debounce_hours` | 24 | Don't re-mail the group about the same itinerary |
| `renotify_drop_usd` | 25 | ...unless it fell at least this much further |
| `max_alerts_per_sweep` | 11 | Caps alerts per digest, keeping the cheapest. 11 = every itinerary fits in one email |

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

Once secrets are set, `.github/workflows/fares-sweep.yml` runs every 4 hours.
Trigger it by hand from the Actions tab — inputs: `dry_run`, `limit`, `test_email`, `record`.

### Where the data lives

Observations are committed to a dedicated **`fares-data`** branch, not to the
code branch. At 6 sweeps/day that's ~600 automated commits by NYE, and they
have no business in the history of a repo that holds your resume. The workflow
restores the panel from that branch each run via a git worktree, appends, and
pushes back. Both the fresh-start and accumulate paths were verified before
shipping.

## How it decides

Every distinct flight option in a sweep is a candidate. One alerts only if
**all** of: under your ceiling (bag-adjusted), not a repeat inside
`debounce_hours` *unless* it dropped `renotify_drop_usd` below the last
announced price, and within `max_alerts_per_sweep` of the sweep's cheapest.
Two further gates are **off** in `policy.json` (2026-09-15) but available:
`percentile` (cheapest decile of that itinerary's own history) and
`veto_google_high` (Google's own "high" rating blocks an alert).

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
