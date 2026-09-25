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
import csv
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GIB = 2**30

RANCHER_PROJECT_ANNOTATION = "field.cattle.io/projectId"  # "<cluster-id>:<project-id>"

DEFAULT_SYSTEM_NS_REGEX = (
    r"^(kube-.*|cattle-.*|fleet-.*|rancher-.*|calico-.*|tigera-.*|cis-operator-system|"
    r"longhorn-system|gatekeeper-system|kyverno|cert-manager|ingress-nginx|"
    r"app-routing-system|local|p-[a-z0-9]{5}|c-[a-z0-9-]+|u-[a-z0-9]+|user-[a-z0-9]+)$"
)

DEFAULT_PROM_SERVICE = "cattle-monitoring-system/http:rancher-monitoring-prometheus:9090"

_SUFFIXES = [
    ("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("Ti", 2**40), ("Pi", 2**50), ("Ei", 2**60),
    ("n", 1e-9), ("u", 1e-6), ("m", 1e-3),
    ("k", 1e3), ("M", 1e6), ("G", 1e9), ("T", 1e12), ("P", 1e15), ("E", 1e18),
]


def log(msg):
    print(msg, file=sys.stderr)


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


def kubectl(context, args, timeout=120):
    cmd = ["kubectl"] + (["--context", context] if context else []) + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KubectlError(f"timeout: {' '.join(cmd)}")
    if p.returncode != 0:
        raise KubectlError(p.stderr.strip() or f"exit {p.returncode}: {' '.join(cmd)}")
    return p.stdout


def kget(context, resource, all_namespaces=True):
    args = ["get", resource, "-o", "json"] + (["-A"] if all_namespaces else [])
    return json.loads(kubectl(context, args)).get("items", [])


class Prometheus:
    """Instant queries against Prometheus, via the API server service proxy or a direct URL."""

    def __init__(self, context, service=None, url=None, token=None):
        self.context, self.service, self.url, self.token = context, service, url, token

    def query(self, promql):
        params = urllib.parse.urlencode({"query": promql})
        if self.url:
            req = urllib.request.Request(f"{self.url.rstrip('/')}/api/v1/query?{params}")
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.load(r)
        else:
            ns, svc = self.service.split("/", 1)
            path = f"/api/v1/namespaces/{ns}/services/{svc}/proxy/api/v1/query?{params}"
            body = json.loads(kubectl(self.context, ["get", "--raw", path], timeout=300))
        if body.get("status") != "success":
            raise KubectlError(f"prometheus query failed: {body.get('error')}")
        return body["data"]["result"]

    def by_namespace(self, promql):
        return {r["metric"].get("namespace", ""): float(r["value"][1]) for r in self.query(promql)}

    def scalar(self, promql):
        res = self.query(promql)
        return float(res[0]["value"][1]) if res else None


def prom_queries(window, step):
    cpu = ('sum by (namespace) (rate(container_cpu_usage_seconds_total'
           '{container!="",container!="POD",image!=""}[5m]))')
    mem = ('sum by (namespace) (container_memory_working_set_bytes'
           '{container!="",container!="POD",image!=""})')
    active = '(kube_pod_status_phase{phase=~"Running|Pending"} == 1)'
    cpu_req = (f'sum by (namespace) (kube_pod_container_resource_requests{{resource="cpu"}} '
               f'and on(namespace, pod) {active})')
    mem_req = (f'sum by (namespace) (kube_pod_container_resource_requests{{resource="memory"}} '
               f'and on(namespace, pod) {active})')
    sub = f"[{window}:{step}]"
    return {
        "cpu_usage_avg": f"avg_over_time({cpu}{sub})",
        "cpu_usage_p95": f"quantile_over_time(0.95, {cpu}{sub})",
        "cpu_usage_max": f"max_over_time({cpu}{sub})",
        "mem_usage_avg": f"avg_over_time({mem}{sub})",
        "mem_usage_p95": f"quantile_over_time(0.95, {mem}{sub})",
        "mem_usage_max": f"max_over_time({mem}{sub})",
        "cpu_requests_peak": f"max_over_time({cpu_req}{sub})",
        "mem_requests_peak": f"max_over_time({mem_req}{sub})",
    }


def load_rancher_projects(local_context):
    """Map "<cluster-id>:<project-id>" -> project display name, and cluster-id -> cluster display name."""
    projects, clusters = {}, {}
    for p in kget(local_context, "projects.management.cattle.io"):
        md = p["metadata"]
        projects[f"{md['namespace']}:{md['name']}"] = p.get("spec", {}).get("displayName", md["name"])
    for c in kget(local_context, "clusters.management.cattle.io", all_namespaces=False):
        clusters[c["metadata"]["name"]] = c.get("spec", {}).get("displayName", c["metadata"]["name"])
    return projects, clusters


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
]
NS_FIELDS = ["cluster", "category", "project_id", "project", "namespace"] + NS_NUMERIC


