import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import discover
import server


class Server(unittest.TestCase):
    """Serves a saved report with collection disabled (no cluster access)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        report = {"collected_at": "2026-09-25T10:00:00+00:00", "duration_s": 1.0,
                  "settings": {"window": "7d", "step": "5m", "prometheus": "auto"},
                  "clusters": [{"cluster": "c1"}], "projects": [], "namespaces": [], "log": []}
        with open(os.path.join(cls.tmp.name, "report.json"), "w") as f:
            json.dump(report, f)
        discover.write_csv(os.path.join(cls.tmp.name, "projects.csv"), ["cluster"], [{"cluster": "c1"}])
        args = server.discover.build_parser().parse_args([])
        args.data_dir, args.interval, args.no_collect = cls.tmp.name, "30m", True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.Collector(args)))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_dashboard(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Cluster resource report", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_report(self):
        code, _, body = self.get("/api/report")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["report"]["clusters"][0]["cluster"], "c1")
        self.assertFalse(data["status"]["collect_enabled"])

    def test_csv_download(self):
        code, headers, body = self.get("/download/projects.csv")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.get("/download/namespaces.csv")[0], 404)   # not written
        self.assertEqual(self.get("/download/..%2Freport.csv")[0], 404)

    def test_refresh_disabled(self):
        self.assertEqual(self.get("/api/refresh", "POST")[0], 409)

    def test_healthz_and_404(self):
        self.assertEqual(self.get("/healthz")[0], 200)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_planner_off_by_default(self):
        self.assertEqual(self.get("/planner")[0], 404)
        self.assertEqual(self.get("/api/planner")[0], 404)
        self.assertFalse(json.loads(self.get("/api/report")[2])["status"]["planner"])

    def test_parse_duration(self):
        self.assertEqual(server.parse_duration("15m"), 900)
        self.assertEqual(server.parse_duration("1h"), 3600)
        with self.assertRaises(ValueError):
            server.parse_duration("15")


class PlannerServer(unittest.TestCase):
    """The planner endpoints with a fake Rancher inventory (no cluster access)."""

    @classmethod
    def setUpClass(cls):
        import test_planner
        cls.tmp = tempfile.TemporaryDirectory()
        args = server.discover.build_parser().parse_args(["--rancher-local-self"])
        args.data_dir, args.interval, args.no_collect, args.planner = cls.tmp.name, "30m", True, True
        collector = server.Collector(args)
        backend = server.PlannerBackend(args, collector, inventory_loader=test_planner.inventory)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(collector, backend))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def req(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def put(self, body, **headers):
        h = {"Content-Type": "application/json", "X-Planner": "1"}
        h.update(headers)
        return self.req("/api/planner", "PUT", body, h)

    def test_flow(self):
        code, headers, page = self.req("/planner")
        self.assertEqual(code, 200)
        self.assertIn(b"Allocation planner", page)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertTrue(json.loads(self.req("/api/report")[2])["status"]["planner"])

        view = json.loads(self.req("/api/planner")[2])
        self.assertEqual(view["plan"]["version"], 0)
        self.assertEqual({c["id"] for c in view["inventory"]["clusters"]}, {"local", "c-m-1"})

        plan = view["plan"]
        plan["clusters"] = {"c-m-1": {"env": "prod", "platform": "onprem"}}
        plan["projects"] = {"payments": {"monthly_budget": 100, "allocations": {"c-m-1": {"cpu": 4, "memory_gib": 16}}}}
        code, _, body = self.put({"version": 0, "plan": plan})
        self.assertEqual(code, 200, body)
        saved = json.loads(body)
        self.assertEqual(saved["plan"]["version"], 1)
        self.assertEqual(saved["evaluation"]["projects"]["payments"]["cost"], 200)
        self.assertTrue(any("over its budget" in i["text"] for i in saved["evaluation"]["issues"]))

        # stale version -> conflict, nothing overwritten
        code, _, body = self.put({"version": 0, "plan": plan})
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["version"], 1)

        code, headers, body = self.req("/api/planner/export.yaml")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b'requests.cpu: "4"', body)

    def test_save_needs_header_json_and_valid_plan(self):
        self.assertEqual(self.put({"version": 0, "plan": {}}, **{"X-Planner": ""})[0], 400)
        self.assertEqual(self.put({"version": 0, "plan": {}}, **{"Content-Type": "text/plain"})[0], 400)
        code, _, body = self.put({"version": 0, "plan": {"settings": {"platforms": {}, "envs": {}}}})
        self.assertIn(code, (400, 409))  # 409 if test_flow already saved
        self.assertEqual(self.req("/api/planner", "PUT", None, {"X-Planner": "1", "Content-Type": "application/json"})[0], 413)


if __name__ == "__main__":
    unittest.main()
