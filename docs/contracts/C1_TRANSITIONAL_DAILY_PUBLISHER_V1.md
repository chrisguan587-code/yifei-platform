# C1 Transitional Daily Publisher V1

This contract is a temporary operational bridge, not the Platform production
data supply chain and not an extension of Bootstrap Migration Publisher.

```text
explicit legacy stock database
+ explicit same-day health artifact (status=success, final_gate=ok)
→ clean temporary public database
→ schema/date/row-count/integrity validation
→ atomic market_data.db replacement
→ immutable v4-market-core ReadinessMarker
```

The command has no source path defaults. It copies only `stock_daily`, derives
`trading_calendar`, and writes neutral `platform_metadata`. It never copies V3
scores, candidates, states, attribution, or artifacts. A missing, failed,
wrong-date, or row-count-mismatched health artifact prevents publication.

Same-day retry is idempotent and may finish marker publication after the data
file was already atomically replaced. Older dates cannot replace newer data.

`historical-st.v1` is a versioned neutral derivation used by consumers while
the source history remains mixed-provider data:

```text
derived_is_st = raw_is_st is true
             OR normalized same-day stock_name matches ^\*?ST(?![A-Z0-9])
```

Raw status, name signal, derived status, reason codes, and rule version remain
available to the consuming application's evidence record. Raw market facts are
never overwritten.

Retirement condition: a Platform-owned updater has published and passed health
checks for five consecutive trading sessions without the V3 data task. Then
the transitional command and its local schedule are disabled and archived.
