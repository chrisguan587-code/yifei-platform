# A4 Fixed Day0 A-share Cohort Market Benchmark v1

Status: candidate for the coordinated V4 core-contract cutover.

## Purpose

`a-share-day0-cohort-median-return.v0.1` answers one neutral question:

> From the same Day0 starting line, how much did the typical comparable A-share rise or fall by the current trading session?

It is a Market Facts capability. It is not a V4 eligibility rule, stock-selection rule, score, or market-timing signal.

## Contract

- The cohort is the sorted stock identity set with a reliable traded close in the authoritative Day0 snapshot.
- The cohort does not consume V4 Eligibility, the CNY 50 million threshold, ST policy, or V4 board scope.
- Cohort identity is anchored to Day0 and never replaced by a rolling current-day universe.
- Each member's Day0-to-current return is calculated independently from its official PIT daily percentage-change chain.
- A confirmed no-trade session does not forward-fill price and does not break the chain. A member with no current-session trade is temporarily excluded from that session's median.
- An unexplained missing row or unreliable required value makes only that member non-comparable.
- A member can participate again after reliable trading resumes, provided its intervening history is authoritative.
- Coverage is `comparable_count / cohort_count`. Coverage below `0.95` makes the whole benchmark `data_unknown`.
- The result records cohort identity fingerprint, actual-input fingerprint, price-basis version, and Market Facts source versions.

## Ownership and consumers

Platform owns the cohort and median-return calculation. PostDiscovery may consume the result to calculate:

```text
stock Day0-to-current return - cohort median Day0-to-current return
```

Missing benchmark data makes only that relative-market fact unknown. It must not block activity, support-zone, recent-low, Outcome, other Decision rules, or the daily report.

No separate benchmark registry, routing module, or cohort lifecycle system is introduced.
