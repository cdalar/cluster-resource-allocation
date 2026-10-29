#!/usr/bin/env python3
"""Web dashboard for discover.py: collects on an interval and serves the latest report.

Endpoints (all relative, so the app also works behind the Rancher/API server service proxy):
  GET  /                    dashboard
  GET  /api/report          latest report (JSON) + collector status; gzip-compressed when the client accepts it,
                            and 304 Not Modified when If-None-Match carries the ETag of the last answer
  POST /api/refresh         trigger a collection now
  GET  /download/<name>.csv clusters / projects / namespaces
  GET  /healthz             liveness/readiness
  GET  /api/clusters        Rancher local cluster with --downstream-report: the clusters Rancher manages (id, name,
                            ready). The page then loads each downstream cluster's own dashboard report through
                            Rancher's proxy (/k8s/clusters/<id>/...) with the browser's Rancher session -- this
                            server never talks to downstream clusters and holds no token.

Live view (live.py; on unless --no-live or --no-collect): nodes, namespaces and pods of this cluster followed with
Kubernetes watches, usage from metrics-server every --live-metrics-interval seconds:
  GET  /live                     live page: Node -> Project -> Namespace -> Workload -> Pod -> Container (or from the
                                 project side), requests / limits / usage against the allocatable
  GET  /api/live/snapshot        the whole state (gzip when accepted): {"seq", nodes, namespaces, pods, usage, status}
  GET  /api/live/changes?since=N what changed after seq N ({"reset": true} = reload the snapshot)
  GET  /api/live/stream?since=N  the same as Server-Sent Events (event: changes, id: seq), a few seconds apart,
                                 an "event: ping" every 15 s while nothing changes
  GET  /text                     plain-text reports from the same state (textreport.py)
  GET  /api/live/text?report=nodes|namespaces|workloads|pods&ns=<namespace>&sort=mem|cpu&top=N
                                 one report as text/plain (workloads needs ns; nodes and pods take it optionally)

With --planner (Rancher local cluster only), also the allocation planner (planner.py):
  GET  /planner                  planner page
  GET  /api/planner              plan + Rancher inventory + checks
  PUT  /api/planner              save the plan ({"version": n, "plan": {...}}; needs header X-Planner: 1);
                                 a restore adds "restored_from": <earlier version>
  GET  /api/planner/export.yaml  the plan as allocation files (docs/04)
  GET  /api/planner/history      the stored versions, newest first (time, totals, what changed)
  GET  /api/planner/history/<n>  one stored version ({"plan": {...}})
With --capacity-planner, the same without money (CPU / memory envelopes per project instead of budgets):
  GET  /capacity, GET|PUT /api/capacity, GET /api/capacity/export.yaml, GET /api/capacity/history[/<n>]
The planners only write their own plan files in --data-dir, never to a cluster.
"""

import copy
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discover
import live
import planner
import textreport

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CSV_NAMES = ("clusters", "projects", "namespaces")
PAGE_CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:"
LIVE_DEBOUNCE = 2.0    # seconds a change waits on the SSE stream for the rest of its burst
LIVE_PING = 15         # seconds between SSE comments while nothing changes (keeps proxies from closing the stream)


def versions():
    """Helm chart version and image tag of this deployment (set by the chart; None when run without it)."""
    return {"chart": os.environ.get("CHART_VERSION") or None, "image": os.environ.get("IMAGE_TAG") or None}


