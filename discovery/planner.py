"""Allocation planner: budgets and quotas per Rancher Project and cluster, planned but never applied.

The planner is a planning aid for the platform team on the Rancher local cluster (docs/04, "Allocation
planner"). It reads the Rancher inventory (clusters with their capacity, projects with their current quota) and
keeps the plan in a JSON file in the report's data directory. It writes nothing to any cluster: the output is
the allocation files of docs/04 ("Allocation as code"), which go through the Git / Terraform flow.

The plan holds, per project, a monthly budget and a CPU/memory quota per cluster. Either can drive the other:
the page converts a budget into quota with the cluster's unit rates, and the checks here compare the cost of the
planned quota with the budget, and the planned quota per cluster with the cluster's limit for projects:

    capacity after failures = allocatable of schedulable nodes - the N largest nodes   (N = environment's node_failures)
    limit for projects      = min(capacity after failures - platform reserve,
                                  max_quota_pct x (allocatable - platform reserve))

computed for CPU and memory separately. The platform reserve is what platform components (cattle-*, kube-system,
monitoring, ...) request: measured for the cluster this collector scans, entered per cluster for the others.
"""

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone

import discover

# Two planners share this module:
#   budget   -- the allocation planner: unit rates, budgets and costs per project (/planner)
#   capacity -- the capacity planner: no money, each project has a CPU and memory envelope instead (/capacity)
# Same inventory, cluster limits and export; separate plan files.
MODES = ("budget", "capacity")
STATE_FILES = {"budget": ("planner.json", "planner-history", "planner"),
               "capacity": ("capacity-plan.json", "capacity-history", "capacity")}
STATE_FILE, HISTORY_DIR = STATE_FILES["budget"][:2]
HISTORY_KEEP = 30
MAX_STATE_BYTES = 1_000_000

# Hidden by default: Rancher's own projects, not streams with a budget.
RANCHER_BUILTIN_PROJECTS = ("System", "Default")

# Unit rates per month (EUR): docs/03-allocation-model.md "Reference rates" -- AKS from the Azure retail price list
# (West Europe, 1-year reservation, per allocatable unit, Sep 2026), on-prem from an assumption-based TCO estimate.
# Environments from docs/03 (N+1, headroom) and docs/02 (S3: memory limit = request in prod, up to 2x elsewhere).
# All meant to be replaced with the organisation's own figures in the planner.
DEFAULT_SETTINGS = {
    "currency": "EUR",
    "platforms": {
        "onprem": {"cpu_rate": 10.60, "mem_rate": 1.23},
        "aks": {"cpu_rate": 14.84, "mem_rate": 1.82},
    },
    "envs": {
        "prod": {"max_quota_pct": 80.0, "mem_limit_factor": 1.0, "node_failures": 1},
        "test": {"max_quota_pct": 100.0, "mem_limit_factor": 2.0, "node_failures": 0},
        "dev": {"max_quota_pct": 150.0, "mem_limit_factor": 2.0, "node_failures": 0},
    },
}


