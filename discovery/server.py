#!/usr/bin/env python3
"""Web dashboard for discover.py: collects on an interval and serves the latest report.

Endpoints (all relative, so the app also works behind the Rancher/API server service proxy):
  GET  /                    dashboard
  GET  /api/report          latest report (JSON) + collector status
  POST /api/refresh         trigger a collection now
  GET  /download/<name>.csv clusters / projects / namespaces
  GET  /healthz             liveness/readiness
"""

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discover

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CSV_NAMES = ("clusters", "projects", "namespaces")


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


def make_handler(collector):
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
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8",
                               {"Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; "
                                                           "script-src 'unsafe-inline'; img-src 'self' data:"})
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
    args = ap.parse_args()
    parse_duration(args.interval)

    collector = Collector(args)
    if not args.no_collect:
        threading.Thread(target=collector.loop, daemon=True).start()

    host, port = args.listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host, int(port)), make_handler(collector))
    discover.log(f"serving on http://{args.listen} (interval {args.interval}, data {args.data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
