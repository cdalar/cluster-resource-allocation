#!/usr/bin/env python3
"""Build the Kibana dashboards "Cluster resources (as-is)" and "Cluster resource report" from OpenTelemetry metrics.

Writes an importable saved-objects file (two dashboards + data view for the filter controls). "Cluster resource
report" follows the in-cluster dashboard of the cluster-resource-report chart (tiles, capacity by requests with
N+1, CPU and memory by project, Projects and Namespaces tables). Every panel is an
ES|QL Lens panel on the OTel-native data streams written by the Elasticsearch exporter (mapping mode `otel`):
`metrics-k8sclusterreceiver.otel-*` (requests, limits, allocatable, pod phase) and
`metrics-kubeletstatsreceiver.otel-*` (usage).

  ./build_dashboard.py                        # writes cluster-resources-as-is.ndjson next to this script
  ./build_dashboard.py --index 'metrics-*'    # other index pattern
  ./build_dashboard.py --no-project           # collector without the Rancher Project attribute
  ./build_dashboard.py --rancher-projects projects.json
      # Project names and System projects from `kubectl get projects.management.cattle.io -A -o json`
      # on the Rancher local cluster (without it the report shows Project IDs)
  ./build_dashboard.py --check http://localhost:9200 --window 1h
      # runs every panel query against Elasticsearch (API key in $ES_API_KEY) and prints the first rows

Standard library only, like the rest of the tools in this repository.
"""

import argparse
import base64
import datetime
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

DASHBOARD_ID = "cra-cluster-resources-as-is"
REPORT_ID = "cra-cluster-resource-report"
DATA_VIEW_ID = "cra-otel-metrics"
ADHOC_ID = "cra-otel-metrics-esql"
DEFAULT_INDEX = "metrics-*.otel-*"
BUCKETS = 60  # time buckets per range; bigger = finer but heavier queries

# Field names (OTel-native mapping). Change here if the central Elastic uses other attribute names.
F = {
    "cluster": "resource.attributes.k8s.cluster.name",
    "project": "resource.attributes.rancher.project.id",
    "namespace": "resource.attributes.k8s.namespace.name",
    "node": "resource.attributes.k8s.node.name",
    "pod_uid": "resource.attributes.k8s.pod.uid",
    "pod": "resource.attributes.k8s.pod.name",
    "container": "resource.attributes.k8s.container.name",
    "alloc_cpu": "metrics.k8s.node.allocatable_cpu",
    "alloc_mem": "metrics.k8s.node.allocatable_memory",
    "req_cpu": "metrics.k8s.container.cpu_request",
    "req_mem": "metrics.k8s.container.memory_request",
    "lim_cpu": "metrics.k8s.container.cpu_limit",
    "lim_mem": "metrics.k8s.container.memory_limit",
    "restarts": "metrics.k8s.container.restarts",
    "use_cpu": "metrics.k8s.pod.cpu.usage",
    "use_mem": "metrics.k8s.pod.memory.working_set",
    "node_use_cpu": "metrics.k8s.node.cpu.usage",
    "node_use_mem": "metrics.k8s.node.memory.working_set",
    "phase": "metrics.k8s.pod.phase",
    "ns_uid": "resource.attributes.k8s.namespace.uid",
    "ns_phase": "metrics.k8s.namespace.phase",
    "rq_uid": "resource.attributes.k8s.resourcequota.uid",
    "rq_hard": "metrics.k8s.resource_quota.hard_limit",
    "hpa_uid": "resource.attributes.k8s.hpa.uid",
    "hpa_max": "metrics.k8s.hpa.max_replicas",
}
GIB = "1073741824.0"  # a double, so byte sums (longs) are not integer-divided
NO_PROJECT = '"(no project)"'


def project_expr():
    """Rancher Project ID, or a constant when the data has no project attribute (--no-project):
    ES|QL rejects a query that names a field missing from every index."""
    return NO_PROJECT if F["project"] is None else f"COALESCE({F['project']}, {NO_PROJECT})"


def q(text):
    """Substitute {field} placeholders and tidy whitespace."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines).format(**{**F, "project": project_expr()}, BUCKETS=BUCKETS, GIB=GIB,
                                    category=category_expr(), pname=pname_expr())


# Common first stage: one row per time bucket and entity (container, pod or node), holding that entity's
# value in the bucket. Each OTel metric unit is a separate document, so values are aggregated per entity
# before they are summed; summing raw documents would count every scrape.
ENTITY_STAGE = """
| EVAL cluster = COALESCE({cluster}, "(unknown)"),
       project = {project},
       namespace = {namespace},
       entity = COALESCE({pod_uid}, {node}),
       container = COALESCE({container}, "")
