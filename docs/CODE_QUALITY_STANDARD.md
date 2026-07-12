# Yifei Code Quality Standard

> Version: v1.0 (2026-07-12)
> Scope: `yifei-platform`, `yifei-v3`, `yifei-v4`, and future Yifei repositories.
> Authority: this document defines the product-family quality process. Repository profiles may add stricter gates but may not weaken it silently.

## 1. Objective

Quality means more than code that runs. A change is acceptable only when its behavior, ownership, evidence timing, failure semantics, and rollback path are reviewable.

```text
Automated checks prove repeatable mechanics.
Open Code Review searches for implementation blind spots.
Architecture review protects ownership and causality.
Replay and operational gates prove production fitness.
```

No single layer replaces another.

## 2. Mandatory Flow

```text
Issue / Contract Reference
        ↓
Small implementation + tests
        ↓
Local Quality Gate
        ↓
Pull Request
        ↓
Open Code Review + human review
        ↓
Resolve findings and rerun gates
        ↓
Merge
        ↓
Phase Gate / Production Gate when applicable
```

Direct changes to the protected branch are forbidden after a remote repository and branch protection are configured.

## 3. Change Preparation

Every non-trivial change must state:

- Purpose and explicit non-goals.
- Owning repository and contract or issue reference.
- Inputs, outputs, persistent state, and dependency changes.
- Verifiable acceptance criteria.
- Test and rollback plan.
- Data migration or compatibility impact, if any.

Trading parameters and architecture contracts are different changes. Do not hide a rule change inside infrastructure work.

## 4. Local Quality Gate

Before opening a PR, the author runs the repository quality command:

```bash
./scripts/quality_gate.sh
```

The minimum automated gate includes:

1. Python syntax compilation.
2. Unit and contract tests.
3. Repository dependency-boundary tests.
4. Deterministic migration/replay tests when persistent state changes.
5. `git diff --check` before commit.

Lint, formatting, type checking, security scanning, and dependency auditing become mandatory when their tools are introduced in a versioned repository change. A missing tool must not be reported as a passing check.

## 5. Pull Request Gate

Every PR must be small enough to review and must include the repository PR checklist. The following block merge:

- Failing required CI.
- Missing tests for changed behavior.
- Unresolved P0 or P1 review findings.
- Undocumented schema, contract, dependency, or compatibility changes.
- Cross-application imports or unauthorized database access.
- Future-data leakage or mutation of immutable history.
- No rollback path for a persistent or operational change.

Documentation-only changes may omit runtime tests when the PR records why, but link and consistency checks still apply.

## 6. Open Code Review Standard

Run Open Code Review:

1. On every non-trivial PR before merge.
2. At each Phase completion over the complete phase diff.
3. Before enabling a real daily runner, data writer, notification path, or migration.
4. Before V3 retirement or any cross-repository cutover.

The review prompt/scope must ask explicitly about:

- Ownership and forbidden dependency violations.
- Incorrect state writers and mutation of immutable records.
- Point-in-time leakage and retrospective field backfill.
- Idempotency, transaction boundaries, retries, and duplicate delivery.
- Missing/degraded/stale semantics.
- Schema compatibility and migration rollback.
- Tests that pass without proving the promised behavior.
- Security, secrets, unsafe file/database access, and supply-chain risk.

Record the reviewed commit SHA, scope, findings, resolutions, and residual risks in the PR or Phase report. A review of an older SHA is not evidence for later unreviewed changes.

## 7. Finding Severity

| Severity | Meaning | Merge Rule |
|:--|:--|:--|
| P0 | Data corruption, future leakage, security breach, wrong ownership, irreversible production failure | Must fix; phase stops |
| P1 | Material behavioral bug, state inconsistency, broken idempotency/compatibility, missing critical test | Must fix before merge |
| P2 | Maintainability or bounded correctness risk without immediate material failure | Fix or record owner and deadline |
| P3 | Improvement, clarity, or optional hardening | Non-blocking |

Severity may not be lowered only to permit merge. A disputed P0/P1 requires an explicit written architecture decision.

## 8. Phase Gate

A phase is complete only when all of the following exist:

- Acceptance criteria mapped to passing evidence.
- Full phase test result.
- Open Code Review of the final phase SHA.
- Architecture-boundary review.
- Data/schema migration and rollback result where relevant.
- Known limitations and deferred P2/P3 items.
- Written completion decision.

“Mostly complete” and percentage estimates do not satisfy a Phase Gate.

## 9. Data and Trading-System Gates

Changes involving market data, outcomes, attribution, or decision support additionally require:

- Explicit `as_of`, source version, schema/rule version, and missing semantics.
- Point-in-time tests proving future fields are unavailable.
- Deterministic replay for the same input and version.
- Immutable capture separated from later outcome enrichment.
- Trading-session rather than calendar-day verification where applicable.
- Fixed price-basis and corporate-action semantics.
- No automatic order-execution implication.

Lower output volume is not evidence of higher signal quality. Evaluation must retain the eligible-universe denominator and rejected/pending observations needed to measure recall and noise.

## 10. Repository-Specific Boundaries

### Platform

- Must not import an application package.
- Owns facts and neutral capabilities, not Strategy, Setup, Pattern, State, score, or recommendation.
- Application readers are read-only; market-data writer ownership is singular.
- Breaking contracts require a major version and consumer contract evidence.

### Applications

- Must not import another application or access its application database.
- Own interpretation, state, rendering, samples, and decisions.
- Pin a released Platform version.
- Persist the evidence snapshot actually used at decision time.

## 11. Production Gate

Before a new real runner or cutover is enabled:

1. CI, contract tests, replay, failure injection, and Open Code Review pass on the release SHA.
2. Secrets, paths, permissions, locks, logging, alerting, and idempotency keys are verified.
3. Backout is executable and does not require reconstructing overwritten history.
4. The previous application can be stopped without interrupting Platform or the new application.
5. A named human records the enablement decision.

## 12. Required Evidence

Keep the following with the PR or Phase report:

```text
commit SHA
contract / issue reference
quality command and result
Open Code Review scope and findings
resolved and deferred findings
migration / replay result
rollback procedure
approval decision
```

The permanent principle is:

```text
No evidence, no merge.
No replay, no state migration.
No final-SHA review, no phase completion.
```
