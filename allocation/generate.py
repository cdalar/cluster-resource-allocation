#!/usr/bin/env python3
"""Generate the Kubernetes ResourceQuotas and LimitRanges for the CPU and memory allocations in an allocations directory.

Input (docs/04, "Allocation as code"), kept in Git and changed by pull request:

  allocations/settings.yaml          clusters (environment, capacity), environment rules, container defaults
  allocations/projects/<name>.yaml   one project: per cluster its namespaces with vCPU and memory, optionally a
                                     total for the project and its own container defaults

Output, one file per namespace under --out: <cluster>/<namespace>.yaml with a ResourceQuota (requests.cpu,
requests.memory, limits.memory, pods) and a LimitRange (defaults for containers without requests/limits). Apply each
cluster's directory on that cluster. Plain Kubernetes: no Rancher objects.

Checks (exit status 1 on any error, so CI can run it on every pull request):
  - required fields, known clusters, valid quantities, one entry per project and cluster
  - limits.memory <= requests.memory x memoryLimitFactor of the environment (1 in prod)
  - a namespace belongs to one project only; the namespaces fit in the project's total, if it has one
  - per cluster: all namespaces together <= the cluster's capacity

  ./generate.py examples --check                 # checks only (CI on pull requests)
  ./generate.py examples --out build             # writes build/<cluster>/<namespace>.yaml
  kubectl --context <cluster> apply --server-side --field-manager=allocations -f build/<cluster>/

Reads YAML in the block style of the examples (maps, lists, [a, b] lists, comments, quoted strings); standard
library only, like the other tools in this repository. Writes nothing to any cluster.
"""

import argparse
import json
import os
import re
import sys

# ---- YAML (the subset the allocation files use) ----


class YamlError(ValueError):
    pass


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(text, where):
    t = text.strip()
    if t == "" or t in ("~", "null"):
        return None
    if t[0] == '"':
        try:
            return json.loads(t)
        except ValueError:
            raise YamlError(f"{where}: bad quoted string {t}")
    if t[0] == "'":
        if not t.endswith("'") or len(t) < 2:
            raise YamlError(f"{where}: bad quoted string {t}")
        return t[1:-1].replace("''", "'")
    if t == "{}":
        return {}
    if t[0] == "[":
        if not t.endswith("]"):
            raise YamlError(f"{where}: unclosed list {t}")
        inner, items, cur, quote = t[1:-1], [], "", None
        for ch in inner:
            if quote:
                quote = None if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch == ",":
                items.append(cur)
                cur = ""
                continue
            cur += ch
        if cur.strip():
            items.append(cur)
        return [_scalar(i, where) for i in items]
    if t[0] == "{":
        raise YamlError(f"{where}: {{...}} maps are not supported; write the keys on their own lines")
    if t in ("true", "false"):
        return t == "true"
    if re.fullmatch(r"[-+]?[0-9]+", t):
        return int(t)
    if re.fullmatch(r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?", t):
        return float(t)
    return t


_KEY_RE = re.compile(r"""([^\s\-"'\[{#][^:]*?|"[^"]*"|'[^']*')\s*:(\s|$)""")


def load_yaml(text, name="<yaml>"):
    """All documents of a YAML text (separated by ---), as Python values."""
    docs, cur = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.rstrip() == "---":
            docs.append(cur)
            cur = []
            continue
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        where = f"{name}:{n}"
        indent = len(line) - len(line.lstrip())
        if "\t" in line[:indent]:
            raise YamlError(f"{where}: tab in indentation")
        text_ = line.strip()
        # "- key: value" -> a "-" line and the map's first key on its own (deeper) line
        while text_.startswith("- ") and _KEY_RE.match(text_[2:].lstrip()):
            rest = text_[2:].lstrip()
            cur.append((indent, "-", where))
            indent += len(text_) - len(rest)
            text_ = rest
        cur.append((indent, text_, where))
    docs.append(cur)
    out = []
    for lines in docs:
        if not lines:
            continue
        value, i = _block(lines, 0, lines[0][0])
        if i != len(lines):
            raise YamlError(f"{lines[i][2]}: unexpected indentation")
        out.append(value)
    return out


def _is_item(text):
    return text == "-" or text.startswith("- ")


def _block(lines, i, indent):
    return (_list if _is_item(lines[i][1]) else _map)(lines, i, indent)


def _nested(lines, i, indent, where):
    """The value of a "key:" or "-" with nothing after it: the deeper block that follows, or null. A list may sit at
    the key's own indentation ("key:\n- a")."""
    if i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and _is_item(lines[i][1]))):
        return _block(lines, i, lines[i][0])
    return None, i


