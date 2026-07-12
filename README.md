# Yifei Platform

Shared Platform owns market facts and versioned neutral capabilities used by Yifei applications.

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

Contract semantics and compatibility rules are documented in [A1_CALENDAR_MARKET_DATA_V1.md](./docs/contracts/A1_CALENDAR_MARKET_DATA_V1.md).
Quality and atomic publication semantics are documented in [A2_QUALITY_READINESS_V1.md](./docs/contracts/A2_QUALITY_READINESS_V1.md).
Artifact and outcome semantics are documented in [A3_ARTIFACT_OUTCOME_V1.md](./docs/contracts/A3_ARTIFACT_OUTCOME_V1.md).
B1 fact boundaries are documented in [B1_ELIGIBILITY_BOARD_CAPITAL_V1.md](./docs/contracts/B1_ELIGIBILITY_BOARD_CAPITAL_V1.md).

Run the repository checks with:

```bash
./scripts/quality_gate.sh
```

Before creating a release tag, run the source-distribution and wheel gate:

```bash
./scripts/release_gate.sh
```

The product-family quality process is defined in [CODE_QUALITY_STANDARD.md](./docs/CODE_QUALITY_STANDARD.md).
