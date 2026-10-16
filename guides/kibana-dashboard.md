# Kibana dashboards: cluster resource report

Kibana dashboards with the current situation of every cluster, built from the OpenTelemetry metrics the
clusters already send to the central Elastic. They show the same report as the in-cluster dashboard, without
installing anything in the clusters: capacity and N+1 per cluster, requests and usage per Rancher Project and
namespace, one Project on every cluster for its owner, the resource-standards gaps, the workloads a limit ≤ 2×
request rule would reject, and whether a new deployment fits a cluster.

| File | Content |
|---|---|
| [`kibana/cluster-resource-report.ndjson`](../kibana/cluster-resource-report.ndjson) | Saved objects to import: five linked dashboards, *Cluster resource report: capacity*, *Cluster resource report: projects*, *Workload limits* ([below](#cluster-resource-report)), *Project owner* ([below](#project-owner)) and *Will it fit?* ([below](#will-it-fit)), and a data view (`metrics-*.otel-*`) for the filter controls |
| [`kibana/cluster-resource-report-no-project.ndjson`](../kibana/cluster-resource-report-no-project.ndjson) | The same without the Rancher Project, for collectors that do not set `rancher.project.id` (see [Import](#import)) |
| [`kibana/cluster-resource-report-project-names.ndjson`](../kibana/cluster-resource-report-project-names.ndjson) | The same with Rancher Project **names** in the report, joined from the lookup index `rancher-projects` in Elasticsearch. Import it only after filling that index ([Project names from a lookup index](#project-names-from-a-lookup-index)) |
| [`kibana/cluster-resources-cra-elastic.ndjson`](../kibana/cluster-resources-cra-elastic.ndjson) | Export of the same objects from the Kibana of the `cra-elastic` test box (2026-10-07): the report as generated with `--rancher-projects`, so its queries carry the Project names of the test Rancher ([Project names](#cluster-resource-report)). An example of that variant; not generated or checked by the tests |
| [`kibana/build_dashboard.py`](../kibana/build_dashboard.py) | Generates the `.ndjson`; all queries and field names live here. `--check` runs the queries against Elasticsearch |
| [`kibana/otel-kube-stack-node-roles.yaml`](../kibana/otel-kube-stack-node-roles.yaml) | Values overlay for the same chart: RKE2 node role labels (`node-role.kubernetes.io/control-plane`, `etcd`) as `k8s.node.role.*` on the metrics, through a second `k8s_attributes` processor that sees all nodes (or in the existing one of a `deployment`-mode collector that runs `k8s_cluster`). Used by the dashboards' *NOT Control-plane nodes* filter ([below](#control-plane-nodes)); rendered with the chart, not run on a cluster |
| [`kibana/build_collector_values.py`](../kibana/build_collector_values.py) | Values overlay for the `opentelemetry-kube-stack` chart that puts the Project **name** on the metrics (`rancher.project.name`), so the Project filter control lists names ([Project names on the metrics](#project-names-on-the-metrics)) |
| [`kibana/add_project_names.py`](../kibana/add_project_names.py) | Writes the Rancher Project names into a dashboard `.ndjson` you already have (a file from here or a Kibana export), without regenerating it |
| [`kibana/publish_project_names.py`](../kibana/publish_project_names.py) | Writes the Rancher Project names to a lookup index in Elasticsearch, for a report generated with `--project-lookup-index` ([Project names](#project-names-from-a-lookup-index)) |
| [`kibana/otel-collector-fragment.yaml`](../kibana/otel-collector-fragment.yaml) | Collector settings the dashboard needs (receivers, `k8sattributes`, cluster name) |
| [`kibana/otel-kube-stack-values.yaml`](../kibana/otel-kube-stack-values.yaml) | Values overlay (`-f`) for the `opentelemetry-kube-stack` Helm chart (0.20.1) that adds `rancher.project.id`; the chart's presets already cover the other settings |

Tested with Elasticsearch and Kibana 9.1.5 and `opentelemetry-collector-contrib` 0.135.0 (Elasticsearch exporter,
mapping mode `otel`) on a throwaway k3s cluster imported into Rancher.

## Cluster resource report

The report follows the in-cluster dashboard of the [chart](../charts/cluster-resource-report/README.md) on the
OTel metrics, for all clusters at once. It is three dashboards, plus [*Project owner*](#project-owner) and [*Will it fit?*](#will-it-fit), and every About
box links the others: *capacity* (tiles, capacity chart, N+1 table), *projects* (by-project charts, Projects and
Namespaces tables) and *Workload limits* (the 2× table). Each has the same controls (cluster, Rancher Project, namespace) and the saved filter that
leaves out control-plane nodes ([below](#control-plane-nodes)); the selections do not carry over when following a
link. Every panel is an ES|QL panel over the dashboard's time range (default: last 1 hour; `--time-from now-1d` for
another). With Project names (any of the ways below), every Project column and chart shows the name.


| Panel | Content | As in the in-cluster report |
|---|---|---|
| Tiles | One value per tile, in two rows; each row of tiles is one query (a Lens metric grid), not one query per tile. Capacity and requests: clusters, nodes, allocatable vCPU and GiB, CPU and memory requested as % of allocatable with a bar (green to 80 %, amber to 100 %, red above), and in vCPU / GiB. Then: room for projects (N+1) and what is left after project requests, used (P95) vCPU and GiB, containers without CPU request and without memory limit (now), pending requests vCPU and GiB (now), unassigned namespaces (now). Every tile adds up all clusters in the selection | Same tiles, plus nodes |
| Cluster capacity by requests | Per cluster and resource (with its node count), % of allocatable: Tenant, System (platform), Unassigned, free up to N+1, N+1 reserve (largest node) | The capacity bar with the N+1 line |
| N+1 table | Nodes; allocatable − largest node − platform = for projects; projects request; left; fits | The N+1 note |
| CPU / Memory by project | Top 15 projects by requests (without System), requested vs used (P95) | Same charts |
| Projects, Namespaces | Requests, P95 use, efficiency, peak, memory limits, pods, pending, no CPU request, no memory limit, BestEffort; namespaces also quota and HPA counts | Same columns (LimitRange isn't in the metrics) |
| Workloads vs. limit ≤ 2× request | Every Deployment, StatefulSet, DaemonSet, CronJob, Job and bare Pod (now), problems first: what a LimitRange `maxLimitRequestRatio: 2` ([10](../docs/10-implementation-plan.md), steps 2.1 and 5.4) would reject at the next pod creation, per resource. A container breaks it with limit > 2× request, or with a request but no limit (unless the LimitRange sets a default limit); without a request Kubernetes sets the request to the limit, so a limit alone passes. Columns: status, worst ratio and problems for memory and CPU, the containers affected, pods, containers, requests. Kind and name come from `k8s.deployment.name`, `k8s.statefulset.name`, `k8s.daemonset.name`, `k8s.job.name` (k8sattributes; a CronJob is its Job's name without the timestamp). The CPU column flags every container without a CPU limit: only relevant if the ratio is set for CPU too ([ADR-0003](../docs/adr/0003-cpu-limits.md) keeps CPU limits optional) | — (new) |

### Project owner

For the owner of one Rancher Project: pick it by **name** in the **Project** control at the top, and the dashboard
shows it on every cluster that has it. The control is an ES|QL control whose options come from a query over the
dashboard's time range: the Project names, worked out the same way as in the panels (from `--rancher-projects`, the
lookup index or `--project-name-field`; a Project without a known name, and every Project in the file without
names, is listed by its ID). Kibana's ES|QL controls take exactly one value, so the first option, *(all projects)*,
stands for all of them; the dashboard opens with it. The cluster and namespace controls narrow it down further, but
list every cluster and namespace (they don't follow the Project control). Not in the `--no-project` file (it needs
the Project attribute).

| Panel | Content |
|---|---|
| Tiles | Clusters and namespaces of the Project, requested and used (P95) vCPU and GiB over the range; P95 used as % of requests for CPU and memory; pods, pending pods, containers without CPU request or memory limit, BestEffort pods (now) |
| CPU / Memory by cluster | Per Project and cluster: requested vs used (P95) |
| CPU / Memory over time | Requests and usage per time bucket (memory also the limits): the trend, and whether usage nears the requests or the limit |
| Clusters | One row per Project and cluster: namespaces (count and names) and the columns of the *Projects* table, plus the Project ID |
| Namespaces | The *Namespaces* table of the projects dashboard for the Project: requests, P95 use, efficiency, limits, pods, standards, quota and HPA |

The panels filter on the picked name after the name is worked out, so every query still reads all Projects of the
time range (as the projects dashboard does) and then keeps one.

### Will it fit?

One question: does a new deployment fit a cluster? Choose it in the controls at the top: **replicas**, **CPU** and
**memory request per replica**, **replicas per node** (any, or one per node for anti-affinity), **node failures to
survive** (0, 1, 2) and a **ceiling** (% of allocatable the requests may reach). Pick a cluster, or compare all. To
offer other values, edit a control in Kibana (pencil icon → values); the queries take any number.

The dashboard opens with the last 24 hours (`--time-from` sets every dashboard): requests are those of the last
10 minutes, usage is each node's P95 over the range, so a longer range catches the daily peak. A cluster **FITS**
when all three hold:

| Check | Rule |
|---|---|
| Now | Per node, free = allocatable − requests of running pods; a node holds ⌊min(free CPU / CPU, free memory / memory)⌋ replicas (at most 1 with one per node). The nodes together hold all replicas: what the scheduler checks |
| After node failures | The same without the largest nodes (as many as *node failures*; the nodes with the most room for replicas and the largest allocatable are taken out, the worst case), **and** running + pending + new requests ≤ ceiling × allocatable of the nodes left: the N+1 rule of the capacity dashboard with the new deployment added |
| By usage | Both again with each node's P95 usage where it is above its requests (pods that use more than they request leave less room) |

Verdicts: *FITS*; *fits by requests, tight by usage* (schedulable and N+1-safe, but the nodes are busier than their
requests say); *fits now, not N+x / ceiling* (schedulable today, but not after a node failure or above the
ceiling); *DOES NOT FIT*.

| Panel | Content |
|---|---|
| Answer | Number of clusters per verdict, and the vCPU and GiB the deployment needs in total |
| Does it fit? | One row per cluster: verdict, nodes, replicas that fit now / after failures / by usage after failures, needed and left vCPU and GiB (after failures, and by usage), most free vCPU and GiB on one node (the largest replica that still fits), pending requests |
| Requests and usage with the new deployment | Per cluster and resource, % of allocatable. What is taken now in three parts: *requested and used*, *requested, unused* and *used above requests* (requests = the first two, used P95 = the first and the third; together, what the *by usage* check subtracts). Then pending, the new deployment, free, and the reserve for node failures and the ceiling |
| Nodes | Per node: replicas that fit (by requests and by usage), which resource limits it, allocatable, requested, used (P95) and free vCPU and GiB |

Not in the metrics, so not checked: taints and tolerations, node selectors and affinity to particular nodes,
ResourceQuotas of the target namespace, cordoned nodes, and DaemonSets that a new node would need. The node
failures are the largest nodes, not a particular zone or rack.

Definitions:

- **Requested / limits**: sum of container requests and limits (`k8s.container.cpu_request` etc.); **used**: CPU
  `k8s.pod.cpu.usage`, memory `k8s.pod.memory.working_set`, as P95 over the time range; **efficiency** = P95 used /
  average requested.
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

  With a dashboard file you already have (one of the committed files, or an export from Kibana with your own
  changes), `add_project_names.py` does the same without the generator:

  ```bash
  ./kibana/add_project_names.py kibana/cluster-resource-report.ndjson projects.json --out cluster-resources-with-names.ndjson
  ```

  The names are written into the queries, so regenerate (or run `add_project_names.py` again) after adding or renaming Projects. Without the file the
  report shows IDs, and System projects are only recognised by their namespace names. To keep the names in
  Elasticsearch instead, see [Project names from a lookup index](#project-names-from-a-lookup-index).
- With `--no-project` the report has no Projects table and no *Unassigned* tile; namespaces are *System* or
  *Workload*.

### Project names from a lookup index

The alternative to names written into the queries: a small index in Elasticsearch holds one document per Project
(`project` = ID, `project_name`, `cluster_id`, and the ID again as `resource.attributes.rancher.project.id`), and
the report joins it with ES|QL `LOOKUP JOIN`. Other ES|QL panels and Discover can use it too, joining on the metrics
field directly: `FROM metrics-*.otel-* | LOOKUP JOIN rancher-projects ON resource.attributes.rancher.project.id`. Renaming or adding a Project
then needs only a new run of the publisher, not a new dashboard file. Needs Elasticsearch 9.1 or later.

```bash
# 1. Fill the index (creates it with index.mode: lookup); Projects are read with kubectl from the Rancher local cluster
ES_API_KEY=... ./kibana/publish_project_names.py https://elastic.example:9200 --context <rancher-local-context>
#    or from a file, and through Kibana when Elasticsearch itself is not reachable (Dev Tools proxy):
ES_USER=... ES_PASSWORD=... ./kibana/publish_project_names.py https://kibana.example --kibana --rancher-projects projects.json
#    or with your own browser login (e.g. Kibana behind AD single sign-on): print Dev Tools text, paste it, run it
./kibana/publish_project_names.py --devtools --rancher-projects projects.json > rancher-projects.devtools
# 2. Check the queries, then import kibana/cluster-resource-report-project-names.ndjson (same object IDs as the other files)
ES_API_KEY=... ./kibana/build_dashboard.py --project-lookup-index rancher-projects --check https://elastic.example:9200
```

The committed file is generated with `./kibana/build_dashboard.py --project-lookup-index rancher-projects`. For
another index name, pass it to both scripts (`publish_project_names.py --index`) and add `--out`.

| Point | Detail |
|---|---|
| Order | Publish first: ES\|QL rejects a query that names a missing index, so every report panel fails until the index exists |
| Updates | Run `publish_project_names.py` again (by hand or from a scheduler); it overwrites the names and removes Projects that no longer exist. `--rancher-projects projects.json` reads a file instead of running kubectl, `--dry-run` prints the documents |
| Permissions | The publisher's API key: `create_index`, `index`, `delete`, `view_index_metadata` and `manage` (it adds new fields to an index from an older version) on the lookup index only; with `--kibana` the Kibana user also needs Dev Tools. Everyone who opens the dashboard needs `read` on it |
| Scope | Names appear in every Project column and chart. The Project filter control keeps the ID |
| Unknown Projects | A Project the index does not have shows its ID |
| Join key | The Project ID alone. Two clusters that happen to have the same ID with different names get one name (the publisher prints a warning) |
| Remote clusters | With cross-cluster search the join needs Elasticsearch 9.2 or later |
| Standalone | `publish_project_names.py` is one file with no imports from the repository; copy it alone |
| Other dashboards | Only ES\|QL can join; classic Lens and visualizations on a data view cannot. The join adds `project_name` and `cluster_id` columns |
| Tested | Unit tests on the generated queries and, with a fake Elasticsearch, on the publisher's requests; neither the join nor the publisher (or `--kibana`) has run against a real Elasticsearch or Kibana yet. Run the `--check` above before importing |

### Project names on the metrics

The filter controls list the values of a field in Elasticsearch, so a *Rancher Project* control by name needs
the name as a field. The collector adds it: a `transform` processor with one rule per Project (ID → name),
generated from the Rancher Project list, sets `rancher.project.name` next to `rancher.project.id`.

```bash
kubectl get projects.management.cattle.io -A -o json > projects.json      # on the Rancher local cluster
./kibana/build_collector_values.py projects.json --out otel-kube-stack-project-names.yaml
helm upgrade <release> <chart> -n <namespace> -f <your values>.yaml -f kibana/otel-kube-stack-values.yaml -f otel-kube-stack-project-names.yaml
./kibana/build_dashboard.py --project-name-field --out cluster-resources-name-field.ndjson   # then import this file
```

| Point | Detail |
|---|---|
| Pipeline | `transform/rancher_project_names` must be in the metrics pipeline after `k8s_attributes`. Helm replaces lists, so add it to the pipeline in your own values, or pass the complete list: `--pipeline k8s_attributes,transform/rancher_project_names,resource_detection/env,resource/hostname,batch` (the chart's default order) |
| Collector | `--collector <name>` when the metrics collector is not the chart's `daemon` |
| Dashboard | The control is *Rancher Project* on `resource.attributes.rancher.project.name`; the report shows names (and the ID in its own column). Import it only once the field exists: ES\|QL rejects a field no index has |
| Old data | Only metrics collected after the rollout carry the name. Over a time range that spans the rollout a Project shows twice (ID before, name after), and selecting a name leaves out the time before |
| Updates | After adding or renaming Projects: regenerate the overlay and redeploy the collectors. A Project missing from the list keeps its ID and is not in the control |
| Tested | 2026-10-13 with chart 0.20.1 (`otel/opentelemetry-collector-contrib` 0.154.0) on a single-node k3s cluster into Elasticsearch and Kibana 9.1.5: field present on cluster and kubelet metrics, all 31 panel queries pass `--check`, the control's value request returns the names, a name filter narrows the report tables (with the as-is dashboard of that time, since removed). The control was not clicked in a browser; a multi-node cluster was not tested |

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

### Control-plane nodes

Control-plane nodes that take no workloads (RKE2 server nodes tainted `NoSchedule`) are not capacity for
projects. Both dashboards open with a saved filter, shown as *NOT Control-plane nodes*
(`NOT resource.attributes.k8s.node.role.control_plane: true`), that drops the nodes' allocatable and the requests
and usage of the pods on them: allocatable, node count, largest node (N+1), requests and usage cover the workers
only. To look at them anyway, disable the filter pill (until the dashboard is reloaded). To generate without it:
`--include-control-plane --out <file>`.

| Point | Detail |
|---|---|
| Field | The collector copies the RKE2 node label `node-role.kubernetes.io/control-plane` ([overlay](../kibana/otel-kube-stack-node-roles.yaml)). Add the rule to the `k8s_attributes` of the collector that runs `k8s_cluster` (allocatable, requests). For usage, add it to the `daemon` collector's too (kubelet metrics); without it the usage of pods on control-plane nodes still counts |
| Without the field | Nothing is excluded and nothing fails: the filter is a dashboard filter, no query names the field. Kibana may mark the pill as referring to an unknown field |
| Labels, not taints | Every node with the label is left out, tainted or not. RKE2 does not taint server nodes by default: check `kubectl get nodes -o custom-columns='NAME:.metadata.name,CP:.metadata.labels.node-role\\.kubernetes\\.io/control-plane,TAINTS:.spec.taints[*].key'` |
| Cordoned workers | Still counted: cordoning sets no label |
| Old data | Metrics from before the rollout have no field and count as workers; choose a time range after it |
| `--check` | Applies the same filter to the queries (none with `--include-control-plane`) |
| Tested | Unit tests on the generated objects only; not yet imported into Kibana |

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

Two metrics exist only while such objects exist, so on an Elastic where no cluster has any yet the report fails
with `Unknown column [metrics.k8s.resource_quota.hard_limit]` (or `metrics.k8s.hpa.max_replicas`):

| Metric | Sent when | If the field is missing |
|---|---|---|
| `metrics.k8s.resource_quota.hard_limit` | Some namespace has a ResourceQuota | Generate with `--no-quota`; the *Quota* column shows 0 |
| `metrics.k8s.hpa.max_replicas` | Some HorizontalPodAutoscaler exists | Generate with `--no-hpa`; the *HPA* column shows 0 |

```bash
./kibana/build_dashboard.py --no-quota --no-hpa --out cluster-resources-no-quota-hpa.ndjson   # with any other option
```

Regenerate without the option once the first quota (or HPA) exists.

The data must be written in OTel-native mode (`mapping: {mode: otel}` in the Elasticsearch exporter): data streams `metrics-k8sclusterreceiver.otel-*` and
`metrics-kubeletstatsreceiver.otel-*`, fields under `metrics.*` and `resource.attributes.*`. With ECS mapping
(`kubernetes.*` fields) the queries need other field names; see [Adapting](#adapting).

## Import

Which file:

- `cluster-resource-report.ndjson` when the data has `resource.attributes.rancher.project.id` (the `_field_caps`
  check above lists it).
- `cluster-resource-report-no-project.ndjson` when it does not. `rancher.project.id` is not a standard OTel
  attribute: the collector only sets it with the `k8sattributes` label rule in the
  [fragment](../kibana/otel-collector-fragment.yaml). ES|QL rejects a query that names a field no index has, so
  the full file fails with `Unknown column`. This variant has no *Rancher Projects* table and no project
  control, and the Project columns show `(no project)`. Cluster, namespace and node panels are the same. Once
  the collector sets the attribute, import the full file; it has the same IDs and replaces this one.

**Kibana UI**: *Stack Management → Saved objects → Import*, select the `.ndjson` file,
*Check for existing objects → Automatically overwrite*. Open *Dashboards → Cluster resource report: capacity*; the
About box links the others. The *Cluster resources (as-is)* dashboard of earlier versions is no longer generated;
delete it in Kibana (*Dashboards*, or saved object `cra-cluster-resources-as-is`) if it is still there.

**Upload too large** (`Request Entity Too Large`, e.g. ingress-nginx's 1 MiB default): write one file per dashboard
and import them one by one; each holds the data view, so the order does not matter:

```bash
./kibana/build_dashboard.py <options> --split --out cluster-resources.ndjson
# cluster-resources-capacity.ndjson, -projects, -owner, -limits, -fit; the sizes are printed
```

`--rancher-projects` adds about 2 KiB per Project to the capacity file (about 270 KiB without names); with several
hundred Projects use `--project-name-field` instead.
Import into the Kibana space where the platform team works.

**API**:

```bash
curl -X POST "https://kibana.example/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" -H "Authorization: ApiKey $KIBANA_API_KEY" \
  --form file=@kibana/cluster-resource-report.ndjson
```

Object IDs are fixed (`cra-cluster-resource-report`, `cra-cluster-resource-report`, data view `cra-otel-metrics`),
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
- **No Rancher Project attribute**: `--no-project` (with `--check` too); writes `cluster-resource-report-no-project.ndjson`.
- **Other field names** (other attribute names, ECS mapping): edit the `F` table at the top of
  `build_dashboard.py`, run `--check`, then regenerate the `.ndjson`.
- Regenerate with `./kibana/build_dashboard.py` and commit both files.

## Limits

| Limit | Effect | Compared with `discover.py` |
|---|---|---|
| Rancher Project **ID**, not the display name | The report shows `p-xxxxx` unless generated with names (`--rancher-projects` is a snapshot) | discover.py reads names from Rancher. Or names from a lookup index in Elasticsearch ([above](#project-names-from-a-lookup-index)) |
| Init containers and pod overhead are not reported by `k8s_cluster` | Requests can be lower than the *effective* requests the scheduler uses | discover.py counts both |
| Report: all nodes count as allocatable | Cordoned nodes are not excluded (the metrics don't say which nodes are unschedulable); control-plane nodes are left out by a saved filter ([above](#control-plane-nodes)) | discover.py counts schedulable nodes only |
| Report: tested | All queries run against Elasticsearch 9.1.5 with `--check`; panels rendered with Kibana 9.1.5 reporting (PNG) on the test cluster | — |
| About 60 buckets per time range; a container's value per bucket is its maximum, usage its average | Peaks and P95 are smoothed over long ranges; pods replaced within a bucket count twice for that bucket | discover.py uses Prometheus at a fixed resolution |
| Query cost grows with containers × buckets | Wide ranges over many clusters are slow; filter by cluster first. Each panel runs its own query (Kibana does not share results between ES\|QL panels), so the tiles are grouped: one query per row of tiles | — |
| Logic in the queries, not in runtime fields | ES\|QL panels cannot use data view runtime fields (Kibana sends them as `runtime_mappings`, which ES\|QL rejects). Runtime fields in the index mapping would work, but they need a change to the OTel index templates in Elasticsearch, cover only per-document logic (the category is computed after the aggregation, on a few rows) and run a script per document: slower, not faster | — |
| *Will it fit?*: per-node arithmetic, not the scheduler | Replicas per node are counted by free requests only (no taints, affinity, topology spread or quotas); after node failures, the pods of the failed nodes are counted against the total (N+1 rule), not placed node by node | — |
| No budgets or cost | The current situation only; the [allocation planner](allocation-planner.md) covers plans and money | — |
| Filter controls | Import, migration and dashboard filters were tested, and the *Will it fit?* queries with the controls' default values (`--check` passes them) and with other values. The controls themselves were not clicked through in a browser | — |
