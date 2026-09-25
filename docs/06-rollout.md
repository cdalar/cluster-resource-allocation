# 6. Rollout Plan

Enabling quotas on running clusters can block deployments if done carelessly. Roll out in phases,
coordinated with the requests/limits initiative.

## Phase 0 — Discovery (2–4 weeks)

- Inventory: clusters (on-prem + AKS), environments, Rancher Projects, namespaces, namespaces outside any project.
- Confirm whether AKS clusters are managed by Rancher.
- Per project/namespace: Σ requests, Σ limits, actual usage (P95, max), % pods without requests.
- Cluster capacity: allocatable per cluster, system/platform reservations.
- Collect budgets per project and cost data for unit rates.
- Check existing tooling: Rancher Monitoring, policy engine, GitOps (Fleet/Argo/Flux), Terraform.

Tooling: [`discovery/discover.py`](../discovery/README.md) collects requests, limits, usage and standards
compliance per cluster / Rancher Project / namespace.

Output: baseline report + answers to [open questions](open-questions.md).

## Phase 1 — Visibility (observe only)

- Deploy dashboards (requests vs. usage per project) and OpenCost with provisional unit rates.
- Deploy policy engine in **audit** mode for standards S1–S3.
- Share first showback reports with projects. No enforcement yet.

## Phase 2 — Defaults and soft quotas

- Enable **Container Default Resource Limits** (LimitRange) on all Rancher Projects — fixes pods without requests.
- Set Project quotas **generously**: `max(current requests, current peak) × 1.3`, so nothing breaks.
- Put allocations into Git; all further changes via the process.
- Start with dev/test clusters, then acc, then prod.

## Phase 3 — Budget-aligned quotas

- Translate budgets to quotas using published unit rates.
- Where a project's current footprint exceeds its budget-based quota: agree a glide path (right-size, increase
  budget, or accept a staged reduction).
- Switch policies from audit to **enforce** per environment.

## Phase 4 — Steady state

- Yearly allocation cycle and quarterly reviews running.
- Consider chargeback, automated reclaim proposals, in-place pod resize, etc.

## Risks

| Risk | Mitigation |
|---|---|
| Quota blocks rollouts / restarts in prod | Generous initial quotas, surge headroom, staged rollout starting with non-prod, emergency procedure |
| Defaults (LimitRange) too small → OOMKills for workloads without limits | Measure in Phase 0; set defaults from observed usage; coordinate with req/lmt initiative |
| Projects over-request to "reserve" capacity | Billing on allocated quota + quarterly reclaim |
| Manual UI edits drift from Git | Restrict quota edit rights to pipeline service account; drift detection via Terraform plan |
| Unit rates disputed | Transparent TCO model, reviewed with finance, published ahead of the cycle |
| AKS not in Rancher → two mechanisms | Generate both from the same allocation files |
