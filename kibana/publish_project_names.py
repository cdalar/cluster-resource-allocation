#!/usr/bin/env python3
"""Publish Rancher Projects (ID, name, cluster) to a lookup index in Elasticsearch, for Kibana dashboards.

The metrics carry only the Project ID (resource.attributes.rancher.project.id). This script writes one document per
Project to a small index with `index.mode: lookup`, so any ES|QL query can join the names (LOOKUP JOIN, Elasticsearch
9.1+). The report generated with `build_dashboard.py --project-lookup-index <index>` does; other dashboards can join
straight on the metrics field, which the documents carry under the same name:

  FROM metrics-*.otel-* | LOOKUP JOIN rancher-projects ON resource.attributes.rancher.project.id
  | STATS ... BY project_name

Fields: project (ID), project_name, cluster_id (Rancher cluster ID, e.g. c-m-abcd1234 or local) and
resource.attributes.rancher.project.id (the ID again, the join key for the metrics).
Run it again after adding or renaming Projects, by hand or from a scheduler: it replaces the documents and removes
Projects that no longer exist.

  ./publish_project_names.py https://elastic.example:9200 --rancher-projects projects.json
      # projects.json: kubectl get projects.management.cattle.io -A -o json   (on the Rancher local cluster)
  ./publish_project_names.py https://elastic.example:9200 --context rancher-local
      # or read the Projects with kubectl itself
  ./publish_project_names.py https://kibana.example --kibana --rancher-projects projects.json
      # only Kibana is reachable: through Kibana's Dev Tools proxy (the user needs Dev Tools access)
  ./publish_project_names.py https://elastic.example:9200 --rancher-projects projects.json --dry-run

Login: $ES_API_KEY, or $ES_USER / $ES_PASSWORD (also for --kibana); $ES_INSECURE=1 for a self-signed certificate.
The key needs create_index, index, delete, view_index_metadata and manage (mapping updates) on this one index. Reads
Rancher, writes only this index.

One file, standard library and kubectl only: it runs on its own, without the rest of the repository.
"""

import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LOOKUP_INDEX = "rancher-projects"
INDEX_NAME_RE = r"[a-z0-9][a-z0-9._-]*"
# build_dashboard.py joins on its "project" column and reads "project_name" (LOOKUP_KEY / LOOKUP_NAME there)
KEY, NAME, CLUSTER, METRICS_KEY = "project", "project_name", "cluster_id", "resource.attributes.rancher.project.id"
FIELDS = (KEY, NAME, CLUSTER, METRICS_KEY)
INDEX_BODY = {
    "settings": {"index": {"mode": "lookup"}},
    "mappings": {"properties": {f: {"type": "keyword"} for f in FIELDS}},
}


def kubectl_projects(context):
    cmd = ["kubectl"] + (["--context", context] if context else []) + [
        "get", "projects.management.cattle.io", "-A", "-o", "json"]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def documents(projects):
    """One document per Project from the parsed kubectl output; the Project ID is the document ID, so a Project
    never matches twice in a join. A Project without a display name keeps its ID as name."""
    docs = {}
    for it in projects.get("items", []):
        meta = it["metadata"]
        pid = meta["name"]
        name = (it.get("spec") or {}).get("displayName") or pid
        if pid in docs and docs[pid][NAME] != name:
            print(f"warning: Project ID {pid} has two names ({docs[pid][NAME]}, {name}); using {name}",
                  file=sys.stderr)
        docs[pid] = {KEY: pid, NAME: name, CLUSTER: meta.get("namespace", ""), METRICS_KEY: pid}
    return [docs[k] for k in sorted(docs)]


def bulk_body(index, docs):
    lines = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": doc[KEY]}}))
        lines.append(json.dumps(doc))
    return "\n".join(lines) + "\n"


def stale_query(docs):
    """Everything in the index that is not one of these Projects."""
    return {"query": {"bool": {"must_not": {"ids": {"values": [d[KEY] for d in docs]}}}}}


