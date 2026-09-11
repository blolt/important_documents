# DTW → MIA Fare Tracking & Alerting — Design Spec

**Status:** Decisions made (§9). Sweep planner built and under test; decision
engine and fetch shell pending an alert policy (§7).
**Date:** 2026-09-11

---

## 1. Problem statement

Track the price of air travel from Detroit (DTW) to Miami (MIA), maintain a
longitudinal record of those prices, and emit an alert when a fare is cheap
relative to its own history.

Non-goals: booking, multi-route generalization, a web UI, and — explicitly
dropped — forecasting. We do not predict where prices are going. We report
when one is cheap against its own record. See §5.

## 2. Feasibility verdict

Plausible, with one important caveat and one course correction.

**Course correction:** the obvious answer as of last year — Amadeus
Self-Service APIs, 2,000 free calls/month — is gone. Amadeus paused new
registrations in spring 2026 and fully decommissioned the Self-Service
portal on **2026-07-17**; existing keys were deactivated. Only the
Enterprise portal survives, which is a sales-contact motion, not a
weekend project. Any tutorial or LLM answer recommending Amadeus
Self-Service is now stale.

**Caveat:** "predictive pricing" is the part most likely to disappoint if
we build it ourselves. See §5.

## 3. Data source options

| Option | Cost | Data quality | Catch |
|---|---|---|---|
| **SerpApi → Google Flights** | Free 250/mo; $25/mo for 1k; $75/mo for 5k | Live, matches what you'd see booking. Ships Google's own `price_insights` | Scraping intermediary, not a licensed feed. No pay-as-you-go; unused searches don't roll over |
| **Travelpayouts / Aviasales Data API** | Free (affiliate model — earn per booking, pay per nothing) | Cached/aggregated, 7-day retention, not live shopping results | Requires affiliate registration. Full real-time Search API gated behind 50k MAU |
| **Duffel** | Per confirmed booking, not per search | Real live inventory, properly licensed | Test mode returns synthetic prices — useless for our purpose. Real access is a merchant-of-record posture |
| **Amadeus Enterprise** | Negotiated | Best | Contract, account manager, overkill |

**Recommendation: SerpApi**, with Travelpayouts as a free fallback if we
want to prove the pipeline before spending anything.

The decisive factor is not the fare data — it's that SerpApi surfaces
Google Flights' `price_insights` block, which carries:

- `price_level` — `low` / `typical` / `high`
- `typical_price_range` — `[low, high]` two-integer band
- `price_history` — timestamped series
- `lowest_price`

That last field is the quiet win: **we get price history without having
accumulated it ourselves.** Day one of the project has a usable baseline
instead of a cold start.

## 4. Query budget (the real cost driver)

"Everything flying DTW→MIA" is unbounded unless we bound the date grid.
Each (departure date [, return date]) pair costs one search.

Naive daily sweep, 90-day one-way horizon: 90 × 30 = **2,700 searches/mo**.
Add a 5-offset return grid for round-trips: **13,500/mo** → $275 tier.

Proposed fix — **tier the sampling rate by lead time**, since fare
volatility is concentrated near departure:

| Lead time | Sweep frequency | Queries/day |
|---|---|---|
| 0–14 days | daily | ~14 |
| 15–60 days | every 3 days | ~15 |
| 61–90 days | weekly | ~4 |

**Corrected by the implementation.** Those cadences actually need **5,100
searches/month** with a 5-offset round-trip grid — over the $75 tier's 5,000,
and the one-way variant came to 1,020 against the $25 tier's 1,000. Both
overran by a few percent, which is exactly the kind of error that surfaces as
a dead pipeline three weeks into a month rather than as an obvious failure.

Widening the mid-band cadence from 3 days to 4 resolves it:

| Lead time | Sweep frequency |
|---|---|
| 1–14 days | daily |
| 15–60 days | every 4 days |
| 61–90 days | every 7 days |

**30 departure dates/day × 5 return offsets = 150 searches/day = 4,500/month**,
90% of the Developer tier with headroom for retries. Unused searches do not
roll over, so the remaining 10% is use-it-or-lose-it, not savings.

`validate_budget()` enforces this at startup and
`tests/test_sweep.py::TestBudget` guards it, including a regression test
pinning the naive config at 5,100. SerpApi also throttles every plan to
20%/hour of monthly volume (1,000/hr here), which 150/day never approaches —
but the sweep staggers dates across days regardless.

## 5. Predictive pricing — dropped

Cut from scope by decision. We are not training a model and not forecasting.

Worth recording why this is the right call rather than a concession. Single-route
models need ~1,800–51,000 records; this pipeline generates roughly 4,500
observations/month, so a credible training set is a year-plus out. The prior
art is not encouraging either: Etzioni's Hamlet (2003) captured 88.6% of
achievable savings and commercialized as Farecast, which Microsoft bought in
2008 and shut down in 2014. Published single-route accuracy sits at 80–83%
on a binary up/down call that a naive "fares rise toward departure" heuristic
already half-solves.

What replaces it is a **percentile rule over our own history** — is this fare
in the bottom Nth percentile for this lead-time bucket? — which is description,
not prediction. No training, interpretable, degrades gracefully at small N
where a thin model degrades silently.

Google's `price_level` still arrives free with every response and is worth
recording in the panel. We read it as one input to the alert gate, not as a
forecast to act on.

Separately, note that Capital One Travel already ships Hopper's price
prediction (§11) — so the forecasting capability exists, it is just not ours
to build.

## 6. Architecture

Near-zero-ops, because this should not become a system to maintain.

```
GitHub Actions (cron, tiered schedule)
  └─ fetch: SerpApi google_flights  ──►  raw JSON response
       └─ normalize  ──►  append JSONL  ──►  committed to this repo
            └─ decide: alert policy (§7)
                 └─ notify: ntfy.sh topic → phone push
```

