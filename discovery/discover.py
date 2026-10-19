#!/usr/bin/env python3
"""Discovery: current resource requests/limits and usage per namespace and Rancher Project.

Read-only. Talks to clusters through kubectl only (no extra Python packages).

Sources per cluster (kubectl context):
  - Kubernetes API: nodes, namespaces, pods, ResourceQuotas, LimitRanges, HPAs
  - metrics-server (optional): usage snapshot
  - Prometheus (optional): usage avg/P95/max and peak requests over a time window,
    reached through the API server service proxy (default: Rancher Monitoring) or a direct URL
  - Rancher local cluster (optional): Project display names

Outputs CSVs (namespaces, projects, clusters) into --out.
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GIB = 2**30

RANCHER_PROJECT_ANNOTATION = "field.cattle.io/projectId"  # "<cluster-id>:<project-id>"
NAMES_CONFIGMAP = "rancher-project-names"  # written by publish_rancher_names.py, delivered by Fleet

# Same value as collection.systemNamespaceRegex in the chart's values.yaml (test_discover checks it).
# Storage providers, then keywords: any name containing "storage", "system" or "ingress"
# (local-path-storage, longhorn-system, ingress-nginx, ...).
DEFAULT_SYSTEM_NS_REGEX = (
    r"^(kube-.*|cattle-.*|fleet-.*|rancher-.*|calico-.*|tigera-.*|kyverno|cert-manager|otel|monitoring|"
    r"local|p-[a-z0-9]{5}|local-p-[a-z0-9]{5}|c-[a-z0-9-]+|u-[a-z0-9]+|user-[a-z0-9]+|"
    r"rook-.*|openebs.*|csi-.*|.*-csi.*|.*provisioner.*|"
    r".*storage.*|.*system.*|.*ingress.*)$"
)

# Rancher Projects whose namespaces all count as System (platform), matched against the project's display name,
# ignoring case: Rancher's own System project and any project with "platform" in its name. Same value as
# collection.systemProjectRegex in the chart's values.yaml (test_discover checks it).
DEFAULT_SYSTEM_PROJECT_REGEX = r"^(system|.*platform.*)$"
DEFAULT_SYSTEM_PROJECT_RE = re.compile(DEFAULT_SYSTEM_PROJECT_REGEX, re.IGNORECASE)

DEFAULT_PROM_SERVICE = "cattle-monitoring-system/http:rancher-monitoring-prometheus:9090"

_SUFFIXES = [
    ("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("Ti", 2**40), ("Pi", 2**50), ("Ei", 2**60),
    ("n", 1e-9), ("u", 1e-6), ("m", 1e-3),
    ("k", 1e3), ("M", 1e6), ("G", 1e9), ("T", 1e12), ("P", 1e15), ("E", 1e18),
]


_log_local = threading.local()   # per-thread capture of log lines during collect()


def log(msg):
    print(msg, file=sys.stderr, flush=True)
    sink = getattr(_log_local, "sink", None)
    if sink is not None:
        sink.append(msg)


def parallel(fns, limit=8):
    """Run the callables concurrently (at most `limit` threads) and return their results in call order; the first
    exception is re-raised. The workers log into the calling thread's capture."""
    sink = getattr(_log_local, "sink", None)

    def run(fn):
        _log_local.sink = sink
        return fn()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(limit, len(fns)))) as pool:
        return list(pool.map(run, fns))


# ---------------------------------------------------------------------------
# Quantities and effective pod resources
# ---------------------------------------------------------------------------

def parse_quantity(q):
    """Parse a Kubernetes resource quantity ("100m", "1.5Gi", "1e3", 2) into a float of base units."""
    if q is None:
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    s = str(q).strip()
    for suffix, mult in _SUFFIXES:
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * mult
    return float(s)


def _res(container, kind, resource):
    return parse_quantity(container.get("resources", {}).get(kind, {}).get(resource))


def _has(container, kind, resource):
    return resource in container.get("resources", {}).get(kind, {})


def pod_effective(spec, kind, resource):
    """Effective pod request/limit as the scheduler computes it.

    max(sum(app containers) + sum(sidecars), max over init steps) + pod overhead,
    where sidecars are init containers with restartPolicy: Always.
    """
    inits = spec.get("initContainers", []) or []
    sidecars_total = sum(_res(c, kind, resource) for c in inits if c.get("restartPolicy") == "Always")
    total = sum(_res(c, kind, resource) for c in spec.get("containers", []) or []) + sidecars_total

    init_peak = 0.0
    running_sidecars = 0.0
    for c in inits:
        v = _res(c, kind, resource)
        if c.get("restartPolicy") == "Always":
            running_sidecars += v
            init_peak = max(init_peak, running_sidecars)
        else:
            init_peak = max(init_peak, v + running_sidecars)

    overhead = parse_quantity((spec.get("overhead") or {}).get(resource))
    return max(total, init_peak) + overhead


