"""Namespace quotas: everything needed to put a ResourceQuota and a LimitRange on each namespace of a cluster.

Computed from the collector's report (read-only, nothing is applied), for the page at /quotas:

  - per namespace: what it has today (ResourceQuota hard / used, LimitRange defaults, Rancher-managed or not),
    the proposed soft quota (docs/10, step 4.1), and what stands in the way of applying it:

        basis    = max(requests now, peak requests over the window, requests with every HPA at maxReplicas)
                   + the requests the LimitRange defaults will add to containers that set none
        quota    = basis x headroom          (CPU rounded up to 0.1 vCPU, memory to whole GiB)
        limits.memory = max(memory quota x memory limit factor,
                            today's memory limits incl. defaults x headroom)   (a quota below them blocks rollouts)
        pods     = pods now x pods factor, rounded up to 10

  - per project: the sum of its namespaces = the Rancher Project limit, with the namespace values as overrides
  - per cluster: sold vs. sellable -- the proposed quotas (and today's) against the room for projects after a
    node failure (N+1) and the environment's maximum share (docs/10, T2)
  - the rollout steps in order, with counts from this cluster

Exports: the allocation files of docs/04 (one document per project, for allocation/generate.py and the Git flow),
plain ResourceQuota + LimitRange manifests (same names and labels as generate.py writes), and a CSV.
"""

import csv
import io
import json
import math
import re
from datetime import datetime, timezone

# Page settings (query parameters), with the limits accepted. Not stored: the page keeps them in the browser.
SETTINGS = {
    # key: (default, min, max, description)
    "headroom": (1.3, 1.0, 3.0, "headroom factor on the basis (requests, peak, HPA maximum)"),
    "mem_limit_factor": (1.0, 1.0, 4.0, "limits.memory = requests.memory x this (1 in prod, up to 2 elsewhere, S3)"),
    "default_cpu_request": (0.1, 0.001, 16.0, "LimitRange default CPU request (vCPU) for containers that set none"),
    "default_mem_request_gib": (0.25, 0.001, 256.0, "LimitRange default memory request (GiB)"),
    "default_mem_limit_gib": (0.25, 0.001, 256.0, "LimitRange default memory limit (GiB)"),
    "max_pct": (80.0, 1.0, 300.0, "share of the capacity after platform reserve that may be sold (A2: 80 in prod)"),
    "pods_factor": (2.0, 1.0, 10.0, "pods quota = pods now x this, rounded up to 10"),
}
MIN_CPU, MIN_MEM_GIB, MIN_PODS = 0.1, 1.0, 10
LABELS = "allocation.cluster-resource-allocation/"   # same labels as allocation/generate.py


def parse_settings(query):
    """Settings from a query string dict ({name: [values]} or {name: value}); unknown keys are ignored, bad values
    fall back to the default. include_system=1 adds the System namespaces."""
    out = {}
    for key, (default, lo, hi, _) in SETTINGS.items():
        raw = query.get(key, default)
        raw = raw[0] if isinstance(raw, list) else raw
        try:
            v = float(raw)
        except (TypeError, ValueError):
            v = default
        out[key] = min(max(v, lo), hi) if math.isfinite(v) else default
    inc = query.get("include_system", "0")
    out["include_system"] = (inc[0] if isinstance(inc, list) else inc) in ("1", "true", "yes")
    return out


def round_up(x, step):
    return round(math.ceil(x / step - 1e-9) * step, 3)


def _num(v):
    return v if isinstance(v, (int, float)) and v != "" else None


def _fmt(n):
    n = round(n, 3)
    return f"{n:g}"