**Storage: append-only JSONL, partitioned by month, committed to git.**
At ~1,000 rows/month this is kilobytes. Git gives us versioned, durable,
free storage with no database to run, and DuckDB reads JSONL directly for
analysis later. Explicitly *not* SQLite — a binary file churns unreadable
diffs on every commit.

**Scheduling:** GitHub Actions cron is free for this volume and the repo
already lives on GitHub. The tiered sweep (§4) is a matter of which dates
each run selects, not multiple workflows.

**Secrets:** `SERPAPI_KEY` as an Actions secret.

## 7. Alert policy (needs your input — §9)

An observation fires an alert when **all** of:

- fare ≤ (user-defined ceiling), AND
- fare is in the bottom *N*th percentile of our own observations for that
  lead-time bucket, AND
- Google's `price_level` is not `high`, AND
- no alert fired for the same (date, price band) in the last *K* hours
  — debounce, so a stable cheap fare doesn't page daily.

Constraints still to be set by you: nonstop-only?, carrier exclusions,
bag-fee normalization (a $59 Spirit fare with a $75 bag is not a $59 fare),
acceptable departure windows.

## 8. Testing strategy

The network layer is a thin shell; the logic is pure and belongs under test.

- **Fixtures:** record real SerpApi responses once, commit as fixtures.
  All parsing and decision tests run offline against them — no API spend in CI.
- **Unit:** `normalize(raw) -> Observation[]` over fixtures, including
  malformed/missing `price_insights` (it is not always present).
- **Unit:** `should_alert(observation, history, policy) -> Decision` —
  table-driven, this is where the real complexity lives.
- **Unit:** lead-time bucketing and the sweep-date selector (pure date math,
  the single most likely place for an off-by-one to silently halve our
  coverage).
- **Integration:** one opt-in, env-gated live call, never in CI.

Write the decision-engine tests before the engine.

## 9. Decisions

| Decision | Resolution |
|---|---|
| Data source | **SerpApi**, Developer tier ($75/mo, 5,000 searches) |
| Trip shape | **Round-trip**, 5 return offsets (3–7 nights) |
| Alert channel | **ntfy.sh** push |
| Language | **Python** — stdlib-only core, DuckDB for later analysis |
| Forecasting | **Out of scope** (§5) |
| Browser automation / scraping | **Rejected** — Google Flights is bot-defended, per-query cost exceeds the API, and non-determinism is disqualifying in a cron job |

Still open: the alert policy itself (§7) — ceiling, percentile, nonstop
preference, bag-fee normalization. These are travel preferences, not
engineering choices.

## 10. Implementation status

- [x] `models.py` — value types, dependency-free
- [x] `sweep.py` — budget-aware date selection, `validate_budget()`
- [x] `config.py` — tuned production config (4,500/mo)
- [x] `tests/test_sweep.py` — 19 tests
- [ ] `normalize.py` — SerpApi JSON → `Observation[]`
- [ ] `decide.py` — alert gate (blocked on policy, §7)
- [ ] `storage.py` — JSONL append
- [ ] `notify.py` — ntfy publish
- [ ] GitHub Actions workflow

## 11. Capital One Venture

Three separate questions, three different answers.

**Account/transaction data: no.** Capital One's DevExchange APIs are
partner-facing, with production access gated behind partner approval — built
for fintechs, not cardholders. Nessie, the openly accessible one, serves mock
data. There is no supported path to your balance, transactions, or miles
programmatically. Dead end; don't spend time here.

**Portal features: yes, but manual.** Capital One Travel is powered by Hopper
and carries three things worth using by hand:

- *Price prediction* — the forecasting we just descoped, already built.
- *Price drop protection* — free, auto-applied when their model says buy.
  Monitors your itinerary for 10 days post-purchase, refunds the difference
  as travel credit, **capped at $50**.
- *Price freeze* — small fee, holds a fare up to 14 days, covers an increase
  up to $500.

One correction worth knowing: the **Venture earns no flight bonus** through
the portal. 5x applies to hotels, vacation rentals, rental cars, and
activities; flights earn the flat 2x. (That 5x-on-flights figure belongs to
the Venture X, which carries a $395 annual fee.) So the portal's value here
is the protections, not the earn rate.

**Award availability: yes, and it's the interesting one.** This route is
unusually well-suited to it — DTW is a Delta hub and MIA is an American hub,
and Capital One transfers 1:1 to partners reaching both:

- **Flying Blue** (Air France/KLM, SkyTeam) → Delta metal out of DTW
- **British Airways / Qatar / Finnair Avios** (oneworld) → American into MIA

So miles you already hold can price the same seats we're tracking in cash.
**seats.aero** sells a Partner API — Pro is $9.99/mo including 1,000 API
calls/day, which dwarfs our SerpApi budget — quoting availability across 20+
mileage programs.

That upgrades the product materially. Instead of "DTW→MIA Nov 14–18 is $214,"
the alert reads "**$214 cash, or 11k Avios + $11 — you hold the miles.**"
That's an actionable decision rather than a number.

*Caveats:* the Partner API is Pro-only, non-commercial without written
agreement, and seats.aero states access may be limited at their discretion —
so treat it as a phase 2 that may not be grantable, not a dependency.

## 12. Known unknowns

- Exact SerpApi `google_flights` parameter surface (return-date grid
  support, whether a price-graph call can return many dates for one
  search). **Needs a spike** — this could materially cut §4's cost and
  serpapi.com is not reachable from this environment to confirm.
- Whether `price_history` granularity is sufficient to seed our percentile
  rule directly.
- SerpApi terms as applied to personal, low-volume use — worth a read
  before scaling the sweep.
