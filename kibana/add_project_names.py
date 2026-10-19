#!/usr/bin/env python3
"""Add Rancher Project names to a dashboard .ndjson you already have, without regenerating it.

Takes a saved-objects file of the "Cluster resource report" dashboard (one of the files in this directory, or an
export from Kibana) and the Projects of the Rancher local cluster, and writes the names into the report's queries:
the Project ID -> name list becomes an ES|QL CASE in every report panel. Nothing is stored in Elasticsearch.

  kubectl get projects.management.cattle.io -A -o json > projects.json      # on the Rancher local cluster
  ./add_project_names.py cluster-resource-report.ndjson projects.json --out cluster-resources-with-names.ndjson

Import the output in Kibana (Saved objects -> Import, overwrite). Run it again on either file after adding or
renaming Projects: names already in the file are replaced. A Project that is not in projects.json keeps its ID.

Self-contained: standard library only, does not need build_dashboard.py.
"""

import argparse
import json
import re
import sys

# The report's queries compute the Project label in one place: "pname = project" (ID) or an earlier CASE of names.
PNAME_RE = re.compile(r"\bpname = (?:project|CASE\(project == .*, project\))$", re.M)
# With names, Projects called System or ...platform... count as System, as in the in-cluster report.
SYSTEM_PROJECTS = ' OR TO_LOWER(pname) RLIKE "system|.*platform.*"'
CATEGORY_RE = re.compile(r'\)(?:' + re.escape(SYSTEM_PROJECTS) + r')?(, "System", project == )')
# The "Project owner" panels keep the picked Project's documents right after FROM, with a condition on the raw
# Project ID field that maps the picked name back to its IDs (one Project name can exist on several clusters).
OWNER_RE = re.compile(r'^(\| WHERE \?project == "\(all projects\)" OR )(?:.* OR )?([A-Za-z0-9_.@]+) == \?project$', re.M)
ABOUT_IDS = "Projects show their ID; regenerate with `--rancher-projects` for names and System projects."
ABOUT_NAMES = "Project names from Rancher."


def project_names(projects):
    """Project ID -> display name from the parsed `kubectl get projects.management.cattle.io -A -o json`."""
    names = {}
    for it in projects.get("items", []):
        pid, name = it["metadata"]["name"], (it.get("spec") or {}).get("displayName") or it["metadata"]["name"]
        if names.get(pid, name) != name:
            print(f"warning: Project ID {pid} has two names ({names[pid]}, {name}); using {name}", file=sys.stderr)
        names[pid] = name
    return names


def esql_str(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pname_expr(names):
    cases = ", ".join(f"project == {esql_str(k)}, {esql_str(v)}" for k, v in sorted(names.items()))
    return f"pname = CASE({cases}, project)"


def owner_condition(field, names):
    """The picked Project's IDs for its name, then the ID itself (a Project without a name is picked by ID)."""
    ids = {}
    for pid, name in sorted(names.items()):
        ids.setdefault(name, []).append(pid)
    return "".join(f"(?project == {esql_str(name)} AND {field} IN ({', '.join(map(esql_str, pids))})) OR "
                   for name, pids in sorted(ids.items())) + f"{field} == ?project"


def patch_text(text, names, count):
    """One query (or other text): put the names in, count the queries changed."""
    expr = pname_expr(names)
    text, n = PNAME_RE.subn(lambda m: expr, text)
    if n:
        count[0] += n
        text = CATEGORY_RE.sub(lambda m: ")" + SYSTEM_PROJECTS + m.group(1), text)
    text, n = OWNER_RE.subn(lambda m: m.group(1) + owner_condition(m.group(2), names), text)
    count[0] += n
    return text.replace(ABOUT_IDS, ABOUT_NAMES)


def patch(value, names, count):
    """Walk a saved object. Kibana keeps the panels as JSON inside a string (panelsJSON), so strings that hold
    JSON are opened, patched and written back."""
    if isinstance(value, dict):
        return {k: patch(v, names, count) for k, v in value.items()}
    if isinstance(value, list):
        return [patch(v, names, count) for v in value]
    if not isinstance(value, str) or ("pname" not in value and "?project" not in value and ABOUT_IDS not in value):
        return value
    if value[:1] in "[{":
        try:
            return json.dumps(patch(json.loads(value), names, count))
        except ValueError:
            pass
    return patch_text(value, names, count)


def add_names(lines, names):
    """Patched .ndjson lines and the number of query copies changed."""
    count, out = [0], []
    for line in lines:
        if line.strip():
            out.append(json.dumps(patch(json.loads(line), names, count), separators=(",", ":")) + "\n")
    return out, count[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard", help="dashboard saved-objects file (.ndjson)")
    ap.add_argument("projects", help="output of `kubectl get projects.management.cattle.io -A -o json`")
    ap.add_argument("--out", required=True, help="file to write (the input is not changed)")
    args = ap.parse_args()
    with open(args.projects) as f:
        names = project_names(json.load(f))
    if not names:
        sys.exit(f"no Projects in {args.projects}; is it from the Rancher local cluster?")
    with open(args.dashboard) as f:
        lines, changed = add_names(f.readlines(), names)
    if not changed:
        sys.exit(f"no report query found in {args.dashboard} (no \"pname = project\"); nothing written. "
                 "The -no-project file has no Project to name.")
    with open(args.out, "w") as f:
        f.writelines(lines)
    print(f"wrote {args.out}: {len(names)} Project names in {changed} queries")


if __name__ == "__main__":
    main()