def _list(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][0] == indent and _is_item(lines[i][1]):
        _, text, where = lines[i]
        if text == "-":
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                value, i = _block(lines, i + 1, lines[i + 1][0])
            else:
                value, i = None, i + 1
        else:
            value, i = _scalar(text[2:], where), i + 1
        items.append(value)
    if i < len(lines) and lines[i][0] > indent:
        raise YamlError(f"{lines[i][2]}: unexpected indentation")
    return items, i


def _map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not _is_item(lines[i][1]):
        _, text, where = lines[i]
        m = _KEY_RE.match(text)
        if not m:
            raise YamlError(f"{where}: expected 'key: value', got {text!r}")
        key = m.group(1)
        if key[0] in "\"'":
            key = key[1:-1]
        if key in out:
            raise YamlError(f"{where}: duplicate key {key}")
        rest = text[m.end():].strip()
        if rest:
            out[key], i = _scalar(rest, where), i + 1
        else:
            out[key], i = _nested(lines, i + 1, indent, where)
    if i < len(lines) and lines[i][0] > indent:
        raise YamlError(f"{lines[i][2]}: unexpected indentation")
    return out, i


# ---- quantities ----

MEM_UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50,
             "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "": 1}


def millicores(v, where):
    """CPU (vCPU number, "1.5" or "500m") -> millicores."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(m?)", str(v).strip())
    if not m or isinstance(v, bool):
        raise ValueError(f"{where}: not a CPU quantity: {v!r} (e.g. 4, 0.5 or 500m)")
    n = float(m.group(1))
    return round(n) if m.group(2) else round(n * 1000)


def mem_bytes(v, where):
    """Memory ("160Gi", "512Mi", "1G", bytes) -> bytes."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P|)", str(v).strip())
    if not m or isinstance(v, bool):
        raise ValueError(f"{where}: not a memory quantity: {v!r} (e.g. 160Gi or 512Mi)")
    return round(float(m.group(1)) * MEM_UNITS[m.group(2)])


def cpu_str(mc):
    return str(mc // 1000) if mc % 1000 == 0 else f"{mc}m"


def mem_str(b):
    for unit in ("Ti", "Gi", "Mi", "Ki"):
        if b % MEM_UNITS[unit] == 0:
            return f"{b // MEM_UNITS[unit]}{unit}"
    return str(b)


def fmt_cpu(mc):
    return f"{mc / 1000:g}"


def fmt_mem(b):
    return f"{b / MEM_UNITS['Gi']:g} GiB"


# ---- model and checks ----

PLANNER_QUOTA = {"requests.cpu": "cpu", "requests.memory": "memory", "limits.memory": "memoryLimit", "pods": "pods"}


class Checker:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, text):
        self.errors.append(text)

    def warn(self, text):
        self.warnings.append(text)


