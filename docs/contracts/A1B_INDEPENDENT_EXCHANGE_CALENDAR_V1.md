# A1B Independent Exchange Calendar Manifest v1

> Status: implemented candidate; production publication requires the V4 coordinated cutover.

## Purpose

This artifact lets an operational consumer decide whether a date is an exchange session even when
the daily market-data publisher or application Runner did not start. It is independent of the
daily market database and Readiness result.

The source for 2026 is Shanghai Stock Exchange announcement
`上证公告〔2025〕45号`. Platform compiles the official closure ranges once into an explicit,
versioned session set. Consumers never infer exchange sessions from weekdays at runtime.

## Manifest

```text
schema_version = exchange-trading-calendar.v1
source_version
source_ref
published_at
coverage_start
coverage_end
sessions[]
```

- Every date inside the coverage interval is classifiable.
- A date in `sessions` is `trading`.
- A date inside coverage but absent from `sessions` is `non_trading`.
- A date outside coverage, an unreadable artifact or an invalid contract is `unknown`.
- `published_at` includes a timezone. Sessions are sorted, unique and inside coverage.
- The writer is immutable and idempotent; different content cannot replace an existing target.

## Ownership and update

Platform owns the source specification and publication. V4 consumes the published artifact
read-only. Emergency exchange changes require a new source version and a newly coordinated
publication; the Finalizer never fetches the network or modifies the artifact at 18:45.
