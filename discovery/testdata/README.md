# Test scenarios

`scenarios.yaml` and `apply-scenarios.sh` put one namespace per situation the dashboard and the planners
distinguish onto a test cluster: efficiency, resource standards, effective requests, peaks, pod phases, namespace
and Rancher Project quotas, and how namespaces are classified. Use a throwaway cluster, never production.
Sized for at least 2 schedulable nodes of 4 CPU / 16 GiB: about 5 CPU of requests plus one deliberately Pending
workload; tested on k3s with Rancher 2.15 (Fleet 0.16).

## Scenarios

| Namespace | Project | Scenario | What the dashboard / planner shows |
|---|---|---|---|
| `payments-api`, `payments-batch`, `crm-web`, `crm-search` | payments, crm | Requests that are never used (pause containers) | Low efficiency |
| `payments-worker` | payments | Uses its requests (CPU burn capped by a CPU limit, 300 Mi held) | ~95 % CPU, ~70 % memory efficiency; CPU limit counted |
| `crm-reports` | crm | Uses more than it requests (0.5 CPU on a 0.1 request) | ~500 % efficiency |
| `crm-legacy` | crm | A BestEffort pod and a pod with a CPU request only | BestEffort (S5), no CPU request, no memory request/limit (S2) |
| `payments-guaranteed` | payments | requests = limits | Guaranteed QoS, memory limit = request (S3 prod) |
| `payments-overcommit` | payments | Limits 16–20× the requests | Limits far above requests |
| `payments-sidecar` | payments | Init container 300m + native sidecar 50m + app 100m | Effective pod requests 300m |
| `payments-overhead` | payments | RuntimeClass with a pod overhead of 250m / 128Mi | Effective requests include the overhead (`OVERHEAD=0` skips it) |
| `payments-hpa` | payments | HPA on a CPU-busy Deployment, 1 → 3 replicas | Requests peak above now; HPA counted |
| `payments-cron` | payments | CronJob every 10 min, 2 × 100m for 3 min | Short request peaks (with Prometheus) |
| `payments-bigjob` | payments | Asks for 1 CPU more than the largest node | Pending requests |
| `payments-batch` (Job), `payments-failed` | payments | A finished and a failed Job | Completed / failed pods not counted |
| `payments-agent` | payments | DaemonSet | Requests on every node |
| `payments-stateful` | payments | StatefulSet | Counted like other pods |
| `crm-quota` | crm | ResourceQuota with headroom + LimitRange; pods without values get the defaults | Quota and LimitRange counts |
| `crm-quota-full` | crm | 3 × 200m against a 500m quota | 2 pods run, the third is refused ("exceeded quota") |
| `crm-quota-strict` | crm | Quota without a LimitRange | Pod without requests refused ("must specify requests") |
| `crm-idle` | crm | Namespace without workloads | Project namespace with 0 requests |
| `search-app`, `search-batch` | search | **Rancher Project quota**: 1 CPU for the project, 500m per namespace, container defaults | Planner's "Rancher quota now"; `search-batch` is refused its third pod by the quota Rancher creates |
| `platform-dns`, `platform-logging` | platform-tools | Project name contains "platform" | Counted as System (platform reserve), hidden in the planner |
| `demo-ingress` | — | Name contains "ingress" | System by the namespace regex |
| `legacy-app` | — | In no project | Unassigned namespaces |
| `sandbox` | Default | In Rancher's Default project | Project "Default" |
| `payments-reserve` | payments | Pause pods scaled so project requests end ~5 % above the N+1 room (only when needed) | "Room for projects (N+1)" red |

The busy workloads (worker, reports, HPA) use about 1.3 CPU continuously.

## Apply

The script applies to the current kubectl context (or `CONTEXT=...`). Namespaces get the label
`resource-report/scenario=true`, which is how `delete` finds them again.

**Rancher-managed cluster, with access to the local cluster from the same place:**

```bash
LOCAL_CONTEXT=rancher-local CLUSTER_NAME=onprem-test-01 CONTEXT=onprem-test-01 ./apply-scenarios.sh apply
```

It looks up the Projects `payments`, `crm`, `search`, `platform-tools` and `Default` on that cluster and creates the
missing ones (`search` with the Rancher Project quota above).

**Local and downstream cluster reachable from different places:** on the local cluster first, then paste the
printed exports where the downstream cluster is reachable:

```bash
CLUSTER_NAME=onprem-test-01 ./apply-scenarios.sh projects     # on the Rancher local cluster: prints export lines
export CLUSTER_ID=c-m-... PROJECT_PAYMENTS=p-... ...           # the printed lines
./apply-scenarios.sh apply                                     # on the downstream cluster
```

**Cluster outside Rancher:** namespaces are grouped by the label `project=<name>`; install the chart with
`collection.projectLabel=project`. The search namespaces get a plain ResourceQuota and LimitRange instead of
Rancher's.

```bash
PROJECT_MODE=label ./apply-scenarios.sh apply
```

Other actions: `status` (pods per scenario namespace and the quota refusals), `print` (render only), `delete`
(`DELETE_PROJECTS=1` also removes the Rancher Projects the script created). Settings: `OVERHEAD=0`,
`N1_BREACH=0`, `PROJECT_LABEL`, `RANCHER_QUOTA=0|1` (whether Rancher manages the search namespaces' quota;
detected when the local cluster is reachable).

Changing a Rancher Project quota to less than its namespaces hold is refused by Rancher; run `delete` first.

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