def evaluate_namespace(n, s, has_prometheus):
    """One namespace: the proposed quota and its issues. n is a namespace row of the report."""
    issues = []
    pods = n["pods_running"] + n["pods_pending"]
    lr_cpu, lr_mem, lr_lim = (_num(n.get("lr_default_cpu_request")), _num(n.get("lr_default_mem_request_gib")),
                              _num(n.get("lr_default_mem_limit_gib")))
    # requests the defaults will add: today's LimitRange if it has one, else the planned one
    add_cpu = n["containers_no_cpu_request"] * (lr_cpu if lr_cpu is not None else s["default_cpu_request"])
    add_mem = n["containers_no_mem_request"] * (lr_mem if lr_mem is not None else s["default_mem_request_gib"])
    add_lim = n["containers_no_mem_limit"] * (lr_lim if lr_lim is not None else s["default_mem_limit_gib"])
    basis_cpu = max(n["cpu_requests"], n.get("cpu_requests_peak", 0), n.get("cpu_requests_hpa_max", 0)) + add_cpu
    basis_mem = max(n["mem_requests_gib"], n.get("mem_requests_peak_gib", 0), n.get("mem_requests_hpa_max_gib", 0)) + add_mem
    cpu = max(MIN_CPU, round_up(basis_cpu * s["headroom"], 0.1))
    mem = max(MIN_MEM_GIB, round_up(basis_mem * s["headroom"], 1))
    limits_today = n["mem_limits_gib"] + add_lim
    mem_limit = round_up(mem * s["mem_limit_factor"], 1)
    if limits_today * s["headroom"] > mem_limit:
        mem_limit = round_up(limits_today * s["headroom"], 1)
        issues.append(("warning", "limits_above", f"today's memory limits ({_fmt(limits_today)} GiB with defaults) are above "
                       f"memory x {_fmt(s['mem_limit_factor'])}: limits.memory raised to {_fmt(mem_limit)} GiB so rollouts "
                       f"are not refused; limits far above requests break S3"))
    pods_quota = max(MIN_PODS, round_up(pods * s["pods_factor"], MIN_PODS))

    needs_defaults = []
    if n["containers_no_cpu_request"] and lr_cpu is None:
        needs_defaults.append(f"{int(n['containers_no_cpu_request'])} without a CPU request")
    if n["containers_no_mem_request"] and lr_mem is None:
        needs_defaults.append(f"{int(n['containers_no_mem_request'])} without a memory request")
    if n["containers_no_mem_limit"] and lr_lim is None:
        needs_defaults.append(f"{int(n['containers_no_mem_limit'])} without a memory limit")
    if needs_defaults:
        issues.append(("defaults", "needs_limitrange", "needs the LimitRange first: containers " + ", ".join(needs_defaults) +
                       " would be refused by the quota (must specify requests / limits)"))

    quota_hard_cpu, quota_hard_mem = _num(n.get("quota_cpu_hard")), _num(n.get("quota_mem_hard_gib"))
    if n.get("quota_names"):
        who = "Rancher Project quota" if n.get("quota_rancher") else "ResourceQuota"
        issues.append(("note", "has_quota", f"has a {who} already ({n['quota_names']})" +
                       (": change it in Rancher / Terraform, don't add a second one" if n.get("quota_rancher") else
                        ": compare it with the proposal and replace it through the allocation files")))
        for hard, now, unit, what in ((quota_hard_cpu, n["cpu_requests"], "vCPU", "requests.cpu"),
                                      (quota_hard_mem, n["mem_requests_gib"], "GiB", "requests.memory")):
            if hard is not None and now > hard + 1e-9:
                issues.append(("blocker", "quota_below_requests", f"the current {what} quota ({_fmt(hard)} {unit}) is below "
                               f"today's requests ({_fmt(now)} {unit}): new pods are refused until it is raised"))
            elif hard is not None and hard > 0 and now >= 0.9 * hard:
                issues.append(("warning", "quota_nearly_used", f"{what} quota {_fmt(100 * now / hard)} % used"))
    if n["category"] == "unassigned":
        issues.append(("warning", "unassigned", "not in a Rancher Project: assign it first (step 1.6); a Rancher "
                       "Project quota would not cover it"))
    if n.get("nolimit_mem_max_now_gib", 0) > (lr_lim if lr_lim is not None else s["default_mem_limit_gib"]) + 1e-9:
        issues.append(("warning", "oom_risk", f"container {n.get('nolimit_mem_max_container') or '?'} has no memory limit "
                       f"and uses {_fmt(n['nolimit_mem_max_now_gib'])} GiB now, above the default limit of "
                       f"{_fmt(lr_lim if lr_lim is not None else s['default_mem_limit_gib'])} GiB: it would be OOMKilled "
                       f"after its next restart (step 2.2)"))
    if n["pods_pending"]:
        issues.append(("note", "pending", f"{int(n['pods_pending'])} pending pod(s) count against the quota"))
    if n["pods_besteffort"]:
        issues.append(("note", "besteffort", f"{int(n['pods_besteffort'])} BestEffort pod(s) get the defaults and become "
                       "Burstable (S5)"))
    if n.get("containers_defaulted", 0):
        issues.append(("note", "defaulted", f"{int(n['containers_defaulted'])} container(s) have requests equal to the "
                       "LimitRange defaults: probably defaulted, not chosen (T3)"))
    if n.get("cpu_requests_hpa_max", 0) > n["cpu_requests"] + 1e-9 or n.get("mem_requests_hpa_max_gib", 0) > n["mem_requests_gib"] + 1e-9:
        issues.append(("note", "hpa", f"with every HPA at maxReplicas it would request {_fmt(n['cpu_requests_hpa_max'])} vCPU / "
                       f"{_fmt(n['mem_requests_hpa_max_gib'])} GiB: included in the basis (T8)"))
    if n.get("cpu_requests_peak", 0) > n["cpu_requests"] + 1e-9 or n.get("mem_requests_peak_gib", 0) > n["mem_requests_gib"] + 1e-9:
        issues.append(("note", "peak", f"peak requests over the window {_fmt(n['cpu_requests_peak'])} vCPU / "
                       f"{_fmt(n['mem_requests_peak_gib'])} GiB: included in the basis"))
    if not pods:
        issues.append(("note", "idle", "no pods: minimum quota; check whether the namespace is still needed"))

    levels = {i[0] for i in issues}
    if "blocker" in levels:
        status = "blocked"
    elif n.get("quota_names"):
        status = "has_quota"
    elif "defaults" in levels:
        status = "needs_defaults"
    else:
        status = "ready"
    return {
        "cluster": n["cluster"], "namespace": n["namespace"], "project": n["project"], "project_id": n["project_id"],
        "category": n["category"], "status": status,
        "pods": pods, "cpu_requests": n["cpu_requests"], "mem_requests_gib": n["mem_requests_gib"],
        "cpu_requests_peak": n.get("cpu_requests_peak", n["cpu_requests"]),
        "mem_requests_peak_gib": n.get("mem_requests_peak_gib", n["mem_requests_gib"]),
        "cpu_requests_hpa_max": n.get("cpu_requests_hpa_max", n["cpu_requests"]),
        "mem_requests_hpa_max_gib": n.get("mem_requests_hpa_max_gib", n["mem_requests_gib"]),
        "cpu_usage": n["cpu_usage_p95"] if has_prometheus else n["cpu_usage_now"],
        "mem_usage_gib": n["mem_usage_p95_gib"] if has_prometheus else n["mem_usage_now_gib"],
        "mem_limits_gib": n["mem_limits_gib"], "hpas": n.get("hpas", 0),
        "containers": n["containers"], "containers_no_cpu_request": n["containers_no_cpu_request"],
        "containers_no_mem_request": n["containers_no_mem_request"], "containers_no_mem_limit": n["containers_no_mem_limit"],
        "containers_defaulted": n.get("containers_defaulted", 0), "pods_besteffort": n["pods_besteffort"],
        "nolimit_mem_max_now_gib": n.get("nolimit_mem_max_now_gib", 0), "nolimit_mem_max_container": n.get("nolimit_mem_max_container", ""),
        "basis_cpu": basis_cpu, "basis_mem_gib": basis_mem, "defaults_add_cpu": add_cpu, "defaults_add_mem_gib": add_mem,
        "proposed": {"cpu": cpu, "memory_gib": mem, "memory_limit_gib": mem_limit, "pods": int(pods_quota)},
        "quota": {k[len("quota_"):]: _num(n.get(k)) for k in ("quota_cpu_hard", "quota_cpu_used", "quota_mem_hard_gib",
                                                            "quota_mem_used_gib", "quota_mem_limit_hard_gib",
                                                            "quota_mem_limit_used_gib", "quota_cpu_limit_hard",
                                                            "quota_pods_hard", "quota_pods_used")},
        "quota_names": n.get("quota_names", ""), "quota_rancher": bool(n.get("quota_rancher")),
        "limitrange_names": n.get("limitrange_names", ""), "limitrange_rancher": bool(n.get("limitrange_rancher")),
        "limitrange": {"cpu_request": lr_cpu, "mem_request_gib": lr_mem, "cpu_limit": _num(n.get("lr_default_cpu_limit")),
                       "mem_limit_gib": lr_lim, "max_cpu": _num(n.get("lr_max_cpu")), "max_mem_gib": _num(n.get("lr_max_mem_gib"))},
        "issues": [{"level": lvl, "code": code, "text": text} for lvl, code, text in issues],
    }


