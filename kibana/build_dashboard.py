#!/usr/bin/env python3
"""Build the Kibana dashboards of the cluster resource report from OpenTelemetry metrics.

Writes an importable saved-objects file: five linked dashboards and a data view for the filter controls. They follow
the in-cluster dashboard of the cluster-resource-report chart: "capacity" (tiles, capacity by requests with N+1),
"projects" (CPU and memory by project, Projects and Namespaces tables) and "Workload limits" (every workload against
limit <= 2x request); "Project owner" shows one Project (chosen in a control) on every cluster that has it: its
namespaces, requests and usage, now and over time; "Will it fit?" answers whether a new deployment (replicas x CPU and memory per replica, chosen
in controls) fits a cluster, node by node, after node failures and by usage. Control-plane nodes are left out by a
filter saved in each dashboard. Every panel is an
ES|QL Lens panel on the OTel-native data streams written by the Elasticsearch exporter (mapping mode `otel`):
`metrics-k8sclusterreceiver.otel-*` (requests, limits, allocatable, pod phase) and
`metrics-kubeletstatsreceiver.otel-*` (usage).

  ./build_dashboard.py                        # writes cluster-resource-report.ndjson next to this script
  ./build_dashboard.py --index 'metrics-*'    # other index pattern
  ./build_dashboard.py --no-project           # collector without the Rancher Project attribute
  ./build_dashboard.py --rancher-projects projects.json
      # Project names and System projects from `kubectl get projects.management.cattle.io -A -o json`
      # on the Rancher local cluster (without it the report shows Project IDs)
  ./build_dashboard.py --project-lookup-index rancher-projects   # writes cluster-resource-report-project-names.ndjson
      # Project names from a lookup index in Elasticsearch instead (ES|QL LOOKUP JOIN, Elasticsearch 9.1+);
      # publish_project_names.py fills it
  ./build_dashboard.py --project-name-field --out with-names.ndjson
      # Project names from the metrics themselves (resource.attributes.rancher.project.name, set by the collector:
      # build_collector_values.py); the Project filter control then lists names
  ./build_dashboard.py --no-quota --no-hpa --out no-quota-hpa.ndjson
      # no cluster has a ResourceQuota / HorizontalPodAutoscaler yet, so Elasticsearch has no such fields
      # ("Unknown column [metrics.k8s.resource_quota.hard_limit]"); the Quota / HPA columns then show 0
  ./build_dashboard.py --include-control-plane --out with-control-plane.ndjson
      # without the dashboard filter that leaves control-plane nodes out (resource.attributes.k8s.node.role.
      # control_plane = "true", set by the collector: otel-kube-stack-node-roles.yaml)
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

REPORT_ID = "cra-cluster-resource-report"
PROJECTS_ID = "cra-cluster-resource-projects"
OWNER_ID = "cra-project-owner"
LIMITS_ID = "cra-workload-limits"
FIT_ID = "cra-deployment-fit"
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
    # Owning workload, set by k8sattributes from the pod (a CronJob's Job is named <cronjob>-<schedule time>)
    "deployment": "resource.attributes.k8s.deployment.name",
    "statefulset": "resource.attributes.k8s.statefulset.name",
    "daemonset": "resource.attributes.k8s.daemonset.name",
    "job": "resource.attributes.k8s.job.name",
}
# Metrics the k8s_cluster receiver only sends while such objects exist. When no cluster has any, no index has the
# field and ES|QL rejects every query that names it ("Unknown column"): --no-quota / --no-hpa leave them out.
OPTIONAL_FIELDS = {"quota": ("rq_uid", "rq_hard"), "hpa": ("hpa_uid", "hpa_max")}
TIME_FROM = ["now-1h"]  # time range the dashboards open with (to now); --time-from
# Boards that open with a longer range: "Will it fit?" checks usage too, and a P95 over one hour misses daily peaks.
# --time-from sets every board.
BOARD_TIME_FROM = {"fit": "now-24h"}
GIB = "1073741824.0"  # a double, so byte sums (longs) are not integer-divided
NO_PROJECT = '"(no project)"'


def project_expr():
    """Rancher Project ID, or a constant when the data has no project attribute (--no-project):
    ES|QL rejects a query that names a field missing from every index."""
    return NO_PROJECT if F["project"] is None else f"COALESCE({F['project']}, {NO_PROJECT})"


def name_agg(raw):
    """Extra STATS column of the report stages that carries the Project name through the aggregations."""
    if not NAME_FIELD[0] or F["project"] is None:
        return ""
    return f", {LOOKUP_NAME} = MAX({NAME_FIELD[0] if raw else LOOKUP_NAME})"


def q(text):
    """Substitute {field} placeholders and tidy whitespace."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    fields = {k: "NULL" if v is None else v for k, v in F.items()}  # --no-quota / --no-hpa
    return "\n".join(lines).format(**{**fields, "project": project_expr()}, BUCKETS=BUCKETS, GIB=GIB,
                                    category=category_expr(), pname_stage=pname_stage(),
                                    name_agg_raw=name_agg(True),
                                    name_agg=name_agg(False))


REPORT_PANELS = []  # filled by panel() below, in layout order; each panel's "board" says its dashboard
_BOARD = ["capacity"]

# The report is several dashboards, so each file stays small enough for proxies in front of Kibana (--split):
# (board, saved-object ID, title, description); the board is also the file suffix with --split
REPORT_BOARDS = [
    ("capacity", REPORT_ID, "Cluster resource report: capacity",
     "Tiles, capacity by requests with N+1, per cluster, from OpenTelemetry metrics."),
    ("projects", PROJECTS_ID, "Cluster resource report: projects",
     "CPU and memory by project, Projects and Namespaces tables, from OpenTelemetry metrics."),
    ("owner", OWNER_ID, "Project owner: my Project on every cluster",
     "One Rancher Project (chosen in the Project control) on every cluster that has it: namespaces, requests and "
     "P95 usage, over time, from OpenTelemetry metrics."),
    ("limits", LIMITS_ID, "Workload limits: limit ≤ 2× request",
     "Every workload checked against a LimitRange maxLimitRequestRatio of 2, from OpenTelemetry metrics."),
    ("fit", FIT_ID, "Will it fit? New deployment vs. cluster capacity",
     "Does a new deployment (replicas x CPU and memory per replica) fit each cluster: per node, after node "
     "failures and by usage, from OpenTelemetry metrics."),
]


def panel(kind, title, esql, grid, **opts):
    REPORT_PANELS.append({"kind": kind, "title": title, "esql": esql, "grid": grid, "board": _BOARD[0], **opts})


def tile_grid(title, src, tiles, grid, subtitle, bar=False):
    """One panel of several single-value tiles from one query: each (label, STATS expression) becomes a row
    (label, value), shown as a Lens metric grid broken down by label. One query per row of tiles instead of one per
    tile: every tile would otherwise run the whole report query again. With bar, every tile gets a bar to 100 %.
    Labels must be short (a tile shows one line). Under each label Lens shows the name of the value column, so that
    column is named after the subtitle, the same for the whole grid."""
    labels = [label for label, _ in tiles]
    stats = ", ".join(f"v{i} = {expr}" for i, (_, expr) in enumerate(tiles))
    value = ", ".join(f"label == {esql_str(l)}, TO_DOUBLE(v{i})" for i, l in enumerate(labels))
    order = ", ".join(f"label == {esql_str(l)}, {i}" for i, l in enumerate(labels))
    panel("metric_grid", title, q(src + f"""
| STATS {stats}
| EVAL label = [{", ".join(esql_str(l) for l in labels)}]
| MV_EXPAND label
| EVAL value = CASE({value}), ord = CASE({order}){", max = 100.0" if bar else ""}
| SORT ord
| KEEP label, value{", max" if bar else ""}
| RENAME value AS `{subtitle}`"""), grid, tiles=len(tiles), bar=bar, subtitle=subtitle, hide_title=True)


def board_panels(board):
    return [p for p in REPORT_PANELS if p["board"] == board]


