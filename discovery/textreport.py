#!/usr/bin/env python3
"""Plain-text reports from the live view's state (server.py /text and /api/live/text).

The same tables as the kubectl + jq one-liners people keep in their shell profile, computed from live.LiveState's
snapshot instead of a fresh `kubectl get pods -A -o json`, so they cost the cluster nothing:

  pods        the pods with the most memory (or CPU) requested, with their node
  namespaces  requests and limits summed per namespace (with its Rancher Project), cluster total last
  workloads   one namespace by workload (Deployment, StatefulSet, ...): pods, per-pod and total requests
  nodes       per node: pods, allocatable, requested, limits and used memory, CPU requested; with a namespace, also
              how many of that namespace's pods (and how much of its memory request) each node carries

Pods counted are those the live view keeps: every pod that has not finished (Running, Pending, Terminating).
Memory is shown in Mi (Gi on node totals), CPU in millicores. Usage needs metrics-server ("n/a" without it).
"""

MI = 2 ** 20
GI = 2 ** 30
REPORTS = ("nodes", "namespaces", "workloads", "pods")
SORTS = ("mem", "cpu")
DEFAULT_TOP = 30
MAX_TOP = 1000


class ReportError(ValueError):
    """A request the reports can't answer (unknown report, missing namespace, ...): 400 on the API."""


def _pct(part, whole):
    return f"{part / whole * 100:.0f}%" if whole else "-"


def _mi(b):
    return f"{b / MI:,.0f} Mi"


def _gi(b):
    return f"{b / GI:,.1f} Gi"


def _m(cores):
    return f"{cores * 1000:,.0f} m"


def _table(headers, rows, right=None, footer=None):
    """Fixed-width table; `right` = indexes of right-aligned (numeric) columns."""
    right = set(right or ())
    allrows = [headers] + rows + ([footer] if footer else [])
    widths = [max(len(str(r[i])) for r in allrows) for i in range(len(headers))]

    def line(r):
        return "  ".join(str(v).rjust(w) if i in right else str(v).ljust(w)
                         for i, (v, w) in enumerate(zip(r, widths))).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows]
    if footer:
        out += ["  ".join("-" * w for w in widths), line(footer)]
    return "\n".join(out)


def _usage_of(snap, pod):
    """(cpu cores, memory bytes) a pod uses now, or None without metrics."""
    u = (snap.get("usage") or {}).get(f"{pod['ns']}/{pod['name']}")
    if u is None:
        return None
    return sum(v[0] for v in u.values()), sum(v[1] for v in u.values())


def _workload(pod):
    return f"{pod['owner_kind']}/{pod['owner']}"


def _sort_key(sort):
    return (lambda r: r["cpu"]) if sort == "cpu" else (lambda r: r["mem"])


def _header(snap, title):
    st = snap.get("status") or {}
    cluster = st.get("cluster") or "this cluster"
    metrics = (st.get("metrics") or {}).get("available")
    usage = f"usage at {snap.get('usage_at') or st.get('usage_at')}" if metrics else "usage n/a (no metrics-server)"
    return f"{title} -- {cluster} -- state at {snap.get('at')}, {usage}\n"


def report_pods(snap, sort="mem", top=DEFAULT_TOP, namespace=None):
    pods = [p for p in snap.get("pods") or [] if not namespace or p["ns"] == namespace]
    rows = sorted(({"pod": p, "mem": p["mem_req"], "cpu": p["cpu_req"]} for p in pods), key=_sort_key(sort), reverse=True)
    out = []
    for r in rows[:top]:
        p, u = r["pod"], _usage_of(snap, r["pod"])
        out.append([p["ns"], p["name"], p["node"] or "-", p["phase"], _mi(p["mem_req"]), _mi(p["mem_lim"]) if p["mem_lim"] else "none",
                    _mi(u[1]) if u else "n/a", _m(p["cpu_req"])])
    what = "memory" if sort == "mem" else "CPU"
    title = f"Top {min(top, len(rows))} of {len(rows)} pods by {what} request" + (f" in {namespace}" if namespace else "")
    return _header(snap, title) + "\n" + _table(
        ["NAMESPACE", "POD", "NODE", "PHASE", "MEM_REQ", "MEM_LIM", "MEM_USED", "CPU_REQ"], out, right={4, 5, 6, 7})


