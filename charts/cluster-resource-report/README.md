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

Step-by-step installation across the Rancher local and downstream clusters: [guides/installation.md](../../guides/installation.md).
This README is the reference for the image, values, permissions and endpoints.

## 1. Chart and image

Each release `vX.Y.Z` publishes both, public, on GHCR and (copied from there) on Docker Hub:

| | GHCR (source) | Docker Hub (copy) | Built by |
|---|---|---|---|
| Chart | `oci://ghcr.io/cdalar/charts/cluster-resource-report`, version `X.Y.Z` | `oci://registry-1.docker.io/cdalar/cluster-resource-report-chart`, version `X.Y.Z` | `.github/workflows/chart.yml` |
| Image | `ghcr.io/cdalar/cluster-resource-report:X.Y.Z` (the chart's default) | `docker.io/cdalar/cluster-resource-report:X.Y.Z` (also `X.Y`, `latest`) | `.github/workflows/image.yml` |

On Docker Hub the chart has its own repository (`-chart`), since image and chart share the tag `X.Y.Z`.
`.github/workflows/dockerhub.yml` does the copy after each push to GHCR (`crane copy`, nothing is rebuilt). It
needs the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with Read &
Write) and optionally the variable `DOCKERHUB_NAMESPACE` (default `cdalar`); without the secrets it only warns.
To copy an existing release, run it manually (Actions → dockerhub → Run workflow, input `tag`).
To use the image from Docker Hub, set `image.repository=docker.io/cdalar/cluster-resource-report`.

`chart.yml` runs on version tags: it checks that the tag matches the chart's `version` and `appVersion`, lints,
runs `helm package` and `helm push` to `oci://ghcr.io/cdalar/charts`, and pulls the chart back as a check. To
publish the chart of an existing tag again, run it manually (Actions → chart → Run workflow, input `tag`).
Rancher can use it as a repository: **Apps → Repositories → Create**, target *OCI repository*, URL
`oci://ghcr.io/cdalar/charts/cluster-resource-report` (the chart's own location: GHCR refuses to list the parent
`oci://ghcr.io/cdalar/charts` without a login, which Rancher reports as *403 Forbidden*), or
`oci://registry-1.docker.io/cdalar/cluster-resource-report-chart`. Both tested with Rancher 2.15.

### Image

The image is `python:3.13-slim` with `kubectl` (checksum-verified) and the discovery code, built for
`linux/amd64` and `linux/arm64`.

#### Built by CI

`.github/workflows/image.yml` runs the unit tests and `helm lint`, then builds the image and pushes it to
`ghcr.io/cdalar/cluster-resource-report`:

| Trigger | Image tags |
|---|---|
| Push to `main` that touches `discovery/`, `charts/` or the workflow | `main`, `sha-<short-sha>` |
| Git tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` |
| Manual run (Actions → image → Run workflow) | Same as for the branch or tag it runs on |

To release, set `version` and `appVersion` in `Chart.yaml` to the same number, push that commit, then tag it:
`git tag v0.4.1 && git push origin v0.4.1`. Both workflows fail if the tag and `Chart.yaml` differ. The chart's
default image tag is its `appVersion`.

The repository and the GHCR package are public, so clusters with internet access pull the image without a
pull secret.

Clusters without internet access (most on-prem clusters) pull from an internal registry instead. In that case,
mirror the image (e.g. `crane copy ghcr.io/cdalar/cluster-resource-report:0.4.1 registry.example.com/platform/cluster-resource-report:0.4.1`)
and set `image.repository`.

#### Build locally

```bash
cd discovery
docker build -t registry.example.com/platform/cluster-resource-report:0.4.1 .
docker push registry.example.com/platform/cluster-resource-report:0.4.1
# KUBECTL_VERSION is a build arg
```

## 2. Install

```bash
helm upgrade --install resource-report oci://ghcr.io/cdalar/charts/cluster-resource-report --version 0.4.1 \
  -n resource-report --create-namespace \
  --set image.repository=registry.example.com/platform/cluster-resource-report \
  --set image.tag=0.4.1          # or omit both to use ghcr.io/cdalar/cluster-resource-report:<appVersion>
```

From a checkout, use `charts/cluster-resource-report` instead of the `oci://` location. On a Rancher-managed
cluster you can also install it from the Rancher UI (Apps → Charts, after adding the OCI repository above), or
roll it out to all clusters with Fleet.

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

Anyone who can open the dashboard sees names and sizes of all namespaces and projects in the cluster.

## 4. Allocation planner (optional, Rancher local cluster)

User guide: [guides/allocation-planner.md](../../guides/allocation-planner.md).

**Without money:** `--set capacityPlanner.enabled=true` adds the capacity planner (`/capacity`, *Capacity planner*
in the Rancher menu): no rates, budgets or costs, each project gets a CPU and memory envelope instead. It can run
alone or next to the allocation planner, with its own plan (ConfigMap `<release>-cluster-resource-report-capacity`).

