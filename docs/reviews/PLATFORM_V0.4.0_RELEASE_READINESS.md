# Platform v0.4.0 Release Readiness Review

> Review date: 2026-07-13
> Decision: approved for fixed Platform release; V4 pin remains conditional on its consumer gate.

## Scope

- Add explicit `pending` Outcome semantics when the caller supplies an evaluation cutoff and the requested window has not matured.
- Add opt-in `price-lineage-guard.candidate.v1` with a frozen 0.1% tolerance between each session's `preclose` and the preceding session's `close`.
- Preserve existing OutcomeCalculatorV1 behavior for callers that do not supply a cutoff or guard.

## Boundary

The Platform still owns only neutral post-observation calculations and data-quality semantics. It does not select samples, determine opportunity maturity, aggregate win rates, or write V4 attribution state. The guard marks a price window unavailable; it does not infer a corporate action type or repair prices.

## Evidence

- Platform release gate passed: 50 source-tree tests, 50 sdist tests, wheel build, isolated wheel install, and `0.4.0` version verification.
- Contract tests cover missing, zero, unpublished, pending, and discontinuous price cases; none become zero return.
- Open Code Review session `1a3c37e7-4349-45e6-bc66-ed120b734c8f` found one repeated-read performance issue and one import-style issue. Both were resolved with a per-calculation cache and module-level import.
- Final focused Open Code Review session `0ab11f70-fbc8-401a-a091-5d81674b63ee` reported no findings.

V4 consumer compatibility is deliberately evaluated after this release commit obtains an immutable SHA. V4 must remain on its prior pin until that gate passes.

## Rollback

Keep V4 pinned to Platform v0.3.0. Published tags are immutable; any defect requires a new release rather than retagging v0.4.0.
