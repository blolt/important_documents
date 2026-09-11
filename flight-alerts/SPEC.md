# DTW → MIA Fare Tracking & Alerting — Design Spec

**Status:** Draft for review. No code written yet; data-source decision (§3) forks the implementation.
**Date:** 2026-09-11

---

## 1. Problem statement

Track the price of air travel from Detroit (DTW) to Miami (MIA), maintain a
longitudinal record of those prices, and emit an alert when a fare is cheap
relative to its own history. Secondary goal: "predictive pricing" — a
buy/wait signal.

Non-goals (for v1): booking, multi-route generalization, a web UI.

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

≈ 33 one-way queries/day ≈ **1,000/month**, or ~5,000/month with a
5-offset round-trip grid. That lands on the $25 or $75 tier respectively.
Note SerpApi throttles every plan to 20%/hour of monthly volume, so the
sweep must be paced, not burst.

## 5. Predictive pricing — an honest assessment

The prior art is real and the outcome is instructive:

- Etzioni & Knoblock's **Hamlet** (2003) captured 88.6% of achievable
  savings over a 41-day pilot, averaging 27.1% saved where saving was
  possible. It commercialized as **Farecast**, was acquired by Microsoft
  in 2008 — and Microsoft **shut it down in 2014**.
- Hopper claims ~95% accuracy on price-movement calls. Published academic
  work on single routes lands closer to **80–83%**.

Two things to hold onto. First, those accuracy figures are on a *binary*
up/down call where the naive heuristic — "fares rise as departure
approaches" — already captures much of the signal, so headline accuracy
overstates the marginal value of a model. Second, single-route models are
demonstrably trainable on ~1,800–51,000 records, but we would be
generating roughly 1,000 records/month. That is a **two-to-four year**
runway to a respectable training set for one route.

**Recommendation: do not train a model in v1.** Instead:

1. **Consume Google's prediction.** `price_level` is the output of a model
   with incomparably more data than we will ever have. Free with each query.
2. **Add our own percentile rule** over accumulated observations —
   "is today's fare in the bottom decile for this route at this lead-time
   bucket?" Cheap, interpretable, no training, and it encodes *our* history
   rather than the global market's.
3. **Accumulate the panel from day one anyway.** The data is a free
   byproduct of alerting. Revisit modeling in a year when there's something
   to model on. If the panel never justifies a model, we've lost nothing.

This is deliberately the heuristic-over-empirics call: the empirics aren't
available yet at this sample size, and a percentile rule degrades gracefully
where a badly-trained model degrades silently.

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

## 9. Open decisions

1. **Data source** — SerpApi paid, or prove it free on Travelpayouts first?
2. **Trip shape** — one-way only (cheap), or round-trip grid (5× cost)?
3. **Alert channel** — ntfy.sh push, email, or a GitHub issue?
4. **Language** — Python (DuckDB/pandas story is better for the eventual
   analysis) or TypeScript?

## 10. Known unknowns

- Exact SerpApi `google_flights` parameter surface (return-date grid
  support, whether a price-graph call can return many dates for one
  search). **Needs a spike** — this could materially cut §4's cost and
  serpapi.com is not reachable from this environment to confirm.
- Whether `price_history` granularity is sufficient to seed our percentile
  rule directly.
- SerpApi terms as applied to personal, low-volume use — worth a read
  before scaling the sweep.
