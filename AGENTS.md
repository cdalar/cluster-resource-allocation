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
| `discovery/` | `discover.py`: read-only baseline of requests, limits and usage per cluster / project / namespace |

## Conventions

- Docs are Markdown and keep the existing structure: numbered files, tables for comparisons, Mermaid for diagrams.
  Update `README.md` when adding a doc or tool, and `docs/open-questions.md` when a question is answered or raised.
- `discovery/discover.py` uses the Python standard library and `kubectl` only; no third-party packages.
  It must stay read-only against clusters.
- Run tests after changing the script: `cd discovery && python3 -m unittest -v test_discover`.
- Keep `discovery/README.md` (columns, options, caveats) and `discovery/rbac.yaml` in sync with the script.
- Test against a throwaway cluster, never against production kubeconfig contexts.
