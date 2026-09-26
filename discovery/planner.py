"""Allocation planner: budgets and quotas per Rancher Project and cluster, planned but never applied.

The planner is a planning aid for the platform team on the Rancher local cluster (docs/04, "Allocation
planner"). It reads the Rancher inventory (clusters with their capacity, projects with their current quota) and
keeps the plan in a JSON file in the report's data directory. It writes nothing to any cluster: the output is
the allocation files of docs/04 ("Allocation as code"), which go through the Git / Terraform flow.

The plan holds, per project, a monthly budget and a CPU/memory quota per cluster. Either can drive the other:
the page converts a budget into quota with the cluster's unit rates, and the checks here compare the cost of the
planned quota with the budget and the planned quota per cluster with the headroom rule of its environment.
"""

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone

import discover

STATE_FILE = "planner.json"
HISTORY_DIR = "planner-history"
HISTORY_KEEP = 30
MAX_STATE_BYTES = 1_000_000

# Hidden by default: Rancher's own projects, not streams with a budget.
RANCHER_BUILTIN_PROJECTS = ("System", "Default")

# docs/03-allocation-model.md (illustrative rates, headroom table) and docs/02-resource-standards.md (S3:
# memory limit = request in prod, up to 2x in non-prod). Meant to be edited in the planner.
DEFAULT_SETTINGS = {
    "currency": "EUR",
    "platforms": {
        "onprem": {"cpu_rate": 25.0, "mem_rate": 6.25},
        "aks": {"cpu_rate": 25.0, "mem_rate": 6.25},
    },
    "envs": {
        "prod": {"max_quota_pct": 80.0, "mem_limit_factor": 1.0},
        "acc": {"max_quota_pct": 100.0, "mem_limit_factor": 2.0},
        "test": {"max_quota_pct": 100.0, "mem_limit_factor": 2.0},
        "dev": {"max_quota_pct": 150.0, "mem_limit_factor": 2.0},
    },
}


def empty_state():
    return {"version": 0, "updated_at": None, "settings": json.loads(json.dumps(DEFAULT_SETTINGS)),
            "clusters": {}, "projects": {}}


# ---------------------------------------------------------------------------
# Rancher inventory (read-only)
# ---------------------------------------------------------------------------

def _qty(value, divisor=1):
    return round(discover.parse_quantity(value) / divisor, 3) if value else None


def parse_inventory(clusters_json, projects_json):
    """Clusters (capacity, requests) and projects (current quota) from Rancher's management objects."""
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
        discover.kget(local_context, "projects.management.cattle.io", kubeconfig=local_kubeconfig))


def add_report_requests(inventory, report):
    """Current requests per project from this collector's own report (only the cluster it scans)."""
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


def validate_state(raw):
    """Normalised copy of a plan sent by the page; raises PlanError on anything malformed."""
    if not isinstance(raw, dict):
        raise PlanError("plan must be an object")
    settings = raw.get("settings") or {}
    platforms, envs = {}, {}
    for name, p in (settings.get("platforms") or {}).items():
        _name(name, "platform")
        platforms[name] = {"cpu_rate": _num(p.get("cpu_rate"), f"{name}.cpu_rate"),
                           "mem_rate": _num(p.get("mem_rate"), f"{name}.mem_rate")}
    for name, e in (settings.get("envs") or {}).items():
        _name(name, "environment")
        envs[name] = {"max_quota_pct": _num(e.get("max_quota_pct"), f"{name}.max_quota_pct", 1, 1000),
                      "mem_limit_factor": _num(e.get("mem_limit_factor"), f"{name}.mem_limit_factor", 1, 10)}
    if not platforms or not envs:
        raise PlanError("settings need at least one platform and one environment")
    clusters = {}
    for cid, c in (raw.get("clusters") or {}).items():
        _name(cid, "cluster id")
        env, platform = c.get("env") or "", c.get("platform") or ""
        if env and env not in envs:
            raise PlanError(f"cluster {cid}: unknown environment {env!r}")
        if platform and platform not in platforms:
            raise PlanError(f"cluster {cid}: unknown platform {platform!r}")
        clusters[cid] = {"env": env, "platform": platform}
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
        budget = p.get("monthly_budget")
        projects[pname] = {
            "cost_center": _text(p.get("cost_center"), f"{pname}.cost_center"),
            "owners": [_text(o, f"{pname}.owners") for o in owners if o],
            "monthly_budget": None if budget in (None, "") else _num(budget, f"{pname}.monthly_budget"),
            "allocations": allocations,
        }
    return {
        "settings": {"currency": _text(settings.get("currency") or "EUR", "currency", 8) or "EUR",
                     "platforms": platforms, "envs": envs},
        "clusters": clusters,
        "projects": projects,
    }


