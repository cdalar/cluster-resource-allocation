#!/usr/bin/env python3
"""Publish Rancher Project names to a lookup index in Elasticsearch, for the Kibana dashboards.

The metrics carry only the Project ID (resource.attributes.rancher.project.id). This script writes one document
per Project (ID, display name) to a small index with `index.mode: lookup`; a report generated with
`build_dashboard.py --project-lookup-index <index>` joins the names from it (ES|QL LOOKUP JOIN, Elasticsearch 9.1+).
Run it again after adding or renaming Projects, by hand or from a scheduler: it replaces the names and removes
Projects that no longer exist.

  ./publish_project_names.py https://elastic.example:9200 --context rancher-local
      # reads the Projects with kubectl from the Rancher local cluster
  ./publish_project_names.py https://elastic.example:9200 --rancher-projects projects.json
      # or from a file: kubectl get projects.management.cattle.io -A -o json
  ./publish_project_names.py https://elastic.example:9200 --rancher-projects projects.json --dry-run

Elasticsearch login as for `build_dashboard.py --check`: $ES_API_KEY, or $ES_USER / $ES_PASSWORD; $ES_INSECURE=1
for a self-signed certificate. The key needs create_index, index, delete and view_index_metadata on this one index
(the script also creates it and removes old Projects). Reads Rancher, writes only this index.

Standard library and kubectl only.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

import build_dashboard as bd

DEFAULT_LOOKUP_INDEX = "rancher-projects"
INDEX_BODY = {
    "settings": {"index": {"mode": "lookup"}},
    "mappings": {"properties": {bd.LOOKUP_KEY: {"type": "keyword"}, bd.LOOKUP_NAME: {"type": "keyword"}}},
}


def kubectl_projects(context):
    cmd = ["kubectl"] + (["--context", context] if context else []) + [
        "get", "projects.management.cattle.io", "-A", "-o", "json"]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def documents(names):
    """One document per Project; its ID is the document ID, so a Project never matches twice in the join."""
    return [{bd.LOOKUP_KEY: pid, bd.LOOKUP_NAME: name} for pid, name in sorted(names.items())]


def bulk_body(index, docs):
    lines = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": doc[bd.LOOKUP_KEY]}}))
        lines.append(json.dumps(doc))
    return "\n".join(lines) + "\n"


def stale_query(docs):
    """Everything in the index that is not one of these Projects."""
    return {"query": {"bool": {"must_not": {"ids": {"values": [d[bd.LOOKUP_KEY] for d in docs]}}}}}


class Elastic:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def request(self, method, path, body=None, content_type="application/json"):
        headers, ctx = bd.es_auth(content_type)
        data = body if isinstance(body, (bytes, type(None))) else (
            body if isinstance(body, str) else json.dumps(body)).encode()
        req = urllib.request.Request(self.url + path, data, headers, method=method)
        with urllib.request.urlopen(req, context=ctx) as r:
            return json.loads(r.read().decode() or "{}")


def publish(es, index, docs):
    """Create the lookup index if it is missing, write the Projects, remove the others. Returns (written, removed)."""
    try:
        settings = es.request("GET", f"/{index}/_settings")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        es.request("PUT", f"/{index}", INDEX_BODY)
    else:
        mode = next(iter(settings.values()))["settings"]["index"].get("mode")
        if mode != "lookup":
            sys.exit(f"index {index} exists but is not a lookup index (index.mode: {mode or 'standard'}); "
                     "delete it or choose another --index")
    result = es.request("POST", f"/{index}/_bulk?refresh=true", bulk_body(index, docs), "application/x-ndjson")
    if result.get("errors"):
        failed = [it["index"] for it in result["items"] if it["index"].get("error")]
        sys.exit(f"{len(failed)} of {len(docs)} Projects not written; first error: {json.dumps(failed[0]['error'])}")
    removed = es.request("POST", f"/{index}/_delete_by_query?refresh=true", stale_query(docs)).get("deleted", 0)
    return len(docs), removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("es_url", metavar="ES_URL", help="Elasticsearch, e.g. https://elastic.example:9200")
    ap.add_argument("--index", default=DEFAULT_LOOKUP_INDEX, help=f"lookup index (default {DEFAULT_LOOKUP_INDEX})")
    ap.add_argument("--rancher-projects", metavar="FILE",
                    help="output of `kubectl get projects.management.cattle.io -A -o json` on the Rancher local "
                         "cluster; without it the script runs that command itself")
    ap.add_argument("--context", help="kubeconfig context of the Rancher local cluster (default: the current one)")
    ap.add_argument("--dry-run", action="store_true", help="print the documents, write nothing")
    args = ap.parse_args()
    if not re.fullmatch(bd.INDEX_NAME_RE, args.index):
        ap.error(f"--index: not an index name: {args.index}")
    if args.rancher_projects:
        names = bd.load_rancher_projects(args.rancher_projects)
    else:
        try:
            names = bd.rancher_project_names(kubectl_projects(args.context))
        except subprocess.CalledProcessError as e:
            sys.exit(f"could not read Rancher Projects: {(e.stderr or '').strip().splitlines()[-1:] or e}")
    docs = documents(names)
    if not docs:
        sys.exit("no Rancher Projects found; is this the Rancher local cluster? Nothing written.")
    if args.dry_run:
        for doc in docs:
            print(json.dumps(doc))
        print(f"{len(docs)} Projects for index {args.index} (dry run, nothing written)", file=sys.stderr)
        return
    try:
        written, removed = publish(Elastic(args.es_url), args.index, docs)
    except urllib.error.HTTPError as e:
        sys.exit(f"Elasticsearch: HTTP {e.code} {e.read().decode()[:800]}")
    except urllib.error.URLError as e:
        sys.exit(f"Elasticsearch: {e.reason}")
    print(f"{args.index}: {written} Projects written, {removed} removed")


if __name__ == "__main__":
    main()
