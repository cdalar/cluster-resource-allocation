# cluster-resource-report Helm chart

Runs the [discovery](../../discovery/README.md) collector **inside a cluster** on a schedule and serves a web
dashboard with requests, limits and usage per Rancher Project and namespace. Install it once per cluster.

The dashboard shows:

- **Cluster summary:** schedulable capacity, % requested, P95 usage, containers without requests or limits,
  pending requests and unassigned namespaces.
- **Capacity bar:** requests split into tenant, system, unassigned and free.
- **CPU and memory charts per project:** requested vs used.
- **Sortable project and namespace tables:** efficiency, peak requests and standards violations. Click a project
  to see its namespaces.
- **CSV downloads** of the same data. Light and dark mode are supported.

The page is self-contained, with no CDN or external assets, so it works in air-gapped clusters.

## 1. Image

The image is `python:3.13-slim` with `kubectl` (checksum-verified) and the discovery code, built for
`linux/amd64` and `linux/arm64`.

### Built by CI

`.github/workflows/image.yml` runs the unit tests and `helm lint`, then builds the image and pushes it to
`ghcr.io/cdalar/cluster-resource-report`:

| Trigger | Image tags |
|---|---|
| Push to `main` that touches `discovery/`, `charts/` or the workflow | `main`, `sha-<short-sha>` |
| Git tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` |
| Manual run (Actions → image → Run workflow) | Same as for the branch or tag it runs on |

To release, set `appVersion` (and `version`) in `Chart.yaml`, push that commit, then tag it:
`git tag v0.3.0 && git push origin v0.3.0`. The workflow fails if the tag and `appVersion` differ. The chart's
default image tag is its `appVersion`.

The repository is private, so the GHCR package is private too. Clusters pulling from GHCR need a pull secret, for
example with a token that has `read:packages`:

```bash
kubectl -n resource-report create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<github-user> --docker-password=<token>
helm upgrade --install ... --set 'imagePullSecrets[0].name=ghcr-pull'
```

Most on-prem clusters pull from an internal registry instead. In that case, mirror the image
(e.g. `crane copy ghcr.io/cdalar/cluster-resource-report:0.3.0 registry.example.com/platform/cluster-resource-report:0.3.0`)
and set `image.repository`.

### Build locally

```bash
cd discovery
docker build -t registry.example.com/platform/cluster-resource-report:0.3.0 .
docker push registry.example.com/platform/cluster-resource-report:0.3.0
# KUBECTL_VERSION is a build arg
```

## 2. Install

```bash
helm upgrade --install resource-report charts/cluster-resource-report \
  -n resource-report --create-namespace \
  --set image.repository=registry.example.com/platform/cluster-resource-report \
  --set image.tag=0.3.0          # or omit both to use ghcr.io/cdalar/cluster-resource-report:<appVersion>
```

On a Rancher-managed cluster you can also install it from the Rancher UI (Apps → Charts, from a Git or Helm repo),
or roll it out to all clusters with Fleet.

### Show Rancher Project names instead of IDs

Downstream clusters only know project IDs (`p-xxxxx`). To show names, give the collector read access to the
Rancher local cluster through a kubeconfig in a Secret:

```bash
kubectl -n resource-report create secret generic rancher-local --from-file=kubeconfig=local.yaml
helm upgrade --install ... --set rancher.localKubeconfigSecret.name=rancher-local
```

Use a token with read-only access to `projects.management.cattle.io` and `clusters.management.cattle.io`.
Without it, the dashboard still works and shows project IDs.

On the Rancher local cluster itself no secret is needed: `--set rancher.isLocalCluster=true` reads the names
from that cluster and adds read access to those two resources to the chart's ClusterRole.

**Without any token on downstream clusters (Fleet, no GitRepo):** on the local cluster, also set
`rancher.publishNames.enabled=true`. A CronJob (own service account, may only create/update the one Bundle)
publishes a Fleet Bundle containing the ConfigMap `rancher-project-names`, which Fleet copies to the downstream
clusters. There, install with `rancher.namesConfigMap.enabled=true`:

```bash
# Rancher local cluster
helm upgrade --install ... --set rancher.isLocalCluster=true --set rancher.publishNames.enabled=true
# each downstream cluster
helm upgrade --install ... --set rancher.namesConfigMap.enabled=true
```

Names appear after the next publish (every 10 minutes by default) and Fleet sync.

## 3. Open the dashboard

The app has **no login of its own**. Recommended ways to open it, in order:

1. **Through Rancher (uses your Rancher login and RBAC):** in the Rancher UI, open the cluster and click
   **Resource report** in its side menu (a Rancher `NavLink` the chart creates, `rancher.navLink`). It opens
   `https://<rancher-host>/k8s/clusters/<cluster-id>/api/v1/namespaces/resource-report/services/http:resource-report-cluster-resource-report:80/proxy/`
2. **Port-forward:** `kubectl -n resource-report port-forward svc/resource-report-cluster-resource-report 8080:80`
3. **Ingress** (`ingress.enabled=true`): only with authentication in front of it, for example oauth2-proxy.

Anyone who can open the dashboard sees names and sizes of all namespaces and projects in the cluster.

## 4. Allocation planner (optional, Rancher local cluster)

User guide: [guides/allocation-planner.md](../../guides/allocation-planner.md).

