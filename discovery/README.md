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

# Same, but the Rancher local cluster via its own kubeconfig file (downloaded from Rancher)
./discover.py --context onprem-prod-01 --rancher-local-kubeconfig ~/Downloads/local.yaml

# Scanning the Rancher local cluster itself: read the names from it
./discover.py --context rancher-local --rancher-local-self

# Downstream cluster without any Rancher access: names from the ConfigMap published by publish_rancher_names.py
./discover.py --context onprem-prod-01 --rancher-names-configmap resource-report/rancher-project-names

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
| Rancher local cluster (`--rancher-local-context` and/or `--rancher-local-kubeconfig`, or `--rancher-local-self` when scanning it), or the `rancher-project-names` ConfigMap (`--rancher-names-configmap`) | Project and cluster display names | Optional |

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
| `largest_node_cpu`, `largest_node_mem_gib` | Allocatable of the largest schedulable node (clusters.csv) |
| `alloc_cpu_n1`, `alloc_mem_gib_n1` | N+1: schedulable allocatable minus the largest node, i.e. what is left if it fails |
| `cpu_for_projects_n1`, `mem_gib_for_projects_n1` | N+1 capacity minus the `system` (platform component) requests: what projects can safely have. The dashboard shows it as the dashed line in *Cluster capacity*, the text under it and the *Room for projects (N+1)* tile |

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

## In-cluster dashboard

`server.py` runs the same collection on a schedule inside a cluster and serves a web dashboard, a JSON API and
CSV downloads. Deploy it with the Helm chart in [`charts/cluster-resource-report`](../charts/cluster-resource-report/README.md),
which uses the image built from this directory's `Dockerfile`.

To look at a saved report locally without cluster access:

```bash
python3 server.py --no-collect --data-dir <dir-with-report.json> --listen 127.0.0.1:8080
```

## Project names on downstream clusters without a token

Downstream clusters only carry project IDs. Instead of giving each cluster a token for the Rancher local cluster,
`publish_rancher_names.py` runs on the local cluster and applies one Fleet `Bundle` (no GitRepo) whose only
resource is a ConfigMap `rancher-project-names` with `projects.json` and `clusters.json`. Fleet delivers it to the
downstream clusters (workspace `fleet-default`, all clusters or a `--cluster-selector`), and the report reads it
there with `--rancher-names-configmap <ns>/rancher-project-names`.

```bash
./publish_rancher_names.py --context rancher-local --dry-run   # print the Bundle
./publish_rancher_names.py --context rancher-local             # apply it
```

It needs read on `projects`/`clusters.management.cattle.io` and create/get/patch on that one Bundle. The ConfigMap
holds the names of all projects in the workspace, so every targeted cluster sees them. Names show up downstream
after the next publish and Fleet sync. In the chart this is `rancher.publishNames` (local) and
`rancher.namesConfigMap` (downstream).

## Allocation planner

User guide with every parameter explained: [guides/allocation-planner.md](../guides/allocation-planner.md).

`server.py --planner` (Rancher local cluster only; needs `--rancher-local-self`, `--rancher-local-context` or
`--rancher-local-kubeconfig`) adds a planning page at `/planner`, implemented in `planner.py`:

- **Input:** one pair of unit rates (€ per vCPU-month / GiB-month), per environment the node failures to tolerate,
  the maximum Σ quota as a share of allocatable and the memory-limit factor, per cluster its environment and
  platform reserve, and per project a monthly
  budget, cost center, owners and a CPU/memory quota per cluster -- typed in, converted from an amount with the
  unit rates, or taken from current requests + 25 %.
- **Rancher inventory (read-only):** `clusters.management.cattle.io` (allocatable, requested, nodes) and
  `projects.management.cattle.io` (current quota). Current requests per project come from this collector's own
  report, so only for the cluster it scans.
- **Checks:** planned cost vs. budget per project; planned quota vs. each cluster's **limit for projects** =
  min(allocatable − the environment's N largest nodes (`nodes.management.cattle.io`) − platform reserve,
  max % × (allocatable − platform reserve)), per CPU and memory; projects missing in Rancher; clusters without
  environment/platform/platform reserve. The platform reserve is measured (`system` requests) for the cluster the
  collector scans and entered for the others.
- **Storage:** `<data-dir>/planner.json` with a version number (a save based on an older version is refused with
  409, so two people can't overwrite each other) and the last 30 versions in `planner-history/`.
- **Export:** `/api/planner/export.yaml`, one document per project in the allocation-file format of docs/04, with
  `limits.memory` = requests × the environment's factor.

It never writes to a cluster; applying stays with the Git / Terraform flow.

## Tests

```bash
python3 -m unittest -v test_discover test_server test_planner
```

The script was verified end-to-end on a k3s cluster with fake Rancher Project objects, a kube-prometheus-stack
installed under Rancher Monitoring's service name, and fixture workloads (sidecars, init containers,
BestEffort, pending, HPA, existing quota/LimitRange).
It was also run against a real Rancher v2.15.2: the dashboard on the Rancher local cluster (k3s, with
`rancher.isLocalCluster`) and on an imported k3s cluster, with stream Projects on both.