def nav_links(current):
    """Links between the dashboards (relative to Kibana's dashboards app, so they work under a base path)."""
    links = [f"[{title}](#/view/{did})" if board != current else f"**{title}**"
             for board, did, title, _ in REPORT_BOARDS if board != "owner" or F["project"] is not None]
    return "Dashboards: " + " · ".join(links)


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
# Lookup index (index.mode: lookup) with one document per Project, from --project-lookup-index:
# LOOKUP_KEY = Project ID, LOOKUP_NAME = display name. publish_project_names.py writes it.
LOOKUP_INDEX = [None]
DEFAULT_LOOKUP_INDEX = "rancher-projects"
LOOKUP_KEY = "project"  # the queries' column of the Project ID; LOOKUP JOIN needs the same name on both sides
LOOKUP_NAME = "project_name"
INDEX_NAME_RE = r"[a-z0-9][a-z0-9._-]*"
# Field with the Project display name on the metrics themselves, from --project-name-field. The collector sets it
# (build_collector_values.py). With it the Project filter control lists names instead of IDs.
NAME_FIELD = [None]
DEFAULT_NAME_FIELD = "resource.attributes.rancher.project.name"
FIELD_NAME_RE = r"[A-Za-z_@][A-Za-z0-9_@]*(\.[A-Za-z_@][A-Za-z0-9_@]*)*"
# Node role field: "true" on control-plane nodes and on the metrics of their pods (the collector copies the RKE2
# node label node-role.kubernetes.io/control-plane: otel-kube-stack-node-roles.yaml). Those nodes take no workloads:
# a filter saved in both dashboards leaves them out of every panel. It is a dashboard filter, not a query change, so
# where no collector sets the field nothing is excluded and no query fails. None = no filter (--include-control-plane).
CP_FIELD = ["resource.attributes.k8s.node.role.control_plane"]
NOW_WINDOW = "10 minutes"  # "now" values (pods, pending, standards) = the last 10 minutes of the time range


def load_rancher_projects(path):
    """Project ID -> display name from `kubectl get projects.management.cattle.io -A -o json`."""
    with open(path) as f:
        return rancher_project_names(json.load(f))


def rancher_project_names(projects):
    """Project ID -> display name from a Project list (the parsed kubectl output)."""
    items = projects.get("items", [])
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


def pname_stage():
    """Stage that adds pname. With a lookup index the name is joined in Elasticsearch; a Project the index does
    not have (and "(no project)") keeps its ID."""
    if NAME_FIELD[0] and F["project"] is not None:
        return f"| EVAL pname = COALESCE({LOOKUP_NAME}, project)"
    if LOOKUP_INDEX[0] and F["project"] is not None:
        return (f"| LOOKUP JOIN {LOOKUP_INDEX[0]} ON {LOOKUP_KEY}\n"
                f"| EVAL pname = COALESCE({LOOKUP_NAME}, project)")
    return f"| EVAL pname = {pname_expr()}"


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
    if PROJECT_NAMES or LOOKUP_INDEX[0] or NAME_FIELD[0]:
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
        c_now = MAX(c_now), rq_now = MAX(rq_now), hpa_now = MAX(hpa_now), ns_now = MAX(ns_now){name_agg_raw}
  BY t = BUCKET(@timestamp, {BUCKETS}, ?_tstart, ?_tend), cluster, project, namespace, entity, container
| EVAL is_c = CASE(c_now == 1 AND container != "", 1, 0),
       no_cpu = CASE(c_now == 1 AND container != "" AND req_cpu IS NULL, 1, 0),
       no_mem_lim = CASE(c_now == 1 AND container != "" AND lim_mem IS NULL, 1, 0),
       any_set = CASE(container != "" AND COALESCE(req_cpu, req_mem, lim_cpu, lim_mem) IS NOT NULL, 1, 0)
| STATS alloc_cpu = MAX(alloc_cpu), alloc_mem = MAX(alloc_mem), req_cpu = SUM(req_cpu), req_mem = SUM(req_mem),
        lim_mem = SUM(lim_mem), use_cpu = SUM(use_cpu), use_mem = SUM(use_mem), phase = MAX(phase),
        phase_now = MAX(phase_now), containers = SUM(is_c), no_cpu = SUM(no_cpu), no_mem_lim = SUM(no_mem_lim),
        any_set = MAX(any_set), rq = MAX(rq_now), hpa = MAX(hpa_now), ns_now = MAX(ns_now){name_agg}
  BY t, cluster, project, namespace, entity
| EVAL req_cpu = TO_DOUBLE(req_cpu), req_mem = TO_DOUBLE(req_mem), lim_mem = TO_DOUBLE(lim_mem),
       run = CASE(phase == 1 OR phase >= 3, 0.0, 1.0)
| EVAL rc = req_cpu * run, rm = req_mem * run, lm = lim_mem * run,
       pend_cpu = CASE(phase_now == 1, req_cpu, 0.0), pend_mem = CASE(phase_now == 1, req_mem, 0.0),
       pods = CASE(phase_now == 2, 1, 0), pending = CASE(phase_now == 1, 1, 0),
       besteffort = CASE(phase_now == 2 AND containers > 0 AND any_set == 0, 1, 0)
{pname_stage}
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
          "N+1 reserve": "#fca5a5", "Requested": "#1d4ed8", "Used (P95)": "#0d9488", "Pending": "#f59e0b",
          "New deployment": "#7c3aed", "Free": "#e2e8f0", "Reserve (failures, ceiling)": "#fca5a5",
          "Requested and used": "#1d4ed8", "Requested, unused": "#93c5fd", "Used above requests": "#dc2626",
          "Used": "#0d9488", "Limit": "#64748b"}


def build_report_panels(index):
    OWNER_CONTROL[0] = None
    _build_report_panels(f"FROM {index}")


# LimitRange maxLimitRequestRatio planned for the "limit <= 2x request" rule (docs/10-implementation-plan.md, 2.1/5.4).
# At admission a container is rejected when limit / request is above it, or when it has a request but no limit (and
# the LimitRange sets no default limit). Without a request the request defaults to the limit (ratio 1).
LIMIT_RATIO = 2

# One row per workload (Deployment, StatefulSet, DaemonSet, CronJob, Job, bare Pod) as of the last minutes of the
# range: its containers' worst limit/request ratio per resource and what would break at LIMIT_RATIO.
WORKLOAD_RATIO = """
| WHERE @timestamp >= TO_DATETIME(?_tend) - NOW_WINDOW
| WHERE {restarts} IS NOT NULL OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {lim_cpu} IS NOT NULL OR {lim_mem} IS NOT NULL
| EVAL kind = CASE({deployment} IS NOT NULL, "Deployment", {statefulset} IS NOT NULL, "StatefulSet",
                   {daemonset} IS NOT NULL, "DaemonSet", {job} RLIKE ".*-[0-9]{{8,}}", "CronJob",
                   {job} IS NOT NULL, "Job", "Pod"),
       workload = COALESCE({deployment}, {statefulset}, {daemonset}, REPLACE({job}, "-[0-9]{{8,}}$", ""), {pod})
| STATS req_cpu = MAX({req_cpu}), lim_cpu = MAX({lim_cpu}), req_mem = MAX({req_mem}), lim_mem = MAX({lim_mem}),
        pods = COUNT_DISTINCT({pod_uid}){name_agg_raw}
  BY cluster = COALESCE({cluster}, "(unknown)"), project = {project}, namespace = {namespace}, kind, workload,
     container = {container}
| WHERE container IS NOT NULL
| EVAL cpu_ratio = TO_DOUBLE(lim_cpu) / req_cpu, mem_ratio = TO_DOUBLE(lim_mem) / req_mem,
       cpu_issue = CASE(req_cpu IS NULL AND lim_cpu IS NULL, "no request, no limit", lim_cpu IS NULL, "no limit",
                        cpu_ratio > RATIO, CONCAT("limit ", TO_STRING(ROUND(cpu_ratio, 1)), "x request")),
       mem_issue = CASE(req_mem IS NULL AND lim_mem IS NULL, "no request, no limit", lim_mem IS NULL, "no limit",
                        mem_ratio > RATIO, CONCAT("limit ", TO_STRING(ROUND(mem_ratio, 1)), "x request"))
| EVAL bad = CASE(cpu_issue IS NOT NULL OR mem_issue IS NOT NULL, container)
| STATS containers = COUNT(*), cpu_bad = COUNT(cpu_issue), mem_bad = COUNT(mem_issue),
        cpu_ratio = MAX(cpu_ratio), mem_ratio = MAX(mem_ratio), cpu_issues = VALUES(cpu_issue),
        mem_issues = VALUES(mem_issue), bad = VALUES(bad), pods = MAX(pods),
        req_cpu = SUM(req_cpu), req_mem = SUM(req_mem){name_agg}
  BY cluster, project, namespace, kind, workload
{pname_stage}
| EVAL category = {category}""".replace("NOW_WINDOW", NOW_WINDOW).replace("RATIO", str(LIMIT_RATIO))