def parse_duration(s):
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise ValueError(f"invalid duration: {s} (use e.g. 90s, 15m, 1h)")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Collector:
    """Runs discover.collect() in a background thread and keeps the latest report."""

    def __init__(self, args):
        self.args = args
        self.interval = parse_duration(args.interval)
        self.lock = threading.Lock()
        self.report = None
        self.report_json = b"null"   # the report serialized once per collection, not once per request
        self._encoded = (None, None, None)  # (response body, ETag, gzip of it) of the last /api/report answer
        self.state = "idle"
        self.last_error = None
        self.next_run = None
        self.wake = threading.Event()
        self.live = None   # live.LiveState when the live view is on: gets the Rancher Project names of each report
        self._load_saved()

    def _path(self, name):
        return os.path.join(self.args.data_dir, name)

    def _load_saved(self):
        try:
            with open(self._path("report.json")) as f:
                self.set_report(json.load(f))
            discover.log(f"loaded previous report from {self._path('report.json')}")
        except (OSError, json.JSONDecodeError):
            pass

    def set_report(self, report):
        data = json.dumps(report).encode()
        with self.lock:
            self.report, self.report_json, self.last_error = report, data, None
        if self.live is not None:
            self.live.set_names_from_report(report)

    def encoded(self, body):
        """(ETag, gzip body) for a /api/report response body; computed once per distinct body, so the polling
        pages don't cost a hash and a compression each."""
        with self.lock:
            cached_body, etag, gz = self._encoded
            if cached_body == body:
                return etag, gz
        etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        gz = gzip.compress(body, compresslevel=6, mtime=0)
        with self.lock:
            self._encoded = (body, etag, gz)
        return etag, gz

    def status(self):
        downstream = None
        if getattr(self.args, "downstream_report", None):
            ns, svc, port = parse_downstream_report(self.args.downstream_report)
            downstream = {"namespace": ns, "service": svc, "port": port}
        with self.lock:
            return {
                "downstream": downstream,
                "state": self.state,
                "last_error": self.last_error,
                "next_run": self.next_run,
                "interval": self.args.interval,
                "collect_enabled": not self.args.no_collect,
                "planner": bool(getattr(self.args, "planner", False)),
                "capacity_planner": bool(getattr(self.args, "capacity_planner", False)),
                "live": self.live is not None,
                "versions": versions(),
            }

    def trigger(self):
        self.wake.set()

    def run_once(self):
        with self.lock:
            self.state = "collecting"
        try:
            report = discover.collect(self.args)
            if not report["clusters"]:
                raise RuntimeError("no cluster data collected: " + "; ".join(report["log"][-3:]))
            os.makedirs(self.args.data_dir, exist_ok=True)
            discover.write_report_csvs(report, self.args.data_dir)
            tmp = self._path("report.json.tmp")
            with open(tmp, "w") as f:
                json.dump(report, f)
            os.replace(tmp, self._path("report.json"))
            self.set_report(report)
            discover.log(f"collection done in {report['duration_s']}s")
        except Exception as e:  # keep serving the previous report whatever went wrong
            discover.log(f"collection failed: {e}")
            with self.lock:
                self.last_error = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {e}"
        finally:
            with self.lock:
                self.state = "idle"

    def loop(self):
        while True:
            self.run_once()
            nxt = datetime.now(timezone.utc) + timedelta(seconds=self.interval)
            with self.lock:
                self.next_run = nxt.isoformat(timespec="seconds")
            self.wake.wait(self.interval)
            self.wake.clear()


def parse_downstream_report(s):
    """NAMESPACE/SERVICE:PORT of this chart's Service on the downstream clusters -> (namespace, service, port)."""
    m = re.fullmatch(r"([a-z0-9.-]+)/([a-z0-9.-]+):(\d{1,5})", s or "")
    if not m:
        raise ValueError(f"--downstream-report must be NAMESPACE/SERVICE:PORT, got {s!r}")
    return m.group(1), m.group(2), int(m.group(3))


class RancherClusters:
    """The clusters Rancher manages, read from the local cluster and cached for a minute (for /api/clusters)."""

    TTL = 60

    def __init__(self, args, loader=None):
        if loader is None:
            if args.rancher_local_self:
                ctx, kubeconfig = (args.context[0] if args.context else None), None
            else:
                ctx = args.rancher_local_context
                kubeconfig = os.path.expanduser(args.rancher_local_kubeconfig) if args.rancher_local_kubeconfig else None
            loader = lambda: discover.rancher_cluster_list(ctx, kubeconfig)  # noqa: E731
        self._load, self._clusters, self._at, self.lock = loader, None, 0.0, threading.Lock()

    def list(self):
        with self.lock:
            if self._clusters is None or time.monotonic() - self._at > self.TTL:
                self._clusters, self._at = self._load(), time.monotonic()
            return self._clusters