# Where the default unit rates come from, shown on the planner page (docs/03-allocation-model.md "Reference rates").
RATE_REFERENCE = {
    "as_of": "September 2026",
    "currency": "EUR",
    "aks": {
        "method": "Azure Retail Prices API (prices.azure.com), region West Europe, Linux VMs, D- and E-series v5 and v6 "
                  "(Intel and AMD). D has 4 GiB per vCPU and E 8 GiB, so for each same-size D/E pair the price "
                  "difference is the memory price and the rest the CPU price; medians of 28 pairs from 2 to 64 vCPU. "
                  "Then divided by the allocatable share of a node (D8s_v5, 110 pods: 97.8 % of CPU, 93 % of memory "
                  "after AKS kube-reserved and eviction), because quota is planned against allocatable capacity.",
        "options": [
            {"name": "Pay-as-you-go", "cpu_rate": 24.68, "mem_rate": 3.01},
            {"name": "1-year reservation", "cpu_rate": 14.84, "mem_rate": 1.82, "default": True},
            {"name": "3-year reservation", "cpu_rate": 9.53, "mem_rate": 1.17},
        ],
        "notes": [
            "Default: 1-year reservation, typical for long-running services; switch if your node pools run pay-as-you-go "
            "or on a 3-year reservation or savings plan.",
            "Region: Germany West Central costs the same as West Europe; North Europe about 7 % less.",
            "Not included: the AKS Standard tier (uptime SLA) at 0.0859 per cluster-hour, about 63 per cluster and "
            "month; node OS disks, load balancers, public IPs and egress.",
            "Allocatable assumes 110 max pods per node; with Azure CNI Overlay's default of 250 pods, memory "
            "allocatable drops to about 84 % and the memory rate rises by about 11 %.",
        ],
    },
    "onprem": {
        "method": "Estimate, not market data: the monthly cost of one Kubernetes node (total cost of ownership) divided "
                  "by what it can sell as quota. Replace the assumptions with your own figures.",
        "items": [
            ["Server: 2 sockets, 64 cores / 128 threads, 512 GiB, NVMe, 25 GbE, 5-year warranty; 25,000 over 60 months", 417],
            ["Power: 600 W average x PUE 1.5 x 0.20 per kWh", 131],
            ["Rack space, network and cabling share", 100],
            ["Rancher Prime / SUSE subscription and OS support", 150],
            ["Shared services: monitoring, logging, backup, registry", 50],
            ["Platform team: 2 FTE x 110,000 a year, spread over a 30-node estate", 611],
        ],
        "total": 1459,
        "capacity": "126 vCPU / 496 GiB allocatable per node, 75 % of it sellable after N+1 and headroom "
                    "= 94.5 vCPU / 372 GiB",
        "split": "cost split between CPU and memory in the market price ratio (1 vCPU costs as much as 8.6 GiB)",
        "rates": {"cpu_rate": 10.60, "mem_rate": 1.23},
        "without_team": {"cpu_rate": 6.16, "mem_rate": 0.71},
    },
}


def empty_state(mode="budget"):
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if mode == "capacity":
        settings = {"envs": settings["envs"]}
    return {"version": 0, "updated_at": None, "settings": settings, "clusters": {}, "projects": {}}


# ---------------------------------------------------------------------------
# Rancher inventory (read-only)
# ---------------------------------------------------------------------------

def _qty(value, divisor=1):
    return round(discover.parse_quantity(value) / divisor, 3) if value else None


def parse_inventory(clusters_json, projects_json, nodes_json=None):
    """Clusters (capacity, requests, node sizes) and projects (current quota) from Rancher's management objects."""
    node_sizes = {}
    for n in nodes_json or []:
        st, spec = n.get("status") or {}, n.get("spec") or {}
        if (spec.get("internalNodeSpec") or {}).get("unschedulable"):
            continue  # cordoned: already not usable for pods, so not part of what can fail over
        alloc = (st.get("internalNodeStatus") or {}).get("allocatable") or {}
        node_sizes.setdefault(n["metadata"]["namespace"], []).append({
            "name": st.get("nodeName") or n["metadata"]["name"],
            "cpu": _qty(alloc.get("cpu")) or 0.0,
            "mem_gib": _qty(alloc.get("memory"), discover.GIB) or 0.0,
        })
    clusters = []
    for c in clusters_json:
        st = c.get("status") or {}
        alloc, req = st.get("allocatable") or {}, st.get("requested") or {}
        clusters.append({
            "id": c["metadata"]["name"],
            "name": (c.get("spec") or {}).get("displayName") or c["metadata"]["name"],
            "nodes": st.get("nodeCount"),
            "alloc_cpu": _qty(alloc.get("cpu")),
            "alloc_mem_gib": _qty(alloc.get("memory"), discover.GIB),
            "requested_cpu": _qty(req.get("cpu")),
            "requested_mem_gib": _qty(req.get("memory"), discover.GIB),
            # None when Rancher reported no nodes for it (then N+1 can't be computed)
            "node_sizes": sorted(node_sizes.get(c["metadata"]["name"], []), key=lambda x: x["name"]) or None,
            "platform_measured_cpu": None,
            "platform_measured_mem_gib": None,
        })
    projects = []
    for p in projects_json:
        md, spec = p["metadata"], p.get("spec") or {}
        rq = spec.get("resourceQuota") or {}
        limit, used = rq.get("limit") or {}, rq.get("usedLimit") or {}
        projects.append({
            "cluster_id": spec.get("clusterName") or md.get("namespace"),
            "project_id": md["name"],
            "name": spec.get("displayName") or md["name"],
            "quota_cpu": _qty(limit.get("requestsCpu")),
            "quota_mem_gib": _qty(limit.get("requestsMemory"), discover.GIB),
            "quota_mem_limit_gib": _qty(limit.get("limitsMemory"), discover.GIB),
            "used_cpu": _qty(used.get("requestsCpu")),
            "used_mem_gib": _qty(used.get("requestsMemory"), discover.GIB),
        })
    clusters.sort(key=lambda c: c["name"])
    projects.sort(key=lambda p: (p["name"], p["cluster_id"]))
    return {"clusters": clusters, "projects": projects}


