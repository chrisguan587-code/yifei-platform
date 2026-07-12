# Platform v0.1.0 Release Readiness Review

> Review date: 2026-07-12
> Scope: first fixed Platform contract release for V4 consumer integration.
> Decision: approved for a versioned read-contract release; not approved as M1 production readiness.

## Release Scope

This release freezes the first consumer-testable versions of:

- `TradingCalendarV1`
- `MarketDataReaderV1`
- `DataQualitySnapshotV1` and `ReadinessMarkerV1`
- `ArtifactEnvelopeV1`
- `OutcomeCalculatorV1`
- `EligibilityPrimitiveV1`
- `BoardFactReaderV1` and `CapitalFactReaderV1`

It does not provide or approve a production market-data writer, production database path,
application eligibility policy, Detector parameters, Setup state, recommendation, or order execution.

## Evidence

- Code reviewed through commit `9a128ea4f438eefa93c9d0f0af570d4f2f7c8e47`.
- Repository quality gate: 40 tests passed.
- Source-distribution gate: 40 tests passed from the generated sdist.
- Wheel gate: build, isolated install, and `0.1.0` import-version verification passed.
- Dependency scan: Platform imports no V3 or V4 application package.
- Repository integrity and whitespace checks passed.
- Package version and runtime `__version__` both equal `0.1.0`.

## Review Findings

### Resolved

- P1: the source distribution omitted `tests/fixtures/market_contract_v1.json`, so its
  contract suite was not reproducible. `MANIFEST.in` now includes contracts, tests, and
  fixtures; the release gate runs tests from the built sdist.
- P2: release verification initially risked validating only the working tree. CI now runs
  the complete release gate, including the built artifacts.

### Open Code Review Exception

No callable Open Code Review, CodeRabbit, reviewdog, or equivalent scanner is installed in
the current environment. The user explicitly approved a one-time exception for `v0.1.0`:
Codex performed the repository-wide code, architecture-boundary, packaging, replay,
missing-data, and dependency review. No unresolved P0 or P1 code finding remains.

This exception applies only to Platform `v0.1.0`. A configured PR review service or a
versioned local review tool is required for later non-trivial releases unless separately
approved and recorded.

## Residual Risks

- A Platform-owned production market database path and single writer are not yet verified.
- V4 consumer compatibility against this fixed release has not yet run.
- Board membership remains unsuitable for point-in-time replay, as documented in the B1 contract.
- There is no audited public individual-stock capital-flow table; consumers must not infer one.

These risks block M1 production readiness where applicable, but do not block publication of
the fixed read-contract version needed for the first V4 consumer test.

## Rollback

The release adds no database migration or writer. Rollback is performed by keeping the
consumer pinned to its previous Platform version and removing no historical tag. Published
tags are immutable; a defect requires a new patch release rather than retagging `v0.1.0`.

## Decision

Platform `v0.1.0` may be tagged after the final tag-target commit passes
`./scripts/release_gate.sh`. V4 may consume only the fixed release identity and must not
depend on a sibling source path or an unversioned branch.