def resources(spec, where, factor, ck, required=True):
    """{cpu, memory, memoryLimit, pods} of an allocation, namespace default or namespace -> millicores and bytes.
    Missing cpu/memory: None (an error when required). memoryLimit defaults to memory x factor."""
    if not isinstance(spec, dict):
        ck.error(f"{where}: expected a map with cpu and memory")
        return None
    spec = dict(spec)
    for k, v in (spec.pop("quota", None) or {}).items():  # the allocation planner's export
        if k not in PLANNER_QUOTA:
            ck.error(f"{where}.quota: unknown key {k} (supported: {', '.join(PLANNER_QUOTA)})")
        else:
            spec.setdefault(PLANNER_QUOTA[k], v)
    out = {}
    try:
        if spec.get("cpu") is None or spec.get("memory") is None:
            if required:
                ck.error(f"{where}: needs cpu and memory")
            return None
        out["cpu"] = millicores(spec["cpu"], f"{where}.cpu")
        out["memory"] = mem_bytes(spec["memory"], f"{where}.memory")
        out["memoryLimit"] = (mem_bytes(spec["memoryLimit"], f"{where}.memoryLimit")
                              if spec.get("memoryLimit") is not None else round(out["memory"] * factor))
        if spec.get("pods") is not None:
            if not isinstance(spec["pods"], int) and not str(spec["pods"]).isdigit():
                raise ValueError(f"{where}.pods: not a number: {spec['pods']!r}")
            out["pods"] = int(spec["pods"])
    except ValueError as e:
        ck.error(str(e))
        return None
    if out["cpu"] <= 0 or out["memory"] <= 0:
        ck.error(f"{where}: cpu and memory must be above 0")
    if out["memoryLimit"] < out["memory"]:
        ck.error(f"{where}: memoryLimit {mem_str(out['memoryLimit'])} is below memory {mem_str(out['memory'])}")
    elif out["memoryLimit"] > out["memory"] * factor + 0.5:
        ck.error(f"{where}: memoryLimit {mem_str(out['memoryLimit'])} is above {factor:g}x memory "
                 f"{mem_str(out['memory'])} (memoryLimitFactor of its environment)")
    return out


def load_dir(path, ck):
    """settings.yaml and projects/*.yaml of an allocations directory."""
    def read(f):
        try:
            with open(f) as fh:
                return load_yaml(fh.read(), os.path.relpath(f))
        except (OSError, YamlError) as e:
            ck.error(str(e))
            return []
    settings = (read(os.path.join(path, "settings.yaml")) or [{}])[0] or {}
    projects = []
    pdir = os.path.join(path, "projects")
    if not os.path.isdir(pdir):
        ck.error(f"{pdir}: missing (one YAML file per project)")
        return settings, projects
    for name in sorted(os.listdir(pdir)):
        if name.endswith((".yaml", ".yml")):
            for doc in read(os.path.join(pdir, name)):
                if doc:
                    projects.append((os.path.join("projects", name), doc))
    return settings, projects