def load_inventory(local_context=None, local_kubeconfig=None):
    return parse_inventory(
        discover.kget(local_context, "clusters.management.cattle.io", all_namespaces=False,
                      kubeconfig=local_kubeconfig),
        discover.kget(local_context, "projects.management.cattle.io", kubeconfig=local_kubeconfig),
        discover.kget(local_context, "nodes.management.cattle.io", kubeconfig=local_kubeconfig))


def add_report_requests(inventory, report):
    """Current requests per project, and the measured platform reserve, from this collector's own report.

    Both only for the cluster the collector scans: the platform reserve is what the report classes as `system`
    (Rancher's System project and the system namespaces: cattle-*, kube-system, monitoring, ...).
    """
    report = report or {}
    scanned = {c.get("cluster") for c in report.get("clusters", [])}
    scanned |= {p["project_id"].split(":", 1)[0] for p in report.get("projects", []) if ":" in (p.get("project_id") or "")}
    system = [p for p in report.get("projects", []) if p.get("category") == "system"]
    for c in inventory["clusters"]:
        if system and (c["id"] in scanned or c["name"] in scanned):
            c["platform_measured_cpu"] = round(sum(p["cpu_requests"] for p in system), 3)
            c["platform_measured_mem_gib"] = round(sum(p["mem_requests_gib"] for p in system), 3)
    by_key = {}
    for p in (report or {}).get("projects", []):
        if p.get("project_id"):
            by_key[p["project_id"]] = p
    for p in inventory["projects"]:
        r = by_key.get(f'{p["cluster_id"]}:{p["project_id"]}')
        p["requests_cpu"] = round(r["cpu_requests"], 3) if r else None
        p["requests_mem_gib"] = round(r["mem_requests_gib"], 3) if r else None
    return inventory


# ---------------------------------------------------------------------------
# Plan state: validation and storage
# ---------------------------------------------------------------------------

class PlanError(ValueError):
    pass


def _num(v, where, minimum=0.0, maximum=1e9):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise PlanError(f"{where}: expected a number")
    if not minimum <= v <= maximum:
        raise PlanError(f"{where}: must be between {minimum:g} and {maximum:g}")
    return float(v)


def _name(v, where, pattern=r"[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,99}"):
    if not isinstance(v, str) or not re.fullmatch(pattern, v):
        raise PlanError(f"{where}: invalid name {v!r}")
    return v


def _text(v, where, maxlen=200):
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > maxlen or any(ch in v for ch in "\r\n"):
        raise PlanError(f"{where}: expected a single-line string of at most {maxlen} characters")
    return v.strip()


