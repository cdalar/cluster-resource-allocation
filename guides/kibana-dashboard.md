# Kibana dashboards: cluster resources (as-is) and cluster resource report

A Kibana dashboard with the current situation of every cluster, built from the OpenTelemetry metrics the
clusters already send to the central Elastic. It shows the same baseline as [`discover.py`](../discovery/README.md)
and the in-cluster dashboard, without installing anything in the clusters: allocatable, requested, limited and
used CPU and memory per cluster, Rancher Project, namespace and node, plus the resource-standards gaps.

| File | Content |
|---|---|
| [`kibana/cluster-resources-as-is.ndjson`](../kibana/cluster-resources-as-is.ndjson) | Saved objects to import: two dashboards, *Cluster resources (as-is)* and *Cluster resource report* ([below](#cluster-resource-report)), and a data view (`metrics-*.otel-*`) for the filter controls |
| [`kibana/cluster-resources-as-is-no-project.ndjson`](../kibana/cluster-resources-as-is-no-project.ndjson) | The same without the Rancher Project, for collectors that do not set `rancher.project.id` (see [Import](#import)) |
| [`kibana/build_dashboard.py`](../kibana/build_dashboard.py) | Generates the `.ndjson`; all queries and field names live here. `--check` runs the queries against Elasticsearch |
| [`kibana/otel-collector-fragment.yaml`](../kibana/otel-collector-fragment.yaml) | Collector settings the dashboard needs (receivers, `k8sattributes`, cluster name) |

Tested with Elasticsearch and Kibana 9.1.5 and `opentelemetry-collector-contrib` 0.135.0 (Elasticsearch exporter,
mapping mode `otel`) on a throwaway k3s cluster imported into Rancher.

## What it shows

Every panel is an ES|QL panel over the dashboard's time range (default: last 7 days).

| Panel | Content |
|---|---|
| Tiles | CPU and memory requested as % of allocatable; used as % of requested; pods pending now (last 10 min); containers without a CPU or memory request |
| CPU / Memory over time | Allocatable, requested, limits and used per time bucket (sum over the selected clusters) |
| Clusters | Nodes, allocatable, requested, req %, used, and *free N+1* (allocatable minus the largest node minus requests) |
| Rancher Projects | Per cluster and project: namespaces, requests (avg and peak), limits, usage (avg and P95), efficiency |
| Top 20 namespaces | Requested vs P95 used, CPU and memory |
| Namespaces | Same columns as Projects, per namespace |
| Resource standards | Per namespace: pods, containers, no CPU request, no memory request, no memory limit, CPU limit set, BestEffort pods ([standards](../docs/02-resource-standards.md)) |
| Pending pods | Pods in phase Pending in the last 10 minutes, with their requests |
| Nodes | Allocatable, requested and used per node |

Controls at the top filter all panels by cluster, Rancher Project ID and namespace. The filter bar (KQL) works too,
e.g. `resource.attributes.k8s.cluster.name : "onprem-prod-01"`.

Definitions, as in the discovery tools:

- **Requested / limits**: sum of container requests and limits (`k8s.container.cpu_request` etc.), per time bucket;
  *avg* is the average over the buckets, *peak* the highest bucket.
- **Used**: CPU `k8s.pod.cpu.usage` (cores), memory `k8s.pod.memory.working_set`; *P95* is the 95th percentile of the
  bucket sums.
- **Efficiency** = P95 used / average requested.
- **(no project)**: the namespace has no `field.cattle.io/projectId` label. Rancher's *System* project has an ID
  too (e.g. `p-bk8gt`); look it up in Rancher.

## Cluster resource report

The second dashboard in the same file, *Cluster resource report*, follows the in-cluster dashboard of the
[chart](../charts/cluster-resource-report/README.md) on the same OTel metrics, for all clusters at once:

| Panel | Content | As in the in-cluster report |
|---|---|---|
| Tiles | Nodes (and clusters), allocatable, CPU and memory requested as % of allocatable with a bar (green to 80 %, amber to 100 %, red above), room for projects (N+1) and what is left after project requests, used (P95), containers without CPU request / memory limit (now), pending requests (now), unassigned namespaces (now) | Same tiles, plus nodes |
| Cluster capacity by requests | Per cluster and resource (with its node count), % of allocatable: Tenant, System (platform), Unassigned, free up to N+1, N+1 reserve (largest node) | The capacity bar with the N+1 line |
| N+1 table | Nodes; allocatable − largest node − platform = for projects; projects request; left; fits | The N+1 note |
| CPU / Memory by project | Top 15 projects by requests (without System), requested vs used (P95) | Same charts |
| Projects, Namespaces | Requests, P95 use, efficiency, peak, memory limits, pods, pending, no CPU request, no memory limit, BestEffort; namespaces also quota and HPA counts | Same columns (LimitRange isn't in the metrics) |

Definitions, where they differ from the as-is dashboard:

- **Requested** counts running pods only; pending pods are shown apart (*Pending requests*), finished pods not at
  all, as the scheduler and the in-cluster report count them.
- **Now** (tiles marked *now*, pods, pending and standards columns) = the last 10 minutes of the time range.
- **Category**, as in [`discover.py`](../discovery/README.md): *System* = platform namespaces by name (same regex
  as `collection.systemNamespaceRegex`) or every namespace of a Project named *System* or *…platform…*;
  *Unassigned* = no Rancher Project; otherwise *Tenant*. `test_build_dashboard` keeps the regexes in sync.
- **Project names**: the metrics carry only the Project ID. Export the Projects on the Rancher local cluster and
  regenerate, then import that file instead of the committed one:

  ```bash
  kubectl get projects.management.cattle.io -A -o json > projects.json   # on the Rancher local cluster
  ./kibana/build_dashboard.py --rancher-projects projects.json --out cluster-resources-with-names.ndjson
  ```

  The names are written into the queries, so regenerate after adding or renaming Projects. Without the file the
  report shows IDs, and System projects are only recognised by their namespace names.
- With `--no-project` the report has no Projects table and no *Unassigned* tile; namespaces are *System* or
  *Workload*.

**Link in Rancher**: a NavLink on the cluster that runs Kibana puts the dashboard in Rancher's cluster menu
(Kibana's `server.basePath` must be the Rancher service proxy path):

```yaml
apiVersion: ui.cattle.io/v1
kind: NavLink
metadata:
  name: kibana-cluster-resource-report
spec:
  label: Resource report (Kibana)
  group: Kibana
  toService: {namespace: kibana, name: kibana, scheme: http, port: "5601",
              path: "app/dashboards#/view/cra-cluster-resource-report"}
```

## Prerequisites in the collector

The dashboard needs four things from the collectors in the clusters. Check them in Kibana **Dev Tools** first:

```
GET _data_stream/metrics-*.otel-*
GET metrics-*.otel-*/_field_caps?fields=metrics.k8s.container.cpu_request,metrics.k8s.node.allocatable_cpu,metrics.k8s.pod.cpu.usage,resource.attributes.k8s.cluster.name,resource.attributes.rancher.project.id
```

| Needed | Field | Collector setting ([fragment](../kibana/otel-collector-fragment.yaml)) | If missing |
|---|---|---|---|
| Requests, limits, pod phase | `metrics.k8s.container.*`, `metrics.k8s.pod.phase` | `k8s_cluster` receiver (defaults) | Most panels are empty |
| Node allocatable | `metrics.k8s.node.allocatable_cpu`, `..._memory` | `k8s_cluster`: `allocatable_types_to_report: [cpu, memory]` (off by default) | No allocatable, req %, N+1 |
| Usage | `metrics.k8s.pod.cpu.usage`, `metrics.k8s.pod.memory.working_set` | `kubeletstats` receiver | No usage or efficiency |
| Cluster name | `resource.attributes.k8s.cluster.name` | `resource` processor, set per cluster | All clusters show as `(unknown)` and are added together |
| Rancher Project | `resource.attributes.rancher.project.id` | `k8sattributes`: namespace label `field.cattle.io/projectId` as `rancher.project.id` | Panels fail with `Unknown column [resource.attributes.rancher.project.id]`; import the `-no-project` file instead |

The data must be written in OTel-native mode (`mapping: {mode: otel}` in the Elasticsearch exporter): data streams `metrics-k8sclusterreceiver.otel-*` and
`metrics-kubeletstatsreceiver.otel-*`, fields under `metrics.*` and `resource.attributes.*`. With ECS mapping
(`kubernetes.*` fields) the queries need other field names; see [Adapting](#adapting).

## Import

Which file:

- `cluster-resources-as-is.ndjson` when the data has `resource.attributes.rancher.project.id` (the `_field_caps`
  check above lists it).
- `cluster-resources-as-is-no-project.ndjson` when it does not. `rancher.project.id` is not a standard OTel
  attribute: the collector only sets it with the `k8sattributes` label rule in the
  [fragment](../kibana/otel-collector-fragment.yaml). ES|QL rejects a query that names a field no index has, so
  the full file fails with `Unknown column`. This variant has no *Rancher Projects* table and no project
  control, and the Project columns show `(no project)`. Cluster, namespace and node panels are the same. Once
  the collector sets the attribute, import the full file; it has the same IDs and replaces this one.

**Kibana UI**: *Stack Management → Saved objects → Import*, select the `.ndjson` file,
*Check for existing objects → Automatically overwrite*. Open *Dashboards → Cluster resources (as-is)* or
*Cluster resource report*.
Import into the Kibana space where the platform team works.

**API**:

```bash
curl -X POST "https://kibana.example/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" -H "Authorization: ApiKey $KIBANA_API_KEY" \
  --form file=@kibana/cluster-resources-as-is.ndjson
```

Object IDs are fixed (`cra-cluster-resources-as-is`, `cra-cluster-resource-report`, data view `cra-otel-metrics`),
so importing again updates the dashboards in place. Changes made in the Kibana UI are overwritten by the next import; make them in
`build_dashboard.py` instead.

## Checking and adapting

Run all panel queries against Elasticsearch before importing, with a read-only API key:

```bash
ES_API_KEY=... ./kibana/build_dashboard.py --check https://elastic.example:9200 --window 6h
# ES_USER / ES_PASSWORD instead of an API key; ES_INSECURE=1 for a self-signed certificate
```

It prints the first rows of every panel and exits non-zero on a query error or a column mismatch.

### Adapting

- **Other index pattern** (e.g. a data stream namespace per environment): `./kibana/build_dashboard.py --index 'metrics-*.otel-prod'`.
- **No Rancher Project attribute**: `--no-project` (with `--check` too); writes `cluster-resources-as-is-no-project.ndjson`.
- **Other field names** (other attribute names, ECS mapping): edit the `F` table at the top of
  `build_dashboard.py`, run `--check`, then regenerate the `.ndjson`.
- Regenerate with `./kibana/build_dashboard.py` and commit both files.

## Limits

| Limit | Effect | Compared with `discover.py` |
|---|---|---|
| Rancher Project **ID**, not the display name | The as-is tables show `p-xxxxx`; the report shows names only when generated with `--rancher-projects` (a snapshot) | discover.py reads names from Rancher. Possible later: a lookup index (`project_id → name`) filled from the Rancher local cluster, joined with ES\|QL `LOOKUP JOIN` |
| Init containers and pod overhead are not reported by `k8s_cluster` | Requests can be lower than the *effective* requests the scheduler uses | discover.py counts both |
| As-is: requests include Pending pods (and pods of finished Jobs while they exist) | *CPU requested %* can exceed 100 % when big pods are Pending | discover.py and the report dashboard count pending separately |
| Report: all nodes count as allocatable | Cordoned nodes are not excluded (the metrics don't say which nodes are unschedulable) | discover.py counts schedulable nodes only |
| Report: tested | All queries run against Elasticsearch 9.1.5 with `--check`; panels rendered with Kibana 9.1.5 reporting (PNG) on the test cluster | — |
| About 60 buckets per time range; a container's value per bucket is its maximum, usage its average | Peaks and P95 are smoothed over long ranges; pods replaced within a bucket count twice for that bucket | discover.py uses Prometheus at a fixed resolution |
| Query cost grows with containers × buckets | Wide ranges over many clusters are slow; filter by cluster first | — |
| No budgets or cost | As-is only; the [allocation planner](allocation-planner.md) covers plans and money | — |
| Filter controls | Import, migration and dashboard filters were tested. The controls themselves were not clicked through in a browser | — |
