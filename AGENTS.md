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
| `docs/` | Design and process docs (numbered), `open-questions.md` |
| `docs/adr/` | Architecture decision records; copy `0000-template.md` for new ones |
| `discovery/` | `discover.py` (CLI) and `server.py` + `static/index.html` (in-cluster dashboard): read-only baseline of requests, limits and usage; `publish_rancher_names.py` publishes Rancher Project names to downstream clusters (Fleet Bundle) |
| `charts/cluster-resource-report/` | Helm chart that runs `server.py` in a cluster; image from `discovery/Dockerfile` |

## Conventions

- Docs are Markdown and keep the existing structure: numbered files, tables for comparisons, Mermaid for diagrams.
  Update `README.md` when adding a doc or tool, and `docs/open-questions.md` when a question is answered or raised.
- `discovery/` code uses the Python standard library and `kubectl` only; no third-party packages.
  It must stay read-only against clusters. Sole exception: `publish_rancher_names.py` writes one Fleet Bundle
  (`rancher-project-names`) on the Rancher local cluster, with its own service account.
  The dashboard page is self-contained (no CDN) for air-gapped clusters.
- Run tests after changing the code: `cd discovery && python3 -m unittest -v test_discover test_server`.
  After changing the chart: `helm lint charts/cluster-resource-report`.
- Keep `discovery/README.md`, `discovery/rbac.yaml` and the chart (values, RBAC, README) in sync with the code.
- Test against a throwaway cluster, never against production kubeconfig contexts.
- CI (`.github/workflows/image.yml`) builds the image on pushes to `main` that touch `discovery/` or `charts/`.
  Releases are tags `vX.Y.Z` that must match `appVersion` in `charts/cluster-resource-report/Chart.yaml`.