# ---- "Will it fit?": a new deployment against each cluster ----

# The deployment to check, as ES|QL variables set by controls on the dashboard: (variable, title, values, default).
# The values are strings, as Kibana stores them; the queries convert them. Edit a control in Kibana to add values.
FIT_CONTROLS = [
    ("replicas", "Replicas", ["1", "2", "3", "4", "5", "6", "8", "10", "12", "16", "20", "30", "50"], "3"),
    ("cpu", "CPU request per replica (vCPU)", ["0.05", "0.1", "0.25", "0.5", "1", "1.5", "2", "4", "8", "16"], "0.5"),
    ("memory", "Memory request per replica (GiB)",
     ["0.125", "0.25", "0.5", "1", "1.5", "2", "4", "8", "16", "32", "64"], "1"),
    ("spread", "Replicas per node", ["any", "one per node"], "any"),
    ("failures", "Node failures to survive", ["0", "1", "2"], "1"),
    ("ceiling", "Ceiling (% of allocatable)", ["100", "90", "80", "70"], "100"),
]
FIT_DEFAULTS = {name: default for name, _, _, default in FIT_CONTROLS}

# "Project owner": the Project control (ES|QL variable ?project) lists Project names from a query; Kibana's ES|QL
# controls take exactly one value, so the first option (and the one the dashboard opens with) stands for all of them.
OWNER_ALL = "(all projects)"
OWNER_CONTROL = [None]  # the control's options query, set by build_owner_panels
CHECK_PARAMS = {**FIT_DEFAULTS, "project": OWNER_ALL}  # the ES|QL variables as the dashboards open, for --check

FIT_INPUTS = """
| EVAL replicas = TO_DOUBLE(?replicas), c = TO_DOUBLE(?cpu), m = TO_DOUBLE(?memory) * {GIB},
       fails = TO_INTEGER(?failures), ceil = TO_DOUBLE(?ceiling) / 100.0, one = TO_STRING(?spread) == "one per node\""""

# One row per node (and one with node "" for the pending pods): allocatable, requests of running pods and of pending
# pods as of the last minutes of the range, P95 of the node's usage over the range, and how many replicas of the new
# deployment fit on it: by requests (what the scheduler checks) and by requests or usage, whichever is higher.
# A pod's node comes from its own metrics (MAX: the container rows may not have it); pending pods have none.
FIT_NODE_STAGE = """
| WHERE {alloc_cpu} IS NOT NULL OR {alloc_mem} IS NOT NULL OR {req_cpu} IS NOT NULL OR {req_mem} IS NOT NULL
     OR {phase} IS NOT NULL OR {node_use_cpu} IS NOT NULL OR {node_use_mem} IS NOT NULL
| EVAL cluster = COALESCE({cluster}, "(unknown)"), node = COALESCE({node}, ""),
       entity = COALESCE({pod_uid}, {node}, ""), container = COALESCE({container}, ""),
       recent = @timestamp >= TO_DATETIME(?_tend) - NOW_WINDOW
| EVAL a_cpu = CASE(recent, {alloc_cpu}), a_mem = CASE(recent, {alloc_mem}), r_cpu = CASE(recent, {req_cpu}),
       r_mem = CASE(recent, {req_mem}), ph = CASE(recent, {phase})
| STATS a_cpu = MAX(a_cpu), a_mem = MAX(a_mem), r_cpu = MAX(r_cpu), r_mem = MAX(r_mem), ph = MAX(ph),
        node = MAX(node), u_cpu = AVG({node_use_cpu}), u_mem = AVG({node_use_mem})
  BY t = BUCKET(@timestamp, {BUCKETS}, ?_tstart, ?_tend), cluster, entity, container
| STATS a_cpu = MAX(a_cpu), a_mem = MAX(a_mem), r_cpu = SUM(r_cpu), r_mem = SUM(r_mem), ph = MAX(ph),
        node = MAX(node), u_cpu = MAX(u_cpu), u_mem = MAX(u_mem)
  BY t, cluster, entity
| EVAL run = CASE(ph == 1 OR ph >= 3, 0.0, 1.0), pend = CASE(ph == 1, 1.0, 0.0)
| EVAL rc = TO_DOUBLE(r_cpu) * run, rm = TO_DOUBLE(r_mem) * run, pc = TO_DOUBLE(r_cpu) * pend,
       pm = TO_DOUBLE(r_mem) * pend, node = CASE(pend == 1.0, "", node)
| STATS a_cpu = MAX(a_cpu), a_mem = MAX(a_mem), rc = SUM(rc), rm = SUM(rm), pc = SUM(pc), pm = SUM(pm),
        u_cpu = MAX(u_cpu), u_mem = MAX(u_mem)
  BY t, cluster, node
| STATS a_cpu = MAX(a_cpu), a_mem = MAX(a_mem), rc = MAX(rc), rm = MAX(rm), pc = MAX(pc), pm = MAX(pm),
        uc = PERCENTILE(u_cpu, 95), um = PERCENTILE(u_mem, 95)
  BY cluster, node
| EVAL rc = COALESCE(rc, 0.0), rm = COALESCE(rm, 0.0), pc = COALESCE(pc, 0.0), pm = COALESCE(pm, 0.0),
       uc = COALESCE(uc, 0.0), um = COALESCE(um, 0.0)""".replace("NOW_WINDOW", NOW_WINDOW) + FIT_INPUTS + """
| EVAL fc = a_cpu - rc, fm = a_mem - rm, fcu = a_cpu - GREATEST(rc, uc), fmu = a_mem - GREATEST(rm, um)
| EVAL fit = FLOOR(GREATEST(LEAST(fc / c, fm / m), 0.0) + 0.000001),
       fit_u = FLOOR(GREATEST(LEAST(fcu / c, fmu / m), 0.0) + 0.000001)
| EVAL fit = CASE(one, LEAST(fit, 1.0), fit), fit_u = CASE(one, LEAST(fit_u, 1.0), fit_u)"""


def fit_lost(top):
    """What the failed nodes take with them: the largest `fails` values of TOP(..., 2, "desc")."""
    return f"COALESCE(CASE(fails <= 0, 0.0, fails == 1, MV_MAX({top}), MV_SUM({top})), 0.0)"


