# B4 SW Sector Market Daily Facts v2

> Physical table: `sector_market_daily`  
> Logical dataset: `sector_market_daily_sw_l2`  
> Source version: `platform-stock-daily-cninfo-sw-l2.v2`

## Purpose and consumers

Platform publishes timely, neutral daily facts for PIT-valid CNInfo Shenwan
level-2 industries. The first approved consumer is the V4 human-only market
cognition card. `mainline_divergence_strong_acceptance@v2` remains a future
consumer and is not activated by this contract.

The dataset contains no quadrant, mainline, score, recommendation, Setup,
Decision or trading semantics.

## Inputs and calculation

For each of the latest 25 market sessions required to calculate a five-session
trail whose earliest point has a full 20-session lookback, the publisher joins exact-date
`stock_daily` with `sector_membership_history` rows whose `sector_level` is
`L2` and whose PIT validity interval contains that session.

Each industry row stores:

- arithmetic equal-weight member return in percentage points;
- summed observed member amount in CNY;
- member and observed-member counts;
- observed-member coverage;
- membership, source and publication versions.

Industry amount share must use the sum of the same published L2 rows as its
denominator. It must not be described as vendor fund flow or main-force flow.

## Quality and timing

Publication requires all of the following for every newly written session:

- at least 120 active L2 industries;
- at least 95% observation coverage among assigned members;
- the exact `as_of` industry amount covering at least 97% of same-date
  non-negative `stock_daily.amount`;
- each preceding supporting session covering at least 90%; this lower history
  gate only allows a complete five-session trail across exceptional unmapped
  new-listing turnover and does not weaken the exact `as_of` 97% gate;
- exactly one membership source version across the requested window;
- no ambiguous PIT membership and no industry without an observation.

Failure leaves the existing database unchanged. Exact historical rows are
immutable and a different source version cannot reuse an existing L2 date.

The production publisher starts after exact-date stock market facts are ready,
normally before 18:00. It publishes the independent readiness bundle
`v4-market-sector-sw-l2`; it never waits for or falls back to late
`ths_board_daily`.

## Compatibility and audit

The existing THS_L2 v1 rows and `v4-market-sector` readiness remain as audit
history. L2 v2 rows coexist in the same physical table under a different
`sector_level` and source version; the production publisher no longer writes
new THS_L2 v1 rows.

Late `ths_board_daily` remains an audit dataset only. Its current publication
requires at least 90 same-date industry rows; a partial 80--89 row response is
not ready.

A future change to taxonomy, weighting, amount coverage, membership validity,
units or source requires a new version. Missing input is unavailable, never
zero or neutral.