| STATS alloc_cpu = MAX({alloc_cpu}), alloc_mem = MAX({alloc_mem}),
        req_cpu = MAX({req_cpu}), req_mem = MAX({req_mem}),
        lim_cpu = MAX({lim_cpu}), lim_mem = MAX({lim_mem}),
        use_cpu = AVG({use_cpu}), use_mem = AVG({use_mem})
  BY t = BUCKET(@timestamp, {BUCKETS}, ?_tstart, ?_tend), cluster, project, namespace, entity, container"""

WHERE_ALL = """
| WHERE {alloc_cpu} IS NOT NULL OR {alloc_mem} IS NOT NULL
     OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {lim_cpu} IS NOT NULL OR {lim_mem} IS NOT NULL
     OR {use_cpu} IS NOT NULL OR {use_mem} IS NOT NULL"""

WHERE_WORKLOAD = """
| WHERE {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {lim_cpu} IS NOT NULL OR {lim_mem} IS NOT NULL
     OR {use_cpu} IS NOT NULL OR {use_mem} IS NOT NULL"""

# Per-bucket sums per cluster, then averaged over the buckets.
CLUSTER_BUCKETS = """
| STATS alloc_cpu = SUM(alloc_cpu), alloc_mem = SUM(alloc_mem), largest_cpu = MAX(alloc_cpu),
        largest_mem = MAX(alloc_mem), nodes = COUNT(alloc_cpu),
        req_cpu = SUM(req_cpu), req_mem = SUM(req_mem), use_cpu = SUM(use_cpu), use_mem = SUM(use_mem)
  BY t, cluster"""


def group_table(key_cols):
    """Requests, limits and usage per key (e.g. cluster, project) over the time range."""
    keys = ", ".join(key_cols)
    return f"""
| STATS req_cpu = SUM(req_cpu), req_mem = SUM(req_mem), lim_cpu = SUM(lim_cpu), lim_mem = SUM(lim_mem),
        use_cpu = SUM(use_cpu), use_mem = SUM(use_mem), namespaces = VALUES(namespace)
  BY t, {keys}
| STATS rc = AVG(req_cpu), rc_max = MAX(req_cpu), lc = AVG(lim_cpu), uc = AVG(use_cpu),
        uc95 = PERCENTILE(use_cpu, 95),
        rm = AVG(req_mem), rm_max = MAX(req_mem), lm = AVG(lim_mem), um = AVG(use_mem),
        um95 = PERCENTILE(use_mem, 95), ns = VALUES(namespaces)
  BY {keys}
| EVAL `CPU req (avg)` = ROUND(rc, 2), `CPU req (peak)` = ROUND(rc_max, 2), `CPU limits` = ROUND(lc, 2),
       `CPU used (avg)` = ROUND(uc, 3), `CPU used (P95)` = ROUND(uc95, 3),
       `CPU efficiency %` = ROUND(uc95 / rc * 100, 0),
       `Mem req GiB (avg)` = ROUND(rm / {{GIB}}, 2), `Mem req GiB (peak)` = ROUND(rm_max / {{GIB}}, 2),
       `Mem limits GiB` = ROUND(lm / {{GIB}}, 2), `Mem used GiB (avg)` = ROUND(um / {{GIB}}, 2),
       `Mem used GiB (P95)` = ROUND(um95 / {{GIB}}, 2), `Mem efficiency %` = ROUND(um95 / rm * 100, 0)"""


PANELS = []  # "Cluster resources (as-is)", filled by panel() below, in layout order
REPORT_PANELS = []  # "Cluster resource report"
_TARGET = [PANELS]


def panel(kind, title, esql, grid, **opts):
    _TARGET[0].append({"kind": kind, "title": title, "esql": esql, "grid": grid, **opts})


def build_panels(index):
    src = f"FROM {index}"

    panel("markdown", "About", None, (0, 0, 48, 5), markdown=(
        "**As-is view of cluster resources**: allocatable, requested (what the scheduler reserves), limited "
        "and actually used CPU and memory per cluster, Rancher Project and namespace, from the OpenTelemetry "
        "metrics in Elastic. No quotas exist yet: this is the baseline for the allocation model.\n\n"
        "Averages and peaks are over the selected time range (in ~60 buckets). *Efficiency* = P95 usage / "
        "average requests. *(no project)* = namespace without the `field.cattle.io/projectId` label "
        "(system or unassigned). Filter with the controls above. "
        "Docs: `guides/kibana-dashboard.md` in cluster-resource-allocation."))

    # The value column name is shown under the tile title.
    tiles = [
        ("CPU requested", "% of allocatable", "ROUND(AVG(req_cpu) / AVG(alloc_cpu) * 100, 1)"),
        ("Memory requested", "% of allocatable", "ROUND(AVG(req_mem) / AVG(alloc_mem) * 100, 1)"),
        ("CPU used", "% of requested", "ROUND(AVG(use_cpu) / AVG(req_cpu) * 100, 1)"),
        ("Memory used", "% of requested", "ROUND(AVG(use_mem) / AVG(req_mem) * 100, 1)"),
    ]
    for i, (title, name, expr) in enumerate(tiles):
        panel("metric", title, q(src + WHERE_ALL + ENTITY_STAGE + f"""
| STATS alloc_cpu = SUM(alloc_cpu), alloc_mem = SUM(alloc_mem), req_cpu = SUM(req_cpu),
        req_mem = SUM(req_mem), use_cpu = SUM(use_cpu), use_mem = SUM(use_mem) BY t
| STATS `{name}` = {expr}"""), (i * 8, 5, 8, 6), value=name)

    panel("metric", "Pods pending (last 10 min)", q(src + """
| WHERE {phase} IS NOT NULL AND @timestamp >= NOW() - 10 minutes
| STATS phase = MAX({phase}) BY {cluster}, {pod_uid}
| STATS pods = COUNT(*) WHERE phase == 1"""), (32, 5, 8, 6), value="pods")

    panel("metric", "Missing requests", q(src + """
| WHERE {restarts} IS NOT NULL OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
| STATS req_cpu = MAX({req_cpu}), req_mem = MAX({req_mem}) BY {cluster}, {pod_uid}, {container}
| STATS `containers without CPU or memory request` = COUNT(*) WHERE req_cpu IS NULL OR req_mem IS NULL"""),
          (40, 5, 8, 6), value="containers without CPU or memory request")

    for i, (res, unit, div) in enumerate([("cpu", "cores", ""), ("mem", "GiB", " / {GIB}")]):
        label = "CPU" if res == "cpu" else "Memory"
        panel("line", f"{label}: allocatable vs requested vs used ({unit})", q(src + WHERE_ALL + ENTITY_STAGE + f"""
| STATS alloc = SUM(alloc_{res}), req = SUM(req_{res}), lim = SUM(lim_{res}), used = SUM(use_{res}) BY t
| EVAL Allocatable = ROUND(alloc{div}, 2), Requested = ROUND(req{div}, 2), Limits = ROUND(lim{div}, 2),
       Used = ROUND(used{div}, 2)
| KEEP t, Allocatable, Requested, Limits, Used
| SORT t"""), (i * 24, 11, 24, 12), x="t", y=["Allocatable", "Requested", "Limits", "Used"])

    panel("table", "Clusters (averages over the time range)", q(src + WHERE_ALL + ENTITY_STAGE + CLUSTER_BUCKETS + """
| STATS Nodes = MAX(nodes),
        `CPU allocatable` = ROUND(AVG(alloc_cpu), 1), `CPU requested` = ROUND(AVG(req_cpu), 2),
        `CPU used` = ROUND(AVG(use_cpu), 2),
        `Mem allocatable GiB` = ROUND(AVG(alloc_mem) / {GIB}, 1), `Mem requested GiB` = ROUND(AVG(req_mem) / {GIB}, 1),
        `Mem used GiB` = ROUND(AVG(use_mem) / {GIB}, 1),
        n1_cpu = AVG(alloc_cpu - largest_cpu - req_cpu), n1_mem = AVG(alloc_mem - largest_mem - req_mem)
  BY Cluster = cluster
| EVAL `CPU req %` = ROUND(`CPU requested` / `CPU allocatable` * 100, 0),
       `Mem req %` = ROUND(`Mem requested GiB` / `Mem allocatable GiB` * 100, 0),
       `CPU free N+1` = ROUND(n1_cpu, 1), `Mem free GiB N+1` = ROUND(n1_mem / {GIB}, 1)
| KEEP Cluster, Nodes, `CPU allocatable`, `CPU requested`, `CPU req %`, `CPU used`, `CPU free N+1`,
       `Mem allocatable GiB`, `Mem requested GiB`, `Mem req %`, `Mem used GiB`, `Mem free GiB N+1`
| SORT Cluster"""), (0, 23, 48, 8))

    if F["project"] is not None:
        panel("table", "Rancher Projects", q(src + WHERE_WORKLOAD + ENTITY_STAGE + group_table(["cluster", "project"]) + """
| EVAL Namespaces = MV_COUNT(MV_DEDUPE(ns))
| RENAME cluster AS Cluster, project AS Project
| KEEP Cluster, Project, Namespaces, `CPU req (avg)`, `CPU req (peak)`, `CPU limits`, `CPU used (avg)`,
       `CPU used (P95)`, `CPU efficiency %`, `Mem req GiB (avg)`, `Mem req GiB (peak)`, `Mem limits GiB`,
       `Mem used GiB (avg)`, `Mem used GiB (P95)`, `Mem efficiency %`
| SORT `CPU req (avg)` DESC NULLS LAST
| LIMIT 500"""), (0, 31, 48, 12))

    panel("bar", "Top 20 namespaces: CPU requested vs used (cores)", q(src + WHERE_WORKLOAD + ENTITY_STAGE + """
| STATS req = SUM(req_cpu), used = SUM(use_cpu) BY t, cluster, namespace
| STATS Requested = ROUND(AVG(req), 3), `Used (P95)` = ROUND(PERCENTILE(used, 95), 3) BY cluster, namespace
| EVAL Namespace = CONCAT(cluster, " / ", namespace)
| SORT Requested DESC NULLS LAST
| LIMIT 20
| KEEP Namespace, Requested, `Used (P95)`"""), (0, 43, 24, 14), x="Namespace", y=["Requested", "Used (P95)"])

    panel("bar", "Top 20 namespaces: memory requested vs used (GiB)", q(src + WHERE_WORKLOAD + ENTITY_STAGE + """
| STATS req = SUM(req_mem), used = SUM(use_mem) BY t, cluster, namespace
| STATS Requested = ROUND(AVG(req) / {GIB}, 2), `Used (P95)` = ROUND(PERCENTILE(used, 95) / {GIB}, 2)
  BY cluster, namespace
| EVAL Namespace = CONCAT(cluster, " / ", namespace)
| SORT Requested DESC NULLS LAST
| LIMIT 20
| KEEP Namespace, Requested, `Used (P95)`"""), (24, 43, 24, 14), x="Namespace", y=["Requested", "Used (P95)"])

    panel("table", "Namespaces", q(src + WHERE_WORKLOAD + ENTITY_STAGE + group_table(["cluster", "project", "namespace"]) + """
| RENAME cluster AS Cluster, project AS Project, namespace AS Namespace
| KEEP Cluster, Project, Namespace, `CPU req (avg)`, `CPU req (peak)`, `CPU limits`, `CPU used (avg)`,
       `CPU used (P95)`, `CPU efficiency %`, `Mem req GiB (avg)`, `Mem req GiB (peak)`, `Mem limits GiB`,
       `Mem used GiB (avg)`, `Mem used GiB (P95)`, `Mem efficiency %`
| SORT `CPU req (avg)` DESC NULLS LAST
| LIMIT 1000"""), (0, 57, 48, 14))

    panel("table", "Resource standards per namespace (containers seen in the time range)", q(src + """
| WHERE {restarts} IS NOT NULL OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {lim_cpu} IS NOT NULL OR {lim_mem} IS NOT NULL
| EVAL any_set = COALESCE({req_cpu}, {req_mem}, {lim_cpu}, {lim_mem})
| STATS req_cpu = MAX({req_cpu}), req_mem = MAX({req_mem}), lim_cpu = MAX({lim_cpu}), lim_mem = MAX({lim_mem}),
        any_set = MAX(any_set)
  BY cluster = {cluster}, project = {project}, namespace = {namespace},
     pod = {pod_uid}, container = {container}
| EVAL no_cpu_req = CASE(req_cpu IS NULL, 1, 0), no_mem_req = CASE(req_mem IS NULL, 1, 0),
       no_mem_lim = CASE(lim_mem IS NULL, 1, 0), cpu_lim = CASE(lim_cpu IS NULL, 0, 1),
       any_set = CASE(any_set IS NULL, 0, 1)
| STATS containers = COUNT(*), no_cpu_req = SUM(no_cpu_req), no_mem_req = SUM(no_mem_req),
        no_mem_lim = SUM(no_mem_lim), cpu_lim = SUM(cpu_lim), any_set = MAX(any_set)
  BY cluster, project, namespace, pod
| STATS Pods = COUNT(*), Containers = SUM(containers), `No CPU request` = SUM(no_cpu_req),
        `No memory request` = SUM(no_mem_req), `No memory limit` = SUM(no_mem_lim),
        `CPU limit set` = SUM(cpu_lim), `BestEffort pods` = SUM(CASE(any_set == 0, 1, 0))
  BY Cluster = cluster, Project = project, Namespace = namespace
| KEEP Cluster, Project, Namespace, Pods, Containers, `No CPU request`, `No memory request`, `No memory limit`,
       `CPU limit set`, `BestEffort pods`
| SORT `No CPU request` DESC, `No memory request` DESC, Namespace
| LIMIT 1000"""), (0, 71, 30, 14))

    panel("table", "Pending pods (last 10 min)", q(src + """
| WHERE (({phase} IS NOT NULL AND @timestamp >= NOW() - 10 minutes) OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL)
| STATS phase = MAX({phase}), req_cpu = MAX({req_cpu}), req_mem = MAX({req_mem})
  BY cluster = {cluster}, namespace = {namespace}, pod = {pod}, uid = {pod_uid}, container = {container}
| STATS phase = MAX(phase), `CPU req` = SUM(req_cpu), `Mem req GiB` = ROUND(SUM(req_mem) / {GIB}, 2)
  BY Cluster = cluster, Namespace = namespace, Pod = pod, uid
| WHERE phase == 1
| KEEP Cluster, Namespace, Pod, `CPU req`, `Mem req GiB`
| SORT `CPU req` DESC NULLS LAST
| LIMIT 200"""), (30, 71, 18, 14))

    panel("table", "Nodes (averages over the time range)", q(src + """
| WHERE {alloc_cpu} IS NOT NULL OR {alloc_mem} IS NOT NULL OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {node_use_cpu} IS NOT NULL OR {node_use_mem} IS NOT NULL
| STATS alloc_cpu = MAX({alloc_cpu}), alloc_mem = MAX({alloc_mem}), req_cpu = MAX({req_cpu}),
        req_mem = MAX({req_mem}), use_cpu = AVG({node_use_cpu}), use_mem = AVG({node_use_mem})
  BY t = BUCKET(@timestamp, {BUCKETS}, ?_tstart, ?_tend), cluster = {cluster}, node = {node},
     pod = {pod_uid}, container = {container}
| WHERE node IS NOT NULL AND node != ""
| STATS alloc_cpu = SUM(alloc_cpu), alloc_mem = SUM(alloc_mem), req_cpu = SUM(req_cpu), req_mem = SUM(req_mem),
        use_cpu = SUM(use_cpu), use_mem = SUM(use_mem)
  BY t, cluster, node
| STATS `CPU allocatable` = ROUND(AVG(alloc_cpu), 1), `CPU requested` = ROUND(AVG(req_cpu), 2),
        `CPU used` = ROUND(AVG(use_cpu), 2),
        `Mem allocatable GiB` = ROUND(AVG(alloc_mem) / {GIB}, 1), `Mem requested GiB` = ROUND(AVG(req_mem) / {GIB}, 1),
        `Mem used GiB` = ROUND(AVG(use_mem) / {GIB}, 1)
  BY Cluster = cluster, Node = node
| EVAL `CPU req %` = ROUND(`CPU requested` / `CPU allocatable` * 100, 0),
       `Mem req %` = ROUND(`Mem requested GiB` / `Mem allocatable GiB` * 100, 0)
| KEEP Cluster, Node, `CPU allocatable`, `CPU requested`, `CPU req %`, `CPU used`,
       `Mem allocatable GiB`, `Mem requested GiB`, `Mem req %`, `Mem used GiB`
| SORT Cluster, Node
| LIMIT 1000"""), (0, 85, 48, 10))


# ---- "Cluster resource report": the in-cluster dashboard of the chart, on the OTel metrics ----

# Same classification as discovery/discover.py (test_build_dashboard checks it): System = platform namespaces by
# name, or every namespace of a Rancher Project whose name matches SYSTEM_PROJECT_LUCENE; Unassigned = no Project.
# Lucene regular expressions (RLIKE) are anchored and have no ^ $ or (?i).
SYSTEM_NS_LUCENE = (
    "kube-.*|cattle-.*|fleet-.*|rancher-.*|calico-.*|tigera-.*|kyverno|cert-manager|otel|monitoring|"
    "local|p-[a-z0-9]{5}|local-p-[a-z0-9]{5}|c-[a-z0-9-]+|u-[a-z0-9]+|user-[a-z0-9]+|"
    "rook-.*|openebs.*|csi-.*|.*-csi.*|.*provisioner.*|"
    ".*storage.*|.*system.*|.*ingress.*"
)
SYSTEM_PROJECT_LUCENE = "system|.*platform.*"  # matched against the lower-case Project name
PROJECT_NAMES = {}  # Project ID -> display name, from --rancher-projects
NOW_WINDOW = "10 minutes"  # "now" values (pods, pending, standards) = the last 10 minutes of the time range


def load_rancher_projects(path):
    """Project ID -> display name from `kubectl get projects.management.cattle.io -A -o json`."""
    with open(path) as f:
        items = json.load(f).get("items", [])
    names = {}
    for it in items:
        pid, name = it["metadata"]["name"], (it.get("spec") or {}).get("displayName") or it["metadata"]["name"]
        if names.get(pid, name) != name:
            print(f"warning: Project ID {pid} has two names ({names[pid]}, {name}); using {name}", file=sys.stderr)
        names[pid] = name
    return names


def esql_str(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pname_expr():
    """Project label: its Rancher name when known, else the ID; "(no project)" for namespaces without one."""
    if F["project"] is None:
        return NO_PROJECT
    if not PROJECT_NAMES:
        return "project"
    cases = ", ".join(f"project == {esql_str(k)}, {esql_str(v)}" for k, v in sorted(PROJECT_NAMES.items()))
    return f"CASE({cases}, project)"


def system_ns_expr():
    """namespace matches SYSTEM_NS_LUCENE. One RLIKE over the whole expression is too complex for Lucene's automaton
    (too_complex_to_determinize), so plain wildcards become LIKE and only the rest stays RLIKE."""
    terms = []
    for alt in SYSTEM_NS_LUCENE.split("|"):
        if re.fullmatch(r"[a-z0-9-]*(\.\*[a-z0-9-]*)*", alt):
            terms.append(f"namespace LIKE {esql_str(alt.replace('.*', '*'))}")
        else:
            terms.append(f"namespace RLIKE {esql_str(alt)}")
    return "(" + " OR ".join(terms) + ")"


def category_expr():
    """System / Tenant / Unassigned, as in the report. Without the Project attribute: System or Workload."""
    sys_ns = system_ns_expr()
    if F["project"] is None:
        return f'CASE({sys_ns}, "System", "Workload")'
    if PROJECT_NAMES:
        sys_ns += f' OR TO_LOWER(pname) RLIKE "{SYSTEM_PROJECT_LUCENE}"'
    return f'CASE({sys_ns}, "System", project == {NO_PROJECT}, "Unassigned", "Tenant")'


# One row per time bucket and pod (or node, quota, HPA, namespace): requests and limits of running pods, usage,
# and "now" values from the last minutes of the range. Succeeded/failed pods don't reserve anything; pending pods
# are counted apart, as in the report.
REPORT_POD_STAGE = """
| WHERE {alloc_cpu} IS NOT NULL OR {alloc_mem} IS NOT NULL
     OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL OR {lim_cpu} IS NOT NULL OR {lim_mem} IS NOT NULL
     OR {use_cpu} IS NOT NULL OR {use_mem} IS NOT NULL OR {phase} IS NOT NULL OR {restarts} IS NOT NULL
     OR {rq_hard} IS NOT NULL OR {hpa_max} IS NOT NULL OR {ns_phase} IS NOT NULL
| EVAL cluster = COALESCE({cluster}, "(unknown)"), project = {project}, namespace = COALESCE({namespace}, ""),
       entity = COALESCE({pod_uid}, {rq_uid}, {hpa_uid}, {ns_uid}, {node}), container = COALESCE({container}, ""),
       recent = @timestamp >= TO_DATETIME(?_tend) - NOW_WINDOW
| EVAL phase_now = CASE(recent, {phase}), c_now = CASE(recent AND {restarts} IS NOT NULL, 1),
       rq_now = CASE(recent AND {rq_hard} IS NOT NULL, 1), hpa_now = CASE(recent AND {hpa_max} IS NOT NULL, 1),
       ns_now = CASE(recent AND {ns_phase} IS NOT NULL, 1)
| STATS alloc_cpu = MAX({alloc_cpu}), alloc_mem = MAX({alloc_mem}),
        req_cpu = MAX({req_cpu}), req_mem = MAX({req_mem}), lim_cpu = MAX({lim_cpu}), lim_mem = MAX({lim_mem}),
        use_cpu = AVG({use_cpu}), use_mem = AVG({use_mem}), phase = MAX({phase}), phase_now = MAX(phase_now),
        c_now = MAX(c_now), rq_now = MAX(rq_now), hpa_now = MAX(hpa_now), ns_now = MAX(ns_now)
  BY t = BUCKET(@timestamp, {BUCKETS}, ?_tstart, ?_tend), cluster, project, namespace, entity, container
| EVAL is_c = CASE(c_now == 1 AND container != "", 1, 0),
       no_cpu = CASE(c_now == 1 AND container != "" AND req_cpu IS NULL, 1, 0),
       no_mem_lim = CASE(c_now == 1 AND container != "" AND lim_mem IS NULL, 1, 0),
       any_set = CASE(container != "" AND COALESCE(req_cpu, req_mem, lim_cpu, lim_mem) IS NOT NULL, 1, 0)
| STATS alloc_cpu = MAX(alloc_cpu), alloc_mem = MAX(alloc_mem), req_cpu = SUM(req_cpu), req_mem = SUM(req_mem),
        lim_mem = SUM(lim_mem), use_cpu = SUM(use_cpu), use_mem = SUM(use_mem), phase = MAX(phase),
        phase_now = MAX(phase_now), containers = SUM(is_c), no_cpu = SUM(no_cpu), no_mem_lim = SUM(no_mem_lim),
        any_set = MAX(any_set), rq = MAX(rq_now), hpa = MAX(hpa_now), ns_now = MAX(ns_now)
  BY t, cluster, project, namespace, entity
| EVAL req_cpu = TO_DOUBLE(req_cpu), req_mem = TO_DOUBLE(req_mem), lim_mem = TO_DOUBLE(lim_mem),
       run = CASE(phase == 1 OR phase >= 3, 0.0, 1.0)
| EVAL rc = req_cpu * run, rm = req_mem * run, lm = lim_mem * run,
       pend_cpu = CASE(phase_now == 1, req_cpu, 0.0), pend_mem = CASE(phase_now == 1, req_mem, 0.0),
       pods = CASE(phase_now == 2, 1, 0), pending = CASE(phase_now == 1, 1, 0),
       besteffort = CASE(phase_now == 2 AND containers > 0 AND any_set == 0, 1, 0),
       pname = {pname}
| EVAL category = {category}""".replace("NOW_WINDOW", NOW_WINDOW)

# Per cluster, averaged over the buckets; "now" values are the maximum over the buckets (only the last ones have any).
REPORT_CLUSTER = """
| EVAL rc_t = CASE(category == "System", 0.0, rc), rm_t = CASE(category == "System", 0.0, rm),
       rc_s = CASE(category == "System", rc, 0.0), rm_s = CASE(category == "System", rm, 0.0),
       rc_u = CASE(category == "Unassigned", rc, 0.0), rm_u = CASE(category == "Unassigned", rm, 0.0),
       ns_u = CASE(category == "Unassigned" AND ns_now == 1, namespace)
| STATS alloc_cpu = SUM(alloc_cpu), alloc_mem = SUM(alloc_mem), largest_cpu = MAX(alloc_cpu),
        largest_mem = MAX(alloc_mem), nodes = COUNT(alloc_cpu), rc = SUM(rc), rm = SUM(rm),
        rc_t = SUM(rc_t), rm_t = SUM(rm_t), rc_s = SUM(rc_s), rm_s = SUM(rm_s), rc_u = SUM(rc_u), rm_u = SUM(rm_u),
        use_cpu = SUM(use_cpu), use_mem = SUM(use_mem), pend_cpu = SUM(pend_cpu), pend_mem = SUM(pend_mem),
        containers = SUM(containers), no_cpu = SUM(no_cpu), no_mem_lim = SUM(no_mem_lim),
        ns_u = COUNT_DISTINCT(ns_u)
  BY t, cluster
| STATS alloc_cpu = AVG(alloc_cpu), alloc_mem = AVG(alloc_mem), largest_cpu = AVG(largest_cpu),
        largest_mem = AVG(largest_mem), nodes = MAX(nodes), rc = AVG(rc), rm = AVG(rm),
        rc_t = AVG(rc_t), rm_t = AVG(rm_t), rc_s = AVG(rc_s), rm_s = AVG(rm_s), rc_u = AVG(rc_u), rm_u = AVG(rm_u),
        uc = PERCENTILE(use_cpu, 95), um = PERCENTILE(use_mem, 95), pend_cpu = MAX(pend_cpu),
        pend_mem = MAX(pend_mem), containers = MAX(containers), no_cpu = MAX(no_cpu),
        no_mem_lim = MAX(no_mem_lim), ns_u = MAX(ns_u)
  BY cluster
| EVAL room_cpu = GREATEST(alloc_cpu - largest_cpu - rc_s, 0), room_mem = GREATEST(alloc_mem - largest_mem - rm_s, 0)"""

# CPU in vCPU, memory in GiB, one row per cluster and resource.
REPORT_BY_RESOURCE = """
| EVAL res = ["CPU", "Memory"]
| MV_EXPAND res
| EVAL k = CASE(res == "CPU", 1.0, 1.0 / {GIB}),
       a = CASE(res == "CPU", alloc_cpu, alloc_mem) * k, lg = CASE(res == "CPU", largest_cpu, largest_mem) * k,
       tn = CASE(res == "CPU", rc_t - rc_u, rm_t - rm_u) * k, sy = CASE(res == "CPU", rc_s, rm_s) * k,
       un = CASE(res == "CPU", rc_u, rm_u) * k, room = CASE(res == "CPU", room_cpu, room_mem) * k"""


def report_group(keys, extra=""):
    """Namespace or project rows: averages and peaks over the buckets, P95 usage, "now" counts."""
    return f"""
| STATS rc = SUM(rc), rm = SUM(rm), lm = SUM(lm), use_cpu = SUM(use_cpu), use_mem = SUM(use_mem),
        pods = SUM(pods), pending = SUM(pending), no_cpu = SUM(no_cpu), no_mem_lim = SUM(no_mem_lim),
        besteffort = SUM(besteffort), rq = SUM(rq), hpa = SUM(hpa), namespaces = VALUES(namespace)
  BY t, {keys}
| STATS rc_avg = AVG(rc), rc_peak = MAX(rc), uc = PERCENTILE(use_cpu, 95), rm_avg = AVG(rm), lm_avg = AVG(lm),
        um = PERCENTILE(use_mem, 95), pods = MAX(pods), pending = MAX(pending), no_cpu = MAX(no_cpu),
        no_mem_lim = MAX(no_mem_lim), besteffort = MAX(besteffort), rq = MAX(rq), hpa = MAX(hpa),
        ns = VALUES(namespaces)
  BY {keys}
| EVAL `CPU req` = ROUND(rc_avg, 2), `CPU P95` = ROUND(uc, 2), `CPU eff. %` = ROUND(uc / rc_avg * 100, 0),
       `CPU req peak` = ROUND(rc_peak, 2), `Mem req GiB` = ROUND(rm_avg / {{GIB}}, 2),
       `Mem P95 GiB` = ROUND(um / {{GIB}}, 2), `Mem eff. %` = ROUND(um / rm_avg * 100, 0),
       `Mem limit GiB` = ROUND(lm_avg / {{GIB}}, 2), Pods = pods, Pending = pending, `No CPU req` = no_cpu,
       `No mem limit` = no_mem_lim, BestEffort = besteffort{extra}"""


REPORT_COLUMNS = ("`CPU req`, `CPU P95`, `CPU eff. %`, `CPU req peak`, `Mem req GiB`, `Mem P95 GiB`, `Mem eff. %`, "
                  "`Mem limit GiB`, Pods, Pending, `No CPU req`, `No mem limit`, BestEffort")

COLORS = {"Tenant": "#1d4ed8", "System": "#94a3b8", "Unassigned": "#f59e0b", "Free (N+1)": "#e2e8f0",
          "N+1 reserve": "#fca5a5", "Requested": "#1d4ed8", "Used (P95)": "#0d9488"}


def build_report_panels(index):
    _TARGET[0] = REPORT_PANELS
    try:
        _build_report_panels(f"FROM {index}")
    finally:
        _TARGET[0] = PANELS


def _build_report_panels(src):
    base = src + REPORT_POD_STAGE
    names = ("Project names from Rancher." if PROJECT_NAMES else
             "Projects show their ID; regenerate with `--rancher-projects` for names and System projects.")
    panel("markdown", "About", None, (0, 0, 48, 4), markdown=(
        "**Cluster resource report**: the in-cluster dashboard of the cluster-resource-report chart, on the "
        "OpenTelemetry metrics. *Requested* = requests of running pods (averaged over the time range); *used* = "
        f"P95 over the time range; tiles marked *now* and the pod and standards columns = the last {NOW_WINDOW} "
        "of the range. *N+1*: allocatable − largest node − platform (System) requests = room for projects. "
        f"{names} Docs: `guides/kibana-dashboard.md`."))

    summary = base + REPORT_CLUSTER
    # (label, value, secondary label, secondary value, bar): the label names the value, so the panel title is
    # hidden; % tiles get a bar to 100 % coloured by the allocation rule (80 % prod ceiling, over 100 % overcommitted).
    tiles = [
        ("Nodes", "SUM(nodes)", "clusters", "COUNT(*)", False),
        ("Allocatable vCPU", "ROUND(SUM(alloc_cpu), 1)", "GiB memory", "ROUND(SUM(alloc_mem) / {GIB}, 1)", False),
        ("CPU requested (% of allocatable)", "ROUND(SUM(rc) / SUM(alloc_cpu) * 100, 0)", "vCPU",
         "ROUND(SUM(rc), 1)", True),
        ("Memory requested (% of allocatable)", "ROUND(SUM(rm) / SUM(alloc_mem) * 100, 0)", "GiB",
         "ROUND(SUM(rm) / {GIB}, 1)", True),
        ("Room for projects (N+1), vCPU", "ROUND(SUM(room_cpu), 1)", "left after project requests",
         "ROUND(SUM(room_cpu - rc_t), 1)", False),
        ("Used (P95), vCPU", "ROUND(SUM(uc), 2)", "GiB memory", "ROUND(SUM(um) / {GIB}, 1)", False),
        ("Containers without CPU request (now)", "SUM(no_cpu)", "without memory limit", "SUM(no_mem_lim)", False),
        ("Pending requests (now), vCPU", "ROUND(SUM(pend_cpu), 2)", "GiB", "ROUND(SUM(pend_mem) / {GIB}, 2)", False),
    ]
    if F["project"] is not None:
        tiles.append(("Unassigned namespaces (now)", "SUM(ns_u)", "not in any Rancher Project", "0", False))
    widths = [5, 5, 6, 6, 5, 5, 6, 5, 5][:len(tiles)]
    widths[-1] += 48 - sum(widths)
    x = 0
    for (label, expr, sec_name, sec_expr, bar), w in zip(tiles, widths):
        stats = f"`{label}` = {expr}, `{sec_name}` = {sec_expr}" + ("\n| EVAL max = 100" if bar else "")
        if sec_expr == "0":  # label only
            stats = f"`{label}` = {expr}"
        panel("metric", label, q(summary + f"\n| STATS {stats}"), (x, 4, w, 6), value=label,
              secondary=None if sec_expr == "0" else sec_name, bar=bar, hide_title=True,
              subtitle=sec_name if sec_expr == "0" else None)
        x += w

    panel("bar_stacked", "Cluster capacity by requests (% of allocatable)", q(summary + REPORT_BY_RESOURCE + """
| EVAL used = tn + sy + un,
       Label = CONCAT(cluster, " · ", res, " (", TO_STRING(nodes), CASE(nodes == 1, " node)", " nodes)")), Tenant = ROUND(tn / a * 100, 1), System = ROUND(sy / a * 100, 1),
       Unassigned = ROUND(un / a * 100, 1), `Free (N+1)` = ROUND(GREATEST(a - lg - used, 0) / a * 100, 1),
       `N+1 reserve` = ROUND(GREATEST(a - GREATEST(a - lg, used), 0) / a * 100, 1)
| SORT cluster, res
| KEEP Label, Tenant, System, Unassigned, `Free (N+1)`, `N+1 reserve`"""), (0, 10, 26, 10), x="Label",
          y=["Tenant", "System", "Unassigned", "Free (N+1)", "N+1 reserve"])

    panel("table", "N+1: room for projects if the largest node fails (vCPU / GiB)", q(summary + REPORT_BY_RESOURCE + """
| EVAL Cluster = cluster, Resource = res, Nodes = nodes, Allocatable = ROUND(a, 1), `Largest node` = ROUND(lg, 1),
       Platform = ROUND(sy, 1), `For projects` = ROUND(room, 1), `Projects request` = ROUND(tn + un, 1),
       `Left (N+1)` = ROUND(room - tn - un, 1), Fits = CASE(room >= tn + un, "yes", "NO")
| SORT Cluster, Resource
| KEEP Cluster, Resource, Nodes, Allocatable, `Largest node`, Platform, `For projects`, `Projects request`,
       `Left (N+1)`, Fits"""), (26, 10, 22, 10))

    for i, (res, unit) in enumerate([("cpu", "vCPU"), ("mem", "GiB")]):
        label = "CPU" if res == "cpu" else "Memory"
        r, u = ("rc", "use_cpu") if res == "cpu" else ("rm", "use_mem")
        div = "" if res == "cpu" else " / {GIB}"
        panel("bar", f"{label} by project ({unit}, top 15 by requests, without System)", q(base + f"""
| WHERE category != "System"
| STATS req = SUM({r}), used = SUM({u}) BY t, cluster, pname
| STATS Requested = ROUND(AVG(req){div}, 2), `Used (P95)` = ROUND(PERCENTILE(used, 95){div}, 2) BY cluster, pname
| EVAL Project = CONCAT(cluster, " / ", pname)
| SORT Requested DESC NULLS LAST
| LIMIT 15
| KEEP Project, Requested, `Used (P95)`"""), (i * 24, 20, 24, 13), x="Project", y=["Requested", "Used (P95)"])

    if F["project"] is not None:
        panel("table", "Projects", q(base + """
| WHERE namespace != ""
""" + report_group("cluster, project, pname, category") + """
| EVAL NS = MV_COUNT(MV_DEDUPE(ns)), Cluster = cluster, Project = pname, `Project ID` = project,
       Category = category
| SORT `CPU req` DESC NULLS LAST
| LIMIT 500
| KEEP Cluster, Project, Category, NS, """ + REPORT_COLUMNS + ", `Project ID`"), (0, 33, 48, 12))

    panel("table", "Namespaces", q(base + """
| WHERE namespace != ""
""" + report_group("cluster, project, pname, category, namespace", extra=", Quota = COALESCE(rq, 0), HPA = COALESCE(hpa, 0)") + """
| EVAL Cluster = cluster, Namespace = namespace, Project = pname, Category = category
| SORT `CPU req` DESC NULLS LAST
| LIMIT 1000
| KEEP Cluster, Namespace, Project, Category, """ + REPORT_COLUMNS + ", Quota, HPA"), (0, 45, 48, 16))


# ---- Lens / dashboard saved-object structure (as built by Kibana's lens-embeddable-utils config_builder) ----

def adhoc_data_view(index):
    return {ADHOC_ID: {"id": ADHOC_ID, "title": index, "name": index, "timeFieldName": "@timestamp",
                       "sourceFilters": [], "fieldFormats": {}, "runtimeFieldMap": {}, "fieldAttrs": {},
                       "allowNoIndex": False, "allowHidden": False, "type": "esql"}}


def col(column_id, field, typ=None):
    c = {"columnId": column_id, "fieldName": field}
    if typ:
        c["meta"] = {"type": typ}
    return c


# Metric colour by value: green up to 80 %, amber to 100 %, red above (the allocation model's prod ceiling).
ALLOCATION_PALETTE = {"type": "palette", "name": "custom", "params": {
    "name": "custom", "reverse": False, "rangeType": "number", "rangeMin": 0, "rangeMax": None,
    "progression": "fixed", "continuity": "above", "maxSteps": 5, "steps": 3,
    "stops": [{"color": "#54B399", "stop": 80}, {"color": "#D6BF57", "stop": 100}, {"color": "#E7664C", "stop": 101}],
    "colorStops": [{"color": "#54B399", "stop": 0}, {"color": "#D6BF57", "stop": 80}, {"color": "#E7664C", "stop": 100}]}}


def lens_attributes(p, index):
    kind = p["kind"]
    if kind == "metric":
        columns = [col("value", p["value"], "number")]
        vis_type = "lnsMetric"
        vis = {"layerId": "layer_0", "layerType": "data", "metricAccessor": "value", "showBar": False}
        if p.get("secondary"):
            columns.append(col("secondary", p["secondary"], "number"))
            vis["secondaryMetricAccessor"] = "secondary"
        if p.get("subtitle"):
            vis["subtitle"] = p["subtitle"]
        if p.get("bar"):
            columns.append(col("max", "max", "number"))
            vis.update(maxAccessor="max", showBar=True, progressDirection="horizontal", palette=ALLOCATION_PALETTE)
    elif kind == "table":
        columns = [col(f"c{i}", n) for i, n in enumerate(table_columns(p["esql"]))]
        vis_type = "lnsDatatable"
        widths = {"Cluster": 150, "Namespace": 170, "Project": 130}
        vis = {"layerId": "layer_0", "layerType": "data",
               "columns": [{"columnId": c["columnId"], **({"width": widths[c["fieldName"]]}
                                                          if c["fieldName"] in widths else {})} for c in columns]}
    else:  # line / bar
        columns = [col("x", p["x"], "date" if kind == "line" else "string")]
        columns += [col(f"y{i}", y, "number") for i, y in enumerate(p["y"])]
        vis_type = "lnsXY"
        series = {"line": "line", "bar": "bar_horizontal", "bar_stacked": "bar_horizontal_stacked"}[kind]
        vis = {
            "legend": {"isVisible": True, "position": "bottom"},
            "valueLabels": "hide", "fittingFunction": "Linear", "preferredSeriesType": series,
            "axisTitlesVisibilitySettings": {"x": False, "yLeft": False, "yRight": False},
            "layers": [{"layerId": "layer_0", "layerType": "data", "seriesType": series, "xAccessor": "x",
                        "accessors": [f"y{i}" for i in range(len(p["y"]))],
                        "yConfig": [{"forAccessor": f"y{i}", "color": COLORS[y]}
                                    for i, y in enumerate(p["y"]) if y in COLORS]}],
        }
    layer = {"index": ADHOC_ID, "query": {"esql": p["esql"]}, "timeField": "@timestamp",
             "columns": columns, "allColumns": columns}
    return {
        "title": p["title"],
        "visualizationType": vis_type,
        "type": "lens",
        "references": [],
        "state": {
            "datasourceStates": {"textBased": {"layers": {"layer_0": layer}}},
            "internalReferences": [],
            "filters": [],
            "query": {"esql": p["esql"]},
            "visualization": vis,
            "adHocDataViews": adhoc_data_view(index),
        },
    }


def table_columns(esql):
    """Output columns of a table query: the KEEP list, else the names of the last STATS (aggregates, then BY)."""
    text = " ".join(esql.split())
    commands = [c.strip() for c in text.split("|")]
    for c in reversed(commands):
        if c.startswith("KEEP "):
            return [n.strip().strip("`") for n in split_top(c[5:])]
        if c.startswith("STATS "):
            body = c[6:]
            by = ""
            m = re.search(r"\sBY\s", body)
            if m:
                body, by = body[:m.start()], body[m.end():]
            aggs = [a.split("=")[0].strip().strip("`") for a in split_top(body)]
            keys = [k.split("=")[0].strip().strip("`") for k in split_top(by)] if by else []
            return aggs + keys
    raise ValueError("table query needs KEEP or STATS")


def split_top(s):
    """Split on commas that are not inside parentheses or backticks."""
    out, depth, tick, cur = [], 0, False, ""
    for ch in s:
        if ch == "`":
            tick = not tick
        elif not tick and ch == "(":
            depth += 1
        elif not tick and ch == ")":
            depth -= 1
        if ch == "," and depth == 0 and not tick:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def dashboard_panels(index, panels):
    out = []
    for i, p in enumerate(panels):
        x, y, w, h = p["grid"]
        pid = f"p{i}"
        grid = {"x": x, "y": y, "w": w, "h": h, "i": pid}
        if p["kind"] == "markdown":
            out.append({"type": "visualization", "gridData": grid, "panelIndex": pid, "embeddableConfig": {
                "savedVis": {"title": "", "description": "", "type": "markdown", "uiState": {},
                             "params": {"fontSize": 12, "openLinksInNewTab": False, "markdown": p["markdown"]},
                             "data": {"aggs": [], "searchSource": {}}},
                "hidePanelTitles": True, "enhancements": {}}})
        else:
            config = {"attributes": lens_attributes(p, index), "enhancements": {}}
            if p.get("hide_title"):
                config["hidePanelTitles"] = True
            out.append({"type": "lens", "gridData": grid, "panelIndex": pid, "embeddableConfig": config})
    return out


def control_fields():
    return [f for f in ("cluster", "project", "namespace") if F[f] is not None]


def control_group():
    titles = {"cluster": "Cluster", "project": "Rancher Project ID", "namespace": "Namespace"}
    controls = [(f, titles[f]) for f in control_fields()]
    panels = {}
    for i, (field, title) in enumerate(controls):
        cid = f"ctrl-{field}"
        panels[cid] = {"order": i, "width": "medium", "grow": True, "type": "optionsListControl",
                       "explicitInput": {"id": cid, "dataViewId": DATA_VIEW_ID, "fieldName": F[field],
                                         "title": title, "searchTechnique": "prefix", "selectedOptions": [],
                                         "sort": {"by": "_key", "direction": "asc"}}}
    return {"chainingSystem": "HIERARCHICAL", "controlStyle": "oneLine", "showApplySelections": False,
            "ignoreParentSettingsJSON": json.dumps({"ignoreFilters": False, "ignoreQuery": False,
                                                     "ignoreTimerange": False, "ignoreValidations": False}),
            "panelsJSON": json.dumps(panels)}


def saved_objects(index):
    data_view = {"type": "index-pattern", "id": DATA_VIEW_ID, "attributes": {
        "title": index, "name": "OTel metrics (cluster resources)", "timeFieldName": "@timestamp"},
        "references": [], "managed": False, "coreMigrationVersion": "8.8.0", "typeMigrationVersion": "8.0.0"}
    dashboards = [dashboard_object(DASHBOARD_ID, "Cluster resources (as-is)",
                                   "Allocatable, requested, limited and used CPU/memory per cluster, Rancher Project "
                                   "and namespace from OpenTelemetry metrics.", PANELS, index)]
    if REPORT_PANELS:
        dashboards.append(dashboard_object(
            REPORT_ID, "Cluster resource report",
            "The in-cluster resource report (tiles, capacity by requests with N+1, CPU and memory by project, "
            "Projects and Namespaces) from OpenTelemetry metrics.", REPORT_PANELS, index))
    return [data_view] + dashboards


def dashboard_object(dashboard_id, title, description, panels, index):
    return {"type": "dashboard", "id": dashboard_id, "attributes": {
        "title": title,
        "description": description + " Generated by kibana/build_dashboard.py in cluster-resource-allocation.",
        "timeRestore": True, "timeFrom": "now-7d", "timeTo": "now", "refreshInterval": {"pause": True, "value": 0},
        "optionsJSON": json.dumps({"useMargins": True, "syncColors": False, "syncCursor": True,
                                   "syncTooltips": False, "hidePanelTitles": False}),
        "panelsJSON": json.dumps(dashboard_panels(index, panels)),
        "controlGroupInput": control_group(),
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"},
                                                                  "filter": []})},
    }, "references": [{"type": "index-pattern", "id": DATA_VIEW_ID,
                       "name": f"controlGroup_ctrl-{f}:optionsListDataView"} for f in control_fields()],
        # Current versions (Kibana 9.x); without them Kibana runs legacy migrations that break ES|QL panels.
        "coreMigrationVersion": "8.8.0", "typeMigrationVersion": "10.3.0"}


def default_out(no_project=False):
    name = "cluster-resources-as-is-no-project.ndjson" if no_project else "cluster-resources-as-is.ndjson"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# ---- check mode ----

def run_checks(url, window, end=None, only=None):
    end = datetime.datetime.fromisoformat(end) if end else datetime.datetime.now(datetime.timezone.utc)
    unit = {"m": "minutes", "h": "hours", "d": "days"}[window[-1]]
    start = end - datetime.timedelta(**{unit: int(window[:-1])})
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("ES_API_KEY"):
        headers["Authorization"] = "ApiKey " + os.environ["ES_API_KEY"]
    elif os.environ.get("ES_USER"):
        creds = f"{os.environ['ES_USER']}:{os.environ.get('ES_PASSWORD', '')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
    ctx = ssl.create_default_context()
    if os.environ.get("ES_INSECURE"):
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ok = True
    for p in PANELS + REPORT_PANELS:
        if not p["esql"]:
            continue
        if only and only not in p["title"]:
            continue
        body = {"query": p["esql"], "params": [{"_tstart": iso(start)}, {"_tend": iso(end)}],
                "filter": {"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}}}
        req = urllib.request.Request(url.rstrip("/") + "/_query?format=txt", json.dumps(body).encode(), headers)
        print(f"== {p['title']}")
        try:
            rows = urllib.request.urlopen(req, context=ctx).read().decode().splitlines()
            print("\n".join(rows[:8]) + (f"\n... {len(rows) - 2} rows" if len(rows) > 8 else ""))
            if p["kind"] == "table":
                header = [h.strip() for h in rows[0].split("|")] if rows else []
                expected = table_columns(p["esql"])
                if header != expected:
                    ok = False
                    print(f"!! column mismatch: panel expects {expected}")
        except urllib.error.HTTPError as e:
            ok = False
            print("!! ERROR", e.read().decode()[:800])
        print()
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=DEFAULT_INDEX, help=f"index pattern (default {DEFAULT_INDEX})")
    ap.add_argument("--out", help="output file (default cluster-resources-as-is[-no-project].ndjson here)")
    ap.add_argument("--no-project", action="store_true",
                    help=f"leave out the Rancher Project ({F['project']}) when the collector does not set it")
    ap.add_argument("--check", metavar="ES_URL", help="run the panel queries against Elasticsearch instead")
    ap.add_argument("--window", default="1h", help="time range for --check, e.g. 30m, 6h, 7d (default 1h)")
    ap.add_argument("--end", help="end of the --check range (ISO, e.g. 2026-09-28T17:30:00+00:00; default now)")
    ap.add_argument("--only", help="--check only the panels whose title contains this text")
    ap.add_argument("--rancher-projects", metavar="FILE",
                    help="output of `kubectl get projects.management.cattle.io -A -o json` on the Rancher local "
                         "cluster: Project names and System projects in the report")
    args = ap.parse_args()
    if args.no_project:
        F["project"] = None
    if args.rancher_projects:
        PROJECT_NAMES.update(load_rancher_projects(args.rancher_projects))
    out = args.out or default_out(args.no_project)
    build_panels(args.index)
    build_report_panels(args.index)
    if args.check:
        sys.exit(0 if run_checks(args.check, args.window, args.end, args.only) else 1)
    with open(out, "w") as f:
        for obj in saved_objects(args.index):
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
    print(f"wrote {out} ({len(PANELS)} + {len(REPORT_PANELS)} panels)")


if __name__ == "__main__":
    main()