# One row per cluster. With `fails` nodes down (the largest by allocatable; the ones with the most room for replicas)
# the deployment must still fit node by node, and all requests (running, pending, new) must stay under the ceiling
# of what is left: the N+1 rule of the capacity dashboard, for the new deployment too.
FIT_CLUSTER = """
| STATS nodes = COUNT(a_cpu), a_cpu = SUM(a_cpu), a_mem = SUM(a_mem), rc = SUM(rc), rm = SUM(rm), pc = SUM(pc),
        pm = SUM(pm), uc = SUM(uc), um = SUM(um), top_cpu = TOP(a_cpu, 2, "desc"), top_mem = TOP(a_mem, 2, "desc"),
        fit = SUM(fit), fit_u = SUM(fit_u), top_fit = TOP(fit, 2, "desc"), top_fit_u = TOP(fit_u, 2, "desc"),
        max_fc = MAX(fc), max_fm = MAX(fm)
  BY cluster""" + FIT_INPUTS + f"""
| EVAL need_cpu = replicas * c, need_mem = replicas * m,
       cap_cpu = (a_cpu - {fit_lost("top_cpu")}) * ceil, cap_mem = (a_mem - {fit_lost("top_mem")}) * ceil,
       fit_f = GREATEST(fit - {fit_lost("top_fit")}, 0.0), fit_uf = GREATEST(fit_u - {fit_lost("top_fit_u")}, 0.0)
| EVAL left_cpu = cap_cpu - rc - pc - need_cpu, left_mem = cap_mem - rm - pm - need_mem,
       left_cpu_u = cap_cpu - GREATEST(rc, uc) - pc - need_cpu, left_mem_u = cap_mem - GREATEST(rm, um) - pm - need_mem
| EVAL ok_now = fit >= replicas, ok_fail = fit_f >= replicas AND left_cpu >= 0 AND left_mem >= 0,
       ok_use = fit_uf >= replicas AND left_cpu_u >= 0 AND left_mem_u >= 0
| EVAL verdict = CASE(ok_now AND ok_fail AND ok_use, "FITS",
                      ok_now AND ok_fail, "fits by requests, tight by usage",
                      ok_now, CONCAT("fits now, not N+", TO_STRING(fails), " / ceiling"),
                      "DOES NOT FIT")"""

TILE_H = 5  # height of the report's tile rows
TOP = 4 + 2 * TILE_H  # first row below the About text and the two tile rows


def _build_report_panels(src):
    base = src + REPORT_POD_STAGE
    names = ("Project names from the metrics (set by the collector)." if NAME_FIELD[0] else
             f"Project names from the lookup index `{LOOKUP_INDEX[0]}`." if LOOKUP_INDEX[0] else
             "Project names from Rancher." if PROJECT_NAMES else
             "Projects show their ID; regenerate with `--rancher-projects` for names and System projects.")
    cp_note = ("Control-plane nodes and the pods on them are left out (the filter above the panels). "
               if CP_FIELD[0] else "")
    _BOARD[0] = "capacity"
    panel("markdown", "About", None, (0, 0, 48, 4), markdown=(
        "**Cluster resource report: capacity**, as in the in-cluster dashboard of the cluster-resource-report chart, "
        "on the OpenTelemetry metrics. *Requested* = requests of running pods (averaged over the time range); "
        f"*used* = P95 over the time range; tiles marked *now* = the last {NOW_WINDOW} of the range. *N+1*: "
        "allocatable − largest node − platform (System) requests = room for projects. "
        + cp_note + f"{names} Docs: `guides/kibana-dashboard.md`.\n\n" + nav_links("capacity")))

    summary = base + REPORT_CLUSTER
    # (label, value, bar): one value per tile; the label names it, so the panel title is hidden. % tiles get a bar
    # to 100 % coloured by the allocation rule (80 % prod ceiling, over 100 % overcommitted). Two rows: capacity and
    # requests, then N+1 room, usage and the "now" checks.
    # Three queries for the tiles: capacity and requests, the same as % of allocatable (bars coloured by the
    # allocation rule: 80 % prod ceiling, over 100 % overcommitted), then N+1 room, usage and the "now" checks.
    tile_grid("Capacity and requests", summary, [
        ("Clusters", "COUNT(*)"),
        ("Nodes", "SUM(nodes)"),
        ("Allocatable vCPU", "ROUND(SUM(alloc_cpu), 1)"),
        ("Allocatable GiB", "ROUND(SUM(alloc_mem) / {GIB}, 1)"),
        ("Requested vCPU", "ROUND(SUM(rc), 1)"),
        ("Requested GiB", "ROUND(SUM(rm) / {GIB}, 1)"),
    ], (0, 4, 36, TILE_H), "average over the range")
    tile_grid("Requested (% of allocatable)", summary, [
        ("CPU requested", "ROUND(SUM(rc) / SUM(alloc_cpu) * 100, 0)"),
        ("Memory requested", "ROUND(SUM(rm) / SUM(alloc_mem) * 100, 0)"),
    ], (36, 4, 12, TILE_H), "% of allocatable", bar=True)
    checks = [
        ("N+1 room vCPU", "ROUND(SUM(room_cpu), 1)"),
        ("Left after projects", "ROUND(SUM(room_cpu - rc_t), 1)"),
        ("Used P95 vCPU", "ROUND(SUM(uc), 2)"),
        ("Used P95 GiB", "ROUND(SUM(um) / {GIB}, 1)"),
        ("No CPU request", "SUM(no_cpu)"),
        ("No memory limit", "SUM(no_mem_lim)"),
        ("Pending vCPU", "ROUND(SUM(pend_cpu), 2)"),
        ("Pending GiB", "ROUND(SUM(pend_mem) / {GIB}, 2)"),
    ]
    if F["project"] is not None:
        checks.append(("Unassigned NS", "SUM(ns_u)"))
    # one subtitle for the row: N+1 room and what is left (vCPU), usage P95, then containers, pending, namespaces now
    tile_grid("N+1 room, usage and checks", summary, checks, (0, 4 + TILE_H, 48, TILE_H),
              "N+1 · P95 · now")

    panel("bar_stacked", "Cluster capacity by requests (% of allocatable)", q(summary + REPORT_BY_RESOURCE + """
| EVAL used = tn + sy + un,
       Label = CONCAT(cluster, " · ", res, " (", TO_STRING(nodes), CASE(nodes == 1, " node)", " nodes)")), Tenant = ROUND(tn / a * 100, 1), System = ROUND(sy / a * 100, 1),
       Unassigned = ROUND(un / a * 100, 1), `Free (N+1)` = ROUND(GREATEST(a - lg - used, 0) / a * 100, 1),
       `N+1 reserve` = ROUND(GREATEST(a - GREATEST(a - lg, used), 0) / a * 100, 1)
| SORT cluster, res
| KEEP Label, Tenant, System, Unassigned, `Free (N+1)`, `N+1 reserve`"""), (0, TOP, 26, 10), x="Label",
          y=["Tenant", "System", "Unassigned", "Free (N+1)", "N+1 reserve"])

    panel("table", "N+1: room for projects if the largest node fails (vCPU / GiB)", q(summary + REPORT_BY_RESOURCE + """
| EVAL Cluster = cluster, Resource = res, Nodes = nodes, Allocatable = ROUND(a, 1), `Largest node` = ROUND(lg, 1),
       Platform = ROUND(sy, 1), `For projects` = ROUND(room, 1), `Projects request` = ROUND(tn + un, 1),
       `Left (N+1)` = ROUND(room - tn - un, 1), Fits = CASE(room >= tn + un, "yes", "NO")
| SORT Cluster, Resource
| KEEP Cluster, Resource, Nodes, Allocatable, `Largest node`, Platform, `For projects`, `Projects request`,
       `Left (N+1)`, Fits"""), (26, TOP, 22, 10))

    _BOARD[0] = "projects"
    panel("markdown", "About", None, (0, 0, 48, 3), markdown=(
        "**Cluster resource report: projects**: requests and P95 usage per Rancher Project and namespace "
        f"(averages over the time range; pods, pending and standards columns = the last {NOW_WINDOW}). "
        + cp_note + f"{names}\n\n" + nav_links("projects")))
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
| KEEP Project, Requested, `Used (P95)`"""), (i * 24, 3, 24, 13), x="Project", y=["Requested", "Used (P95)"])

    if F["project"] is not None:
        panel("table", "Projects", q(base + """
| WHERE namespace != ""
""" + report_group("cluster, project, pname, category") + """
| EVAL NS = MV_COUNT(MV_DEDUPE(ns)), Cluster = cluster, Project = pname, `Project ID` = project,
       Category = category
| SORT `CPU req` DESC NULLS LAST
| LIMIT 500
| KEEP Cluster, Project, Category, NS, """ + REPORT_COLUMNS + ", `Project ID`"), (0, 16, 48, 12))

    panel("table", "Namespaces", q(base + """
