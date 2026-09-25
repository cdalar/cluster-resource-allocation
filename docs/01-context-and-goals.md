# 1. Context and Goals

## Background

The platform team runs shared Kubernetes clusters for multiple delivery streams / projects.

| Aspect | Current situation |
|---|---|
| Hosting | Mainly on-prem clusters, plus some AKS clusters |
| Management | Rancher (on-prem clusters); AKS integration with Rancher to be confirmed |
| Environments | Several (e.g. dev / test / acc / prod), typically separate clusters per environment |
| Tenancy | Shared clusters. Each stream/project has a **Rancher Project** containing several namespaces |
| Workloads | Mostly long-running, classic services (Deployments/StatefulSets) |
| Funding | A **budget per project** exists |
| Resource governance | **None** today: no quotas, no default limits, no showback |
| Related work | A separate ongoing initiative is setting requests/limits on Deployments |

## Problem statement

Cluster capacity is consumed on a first-come-first-served basis. There is no link between what a project
pays (budget) and what it may consume (capacity). Consequences:

- **Noisy neighbours**: one project can exhaust a cluster and cause Pending pods or evictions for others.
- **No capacity planning signal**: on-prem hardware has long lead times; without allocations we cannot forecast.
- **No accountability**: projects have no visibility of their footprint or cost, and no incentive to right-size.
- **Unfair distribution**: projects with larger budgets do not get correspondingly guaranteed capacity.

## Goals

1. **Guaranteed, budget-backed capacity** per project, per environment.
2. **Hard limits** so no project can consume more than its allocation.
3. **A clear, lightweight process** to request, approve, change and review allocations.
4. **Transparency**: each project sees allocation vs. requested vs. actual usage, and its cost.
5. **Capacity planning input**: sum of allocations drives hardware / AKS node pool planning.
6. **Self-service within budget**: projects can redistribute their allocation across their own namespaces
   without platform-team involvement.

## Non-goals (for now)

- Automatic scaling of quota with usage.
- Real financial chargeback (cross-charging invoices). Start with **showback**; chargeback may follow.
- Batch/ML/GPU queueing (Kueue etc.) — not needed for the current workload profile.
- Setting requests/limits for individual workloads — that is the scope of the related req/lmt initiative.
  This project defines the **standards** those values must follow and the **envelopes** they must fit in.

## Guiding principles

- **Allocate on requests, not on usage.** Requests are what the scheduler reserves; that is the capacity a
  project blocks for others.
- **Use what Rancher already gives us** (Project quotas, container default limits) before adding tools.
- **Everything as code**: allocations live in Git, applied automatically; the UI is read-only for quota.
- **Measure before enforcing**: roll out in observe → soft → hard stages.
- **Same model on-prem and AKS**, even if the implementation mechanism differs.

## Stakeholders

| Role | Interest |
|---|---|
| Platform team | Owns clusters, capacity, the allocation mechanism and process |
| Project / stream leads | Consume capacity, own their budget, request changes |
| Finance / budget owners | Budgets, unit rates, showback reporting |
| Infrastructure / DC team | On-prem hardware procurement and lead times |
| Architecture / management | Approve policy and escalations |