def report_namespaces(snap, sort="mem"):
    projects = {n["name"]: n.get("project") or "-" for n in snap.get("namespaces") or []}
    agg = {}
    for p in snap.get("pods") or []:
        a = agg.setdefault(p["ns"], {"ns": p["ns"], "pods": 0, "mem": 0, "mem_lim": 0, "cpu": 0, "used": 0, "has_use": False})
        u = _usage_of(snap, p)
        a["pods"] += 1
        a["mem"] += p["mem_req"]
        a["mem_lim"] += p["mem_lim"]
        a["cpu"] += p["cpu_req"]
        if u:
            a["used"] += u[1]
            a["has_use"] = True
    rows = sorted(agg.values(), key=_sort_key(sort), reverse=True)
    out = [[r["ns"], projects.get(r["ns"], "-"), r["pods"], _mi(r["mem"]), _mi(r["mem_lim"]),
            _mi(r["used"]) if r["has_use"] else "n/a", _m(r["cpu"])] for r in rows]
    total = ["TOTAL", "", sum(r["pods"] for r in rows), _mi(sum(r["mem"] for r in rows)),
             _mi(sum(r["mem_lim"] for r in rows)),
             _mi(sum(r["used"] for r in rows)) if any(r["has_use"] for r in rows) else "n/a",
             _m(sum(r["cpu"] for r in rows))]
    what = "memory" if sort == "mem" else "CPU"
    return _header(snap, f"Requests per namespace ({len(rows)}), by {what} request") + "\n" + _table(
        ["NAMESPACE", "PROJECT", "PODS", "MEM_REQ", "MEM_LIM", "MEM_USED", "CPU_REQ"], out, right={2, 3, 4, 5, 6}, footer=total)


def report_workloads(snap, namespace, sort="mem"):
    if not namespace:
        raise ReportError("the workloads report needs a namespace (ns=...)")
    agg = {}
    for p in snap.get("pods") or []:
        if p["ns"] != namespace:
            continue
        a = agg.setdefault(_workload(p), {"wl": _workload(p), "pods": 0, "mem": 0, "cpu": 0, "nodes": set()})
        a["pods"] += 1
        a["mem"] += p["mem_req"]
        a["cpu"] += p["cpu_req"]
        if p["node"]:
            a["nodes"].add(p["node"])
    if not agg and namespace not in {n["name"] for n in snap.get("namespaces") or []}:
        raise ReportError(f"namespace {namespace!r} not found")
    rows = sorted(agg.values(), key=_sort_key(sort), reverse=True)
    out = [[r["wl"], r["pods"], len(r["nodes"]), _mi(r["mem"] / r["pods"]), _mi(r["mem"]), _m(r["cpu"] / r["pods"]),
            _m(r["cpu"])] for r in rows]
    total = ["TOTAL", sum(r["pods"] for r in rows), "", "", _mi(sum(r["mem"] for r in rows)), "",
             _m(sum(r["cpu"] for r in rows))]
    what = "memory" if sort == "mem" else "CPU"
    return _header(snap, f"Workloads in {namespace} ({len(rows)}), by {what} request") + "\n" + _table(
        ["WORKLOAD", "PODS", "NODES", "MEM/POD", "MEM_TOTAL", "CPU/POD", "CPU_TOTAL"], out,
        right={1, 2, 3, 4, 5, 6}, footer=total)