| WHERE namespace != ""
""" + report_group("cluster, project, pname, category, namespace", extra=", Quota = COALESCE(rq, 0), HPA = COALESCE(hpa, 0)") + """
| EVAL Cluster = cluster, Namespace = namespace, Project = pname, Category = category
| SORT `CPU req` DESC NULLS LAST
| LIMIT 1000
| KEEP Cluster, Namespace, Project, Category, """ + REPORT_COLUMNS + ", Quota, HPA"), (0, 28, 48, 16))

    if F["project"] is not None:
        build_owner_panels(src, base, cp_note, names)

    _BOARD[0] = "limits"
    panel("markdown", "About", None, (0, 0, 48, 5), markdown=(
        f"**Workload limits: limit ≤ {LIMIT_RATIO}× request.** Every Deployment, StatefulSet, DaemonSet, CronJob, "
        f"Job and bare Pod of the last {NOW_WINDOW} of the range, checked against a LimitRange "
        f"`maxLimitRequestRatio: {LIMIT_RATIO}`. Kubernetes would reject its next pod (restart, rollout, scale-up, "
        f"drain) when a container has a limit above {LIMIT_RATIO}× its request, or a request without a limit "
        "(unless the LimitRange sets a default limit); a limit without a request passes (the request becomes the "
        "limit). Problems first; *CPU* only matters if the ratio is set for CPU too. "
        + cp_note + f"{names}\n\n" + nav_links("limits")))
    panel("table", f"Workloads vs. limit ≤ {LIMIT_RATIO}× request (LimitRange maxLimitRequestRatio: {LIMIT_RATIO}; now)",
          q(src + WORKLOAD_RATIO + f"""
| EVAL Cluster = cluster, Namespace = namespace, Project = pname, Category = category, Kind = kind,
       Workload = workload, Pods = pods, Containers = containers,
       Status = CASE(mem_bad + cpu_bad == 0, "OK", mem_bad > 0 AND cpu_bad > 0, "breaks: memory + CPU",
                     mem_bad > 0, "breaks: memory", "breaks: CPU"),
       `Memory: max limit/request` = ROUND(mem_ratio, 1), `Memory problems` = mem_issues,
       `CPU: max limit/request` = ROUND(cpu_ratio, 1), `CPU problems` = cpu_issues, `Containers affected` = bad,
       `CPU req` = ROUND(req_cpu, 2), `Mem req GiB` = ROUND(req_mem / {{GIB}}, 2)
| SORT mem_bad DESC, cpu_bad DESC, Cluster, Namespace, Workload
| LIMIT 2000
| KEEP Cluster, Namespace, Project, Category, Kind, Workload, Status, `Memory: max limit/request`,
       `Memory problems`, `CPU: max limit/request`, `CPU problems`, `Containers affected`, Pods, Containers,
       `CPU req`, `Mem req GiB`"""), (0, 5, 48, 22))

    _BOARD[0] = "fit"
    nodes = src + FIT_NODE_STAGE
    clusters = nodes + FIT_CLUSTER
    panel("markdown", "About", None, (0, 0, 48, 7), markdown=(
        "**Will it fit?** Set the new deployment in the controls above (replicas, CPU and memory *request* per "
        "replica, one replica per node or any, node failures to survive, ceiling), and pick a cluster or compare all. "
        f"Requests are those of the last {NOW_WINDOW} of the range (running pods; pending pods count as demand); "
        "usage is the nodes' P95 over the range (default: 24 hours). A cluster **FITS** when all three hold:\n\n"
        "1. *now*: enough nodes each have room for a whole replica (CPU **and** memory free by requests: what the "
        "scheduler checks);\n"
        "2. *after node failures*: without the largest nodes it still has room for every replica, and running + "
        "pending + new requests stay within the ceiling of what is left (the N+1 rule of the capacity dashboard);\n"
        "3. *by usage*: the same with each node's usage where it is higher than its requests (pods using more than "
        "they request leave less room than the requests say).\n\n"
        "Taints, node selectors, affinity, quotas and cordoned nodes are not in the metrics: check those for the "
        "target nodes. " + cp_note + "\n\n" + nav_links("fit")))
    tight = '"fits by requests, tight by usage"'
    tile_grid("Answer", clusters, [
        ("Fits", 'COUNT(CASE(verdict == "FITS", 1))'),
        ("Fits by requests only", f"COUNT(CASE(verdict == {tight}, 1))"),
        ("Does not fit", f'COUNT(CASE(verdict != "FITS" AND verdict != {tight}, 1))'),
        ("Needed vCPU", "ROUND(MAX(need_cpu), 2)"),
        ("Needed GiB", "ROUND(MAX(need_mem) / {GIB}, 2)"),
    ], (0, 7, 48, TILE_H), "clusters · needed for all replicas")

    panel("table", "Does it fit? One row per cluster", q(clusters + """
| EVAL Cluster = cluster, Verdict = verdict, Nodes = nodes, Replicas = replicas, `Fit now` = fit,
       `Fit after failures` = fit_f, `Fit by usage, after failures` = fit_uf,
       `Needed vCPU` = ROUND(need_cpu, 2), `vCPU left after failures` = ROUND(left_cpu, 2),
       `vCPU left by usage` = ROUND(left_cpu_u, 2), `Needed GiB` = ROUND(need_mem / {GIB}, 2),
       `GiB left after failures` = ROUND(left_mem / {GIB}, 2), `GiB left by usage` = ROUND(left_mem_u / {GIB}, 2),
       `Most free vCPU on a node` = ROUND(max_fc, 2), `Most free GiB on a node` = ROUND(max_fm / {GIB}, 2),
       `Pending vCPU` = ROUND(pc, 2), `Pending GiB` = ROUND(pm / {GIB}, 2)
| SORT Cluster
| KEEP Cluster, Verdict, Nodes, Replicas, `Fit now`, `Fit after failures`, `Fit by usage, after failures`,
       `Needed vCPU`, `vCPU left after failures`, `vCPU left by usage`, `Needed GiB`, `GiB left after failures`,
       `GiB left by usage`, `Most free vCPU on a node`, `Most free GiB on a node`, `Pending vCPU`, `Pending GiB`"""),
          (0, 7 + TILE_H, 48, 9))

    # What is taken now is split into requested-and-used, requested-but-unused and used-above-requests: requests are the
    # first two, used (P95) the first and the last, and all three together are what the "by usage" check subtracts.
    panel("bar_stacked", "Requests and usage (P95) with the new deployment (% of allocatable)", q(clusters + """
| EVAL res = ["CPU", "Memory"]
| MV_EXPAND res
| EVAL a = CASE(res == "CPU", a_cpu, a_mem), r = CASE(res == "CPU", rc, rm), u = CASE(res == "CPU", uc, um),
       p = CASE(res == "CPU", pc, pm), n = CASE(res == "CPU", need_cpu, need_mem),
       cap = CASE(res == "CPU", cap_cpu, cap_mem)
| EVAL taken = GREATEST(r, u)
| EVAL Label = CONCAT(cluster, " · ", res), `Requested and used` = ROUND(LEAST(r, u) / a * 100, 1),
       `Requested, unused` = ROUND(GREATEST(r - u, 0) / a * 100, 1),
       `Used above requests` = ROUND(GREATEST(u - r, 0) / a * 100, 1), Pending = ROUND(p / a * 100, 1),
       `New deployment` = ROUND(n / a * 100, 1), Free = ROUND(GREATEST(cap - taken - p - n, 0) / a * 100, 1),
       `Reserve (failures, ceiling)` = ROUND(GREATEST(a - GREATEST(cap, taken + p + n), 0) / a * 100, 1)
| SORT cluster, res
| KEEP Label, `Requested and used`, `Requested, unused`, `Used above requests`, Pending, `New deployment`, Free,
       `Reserve (failures, ceiling)`"""),
          (0, 16 + TILE_H, 48, 10), x="Label",
          y=["Requested and used", "Requested, unused", "Used above requests", "Pending", "New deployment", "Free",
             "Reserve (failures, ceiling)"])

    panel("table", "Nodes: room and replicas per node", q(nodes + """