class PlannerBackend:
    """The planner's stored plan (ConfigMap, or a file in --data-dir) plus a short-lived cache of the Rancher inventory it plans against."""

    INVENTORY_TTL = 30  # seconds; the page reloads it on every open and save

    def __init__(self, args, collector, inventory_loader=None, mode="budget", store=None):
        self.collector, self.mode = collector, mode
        if store is None:
            if getattr(args, "plan_configmaps", None):
                namespace, prefix = args.plan_configmaps.split("/", 1)
                store = planner.ConfigMapPlanStore(namespace, f"{prefix}-{planner.STATE_FILES[mode][2]}", mode,
                                                   legacy_dir=args.data_dir)
            else:
                store = planner.PlanStore(args.data_dir, mode)
        self.store = store
        self.save_lock = threading.Lock()
        if inventory_loader is None:
            if args.rancher_local_self:
                ctx, kubeconfig = (args.context[0] if args.context else None), None
            else:
                ctx = args.rancher_local_context
                kubeconfig = os.path.expanduser(args.rancher_local_kubeconfig) if args.rancher_local_kubeconfig else None
            inventory_loader = lambda: planner.load_inventory(ctx, kubeconfig)  # noqa: E731
        self._load_inventory = inventory_loader
        self._inventory, self._inventory_at = None, 0.0
        self._system_project_re = re.compile(
            getattr(args, "system_project_regex", None) or discover.DEFAULT_SYSTEM_PROJECT_REGEX, re.IGNORECASE)

    def inventory(self):
        now = time.monotonic()
        if self._inventory is None or now - self._inventory_at > self.INVENTORY_TTL:
            self._inventory, self._inventory_at = self._load_inventory(), now
        with self.collector.lock:
            report = self.collector.report
        return planner.add_report_requests(copy.deepcopy(self._inventory), report, self._system_project_re)

    def view(self, plan=None):
        plan = plan or self.store.load()
        inventory = self.inventory()
        out = {"mode": self.mode, "plan": plan, "inventory": inventory, "versions": versions(),
               "evaluation": planner.evaluate(plan, inventory, self.mode)}
        if self.mode == "budget":
            out["reference"] = planner.RATE_REFERENCE
        return out

    def export(self):
        return planner.export_yaml(self.store.load(), self.inventory(), self.mode)

    def history(self):
        return {"mode": self.mode, "current": self.store.load().get("version", 0), "versions": self.store.history()}

    def version(self, n):
        return {"mode": self.mode, "plan": self.store.version(n)}

    def save(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("version"), int):
            raise planner.PlanError('expected {"version": <int>, "plan": {...}}')
        with self.save_lock:
            return self.view(self.store.save(body.get("plan"), body["version"], body.get("restored_from")))


