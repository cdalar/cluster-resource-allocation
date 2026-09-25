# cluster-resource-allocation

Design, process and technical solution for allocating Kubernetes cluster resources (requests/limits, quotas)
to projects/streams based on their budgets, across on-prem Rancher-managed clusters and AKS.

## Documentation

| Doc | Content |
|---|---|
| [1. Context & goals](docs/01-context-and-goals.md) | Current situation, problem, goals, principles |
| [2. Resource standards](docs/02-resource-standards.md) | Requests/limits standards and defaults |
| [3. Allocation model](docs/03-allocation-model.md) | Budget → quota, unit rates, headroom, billing basis |
| [4. Technical design](docs/04-technical-design.md) | Rancher Project quotas, allocation-as-code, policies, showback |
| [5. Process](docs/05-process.md) | Onboarding, change requests, reviews, capacity planning, RACI |
| [6. Rollout](docs/06-rollout.md) | Phased rollout and risks |
| [Open questions](docs/open-questions.md) | Items to resolve during discovery |
| [ADRs](docs/adr/) | Architecture decision records |

## Tools

| Tool | Purpose |
|---|---|
| [discovery](discovery/README.md) | Baseline of current requests, limits and usage per cluster / project / namespace |

## Status

Draft — design phase.
