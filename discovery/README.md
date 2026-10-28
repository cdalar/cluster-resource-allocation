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
| `category` | `tenant` (in a Rancher Project / has `--project-label`), `system` (project name matches `--system-project-regex`: Rancher's *System* project or a name containing `platform`; or the namespace matches `--system-ns-regex`), `unassigned` (no project) |
| `cpu_requests`, `mem_requests_gib` | Σ **effective** pod requests of non-terminated pods (as the scheduler and ResourceQuota count them: incl. init/sidecar containers and pod overhead). Includes pending pods |
| `cpu_requests_pending`, … | Part of the above from pods not yet scheduled |
| `cpu_limits`, `mem_limits_gib` | Σ limits that are set (containers without a limit add 0) |
| `containers_no_cpu_request`, `containers_no_mem_request`, `containers_no_mem_limit` | Standards S1/S2 violations |
| `containers_cpu_limit` | Containers that set a CPU limit (input for ADR-0003) |
| `pods_besteffort` | Pods with no requests/limits at all (S5) |
| `cpu_usage_now`, `mem_usage_now_gib` | metrics-server snapshot |
| `cpu_usage_{avg,p95,max}`, `mem_usage_{avg,p95,max}_gib` | Namespace-total usage over `--window` (Prometheus) |
| `cpu_requests_peak`, `mem_requests_peak_gib` | Max Σ requests over `--window` (captures HPA scale-outs and rollouts) |
| `quota_names`, `quota_rancher` | The namespace's ResourceQuotas (`(scoped)` = only counts some pods, not summed), and whether Rancher manages them (Project quota: namespace annotation `field.cattle.io/resourceQuota` or Rancher's label on the quota) |
| `quota_cpu_hard`, `quota_cpu_used`, `quota_mem_hard_gib`, `quota_mem_used_gib` | The tightest `requests.cpu` / `requests.memory` (or `cpu` / `memory`) hard value with its `status.used`; empty without a quota. Also `quota_cpu_limit_hard`, `quota_mem_limit_hard_gib`, `quota_mem_limit_used_gib`, `quota_pods_hard`, `quota_pods_used`. The dashboard's *Requests vs. ResourceQuota* graphs `cpu_requests` / `mem_requests_gib` against these |
| `namespaces_with_quota`, `quota_cpu_hard`, `quota_mem_hard_gib`, `cpu_requests_with_quota`, `mem_requests_with_quota_gib` | projects.csv: how many of the project's namespaces have a quota, Σ of their hard values and Σ of their requests (what the quota sum applies to); the other namespaces' requests are unlimited |
| `cpu_request_efficiency_pct`, `mem_request_efficiency_pct` | P95 usage / requests (projects.csv; falls back to snapshot without Prometheus) |
| `*_pct_of_schedulable` | Requests on schedulable nodes / allocatable of schedulable nodes (clusters.csv). *Schedulable* = not cordoned and no `NoSchedule` / `NoExecute` taint, so dedicated control-plane and etcd nodes are not capacity |
| `cpu_requests_excluded_nodes`, `mem_requests_excluded_nodes_gib` | Part of the requests on nodes that are not schedulable (tainted control plane, cordoned): counted in requests and quota, but not against the schedulable nodes; `*_excluded_system*` = the System part of it (clusters.csv) |
| `largest_node_cpu`, `largest_node_mem_gib` | Allocatable of the largest schedulable node (clusters.csv) |
| `alloc_cpu_n1`, `alloc_mem_gib_n1` | N+1: schedulable allocatable minus the largest node, i.e. what is left if it fails |
| `cpu_for_projects_n1`, `mem_gib_for_projects_n1` | N+1 capacity minus the `system` (platform component) requests on schedulable nodes: what projects can safely have. The dashboard shows it as the dashed line in *Cluster capacity*, the text under it and the *Room for projects (N+1)* tile |

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
- The usage history is four range queries per cluster (CPU and memory usage, CPU and memory requests, one value per
  namespace and `--step`), run at the same time; avg / P95 / max are computed from the samples the way Prometheus'
  `avg_over_time`, `quantile_over_time(0.95)` and `max_over_time` do. The window is read in several requests when
  one response would hold more than 200,000 samples (steps × namespaces) or 11,000 steps. On a large cluster use a
  coarser `--step` (e.g. `15m`) if the queries time out.