def validate_state(raw, mode="budget"):
    """Normalised copy of a plan sent by the page; raises PlanError on anything malformed."""
    if not isinstance(raw, dict):
        raise PlanError("plan must be an object")
    money = mode == "budget"
    settings = raw.get("settings") or {}
    platforms, envs = {}, {}
    for name, p in ((settings.get("platforms") or {}) if money else {}).items():
        _name(name, "platform")
        platforms[name] = {"cpu_rate": _num(p.get("cpu_rate"), f"{name}.cpu_rate"),
                           "mem_rate": _num(p.get("mem_rate"), f"{name}.mem_rate")}
    for name, e in (settings.get("envs") or {}).items():
        _name(name, "environment")
        default_failures = DEFAULT_SETTINGS["envs"].get(name, {}).get("node_failures", 0)
        envs[name] = {"max_quota_pct": _num(e.get("max_quota_pct"), f"{name}.max_quota_pct", 1, 1000),
                      "mem_limit_factor": _num(e.get("mem_limit_factor"), f"{name}.mem_limit_factor", 1, 10),
                      "node_failures": int(_num(e.get("node_failures", default_failures),
                                                f"{name}.node_failures", 0, 10))}
    if money and not platforms:
        raise PlanError("settings need at least one platform")
    if not envs:
        raise PlanError("settings need at least one environment")
    clusters = {}
    for cid, c in (raw.get("clusters") or {}).items():
        _name(cid, "cluster id")
        env, platform = c.get("env") or "", (c.get("platform") or "") if money else ""
        if env and env not in envs:
            raise PlanError(f"cluster {cid}: unknown environment {env!r}")
        if platform and platform not in platforms:
            raise PlanError(f"cluster {cid}: unknown platform {platform!r}")
        reserve = {}
        for key in ("platform_cpu", "platform_mem_gib"):
            v = c.get(key)
            reserve[key] = None if v in (None, "") else _num(v, f"cluster {cid}.{key}", 0, 1000000)
        clusters[cid] = {"env": env, **reserve}
        if money:
            clusters[cid]["platform"] = platform
    projects = {}
    for pname, p in (raw.get("projects") or {}).items():
        _name(pname, "project")
        owners = p.get("owners") or []
        if not isinstance(owners, list) or len(owners) > 20:
            raise PlanError(f"{pname}: owners must be a list")
        allocations = {}
        for cid, a in (p.get("allocations") or {}).items():
            _name(cid, f"{pname}: cluster id")
            allocations[cid] = {"cpu": _num(a.get("cpu", 0), f"{pname}/{cid}.cpu", 0, 100000),
                                "memory_gib": _num(a.get("memory_gib", 0), f"{pname}/{cid}.memory_gib", 0, 1000000)}
        project = {"owners": [_text(o, f"{pname}.owners") for o in owners if o], "allocations": allocations}
        if money:
            budget = p.get("monthly_budget")
            project["cost_center"] = _text(p.get("cost_center"), f"{pname}.cost_center")
            project["monthly_budget"] = None if budget in (None, "") else _num(budget, f"{pname}.monthly_budget")
        else:  # the project's total CPU / memory across all clusters; None = no envelope yet
            for key in ("cpu_envelope", "memory_gib_envelope"):
                v = p.get(key)
                project[key] = None if v in (None, "") else _num(v, f"{pname}.{key}", 0, 1000000)
        projects[pname] = project
    out_settings = {"envs": envs}
    if money:
        out_settings.update(currency=_text(settings.get("currency") or "EUR", "currency", 8) or "EUR",
                            platforms=platforms)
    return {"settings": out_settings, "clusters": clusters, "projects": projects}