def report_nodes(snap, namespace=None, sort="mem"):
    node_usage = snap.get("node_usage") or {}
    agg = {n["name"]: {"node": n, "pods": 0, "mem": 0, "mem_lim": 0, "cpu": 0, "ns_pods": 0, "ns_mem": 0}
           for n in snap.get("nodes") or []}
    for p in snap.get("pods") or []:
        a = agg.get(p["node"])
        if a is None:
            continue   # not scheduled yet
        a["pods"] += 1
        a["mem"] += p["mem_req"]
        a["mem_lim"] += p["mem_lim"]
        a["cpu"] += p["cpu_req"]
        if namespace and p["ns"] == namespace:
            a["ns_pods"] += 1
            a["ns_mem"] += p["mem_req"]
    for a in agg.values():
        n = a["node"]
        a["mem_ratio"] = a["mem"] / n["mem_alloc"] if n["mem_alloc"] else 0
        a["cpu_ratio"] = a["cpu"] / n["cpu_alloc"] if n["cpu_alloc"] else 0
    rows = sorted(agg.values(), key=lambda r: r["cpu_ratio"] if sort == "cpu" else r["mem_ratio"], reverse=True)
    headers = ["NODE", "PODS", "MEM_ALLOC", "MEM_REQ", "REQ%", "MEM_LIM", "LIM%", "USE%", "CPU_REQ%"]
    if namespace:
        headers += ["NS_PODS", "NS_MEM_REQ"]
    out = []
    for r in rows:
        n = r["node"]
        used = node_usage.get(n["name"])
        flag = "!" if n["mem_alloc"] and r["mem_lim"] > n["mem_alloc"] else ""
        state = "" if n["ready"] else " (NotReady)"
        state += " (cordoned)" if n["unschedulable"] else ""
        row = [n["name"] + state, r["pods"], _gi(n["mem_alloc"]), _gi(r["mem"]), _pct(r["mem"], n["mem_alloc"]),
               _gi(r["mem_lim"]), _pct(r["mem_lim"], n["mem_alloc"]) + flag,
               _pct(used[1], n["mem_alloc"]) if used else "n/a", _pct(r["cpu"], n["cpu_alloc"])]
        if namespace:
            row += [r["ns_pods"], _gi(r["ns_mem"])]
        out.append(row)
    ta = sum(r["node"]["mem_alloc"] for r in rows)
    tm, tl = sum(r["mem"] for r in rows), sum(r["mem_lim"] for r in rows)
    tu = [node_usage[r["node"]["name"]][1] for r in rows if r["node"]["name"] in node_usage]
    tc, tca = sum(r["cpu"] for r in rows), sum(r["node"]["cpu_alloc"] for r in rows)
    total = ["TOTAL", sum(r["pods"] for r in rows), _gi(ta), _gi(tm), _pct(tm, ta), _gi(tl), _pct(tl, ta),
             _pct(sum(tu), ta) if tu else "n/a", _pct(tc, tca)]
    if namespace:
        total += [sum(r["ns_pods"] for r in rows), _gi(sum(r["ns_mem"] for r in rows))]
    what = "memory" if sort == "mem" else "CPU"
    title = f"Nodes ({len(rows)}), by {what} requested %" + (f", with namespace {namespace}" if namespace else "")
    notes = "\n\nREQ% = memory requested / allocatable (what the scheduler sees). LIM% over 100 (!) = limits overcommit " \
            "the node. USE% = memory used now (metrics-server)."
    return _header(snap, title) + "\n" + _table(headers, out, right=set(range(1, len(headers))), footer=total) + notes


def render(snap, report, namespace=None, sort="mem", top=DEFAULT_TOP):
    """One report as text. Raises ReportError for a bad request."""
    if report not in REPORTS:
        raise ReportError(f"unknown report {report!r} (one of: {', '.join(REPORTS)})")
    if sort not in SORTS:
        raise ReportError(f"sort must be one of: {', '.join(SORTS)}")
    if not 1 <= top <= MAX_TOP:
        raise ReportError(f"top must be between 1 and {MAX_TOP}")
    namespace = namespace or None
    if report == "pods":
        text = report_pods(snap, sort, top, namespace)
    elif report == "namespaces":
        text = report_namespaces(snap, sort)
    elif report == "workloads":
        text = report_workloads(snap, namespace, sort)
    else:
        text = report_nodes(snap, namespace, sort)
    return text + "\n"