def make_handler(collector, planner_backend=None, capacity_backend=None, rancher_clusters=None, live_state=None):
    # URL name -> planner backend; both are served by static/planner.html, which takes its mode from the API
    planners = {name: b for name, b in (("planner", planner_backend), ("capacity", capacity_backend)) if b}

    def since_of(query):
        """?since=N (or the SSE Last-Event-ID header value passed in); -1 = nothing yet."""
        try:
            return int(query)
        except (TypeError, ValueError):
            return -1

    def planner_route(path):
        """(backend, what) for /<name>, /api/<name>, /api/<name>/export.yaml, /api/<name>/history and
        /api/<name>/history/<n> (what = the version number), else (None, None)."""
        for name, backend in planners.items():
            if path == f"/{name}":
                return backend, "page"
            if path == f"/api/{name}":
                return backend, "api"
            if path == f"/api/{name}/export.yaml":
                return backend, "export"
            if path == f"/api/{name}/history":
                return backend, "history"
            m = re.fullmatch(rf"/api/{name}/history/(\d{{1,9}})", path)
            if m:
                return backend, int(m.group(1))
        return None, None

    class Handler(BaseHTTPRequestHandler):
        server_version = "cluster-resource-report"

        def log_message(self, fmt, *a):
            if not self.path.startswith("/healthz"):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

        def _send(self, code, body, ctype, extra=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            backend, what = planner_route(path)
            if path in ("/", "/index.html"):
                self._page("index.html")
            elif what == "page":
                self._page("planner.html")
            elif what == "api":
                try:
                    self._json(200, backend.view())
                except planner.PlanStoreError as e:
                    self._json(502, {"error": discover.first_line(e)})
                except discover.KubectlError as e:
                    self._json(502, {"error": f"could not read the Rancher inventory: {discover.first_line(e)}"})
                except Exception as e:  # a bug must show up on the page, not as a dropped connection
                    discover.log(f"planner: {type(e).__name__}: {e}")
                    self._json(500, {"error": f"planner error: {type(e).__name__}: {e}"})
            elif what == "history" or isinstance(what, int):
                try:
                    self._json(200, backend.history() if what == "history" else backend.version(what))
                except planner.PlanNotFound as e:
                    self._json(404, {"error": str(e)})
                except planner.PlanStoreError as e:
                    self._json(502, {"error": discover.first_line(e)})
            elif what == "export":
                try:
                    data = backend.export()
                except planner.PlanStoreError as e:
                    return self._send(502, discover.first_line(e), "text/plain")
                except discover.KubectlError as e:
                    return self._send(502, f"could not read the Rancher inventory: {discover.first_line(e)}",
                                      "text/plain")
                self._send(200, data, "application/yaml; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="allocations.yaml"'})
            elif path == "/api/report":
                self._report()
            elif path == "/live":
                self._page("live.html")
            elif path == "/text":
                self._page("text.html")
            elif path.startswith("/api/live/"):
                if live_state is None:
                    return self._json(404, {"error": "live view is off (--no-live or --no-collect)"})
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                since = since_of((query.get("since") or [self.headers.get("Last-Event-ID")])[0])
                if path == "/api/live/snapshot":
                    body, gz = live_state.snapshot_json()
                    headers = {"Vary": "Accept-Encoding"}
                    if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                        body, headers["Content-Encoding"] = gz, "gzip"
                    self._send(200, body, "application/json", headers)
                elif path == "/api/live/changes":
                    self._json(200, live_state.changes(since))
                elif path == "/api/live/stream":
                    self._live_stream(since)
                elif path == "/api/live/text":
                    first = lambda k, d=None: (query.get(k) or [d])[0]  # noqa: E731
                    try:
                        top = int(first("top", textreport.DEFAULT_TOP))
                    except ValueError:
                        return self._send(400, "top must be a number\n", "text/plain; charset=utf-8")
                    try:
                        text = textreport.render(live_state.snapshot(), first("report", "nodes"), first("ns"),
                                                 first("sort", "mem"), top)
                    except textreport.ReportError as e:
                        return self._send(400, f"{e}\n", "text/plain; charset=utf-8")
                    self._send(200, text, "text/plain; charset=utf-8")
                else:
                    self._send(404, "not found", "text/plain")
            elif path == "/api/clusters":
                if rancher_clusters is None:
                    return self._json(404, {"error": "not on the Rancher local cluster (no --downstream-report)"})
                try:
                    self._json(200, {"clusters": rancher_clusters.list()})
                except discover.KubectlError as e:
                    self._json(502, {"error": f"could not list the Rancher clusters: {discover.first_line(e)}"})
            elif path == "/healthz":
                self._send(200, "ok", "text/plain")
            elif path.startswith("/download/") and path.endswith(".csv"):
                name = path[len("/download/"):-4]
                if name not in CSV_NAMES:
                    return self._send(404, "not found", "text/plain")
                try:
                    with open(collector._path(f"{name}.csv"), "rb") as f:
                        data = f.read()
                except OSError:
                    return self._send(404, "no report yet", "text/plain")
                self._send(200, data, "text/csv; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{name}.csv"'})
            else:
                self._send(404, "not found", "text/plain")

        def _report(self):
            with collector.lock:
                report_json = collector.report_json
            body = b'{"status": ' + json.dumps(collector.status()).encode() + b', "report": ' + report_json + b"}"
            etag, gz = collector.encoded(body)
            headers = {"ETag": etag, "Vary": "Accept-Encoding"}
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                for k, v in {"Cache-Control": "no-store", **headers}.items():
                    self.send_header(k, v)
                return self.end_headers()
            if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body, headers["Content-Encoding"] = gz, "gzip"
            self._send(200, body, "application/json", headers)

        def _page(self, name):
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})

        def _live_stream(self, since):
            """Server-Sent Events: the changes after `since`, each burst a few seconds after its first change
            (event: changes, id: the new seq), and a comment every LIVE_PING seconds while nothing changes. A client
            that fell behind the change log gets {"reset": true} and reloads the snapshot."""
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                         ("X-Accel-Buffering", "no"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
                while True:
                    seq = live_state.wait(since, LIVE_PING)
                    if seq <= since:
                        # an event, not a comment: the page sees it (EventSource hides comments) and knows the
                        # stream is alive; without one for 45 s it falls back to polling /api/live/changes
                        self.wfile.write(f"event: ping\ndata: {json.dumps({'seq': seq})}\n\n".encode())
                        self.wfile.flush()
                        continue
                    time.sleep(LIVE_DEBOUNCE)
                    delta = live_state.changes(since)
                    self.wfile.write(f"id: {delta['seq']}\nevent: changes\ndata: {json.dumps(delta)}\n\n".encode())
                    self.wfile.flush()
                    since = delta["seq"]
            except (BrokenPipeError, ConnectionResetError, OSError):
                return   # the page went away

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_PUT(self):
            backend, what = planner_route(self.path.split("?", 1)[0])
            if what != "api":
                return self._send(404, "not found", "text/plain")
            # Opened through Rancher's proxy, the page rides on the user's Rancher session cookie. A custom
            # header plus a JSON body can't be sent cross-origin without a CORS preflight, which this server
            # never answers -- so another site can't make a logged-in browser save a plan.
            if self.headers.get("X-Planner") != "1" or \
                    not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(400, {"error": "expected Content-Type: application/json and X-Planner: 1"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= planner.MAX_STATE_BYTES:
                return self._json(413, {"error": "missing or too large body"})
            try:
                body = json.loads(self.rfile.read(length))
                self._json(200, backend.save(body))
            except json.JSONDecodeError as e:
                self._json(400, {"error": f"invalid JSON: {e}"})
            except planner.PlanConflict as e:
                self._json(409, {"error": str(e), "version": e.version})
            except planner.PlanError as e:
                self._json(400, {"error": str(e)})
            except planner.PlanStoreError as e:
                self._json(502, {"error": f"not saved: {discover.first_line(e)}"})
            except discover.KubectlError as e:
                self._json(502, {"error": f"saved, but could not read the Rancher inventory: {discover.first_line(e)}"})

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/refresh":
                return self._send(404, "not found", "text/plain")
            if collector.args.no_collect:
                return self._json(409, {"error": "collection disabled"})
            collector.trigger()
            self._json(202, {"status": "triggered"})

    return Handler


def main():
    ap = discover.build_parser()
    ap.description = __doc__
    ap.add_argument("--listen", default="0.0.0.0:8080", help="host:port to listen on (default: 0.0.0.0:8080)")
    ap.add_argument("--interval", default="30m", help="time between collections (default: 30m)")
    ap.add_argument("--data-dir", default="/data", help="where report.json and CSVs are kept (default: /data)")
    ap.add_argument("--no-collect", action="store_true",
                    help="only serve the report already in --data-dir (demo / offline review)")
    ap.add_argument("--planner", action="store_true",
                    help="also serve the allocation planner (needs Rancher local cluster access: "
                         "--rancher-local-self, --rancher-local-context or --rancher-local-kubeconfig)")
    ap.add_argument("--capacity-planner", action="store_true",
                    help="also serve the capacity planner: the allocation planner without money, with a CPU and "
                         "memory envelope per project (same Rancher access as --planner)")
    ap.add_argument("--downstream-report", metavar="NAMESPACE/SERVICE:PORT",
                    help="Rancher local cluster only: where this dashboard's Service is on the downstream clusters "
                         "(e.g. resource-report/resource-report-cluster-resource-report:80). The page then lists all "
                         "Rancher clusters and shows each one's report through Rancher's proxy (fleet view)")
    ap.add_argument("--no-live", action="store_true",
                    help="no live view (/live): no watches on nodes, namespaces and pods and no metrics-server polling")
    ap.add_argument("--live-metrics-interval", type=int, default=live.DEFAULT_METRICS_INTERVAL, metavar="SECONDS",
                    help=f"live view: seconds between metrics-server usage snapshots (default: {live.DEFAULT_METRICS_INTERVAL})")
    ap.add_argument("--plan-configmaps", metavar="NAMESPACE/PREFIX",
                    help="keep the planners' plans in the ConfigMaps PREFIX-planner and PREFIX-capacity (in this "
                         "cluster; needs get/update on them) instead of files in --data-dir")
    args = ap.parse_args()
    parse_duration(args.interval)
    for opt in ("window", "step"):
        try:
            discover.duration_seconds(getattr(args, opt))
        except ValueError as e:
            ap.error(f"--{opt}: {e}")
    if args.plan_configmaps and "/" not in args.plan_configmaps:
        ap.error("--plan-configmaps must be NAMESPACE/PREFIX")
    if args.downstream_report:
        try:
            parse_downstream_report(args.downstream_report)
        except ValueError as e:
            ap.error(str(e))
        if not (args.rancher_local_self or args.rancher_local_context or args.rancher_local_kubeconfig):
            ap.error("--downstream-report needs the Rancher local cluster: --rancher-local-self, "
                     "--rancher-local-context or --rancher-local-kubeconfig")
    if (args.planner or args.capacity_planner) and not (
            args.rancher_local_self or args.rancher_local_context or args.rancher_local_kubeconfig):
        ap.error("--planner / --capacity-planner need the Rancher local cluster: --rancher-local-self, "
                 "--rancher-local-context or --rancher-local-kubeconfig")

    if args.live_metrics_interval < 5:
        ap.error("--live-metrics-interval must be at least 5 seconds")
    if len(args.context) > 1 and not (args.no_live or args.no_collect):
        ap.error("the live view follows one cluster: with several --context use --no-live")

    collector = Collector(args)
    planner_backend = PlannerBackend(args, collector) if args.planner else None
    capacity_backend = PlannerBackend(args, collector, mode="capacity") if args.capacity_planner else None
    rancher_clusters = RancherClusters(args) if args.downstream_report else None
    live_state = None
    if not args.no_collect and not args.no_live:
        live_state = live.LiveState(args, metrics_interval=args.live_metrics_interval)
        collector.live = live_state
        if collector.report:
            live_state.set_names_from_report(collector.report)
        live.start(live_state, context=args.context[0] if args.context else None)
    if not args.no_collect:
        threading.Thread(target=collector.loop, daemon=True).start()

    host, port = args.listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(collector, planner_backend, capacity_backend, rancher_clusters,
                                                                live_state))
    discover.log(f"serving on http://{args.listen} (interval {args.interval}, data {args.data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
