# C2 BaoStock Turnover-Enriched Daily Publisher V1

> Status: implementation candidate; production enablement requires release and
> V4 consumer compatibility evidence.

## 1. Purpose

The transitional daily source contains neutral OHLC, volume and amount facts,
but its `turnover` column is systematically empty. This contract adds one
Platform-owned, exact-date BaoStock turnover snapshot before the temporary
database is atomically published.

```text
explicit legacy stock database
+ explicit same-day source health artifact
+ immutable BaoStock-derived turnover snapshot
→ cross-source unit and value checks
→ clean turnover-enriched temporary database
→ schema/date/row-count/integrity/coverage validation
→ atomic market_data.db replacement
→ immutable v4-market-core ReadinessMarker
```

This remains a transitional bridge. It does not make V3 an application
dependency of V4 and does not change the retirement condition in C1.

## 2. Source and units

The preferred overlay source is:

```text
source = baostock.daily
source_version = baostock-daily-turnover.v1
turnover_unit = PERCENT
volume_unit = SHARE
amount_unit = CNY
```

The publisher must not consume the research `supplemental_facts.db`. Research
capital or sector facts remain physically separate from production market
publication.

The BaoStock adapter requests unadjusted daily facts:

```text
date, close, volume, amount, turn
adjustflag = 3
frequency = d
```

Blank or non-trading BaoStock rows are missing facts, never zero-filled.

When exact-date BaoStock login is unavailable, v1 also permits a bounded
derivation:

```text
audited BaoStock float shares reference
+ exact-date source volume in SHARE
→ turnover_percent = volume_shares / float_shares * 100
```

The runtime consumes only an immutable Platform reference artifact. It never
reads the research supplemental database. The initial reference may be
published once from the already audited BaoStock-derived capital dataset,
after checking its `SHARE`, `CNY` and `PERCENT` metadata.

After the initial publication, every validated exact-date BaoStock turnover
snapshot may renew the reference deterministically:

```text
float_shares = volume_shares / (turnover_percent / 100)
```

The dated reference remains immutable. The daily runner atomically advances a
`current.v1.json` symbolic link only after the new reference passes the same
unit, plausibility and 99% coverage gates. Eastmoney snapshots never renew a
BaoStock reference.

The reference may be at most 20 published trading sessions old. An older
reference is a hard failure. The derived snapshot uses:

```text
source = baostock.float-shares-derived
source_version = baostock-float-share-derived-turnover.v1
```

This bounded fallback avoids making every daily publication depend on a
successful BaoStock login while preventing a stale float-share denominator
from being used indefinitely. If all exact providers are unavailable and the
reference is missing or older than 20 sessions, turnover is published as an
explicit missing fact; the independently valid stock-daily core may still
publish readiness.

## 3. Supported neutral universe

The v1 overlay requests all exact-date, positively traded source rows whose
codes are supported by the BaoStock adapter:

```text
00, 30, 60, 68
```

This is a source-capability boundary, not a V4 eligibility or Discovery
universe. Unsupported exchanges remain explicit missing turnover facts.

## 4. Cross-source hard gate

Every accepted overlay row must match the same stock and date in the
transitional source.

The publisher checks:

- stock code and trade date exact match;
- BaoStock volume unit is `SHARE`;
- source `stock_daily.volume` and BaoStock volume match within frozen numeric
  tolerance;
- BaoStock amount unit is `CNY`;
- source `stock_daily.amount` and BaoStock amount match within frozen numeric
  tolerance;
- close matches within frozen numeric tolerance;
- turnover unit is `PERCENT`;
- all numeric values are finite and non-negative.

A volume or amount mismatch is a hard failure. The publisher must never
silently multiply or divide by 100 to rescue a row, because that can hide a
lot/share or ratio/percentage error.

## 5. Coverage and readiness

Coverage denominator:

```text
exact-date BaoStock-supported source rows
with source volume > 0 and source amount > 0
```

Coverage numerator:

```text
denominator rows with a validated BaoStock turnover overlay
```

The frozen minimum is:

```text
coverage >= 99%
```

Below the minimum, no target replacement and no ReadinessMarker publication
may occur. The 99% gate is applied before an immutable snapshot is written as
well as at final database publication, so a transient incomplete response
cannot permanently occupy the same-day snapshot identity.

Published metadata includes:

```text
producer_version
schema_version
turnover_source
turnover_source_version
turnover_unit
stock_daily_volume_unit
stock_daily_amount_unit
turnover_coverage
turnover_covered_row_count
turnover_eligible_row_count
```

## 6. Immutability and point-in-time rules

- The turnover snapshot contains one explicit `as_of`.
- A snapshot cannot contain future or other-date rows.
- Output is written atomically.
- Repeating the same output path is allowed only for byte-equivalent content.
- Competing writers use atomic create semantics; a later writer cannot replace
  the first immutable snapshot.
- A same-day market publication may be retried only when its complete content
  is unchanged.
- Existing Production Timeline records are never backfilled or rewritten.
  Enrichment affects only market databases published after production
  enablement and the future Timeline observations that consume them.

## 7. Failure behavior

Any of the following blocks publication:

- missing or invalid source health;
- missing/unreadable turnover snapshot;
- wrong snapshot date, schema, source or units;
- duplicate stock/date row;
- close, volume or amount mismatch;
- non-finite or negative physical value;
- coverage below 99%;
- target schema/integrity failure;
- changed same-day content.

Only a distinguishable temporary upstream failure may activate the bounded
float-share fallback. Contract, unit, coverage, identity and local validation
failures abort the daily script rather than being reclassified as source
unavailability.

Failure must not be represented as a successful zero-row overlay.

## 8. Compatibility

`MarketDataReaderV1` is unchanged: `turnover` was already an optional field in
the public schema. Populating that field is additive, while the new producer
and source metadata preserve lineage.

Production enablement still requires:

1. Platform contract and tests;
2. isolated publication acceptance;
3. Platform release;
4. V4 consumer compatibility suite against that release;
5. explicit operational cutover with rollback.