def evaluate(report, cluster, settings):
    """The quotas view of one cluster of the report."""
    names = [c["cluster"] for c in report["clusters"]]
    c = next((x for x in report["clusters"] if x["cluster"] == cluster), None) or (report["clusters"][0] if names else None)
    if c is None:
        return {"clusters": [], "cluster": None, "namespaces": [], "projects": [], "steps": [], "settings": settings}
    has_prom = c.get("prometheus_days_of_data", "") not in ("", None)
    cats = ("tenant", "unassigned", "system") if settings["include_system"] else ("tenant", "unassigned")
    rows = [evaluate_namespace(n, settings, has_prom) for n in report["namespaces"]
            if n["cluster"] == c["cluster"] and n["category"] in cats]
    rows.sort(key=lambda r: (r["category"], r["project"].lower(), r["namespace"]))
    projects = _projects(rows)
    summary = _cluster(c, rows, settings)
    return {
        "collected_at": report.get("collected_at"), "window": (report.get("settings") or {}).get("window"),
        "has_prometheus": has_prom, "settings": settings, "clusters": names, "cluster": summary,
        "namespaces": rows, "projects": projects, "steps": _steps(summary, rows, projects),
    }


def _projects(rows):
    agg = {}
    for r in rows:
        key = r["project_id"] or f"ns:{r['namespace']}"
        a = agg.setdefault(key, {"project_key": key, "project": r["project"] or f"(no project) {r['namespace']}",
                                 "category": r["category"], "namespaces": 0, "cpu_requests": 0.0, "mem_requests_gib": 0.0,
                                 "proposed_cpu": 0.0, "proposed_mem_gib": 0.0, "proposed_mem_limit_gib": 0.0,
                                 "quota_cpu_hard": 0.0, "quota_mem_hard_gib": 0.0, "namespaces_with_quota": 0,
                                 "statuses": {"ready": 0, "needs_defaults": 0, "has_quota": 0, "blocked": 0}})
        a["namespaces"] += 1
        a["cpu_requests"] += r["cpu_requests"]
        a["mem_requests_gib"] += r["mem_requests_gib"]
        a["proposed_cpu"] += r["proposed"]["cpu"]
        a["proposed_mem_gib"] += r["proposed"]["memory_gib"]
        a["proposed_mem_limit_gib"] += r["proposed"]["memory_limit_gib"]
        if r["quota_names"]:
            a["namespaces_with_quota"] += 1
            a["quota_cpu_hard"] += r["quota"]["cpu_hard"] or 0
            a["quota_mem_hard_gib"] += r["quota"]["mem_hard_gib"] or 0
        a["statuses"][r["status"]] += 1
    for a in agg.values():   # the Rancher Project limit: the namespaces plus the room for one more (namespace default)
        a["proposed_cpu"], a["proposed_mem_gib"] = round(a["proposed_cpu"], 3), round(a["proposed_mem_gib"], 3)
    return sorted(agg.values(), key=lambda a: (a["category"], -a["proposed_cpu"], a["project"].lower()))


