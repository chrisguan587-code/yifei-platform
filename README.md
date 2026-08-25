# Yifei Platform

Shared Platform owns market facts and versioned neutral capabilities used by Yifei applications.

## Daily market ownership

`yifei-platform-publish-daily-market` is the production owner of the neutral
A-share post-close snapshot. It appends one exact exchange session to the
Platform database, publishes `v4-market-core` readiness, and has no V3 path or
module dependency. Sina supplies whole-market discovery; a bounded Tencent
fallback can cover only the prior known universe and is reported as degraded
universe discovery. See
[`C3_PLATFORM_DAILY_MARKET_PUBLISHER_V1.md`](./docs/contracts/C3_PLATFORM_DAILY_MARKET_PUBLISHER_V1.md).

The production wrapper requires the official annual exchange calendar before
it calls the publisher:

```bash
./scripts/run_daily_market.sh \
  /path/to/exchange_calendar/sse-2026.v1.json \
  /path/to/market_data.db \
  /path/to/platform_state
```

## Bootstrap migration

`yifei-platform-bootstrap` is a one-time migration tool, not the Platform
production data supply chain. Its legacy source path is required explicitly on
the command line and is never a package default or application dependency.

It publishes an independent SQLite database containing only `stock_daily`,
`trading_calendar`, and `platform_metadata`, then publishes the immutable
`v4-market-core` readiness marker. The tool is retired once the independent
database exists.

The historical `yifei-platform-publish-transitional` bridge advanced the public
database from an explicitly supplied same-day V3 source health artifact. It is
specified in
[`C1_TRANSITIONAL_DAILY_PUBLISHER_V1.md`](./docs/contracts/C1_TRANSITIONAL_DAILY_PUBLISHER_V1.md)
and must not be loaded alongside the Platform-owned daily publisher. It remains
only for migration audit and rollback diagnosis.

```text
Applications -> Versioned Shared Platform

Forbidden:
Application -> Application
Shared Platform -> Application
```

Platform may contain calendar, market-data access, quality/readiness, artifact protocol, notification transport, outcome calculation, and runtime primitives. It must not contain Strategy, Candidate, Setup, Pattern, Maturity, recommendation, or application state semantics.

The initial engineering sequence is:

```text
A0 golden contract fixtures
-> A1 TradingCalendarV1 + MarketDataReaderV1
-> A2 DataQualitySnapshotV1 + ReadinessMarkerV1
-> A3 ArtifactEnvelopeV1 + OutcomeCalculatorV1
```

Implemented public contracts:

- `TradingCalendarV1`: deterministic session lookup from an explicit versioned session set.
- `MarketDataReaderV1`: read-only, exact-`as_of` access to neutral `stock_daily` facts without an application Universe filter.
- `DataQualitySnapshotV1`: immutable, versioned quality facts for each dataset.
- `ReadinessMarkerV1`: strict, atomic publication that references a verified quality snapshot.
- `ArtifactEnvelopeV1`: content-addressed producer/payload metadata with immutable storage and indexing.
- `OutcomeCalculatorV1`: neutral T+N, MFE, MAE, and close-drawdown calculation for caller-owned samples.
- `EligibilityPrimitiveV1`: versioned segment/ST/liquidity facts without an eligibility decision.
- `BoardFactReaderV1` and `CapitalFactReaderV1`: exact-date board and sector-capital facts without V3 score/action semantics.
- `StockCapitalFactReaderV1`, `SectorMembershipReaderV1`, and
  `calculate_sector_strength_v1`: unit-safe vendor capital facts, PIT sector
  membership, and neutral sector breadth facts.

Contract semantics and compatibility rules are documented in [A1_CALENDAR_MARKET_DATA_V1.md](./docs/contracts/A1_CALENDAR_MARKET_DATA_V1.md).
Quality and atomic publication semantics are documented in [A2_QUALITY_READINESS_V1.md](./docs/contracts/A2_QUALITY_READINESS_V1.md).
Artifact and outcome semantics are documented in [A3_ARTIFACT_OUTCOME_V1.md](./docs/contracts/A3_ARTIFACT_OUTCOME_V1.md).
B1 fact boundaries are documented in [B1_ELIGIBILITY_BOARD_CAPITAL_V1.md](./docs/contracts/B1_ELIGIBILITY_BOARD_CAPITAL_V1.md).
B2 supplemental fact boundaries and quality gates are documented in
[B2_SUPPLEMENTAL_CAPITAL_SECTOR_FACTS_V1.md](./docs/contracts/B2_SUPPLEMENTAL_CAPITAL_SECTOR_FACTS_V1.md).

