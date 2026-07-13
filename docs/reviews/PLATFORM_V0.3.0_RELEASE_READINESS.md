# Platform v0.3.0 Release Readiness Review

> Review date: 2026-07-13
> Decision: approved as a bounded transition release, not as the permanent Platform updater.

## Scope

- Add `transitional-daily-market-data.v1`, gated by an explicit same-day source
  health artifact and retired after five independent Platform-updater sessions.
- Add serialized, validated, atomic public database publication and recoverable
  same-day marker completion.
- Add `historical-st.v1`, preserving raw status while deriving a conservative
  ST fact from the same-day name prefix.
- Add published market metadata loading so consumers retain release lineage.

## Evidence

- Platform quality gate: 48 tests passed.
- V4 consumer suite against the working v0.3.0 contract: 83 tests passed.
- Open Code Review examined the complete Platform diff and focused scans. The
  target publication race, schema validation, shell diagnostics, and same-day
  stale-source risk were fixed. The remaining scan note is an accepted retry
  cost: rebuilding the source is required to prove content equality before a
  readiness marker can be recovered.
- The transitional command has no V3 path default and copies no V3 application
  table or interpretation.

## Boundaries

This release does not make Bootstrap a daily writer and does not authorize V4
to read V3. The V3 database and health paths exist only in local deployment
arguments to the transitional command. A missing or failed health artifact
publishes neither data nor readiness.

## Rollback

Keep V4 pinned to v0.2.0 and disable the two local schedules. Published tags are
immutable; defects require a new release. An already published public database
is not rolled backward to an older `as_of`.