def build_model(settings, projects, ck):
    """Validate everything; returns the clusters, the per-project allocations with numbers, and the totals."""
    envs = settings.get("environments") or {}
    clusters = {}
    for cname, c in (settings.get("clusters") or {}).items():
        where = f"settings.yaml: clusters.{cname}"
        c = c or {}
        env = c.get("env")
        if env not in envs:
            ck.error(f"{where}: env {env!r} is not one of the environments ({', '.join(envs) or 'none defined'})")
            continue
        factor = (envs[env] or {}).get("memoryLimitFactor", 1)
        if not isinstance(factor, (int, float)) or factor < 1:
            ck.error(f"settings.yaml: environments.{env}.memoryLimitFactor must be a number >= 1")
            factor = 1
        cap = resources(c["capacity"], f"{where}.capacity", 1, ck) if c.get("capacity") is not None else None
        clusters[cname] = {"name": cname, "env": env, "factor": factor, "capacity": cap}
    if not clusters:
        ck.error("settings.yaml: no clusters")
    default_cd = settings.get("containerDefault")

    allocs, seen, owner = [], {}, {}
    for file, p in projects:
        if not isinstance(p, dict) or not isinstance(p.get("project"), str) or not p["project"].strip():
            ck.error(f"{file}: needs 'project: <name>'")
            continue
        pname = p["project"]
        if pname in seen:
            ck.error(f"{file}: project {pname} is also defined in {seen[pname]}")
            continue
        seen[pname] = file
        if not p.get("owners"):
            ck.warn(f"{file}: no owners")
        entries = p.get("allocations")
        if entries is None or not isinstance(entries, list):
            ck.error(f"{file}: needs 'allocations:' (a list, may be [])")
            continue
        done = set()
        for n, a in enumerate(entries):
            where = f"{file}: allocations[{n}]"
            if not isinstance(a, dict) or a.get("cluster") not in clusters:
                ck.error(f"{where}: cluster {(a or {}).get('cluster')!r} is not in settings.yaml")
                continue
            c = clusters[a["cluster"]]
            where = f"{file}: {pname} on {c['name']}"
            if c["name"] in done:
                ck.error(f"{where}: listed twice")
                continue
            done.add(c["name"])
            has_total = a.get("cpu") is not None or a.get("memory") is not None or a.get("quota") is not None
            total = resources(a, where, c["factor"], ck) if has_total else None
            nsd = (resources(a["namespaceDefault"], f"{where}: namespaceDefault", c["factor"], ck)
                   if a.get("namespaceDefault") is not None else None)
            cd_spec = a.get("containerDefault") or default_cd
            cd = resources(cd_spec, f"{where}: containerDefault", c["factor"], ck, required=False) if cd_spec else None
            if not a.get("namespaces"):
                ck.error(f"{where}: list its namespaces (each gets a ResourceQuota)")
                continue
            namespaces = []
            for k, ns in enumerate(a["namespaces"]):
                if not isinstance(ns, dict) or not ns.get("name"):
                    ck.error(f"{where}: namespaces[{k}] needs a name")
                    continue
                nwhere = f"{where}: namespace {ns['name']}"
                key = (c["name"], ns["name"])
                if key in owner:
                    ck.error(f"{nwhere}: already allocated to {owner[key]}")
                    continue
                owner[key] = pname
                if ns.get("cpu") is None and ns.get("memory") is None:
                    if nsd is None:
                        ck.error(f"{nwhere}: no cpu/memory and no namespaceDefault")
                        continue
                    own = dict(nsd)
                    if ns.get("pods") is not None:
                        own["pods"] = int(ns["pods"])
                else:
                    own = resources(ns, nwhere, c["factor"], ck)
                if own is not None:
                    namespaces.append((ns["name"], own))
            if total is not None and namespaces:
                for k, show in (("cpu", fmt_cpu), ("memory", fmt_mem), ("memoryLimit", fmt_mem)):
                    used = sum(q[k] for _, q in namespaces)
                    if used > total[k]:
                        ck.error(f"{where}: the namespaces add up to {show(used)} {k}, above the project's "
                                 f"{show(total[k])}")
            allocs.append({"project": pname, "cluster": c, "total": total, "containerDefault": cd,
                           "namespaces": namespaces, "file": file})

    usage = {}
    for a in allocs:
        u = usage.setdefault(a["cluster"]["name"], {"cpu": 0, "memory": 0})
        for _, q in a["namespaces"]:
            u["cpu"] += q["cpu"]
            u["memory"] += q["memory"]
    for cname, u in usage.items():
        cap = clusters[cname]["capacity"]
        if cap is None:
            ck.warn(f"{cname}: no capacity in settings.yaml, so the sum of the namespaces is not checked")
            continue
        for k, show in (("cpu", fmt_cpu), ("memory", fmt_mem)):
            if u[k] > cap[k]:
                ck.error(f"{cname}: the namespaces get {show(u[k])} {k} in total, above its capacity of "
                         f"{show(cap[k])}")
    return clusters, allocs, usage


# ---- output ----

LABELS = "allocation.cluster-resource-allocation/"


def namespace_objects(a, ns, q):
    labels = {LABELS + "managed": "true", LABELS + "project": a["project"]}
    hard = {"requests.cpu": cpu_str(q["cpu"]), "requests.memory": mem_str(q["memory"]),
            "limits.memory": mem_str(q["memoryLimit"])}
    if "pods" in q:
        hard["pods"] = str(q["pods"])
    objs = [{"apiVersion": "v1", "kind": "ResourceQuota",
             "metadata": {"name": "allocation", "namespace": ns, "labels": labels}, "spec": {"hard": hard}}]
    cd = a["containerDefault"]
    if cd:
        objs.append({"apiVersion": "v1", "kind": "LimitRange",
                     "metadata": {"name": "allocation-defaults", "namespace": ns, "labels": labels},
                     "spec": {"limits": [{"type": "Container",
                                          "defaultRequest": {"cpu": cpu_str(cd["cpu"]), "memory": mem_str(cd["memory"])},
                                          "default": {"memory": mem_str(cd["memoryLimit"])}}]}})
    return objs