def _cluster(c, rows, s):
    """Sold vs. sellable (T2): proposed and current quotas against the room for projects."""
    sys_cpu = c.get("cpu_requests_system", 0) - c.get("cpu_requests_excluded_system", 0)
    sys_mem = c.get("mem_requests_gib_system", 0) - c.get("mem_requests_excluded_system_gib", 0)
    out = {"cluster": c["cluster"], "nodes": c.get("nodes"), "nodes_schedulable": c.get("nodes_schedulable"),
           "prometheus_days_of_data": c.get("prometheus_days_of_data", "")}
    for dim, unit, alloc, n1, largest, system in (
            ("cpu", "vCPU", c.get("alloc_cpu_schedulable", 0), c.get("cpu_for_projects_n1"), c.get("largest_node_cpu", 0), sys_cpu),
            ("mem", "GiB", c.get("alloc_mem_gib_schedulable", 0), c.get("mem_gib_for_projects_n1"), c.get("largest_node_mem_gib", 0), sys_mem)):
        n1 = n1 if n1 is not None else max(0.0, alloc - largest - system)
        pct_cap = max(0.0, s["max_pct"] / 100 * (alloc - system))
        sellable = min(n1, pct_cap)
        k = "proposed_cpu" if dim == "cpu" else "proposed_mem_gib"
        proposed = sum(r["proposed"]["cpu" if dim == "cpu" else "memory_gib"] for r in rows)
        requests = sum(r["cpu_requests" if dim == "cpu" else "mem_requests_gib"] for r in rows)
        sold = sum((r["quota"]["cpu_hard" if dim == "cpu" else "mem_hard_gib"] or 0) for r in rows if r["quota_names"])
        share = 100 * proposed / sellable if sellable > 0 else (0.0 if proposed == 0 else math.inf)
        out[dim] = {"unit": unit, "allocatable": alloc, "largest_node": largest, "platform": system, "room_n1": n1,
                    "max_pct_capacity": pct_cap, "sellable": sellable, "binding": "n1" if n1 <= pct_cap else "max_pct",
                    "requests": requests, "sold_now": sold, "proposed": proposed, "share_pct": share,
                    "status": "red" if share > 100 else "amber" if share >= 70 else "green"}
        out[k] = proposed
    return out