def classify(ns_name, project_name, has_project, system_re):
    if project_name == "System" or system_re.match(ns_name):
        return "system"
    return "tenant" if has_project else "unassigned"


def collect_cluster(context, args, rancher_projects, rancher_clusters):
    log(f"[{context}] collecting")
    nodes = kget(context, "nodes", all_namespaces=False)
    namespaces = kget(context, "namespaces", all_namespaces=False)
    pods = kget(context, "pods")
    quotas = kget(context, "resourcequotas")
    limitranges = kget(context, "limitranges")
    try:
        hpas = kget(context, "horizontalpodautoscalers.autoscaling")
    except KubectlError:
        hpas = []

    system_re = re.compile(args.system_ns_regex)
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
                   category=classify(name, project_name, bool(project_id), system_re))
        ns_rows[name] = row

    cluster_name = rancher_clusters.get(rancher_cluster_id, context) if rancher_cluster_id else context
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
    try:
        metrics = json.loads(kubectl(context, ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"]))
        for pm in metrics.get("items", []):
            row = ns_rows.get(pm["metadata"]["namespace"])
            if row is None:
                continue
            for c in pm.get("containers", []):
                row["cpu_usage_now"] += parse_quantity(c["usage"].get("cpu"))
                row["mem_usage_now_gib"] += parse_quantity(c["usage"].get("memory")) / GIB
    except (KubectlError, json.JSONDecodeError) as e:
        log(f"[{context}] metrics-server unavailable, skipping usage snapshot: {first_line(e)}")

    # Prometheus history
    prom_coverage_days = ""
    if args.prometheus != "none":
        prom = Prometheus(context, service=args.prom_service, url=args.prom_url, token=os.environ.get("PROM_TOKEN"))
        try:
            lowest = prom.scalar("min(prometheus_tsdb_lowest_timestamp_seconds)")
            if lowest:
                prom_coverage_days = round((datetime.now(timezone.utc).timestamp() - lowest) / 86400, 1)
                log(f"[{context}] prometheus has {prom_coverage_days} days of data (window {args.window})")
                if prom_coverage_days < window_days(args.window):
                    log(f"[{context}] WARNING: less history than --window; P95/max cover only available data")
            for key, q in prom_queries(args.window, args.step).items():
                col = key + ("_gib" if key.startswith("mem_") else "")
                div = GIB if key.startswith("mem_") else 1
                for ns, v in prom.by_namespace(q).items():
                    if ns in ns_rows:
                        ns_rows[ns][col] = v / div
        except (KubectlError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            log(f"[{context}] prometheus unavailable, skipping history: {first_line(e)}")

    # kube-state-metrics only exports app-container requests, so the Prometheus peak misses init/sidecar
    # containers; never report a peak below the current effective requests.
    for row in ns_rows.values():
        row["cpu_requests_peak"] = max(row["cpu_requests_peak"], row["cpu_requests"])
        row["mem_requests_peak_gib"] = max(row["mem_requests_peak_gib"], row["mem_requests_gib"])

    cluster_row = summarize_cluster(cluster_name, context, nodes, ns_rows.values(), prom_coverage_days)
    return list(ns_rows.values()), cluster_row


def window_days(w):
    units = {"s": 1 / 86400, "m": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7, "y": 365}
    m = re.fullmatch(r"(\d+)([smhdwy])", w)
    return int(m.group(1)) * units[m.group(2)] if m else 0


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
    "cpu_requests_pct_of_schedulable", "mem_requests_pct_of_schedulable",
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
    for r in ns_rows:
        cat = r["category"]
        c[f"cpu_requests_{cat}"] += r["cpu_requests"]
        c[f"mem_requests_gib_{cat}"] += r["mem_requests_gib"]
        c["cpu_requests_total"] += r["cpu_requests"]
        c["mem_requests_gib_total"] += r["mem_requests_gib"]
        for k in ("cpu_requests_pending", "mem_requests_pending_gib", "cpu_usage_now", "mem_usage_now_gib",
                  "containers",
                  "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit"):
            c[k] += r[k]
        if cat in ("tenant", "unassigned"):
            c[f"namespaces_{cat}"] += 1
    # scheduled requests only: pending pods count against quota but occupy no node capacity
    c["cpu_requests_pct_of_schedulable"] = pct(c["cpu_requests_total"] - c["cpu_requests_pending"],
                                               c["alloc_cpu_schedulable"])
    c["mem_requests_pct_of_schedulable"] = pct(c["mem_requests_gib_total"] - c["mem_requests_pending_gib"],
                                               c["alloc_mem_gib_schedulable"])
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", action="append", default=[],
                    help="kubectl context to scan (repeatable). Default: current context")
    ap.add_argument("--rancher-local-context",
                    help="context of the Rancher local (management) cluster, to resolve Project display names")
    ap.add_argument("--project-label",
                    help="namespace label to group by when there is no Rancher Project (e.g. on non-Rancher AKS)")
    ap.add_argument("--prometheus", choices=["auto", "none"], default="auto",
                    help="query Prometheus for usage history (default: auto, skipped if unreachable)")
    ap.add_argument("--prom-service", default=DEFAULT_PROM_SERVICE,
                    help=f"Prometheus service for API server proxy as ns/[scheme:]name:port (default: {DEFAULT_PROM_SERVICE})")
    ap.add_argument("--prom-url", help="direct Prometheus URL instead of the service proxy (token in $PROM_TOKEN)")
    ap.add_argument("--window", default="7d", help="usage history window (default: 7d; Rancher Monitoring keeps ~10d)")
    ap.add_argument("--step", default="5m", help="subquery resolution (default: 5m)")
    ap.add_argument("--system-ns-regex", default=DEFAULT_SYSTEM_NS_REGEX,
                    help="namespaces treated as platform/system")
    ap.add_argument("--out", default="discovery-output", help="output directory (default: discovery-output)")
    ap.add_argument("--top", type=int, default=15, help="projects per cluster in console summary")
    args = ap.parse_args()

    contexts = args.context or [kubectl(None, ["config", "current-context"]).strip()]

    rancher_projects, rancher_clusters = {}, {}
    if args.rancher_local_context:
        try:
            rancher_projects, rancher_clusters = load_rancher_projects(args.rancher_local_context)
            log(f"loaded {len(rancher_projects)} Rancher projects, {len(rancher_clusters)} clusters")
        except KubectlError as e:
            log(f"could not read Rancher projects: {first_line(e)}")

    all_ns, all_clusters = [], []
    for ctx in contexts:
        try:
            ns_rows, cluster_row = collect_cluster(ctx, args, rancher_projects, rancher_clusters)
        except KubectlError as e:
            log(f"[{ctx}] FAILED: {first_line(e)}")
            continue
        all_ns.extend(ns_rows)
        all_clusters.append(cluster_row)

    if not all_clusters:
        log("no cluster data collected")
        return 1

    projects = aggregate_projects(all_ns)
    all_ns.sort(key=lambda r: (r["cluster"], r["category"], r["project"], r["namespace"]))

    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "namespaces.csv"), NS_FIELDS, all_ns)
    write_csv(os.path.join(args.out, "projects.csv"), PROJECT_FIELDS, projects)
    write_csv(os.path.join(args.out, "clusters.csv"), CLUSTER_FIELDS, all_clusters)

    print_summary(all_clusters, projects, args.top)
    print(f"\nCSV written to {args.out}/ (namespaces.csv, projects.csv, clusters.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
