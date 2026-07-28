# Platform v0.7.0 Release Readiness Review

> Review date: 2026-07-28
>
> Decision: `release_ready_pending_final_sha_independent_review`
>
> Production turnover publisher: `not_enabled`

## Scope

This candidate release contains two additive fact capabilities:

1. B2 supplemental stock-capital, point-in-time sector-membership and neutral
   sector-strength facts used by isolated research.
2. C2 turnover-enriched transitional daily publication used to repair future
   V4 Timeline completeness.

Neither capability changes Discovery, Setup, Timeline, Outcome, ranking,
Maturity or action semantics. V4 remains pinned to Platform v0.6.0 until an
immutable v0.7.0 release exists and the fixed-release consumer gate passes.

## Turnover contract and unit controls

The preferred daily source is exact-date BaoStock. When BaoStock login is
unavailable, the publisher may use an immutable BaoStock-derived float-share
reference for at most 20 published sessions:

```text
turnover_percent = source_volume_shares / float_shares * 100
```

The publication boundary fixes and verifies:

```text
stock_daily.volume = SHARE
stock_daily.amount  = CNY
turnover            = PERCENT
```

It never multiplies or divides an observed source value by 100 to rescue a
mismatch. Exact snapshots must match source date, stock, close, volume and
amount. Derived references must declare the BaoStock source, a BaoStock-bearing
source version, timezone-aware creation time, matching row count, unique
supported stock codes, one reference date and plausible float-share values.

Coverage below 99%, stale references, unit disagreement, duplicate rows,
changed same-day content or failed database integrity leave the existing target
and Readiness state untouched.

## Acceptance evidence

### Package gate

Final working-tree release gate:

```text
source-tree tests: 100 passed
sdist tests:       100 passed
wheel build:       passed
isolated install:  yifei-platform 0.7.0 passed
```

The focused turnover suite contains thirteen passing tests covering exact
publication, bounded derivation, BaoStock login retry, lot/share mismatch,
coverage failure, same-day conflict, stale reference and unverified or
internally inconsistent reference rejection, concurrent immutable publication,
temporary-source exit classification and PIT timestamp ordering.

### Real-data isolated publication

Using the 2026-07-28 transitional source and the immutable 2026-07-09
BaoStock-derived float-share reference:

```text
published rows:          3,717,939
published sessions:      862
eligible current rows:   5,193
turnover-covered rows:   5,177
coverage:                99.6918929%
reference age:           13 published sessions
producer:                transitional-daily-market-data+baostock-turnover.v2
```

The reference contains 5,189 rows and has SHA256
`86376d0d9b32b9a5f67eff5233918ffcac46f9b34b9400751ff1a761d50b1557`.
The derived 2026-07-28 snapshot has SHA256
`863dfb2cdb95c9b163aadc089b3083bb220162d0af19139cfade605cc52341c7`.

Five exact-date BaoStock control stocks confirmed volume ratios of exactly 1
against the transitional source and amount ratios approximately 1. The derived
turnover values differed by less than 0.02% from their exact BaoStock values.
This directly covers the known V3 share-versus-lot failure mode.

### V4 isolated consumer

The enriched database was consumed by a fresh V4 database and runtime:

```text
runner status:            completed
evaluation samples:       57
setups:                   56
Timeline nodes:           56
Timeline available:       56
Timeline missing fields:  []
```

The V4 worktree compatibility suite produced 187 passes and one intentional
failure: the fixed-release test still requires Platform 0.6.0 and correctly
rejects the untagged 0.7.0 working tree. This is pre-release compatibility
evidence, not authorization to change the consumer pin.

## Review status

A focused human review found one missing trust-boundary check on the fallback
reference. Source identity, source-version lineage, timestamp, declared row
count, duplicate code, reference-date and plausible-share checks were added,
then the complete release gate passed again.

Open Code Review v1.7.7 reviewed all 18 supported code and test files in the
complete working-tree diff:

```text
session: cf7911a1-6f33-461c-9fdb-12c016259c92
files reviewed: 18
findings: 17
```

The review identified valid gaps in immutable concurrent publication,
pre-snapshot coverage enforcement, temporary-source fallback classification,
source-mode identity, PIT timestamp order, Tushare membership availability
bounds, sector snapshot/database conflict ordering, optional columns, cache
range identity and deterministic SQLite closure. These were resolved and
covered by tests before the 100-test release gate.

One finding was rejected with contract evidence: a stock-specific Eastmoney
transport failure may be cached as explicit missing only after a fixed control
stock returns a healthy non-empty response. B2 deliberately freezes this
behavior; missing counts against the 98% batch gate and is never published as
zero capital flow.

The product quality standard still requires a second Open Code Review against
the immutable final commit SHA. Until that review passes, this document does
not approve a tag, consumer pin update or production LaunchAgent change.

## Operational cutover and rollback

After final-SHA independent review and immutable v0.7.0 release:

1. run V4 compatibility against that fixed release and update its pin;
2. change only the Platform LaunchAgent from
   `run_transitional_daily.sh` to `run_turnover_enriched_daily.sh`, adding the
   immutable turnover-root and float-share-reference arguments;
3. allow more than the current 15-minute Platform-to-V4 scheduling gap, then
   reload both LaunchAgents;
4. verify producer version, date, coverage, units and readiness before the V4
   run;
5. confirm newly created Timeline nodes are `available`.

Rollback restores the prior Platform script and V4 schedule/pin. The publisher
uses atomic replacement, so a failed run does not overwrite the last good
database. Existing Timeline history is never backfilled or mutated.