class PlanStore:
    """A plan in <data_dir> (planner.json, or capacity-plan.json for the capacity planner), with a version for
    optimistic locking and a short history."""

    def __init__(self, data_dir, mode="budget"):
        self.data_dir, self.mode = data_dir, mode
        self.state_file, self.history_dir, self.prefix = STATE_FILES[mode]
        self.path = os.path.join(data_dir, self.state_file)

    def load(self):
        """The stored plan, normalised: fields added in later versions get their defaults."""
        try:
            with open(self.path) as f:
                stored = json.load(f)
        except FileNotFoundError:
            return empty_state(self.mode)
        try:
            plan = validate_state(stored, self.mode)
        except PlanError as e:  # written by an older version with rules that have since changed
            discover.log(f"planner: stored plan doesn't validate ({e}); showing it unnormalised")
            return stored
        plan["version"], plan["updated_at"] = stored.get("version", 0), stored.get("updated_at")
        return plan

    def save(self, raw, expected_version):
        """Validate and write raw if the stored version is still expected_version; returns the saved plan."""
        current = self.load()
        if expected_version != current.get("version", 0):
            raise PlanConflict(current.get("version", 0))
        plan = validate_state(raw, self.mode)
        plan["version"] = current.get("version", 0) + 1
        plan["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        os.makedirs(self.data_dir, exist_ok=True)
        data = json.dumps(plan, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=f".{self.prefix}-")
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, self.path)
        self._keep_history(plan, data)
        return plan

    def _keep_history(self, plan, data):
        hist = os.path.join(self.data_dir, self.history_dir)
        try:
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, f'{self.prefix}-v{plan["version"]:05d}.json'), "w") as f:
                f.write(data)
            for old in sorted(os.listdir(hist))[:-HISTORY_KEEP]:
                os.remove(os.path.join(hist, old))
        except OSError as e:
            discover.log(f"planner: could not write history: {e}")


class PlanConflict(Exception):
    def __init__(self, version):
        super().__init__(f"the plan was changed by someone else (now version {version}); reload and retry")
        self.version = version


# ---------------------------------------------------------------------------
# Checks and export
# ---------------------------------------------------------------------------

def _cluster_rules(plan, cluster_id):
    c = plan["clusters"].get(cluster_id) or {}
    settings = plan["settings"]
    return (settings.get("platforms") or {}).get(c.get("platform")), settings["envs"].get(c.get("env")), c.get("env") or ""


def cluster_limits(cluster, cluster_cfg, env):
    """How much quota a cluster can hand out to projects, per CPU and memory (see the module docstring)."""
    failures = int(env["node_failures"]) if env else 0
    sizes = cluster.get("node_sizes")
    reserve_source = "entered" if cluster_cfg.get("platform_cpu") is not None or \
        cluster_cfg.get("platform_mem_gib") is not None else \
        "measured" if cluster.get("platform_measured_cpu") is not None else "none"
    out = {"node_failures": failures, "nodes_known": sizes is not None, "node_count": len(sizes or []),
           "reserve_source": reserve_source, "max_pct": env["max_quota_pct"] if env else None}
    for dim, alloc_key, size_key, cfg_key, measured_key in (
            ("cpu", "alloc_cpu", "cpu", "platform_cpu", "platform_measured_cpu"),
            ("mem", "alloc_mem_gib", "mem_gib", "platform_mem_gib", "platform_measured_mem_gib")):
        alloc = cluster.get(alloc_key)
        if reserve_source == "entered":
            reserve = cluster_cfg.get(cfg_key) or 0.0
        else:
            reserve = cluster.get(measured_key) or 0.0
        lost = sum(sorted((n[size_key] for n in sizes or []), reverse=True)[:failures]) if failures else 0.0
        if sizes is not None and failures >= len(sizes):
            lost = alloc or 0.0
        out[f"alloc_{dim}"] = alloc
        out[f"failover_{dim}"] = round(lost, 3)
        out[f"reserve_{dim}"] = round(reserve, 3)
        if alloc is None or not env:
            out[f"limit_{dim}"], out[f"binding_{dim}"], out[f"binding_{dim}_text"] = None, None, ""
            continue
        by_failures = max(0.0, alloc - lost - reserve)
        by_pct = max(0.0, env["max_quota_pct"] / 100 * (alloc - reserve))
        binding = "failures" if failures and by_failures <= by_pct else "pct"
        out[f"limit_{dim}"] = round(min(by_failures, by_pct), 3)
        out[f"binding_{dim}"] = binding
        out[f"binding_{dim}_text"] = (
            f"allocatable minus its {failures} largest node(s) and the platform reserve" if binding == "failures"
            else f"{env['max_quota_pct']:g} % of allocatable minus the platform reserve")
    return out


