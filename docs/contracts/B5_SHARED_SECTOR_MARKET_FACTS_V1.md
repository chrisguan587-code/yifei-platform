# B5 Shared Sector Market Facts v1

> Physical table: `sector_market_daily`

## Ownership

Platform owns sector taxonomies, point-in-time membership and neutral daily
sector facts. Applications only consume the published facts and may attach
their own interpretation. Platform never publishes mainline, quadrant,
attention, score, recommendation, Setup or Decision semantics.

## Daily datasets

The weekday publisher derives both datasets from exact-date `stock_daily` and
the matching rows in `sector_membership_history`:

| Logical dataset | Taxonomy | Source version | Readiness bundle |
|:--|:--|:--|:--|
| `sector_market_daily_ths_l2` | THS level 2 industry | `platform-stock-daily-ths-l2.v1` | `sector-market-ths-l2` |
| `sector_market_daily_sw_l2` | CNInfo Shenwan level 2 industry | `platform-stock-daily-cninfo-sw-l2.v2` | `sector-market-sw-l2` |

Each row contains the arithmetic equal-weight member return, summed observed
member amount in CNY, member counts, observation coverage, taxonomy version and
publication version. Amount is a trading-activity fact; a consumer may derive
an amount share or change but Platform does not label it as inflow, strength or
attention.

The two taxonomies publish independently. Failure of one dataset must not stop
the other publisher from attempting its own atomic publication. Each missing
dataset remains unavailable and is never replaced by the other taxonomy or by
the preceding trading day.

## Timing and compatibility

The publisher starts after exact-date market facts are available. Repeated runs
are idempotent. `ths_board_daily` remains an audit-only vendor-index dataset and
is not a production fallback for either derived dataset.

The temporary `v4-market-sector-sw-l2` marker is retired. Current consumers
read the exact-date shared table directly or use the neutral readiness bundles
listed above.

The scheduled vendor `ths_board_daily` updater is also retired after the V4
divergence detector moved to `sector_market_daily_ths_l2`. Its historical table,
manual importer and read-only adapter remain only for reproducible audits; no
production task waits for or updates that dataset.

This contract supersedes B3's single-consumer wording and B4's statement that
THS L2 derived publication has stopped. It does not change either taxonomy,
membership source, weighting rule or stored row schema.