## In-cluster dashboard

`server.py` runs the same collection on a schedule inside a cluster and serves a web dashboard, a JSON API and
CSV downloads. The report is serialized once per collection; `/api/report` is gzip-compressed for clients that
accept it and answers `304 Not Modified` to the dashboard's polls (every minute, with the last `ETag`) while
nothing changed. Deploy it with the Helm chart in [`charts/cluster-resource-report`](../charts/cluster-resource-report/README.md),
which uses the image built from this directory's `Dockerfile`.

On the Rancher local cluster, `--downstream-report NAMESPACE/SERVICE:PORT` (where this dashboard's Service is on
the downstream clusters) adds `/api/clusters`, the clusters Rancher manages. The page, when opened through
Rancher's proxy (`/k8s/clusters/<id>/.../proxy/`), then loads every downstream cluster's own `/api/report` through
the same proxy with the user's Rancher session and shows an *All clusters* overview (totals, capacity per
cluster, a clusters table) plus each cluster's dashboard behind the cluster selector. The server never talks to
downstream clusters and holds no token; the user sees the clusters Rancher lets them access. A downstream report
that calls its cluster `in-cluster` (no names ConfigMap) is shown under its Rancher name.

**Live view** (`/live`, `live.py`; off with `--no-live`): the server follows nodes, namespaces and pods with
Kubernetes watches (`kubectl get --raw '/api/v1/pods?watch=1...'`, one list then a stream of events per resource,
repeated every 30 minutes and after errors) and reads metrics-server every `--live-metrics-interval` seconds
(default 15; usage shows *n/a* without it). It keeps compact summaries in memory: each pod's effective requests and
limits, its owner workload (Deployment through the ReplicaSet's `pod-template-hash`, StatefulSet, DaemonSet, Job,
static pod, bare pod), node, phase, per-container figures and the PodDisruptionBudgets that select it; finished
pods are dropped. A workload's pods per node (skew, as topology spread constraints count it) and an N+1 check (if
the node with most of its pods fails, do the remaining pods satisfy its PodDisruptionBudget, or is it an outage?)
are shown per workload. The page drills Node →
Rancher Project → Namespace → Workload → Pod → Container, or starts from the projects, with every level's
requests, limits and usage against the allocatable (the node's, or the schedulable nodes'); the URL hash
(`#live/node=.../project=.../ns=.../wl=<kind>/<name>/pod=...`) addresses a level. `/api/live/snapshot` is the whole
state, `/api/live/changes?since=N` what changed after seq `N`, `/api/live/stream` the same as Server-Sent Events;
the page falls back to polling when a proxy does not pass the stream. Project names come from the collector's
report (so a new namespace shows its project id until the next collection). The live view follows one cluster:
with several `--context`, pass `--no-live`.

To look at a saved report locally without cluster access:

```bash
python3 server.py --no-collect --data-dir <dir-with-report.json> --listen 127.0.0.1:8080
```

## Project names on downstream clusters without a token

Downstream clusters only carry project IDs. Instead of giving each cluster a token for the Rancher local cluster,
`publish_rancher_names.py` runs on the local cluster and applies one Fleet `Bundle` (no GitRepo) whose only
resource is a ConfigMap `rancher-project-names` with `projects.json` and `clusters.json`. Fleet delivers it to the
downstream clusters (workspace `fleet-default`, all clusters, a `--cluster-selector`, or a list of Fleet `--targets`;
the chart passes the targets of its downstream HelmOp), and the report reads it
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

- **Input:** unit rates per platform (€ per vCPU-month / GiB-month), per environment the maximum Σ quota as a share
  of allocatable and the memory-limit factor, per cluster its environment and platform, and per project a monthly
  budget, cost center, owners and a CPU/memory quota per cluster -- typed in, converted from an amount with the
  cluster's rates, or taken from current requests + 25 %.
- **Rancher inventory (read-only):** `clusters.management.cattle.io` (allocatable, requested, nodes) and
  `projects.management.cattle.io` (current quota). Current requests per project come from this collector's own
  report, so only for the cluster it scans.