def long_running_containers(spec):
    """App containers plus sidecar init containers (the ones that consume resources for the pod's lifetime)."""
    sidecars = [c for c in (spec.get("initContainers") or []) if c.get("restartPolicy") == "Always"]
    return (spec.get("containers") or []) + sidecars


# ---------------------------------------------------------------------------
# Cluster access
# ---------------------------------------------------------------------------

class KubectlError(Exception):
    pass


def kubectl(context, args, timeout=120, kubeconfig=None, stdin=None):
    cmd = (["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else [])
           + (["--context", context] if context else []) + args)
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KubectlError(f"timeout: {' '.join(cmd)}")
    if p.returncode != 0:
        raise KubectlError(p.stderr.strip() or f"exit {p.returncode}: {' '.join(cmd)}")
    return p.stdout


def kget(context, resource, all_namespaces=True, kubeconfig=None):
    args = ["get", resource, "-o", "json"] + (["-A"] if all_namespaces else [])
    return json.loads(kubectl(context, args, kubeconfig=kubeconfig)).get("items", [])


class Prometheus:
    """Instant and range queries against Prometheus, via the API server service proxy or a direct URL."""

    MAX_POINTS = 11000  # Prometheus refuses a range query with more points than this per series

    def __init__(self, context, service=None, url=None, token=None):
        self.context, self.service, self.url, self.token = context, service, url, token

    def _api(self, endpoint, params):
        params = urllib.parse.urlencode(params)
        if self.url:
            req = urllib.request.Request(f"{self.url.rstrip('/')}/api/v1/{endpoint}?{params}")
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.load(r)
        else:
            ns, svc = self.service.split("/", 1)
            path = f"/api/v1/namespaces/{ns}/services/{svc}/proxy/api/v1/{endpoint}?{params}"
            body = json.loads(kubectl(self.context, ["get", "--raw", path], timeout=300))
        if body.get("status") != "success":
            raise KubectlError(f"prometheus {endpoint} failed: {body.get('error')}")
        return body["data"]["result"]

    def query(self, promql):
        return self._api("query", {"query": promql})

    def scalar(self, promql):
        res = self.query(promql)
        return float(res[0]["value"][1]) if res else None

    def range_by_namespace(self, promql, start, end, step, max_points=MAX_POINTS):
        """{namespace: [sample, ...]} of a `sum by (namespace)` expression evaluated every `step` seconds from
        `start` to `end` (epoch seconds), in several requests of at most max_points steps each (never more than
        MAX_POINTS)."""
        out = {}
        for s, e in range_chunks(start, end, step, min(max_points, self.MAX_POINTS)):
            for r in self._api("query_range", {"query": promql, "start": s, "end": e, "step": step}):
                out.setdefault(r["metric"].get("namespace", ""), []).extend(float(v[1]) for v in r["values"])
        return out


def range_chunks(start, end, step, max_points):
    """Split [start, end] into consecutive (start, end) ranges of at most max_points evaluation steps each, on the
    same step grid as one query over the whole range."""
    points = int((end - start) // step) + 1
    chunks = []
    for first in range(0, points, max_points):
        last = min(first + max_points, points) - 1
        chunks.append((start + first * step, start + last * step))
    return chunks


# Per-namespace series read from Prometheus over --window at --step (one range query each, so Prometheus evaluates
# every expression once per step) and the report columns computed from the samples. A subquery per statistic
# (avg_over_time, quantile_over_time, max_over_time of the same expression) made Prometheus evaluate the usage
# expressions three times over and was the slow part of a collection.
PROM_SERIES = {
    "cpu_usage": ('sum by (namespace) (rate(container_cpu_usage_seconds_total'
                  '{container!="",container!="POD",image!=""}[5m]))'),
    "mem_usage": ('sum by (namespace) (container_memory_working_set_bytes'
                  '{container!="",container!="POD",image!=""})'),
    "cpu_requests": ('sum by (namespace) (kube_pod_container_resource_requests{resource="cpu"} '
                     'and on(namespace, pod) (kube_pod_status_phase{phase=~"Running|Pending"} == 1))'),
    "mem_requests": ('sum by (namespace) (kube_pod_container_resource_requests{resource="memory"} '
                     'and on(namespace, pod) (kube_pod_status_phase{phase=~"Running|Pending"} == 1))'),
}
# Samples (steps x namespaces) per range request: bounds the size of one response, and the memory to parse it, on
# a cluster with hundreds of namespaces (about 150 bytes per sample once parsed); the window is read in several
# requests when it holds more.
PROM_SAMPLES_PER_REQUEST = 200_000
PROM_COLUMNS = {  # series -> [(statistic, namespace column)]; memory series are bytes, columns GiB
    "cpu_usage": [("avg", "cpu_usage_avg"), ("p95", "cpu_usage_p95"), ("max", "cpu_usage_max")],
    "mem_usage": [("avg", "mem_usage_avg_gib"), ("p95", "mem_usage_p95_gib"), ("max", "mem_usage_max_gib")],
    "cpu_requests": [("max", "cpu_requests_peak")],
    "mem_requests": [("max", "mem_requests_peak_gib")],
}


def series_stats(samples):
    """avg, p95 and max of the samples, as Prometheus computes avg_over_time, quantile_over_time(0.95) and
    max_over_time (the quantile interpolates linearly between the two nearest ranks)."""
    if not samples:
        return {"avg": 0.0, "p95": 0.0, "max": 0.0}
    s = sorted(samples)
    rank = 0.95 * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return {"avg": sum(s) / len(s), "p95": s[lo] + (s[hi] - s[lo]) * (rank - lo), "max": s[-1]}


def load_rancher_projects(local_context=None, local_kubeconfig=None):
    """Map "<cluster-id>:<project-id>" -> project display name, and cluster-id -> cluster display name."""
    projects, clusters = {}, {}
    for p in kget(local_context, "projects.management.cattle.io", kubeconfig=local_kubeconfig):
        md = p["metadata"]
        projects[f"{md['namespace']}:{md['name']}"] = p.get("spec", {}).get("displayName", md["name"])
    for c in kget(local_context, "clusters.management.cattle.io", all_namespaces=False,
                  kubeconfig=local_kubeconfig):
        clusters[c["metadata"]["name"]] = c.get("spec", {}).get("displayName", c["metadata"]["name"])
    return projects, clusters


def names_from_configmap(cm):
    """Project and cluster name maps from the ConfigMap written by publish_rancher_names.py."""
    data = cm.get("data") or {}
    return json.loads(data.get("projects.json") or "{}"), json.loads(data.get("clusters.json") or "{}")


def load_names_configmap(context, ref):
    namespace, name = ref.split("/", 1)
    return names_from_configmap(json.loads(kubectl(context, ["get", "configmap", name, "-n", namespace, "-o", "json"])))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

NS_NUMERIC = [
    "pods_running", "pods_pending", "pods_besteffort", "containers",
    "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit", "containers_cpu_limit",
    "cpu_requests", "cpu_limits", "mem_requests_gib", "mem_limits_gib",
    "cpu_requests_pending", "mem_requests_pending_gib",
    "resourcequotas", "limitranges", "hpas",
    "cpu_usage_now", "mem_usage_now_gib",
    "cpu_usage_avg", "cpu_usage_p95", "cpu_usage_max",
    "mem_usage_avg_gib", "mem_usage_p95_gib", "mem_usage_max_gib",
    "cpu_requests_peak", "mem_requests_peak_gib",
    # part of the requests that sits on nodes that don't count as capacity (tainted control-plane / etcd nodes,
    # cordoned nodes): counted in requests and quota, but not against the schedulable nodes
    "cpu_requests_excluded_nodes", "mem_requests_excluded_nodes_gib",
]
NS_FIELDS = ["cluster", "category", "project_id", "project", "namespace"] + NS_NUMERIC


def classify(ns_name, project_name, has_project, system_re, system_project_re=DEFAULT_SYSTEM_PROJECT_RE):
    if (has_project and system_project_re.match(project_name or "")) or system_re.match(ns_name):
        return "system"
    return "tenant" if has_project else "unassigned"


def context_label(context):
    """Name for a kubectl context; None means the current context, or the in-cluster service account."""
    if context:
        return context
    try:
        return kubectl(None, ["config", "current-context"]).strip() or "in-cluster"
    except KubectlError:
        return "in-cluster"


def collect_cluster(context, args, rancher_projects, rancher_clusters):
    label = context_label(context)
    log(f"[{label}] collecting")

    def hpas_or_none():
        try:
            return kget(context, "horizontalpodautoscalers.autoscaling")
        except KubectlError:
            return []

    def metrics_snapshot():
        try:
            return json.loads(kubectl(context, ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"]))
        except (KubectlError, json.JSONDecodeError) as e:
            log(f"[{label}] metrics-server unavailable, skipping usage snapshot: {first_line(e)}")
            return None

    # independent reads, so they run at the same time (the pod list dominates on a large cluster); a few at a time,
    # as every read is a kubectl process in the dashboard pod's memory
    nodes, namespaces, pods, quotas, limitranges, hpas, metrics = parallel(limit=4, fns=[
        lambda: kget(context, "nodes", all_namespaces=False),
        lambda: kget(context, "namespaces", all_namespaces=False),
        lambda: kget(context, "pods"),
        lambda: kget(context, "resourcequotas"),
        lambda: kget(context, "limitranges"),
        hpas_or_none,
        metrics_snapshot,
    ])

    system_re = re.compile(args.system_ns_regex)
    system_project_re = re.compile(args.system_project_regex, re.IGNORECASE)
    ns_rows = {}
    rancher_cluster_id = None
    for ns in namespaces:
        md = ns["metadata"]
        name = md["name"]
        project_ref = (md.get("annotations") or {}).get(RANCHER_PROJECT_ANNOTATION, "")
        project_id, project_name = "", ""
        if project_ref:
            rancher_cluster_id = project_ref.split(":", 1)[0]
            project_id = project_ref
            project_name = rancher_projects.get(project_ref, project_ref.split(":")[-1])
        elif args.project_label and (md.get("labels") or {}).get(args.project_label):
            project_name = md["labels"][args.project_label]
            project_id = f"label:{project_name}"
        row = {k: 0.0 for k in NS_NUMERIC}
        row.update(namespace=name, project_id=project_id, project=project_name,
                   category=classify(name, project_name, bool(project_id), system_re, system_project_re))
        ns_rows[name] = row

    excluded_nodes = {n["metadata"]["name"] for n in nodes if not node_schedulable(n)}
    cluster_name = args.cluster_name or rancher_clusters.get(rancher_cluster_id) or label
    for row in ns_rows.values():
        row["cluster"] = cluster_name

    for p in pods:
        phase = p.get("status", {}).get("phase")
        if phase in ("Succeeded", "Failed"):
            continue
        row = ns_rows.get(p["metadata"]["namespace"])
        if row is None:
            continue
        spec = p["spec"]
        row["pods_running" if phase == "Running" else "pods_pending"] += 1
        if p.get("status", {}).get("qosClass") == "BestEffort":
            row["pods_besteffort"] += 1
        cpu_req = pod_effective(spec, "requests", "cpu")
        mem_req = pod_effective(spec, "requests", "memory") / GIB
        row["cpu_requests"] += cpu_req
        row["mem_requests_gib"] += mem_req
        if not spec.get("nodeName"):
            row["cpu_requests_pending"] += cpu_req
            row["mem_requests_pending_gib"] += mem_req
        elif spec["nodeName"] in excluded_nodes:
            row["cpu_requests_excluded_nodes"] += cpu_req
            row["mem_requests_excluded_nodes_gib"] += mem_req
        row["cpu_limits"] += pod_effective(spec, "limits", "cpu")
        row["mem_limits_gib"] += pod_effective(spec, "limits", "memory") / GIB
        for c in long_running_containers(spec):
            row["containers"] += 1
            row["containers_no_cpu_request"] += not _has(c, "requests", "cpu")
            row["containers_no_mem_request"] += not _has(c, "requests", "memory")
            row["containers_no_mem_limit"] += not _has(c, "limits", "memory")
            row["containers_cpu_limit"] += _has(c, "limits", "cpu")

    for items, key in ((quotas, "resourcequotas"), (limitranges, "limitranges"), (hpas, "hpas")):
        for o in items:
            row = ns_rows.get(o["metadata"]["namespace"])
            if row:
                row[key] += 1

    # metrics-server snapshot
    for pm in (metrics or {}).get("items", []):
        row = ns_rows.get(pm["metadata"]["namespace"])
        if row is None:
            continue
        for c in pm.get("containers", []):
            row["cpu_usage_now"] += parse_quantity(c["usage"].get("cpu"))
            row["mem_usage_now_gib"] += parse_quantity(c["usage"].get("memory")) / GIB

    # Prometheus history
    prom_coverage_days = ""
    if args.prometheus != "none":
        prom = Prometheus(context, service=args.prom_service, url=args.prom_url, token=os.environ.get("PROM_TOKEN"))
        try:
            lowest = prom.scalar("min(prometheus_tsdb_lowest_timestamp_seconds)")
            if lowest:
                prom_coverage_days = round((datetime.now(timezone.utc).timestamp() - lowest) / 86400, 1)
                log(f"[{label}] prometheus has {prom_coverage_days} days of data (window {args.window})")
                if prom_coverage_days < window_days(args.window):
                    log(f"[{label}] WARNING: less history than --window; P95/max cover only available data")
            end = datetime.now(timezone.utc).timestamp()
            start, step = end - duration_seconds(args.window), duration_seconds(args.step)
            max_points = max(1, PROM_SAMPLES_PER_REQUEST // max(1, len(ns_rows)))
            results = parallel([lambda q=q: prom.range_by_namespace(q, start, end, step, max_points)
                                for q in PROM_SERIES.values()])
            for name, by_ns in zip(PROM_SERIES, results):
                div = GIB if name.startswith("mem_") else 1
                for ns, samples in by_ns.items():
                    if ns in ns_rows:
                        stats = series_stats(samples)
                        for stat, col in PROM_COLUMNS[name]:
                            ns_rows[ns][col] = stats[stat] / div
        except (KubectlError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            log(f"[{label}] prometheus unavailable, skipping history: {first_line(e)}")

    # kube-state-metrics only exports app-container requests, so the Prometheus peak misses init/sidecar
    # containers; never report a peak below the current effective requests.
    for row in ns_rows.values():
        row["cpu_requests_peak"] = max(row["cpu_requests_peak"], row["cpu_requests"])
        row["mem_requests_peak_gib"] = max(row["mem_requests_peak_gib"], row["mem_requests_gib"])

    cluster_row = summarize_cluster(cluster_name, label, nodes, ns_rows.values(), prom_coverage_days)
    return list(ns_rows.values()), cluster_row


def duration_seconds(s):
    """Seconds in a duration like 5m, 7d or 2w (the units Prometheus accepts for --window and --step)."""
    m = re.fullmatch(r"(\d+)([smhdwy])", s or "")
    if not m:
        raise ValueError(f"invalid duration: {s} (use e.g. 15m, 7d, 2w)")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}[m.group(2)]


def window_days(w):
    return duration_seconds(w) / 86400


def first_line(e):
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def node_schedulable(node):
    if node.get("spec", {}).get("unschedulable"):
        return False
    taints = node.get("spec", {}).get("taints") or []
    return not any(t.get("effect") in ("NoSchedule", "NoExecute") for t in taints)


CLUSTER_FIELDS = [
    "cluster", "context", "nodes", "nodes_schedulable",
    "alloc_cpu", "alloc_mem_gib", "alloc_cpu_schedulable", "alloc_mem_gib_schedulable",
    "cpu_requests_total", "mem_requests_gib_total",
    "cpu_requests_pending", "mem_requests_pending_gib",
    "cpu_requests_tenant", "mem_requests_gib_tenant",
    "cpu_requests_system", "mem_requests_gib_system",
    "cpu_requests_unassigned", "mem_requests_gib_unassigned",
    "cpu_requests_excluded_nodes", "mem_requests_excluded_nodes_gib",
    "cpu_requests_excluded_system", "mem_requests_excluded_system_gib",
    "cpu_requests_pct_of_schedulable", "mem_requests_pct_of_schedulable",
    "largest_node_cpu", "largest_node_mem_gib", "alloc_cpu_n1", "alloc_mem_gib_n1",
    "cpu_for_projects_n1", "mem_gib_for_projects_n1",
    "cpu_usage_now", "mem_usage_now_gib",
    "containers", "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit",
    "namespaces_tenant", "namespaces_unassigned", "prometheus_days_of_data",
]


def summarize_cluster(cluster_name, context, nodes, ns_rows, prom_days):
    sched = [n for n in nodes if node_schedulable(n)]

    def alloc(ns, res):
        return sum(parse_quantity(n.get("status", {}).get("allocatable", {}).get(res)) for n in ns)

    c = {
        "cluster": cluster_name, "context": context,
        "nodes": len(nodes), "nodes_schedulable": len(sched),
        "alloc_cpu": alloc(nodes, "cpu"), "alloc_mem_gib": alloc(nodes, "memory") / GIB,
        "alloc_cpu_schedulable": alloc(sched, "cpu"), "alloc_mem_gib_schedulable": alloc(sched, "memory") / GIB,
        "prometheus_days_of_data": prom_days,
    }
    for k in ("cpu_requests_total", "mem_requests_gib_total", "cpu_requests_pending", "mem_requests_pending_gib",
              "cpu_usage_now", "mem_usage_now_gib",
              "containers", "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit",
              "namespaces_tenant", "namespaces_unassigned"):
        c[k] = 0.0
    for cat in ("tenant", "system", "unassigned"):
        c[f"cpu_requests_{cat}"] = c[f"mem_requests_gib_{cat}"] = 0.0
    for k in ("cpu_requests_excluded_nodes", "mem_requests_excluded_nodes_gib",
              "cpu_requests_excluded_system", "mem_requests_excluded_system_gib"):
        c[k] = 0.0
    for r in ns_rows:
        cat = r["category"]
        c[f"cpu_requests_{cat}"] += r["cpu_requests"]
        c[f"mem_requests_gib_{cat}"] += r["mem_requests_gib"]
        c["cpu_requests_excluded_nodes"] += r.get("cpu_requests_excluded_nodes", 0.0)
        c["mem_requests_excluded_nodes_gib"] += r.get("mem_requests_excluded_nodes_gib", 0.0)
        if cat == "system":
            c["cpu_requests_excluded_system"] += r.get("cpu_requests_excluded_nodes", 0.0)
            c["mem_requests_excluded_system_gib"] += r.get("mem_requests_excluded_nodes_gib", 0.0)
        c["cpu_requests_total"] += r["cpu_requests"]
        c["mem_requests_gib_total"] += r["mem_requests_gib"]
        for k in ("cpu_requests_pending", "mem_requests_pending_gib", "cpu_usage_now", "mem_usage_now_gib",
                  "containers",
                  "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit"):
            c[k] += r[k]
        if cat in ("tenant", "unassigned"):
            c[f"namespaces_{cat}"] += 1
    # requests on the schedulable nodes only: pending pods count against quota but occupy no node capacity, and pods
    # on excluded nodes (tainted control plane, cordoned) occupy capacity that isn't counted as allocatable
    c["cpu_requests_pct_of_schedulable"] = pct(
        c["cpu_requests_total"] - c["cpu_requests_pending"] - c["cpu_requests_excluded_nodes"], c["alloc_cpu_schedulable"])
    c["mem_requests_pct_of_schedulable"] = pct(
        c["mem_requests_gib_total"] - c["mem_requests_pending_gib"] - c["mem_requests_excluded_nodes_gib"],
        c["alloc_mem_gib_schedulable"])
    # N+1: what is left if the largest schedulable node fails, and of that what platform components (the `system`
    # requests on schedulable nodes) leave for projects. With one schedulable node, nothing survives a node failure.
    c["largest_node_cpu"] = max((alloc([n], "cpu") for n in sched), default=0.0)
    c["largest_node_mem_gib"] = max((alloc([n], "memory") / GIB for n in sched), default=0.0)
    c["alloc_cpu_n1"] = c["alloc_cpu_schedulable"] - c["largest_node_cpu"]
    c["alloc_mem_gib_n1"] = c["alloc_mem_gib_schedulable"] - c["largest_node_mem_gib"]
    c["cpu_for_projects_n1"] = max(0.0, c["alloc_cpu_n1"] - (c["cpu_requests_system"] - c["cpu_requests_excluded_system"]))
    c["mem_gib_for_projects_n1"] = max(0.0, c["alloc_mem_gib_n1"] - (c["mem_requests_gib_system"] -
                                                                     c["mem_requests_excluded_system_gib"]))
    return c


def pct(a, b):
    return round(100 * a / b, 1) if b else ""


PROJECT_NUMERIC = [k for k in NS_NUMERIC if k not in ("resourcequotas", "limitranges")]
PROJECT_FIELDS = ["cluster", "category", "project_id", "project", "namespaces"] + PROJECT_NUMERIC + [
    "cpu_request_efficiency_pct", "mem_request_efficiency_pct",
]


def aggregate_projects(ns_rows):
    """Aggregate namespaces to projects. Namespace-level P95/max are summed, which overstates the
    project-level P95/max slightly (peaks don't always coincide) — conservative for sizing."""
    agg = {}
    for r in ns_rows:
        key = (r["cluster"], r["project_id"] or f"ns:{r['namespace']}")
        a = agg.setdefault(key, {
            "cluster": r["cluster"], "category": r["category"], "project_id": r["project_id"],
            "project_key": key[1],
            "project": r["project"] or f"(no project) {r['namespace']}", "namespaces": 0,
            **{k: 0.0 for k in PROJECT_NUMERIC},
        })
        a["namespaces"] += 1
        for k in PROJECT_NUMERIC:
            a[k] += r[k]
    for a in agg.values():
        cpu_used = a["cpu_usage_p95"] or a["cpu_usage_now"]
        mem_used = a["mem_usage_p95_gib"] or a["mem_usage_now_gib"]
        a["cpu_request_efficiency_pct"] = pct(cpu_used, a["cpu_requests"])
        a["mem_request_efficiency_pct"] = pct(mem_used, a["mem_requests_gib"])
    return sorted(agg.values(), key=lambda a: (a["cluster"], a["category"], -a["cpu_requests"]))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return v


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(r.get(k, "")) for k in fields})


def print_summary(clusters, projects, top):
    for c in clusters:
        print(f"\n== {c['cluster']} ({c['context']}) ==")
        print(f"  nodes {c['nodes']} ({c['nodes_schedulable']} schedulable) | "
              f"allocatable {c['alloc_cpu_schedulable']:.1f} vCPU / {c['alloc_mem_gib_schedulable']:.1f} GiB")
        print(f"  scheduled requests {c['cpu_requests_total'] - c['cpu_requests_pending']:.1f} vCPU "
              f"({c['cpu_requests_pct_of_schedulable']}%) / "
              f"{c['mem_requests_gib_total'] - c['mem_requests_pending_gib']:.1f} GiB "
              f"({c['mem_requests_pct_of_schedulable']}%)")
        if c["cpu_requests_pending"] or c["mem_requests_pending_gib"]:
            print(f"  pending requests   {c['cpu_requests_pending']:.1f} vCPU / "
                  f"{c['mem_requests_pending_gib']:.1f} GiB (unschedulable or waiting)")
        print(f"    all requests by category: tenant {c['cpu_requests_tenant']:.1f} vCPU / {c['mem_requests_gib_tenant']:.1f} GiB | "
              f"system {c['cpu_requests_system']:.1f} / {c['mem_requests_gib_system']:.1f} | "
              f"unassigned {c['cpu_requests_unassigned']:.1f} / {c['mem_requests_gib_unassigned']:.1f}")
        print(f"  usage now {c['cpu_usage_now']:.1f} vCPU / {c['mem_usage_now_gib']:.1f} GiB")
        n = c["containers"] or 1
        print(f"  containers {int(c['containers'])}: no cpu request {int(c['containers_no_cpu_request'])} "
              f"({100 * c['containers_no_cpu_request'] / n:.0f}%), no mem request "
              f"{int(c['containers_no_mem_request'])}, no mem limit {int(c['containers_no_mem_limit'])}")
        rows = [p for p in projects if p["cluster"] == c["cluster"] and p["category"] != "system"
                and (p["pods_running"] or p["pods_pending"])][:top]
        if rows:
            used = "p95" if c["prometheus_days_of_data"] != "" else "now"
            print(f"  {'project (non-system, with pods)':<32} {'ns':>3} {'cpu req':>8} {'cpu ' + used:>8} "
                  f"{'mem req':>8} {'mem ' + used:>8}")
            for p in rows:
                print(f"  {p['project'][:32]:<32} {p['namespaces']:>3} {p['cpu_requests']:>8.2f} "
                      f"{p['cpu_usage_p95'] or p['cpu_usage_now']:>8.2f} {p['mem_requests_gib']:>8.2f} "
                      f"{p['mem_usage_p95_gib'] or p['mem_usage_now_gib']:>8.2f}")


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", action="append", default=[],
                    help="kubectl context to scan (repeatable). Default: current context, or in-cluster")
    ap.add_argument("--cluster-name",
                    help="display name for the cluster (default: Rancher cluster name, else the context name)")
    ap.add_argument("--rancher-local-context",
                    help="context of the Rancher local (management) cluster, to resolve Project display names")
    ap.add_argument("--rancher-local-kubeconfig",
                    help="kubeconfig file of the Rancher local cluster (instead of, or together with, "
                         "--rancher-local-context; without a context its current-context is used)")
    ap.add_argument("--rancher-local-self", action="store_true",
                    help="the scanned cluster is the Rancher local cluster: read Project/cluster names from it "
                         "(no separate kubeconfig needed)")
    ap.add_argument("--rancher-names-configmap", metavar="NAMESPACE/NAME",
                    help=f"read Project/cluster names from this ConfigMap on each scanned cluster (published by "
                         f"publish_rancher_names.py via Fleet; usually <ns>/{NAMES_CONFIGMAP})")
    ap.add_argument("--project-label",
                    help="namespace label to group by when there is no Rancher Project (e.g. on non-Rancher AKS)")
    ap.add_argument("--prometheus", choices=["auto", "none"], default="auto",
                    help="query Prometheus for usage history (default: auto, skipped if unreachable)")
    ap.add_argument("--prom-service", default=DEFAULT_PROM_SERVICE,
                    help=f"Prometheus service for API server proxy as ns/[scheme:]name:port (default: {DEFAULT_PROM_SERVICE})")
    ap.add_argument("--prom-url", help="direct Prometheus URL instead of the service proxy (token in $PROM_TOKEN)")
    ap.add_argument("--window", default="7d", help="usage history window (default: 7d; Rancher Monitoring keeps ~10d)")
    ap.add_argument("--step", default="5m", help="resolution of the usage history (default: 5m)")
    ap.add_argument("--system-ns-regex", default=DEFAULT_SYSTEM_NS_REGEX,
                    help="namespaces treated as platform/system")
    ap.add_argument("--system-project-regex", default=DEFAULT_SYSTEM_PROJECT_REGEX,
                    help="Rancher Project names (ignoring case) whose namespaces are treated as platform/system "
                         "(default: System and any name containing 'platform')")
    return ap


def collect(args):
    """Run one collection over all contexts. Returns a report dict; raises ValueError on bad arguments."""
    lines = []
    _log_local.sink = lines
    started = datetime.now(timezone.utc)
    try:
        local_kubeconfig = (os.path.expanduser(args.rancher_local_kubeconfig)
                            if args.rancher_local_kubeconfig else None)
        if local_kubeconfig and not os.path.isfile(local_kubeconfig):
            raise ValueError(f"--rancher-local-kubeconfig: file not found: {local_kubeconfig}")

        if args.rancher_names_configmap and args.rancher_names_configmap.count("/") != 1:
            raise ValueError("--rancher-names-configmap: expected NAMESPACE/NAME")
        for opt in ("window", "step"):
            try:
                duration_seconds(getattr(args, opt))
            except ValueError as e:
                raise ValueError(f"--{opt}: {e}")
        local_context = args.rancher_local_context
        if args.rancher_local_self:
            if local_context or local_kubeconfig or len(args.context) > 1:
                raise ValueError("--rancher-local-self scans a single cluster; with several contexts or another "
                                 "Rancher local cluster use --rancher-local-context / --rancher-local-kubeconfig")
            local_context = args.context[0] if args.context else None

        rancher_projects, rancher_clusters = {}, {}
        if args.rancher_local_self or local_context or local_kubeconfig:
            try:
                rancher_projects, rancher_clusters = load_rancher_projects(local_context, local_kubeconfig)
                log(f"loaded {len(rancher_projects)} Rancher projects, {len(rancher_clusters)} clusters")
            except KubectlError as e:
                log(f"could not read Rancher projects: {first_line(e)}")

        def one_cluster(ctx):
            projects, clusters = rancher_projects, rancher_clusters
            if args.rancher_names_configmap:
                try:
                    cm_projects, cm_clusters = load_names_configmap(ctx, args.rancher_names_configmap)
                    projects, clusters = {**cm_projects, **projects}, {**cm_clusters, **clusters}
                    log(f"[{ctx or 'current'}] {len(cm_projects)} Rancher project names from "
                        f"{args.rancher_names_configmap}")
                except (KubectlError, ValueError) as e:
                    log(f"[{ctx or 'current'}] could not read {args.rancher_names_configmap}: {first_line(e)}")
            try:
                return collect_cluster(ctx, args, projects, clusters)
            except KubectlError as e:
                log(f"[{ctx or 'current'}] FAILED: {first_line(e)}")
                return None

        all_ns, all_clusters = [], []
        # clusters are independent too: scan a few at a time (each scan runs several kubectl processes), results
        # in --context order
        for result in parallel([lambda c=c: one_cluster(c) for c in args.context or [None]], limit=4):
            if result:
                all_ns.extend(result[0])
                all_clusters.append(result[1])
    finally:
        _log_local.sink = None

    all_ns.sort(key=lambda r: (r["cluster"], r["category"], r["project"], r["namespace"]))
    finished = datetime.now(timezone.utc)
    return {
        "collected_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "settings": {"window": args.window, "step": args.step, "prometheus": args.prometheus},
        "clusters": all_clusters,
        "projects": aggregate_projects(all_ns),
        "namespaces": all_ns,
        "log": lines,
    }


def write_report_csvs(report, out):
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "namespaces.csv"), NS_FIELDS, report["namespaces"])
    write_csv(os.path.join(out, "projects.csv"), PROJECT_FIELDS, report["projects"])
    write_csv(os.path.join(out, "clusters.csv"), CLUSTER_FIELDS, report["clusters"])


def main():
    ap = build_parser()
    ap.add_argument("--out", default="discovery-output", help="output directory (default: discovery-output)")
    ap.add_argument("--top", type=int, default=15, help="projects per cluster in console summary")
    args = ap.parse_args()

    try:
        report = collect(args)
    except ValueError as e:
        log(str(e))
        return 2
    if not report["clusters"]:
        log("no cluster data collected")
        return 1

    write_report_csvs(report, args.out)
    print_summary(report["clusters"], report["projects"], args.top)
    print(f"\nCSV written to {args.out}/ (namespaces.csv, projects.csv, clusters.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
