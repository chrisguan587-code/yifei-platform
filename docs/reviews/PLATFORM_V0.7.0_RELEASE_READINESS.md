# Platform v0.7.0 Release Readiness Review

> Review date: 2026-07-29
>
> Decision: `release_ready_for_tag_no_production_cutover`
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
source-tree tests: 118 passed
sdist tests:       118 passed
wheel build:       passed
isolated install:  yifei-platform 0.7.0 passed
```

The focused turnover suite contains twenty passing tests covering exact
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
covered by tests before the initial 100-test release gate.

Final-SHA review session `d6142175-9c32-461a-a74e-992ac762dda4` examined
commit `d62348c` and did not approve it. It identified additional writer-lock,
Readiness verification, Tushare interval-preservation, HTTPS integrity,
fallback-race and strict-reference-lineage gaps. It also reported one remote
subtask error, so zero findings could not have been inferred from that run.

The valid findings were resolved in a follow-up commit. All supplemental
database replacement writers now share one target-scoped inter-process lock;
Readiness verifies exact-date/source rows in the target database; smaller
Tushare reruns preserve validated outer intervals; Eastmoney is HTTPS-only and
transport failures are retried rather than cached as legitimate empties; the
turnover fallback rechecks a concurrently published snapshot; and the float
share reference accepts only the audited source version.

The suggested reduction of the sector-flow floor from 400 was rejected using
source data rather than assumption: V3's actual `sector_em(industry)` history
contains 487–496 rows in recent complete snapshots and 496 on 2026-07-28.

Five successive full-range reviews against candidate SHAs found and drove
fixes for: cross-source writer ownership; immutable reference identity;
readiness completeness and unit gates; fixed vendor history windows; exact and
derived snapshot reuse; uncovered legacy turnover clearing; latest-session
coverage; temporal lineage; and concurrent fallback recovery. The resulting
code candidate is commit `cad341e`; its release gate passed 118 tests from both
the source tree and built sdist.

After provider access recovered, session
`ef67e0f1-5018-45b1-8009-3b76277070d9` completed the full 19-file review of
`v0.6.0..17a80ad`. Its actionable high findings were fixed in `d36bd67`.
Focused final review session `c909ee69-daf2-46ad-8065-0dd7a83952a2` completed
all six changed files with zero comments.

The code candidate is therefore ready for an immutable tag. This document does
not itself authorize a consumer pin update, push/merge, or production
LaunchAgent change.

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