def to_yaml(value, indent=0):
    """Block YAML for dicts, lists and scalars (strings quoted as JSON when they need it)."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{_ystr(k)}:\n{to_yaml(v, indent + 2)}")
            else:
                lines.append(f"{pad}{_ystr(k)}: {to_yaml(v, indent + 2)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for v in value:
            text = to_yaml(v, indent + 2)
            lines.append(f"{pad}- {text.lstrip()}" if isinstance(v, (dict, list)) and v else f"{pad}- {text}")
        return "\n".join(lines)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _ystr(value)


def _ystr(s):
    s = str(s)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./-]*", s) and s not in ("true", "false", "null", "yes", "no", "on", "off"):
        return s
    return json.dumps(s)


def manifests(allocs):
    """{relative path: YAML text}: <cluster>/<namespace>.yaml"""
    header = "# Generated by allocation/generate.py from the allocations in Git. Do not edit; change the allocation.\n"
    files = {}
    for a in allocs:
        for ns, q in a["namespaces"]:
            objs = namespace_objects(a, ns, q)
            files[f"{a['cluster']['name']}/{ns}.yaml"] = header + "".join("---\n" + to_yaml(o) + "\n" for o in objs)
    return files


def summary(clusters, allocs, usage):
    lines = ["cluster              env    project                namespaces    vCPU      memory"]
    for a in sorted(allocs, key=lambda a: (a["cluster"]["name"], a["project"])):
        cpu = sum(q["cpu"] for _, q in a["namespaces"])
        mem = sum(q["memory"] for _, q in a["namespaces"])
        lines.append(f"{a['cluster']['name']:<20} {a['cluster']['env']:<6} {a['project']:<22} "
                     f"{len(a['namespaces']):>10}  {fmt_cpu(cpu):>6}  {fmt_mem(mem):>10}")
    lines.append("")
    lines.append("cluster              allocated vCPU / capacity      allocated memory / capacity")
    for cname in sorted(usage):
        cap, u = clusters[cname]["capacity"], usage[cname]
        pct = (lambda k: f" ({100 * u[k] / cap[k]:.0f} %)") if cap else (lambda k: "")
        lines.append(f"{cname:<20} {fmt_cpu(u['cpu']):>6} / {fmt_cpu(cap['cpu']) if cap else '?':<6}{pct('cpu'):<9}"
                     f"      {fmt_mem(u['memory'])} / {fmt_mem(cap['memory']) if cap else '?'}{pct('memory')}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("allocations", help="directory with settings.yaml and projects/*.yaml")
    ap.add_argument("--out", default="build", help="output directory (default build); the generated YAML files in "
                                                    "it are replaced")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing (for pull requests)")
    args = ap.parse_args(argv)
    ck = Checker()
    settings, projects = load_dir(args.allocations, ck)
    clusters, allocs, usage = build_model(settings, projects, ck) if not ck.errors else ({}, [], {})
    if allocs:
        print(summary(clusters, allocs, usage))
    for w in ck.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in ck.errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if ck.errors:
        print(f"{len(ck.errors)} error(s); nothing written", file=sys.stderr)
        return 1
    if args.check:
        print("\nall checks passed")
        return 0
    files = manifests(allocs)
    if os.path.isdir(args.out):  # remove what an earlier run wrote: generated YAML only
        for dirpath, _, names in os.walk(args.out, topdown=False):
            for n in names:
                full = os.path.join(dirpath, n)
                if n.endswith(".yaml"):
                    with open(full) as f:
                        generated = f.readline().startswith("# Generated by allocation/generate.py")
                    if generated:
                        os.remove(full)
            if dirpath != args.out and not os.listdir(dirpath):
                os.rmdir(dirpath)
    for path, text in sorted(files.items()):
        full = os.path.join(args.out, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    print(f"\nwrote {len(files)} file(s) to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
