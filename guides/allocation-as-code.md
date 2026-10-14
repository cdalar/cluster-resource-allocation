# Allocation as code: from YAML to quotas

Each project's CPU and memory per cluster and namespace lives in a YAML file in Git, and changes go by pull request
([04.2](../docs/04-technical-design.md#42-allocation-as-code)). [`allocation/generate.py`](../allocation/generate.py)
checks the files and generates a `ResourceQuota` and a `LimitRange` for every namespace. It uses plain Kubernetes
objects, with no Rancher objects, and works in vCPU and memory only, with no money involved. It writes files and
applies nothing.

```
allocations/                       (sample: allocation/examples/)
├── settings.yaml                  clusters, environments, container defaults
└── projects/
    ├── payments.yaml              one file per project
    └── data-platform.yaml
```

## Format

**`settings.yaml`**

| Key | Meaning |
|---|---|
| `environments.<env>.memoryLimitFactor` | `limits.memory` = `requests.memory` × factor: 1 in prod (S3), up to 2 elsewhere |
| `environments.<env>.nodeFailures` | `--discover`: the N largest nodes are left out of capacity (prod: 1 = N+1; default 0) |
| `environments.<env>.platformOverheadPercent` | `--discover`: % of the counted nodes' allocatable kept for the platform **on top of** its measured requests: growth, platform pods using more than they request, upgrades (example: 5; default 0) |
| `environments.<env>.maxQuotaPercent` | `--discover`: share of what is left that may be allocated (prod 80, test 100, dev 150; default 100) |
| `discovery.skipCordoned`, `.skipTaintEffects`, `.skipNodeLabels` | `--discover`: nodes that don't count as capacity. Default: cordoned nodes and nodes with a `NoSchedule` or `NoExecute` taint (control-plane), no labels |
| `discovery.systemNamespaces` | `--discover`: platform namespaces (regex); their pods' requests are subtracted. Default: the dashboards' system-namespace regex |
| `containerDefault.cpu`, `.memory` | Requests for containers that set none (LimitRange); their memory limit = memory × factor |
| `clusters.<name>.env` | One of the environments. The name is also the directory under `build/` |
| `clusters.<name>.capacity.cpu`, `.memory` | What may be allocated in total, after N+1, platform and headroom ([03](../docs/03-allocation-model.md#headroom-and-overcommit)). Written by `--discover`, or by hand. Optional; without it the sum is not checked |

**`projects/<name>.yaml`**

```yaml
project: payments
owners: [payments-lead@corp.example]
allocations:
  - cluster: prod-01
    cpu: 40                       # optional total for the project: its namespaces must fit in it
    memory: 160Gi
    namespaces:                   # each gets a ResourceQuota with these numbers
      - name: payments-api
        cpu: 24                   # vCPU: requests.cpu
        memory: 96Gi              # requests.memory
        # memoryLimit: 96Gi       # optional; default memory × memoryLimitFactor
        # pods: 200               # optional
      - name: payments-worker
        cpu: 16
        memory: 64Gi
  - cluster: test-01
    namespaceDefault:             # for namespaces listed without numbers
      cpu: 4
      memory: 16Gi
    # containerDefault: {...}     # optional, instead of the one in settings.yaml
    namespaces:
      - name: payments-api
      - name: payments-worker
```

## Capacity from the clusters

`--discover CONTEXT=ENV` reads a cluster with `kubectl get nodes` and `kubectl get pods -A` (read-only) and writes
its block under `clusters:` in `settings.yaml`. The cluster is named after the context. Other clusters, comments and
the other sections are kept, so you commit the change like any other.

```
capacity = (allocatable of the counted nodes − the nodeFailures largest nodes − platform requests
            − platformOverheadPercent of the allocatable) × maxQuotaPercent / 100
           (CPU rounded down to 0.1 vCPU, memory to whole GiB)
```

```bash
./allocation/generate.py allocations --discover prod-01=prod --discover test-01=test
./allocation/generate.py allocations --kubeconfig ~/.kube/aks.yaml --discover aks-prod=prod
```

Each block gets the full calculation with the real numbers as comments, so a pull request shows how every value came
about and why it changed:

```yaml
  prod-01:
    env: prod
    # capacity discovered 2026-10-14 from context prod-01:
    #   environment prod: nodeFailures 1, platformOverheadPercent 5, maxQuotaPercent 80
    #   nodes counted (3): 2 x 16 vCPU / 64.0 GiB, 1 x 8 vCPU / 32.0 GiB
    #   nodes left out (2): cp-1 (taint node-role.kubernetes.io/control-plane:NoSchedule), w-4 (cordoned)
    #   platform requests: 3 pod(s) in platform namespaces on counted nodes
    #   CPU: allocatable 40.00 - largest 1 node(s) 16.00 - platform requests 2.00 - overhead (5 % of 40.00) 2.00 = 20.00 vCPU; x 80 % = 16.00 vCPU, rounded down -> 16 vCPU
    #   memory: allocatable 160.0 - largest 1 node(s) 64.0 - platform requests 3.5 - overhead (5 % of 160.0) 8.0 = 84.5 GiB; x 80 % = 67.6 GiB, rounded down -> 67 GiB
    capacity:
      cpu: 16
      memory: 67Gi
```

Platform requests are the effective requests (with init containers, sidecars and overhead, as the scheduler counts
them) of the pods in platform namespaces on counted nodes. Pods on nodes that are left out don't count. If any
cluster fails, for example because `kubectl` can't reach it, `settings.yaml` is left unchanged.

## Use

```bash
./allocation/generate.py allocations --check              # pull request: checks only
./allocation/generate.py allocations --out build          # after merge: build/<cluster>/<namespace>.yaml
./allocation/generate.py allocations --out build --per-project   # or one file per project: build/<cluster>/<project>.yaml
kubectl --context prod-01 apply --server-side --field-manager=allocations -f build/prod-01/
```

It prints the namespaces and vCPU / memory per project and cluster, and each cluster's total against its capacity.
It exits with 1 and writes nothing when any check fails.

| Check | Rule |
|---|---|
| Input | Known clusters, valid quantities (`4`, `500m`, `160Gi`), one entry per project and cluster, each project in one file, every allocation lists its namespaces |
| Memory limit | `memory` ≤ `memoryLimit` ≤ `memory` × `memoryLimitFactor` (prod: equal) |
| Namespaces | A namespace belongs to one project; the namespaces fit in the project's total, if it has one |
| Cluster | All namespaces together ≤ `capacity` |

## Output

`build/<cluster>/<namespace>.yaml`, or with `--per-project` `build/<cluster>/<project>.yaml` holding all the project's
namespaces on that cluster. For each namespace:

| Object | Content |
|---|---|
| `ResourceQuota` `allocation` | `requests.cpu`, `requests.memory`, `limits.memory`, `pods` (if set) |
| `LimitRange` `allocation-defaults` | Default request (CPU, memory) and memory limit for containers that set none. Needed because once a quota limits a resource, Kubernetes rejects pods that don't set it |

Both are labelled `allocation.cluster-resource-allocation/managed: "true"` and `…/project: <project>`.

| Point | Detail |
|---|---|
| Namespaces | Not created: apply into existing namespaces (create them first, or apply fails for that file) |
| Existing pods | A quota doesn't stop running pods. It blocks new ones (rollouts, restarts, drains) once the namespace is over it. Roll out per environment ([06](../docs/06-rollout.md)) |
| Removed allocations | A second run removes their files from `build/` (only files it generated), but not the objects in the cluster. Delete them with `kubectl delete resourcequota,limitrange -l allocation.cluster-resource-allocation/project=<project> -n <namespace>` |
| Rancher | On clusters managed by Rancher, a namespace in a Rancher Project with a Project quota gets Rancher's own ResourceQuota as well. Don't use both on the same namespace |
| YAML | Standard library only: block maps and lists, `[a, b]` lists, comments and quoted strings. `{a: 1}` maps are rejected with a message |
| Tested | Unit tests on the checks, the generated objects and `--discover` with a fake `kubectl` (`cd allocation && python3 -m unittest -v test_generate`). Not yet run against a cluster |