A planning page next to the dashboard for the platform team: a monthly budget per project and a CPU/memory quota
per cluster, entered either way round (quota directly, or an amount converted with the cluster's unit rates).
It checks the plan live against the budgets and each cluster's headroom rule (e.g. Σ quota ≤ 80 % of allocatable
in prod) and exports it as the allocation files of [docs/04](../../docs/04-technical-design.md#42-allocation-as-code).

**It applies nothing.** The plan is saved in a ConfigMap in the release namespace
(`<release>-cluster-resource-report-planner`, compressed, with the last 30 versions), so no volume or storage
class is needed. The chart creates it empty and keeps it on `helm uninstall`. Quotas still reach Rancher only
through the Git / Terraform flow. A plan file from an older version on the volume (`/data/planner.json`) is read
until the first save, which moves it to the ConfigMap.

```bash
helm upgrade --install ... --set rancher.isLocalCluster=true --set planner.enabled=true
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
| `collection.systemNamespaceRegex` | `kube-.*`, `cattle-.*`, … `.*storage.*`, `.*system.*`, `.*ingress.*` (see `values.yaml`) | Namespaces counted as System (platform), whatever their Rancher Project; must match the whole name. See the [installation guide](../../guides/installation.md#mark-more-namespaces-as-system) |
| `prometheus.enabled` | `true` | `false` = no usage history (metrics-server snapshot only) |
| `prometheus.service` | Rancher Monitoring | `<ns>/<scheme>:<svc>:<port>`, queried via the API server service proxy; the chart grants `services/proxy` on exactly this service |
| `prometheus.url` / `prometheus.tokenSecret` | `""` | Direct Prometheus URL (and optional bearer token secret) instead of the proxy |
| `rancher.isLocalCluster` | `false` | Installed on the Rancher local cluster: read project/cluster names from it (no secret) |
| `rancher.publishNames.enabled` | `false` | Local cluster only: CronJob that publishes the names to downstream clusters as a Fleet Bundle |
| `rancher.publishNames.schedule` / `.workspace` / `.targetNamespace` / `.clusterSelector` | `*/10 * * * *` / `fleet-default` / `resource-report` / `{}` | Publish schedule, Fleet workspace, ConfigMap namespace on downstream clusters, Fleet clusterSelector |
| `rancher.namesConfigMap.enabled` / `.namespace` | `false` / release namespace | Downstream: read names from the published ConfigMap |
| `capacityPlanner.enabled` | `false` | Capacity planner at `/capacity`: the allocation planner without money, a CPU / memory envelope per project; same requirements |
| `planner.enabled` | `false` | Allocation planner at `/planner` (needs `rancher.isLocalCluster` or `rancher.localKubeconfigSecret`); plan in a ConfigMap |
| `rancher.navLink.enabled` / `.label` / `.group` | `true` / `Resource report` / `""` | Menu entry in the Rancher UI that opens the dashboard through Rancher's proxy; only created where the NavLink CRD (`ui.cattle.io/v1`) exists |
| `rancher.localKubeconfigSecret.name` / `.key` | `""` / `kubeconfig` | Secret with a kubeconfig for the Rancher local cluster (project/cluster names) |
| `rancher.localContext` | `""` | Context in that kubeconfig |
| `persistence.enabled` | `false` | Keep the last report on a PVC so a restarted pod shows data immediately (needs a storage class; not needed by the planners) |
| `resources` | 50m / 128Mi, limit 512Mi | Raise the memory limit for clusters with many thousands of pods |
| `podSecurityContext` / `securityContext` | non-root 65534, read-only root FS, no capabilities | |

## Permissions

These are created by the chart. The dashboard's are all read-only; the planners can write only their own plan
ConfigMaps.

- **ClusterRole:** `get`/`list` on nodes, namespaces, pods, resourcequotas, limitranges, HPAs and `metrics.k8s.io` pods;
  with `rancher.isLocalCluster` also on `projects`, `clusters` and `nodes.management.cattle.io` (node sizes for the planner's N+1 check).
- **Role in the Prometheus namespace:** `get` on `services/proxy` for the configured Prometheus service only.
  It is only created when the service proxy is used.
- **Role (`rancher.namesConfigMap`):** `get` on the `rancher-project-names` ConfigMap only.
- **Planners (`planner` / `capacityPlanner`):** `get`/`update` on exactly their plan ConfigMaps
  (`<fullname>-planner`, `<fullname>-capacity`) in the release namespace. No `create`: the chart creates them.
- **Name publisher (`rancher.publishNames`, own service account):** read on `projects`/`clusters.management.cattle.io`,
  and `create` bundles plus `get`/`patch`/`update` on the `rancher-project-names` Bundle in the Fleet workspace.
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
| `/capacity`, `/api/capacity` (`GET`, `PUT`), `/api/capacity/export.yaml` | Capacity planner, with `capacityPlanner.enabled` |

All links in the page are relative, so it also works behind path-prefix proxies such as Rancher's.
