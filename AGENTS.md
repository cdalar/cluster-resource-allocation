# AGENTS.md

Guidance for coding agents working in this repository.

## Git workflow

- Work directly on `main`. Do not create branches and do not open pull requests.
- Commit to `main` and push to `origin main`.
- Keep commits focused, with a descriptive message.

## Project

Design, process and tooling for allocating Kubernetes resources (requests/limits, quotas) to projects/streams
based on their budgets. Clusters are mainly on-prem, managed by Rancher (one Rancher Project per stream,
several namespaces each), plus some AKS clusters.

| Path | Content |
|---|---|
| `docs/` | Design and process docs (numbered), `open-questions.md`; `docs/images/` holds the README screenshots (synthetic demo data) |
| `docs/adr/` | Architecture decision records; copy `0000-template.md` for new ones |
| `guides/` | User guides for the tools (`installation.md`, `allocation-planner.md`, `allocation-as-code.md`, `kibana-dashboard.md`), separate from the design docs |
| `discovery/` | `discover.py` (CLI) and `server.py` + `static/index.html` (in-cluster dashboard): read-only baseline of requests, limits and usage; `planner.py` + `static/planner.html` (allocation planner, plans only); `publish_rancher_names.py` publishes Rancher Project names to downstream clusters (Fleet Bundle) |
| `allocation/` | `generate.py` checks allocation files (`settings.yaml`, `projects/*.yaml`: vCPU and memory per project, cluster and namespace, no money) and writes a ResourceQuota + LimitRange per namespace (plain Kubernetes, no Rancher objects); applies nothing. `--discover` reads nodes and pods (read-only, `kubectl get`) and rewrites the `clusters:` section of `settings.yaml`; `--import-projects` writes starting `projects/*.yaml` from Rancher Projects (namespace annotation + `projects.json`) and never changes an existing allocation. `examples/` is the sample allocations directory |
| `kibana/` | `build_dashboard.py` generates `cluster-resource-report.ndjson` (the report as five linked dashboards: capacity, projects, project owner (one Project picked by name in an ES|QL control, on every cluster), workload limits, and "Will it fit?" for a new deployment (ES|QL controls for replicas and size), each with a saved filter that leaves out control-plane nodes; `--split` writes one file per dashboard) and, with `--no-project`, `cluster-resource-report-no-project.ndjson` (Kibana dashboard on the OTel metrics in the central Elastic; stdlib only); `otel-collector-fragment.yaml` lists the collector settings it needs, `otel-kube-stack-values.yaml` is the values overlay for the `opentelemetry-kube-stack` chart; `build_collector_values.py` generates the kube-stack overlay that sets `rancher.project.name` (dashboard variant `build_dashboard.py --project-name-field`); `add_project_names.py` writes Rancher Project names into an existing dashboard `.ndjson` (must give the same file as `build_dashboard.py --rancher-projects`; the test checks it); `publish_project_names.py` writes Rancher Projects (ID, name, cluster) to one lookup index in Elasticsearch (its only write; directly or through Kibana's Dev Tools proxy with `--kibana`; one file, no repository imports). With `--project-lookup-index rancher-projects` it writes `cluster-resource-report-project-names.ndjson` (Project names joined from that lookup index). Regenerate and commit the script and the three generated `.ndjson` files after changing queries. `cluster-resources-cra-elastic.ndjson` is an export from the `cra-elastic` test Kibana (report with Project names), not generated |
| `charts/cluster-resource-report/` | Helm chart that runs `server.py` in a cluster; image from `discovery/Dockerfile` |

## Conventions

- Docs are Markdown and keep the existing structure: numbered files, tables for comparisons, Mermaid for diagrams.
  Update `README.md` when adding a doc or tool, and `docs/open-questions.md` when a question is answered or raised.
- `discovery/` code uses the Python standard library and `kubectl` only; no third-party packages.
  It must stay read-only against clusters. Exceptions: `publish_rancher_names.py` writes one Fleet Bundle
  (`rancher-project-names`) on the Rancher local cluster, with its own service account; the planners write
  their own plan ConfigMaps (`<fullname>-planner`, `<fullname>-capacity`) in their own namespace. The chart (Helm, not
  the running app) may render one Fleet `HelmOp` that installs this chart itself on chosen downstream clusters
  (`rancher.deployDownstream`). Never other workloads, quotas or Rancher objects.
  The dashboard page is self-contained (no CDN) for air-gapped clusters.
- `allocation/` uses the Python standard library and `kubectl get` only (its own YAML subset reader) and writes files only, never to a cluster.
- Run tests after changing the code: `cd discovery && python3 -m unittest -v test_discover test_server test_planner`;
  for `allocation/`: `cd allocation && python3 -m unittest -v test_generate`;
  for `kibana/`: `cd kibana && python3 -m unittest -v test_build_dashboard`.
  After changing the chart: `helm lint charts/cluster-resource-report`.
- Keep `discovery/README.md`, `discovery/rbac.yaml` and the chart (values, RBAC, README) in sync with the code.
- Test against a throwaway cluster, never against production kubeconfig contexts.
- CI (`.github/workflows/image.yml`) builds the image on pushes to `main` that touch `discovery/` or `charts/`.
  Releases are tags `vX.Y.Z` that must match `version` and `appVersion` in `charts/cluster-resource-report/Chart.yaml`;
  on a tag, `image.yml` pushes the image and `.github/workflows/chart.yml` pushes the chart to
  `oci://ghcr.io/cdalar/charts/cluster-resource-report-chart`; both then call `dockerhub.yml`, which copies them to
  Docker Hub (`cdalar/cluster-resource-report`, `cdalar/cluster-resource-report-chart`).