def _steps(summary, rows, projects):
    """The rollout in order (docs/10), with this cluster's numbers."""
    by = lambda st: [r for r in rows if r["status"] == st]  # noqa: E731
    unassigned = [r for r in rows if r["category"] == "unassigned"]
    oom = [r for r in rows if any(i["code"] == "oom_risk" for i in r["issues"])]
    need_lr = [r for r in rows if any(i["code"] == "needs_limitrange" for i in r["issues"])]
    no_lr = [r for r in rows if not r["limitrange_names"]]
    blocked, has_quota, ready = by("blocked"), by("has_quota"), by("ready")
    rancher = [r for r in has_quota if r["quota_rancher"]]
    steps = [
        {"step": "1.6", "title": "Every tenant namespace in a Rancher Project",
         "count": len(unassigned), "done": not unassigned, "items": [r["namespace"] for r in unassigned],
         "text": "Move them in Rancher (or label them on clusters outside Rancher). A Rancher Project quota covers only the Project's namespaces."},
        {"step": "2.2", "title": "Remove the OOMKill risk of the default memory limit",
         "count": len(oom), "done": not oom, "items": [f"{r['namespace']}: {r['nolimit_mem_max_container']} {_fmt(r['nolimit_mem_max_now_gib'])} GiB" for r in oom],
         "text": "Containers without a memory limit that use more than the planned default limit now: the team sets a real limit first, or the namespace gets a higher default."},
        {"step": "2.3", "title": "Apply the LimitRange (container defaults)",
         "count": len(no_lr), "done": not no_lr, "items": [r["namespace"] for r in need_lr],
         "text": f"{len(no_lr)} namespace(s) have no LimitRange; {len(need_lr)} of them run containers without requests or a memory limit, "
                 "which a quota would refuse (must specify). The manifests export includes the LimitRange. Only new pods get the defaults."},
        {"step": "4.1", "title": "Check the proposed quotas",
         "count": len(rows), "done": True, "items": [],
         "text": f"basis = max(requests now, peak, HPA maximum) + defaults, x headroom. Σ proposed: {_fmt(summary['proposed_cpu'])} vCPU / "
                 f"{_fmt(summary['proposed_mem_gib'])} GiB over {len(rows)} namespace(s) in {len(projects)} project(s)."},
        {"step": "4.2", "title": "Sold vs. sellable",
         "count": sum(1 for d in ("cpu", "mem") if summary[d]["status"] == "red"), "done": all(summary[d]["status"] != "red" for d in ("cpu", "mem")),
         "items": [f"{d.upper()}: proposed {_fmt(summary[d]['proposed'])} of {_fmt(summary[d]['sellable'])} {summary[d]['unit']} sellable "
                   f"({'fits' if summary[d]['share_pct'] <= 100 else 'over by ' + _fmt(summary[d]['proposed'] - summary[d]['sellable'])})" for d in ("cpu", "mem")],
         "text": "A soft quota describes what already runs: a red cluster is already over capacity. Record it as a capacity finding, don't squeeze the quota."},
        {"step": "4.3", "title": "Namespaces that already have a quota",
         "count": len(has_quota) + len(blocked), "done": not (has_quota or blocked),
         "items": [f"{r['namespace']} ({r['quota_names']})" + (" Rancher" if r["quota_rancher"] else "") for r in has_quota + blocked],
         "text": f"{len(rancher)} managed by a Rancher Project quota (change them in Rancher / Terraform, never add a second ResourceQuota); "
                 f"{len(blocked)} blocked: the quota is below today's requests, new pods are refused there."},
        {"step": "4.4", "title": "Apply the quotas",
         "count": len(ready), "done": False,
         "items": [r["namespace"] for r in ready],
         "text": f"{len(ready)} namespace(s) ready now: Σ {_fmt(sum(r['proposed']['cpu'] for r in ready))} vCPU / "
                 f"{_fmt(sum(r['proposed']['memory_gib'] for r in ready))} GiB. Export the allocation files, pull request per wave "
                 "(dev → test → acc → prod, one pilot project first), then watch for 'exceeded quota' and 'must specify' events."},
    ]
    return steps


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _ystr(s):
    s = str(s)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./-]*", s) and s not in ("true", "false", "null", "yes", "no", "on", "off"):
        return s
    return json.dumps(s)


