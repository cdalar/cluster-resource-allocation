# Test scenarios

`scenarios.yaml` puts one namespace per situation the dashboard and the planners distinguish onto a Rancher-managed
test cluster. Use a throwaway cluster with at least two schedulable nodes of about 4 CPU / 16 GiB, never production.

| Namespace | Project | Scenario | What the dashboard shows |
|---|---|---|---|
| `payments-api`, `payments-batch`, `crm-web`, `crm-search` | payments, crm | Requests that are never used (pause containers) | 0 % efficiency, "low" |
| `payments-worker` | payments | Uses its requests (CPU burn capped by a CPU limit, 300 Mi held) | ~95 % CPU, ~70 % memory efficiency; CPU limit counted |
| `crm-reports` | crm | Uses more than it requests (0.5 CPU on a 0.1 request) | ~500 % efficiency |
| `crm-legacy` | crm | A BestEffort pod and a pod with a CPU request only | BestEffort pod (S5), no CPU request, no memory request/limit (S2) |
| `payments-sidecar` | payments | Init container (500m) + native sidecar (100m) + app (200m) | Effective pod requests 500m |
| `payments-hpa` | payments | HPA on a CPU-busy Deployment, 1 → 3 replicas | HPA counted, efficiency above 100 % |
| `crm-quota` | crm | Existing ResourceQuota and LimitRange; pods without values get its defaults | Quota and LimitRange counts |
| `payments-bigjob` | payments | Asks for 6 CPU, more than any node has | Pending requests |
| `payments-batch` (Job) | payments | A finished Job | Completed pods not counted |
| `payments-agent` | payments | DaemonSet, one pod per node | Requests on every node |
| `legacy-app` | — | Namespace in no Rancher Project | Unassigned namespaces |
| `sandbox` | Default | Namespace in Rancher's Default project | Project "Default" |

Together the project requests exceed what survives a node failure on a 2 × 4 CPU cluster, so the dashboard's
*Room for projects (N+1)* tile turns red. The busy workloads use about 1.4 CPU continuously.

## Apply

The Projects `payments` and `crm` must exist on the cluster (create them in Rancher). Then, with the current kubectl
context on that cluster:

```bash
CLUSTER_ID=c-m-xxxxxxxx PAYMENTS_PROJECT=p-aaaaa CRM_PROJECT=p-bbbbb DEFAULT_PROJECT=p-ccccc \
  ./apply-scenarios.sh apply        # or: delete, print
```

The IDs are Rancher's cluster ID and the Project IDs (on the local cluster:
`kubectl -n <cluster-id> get projects.management.cattle.io`).

## Usage history (optional)

Without Prometheus the dashboard compares requests with a metrics-server snapshot. For P95 usage and request peaks,
install a Prometheus under Rancher Monitoring's service name and enable it in the chart:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install rancher-monitoring prometheus-community/kube-prometheus-stack \
  -n cattle-monitoring-system --create-namespace \
  --set fullnameOverride=rancher-monitoring --set grafana.enabled=false --set alertmanager.enabled=false
helm upgrade resource-report charts/cluster-resource-report -n resource-report --reuse-values \
  --set prometheus.enabled=true --set collection.window=6h
```

On a real Rancher cluster, install Rancher Monitoring from the Rancher UI instead.
