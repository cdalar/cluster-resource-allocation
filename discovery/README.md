# Discovery: current requests and usage

`discover.py` collects the Phase 0 baseline ([rollout plan](../docs/06-rollout.md)): per cluster, per Rancher
Project and per namespace, how much is **requested**, **limited** and **actually used**, and how well workloads
follow the [resource standards](../docs/02-resource-standards.md).

Read-only. Needs only Python 3.8+ and `kubectl` — no extra packages.

## Usage

```bash
# Current kubectl context
./discover.py

# Several downstream clusters, with Rancher Project display names from the Rancher local cluster
./discover.py --context onprem-prod-01 --context onprem-test-01 --context aks-prod \
              --rancher-local-context local --window 7d --out baseline-2026-09

# AKS cluster not managed by Rancher: group namespaces by a label instead of Rancher Project
./discover.py --context aks-prod --project-label project

# Prometheus elsewhere (e.g. Azure Managed Prometheus / other service)
./discover.py --context aks-prod --prom-url https://prom.example --window 14d   # token in $PROM_TOKEN
./discover.py --context aks-prod --prom-service monitoring/http:prometheus-k8s:9090
./discover.py --prometheus none                                              # API + metrics-server only
```

With Rancher, download each cluster's kubeconfig from the Rancher UI (or merge them) so every cluster is a
context. Rancher kubeconfigs proxy through the Rancher server, so the script works from any machine with
Rancher access.

## What it collects

| Source | Data | Required? |
|---|---|---|
| Kubernetes API | nodes (allocatable), namespaces (Rancher project), pods (requests/limits), ResourceQuotas, LimitRanges, HPAs | Yes |
| metrics-server | usage snapshot ("now") | Optional |
| Prometheus (default: Rancher Monitoring via API server service proxy) | CPU/memory usage avg / P95 / max and peak requests over `--window` | Optional |
| Rancher local cluster (`--rancher-local-context`) | Project and cluster display names | Optional |

Permissions: see [`rbac.yaml`](rbac.yaml).

## Output

Console summary per cluster, plus three CSVs in `--out`:

| File | One row per | Use |
|---|---|---|
| `clusters.csv` | cluster | Allocatable vs. requested (tenant / system / unassigned), pending, standards compliance |
| `projects.csv` | cluster × Rancher Project (namespaces without a project are listed individually) | Baseline for initial quotas; efficiency |
| `namespaces.csv` | cluster × namespace | Detail; per-namespace quota defaults |

Key columns:

| Column | Meaning |
|---|---|
| `category` | `tenant` (in a Rancher Project / has `--project-label`), `system` (Rancher *System* project or matches `--system-ns-regex`), `unassigned` (no project) |
| `cpu_requests`, `mem_requests_gib` | Σ **effective** pod requests of non-terminated pods (as the scheduler and ResourceQuota count them: incl. init/sidecar containers and pod overhead). Includes pending pods |
| `cpu_requests_pending`, … | Part of the above from pods not yet scheduled |
| `cpu_limits`, `mem_limits_gib` | Σ limits that are set (containers without a limit add 0) |
| `containers_no_cpu_request`, `containers_no_mem_request`, `containers_no_mem_limit` | Standards S1/S2 violations |
| `containers_cpu_limit` | Containers that set a CPU limit (input for ADR-0003) |
| `pods_besteffort` | Pods with no requests/limits at all (S5) |
| `cpu_usage_now`, `mem_usage_now_gib` | metrics-server snapshot |
| `cpu_usage_{avg,p95,max}`, `mem_usage_{avg,p95,max}_gib` | Namespace-total usage over `--window` (Prometheus) |
| `cpu_requests_peak`, `mem_requests_peak_gib` | Max Σ requests over `--window` (captures HPA scale-outs and rollouts) |
| `cpu_request_efficiency_pct`, `mem_request_efficiency_pct` | P95 usage / requests (projects.csv; falls back to snapshot without Prometheus) |
| `*_pct_of_schedulable` | Scheduled requests / allocatable of schedulable nodes (clusters.csv) |

## Deriving a first quota (Phase 2)

A reasonable generous starting point per project and cluster, as in the rollout plan:

```
quota.requests.cpu    ≈ max(cpu_requests, cpu_requests_peak) × 1.3
quota.requests.memory ≈ max(mem_requests_gib, mem_requests_peak_gib) × 1.3
```

and compare Σ quotas with `alloc_*_schedulable` × the headroom ratio for the environment
([allocation model](../docs/03-allocation-model.md#headroom-and-overcommit)).

## Caveats

- **LimitRange defaults hide missing requests**: in namespaces with a LimitRange, pods get default values at
  admission, so they are not counted as "no request". Check the `limitranges` column.
- **Prometheus retention**: Rancher Monitoring keeps ~10 days by default. The script warns if there is less
  history than `--window`. For quarterly reviews, longer retention (or Thanos/remote storage) is needed.
- **Peak requests** come from kube-state-metrics, which exports app containers only (no init/sidecar containers).
  The script never reports a peak below the current effective requests, but a past peak may be slightly understated
  for pods with sidecars.
- **Project-level P95/max** are sums of namespace-level values — slightly conservative (peaks may not coincide).
- Windows and subqueries over large clusters can be heavy for Prometheus; use a coarser `--step` (e.g. `15m`) if
  queries time out.

## Tests

```bash
python3 -m unittest -v test_discover
```

The script was verified end-to-end on a k3s cluster with fake Rancher Project objects, a kube-prometheus-stack
installed under Rancher Monitoring's service name, and fixture workloads (sidecars, init containers,
BestEffort, pending, HPA, existing quota/LimitRange).
