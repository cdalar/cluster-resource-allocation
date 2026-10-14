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
| `containerDefault.cpu`, `.memory` | Requests for containers that set none (LimitRange); their memory limit = memory × factor |
| `clusters.<name>.env` | One of the environments. The name is also the directory under `build/` |
| `clusters.<name>.capacity.cpu`, `.memory` | What may be allocated in total, after N+1, platform and headroom ([03](../docs/03-allocation-model.md#headroom-and-overcommit); the capacity planner shows it). Optional; without it the sum is not checked |

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

## Use

```bash
./allocation/generate.py allocations --check              # pull request: checks only
./allocation/generate.py allocations --out build          # after merge: build/<cluster>/<namespace>.yaml
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

`build/<cluster>/<namespace>.yaml`, for each namespace:

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
| Tested | Unit tests on the checks and the generated objects (`cd allocation && python3 -m unittest -v test_generate`). Not yet applied to a cluster |