- **Checks:** planned cost vs. budget per project; planned quota vs. each cluster's **limit for projects** =
  min(allocatable − the environment's N largest nodes − platform reserve,
  max % × (allocatable − platform reserve)), per CPU and memory; projects missing in Rancher; clusters without
  environment/platform/platform reserve. Allocatable and the node sizes come from the schedulable nodes in
  `nodes.management.cattle.io` (tainted control-plane / etcd and cordoned nodes are listed as excluded; Rancher's
  cluster total is used only when it reports no nodes). The platform reserve is measured (`system` requests on
  schedulable nodes) for the cluster the collector scans and entered for the others.
- **Storage:** with `--plan-configmaps NAMESPACE/PREFIX` (the chart sets it), a ConfigMap `PREFIX-planner` in the
  cluster the server runs in: current plan and the last 30 versions gzip-compressed in `binaryData`, trimmed to
  stay below the 1 MiB ConfigMap limit. It needs `get`/`update` on that ConfigMap only (`create` if it doesn't
  exist). Without the flag: `<data-dir>/planner.json` and the last 30 versions in `planner-history/`. Each save
  carries a version number: a save based on an older version is refused with 409, so two people can't overwrite
  each other; with the ConfigMap, the API server's resourceVersion check also catches two saves at the same
  moment. A `planner.json` left in `--data-dir` by an older version is read until the first save moves it over.
- **What-if calculator** (on the page, not saved): a new application's CPU / memory on a cluster, checked on top
  of the planned quota or the projects' requests now against the limit for projects; if it doesn't fit, the
  fewest nodes of a given size to add (each adds allocatable, may become the largest node for N+1, and adds its
  platform DaemonSets to the reserve), and their Rancher licence cost (`settings.license`: per node or per vCPU
  and year, saved with the plan).
- **History:** `GET /api/planner/history` lists the stored versions, newest first, with their time, totals
  (projects with quota, Σ CPU / memory, Σ budgets) and what changed against the version before (projects,
  cluster settings, rates and rules); `GET /api/planner/history/<n>` returns one (404 once trimmed). The page
  previews a version and restores it with a normal `PUT` plus `"restored_from": <n>`: the old content becomes a
  new version, the version check applies as for any save, and the history shows the restore. No author is
  stored: the planner doesn't know the Rancher user behind a request.
- **Export:** `/api/planner/export.yaml`, one document per project in the allocation-file format of docs/04, with
  `limits.memory` = requests × the environment's factor.

Apart from its own plan ConfigMap it never writes to a cluster; applying stays with the Git / Terraform flow.

`server.py --capacity-planner` adds the same planner without money at `/capacity` (`GET|PUT /api/capacity`,
`/api/capacity/export.yaml`, `/api/capacity/history[/<n>]`): no platforms, rates, budgets or costs; each project has an optional CPU / memory
envelope checked against its quota summed over all clusters. Its plan is the ConfigMap `PREFIX-capacity` (or `<data-dir>/capacity-plan.json`)
(history in `capacity-history/`); both planners share `planner.py` (`mode="budget"` / `"capacity"`).

## Tests

```bash
python3 -m unittest -v test_discover test_server test_planner
```

[`testdata/`](testdata/README.md) has a scenario file for a test cluster: one namespace per situation the dashboard
and planners distinguish (efficiency, resource standards and QoS, sidecars and pod overhead, HPA and CronJob
peaks, pending / completed / failed pods, namespace quotas used up or refusing pods, a Rancher Project quota,
platform and unassigned namespaces, N+1 breach), applied with `testdata/apply-scenarios.sh` to a Rancher-managed
cluster (Projects looked up or created) or a cluster outside Rancher (namespace labels).

The script was verified end-to-end on a k3s cluster with fake Rancher Project objects, a kube-prometheus-stack
installed under Rancher Monitoring's service name, and fixture workloads (sidecars, init containers,
BestEffort, pending, HPA, existing quota/LimitRange).
It was also run against a real Rancher v2.15.2: the dashboard on the Rancher local cluster (k3s, with
`rancher.isLocalCluster`) and on an imported k3s cluster, with stream Projects on both.
