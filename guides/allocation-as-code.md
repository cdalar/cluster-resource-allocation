# Allocation as code: from YAML to quotas

Each Rancher Project's CPU and memory per cluster lives in a YAML file in Git, and changes go by pull request
([04.2](../docs/04-technical-design.md#42-allocation-as-code)). [`allocation/generate.py`](../allocation/generate.py)
checks the files and generates the Kubernetes resources that enforce them. It works in vCPU and memory only, with no
money involved. It writes files and applies nothing.

```
allocations/                       (sample: allocation/examples/)
├── settings.yaml                  clusters, environments, container defaults
└── projects/
    ├── payments.yaml              one file per Rancher Project
    └── data-platform.yaml
```

## Format

**`settings.yaml`**: the clusters projects can get capacity on.

| Key | Meaning |
|---|---|
| `environments.<env>.memoryLimitFactor` | `limits.memory` = `requests.memory` × factor: 1 in prod (S3), up to 2 elsewhere |
| `containerDefault.cpu`, `.memory` | Requests for containers that set none (LimitRange); their memory limit = memory × factor |
| `clusters.<name>.id` | Rancher cluster ID (`c-m-…`, `local`); or pass `--rancher-clusters` |
| `clusters.<name>.env` | One of the environments |
| `clusters.<name>.managedBy` | `rancher` (default): quota on the Rancher Project. `kubernetes`: cluster not in Rancher, ResourceQuota + LimitRange per namespace |
| `clusters.<name>.capacity.cpu`, `.memory` | What may be allocated to projects in total, after N+1, platform and headroom ([03](../docs/03-allocation-model.md#headroom-and-overcommit); the capacity planner shows it). Optional; without it the sum is not checked |

**`projects/<name>.yaml`**: one Rancher Project.

```yaml
project: payments                 # Rancher Project display name
owners: [payments-lead@corp.example]
allocations:
  - cluster: onprem-prod-01
    cpu: 40                       # vCPU: requests.cpu of the whole Project on this cluster
    memory: 160Gi                 # requests.memory
    # memoryLimit: 160Gi          # optional; default memory × memoryLimitFactor
    # pods: 300                   # optional
    namespaceDefault:             # quota each namespace of the Project gets (Rancher requires it)
      cpu: 10
      memory: 40Gi
    # containerDefault: {...}     # optional, instead of the one in settings.yaml
    # projectId: p-abcde          # optional, instead of --rancher-projects
  - cluster: aks-prod-weu         # managedBy: kubernetes: list the namespaces
    cpu: 8
    memory: 32Gi
    namespaces:
      - name: data-ingest
        cpu: 6
        memory: 24Gi
      - name: data-api            # without cpu/memory: namespaceDefault
    namespaceDefault:
      cpu: 2
      memory: 8Gi
```

The allocation planner's export (`quota:` with `requests.cpu`, `requests.memory`, `limits.memory`) is read as well.
Add `namespaceDefault` to it for Rancher clusters.

## Use

```bash
# pull request: checks only
./allocation/generate.py allocations --check

# after merge: generate; the Project IDs come from the Rancher local cluster
kubectl --context <rancher-local> get projects.management.cattle.io -A -o json > projects.json
./allocation/generate.py allocations --out build --rancher-projects projects.json

# apply
kubectl --context <rancher-local> apply --server-side --field-manager=allocations -R -f build/rancher/
kubectl --context <aks-prod-weu> apply --server-side --field-manager=allocations -f build/kubernetes/aks-prod-weu/
```

It prints the allocations per cluster and the totals against capacity. It exits with 1 and writes nothing when any
check fails.

| Check | Rule |
|---|---|
| Input | Known clusters, valid quantities (`4`, `500m`, `160Gi`), one entry per project and cluster, each project in one file |
| Memory limit | `memory` ≤ `memoryLimit` ≤ `memory` × `memoryLimitFactor` (prod: equal) |
| Namespaces | `namespaceDefault` ≤ the project; listed namespaces add up to ≤ the project |
| Cluster | All projects together ≤ `capacity` |
| Rancher | The Project exists on that cluster (create it in Rancher first; the generator never creates Projects) |

## Output

| File | Content | Applied on |
|---|---|---|
| `build/rancher/<cluster>/<project>.yaml` | Rancher `Project` (`management.cattle.io/v3`): `resourceQuota` (project total), `namespaceDefaultResourceQuota`, `containerDefaultResourceLimit` | Rancher local cluster; Rancher then writes a ResourceQuota and a LimitRange into every namespace of the Project |
| `build/kubernetes/<cluster>/<namespace>.yaml` | `ResourceQuota` `allocation` and `LimitRange` `allocation-defaults` | That cluster (`managedBy: kubernetes`) |

On Rancher clusters, don't write ResourceQuota objects into the namespaces yourself, because Rancher overwrites them.
The Project owner moves quota between their namespaces within the Project limit.

| Point | Detail |
|---|---|
| Existing pods | A quota doesn't stop running pods. It blocks new ones (rollouts, restarts, drains) once the namespace is over it. Roll out per environment ([06](../docs/06-rollout.md)) |
| Rancher apply | Server-side apply with its own field manager sets only the fields in the file. Rancher keeps the rest of the Project |
| Removed allocations | A second run removes their files from `build/`, but not the quota in Rancher. Clear it in Rancher, or keep the project with smaller numbers |
| YAML | Standard library only: block maps and lists, `[a, b]` lists, comments and quoted strings. `{a: 1}` maps are rejected with a message |
| Tested | Unit tests on the checks and the generated objects (`cd allocation && python3 -m unittest -v test_generate`). Not yet applied to a Rancher or Kubernetes cluster |