A planning page next to the dashboard for the platform team: a monthly budget per project and a CPU/memory quota
per cluster, entered either way round (quota directly, or an amount converted with the cluster's unit rates).
It checks the plan live against the budgets and each cluster's headroom rule (e.g. Σ quota ≤ 80 % of allocatable
in prod) and exports it as the allocation files of [docs/04](../../docs/04-technical-design.md#42-allocation-as-code).

**It applies nothing.** The plan is saved in the data volume (`planner.json`, plus the last 30 versions in
`planner-history/`); quotas still reach Rancher only through the Git / Terraform flow.

```bash
helm upgrade --install ... --set rancher.isLocalCluster=true --set persistence.enabled=true --set planner.enabled=true
```

It reads all clusters (capacity, requests) and Rancher Projects (current quota) from the local cluster, so one
installation covers every cluster Rancher manages. Open it from the **Allocation planner** entry in the local
cluster's Rancher menu, or `/planner` next to the dashboard. Current requests per project are only shown for
the cluster the planner runs on. Saving needs the page (it sends `X-Planner: 1`), so another website can't make
a logged-in browser save a plan through Rancher's proxy.

## Values

| Value | Default | Description |
|---|---|---|
| `image.repository` / `image.tag` | `ghcr.io/cdalar/cluster-resource-report` / appVersion | Image built from `discovery/Dockerfile` |
| `imagePullSecrets` | `[]` | For a private registry |
| `clusterName` | `""` | Display name; default is the Rancher cluster name (with `rancher.isLocalCluster` or `rancher.localKubeconfigSecret`) or `in-cluster` |
| `collection.interval` | `30m` | Time between collections; the dashboard also has a "Collect now" button |
| `collection.window` / `collection.step` | `7d` / `5m` | Prometheus usage window and resolution; use `step: 15m` on large clusters |
| `collection.projectLabel` | `""` | Namespace label to group by when there are no Rancher Projects (e.g. AKS outside Rancher) |
| `collection.systemNamespaceRegex` | built-in | Namespaces treated as platform/system |
| `prometheus.enabled` | `true` | `false` = no usage history (metrics-server snapshot only) |
| `prometheus.service` | Rancher Monitoring | `<ns>/<scheme>:<svc>:<port>`, queried via the API server service proxy; the chart grants `services/proxy` on exactly this service |
| `prometheus.url` / `prometheus.tokenSecret` | `""` | Direct Prometheus URL (and optional bearer token secret) instead of the proxy |
| `rancher.isLocalCluster` | `false` | Installed on the Rancher local cluster: read project/cluster names from it (no secret) |
| `rancher.publishNames.enabled` | `false` | Local cluster only: CronJob that publishes the names to downstream clusters as a Fleet Bundle |
| `rancher.publishNames.schedule` / `.workspace` / `.targetNamespace` / `.clusterSelector` | `*/10 * * * *` / `fleet-default` / `resource-report` / `{}` | Publish schedule, Fleet workspace, ConfigMap namespace on downstream clusters, Fleet clusterSelector |
| `rancher.namesConfigMap.enabled` / `.namespace` | `false` / release namespace | Downstream: read names from the published ConfigMap |
| `planner.enabled` | `false` | Allocation planner at `/planner` (needs `rancher.isLocalCluster` or `rancher.localKubeconfigSecret`, and `persistence.enabled`) |
| `rancher.navLink.enabled` / `.label` / `.group` | `true` / `Resource report` / `""` | Menu entry in the Rancher UI that opens the dashboard through Rancher's proxy; only created where the NavLink CRD (`ui.cattle.io/v1`) exists |
| `rancher.localKubeconfigSecret.name` / `.key` | `""` / `kubeconfig` | Secret with a kubeconfig for the Rancher local cluster (project/cluster names) |
| `rancher.localContext` | `""` | Context in that kubeconfig |
| `persistence.enabled` | `false` | Keep the last report on a PVC so a restarted pod shows data immediately |
| `ingress.*` | disabled | Standard ingress settings |
| `resources` | 50m / 128Mi, limit 512Mi | Raise the memory limit for clusters with many thousands of pods |
| `podSecurityContext` / `securityContext` | non-root 65534, read-only root FS, no capabilities | |

## Permissions

These are created by the chart. The dashboard's (and planner's) are all read-only.

- **ClusterRole:** `get`/`list` on nodes, namespaces, pods, resourcequotas, limitranges, HPAs and `metrics.k8s.io` pods;
  with `rancher.isLocalCluster` also on `projects`, `clusters` and `nodes.management.cattle.io` (node sizes for the planner's N+1 check).
- **Role in the Prometheus namespace:** `get` on `services/proxy` for the configured Prometheus service only.
  It is only created when the service proxy is used.
- **Role (`rancher.namesConfigMap`):** `get` on the `rancher-project-names` ConfigMap only.
- **Name publisher (`rancher.publishNames`, own service account):** read on `projects`/`clusters.management.cattle.io`,
  and `create` bundles plus `get`/`patch`/`update` on the `rancher-project-names` Bundle in the Fleet workspace --
  the only write permission in the chart.
- **NavLinks** (`ui.cattle.io`) are created by Helm at install time, not by the running app.

## Endpoints

| Path | |
|---|---|
| `/` | Dashboard |
| `/api/report` | Latest report plus collector status (JSON) |
| `POST /api/refresh` | Trigger a collection |
| `/download/{projects,namespaces,clusters}.csv` | CSV export |
| `/healthz` | Liveness and readiness |
| `/planner`, `/api/planner` (`GET`, `PUT`), `/api/planner/export.yaml` | Allocation planner, with `planner.enabled` |

All links in the page are relative, so it also works behind path-prefix proxies such as Rancher's.