class PlanStore:
    """The plan in <data_dir>/planner.json, with a version for optimistic locking and a short history."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, STATE_FILE)

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return empty_state()

    def save(self, raw, expected_version):
        """Validate and write raw if the stored version is still expected_version; returns the saved plan."""
        current = self.load()
        if expected_version != current.get("version", 0):
            raise PlanConflict(current.get("version", 0))
        plan = validate_state(raw)
        plan["version"] = current.get("version", 0) + 1
        plan["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        os.makedirs(self.data_dir, exist_ok=True)
        data = json.dumps(plan, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".planner-")
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, self.path)
        self._keep_history(plan, data)
        return plan

    def _keep_history(self, plan, data):
        hist = os.path.join(self.data_dir, HISTORY_DIR)
        try:
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, f'planner-v{plan["version"]:05d}.json'), "w") as f:
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
    return settings["platforms"].get(c.get("platform")), settings["envs"].get(c.get("env")), c.get("env") or ""


def evaluate(plan, inventory):
    """Cost per project and headroom per cluster for the plan, plus the issues to show and fix."""
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
        row = {"planned_cpu": round(t["cpu"], 3), "planned_mem_gib": round(t["memory_gib"], 3),
               "cpu_pct": None, "mem_pct": None, "max_pct": env["max_quota_pct"] if env else None}
        if c["alloc_cpu"]:
            row["cpu_pct"] = round(100 * t["cpu"] / c["alloc_cpu"], 1)
        if c["alloc_mem_gib"]:
            row["mem_pct"] = round(100 * t["memory_gib"] / c["alloc_mem_gib"], 1)
        if (t["cpu"] or t["memory_gib"]) and not env:
            issues.append({"level": "warning", "text": f"{c['name']}: no environment set, so its headroom "
                                                       f"rule can't be checked"})
        elif env:
            for dim, pct in (("CPU", row["cpu_pct"]), ("memory", row["mem_pct"])):
                if pct is not None and pct > env["max_quota_pct"]:
                    issues.append({"level": "critical",
                                   "text": f"{c['name']} ({env_name}): planned {dim} quota is {pct:g} % of "
                                           f"allocatable, above the {env['max_quota_pct']:g} % allowed"})
        cluster_rows[c["id"]] = row
    return {"projects": project_rows, "clusters": cluster_rows, "issues": issues}


def _fmt(n):
    return f"{n:g}" if n == int(n) else f"{round(n, 3):g}"


def _yaml_str(s):
    return json.dumps(s)  # a JSON string is a valid YAML scalar


def export_yaml(plan, inventory):
    """The plan as allocation files (docs/04, "Allocation as code"): one YAML document per project."""
    clusters = {c["id"]: c for c in inventory["clusters"]}
    docs = []
    for pname, p in sorted(plan["projects"].items()):
        allocs = [(cid, a) for cid, a in sorted(p["allocations"].items(),
                                                 key=lambda kv: clusters.get(kv[0], {}).get("name", kv[0]))
                  if a["cpu"] or a["memory_gib"]]
        lines = [f"# allocations/projects/{pname}.yaml", f"project: {_yaml_str(pname)}"]
        if p["cost_center"]:
            lines.append(f"costCenter: {_yaml_str(p['cost_center'])}")
        if p["owners"]:
            lines.append("owners: [" + ", ".join(_yaml_str(o) for o in p["owners"]) + "]")
        if p["monthly_budget"] is not None:
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
    header = (f"# Allocation plan version {plan.get('version', 0)}, exported "
              f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} by the allocation planner.\n"
              f"# Not applied to any cluster: commit as allocations/projects/<project>.yaml (docs/04).\n")
    return header + "---\n" + "---\n".join(docs) if docs else header
