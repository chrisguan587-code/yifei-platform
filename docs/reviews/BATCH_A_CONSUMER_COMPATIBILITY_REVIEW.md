# Batch A Consumer Compatibility Review

> Review date: 2026-07-12
> Platform commit reviewed: `a3474e9`
> Result: implementation contracts pass; release/consumer pin remains open.

## Scope

- `TradingCalendarV1`
- `MarketDataReaderV1`
- `DataQualitySnapshotV1` / `ReadinessMarkerV1`
- `ArtifactEnvelopeV1`
- `OutcomeCalculatorV1`

## Findings

| Area | Result | Compatibility decision |
|:--|:--|:--|
| Calendar | Pass | Session-set contract is deterministic; provider adapter still required |
| Market Reader | Pass | Exact historical `as_of` intentionally replaces V3's historical `STALE` behavior |
| Quality/Readiness | Pass | Quality facts are neutral; strict ready does not encode V3/V4 action |
| Artifact | Pass | V3 filename/index layout requires a V3 adapter; payload is not migrated into Platform semantics |
| Outcome | Pass with named semantic change | T+N formula matches V3 baseline; V3 `max_drawdown_3d` is MAE and must not map to V1 max drawdown |
| Dependency direction | Pass | Platform dependency scan rejects application imports |

Automated evidence at review time:

- Platform quality gate: 34 tests passed.
- V3 T+N formula baseline: 1 targeted test passed.
- V3 data-service baseline from A1: 5 targeted tests passed.

## Open Items Before First V4 Platform Pin

1. Keep Platform and V4 as independent local Git repositories during the first implementation stage; V4 must not depend on a sibling source path.
2. After Platform is basically stable, establish a remote and select a fixed Git version for the first V4 consumer tests.
3. Consider Package publication only after Platform is genuinely stable; Package publication is not a current-stage requirement.
4. Add V4 consumer tests only against the selected released Git/Package version, never against an unversioned local checkout.
5. Add a V3 adapter only for consumers that remain active; do not rewrite all V3 artifact call sites.
6. Run Open Code Review against the final release SHA. The current environment has no callable Open Code Review scanner, so this evidence is still pending.
7. Verify an actual Platform-owned market database path and writer before claiming M1 production readiness.

Conclusion: Batch A code is ready for release preparation and consumer testing, but M1 is not yet complete. This review does not authorize V4 to import the current V3 repository or use a local sibling path as a production dependency.

## 2026-07-12 Local-First Decision

The user approved this sequence:

```text
Stage 1: independent local Git repositories; no Platform dependency in V4
Stage 2: establish remotes after Platform is basically stable
Stage 3: consider Package publication after Platform is genuinely stable
```

V4 may proceed with its own Phase 1 objects, Schema, and Repository while Platform integration remains on hold.