Supplemental facts are published independently from the transitional market
database:

```bash
yifei-platform-supplemental migrate-board \
  --source-db /path/to/legacy.db \
  --target-db /path/to/supplemental_facts.db \
  --published-at 2026-07-27T10:00:00+08:00 \
  --source-version legacy-ths-board.2026-07-27.v1

TUSHARE_TOKEN=... yifei-platform-supplemental backfill-tushare \
  --market-db /path/to/market_data.db \
  --target-db /path/to/supplemental_facts.db \
  --start-date 2026-01-01 \
  --end-date 2026-07-27 \
  --fetched-at 2026-07-27T18:00:00+08:00

yifei-platform-supplemental backfill-public \
  --market-db /path/to/market_data.db \
  --target-db /path/to/supplemental_facts.db \
  --cache-dir /path/to/immutable-cache/2026-07-27-exploration \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --fetched-at 2026-07-27T18:00:00+08:00

yifei-platform-supplemental backfill-membership-public \
  --market-db /path/to/market_data.db \
  --target-db /path/to/supplemental_facts.db \
  --cache-dir /path/to/immutable-cache/2026-07-28-membership \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --fetched-at 2026-07-28T12:00:00+08:00

yifei-platform-supplemental prefetch-capital-sina \
  --market-db /path/to/market_data.db \
  --cache-dir /path/to/immutable-cache/sina-moneyflow-v2 \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --batch-size 100

yifei-platform-supplemental backfill-capital-sina \
  --market-db /path/to/market_data.db \
  --target-db /path/to/supplemental_facts.db \
  --cache-dir /path/to/immutable-cache/sina-moneyflow-v2 \
  --start-date 2026-04-01 \
  --end-date 2026-07-09 \
  --fetched-at 2026-07-28T18:30:00+08:00

yifei-platform-supplemental collect-sector-flow \
  --market-db /path/to/market_data.db \
  --target-db /path/to/supplemental_facts.db \
  --raw-snapshot-root /path/to/immutable-sector-flow \
  --as-of 2026-07-27 \
  --fetched-at 2026-07-27T15:20:00+08:00 \
  --readiness-root /path/to/platform_state
```

The token is read only from an environment variable and is never printed or
stored in the database. `backfill-public` requires no token and uses
Eastmoney HTTPS capital flow, BaoStock daily volume/turnover, and CNInfo
point-in-time SW level-2 industry changes. Install its optional runtime with
`pip install -e '.[public-data]'`.

The public adapter declares BaoStock volume as shares, not lots. It refuses
publication unless traded amount is consistent with `volume * close`, derives
float shares from percentage turnover, and writes normalized capital values in
`CNY`. The per-stock cache is immutable and resumable; use a new cache directory
for every source version or date-range batch.

`backfill-capital-sina` uses Sina's `r0` vendor class as the capital numerator,
keeps total directional flow separate, and never mixes Eastmoney rows into the
same source version. Its unit and anomaly findings are recorded in
[SINA_CAPITAL_FLOW_PILOT_V0.1.md](./docs/reviews/SINA_CAPITAL_FLOW_PILOT_V0.1.md).

`collect-sector-flow` persists V3's `levistock.sector_em` industry response as
an immutable current-day snapshot and publishes normalized
`sector_fund_flow_daily` facts. It refuses intraday publication, backdating,
truncated responses, duplicates, and inconsistent amount/main-inflow units.
The upstream volume field is deliberately not consumed because its share/lot
unit has not been audited.

Run the repository checks with:

```bash
./scripts/quality_gate.sh
```

Before creating a release tag, run the source-distribution and wheel gate:

```bash
./scripts/release_gate.sh
```

The product-family quality process is defined in [CODE_QUALITY_STANDARD.md](./docs/CODE_QUALITY_STANDARD.md).
