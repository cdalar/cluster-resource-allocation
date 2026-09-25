# 2. Resource Standards (requests & limits)

These standards are the contract between this project and the ongoing requests/limits initiative.
Quotas only work well if workloads declare resources consistently.

## Kubernetes basics (short)

| Concept | What it does | Why it matters for allocation |
|---|---|---|
| `requests` | Amount the scheduler **reserves** on a node | This is the capacity a project occupies → **basis for quota and cost** |
| `limits` | Hard cap at runtime. CPU over limit → throttled; memory over limit → OOMKilled | Protects nodes, but CPU limits often hurt latency |
| QoS class | `Guaranteed` (req = lim for all), `Burstable`, `BestEffort` | Determines eviction order under node pressure |

## Standards

| # | Standard | Rationale |
|---|---|---|
| S1 | Every container **must** set `requests.cpu` and `requests.memory` | Without requests quota and scheduling are meaningless |
| S2 | Every container **must** set `limits.memory` | Memory is not compressible; unbounded memory destabilises nodes |
| S3 | `limits.memory` = `requests.memory` for prod; ≤ 2× request allowed in non-prod | Avoids OOM-driven evictions of neighbours; predictable behaviour |
| S4 | `limits.cpu` is **optional**. If set, ≥ 2× `requests.cpu` recommended | CPU is compressible; tight CPU limits cause throttling even on idle nodes. See [ADR-0003](adr/0003-cpu-limits.md) |
| S5 | No `BestEffort` pods (no requests) in any environment | They can’t be accounted for and get evicted first |
| S6 | Requests should reflect ~P95 of observed usage (right-sizing) | Keeps allocation efficient; reviewed with VPA/KRR recommendations |
| S7 | JVM/runtime heap settings must fit inside `limits.memory` (e.g. `-XX:MaxRAMPercentage=75`) | Avoids OOMKills from runtime unaware of container limits |
| S8 | Sidecars and init containers count too | They consume quota like any container |

> Note for S3/S4: these are proposals to be aligned with the requests/limits initiative. Whatever is agreed must be
> reflected in both the Rancher container defaults ([04](04-technical-design.md)) and the policy engine rules.

## Default values (when a workload does not specify)

Applied through the Rancher **Container Default Resource Limit** (rendered as a `LimitRange` in each namespace).
Defaults are a safety net, not a target — they should be visible in reports as "defaulted".

| Environment | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|
| Non-prod | 50m | – (not set) | 128Mi | 256Mi |
| Prod | 100m | – (not set) | 256Mi | 256Mi |

Values are initial proposals; tune after discovery.

## Units reminder

- CPU: `1` = 1 vCPU/core, `100m` = 0.1 vCPU.
- Memory: use binary units `Mi`/`Gi` (not `M`/`G`).