| WHERE a_cpu IS NOT NULL
| EVAL Cluster = cluster, Node = node, `Replicas fit` = fit, `Replicas fit by usage` = fit_u,
       `Limited by` = CASE(fc / c <= fm / m, "CPU", "memory"),
       `Alloc vCPU` = ROUND(a_cpu, 2), `Req vCPU` = ROUND(rc, 2), `Used P95 vCPU` = ROUND(uc, 2),
       `Free vCPU` = ROUND(fc, 2), `Alloc GiB` = ROUND(a_mem / {GIB}, 2), `Req GiB` = ROUND(rm / {GIB}, 2),
       `Used P95 GiB` = ROUND(um / {GIB}, 2), `Free GiB` = ROUND(fm / {GIB}, 2)
| SORT Cluster, `Replicas fit` DESC, Node
| LIMIT 1000
| KEEP Cluster, Node, `Replicas fit`, `Replicas fit by usage`, `Limited by`, `Alloc vCPU`, `Req vCPU`,
       `Used P95 vCPU`, `Free vCPU`, `Alloc GiB`, `Req GiB`, `Used P95 GiB`, `Free GiB`"""), (0, 26 + TILE_H, 48, 14))


def owner_control_query(src):
    """Options of the Project control: the Project labels of the time range (names when the report has them, as in
    its panels: pname), after OWNER_ALL. Projects without a name keep their ID; namespaces without a Project are not
    offered. A control query gets the dashboard's time range as a filter, not ?_tstart / ?_tend."""
    name = f"{LOOKUP_NAME} = MAX({NAME_FIELD[0]}) " if NAME_FIELD[0] else ""
    return q(src + f"""
| WHERE {F["project"]} IS NOT NULL
| STATS {name}BY project = {F["project"]}
{{pname_stage}}
| STATS names = VALUES(pname)
| EVAL pname = MV_APPEND({esql_str(OWNER_ALL)}, MV_SORT(names))
| MV_EXPAND pname
| KEEP pname""")


def owner_filter():
    """First stage of the "Project owner" panels: keep only the picked Project's documents, right after FROM. The
    condition names raw fields only, so Elasticsearch turns it into an index query (terms on the Project ID or name
    field, plus "has a namespace") and reads one Project's documents instead of every document of the time range;
    a condition on a computed column (an EVAL) is evaluated per document after reading it. The control holds the
    label the panels show, so a name is mapped back to its IDs (one name can be a Project on several clusters);
    "(all projects)" folds to true and the condition falls away. A lookup index can only name a Project after a
    join, so there the panels filter late only (after pname_stage). add_project_names.py rewrites this line too."""
    if F["project"] is None or (LOOKUP_INDEX[0] and not NAME_FIELD[0]):
        return ""
    return f"""
| WHERE {F["namespace"]} IS NOT NULL
| WHERE {owner_project_condition(F["project"], NAME_FIELD[0], PROJECT_NAMES)}"""


def owner_project_condition(field, name_field, names):
    """?project is "(all projects)", or the document's Project (name when known, else ID) is the picked one."""
    if name_field:
        picked = f"{name_field} == ?project OR ({name_field} IS NULL AND {field} == ?project)"
    else:
        ids = {}
        for pid, name in sorted(names.items()):
            ids.setdefault(name, []).append(pid)
        picked = "".join(f"(?project == {esql_str(name)} AND {field} IN ({', '.join(map(esql_str, pids))})) OR "
                         for name, pids in sorted(ids.items())) + f"{field} == ?project"
    return f"?project == {esql_str(OWNER_ALL)} OR {picked}"


def build_owner_panels(src, base, cp_note, names):
    """"Project owner": the Project picked in the Project control (an ES|QL control listing Project names, so the
    panels filter on ?project after the name is known), on every cluster that has it; OWNER_ALL shows every Project.
    Needs the Project attribute. Requests and usage per namespace (as in the Namespaces table of the projects
    dashboard), per cluster, and over time."""
    _BOARD[0] = "owner"
    OWNER_CONTROL[0] = owner_control_query(src)
    panel("markdown", "About", None, (0, 0, 48, 4), markdown=(
        "**Project owner: my Project on every cluster.** Pick your Project in the *Project* control above (then "
        f"a cluster or namespace to narrow it down); *{OWNER_ALL}* shows every Project. "
        "*Requested* = requests of running pods, averaged over the time range; *used* = P95 of usage over the range; "
        f"*used % of requests* = P95 used / requested; pods, pending and standards = the last {NOW_WINDOW}. "
        "Over time: requests, usage and the memory limit per time bucket. "
        + cp_note + f"{names}\n\n" + nav_links("owner")))

    base = src + owner_filter() + base[len(src):]
    ns = base + f'''
| WHERE namespace != "" AND (?project == {esql_str(OWNER_ALL)} OR pname == ?project)'''
    # per time bucket: the selection's totals, then the tiles aggregate over the buckets
    totals = ns + """
| STATS rc = SUM(rc), rm = SUM(rm), lm = SUM(lm), uc = SUM(use_cpu), um = SUM(use_mem), pods = SUM(pods),
        pending = SUM(pending), no_cpu = SUM(no_cpu), no_mem_lim = SUM(no_mem_lim), besteffort = SUM(besteffort),
        clusters = COUNT_DISTINCT(cluster), nss = COUNT_DISTINCT(CONCAT(cluster, "/", namespace))
  BY t"""
    # Lens writes the value column's name under every tile of a grid, so each row holds tiles of one kind: values over
    # the time range, then counts as of the last minutes
    tile_grid("Requests and usage", totals, [
        ("Clusters", "MAX(clusters)"),
        ("Namespaces", "MAX(nss)"),
        ("Requested vCPU", "ROUND(AVG(rc), 2)"),
        ("Used P95 vCPU", "ROUND(PERCENTILE(uc, 95), 2)"),
        ("CPU used % of requests", "ROUND(PERCENTILE(uc, 95) / AVG(rc) * 100, 0)"),
        ("Requested GiB", "ROUND(AVG(rm) / {GIB}, 2)"),
        ("Used P95 GiB", "ROUND(PERCENTILE(um, 95) / {GIB}, 2)"),
        ("Mem used % of requests", "ROUND(PERCENTILE(um, 95) / AVG(rm) * 100, 0)"),
    ], (0, 4, 48, TILE_H), "over the range")
    tile_grid("Pods and standards now", totals, [
        ("Pods", "MAX(pods)"),
        ("Pending pods", "MAX(pending)"),
        ("No CPU request", "MAX(no_cpu)"),
        ("No memory limit", "MAX(no_mem_lim)"),
        ("BestEffort pods", "MAX(besteffort)"),
    ], (0, 4 + TILE_H, 48, TILE_H), f"now (last {NOW_WINDOW})")

    top = 4 + 2 * TILE_H
    for i, (res, unit) in enumerate([("cpu", "vCPU"), ("mem", "GiB")]):
        label = "CPU" if res == "cpu" else "Memory"
        r, u = ("rc", "use_cpu") if res == "cpu" else ("rm", "use_mem")
        div = "" if res == "cpu" else " / {GIB}"
        panel("bar", f"{label} by cluster ({unit}): requested and used (P95)", q(ns + f"""
| STATS req = SUM({r}), used = SUM({u}) BY t, cluster, pname
| STATS Requested = ROUND(AVG(req){div}, 2), `Used (P95)` = ROUND(PERCENTILE(used, 95){div}, 2) BY cluster, pname
| EVAL Cluster = CONCAT(pname, " · ", cluster)
| SORT Requested DESC NULLS LAST
| LIMIT 30
| KEEP Cluster, Requested, `Used (P95)`"""), (i * 24, top, 24, 10), x="Cluster", y=["Requested", "Used (P95)"])

    # requests and usage per time bucket (sums over the pods; usage is the average within a bucket, not the P95)
    panel("line", "CPU over time (vCPU)", q(ns + """
| STATS Requested = ROUND(SUM(rc), 2), Used = ROUND(SUM(use_cpu), 2) BY t
| SORT t
| KEEP t, Requested, Used"""), (0, top + 10, 24, 10), x="t", y=["Requested", "Used"])
    panel("line", "Memory over time (GiB)", q(ns + """
| STATS Requested = ROUND(SUM(rm) / {GIB}, 2), Used = ROUND(SUM(use_mem) / {GIB}, 2),
        Limit = ROUND(SUM(lm) / {GIB}, 2) BY t
| SORT t
| KEEP t, Requested, Used, Limit"""), (24, top + 10, 24, 10), x="t", y=["Requested", "Used", "Limit"])

    panel("table", "Clusters: my Project per cluster", q(ns + report_group("cluster, project, pname, category") + """
| EVAL NS = MV_COUNT(MV_DEDUPE(ns)), Cluster = cluster, Project = pname, `Project ID` = project,
       Category = category, Namespaces = MV_SORT(MV_DEDUPE(ns))
| SORT Project, Cluster
| LIMIT 500
| KEEP Cluster, Project, NS, """ + REPORT_COLUMNS + ", Namespaces, Category, `Project ID`"), (0, top + 20, 48, 9))

    panel("table", "Namespaces", q(ns + report_group(
        "cluster, project, pname, category, namespace", extra=", Quota = COALESCE(rq, 0), HPA = COALESCE(hpa, 0)") + """
| EVAL Cluster = cluster, Namespace = namespace, Project = pname
| SORT Project, Cluster, Namespace
| LIMIT 1000
| KEEP Cluster, Namespace, Project, """ + REPORT_COLUMNS + ", Quota, HPA"), (0, top + 29, 48, 14))


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
    if kind == "metric_grid":
        columns = [col("label", "label", "string"), col("value", p["subtitle"], "number")]
        vis_type = "lnsMetric"
        vis = {"layerId": "layer_0", "layerType": "data", "metricAccessor": "value", "breakdownByAccessor": "label",
               "maxCols": p["tiles"], "showBar": False}
        if p.get("bar"):
            columns.append(col("max", "max", "number"))
            vis.update(maxAccessor="max", showBar=True, progressDirection="horizontal", palette=ALLOCATION_PALETTE)
    elif kind == "table":
        columns = [col(f"c{i}", n) for i, n in enumerate(table_columns(p["esql"]))]
        vis_type = "lnsDatatable"
        widths = {"Cluster": 150, "Namespace": 170, "Project": 130, "Verdict": 260, "Node": 230}
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