def _header(view, what):
    s = view["settings"]
    return (f"# Namespace quotas of cluster {view['cluster']['cluster']}, {what}, exported "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} from the report of {view['collected_at']}.\n"
            f"# Proposal: max(requests now, peak over {view['window'] or '?'}, HPA maximum) + defaults, x {_fmt(s['headroom'])}; "
            f"limits.memory = memory x {_fmt(s['mem_limit_factor'])}; pods x {_fmt(s['pods_factor'])}.\n"
            f"# Container defaults: {_fmt(s['default_cpu_request'])} vCPU / {_fmt(s['default_mem_request_gib'])} GiB request, "
            f"{_fmt(s['default_mem_limit_gib'])} GiB memory limit.\n"
            "# Not applied to any cluster.\n")


def _mem(gib):
    return f"{_fmt(gib)}Gi" if gib == int(gib) else f"{int(round(gib * 1024))}Mi"


def export_allocations(view):
    """The proposal as allocation files (docs/04, guides/allocation-as-code.md): one document per project, with its
    namespaces on this cluster. For allocation/generate.py and the Git / Terraform flow."""
    cluster = view["cluster"]["cluster"]
    docs = []
    groups = {}
    for r in view["namespaces"]:
        groups.setdefault(r["project_id"] or f"ns:{r['namespace']}", []).append(r)
    for key, rows in groups.items():
        first = rows[0]
        project = first["project"] or first["namespace"]
        lines = [f"# allocations/projects/{project}.yaml"]
        if not first["project"]:
            lines.append("# not in a Rancher Project: assign the namespace to a Project first (its name is used here)")
        lines += [f"project: {_ystr(project)}", "owners: []", "allocations:", f"  - cluster: {_ystr(cluster)}",
                  "    namespaces:"]
        for r in rows:
            p = r["proposed"]
            note = {"has_quota": "has a quota already", "blocked": "quota below requests", "needs_defaults": "needs the LimitRange"}.get(r["status"])
            lines.append(f"      - name: {_ystr(r['namespace'])}" + (f"   # {note}" if note else ""))
            lines.append(f"        cpu: {_fmt(p['cpu'])}   # requests now {_fmt(r['cpu_requests'])}, peak {_fmt(r['cpu_requests_peak'])}, "
                         f"HPA max {_fmt(r['cpu_requests_hpa_max'])}")
            lines.append(f"        memory: {_mem(p['memory_gib'])}   # requests now {_fmt(r['mem_requests_gib'])} GiB, peak "
                         f"{_fmt(r['mem_requests_peak_gib'])}, limits now {_fmt(r['mem_limits_gib'])}")
            if p["memory_limit_gib"] != p["memory_gib"]:
                lines.append(f"        memoryLimit: {_mem(p['memory_limit_gib'])}")
            lines.append(f"        pods: {p['pods']}")
        docs.append("\n".join(lines) + "\n")
    s = view["settings"]
    settings_doc = ("# settings.yaml (excerpt): the container defaults the proposal assumes\n"
                    "containerDefault:\n"
                    f"  cpu: {_fmt(s['default_cpu_request'])}\n"
                    f"  memory: {_mem(s['default_mem_request_gib'])}\n")
    return _header(view, "allocation files") + "---\n" + "---\n".join([settings_doc] + docs)


