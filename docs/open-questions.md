# Open Questions

| # | Question | Impacts | Owner | Status |
|---|---|---|---|---|
| Q1 | Are the AKS clusters imported into / managed by Rancher? | Enforcement mechanism on AKS ([04](04-technical-design.md#aks-clusters)) | Platform | Open |
| Q2 | Is there one cluster per environment, or do some clusters host multiple environments? | Allocation granularity | Platform | Open |
| Q3 | Is the budget per project defined per environment or as one total? | Env split ([03](03-allocation-model.md#environments)) | Finance | Open |
| Q4 | Which requests/limits standards has the req/lmt initiative already agreed (esp. CPU limits, memory limit = request)? | [02](02-resource-standards.md), ADR-0003 | Req/lmt initiative | Open |
| Q5 | Is a policy engine (Kyverno / Gatekeeper) already deployed anywhere? | Tool choice | Platform | Open |
| Q6 | Is GitOps (Fleet/Argo CD/Flux) and/or Terraform with the rancher2 provider already in use? | ADR-0001 | Platform | Open |
| Q7 | One blended unit rate or separate on-prem / AKS rates? Who owns the TCO numbers? | Unit rates | Finance + Platform | Open |
| Q8 | Is persistent storage significant enough to allocate/bill? Which storage classes? | Quota dimensions | Platform | Open |
| Q9 | Who should hold Rancher Project Owner role (quota redistribution rights)? | Self-service model | Platform + projects | Open |
| Q10 | Showback only, or real chargeback in a later phase? | Reporting accuracy requirements | Finance | Open |
| Q11 | Is Rancher Monitoring (Prometheus) installed on all clusters, with enough retention for quarterly reviews? | Visibility | Platform | Open |
| Q12 | How are shared/platform services (ingress, monitoring, logging) funded — overhead in unit rate or separate? | Unit rates | Finance + Platform | Open |
| Q13 | Where does the allocations repository (`allocations/projects/*.yaml`) live (GitHub / GitLab, which repo), and may the planner open pull requests there (GitHub App or token)? | Planner action P1 ([07](07-actions.md)) | Platform | Open |
| Q14 | May the planner and dashboard change Rancher directly (quota, Projects, namespace moves, container defaults) as the logged-in user, or must every quota change go through Git? | ADR-0001, actions P2–P4, D2, D3 ([07](07-actions.md)) | Platform + architecture | Open |
| Q15 | Which OpenTelemetry collector configuration do the clusters run (`k8s_cluster` with node allocatable, `kubeletstats`, `k8sattributes` with the `field.cattle.io/projectId` namespace label, `k8s.cluster.name`), in which mapping mode and index do the metrics land in the central Elastic, and how long are they kept? | [Kibana dashboard](../guides/kibana-dashboard.md#prerequisites-in-the-collector) | Platform + observability | Open |
| Q16 | What does a SUSE Rancher Prime subscription cost per added node, and what does the contract count (nodes, vCPU/cores, sockets; AKS nodes too)? | Planner what-if calculator ([guide](../guides/allocation-planner.md#what-if-calculator-a-new-application)), on-prem rate | Platform + procurement | Open |
| Q17 | Are on-prem nodes bare metal or VMs, and if VMs, are a cluster's node VMs spread over different hypervisor hosts (and racks)? What goes down together decides whether N+1 counts nodes or hosts | N+1 and HA ([09](09-operating-principles.md#1-n1-node-failure-tolerance)) | Platform + infra | Open |
| Q18 | Are PriorityClasses, PodDisruptionBudgets and topology spread already standard in the clusters, or part of the req/lmt initiative? | [09](09-operating-principles.md#6-ha-setup) sections 6–7 | Platform + req/lmt initiative | Open |