def control_fields(board=None):
    """Filter controls of a board. "Will it fit?" has only the cluster: a Project or namespace filter would drop the
    nodes' allocatable from its queries. "Project owner" picks its Project in an ES|QL control instead (names)."""
    fields = (("cluster",) if board == "fit" else ("cluster", "namespace") if board == "owner" else
              ("cluster", "project", "namespace"))
    return [f for f in fields if F[f] is not None]


def esql_controls():
    """Static-value ES|QL controls that set the variables of the "Will it fit?" queries (?replicas, ?cpu, ...)."""
    return {f"ctrl-{name}": {"type": "esqlControl", "width": "small", "grow": False, "explicitInput": {
        "id": f"ctrl-{name}", "title": title, "variableName": name, "variableType": "values",
        "controlType": "STATIC_VALUES", "esqlQuery": "", "availableOptions": values, "selectedOptions": [default]}}
        for name, title, values, default in FIT_CONTROLS}


def owner_control():
    """ES|QL control of "Project owner" that sets ?project: options from OWNER_CONTROL, opens with OWNER_ALL."""
    return {"ctrl-project-name": {"type": "esqlControl", "width": "medium", "grow": True, "explicitInput": {
        "id": "ctrl-project-name", "title": "Project", "variableName": "project", "variableType": "values",
        "controlType": "VALUES_FROM_QUERY", "esqlQuery": OWNER_CONTROL[0], "availableOptions": [],
        "selectedOptions": [OWNER_ALL]}}}


def control_group(board=None):
    titles = {"cluster": "Cluster", "project": "Rancher Project ID", "namespace": "Namespace"}
    fields = dict(F)
    if NAME_FIELD[0] and F["project"] is not None:  # select Projects by name
        titles["project"], fields["project"] = "Rancher Project", NAME_FIELD[0]
    controls = [(f, titles[f]) for f in control_fields(board)]
    panels = {cid: {"order": 0, **c} for cid, c in owner_control().items()} if board == "owner" else {}
    for field, title in controls:
        cid = f"ctrl-{field}"
        panels[cid] = {"order": len(panels), "width": "medium", "grow": True, "type": "optionsListControl",
                       "explicitInput": {"id": cid, "dataViewId": DATA_VIEW_ID, "fieldName": fields[field],
                                         "title": title, "searchTechnique": "prefix", "selectedOptions": [],
                                         "sort": {"by": "_key", "direction": "asc"}}}
    if board == "fit":
        for cid, control in esql_controls().items():
            panels[cid] = {"order": len(panels), **control}
    return {"chainingSystem": "HIERARCHICAL", "controlStyle": "oneLine", "showApplySelections": False,
            "ignoreParentSettingsJSON": json.dumps({"ignoreFilters": False, "ignoreQuery": False,
                                                     "ignoreTimerange": False, "ignoreValidations": False}),
            "panelsJSON": json.dumps(panels)}


def saved_objects(index):
    data_view = {"type": "index-pattern", "id": DATA_VIEW_ID, "attributes": {
        "title": index, "name": "OTel metrics (cluster resources)", "timeFieldName": "@timestamp"},
        "references": [], "managed": False, "coreMigrationVersion": "8.8.0", "typeMigrationVersion": "8.0.0"}
    dashboards = [dashboard_object(did, title, description, board_panels(board), index, board)
                  for board, did, title, description in REPORT_BOARDS if board_panels(board)]
    return [data_view] + dashboards


CP_FILTER_REF = "kibanaSavedObjectMeta.searchSourceJSON.filter[0].meta.index"


def saved_filters():
    """Dashboard filter "NOT <role field>: true" (a negated phrase filter, as Kibana saves one)."""
    if not CP_FIELD[0]:
        return []
    return [{"meta": {"alias": "Control-plane nodes", "negate": True, "disabled": False, "type": "phrase",
                      "key": CP_FIELD[0], "params": {"query": "true"}, "indexRefName": CP_FILTER_REF},
             "query": {"match_phrase": {CP_FIELD[0]: "true"}}, "$state": {"store": "appState"}}]


def dashboard_object(dashboard_id, title, description, panels, index, board=None):
    return {"type": "dashboard", "id": dashboard_id, "attributes": {
        "title": title,
        "description": description + " Generated by kibana/build_dashboard.py in cluster-resource-allocation.",
        "timeRestore": True, "timeFrom": BOARD_TIME_FROM.get(board, TIME_FROM[0]), "timeTo": "now", "refreshInterval": {"pause": True, "value": 0},
        "optionsJSON": json.dumps({"useMargins": True, "syncColors": False, "syncCursor": True,
                                   "syncTooltips": False, "hidePanelTitles": False}),
        "panelsJSON": json.dumps(dashboard_panels(index, panels)),
        "controlGroupInput": control_group(board),
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"},
                                                                  "filter": saved_filters()})},
    }, "references": [{"type": "index-pattern", "id": DATA_VIEW_ID,
                       "name": f"controlGroup_ctrl-{f}:optionsListDataView"} for f in control_fields(board)]
                     + ([{"type": "index-pattern", "id": DATA_VIEW_ID, "name": CP_FILTER_REF}] if CP_FIELD[0] else []),
        # Current versions (Kibana 9.x); without them Kibana runs legacy migrations that break ES|QL panels.
        "coreMigrationVersion": "8.8.0", "typeMigrationVersion": "10.3.0"}


def default_out(no_project=False, lookup=False):
    name = ("cluster-resource-report-no-project.ndjson" if no_project else
            "cluster-resource-report-project-names.ndjson" if lookup else "cluster-resource-report.ndjson")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# ---- check mode ----

