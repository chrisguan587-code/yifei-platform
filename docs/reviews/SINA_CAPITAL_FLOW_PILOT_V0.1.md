# Sina Capital Flow Pilot v0.1

> Date: 2026-07-28
> Decision: `accept_for_exploration_backfill`
> Source version: `sina-moneyflow-r0+baostock-daily.v2`

## Purpose and boundary

This pilot evaluates whether the no-token Sina daily money-flow endpoint can
provide the F1/F2 numerator for the frozen Quiet Reversal Phase 2.1 exploration
window, 2026-04-01 through 2026-07-09.

It changes no Discovery, Setup, Timeline, Understanding, Risk, Maturity, or
production rule. No holdout factor count, load, outcome, or descriptive
statistic was inspected.

The published neutral fact is:

```text
vendor_net_amount = Sina r0_net in CNY
net_inflow_ratio = vendor_net_amount / BaoStock-derived float market cap in CNY
```

Sina defines `r0` as trades of at least CNY 1,000,000. It remains a vendor
classification, not exchange-confirmed institutional activity.

## Deterministic sample

The pilot selected 60 stocks from the market database:

- 15 Shenzhen main-board stocks;
- 15 ChiNext stocks;
- 15 Shanghai main-board stocks;
- 15 STAR Market stocks.

Within each segment, codes were ordered and divided into 15 equal buckets; the
first code in each bucket was selected. No capital-flow value or future
outcome participated in sample selection.

The existing B2 contract excludes Beijing Exchange from this public source
coverage universe. The pilot did not silently map Beijing codes to Shenzhen.

## Quality results

| Check | Result |
|---|---:|
| Stocks requested / cached | 60 / 60 |
| Expected stock-days | 3,186 |
| Published stock-days | 3,178 |
| Overall coverage | 99.7489% |
| Frozen minimum coverage | 98% |
| Shenzhen main-board coverage | 99.1803% |
| ChiNext coverage | 100% |
| Shanghai main-board coverage | 99.8954% |
| STAR Market coverage | 100% |
| Duplicate stock/date keys | 0 |
| Float-market-cap unit errors | 0 |
| Published amount-unit pairs | CNY / CNY only |
| SQLite integrity | ok |

For 2,730 rows whose absolute `r0` ratio was at least 0.1%, the ratio-implied
turnover divided by independent BaoStock turnover had:

| Quantile | Ratio |
|---|---:|
| p01 | 0.969642 |
| p05 | 0.979669 |
| median | 0.994087 |
| p95 | 1.000000 |
| p99 | 1.000000 |

One otherwise plausible row had a ratio of 0.806267. This supports a
source-specific unit-check range of 0.75 to 1.25. That range still rejects
10x/100x amount-unit errors and does not change the Eastmoney 0.9 to 1.1
range.

## Explicitly rejected source rows

Eight expected stock-days were excluded because Sina reported an `r0` or total
net-flow ratio outside `[-100%, 100%]`:

| Stock | Date | r0 ratio % | total ratio % |
|---|---|---:|---:|
| 000820 | 2026-06-03 | 0.000000 | -104.688 |
| 000820 | 2026-06-04 | 0.000000 | -101.048 |
| 000820 | 2026-06-30 | 64.227772 | 118.178 |
| 002717 | 2026-05-22 | -13.265098 | -101.496 |
| 002717 | 2026-05-25 | -41.813943 | -122.624 |
| 002717 | 2026-06-09 | 127.162216 | 127.971 |
| 002717 | 2026-06-15 | -77.291652 | -112.139 |
| 603595 | 2026-06-30 | -78.079864 | -104.848 |

These rows remain visible in immutable raw cache with
`vendor_row_status=INVALID_VENDOR_RATIO`. They are not published, zero-filled,
or replaced from Eastmoney. Their absence reduces measured coverage.

## v1 rejection and v2 correction

The first pilot cache version rejected the batch when a zero-flow suspended
row omitted `r0_ratio`. v2 permits a missing ratio only when the corresponding
net amount is exactly zero. A missing ratio with nonzero amount still blocks
the stock response.

The v1 cache was not rewritten. v2 uses a separate immutable cache and source
version.

## Reproduction

```bash
PYTHONPATH=src python3 -m yifei_platform.supplemental_cli \
  prefetch-capital-sina \
  --market-db <pilot-market.db> \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --cache-dir <sina-v2-cache> \
  --batch-size 60

PYTHONPATH=src python3 -m yifei_platform.supplemental_cli \
  backfill-capital-sina \
  --market-db <pilot-market.db> \
  --target-db <pilot-supplemental.db> \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --fetched-at 2026-07-28T18:30:00+08:00 \
  --cache-dir <sina-v2-cache>
```

Platform release gate at this decision point: 83 tests passed, package build
passed, isolated wheel installation passed, and installed-package tests passed.

## Decision

`accept_for_exploration_backfill`

The adapter may proceed to the complete 5,213-stock supported exploration
universe. Final publication still requires the complete atomic coverage and
unit gates. Eastmoney rows must not fill Sina gaps.

## Full-universe completion

The approved full exploration backfill subsequently completed and atomically
published:

| Check | Result |
|---|---:|
| Supported stocks | 5,213 |
| Stocks with at least one valid fact | 5,211 |
| Published stock-days | 311,479 |
| Unique stock/date keys | 311,479 |
| Overall coverage | 99.6586% |
| Worst daily coverage | 98.6716% |
| Median daily coverage | 99.7022% |
| Duplicate stock/date keys | 0 |
| SQLite integrity | ok |

The final table contains only source
`sina.moneyflow.r0+baostock`, source version
`sina-moneyflow-r0+baostock-daily.v2`, and CNY/CNY amount-unit pairs.
The existing 6,250 sector-membership intervals were preserved. No holdout
factor distribution, load, Episode, or outcome was accessed during ingestion.
