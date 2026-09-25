# ADR-0002: Showback/cost basis is the allocated quota

- **Status**: Proposed
- **Date**: 2026-09-23
- **Deciders**: Platform team, Finance

## Context

Projects have budgets. We need a cost basis that connects budget to capacity and discourages hoarding.

## Options

1. **Allocated quota** — cost = quota × unit rate.
2. **Actual requests** — cost = Σ pod requests over time × unit rate.
3. **max(requests, usage)** — common in FinOps tools for shared clusters.

## Decision

Use **allocated quota** as the cost basis. Report requests and usage as efficiency metrics alongside it.

## Consequences

- Cost is predictable and maps 1:1 to the budget.
- Projects are incentivised to return unused quota (lower cost) — supported by the quarterly reclaim process.
- Right-sizing workloads does not reduce cost by itself unless the project also lowers its quota; the report must
  make that link explicit.
- OpenCost's default allocation (requests/usage-based) is used for efficiency insights, not for the cost figure.
