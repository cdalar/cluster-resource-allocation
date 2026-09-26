#!/usr/bin/env python3
"""Web dashboard for discover.py: collects on an interval and serves the latest report.

Endpoints (all relative, so the app also works behind the Rancher/API server service proxy):
  GET  /                    dashboard
  GET  /api/report          latest report (JSON) + collector status
  POST /api/refresh         trigger a collection now
  GET  /download/<name>.csv clusters / projects / namespaces
  GET  /healthz             liveness/readiness

With --planner (Rancher local cluster only), also the allocation planner (planner.py):
  GET  /planner                  planner page
  GET  /api/planner              plan + Rancher inventory + checks
  PUT  /api/planner              save the plan ({"version": n, "plan": {...}}; needs header X-Planner: 1)
  GET  /api/planner/export.yaml  the plan as allocation files (docs/04)
The planner only writes its own plan file in --data-dir, never to a cluster.
"""

import copy
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discover
import planner

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CSV_NAMES = ("clusters", "projects", "namespaces")
PAGE_CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:"


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
        self.state = "idle"
        self.last_error = None
        self.next_run = None
        self.wake = threading.Event()
        self._load_saved()

    def _path(self, name):
        return os.path.join(self.args.data_dir, name)

    def _load_saved(self):
        try:
            with open(self._path("report.json")) as f:
                self.report = json.load(f)
            discover.log(f"loaded previous report from {self._path('report.json')}")
        except (OSError, json.JSONDecodeError):
            pass

    def status(self):
        with self.lock:
            return {
                "state": self.state,
                "last_error": self.last_error,
                "next_run": self.next_run,
                "interval": self.args.interval,
                "collect_enabled": not self.args.no_collect,
                "planner": bool(getattr(self.args, "planner", False)),
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
            with self.lock:
                self.report, self.last_error = report, None
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


class PlannerBackend:
    """The planner's plan file plus a short-lived cache of the Rancher inventory it plans against."""

    INVENTORY_TTL = 30  # seconds; the page reloads it on every open and save

    def __init__(self, args, collector, inventory_loader=None):
        self.collector = collector
        self.store = planner.PlanStore(args.data_dir)
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

    def inventory(self):
        now = time.monotonic()
        if self._inventory is None or now - self._inventory_at > self.INVENTORY_TTL:
            self._inventory, self._inventory_at = self._load_inventory(), now
        with self.collector.lock:
            report = self.collector.report
        return planner.add_report_requests(copy.deepcopy(self._inventory), report)

    def view(self, plan=None):
        plan = plan or self.store.load()
        inventory = self.inventory()
        return {"plan": plan, "inventory": inventory, "evaluation": planner.evaluate(plan, inventory)}

    def save(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("version"), int):
            raise planner.PlanError('expected {"version": <int>, "plan": {...}}')
        with self.save_lock:
            return self.view(self.store.save(body.get("plan"), body["version"]))


def make_handler(collector, planner_backend=None):
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
            if path in ("/", "/index.html"):
                self._page("index.html")
            elif path == "/planner" and planner_backend:
                self._page("planner.html")
            elif path == "/api/planner" and planner_backend:
                try:
                    self._json(200, planner_backend.view())
                except discover.KubectlError as e:
                    self._json(502, {"error": f"could not read the Rancher inventory: {discover.first_line(e)}"})
                except Exception as e:  # a bug must show up on the page, not as a dropped connection
                    discover.log(f"planner: {type(e).__name__}: {e}")
                    self._json(500, {"error": f"planner error: {type(e).__name__}: {e}"})
            elif path == "/api/planner/export.yaml" and planner_backend:
                try:
                    data = planner.export_yaml(planner_backend.store.load(), planner_backend.inventory())
                except discover.KubectlError as e:
                    return self._send(502, f"could not read the Rancher inventory: {discover.first_line(e)}",
                                      "text/plain")
                self._send(200, data, "application/yaml; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="allocations.yaml"'})
            elif path == "/api/report":
                with collector.lock:
                    report = collector.report
                self._json(200, {"status": collector.status(), "report": report})
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

        def _page(self, name):
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})

        def do_PUT(self):
            if self.path.split("?", 1)[0] != "/api/planner" or not planner_backend:
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
                self._json(200, planner_backend.save(body))
            except json.JSONDecodeError as e:
                self._json(400, {"error": f"invalid JSON: {e}"})
            except planner.PlanConflict as e:
                self._json(409, {"error": str(e), "version": e.version})
            except planner.PlanError as e:
                self._json(400, {"error": str(e)})
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
    args = ap.parse_args()
    parse_duration(args.interval)
    if args.planner and not (args.rancher_local_self or args.rancher_local_context or args.rancher_local_kubeconfig):
        ap.error("--planner needs the Rancher local cluster: --rancher-local-self, --rancher-local-context "
                 "or --rancher-local-kubeconfig")

    collector = Collector(args)
    planner_backend = PlannerBackend(args, collector) if args.planner else None
    if not args.no_collect:
        threading.Thread(target=collector.loop, daemon=True).start()

    host, port = args.listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(collector, planner_backend))
    discover.log(f"serving on http://{args.listen} (interval {args.interval}, data {args.data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
