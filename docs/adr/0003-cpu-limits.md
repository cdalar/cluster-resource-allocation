# ADR-0003: No quota on CPU limits; CPU limits optional

- **Status**: Proposed (to align with the requests/limits initiative)
- **Date**: 2026-09-23
- **Deciders**: Platform team, req/lmt initiative

## Context

CPU is a compressible resource. CPU limits enforce throttling via CFS quota even when the node has idle CPU,
which often causes latency problems (notably for JVM and multi-threaded services). CPU *requests* already
guarantee each container its share under contention.

If a `ResourceQuota` includes `limits.cpu`, Kubernetes requires every container to set a CPU limit.

## Options

1. Require CPU limits and quota `limits.cpu`.
2. Make CPU limits optional; quota only `requests.cpu`.

## Decision

Option 2: quota `requests.cpu` only; CPU limits optional (if set, recommended ≥ 2× request). Memory limits remain
mandatory and are quota'd.

## Consequences

- Better CPU utilisation and fewer throttling issues.
- A misbehaving workload can use idle CPU on a node, but never steals *requested* CPU from others.
- Workloads will mostly be `Burstable` QoS rather than `Guaranteed`; acceptable for classic services.
- If the req/lmt initiative has standardised on CPU limits, revisit this ADR.
