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

## 1. Build and push the image

The image is `python:3.13-slim` with `kubectl` (checksum-verified) and the discovery code.

```bash
cd discovery
docker build -t registry.example.com/platform/cluster-resource-report:0.1.0 .
docker push registry.example.com/platform/cluster-resource-report:0.1.0
# other architecture: docker buildx build --platform linux/arm64 ...   (KUBECTL_VERSION is a build arg)
```

## 2. Install

```bash
helm upgrade --install resource-report charts/cluster-resource-report \
  -n resource-report --create-namespace \
  --set image.repository=registry.example.com/platform/cluster-resource-report \
  --set image.tag=0.1.0
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

## 3. Open the dashboard

The app has **no login of its own**. Recommended ways to open it, in order:

1. **Through Rancher (uses your Rancher login and RBAC):**
   `https://<rancher-host>/k8s/clusters/<cluster-id>/api/v1/namespaces/resource-report/services/http:resource-report-cluster-resource-report:80/proxy/`
2. **Port-forward:** `kubectl -n resource-report port-forward svc/resource-report-cluster-resource-report 8080:80`
3. **Ingress** (`ingress.enabled=true`): only with authentication in front of it, for example oauth2-proxy.

Anyone who can open the dashboard sees names and sizes of all namespaces and projects in the cluster.

## Values

| Value | Default | Description |
|---|---|---|
| `image.repository` / `image.tag` | `ghcr.io/cdalar/cluster-resource-report` / appVersion | Image built from `discovery/Dockerfile` |
| `imagePullSecrets` | `[]` | For a private registry |
| `clusterName` | `""` | Display name; default is the Rancher cluster name (with `rancher.localKubeconfigSecret`) or `in-cluster` |
| `collection.interval` | `30m` | Time between collections; the dashboard also has a "Collect now" button |
| `collection.window` / `collection.step` | `7d` / `5m` | Prometheus usage window and resolution; use `step: 15m` on large clusters |
| `collection.projectLabel` | `""` | Namespace label to group by when there are no Rancher Projects (e.g. AKS outside Rancher) |
| `collection.systemNamespaceRegex` | built-in | Namespaces treated as platform/system |
| `prometheus.enabled` | `true` | `false` = no usage history (metrics-server snapshot only) |
| `prometheus.service` | Rancher Monitoring | `<ns>/<scheme>:<svc>:<port>`, queried via the API server service proxy; the chart grants `services/proxy` on exactly this service |
| `prometheus.url` / `prometheus.tokenSecret` | `""` | Direct Prometheus URL (and optional bearer token secret) instead of the proxy |
| `rancher.localKubeconfigSecret.name` / `.key` | `""` / `kubeconfig` | Secret with a kubeconfig for the Rancher local cluster (project/cluster names) |
| `rancher.localContext` | `""` | Context in that kubeconfig |
| `persistence.enabled` | `false` | Keep the last report on a PVC so a restarted pod shows data immediately |
| `ingress.*` | disabled | Standard ingress settings |
| `resources` | 50m / 128Mi, limit 512Mi | Raise the memory limit for clusters with many thousands of pods |
| `podSecurityContext` / `securityContext` | non-root 65534, read-only root FS, no capabilities | |

## Permissions

These are created by the chart; all of them are read-only.

- **ClusterRole:** `get`/`list` on nodes, namespaces, pods, resourcequotas, limitranges, HPAs and `metrics.k8s.io` pods.
- **Role in the Prometheus namespace:** `get` on `services/proxy` for the configured Prometheus service only.
  It is only created when the service proxy is used.

## Endpoints

| Path | |
|---|---|
| `/` | Dashboard |
| `/api/report` | Latest report plus collector status (JSON) |
| `POST /api/refresh` | Trigger a collection |
| `/download/{projects,namespaces,clusters}.csv` | CSV export |
| `/healthz` | Liveness and readiness |

All links in the page are relative, so it also works behind path-prefix proxies such as Rancher's.