def auth(content_type):
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


class Elastic:
    """Elasticsearch REST, directly or (kibana=True) through Kibana's Dev Tools proxy, which passes the status on."""

    def __init__(self, url, kibana=False):
        self.url, self.kibana = url.rstrip("/"), kibana

    def target(self, method, path):
        if not self.kibana:
            return method, self.url + path
        query = urllib.parse.urlencode({"path": path, "method": method})
        return "POST", f"{self.url}/api/console/proxy?{query}"

    def request(self, method, path, body=None, content_type="application/json"):
        headers, ctx = auth(content_type)
        if self.kibana:
            headers["kbn-xsrf"] = "true"
        method, url = self.target(method, path)
        data = body if isinstance(body, (bytes, type(None))) else (
            body if isinstance(body, str) else json.dumps(body)).encode()
        req = urllib.request.Request(url, data, headers, method=method)
        with urllib.request.urlopen(req, context=ctx) as r:
            return json.loads(r.read().decode() or "{}")


def publish(es, index, docs):
    """Create the lookup index if it is missing (or add new fields to it), write the Projects, remove the others.
    Returns (written, removed)."""
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
        es.request("PUT", f"/{index}/_mapping", INDEX_BODY["mappings"])  # an index from an older version
    result = es.request("POST", f"/{index}/_bulk?refresh=true", bulk_body(index, docs), "application/x-ndjson")
    if result.get("errors"):
        failed = [it["index"] for it in result["items"] if it["index"].get("error")]
        sys.exit(f"{len(failed)} of {len(docs)} Projects not written; first error: {json.dumps(failed[0]['error'])}")
    removed = es.request("POST", f"/{index}/_delete_by_query?refresh=true", stale_query(docs)).get("deleted", 0)
    return len(docs), removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", metavar="URL", help="Elasticsearch, e.g. https://elastic.example:9200 (Kibana with --kibana)")
    ap.add_argument("--kibana", action="store_true",
                    help="URL is Kibana: send the requests through its Dev Tools proxy (/api/console/proxy)")
    ap.add_argument("--index", default=DEFAULT_LOOKUP_INDEX, help=f"lookup index (default {DEFAULT_LOOKUP_INDEX})")
    ap.add_argument("--rancher-projects", metavar="FILE",
                    help="output of `kubectl get projects.management.cattle.io -A -o json` on the Rancher local "
                         "cluster; without it the script runs that command itself")
    ap.add_argument("--context", help="kubeconfig context of the Rancher local cluster (default: the current one)")
    ap.add_argument("--dry-run", action="store_true", help="print the documents, write nothing")
    args = ap.parse_args()
    if not re.fullmatch(INDEX_NAME_RE, args.index):
        ap.error(f"--index: not an index name: {args.index}")
    if args.rancher_projects:
        with open(args.rancher_projects) as f:
            projects = json.load(f)
    else:
        try:
            projects = kubectl_projects(args.context)
        except subprocess.CalledProcessError as e:
            sys.exit(f"could not read Rancher Projects: {(e.stderr or '').strip().splitlines()[-1:] or e}")
    docs = documents(projects)
    if not docs:
        sys.exit("no Rancher Projects found; is this the Rancher local cluster? Nothing written.")
    if args.dry_run:
        for doc in docs:
            print(json.dumps(doc))
        print(f"{len(docs)} Projects for index {args.index} (dry run, nothing written)", file=sys.stderr)
        return
    where = "Kibana" if args.kibana else "Elasticsearch"
    try:
        written, removed = publish(Elastic(args.url, args.kibana), args.index, docs)
    except urllib.error.HTTPError as e:
        sys.exit(f"{where}: HTTP {e.code} {e.read().decode()[:800]}")
    except urllib.error.URLError as e:
        sys.exit(f"{where}: {e.reason}")
    print(f"{args.index}: {written} Projects written, {removed} removed")


if __name__ == "__main__":
    main()
