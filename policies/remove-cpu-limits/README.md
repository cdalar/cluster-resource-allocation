# remove-cpu-limits (Kubewarden policy)

Mutating [Kubewarden](https://www.kubewarden.io/) policy that removes `limits.cpu` from every container and init
container at admission. Memory limits and CPU requests stay as they are. It implements
[ADR-0003](../../docs/adr/0003-cpu-limits.md) (CPU limits optional: CPU is compressible and CFS quota throttles
containers even on idle nodes) for teams that cannot or do not want to change every Helm chart.

| | |
|---|---|
| Kinds | `Pod` (CREATE) by default; also works on `Deployment`, `ReplicaSet`, `StatefulSet`, `DaemonSet`, `ReplicationController`, `Job`, `CronJob` (their pod template) when the policy's `rules` include them |
| Mutation | `limits.cpu` removed from `spec.containers[*]` and `spec.initContainers[*]`. An empty `limits` block is removed too. A container with a CPU limit but no CPU request gets the old limit as its request, which is what Kubernetes would have defaulted anyway, so nothing loses its scheduling guarantee |
| Untouched | Memory limits and requests, every other field (the object is handled as plain JSON, so unknown fields survive), ephemeral containers (they cannot set resources), pod `UPDATE` requests (a running pod's resources cannot be changed that way) |
| Opt-out | Images listed in `ignoreImages`; pods or pod templates annotated `allocation.cluster-resource-allocation/keep-cpu-limits: "true"` (name configurable). Use it for workloads that need `Guaranteed` QoS, e.g. for CPU pinning with the static CPU manager |

## Settings

```yaml
settings:
  # Containers whose image matches are left alone: the whole image, or a prefix ending with '*'
  ignoreImages:
    - registry.example.com/platform/*
    - docker.io/library/busybox:1.36
  # Annotation (on the pod or on the pod template) that keeps the CPU limits when set to "true"
  keepAnnotation: allocation.cluster-resource-allocation/keep-cpu-limits
```

Both are optional. Settings are checked when the policy is loaded (`*` only at the end of a pattern, no whitespace
in the annotation name).

## Install

[`manifests/clusteradmissionpolicy.yaml`](manifests/clusteradmissionpolicy.yaml) is a `ClusterAdmissionPolicy` for
tenant namespaces (platform namespaces excluded by `namespaceSelector`). It starts in `monitor` mode: the policy
server logs what it would change and changes nothing. Switch `mode` to `protect` to mutate.

```bash
kubectl apply -f manifests/clusteradmissionpolicy.yaml
kubectl get clusteradmissionpolicy remove-cpu-limits      # status must be "active"
kubectl -n kubewarden logs deploy/policy-server-default | grep remove-cpu-limits
```

Things to know before `protect`:

- **LimitRange defaults are removed too.** The `LimitRanger` admission plugin runs before webhooks, so a CPU limit
  that a `LimitRange` `default` adds (Rancher's *Container Default Resource Limit* does this) is removed by this
  policy. Drop the CPU default from the LimitRange as well, otherwise every pod is mutated twice over.
- **ResourceQuota on `limits.cpu`.** A namespace whose quota counts `limits.cpu` rejects pods without a CPU limit
  after this policy ran. Allocation files from this repository quota `requests.cpu` only
  ([allocation as code](../../guides/allocation-as-code.md)); check other quotas first.
- **Workload objects.** With the default rules only pods are changed, so `kubectl get deploy -o yaml` still shows
  the CPU limit the team wrote, while the pods have none. Adding the workload kinds to `rules` (commented in the
  manifest) keeps both consistent, but every GitOps tool then sees a difference to Git for workloads with a CPU limit.
- **QoS class.** Pods with both limits equal to requests were `Guaranteed`; without a CPU limit they are
  `Burstable`. Eviction under memory pressure is decided by memory usage above requests, which does not change.
  Use the opt-out annotation for workloads that need `Guaranteed` (CPU pinning).
- **Running pods are not changed.** The policy acts on admission. Restart (roll) workloads to apply it. The
  [dashboard](../../charts/cluster-resource-report/README.md) shows the containers that still carry CPU limits.

## Build and test

Needs Go (unit tests), [TinyGo](https://tinygo.org/) (build) and [`kwctl`](https://github.com/kubewarden/kwctl)
plus `bats` and `jq` (end-to-end tests). `go.mod` replaces `go-openapi/strfmt` with Kubewarden's fork, as every Kubewarden
Go policy does, because the original pulls in network lookups TinyGo cannot compile.

```bash
make test                    # go test
make policy.wasm             # tinygo build (or: make docker-build, with the tinygo/tinygo image)
make annotated-policy.wasm   # kwctl annotate with metadata.yml and this README
make e2e-tests               # kwctl run against test_data/*.json

# Try it by hand
kwctl run annotated-policy.wasm -r test_data/pod_with_cpu_limit.json | jq -r .patch | base64 -d | jq
```

Layout: `main.go` registers the waPC functions, `validate.go` holds the mutation, `settings.go` the settings,
`test_data/` the admission requests used by the Go and bats tests.

## Release

CI (`.github/workflows/policy.yml`) tests and builds the policy on pushes to `main` that touch this directory.
A tag `remove-cpu-limits-vX.Y.Z` (must match `io.kubewarden.policy.version` in `metadata.yml`) pushes
`ghcr.io/cdalar/policies/remove-cpu-limits:vX.Y.Z`. Air-gapped clusters: `kwctl pull` the policy and `kwctl push`
it to the internal registry, then point `spec.module` there.