def evaluate(plan, inventory, mode="budget"):
    """Per project cost vs. budget (budget mode) or quota vs. envelope (capacity mode), per cluster quota vs. its
    limit for projects, plus the issues to show and fix."""
    clusters = {c["id"]: c for c in inventory["clusters"]}
    rancher_projects = {(p["cluster_id"], p["name"]) for p in inventory["projects"]}
    issues, project_rows, cluster_totals = [], {}, {}

    for pname, p in sorted(plan["projects"].items()):
        cost, unpriced = 0.0, []
        for cid, a in sorted(p["allocations"].items()):
            if not a["cpu"] and not a["memory_gib"]:
                continue
            cname = clusters.get(cid, {}).get("name", cid)
            if cid not in clusters:
                issues.append({"level": "warning", "text": f"{pname}: cluster {cid} is no longer in Rancher"})
            elif (cid, pname) not in rancher_projects:
                issues.append({"level": "warning",
                               "text": f"{pname}: no Rancher Project of that name on {cname} yet"})
            platform, _, _ = _cluster_rules(plan, cid)
            if platform:
                cost += a["cpu"] * platform["cpu_rate"] + a["memory_gib"] * platform["mem_rate"]
            else:
                unpriced.append(cname)
            t = cluster_totals.setdefault(cid, {"cpu": 0.0, "memory_gib": 0.0})
            t["cpu"] += a["cpu"]
            t["memory_gib"] += a["memory_gib"]
        if mode == "capacity":
            planned = {"cpu": sum(x["cpu"] for x in p["allocations"].values()),
                       "memory_gib": sum(x["memory_gib"] for x in p["allocations"].values())}
            row = {"cpu": round(planned["cpu"], 3), "memory_gib": round(planned["memory_gib"], 3),
                   "cpu_envelope": p["cpu_envelope"], "memory_gib_envelope": p["memory_gib_envelope"]}
            for key, name, unit in (("cpu", "CPU", ""), ("memory_gib", "memory", " GiB")):
                env_ = p[f"{key}_envelope"]
                if env_ is not None and planned[key] > env_ + 1e-9:
                    issues.append({"level": "critical",
                                   "text": f"{pname}: planned {name} quota {_fmt(planned[key])}{unit} across all "
                                           f"clusters is above its envelope of {_fmt(env_)}{unit}"})
            project_rows[pname] = row
            continue
        budget = p["monthly_budget"]
        row = {"cost": round(cost, 2), "budget": budget, "unpriced_clusters": unpriced}
        if unpriced:
            issues.append({"level": "warning", "text": f"{pname}: no platform set for {', '.join(unpriced)}, "
                                                       f"so its quota there has no price"})
        if budget is not None and cost > budget + 0.005:
            issues.append({"level": "critical", "text": f"{pname}: planned quota costs {cost:,.2f} a month, "
                                                        f"over its budget of {budget:,.2f}"})
        project_rows[pname] = row

    cluster_rows = {}
    for c in inventory["clusters"]:
        t = cluster_totals.get(c["id"], {"cpu": 0.0, "memory_gib": 0.0})
        _, env, env_name = _cluster_rules(plan, c["id"])
        cap = cluster_limits(c, plan["clusters"].get(c["id"]) or {}, env)
        planned = {"cpu": t["cpu"], "mem": t["memory_gib"]}
        row = {"planned_cpu": round(t["cpu"], 3), "planned_mem_gib": round(t["memory_gib"], 3), **cap}
        for dim in ("cpu", "mem"):
            lim = cap[f"limit_{dim}"]
            row[f"{dim}_pct"] = round(100 * planned[dim] / lim, 1) if lim else None
        has_plan = bool(t["cpu"] or t["memory_gib"])
        label = f"{c['name']} ({env_name})" if env_name else c["name"]
        if has_plan and not env:
            issues.append({"level": "warning", "text": f"{c['name']}: no environment set, so its limit for "
                                                       f"projects (node failures, headroom) can't be checked"})
        if has_plan and cap["reserve_source"] == "none":
            issues.append({"level": "warning", "text": f"{c['name']}: platform reserve not set, so the limit ignores "
                                                       f"what platform components request (enter the 'system' "
                                                       f"requests from that cluster's dashboard)"})
        if has_plan and env and cap["nodes_known"] and cap["node_failures"] >= cap["node_count"]:
            issues.append({"level": "critical",
                           "text": f"{label}: {cap['node_count']} schedulable node(s) can't tolerate "
                                   f"{cap['node_failures']} node failure(s), so no project quota fits"})
        elif env:
            if has_plan and not cap["nodes_known"] and cap["node_failures"]:
                issues.append({"level": "warning", "text": f"{c['name']}: Rancher reports no node sizes, so the "
                                                           f"node-failure rule can't be checked"})
            for dim, name in (("cpu", "CPU"), ("mem", "memory")):
                lim = cap[f"limit_{dim}"]
                if lim is not None and planned[dim] > lim + 1e-9:
                    unit = "" if dim == "cpu" else " GiB"
                    issues.append({"level": "critical",
                                   "text": f"{label}: planned {name} quota {_fmt(planned[dim])}{unit} is above its "
                                           f"limit for projects of {_fmt(lim)}{unit} ({cap[f'binding_{dim}_text']})"})
        cluster_rows[c["id"]] = row
    return {"projects": project_rows, "clusters": cluster_rows, "issues": issues}