def export_manifests(view):
    """Plain ResourceQuota + LimitRange per namespace, like allocation/generate.py writes them (same names and labels),
    for clusters outside Rancher or a trial on a test cluster. Namespaces that already have a quota are left out."""
    s = view["settings"]
    docs, skipped = [], []
    for r in view["namespaces"]:
        if r["quota_names"]:
            skipped.append(f"{r['namespace']} ({r['quota_names']})")
            continue
        p = r["proposed"]
        labels = {LABELS + "managed": "true", LABELS + "project": r["project"] or "none"}
        lab = "\n".join(f"    {k}: {json.dumps(v)}" for k, v in labels.items())
        docs.append(
            f"apiVersion: v1\nkind: ResourceQuota\nmetadata:\n  name: allocation\n  namespace: {_ystr(r['namespace'])}\n"
            f"  labels:\n{lab}\nspec:\n  hard:\n    requests.cpu: \"{_fmt(p['cpu'])}\"\n"
            f"    requests.memory: {_mem(p['memory_gib'])}\n    limits.memory: {_mem(p['memory_limit_gib'])}\n"
            f"    pods: \"{p['pods']}\"\n")
        if not r["limitrange_names"]:
            docs.append(
                f"apiVersion: v1\nkind: LimitRange\nmetadata:\n  name: allocation-defaults\n  namespace: {_ystr(r['namespace'])}\n"
                f"  labels:\n{lab}\nspec:\n  limits:\n    - type: Container\n      defaultRequest:\n"
                f"        cpu: {_fmt(s['default_cpu_request'])}\n        memory: {_mem(s['default_mem_request_gib'])}\n"
                f"      default:\n        memory: {_mem(s['default_mem_limit_gib'])}\n")
    head = _header(view, "ResourceQuota and LimitRange manifests")
    if skipped:
        head += "# Left out, they have a quota already: " + "; ".join(skipped) + "\n"
    head += "# Apply per wave on a test cluster first: kubectl apply --server-side --field-manager=allocations -f <file>\n"
    return head + "---\n" + "---\n".join(docs) if docs else head


CSV_FIELDS = [
    "cluster", "namespace", "project", "category", "status", "pods", "cpu_requests", "cpu_requests_peak", "cpu_requests_hpa_max",
    "mem_requests_gib", "mem_requests_peak_gib", "mem_requests_hpa_max_gib", "mem_limits_gib", "cpu_usage", "mem_usage_gib",
    "containers", "containers_no_cpu_request", "containers_no_mem_request", "containers_no_mem_limit", "containers_defaulted",
    "pods_besteffort", "nolimit_mem_max_now_gib", "nolimit_mem_max_container",
    "proposed_cpu", "proposed_mem_gib", "proposed_mem_limit_gib", "proposed_pods",
    "quota_names", "quota_rancher", "quota_cpu_hard", "quota_cpu_used", "quota_mem_hard_gib", "quota_mem_used_gib",
    "quota_mem_limit_hard_gib", "quota_pods_hard", "limitrange_names", "limitrange_rancher",
    "lr_default_cpu_request", "lr_default_mem_request_gib", "lr_default_mem_limit_gib", "issues",
]


def export_csv(view):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in view["namespaces"]:
        row = dict(r)
        row.update({"proposed_" + k.replace("memory", "mem"): v for k, v in r["proposed"].items()})
        row.update({"quota_" + k: ("" if v is None else v) for k, v in r["quota"].items()})
        row.update({"lr_default_cpu_request": r["limitrange"]["cpu_request"], "lr_default_mem_request_gib": r["limitrange"]["mem_request_gib"],
                    "lr_default_mem_limit_gib": r["limitrange"]["mem_limit_gib"]})
        row["issues"] = " | ".join(i["text"] for i in r["issues"])
        w.writerow({k: _csv(row.get(k, "")) for k in CSV_FIELDS})
    return out.getvalue()


def _csv(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return v
