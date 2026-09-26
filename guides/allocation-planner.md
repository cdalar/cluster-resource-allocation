# Allocation Planner — User Guide

How to plan CPU and memory quota per project and cluster with the allocation planner, and what each parameter
means.

## What the planner is for

The allocation planner lets the platform team decide how much CPU and memory each project (a Rancher Project, one
per stream) gets on each cluster, and check that the plan fits both the project's budget and the cluster's
capacity.

It is a **planning tool only**. It never changes a cluster or a Rancher quota:

- **Save plan** stores the plan inside the planner itself (on its data volume).
- **Export YAML** produces the allocation files (`allocations/projects/<project>.yaml`).
- Those files go through the normal pull request, CI validation and Terraform flow
  ([04](../docs/04-technical-design.md#42-allocation-as-code)), which is what actually sets the Rancher Project quotas.

So you can try out numbers freely: nothing reaches a running workload until the exported files are merged and
applied.

## Opening the planner

Open it from Rancher, logged in with your own Rancher account:

1. In the Rancher UI, open the **local** cluster (the Rancher server's own cluster).
2. In the cluster's left-hand menu, click **Allocation planner**.

It opens in a new tab. You can also reach it from the resource dashboard: the **Allocation planner** link in its
header appears whenever the planner is enabled.

There is one planner for all clusters. It runs on the local cluster and reads every cluster Rancher manages, so you
don't open it per cluster.

**Who can use it:** anyone who can open services in the `resource-report` namespace of the local cluster through
Rancher, which normally means the platform team (cluster owners of the local cluster). The planner has no login of
its own; it relies on Rancher's.

## The page at a glance

The page reads top to bottom: settings first, then clusters, then projects. Every number updates as you type.

| Part | What it shows or does |
| --- | --- |
| Header | Plan version and when it was last saved; buttons **Dashboard**, **Export YAML** and **Save plan** (enabled once you have unsaved changes) |
| Planning only notice | Reminder that nothing is applied to any cluster |
| Issues | Everything in the plan that breaks a rule, or "No issues" when every budget and every cluster limit fits |
| Tiles | Projects planned, planned cost per month (with the sum of all budgets), projects over budget, clusters over their limit |
| Rates and rules | Collapsed by default; unit rates per platform, and node failures, headroom and memory limit per environment |
| Clusters | Every cluster in Rancher, its environment, platform, capacity and planned quota |
| Projects | One block per project with its budget and a quota row per cluster |

The status text next to the buttons says *Unsaved changes*, *Saving…* or *Saved*, and shows errors in red.

## Parameters: rates and rules

Set these once, before planning projects. Open **Rates and rules** to see them. They apply to the whole plan.

### Currency

The label shown after every amount, e.g. `EUR`. It is a label only; nothing is converted.

### Platforms and unit rates

A platform groups clusters that cost the same to run, e.g. `onprem` and `aks`. Each has two rates: the monthly
price of 1 vCPU and of 1 GiB of **requested** quota.

| Platform | per vCPU | per GiB | Where the default comes from |
| --- | --- | --- | --- |
| `aks` | 14.84 | 1.82 | Azure's public price list, West Europe, 1-year reservation, per allocatable unit |
| `onprem` | 10.60 | 1.23 | Cost estimate of one on-prem node (total cost of ownership) with stated assumptions |

Cost of a quota on a cluster = CPU × rate per vCPU + memory GiB × rate per GiB, using that cluster's platform.
**The defaults are a starting point, not your prices:** replace them with the rates finance publishes. The page
explains both under *Where the default rates come from* (inside **Rates and rules**): the AKS method with
pay-as-you-go, 1-year and 3-year options, and every on-prem cost assumption; **Use** copies a row into the rates.
The full derivation is in the [allocation model](../docs/03-allocation-model.md#reference-rates).
**Add platform** creates another one; **Remove** clears it from the clusters that used it.

### Environments

Each environment carries three rules:

| Parameter | Meaning | prod | test | dev |
| --- | --- | --- | --- | --- |
| node failures to tolerate | How many of a cluster's largest nodes may fail with all project quota still fitting (N+1 = 1) | 1 | 0 | 0 |
| max Σ quota / allocatable | The most a cluster of this environment may hand out as project quota, as a share of its allocatable capacity | 80 % | 100 % | 150 % |
| memory limit = request × | Factor for the `limits.memory` written in the export | 1.0 | 2.0 | 2.0 |

- **Node failures to tolerate** is the N+1 rule of the [allocation model](../docs/03-allocation-model.md#headroom-and-overcommit):
  with 1, a prod cluster must still fit every project's quota after its largest node fails. It is checked
  against the real node sizes from Rancher, so it holds for small clusters too, where a flat percentage doesn't.
- **Max Σ quota** keeps room for rolling updates (surge pods) on top of that, as a share of allocatable minus
  the platform reserve. Dev may go above 100 % (overcommit) because most dev workloads sit idle.
- **Memory limit factor** follows resource standard S3 ([02](../docs/02-resource-standards.md)): memory limit equals the
  request in prod, and may be up to twice the request elsewhere.

Environment names are free text; **Add environment** creates more, e.g. `acc` (acceptance: the pre-production
stage where a release is accepted before prod, also called UAT or staging) with the same rules as test.

## Parameters: clusters

Every cluster Rancher manages is listed automatically. For each one the planner computes the **limit for
projects**: how much CPU and memory quota it can hand out in total. The page states the calculation above the
table; per resource (CPU and memory separately):

1. **Allocatable** of the schedulable nodes,
2. **minus the largest node(s):** the environment's *node failures to tolerate* largest nodes (N+1),
3. **minus the platform reserve:** what platform components (cattle-*, kube-system, monitoring, ingress …)
   request. They are in no project quota but take capacity first,
4. **and at most the environment's max %** of (allocatable − platform reserve).

The limit is the lower of 1–3 and 4. For example, 4 nodes of 4 CPU with a platform reserve of 1 CPU in prod:
16 − 4 − 1 = **11 CPU** by N+1, 80 % × (16 − 1) = 12 CPU by headroom, so the limit is 11 CPU and N+1 sets it.

| Column | Set by | Meaning |
| --- | --- | --- |
| Environment | You | Which environment rules apply (node failures, headroom limit, memory limit factor) |
| Platform | You | Which unit rates price quota on this cluster |
| Nodes | Rancher | Schedulable nodes; hover for each node's size |
| Allocatable | Rancher | CPU / GiB the scheduler can hand out on schedulable nodes, after system reservations |
| − largest node(s) | Planner | Capacity of the N largest nodes, lost when they fail (0 / 0 when the environment tolerates no failures) |
| − platform reserve | You or measured | CPU / GiB requested by platform components, see below |
| = limit for projects | Planner | The result, with the rule that sets it: *N+1* or the environment's *%* |
| Planned | Planner | Sum of the quotas you plan for all projects on this cluster |
| Use of limit | Planner | Planned as a share of the limit (the higher of CPU and memory; hover for both); *no room* when the limit is 0 |

**Platform reserve.** For the cluster the planner runs on, it is **measured**: everything the report classes as
*System* (Rancher's System project and the system namespaces). Leave the fields empty to use it. For every other
cluster the planner can't see its pods, so the reserve is **entered**: take the *System* requests from that
cluster's resource dashboard (see below), or click **Use requests now** to enter the cluster's total requests
today, a safe upper bound. A cluster with planned quota and no reserve is flagged *not set*.

**The same view on each cluster's dashboard.** The resource dashboard of every cluster shows its N+1 picture for
one node failure: a dashed line in *Cluster capacity by requests* marks the capacity left if the largest node fails,
the text under the chart spells out allocatable − largest node − platform components = room for projects, and the
tile **Room for projects (N+1)** turns red when today's project requests wouldn't fit after a node failure.

A cluster without an environment has no limit ("no env") and a cluster without a platform gives its quotas no
price; both show up as issues once a project has quota there.

## Parameters: projects

Every Rancher Project appears as a block, grouped by name across clusters: `payments` on three clusters is one
block with three rows. Rancher's own **System** and **Default** projects are hidden; tick *Show Rancher's System /
Default projects* to plan them too.

### Per project

| Field | Meaning | In the export |
| --- | --- | --- |
| Budget / month | The project's monthly budget. Leave it empty if it isn't known yet; the budget check then skips this project | `budget.monthly` |
| Cost center | The finance cost center the project is charged to | `costCenter` |
| Owners | Comma-separated contacts, e.g. project leads | `owners` |
| Planned … of … | Total cost of the planned quotas against the budget; dot green below 95 %, amber from 95 %, red above 100 % | – |

### Per cluster row

| Column | Meaning |
| --- | --- |
| Env | The cluster's environment, from the Clusters table |
| Requests now CPU / GiB | What the project's pods request today. Only known for the local cluster, where the planner runs; "—" elsewhere |
| Rancher quota now | The Project's current quota in Rancher, or "none" |
| Quota CPU | The CPU quota you plan (Rancher: *CPU Reservation*, `requests.cpu`) |
| Quota GiB | The memory quota you plan (Rancher: *Memory Reservation*, `requests.memory`) |
| Cost / month | Price of that quota with the cluster's platform rates |

### Three ways to fill a row

1. **Type the quota** into *Quota CPU* and *Quota GiB* when you know the size you want.
2. **Convert from an amount** when you start from money: enter an amount per month and the share of it that should
   go to CPU (default 50 %), then click **Convert**. CPU = amount × share ÷ rate per vCPU, memory = amount ×
   (1 − share) ÷ rate per GiB. The cluster needs a platform with rates for this.
3. **Requests +25 %** (only where current requests are known) sets the quota to today's requests plus 25 %
   headroom, a sensible starting point for surge pods during rollouts.

You can mix them: convert first, then round the numbers by hand. A row with 0 CPU and 0 GiB counts as "no
allocation" and is left out of the export.

## The checks

The issues box lists every problem as you type; the server runs the same checks again on save. Red issues break a
rule, amber ones mean the plan is incomplete. You can still save a plan with issues, so a draft is never lost.

| Issue | Level | What it means | How to fix it |
| --- | --- | --- | --- |
| *payments: planned quota costs 375.00 a month, over its budget of 250.00* | red | The priced quota of the project across all clusters exceeds its monthly budget | Lower quota on some cluster, or agree a higher budget |
| *prod-01 (prod): planned CPU quota 11.2 is above its limit for projects of 11 (allocatable minus its 1 largest node(s) and the platform reserve)* | red | All projects together plan more quota than the cluster can keep running after a node failure | Lower quotas there, move a project to another cluster, or add nodes |
| *prod-01 (prod): planned CPU quota 13 is above its limit for projects of 12 (80 % of allocatable minus the platform reserve)* | red | Planned quota leaves less room for rollouts than the environment requires | Same as above |
| *local (prod): 1 schedulable node(s) can't tolerate 1 node failure(s), so no project quota fits* | red | A prod cluster with a single node can't survive a node failure at all | Add a node, or plan this cluster as a non-prod environment |
| *prod-01: platform reserve not set, so the limit ignores what platform components request* | amber | Nothing measured or entered for platform components, so the limit is too high | Enter the *System* requests from the cluster's dashboard, or click *Use requests now* |
| *prod-01: Rancher reports no node sizes, so the node-failure rule can't be checked* | amber | Rancher gave no per-node data for the cluster; only the percentage rule is checked | Check the cluster's agent in Rancher |
| *payments: no platform set for prod-01, so its quota there has no price* | amber | The cluster has no platform, so cost and budget can't be checked | Pick a platform for the cluster in the Clusters table |
| *prod-01: no environment set, so its limit for projects (node failures, headroom) can't be checked* | amber | The cluster has quota planned but no environment | Pick an environment for the cluster |
| *newstream: no Rancher Project of that name on prod-01 yet* | amber | The plan gives quota to a project that doesn't exist on that cluster in Rancher | Create the Rancher Project first, or check the spelling of the name |
| *payments: cluster c-m-xxxxx is no longer in Rancher* | amber | The plan still holds quota for a cluster that was removed from Rancher | Set that row to 0, or remove the project's allocation there |

The tiles above the settings count the red issues: *Projects over budget* and *Clusters over their limit*.

## Saving and versions

Click **Save plan** to store your changes. Each save gets the next version number, shown in the header ("Plan
version 4, saved …").

- **One shared plan.** Everyone who opens the planner sees and edits the same plan.
- **No silent overwrites.** If someone else saved while you were editing, your save is refused with *the plan was
  changed by someone else*. Your edits stay on screen: note what you changed, reload the page, and apply your
  changes to the newer version.
- **History.** The last 30 saved versions are kept on the planner's volume
  (`planner-history/planner-v00004.json`), so an administrator can recover an earlier plan.
- **Leaving with unsaved changes** triggers the browser's "leave page?" warning.
- **Invalid input** (negative numbers, text in a number field, a line break in a cost center) is refused with a
  message naming the field.

## From plan to applied quota

**Export YAML** downloads `allocations.yaml`: one YAML document per project, in the allocation-file format of the
[technical design](../docs/04-technical-design.md#42-allocation-as-code). It exports the **last saved** plan; if you have
unsaved changes, the page asks first.

```yaml
# allocations/projects/payments.yaml
project: "payments"
costCenter: "CC-1234"
owners: ["pay-lead@corp"]
budget:
  currency: "EUR"
  monthly: 200
allocations:
  - cluster: "cra-downstream-2"
    env: "prod"
    quota:
      requests.cpu: "3"
      requests.memory: 8Gi
      limits.memory: 8Gi
```

Clusters appear by their Rancher name. `limits.memory` is the planned memory × the environment's memory limit factor
(8 GiB in prod here; a test cluster at factor 2 would get twice the request). Rows with 0 CPU and 0 GiB are left out.

The path to an applied quota:

1. Split the export into one file per project (each document starts with its file name as a comment) and commit
   them to the allocations repository under `allocations/projects/`.
2. Open a pull request. The pull request is the approval: the platform team approves, and increases above budget go
   to the budget owner ([05](../docs/05-process.md)).
3. CI validates the files: budget per project and the limit per cluster (node failures, platform reserve, headroom), the same checks the planner showed you.
4. After merge, Terraform (`rancher2` provider) sets the Rancher Project quota on each cluster.
5. The planner's *Rancher quota now* column then shows the applied values, so you can see where plan and reality
   differ.

## Worked example

The payments stream has a budget of 400 EUR a month and runs on a prod cluster `prod-01` (4 nodes of 4 CPU /
16 GiB, so 16 CPU / 64 GiB allocatable; platform components request 1 CPU / 4 GiB) and a test cluster `test-01`.
For round numbers the example uses rates of 25 per vCPU and 6.25 per GiB (not the shipped defaults).

1. **Rates and rules:** set the `onprem` rates to 25 / 6.25 for this example (prod tolerates 1 node failure).
2. **Clusters:** set `prod-01` to environment *prod*, platform *onprem*; set `test-01` to *test*, *onprem*.
3. **Platform reserve:** `prod-01` is not the planner's own cluster, so enter 1 / 4 from its dashboard's *System*
   requests. The limit for projects becomes **11 CPU / 44 GiB (N+1)**: 16 − 4 − 1 CPU and 64 − 16 − 4 GiB, both
   below the 80 % rule (12 CPU / 48 GiB).
4. **Project:** in the *payments* block, enter budget 400, cost center `CC-1234`, owner `pay-lead@corp`.
5. **prod-01 row:** payments wants 300 EUR of its budget in prod, 60 % for CPU. Enter 300 and 60, click
   **Convert**: CPU = 300 × 0.6 ÷ 25 = **7.2**, memory = 300 × 0.4 ÷ 6.25 = **19.2 GiB**.
6. **Check:** *Use of limit* shows 65 % (7.2 of 11 CPU), green. If another project already plans 4 CPU there, the
   total is 11.2 and the planner flags *prod-01 (prod): planned CPU quota 11.2 is above its limit for projects of
   11 (allocatable minus its 1 largest node(s) and the platform reserve)*. Round payments down to 7 CPU, or move
   the other project.
7. **test-01 row:** type 2 CPU and 4 GiB directly: 2 × 25 + 4 × 6.25 = 75 EUR.
8. **Budget:** planned 300 + 75 = 375 of 400 EUR, dot green.
9. **Save plan**, then **Export YAML**, and commit the payments document as `allocations/projects/payments.yaml`.
   The export writes `limits.memory` 19.2Gi for prod-01 (factor 1) and 8Gi for test-01 (factor 2).

## Capacity planner (without money)

The **capacity planner** is the same tool without rates, budgets or costs: each project simply has *this much CPU
and memory*. Open it from the local cluster's Rancher menu (**Capacity planner**) or `/capacity` next to the
dashboard. It keeps its own plan, separate from the allocation planner's.

| | Allocation planner | Capacity planner |
| --- | --- | --- |
| Per project | Budget / month, cost center, owners | **Envelope**: total CPU and memory across all clusters; owners |
| Per cluster row | Quota CPU / GiB, cost, conversion from an amount | Quota CPU / GiB, *Requests +25 %* |
| Settings | Unit rates per platform, currency, environments | Environments only (**Rules**) |
| Clusters | Environment, platform, limit for projects | Environment, limit for projects (no platform) |
| Project check | Planned cost ≤ budget | Planned CPU and memory across all clusters ≤ envelope |
| Cluster checks | Node failures, platform reserve, headroom | The same |
| Export | Allocation files with budget and cost center | Allocation files without them |

A project's envelope is optional: without one, the planner shows "(no envelope)" and only checks the clusters. The
project check reads for example *crm: planned CPU quota 1.5 across all clusters is above its envelope of 1*; the
tiles show the planned quota in CPU / GiB and *Projects over their envelope*. Everything in the sections on
clusters, checks, saving and export applies as described above, minus the money.

## Limitations and FAQ

**Why doesn't saving change the quota in Rancher?** By design. Allocations are managed as code: Git is the source of
truth and the pull request is the approval. The planner prepares those files, it doesn't bypass them.

**Why is "Requests now" empty for most clusters?** The planner runs on the local cluster and only collects that
cluster's pods. For other clusters, see each cluster's own resource dashboard (Rancher menu *Resource report*).

**What does quota cover?** CPU and memory requests (Rancher's *CPU Reservation* and *Memory Reservation*), plus the
derived memory limit. CPU limits, storage and object counts are not planned here.

**Can I plan a project that doesn't exist in Rancher yet?** Only on clusters where it exists: a project's rows are
the clusters where Rancher has a Project of that name. Create the Rancher Project first, then plan it.

**Same project name on several clusters?** Treated as one stream with one budget; each cluster gets its own quota
row. Quota itself is always per cluster.

**Does a quota include room for rollouts?** Only if you plan it. During a rolling update, surge pods count against
quota; plan 10–25 % above normal requests (the *Requests +25 %* button does this).

**Is the Git and Terraform flow in place?** It is the designed process
([ADR-0001](../docs/adr/0001-enforcement-mechanism.md), status *proposed*). Until the allocations repository and pipeline
exist, the export is the input for setting quotas by hand.

## For administrators: enabling the planner

The planner is part of the `cluster-resource-report` Helm chart ([chart README](../charts/cluster-resource-report/README.md))
and is installed once, on the Rancher local cluster:

```bash
helm upgrade --install resource-report charts/cluster-resource-report -n resource-report --create-namespace \
  --set rancher.isLocalCluster=true \
  --set persistence.enabled=true \
  --set planner.enabled=true
```

| Value | Why it's needed |
| --- | --- |
| `planner.enabled=true` | Turns the planner on (`/planner`) and adds the *Allocation planner* entry to the local cluster's Rancher menu |
| `capacityPlanner.enabled=true` | The capacity planner (`/capacity`, *Capacity planner* menu entry); can be used alone or next to the allocation planner |
| `rancher.isLocalCluster=true` | Lets it read clusters, nodes and Projects from Rancher (read-only); alternatively `rancher.localKubeconfigSecret` |
| `persistence.enabled=true` | Stores the plans on a volume (`planner.json`, `capacity-plan.json`); without it the chart refuses to install, so a pod restart can't lose the plan |

- **Permissions:** read-only (`get`/`list` on Rancher `projects`, `clusters` and `nodes`). The planner has no permission to
  change a cluster or a quota.
- **Data:** the plan is `planner.json` on the volume, with the last 30 versions in `planner-history/`. Back up that
  volume if the plan matters before it is exported.
- **Security:** saving requires the page's own request header, so another website can't make a logged-in browser
  change the plan through Rancher's proxy.