def _fmt(n):
    return f"{n:g}" if n == int(n) else f"{round(n, 3):g}"


def _yaml_str(s):
    return json.dumps(s)  # a JSON string is a valid YAML scalar


def export_yaml(plan, inventory, mode="budget"):
    """The plan as allocation files (docs/04, "Allocation as code"): one YAML document per project."""
    clusters = {c["id"]: c for c in inventory["clusters"]}
    docs = []
    for pname, p in sorted(plan["projects"].items()):
        allocs = [(cid, a) for cid, a in sorted(p["allocations"].items(),
                                                 key=lambda kv: clusters.get(kv[0], {}).get("name", kv[0]))
                  if a["cpu"] or a["memory_gib"]]
        lines = [f"# allocations/projects/{pname}.yaml", f"project: {_yaml_str(pname)}"]
        if p.get("cost_center"):
            lines.append(f"costCenter: {_yaml_str(p['cost_center'])}")
        if p["owners"]:
            lines.append("owners: [" + ", ".join(_yaml_str(o) for o in p["owners"]) + "]")
        if p.get("monthly_budget") is not None:
            lines += ["budget:", f"  currency: {_yaml_str(plan['settings']['currency'])}",
                      f"  monthly: {_fmt(p['monthly_budget'])}"]
        lines.append("allocations:" if allocs else "allocations: []")
        for cid, a in allocs:
            _, env, env_name = _cluster_rules(plan, cid)
            factor = env["mem_limit_factor"] if env else 1.0
            lines += [f"  - cluster: {_yaml_str(clusters.get(cid, {}).get('name', cid))}"]
            if env_name:
                lines.append(f"    env: {_yaml_str(env_name)}")
            lines += ["    quota:",
                      f'      requests.cpu: "{_fmt(a["cpu"])}"',
                      f"      requests.memory: {_fmt(a['memory_gib'])}Gi",
                      f"      limits.memory: {_fmt(a['memory_gib'] * factor)}Gi"]
        docs.append("\n".join(lines) + "\n")
    tool = "capacity planner" if mode == "capacity" else "allocation planner"
    header = (f"# Allocation plan version {plan.get('version', 0)}, exported "
              f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} by the {tool}.\n"
              f"# Not applied to any cluster: commit as allocations/projects/<project>.yaml (docs/04).\n")
    return header + "---\n" + "---\n".join(docs) if docs else header