def es_auth(content_type="application/json"):
    """Request headers and TLS context from $ES_API_KEY, or $ES_USER / $ES_PASSWORD; $ES_INSECURE skips TLS checks."""
    headers = {"Content-Type": content_type}
    if os.environ.get("ES_API_KEY"):
        headers["Authorization"] = "ApiKey " + os.environ["ES_API_KEY"]
    elif os.environ.get("ES_USER"):
        creds = f"{os.environ['ES_USER']}:{os.environ.get('ES_PASSWORD', '')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
    ctx = ssl.create_default_context()
    if os.environ.get("ES_INSECURE"):
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return headers, ctx


def run_checks(url, window, end=None, only=None):
    end = datetime.datetime.fromisoformat(end) if end else datetime.datetime.now(datetime.timezone.utc)
    unit = {"m": "minutes", "h": "hours", "d": "days"}[window[-1]]
    start = end - datetime.timedelta(**{unit: int(window[:-1])})
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers, ctx = es_auth()
    ok = True
    # the panels, and the query that fills the "Project owner" control
    control = [{"title": "Project owner: options of the Project control", "esql": OWNER_CONTROL[0], "kind": "control"}]
    for p in REPORT_PANELS + (control if OWNER_CONTROL[0] else []):
        if not p["esql"]:
            continue
        if only and only not in p["title"]:
            continue
        # the ES|QL control variables as the dashboards open (strings, as Kibana stores them)
        params = [{"_tstart": iso(start)}, {"_tend": iso(end)}] + [
            {k: v} for k, v in CHECK_PARAMS.items() if re.search(rf"\?{k}\b", p["esql"])]
        body = {"query": p["esql"], "params": params,
                "filter": {"bool": {"filter": [{"range": {"@timestamp": {"gte": iso(start), "lte": iso(end)}}}],
                                    "must_not": [f["query"] for f in saved_filters()]}}}
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
    ap.add_argument("--out", help="output file (default cluster-resource-report[-no-project].ndjson here)")
    ap.add_argument("--no-project", action="store_true",
                    help=f"leave out the Rancher Project ({F['project']}) when the collector does not set it")
    ap.add_argument("--check", metavar="ES_URL", help="run the panel queries against Elasticsearch instead")
    ap.add_argument("--window", default="1h", help="time range for --check, e.g. 30m, 6h, 7d (default 1h)")
    ap.add_argument("--end", help="end of the --check range (ISO, e.g. 2026-09-28T17:30:00+00:00; default now)")
    ap.add_argument("--only", help="--check only the panels whose title contains this text")
    ap.add_argument("--rancher-projects", metavar="FILE",
                    help="output of `kubectl get projects.management.cattle.io -A -o json` on the Rancher local "
                         "cluster: Project names and System projects in the report")
    ap.add_argument("--project-lookup-index", metavar="INDEX",
                    help="Project names from this lookup index in Elasticsearch (LOOKUP JOIN, Elasticsearch 9.1+), "
                         "e.g. rancher-projects; fill it with publish_project_names.py. Every report panel fails "
                         "while the index does not exist")
    ap.add_argument("--time-from", metavar="RANGE",
                    help=f"start of the time range every dashboard opens with, e.g. now-7d (default {TIME_FROM[0]}; "
                         f"\"Will it fit?\" {BOARD_TIME_FROM['fit']})")
    ap.add_argument("--no-quota", action="store_true",
                    help=f"leave out the ResourceQuota metric ({F['rq_hard']}): no cluster has a quota yet, so "
                         "the field does not exist. Needs --out")
    ap.add_argument("--no-hpa", action="store_true",
                    help=f"the same for the HorizontalPodAutoscaler metric ({F['hpa_max']})")
    ap.add_argument("--include-control-plane", action="store_true",
                    help=f"without the dashboard filter that leaves out nodes where {CP_FIELD[0]} is \"true\" "
                         "and the metrics of their pods (also for --check). Needs --out")
    ap.add_argument("--split", action="store_true",
                    help="one file per dashboard (<out>-capacity.ndjson, -projects, -owner, -limits, -fit), each with the "
                         "data view: smaller uploads, e.g. behind a proxy that limits the request size")
    ap.add_argument("--project-name-field", metavar="FIELD", nargs="?", const=DEFAULT_NAME_FIELD,
                    help=f"Project names from this field of the metrics (default {DEFAULT_NAME_FIELD}), which the "
                         "collector sets (build_collector_values.py). The Project filter control then lists names. "
                         "Every panel fails while no index has the field. Needs --out")
    args = ap.parse_args()
    if sum(map(bool, (args.no_project, args.rancher_projects, args.project_lookup_index,
                      args.project_name_field))) > 1:
        ap.error("--no-project, --rancher-projects, --project-lookup-index and --project-name-field "
                 "exclude each other")
    if args.project_name_field:
        if not re.fullmatch(FIELD_NAME_RE, args.project_name_field):
            ap.error(f"--project-name-field: not a field name: {args.project_name_field}")
        if not args.out and not args.check:
            ap.error("--project-name-field needs --out (the committed files are generated without it)")
        NAME_FIELD[0] = args.project_name_field
    if args.include_control_plane:
        if not args.out and not args.check:
            ap.error("--include-control-plane needs --out (the committed files are generated without it)")
        CP_FIELD[0] = None
    if args.project_lookup_index:
        if not re.fullmatch(INDEX_NAME_RE, args.project_lookup_index):
            ap.error(f"--project-lookup-index: not an index name: {args.project_lookup_index}")
        if not args.out and not args.check and args.project_lookup_index != DEFAULT_LOOKUP_INDEX:
            ap.error(f"--project-lookup-index: an index other than {DEFAULT_LOOKUP_INDEX} needs --out "
                     "(the committed file is generated for that name)")
        LOOKUP_INDEX[0] = args.project_lookup_index
    if args.time_from:
        if not re.fullmatch(r"now-[1-9][0-9]*[mhdwMy]", args.time_from):
            ap.error(f"--time-from: expected e.g. now-1d or now-12h, got {args.time_from}")
        TIME_FROM[0] = args.time_from
        BOARD_TIME_FROM.clear()
    if args.no_project:
        F["project"] = None
    if (args.no_quota or args.no_hpa) and not args.out and not args.check:
        ap.error("--no-quota / --no-hpa need --out (the committed files are generated without them)")
    for flag, kind in ((args.no_quota, "quota"), (args.no_hpa, "hpa")):
        if flag:
            F.update(dict.fromkeys(OPTIONAL_FIELDS[kind]))
    if args.rancher_projects:
        PROJECT_NAMES.update(load_rancher_projects(args.rancher_projects))
    out = args.out or default_out(args.no_project, bool(args.project_lookup_index))
    build_report_panels(args.index)
    if args.check:
        sys.exit(0 if run_checks(args.check, args.window, args.end, args.only) else 1)
    data_view, *dashboards = saved_objects(args.index)
    if not args.split:
        write_objects(out, [data_view] + dashboards)
        return
    stem = out[:-len(".ndjson")] if out.endswith(".ndjson") else out
    boards = {did: board for board, did, _, _ in REPORT_BOARDS}
    for d in dashboards:
        write_objects(f"{stem}-{boards[d['id']]}.ndjson", [data_view, d])


def write_objects(path, objects):
    text = "".join(json.dumps(obj, separators=(",", ":")) + "\n" for obj in objects)
    with open(path, "w") as f:
        f.write(text)
    titles = ", ".join(o["attributes"]["title"] for o in objects if o["type"] == "dashboard")
    print(f"wrote {path} ({len(text.encode()) // 1024} KiB: {titles})")
    if len(text.encode()) > 900 * 1024:
        print(f"warning: {path} is near or above 1 MiB, the default request limit of ingress-nginx; with many "
              "Projects use --project-name-field instead of --rancher-projects (names on the metrics, not in "
              "every query)", file=sys.stderr)


if __name__ == "__main__":
    main()
