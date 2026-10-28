import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
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

    def get(self, path, method="GET", headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_dashboard(self):
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Cluster resource report", body)
        self.assertIn(b"Requests vs. ResourceQuota", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_report(self):
        code, _, body = self.get("/api/report")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["report"]["clusters"][0]["cluster"], "c1")
        self.assertFalse(data["status"]["collect_enabled"])

    def test_report_gzip_and_not_modified(self):
        import gzip
        code, headers, body = self.get("/api/report")
        etag = headers["ETag"]
        self.assertTrue(etag.startswith('"') and len(etag) > 10)
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertIsNone(headers.get("Content-Encoding"))
        # same content compressed, same ETag
        code, headers, gz = self.get("/api/report", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual((code, headers["Content-Encoding"], headers["ETag"]), (200, "gzip", etag))
        self.assertEqual(gzip.decompress(gz), body)
        # the page polls with the last ETag and gets no body while nothing changed
        code, headers, body = self.get("/api/report", headers={"If-None-Match": etag})
        self.assertEqual((code, headers["ETag"], body), (304, etag, b""))
        # a different collector state changes the ETag (the page shows "collecting now")
        with mock.patch.object(server.Collector, "status", return_value={"state": "collecting"}):
            code, headers, body = self.get("/api/report", headers={"If-None-Match": etag})
        self.assertEqual(code, 200)
        self.assertNotEqual(headers["ETag"], etag)
        self.assertEqual(json.loads(body)["status"], {"state": "collecting"})

    def test_versions_from_the_chart(self):
        with mock.patch.dict(os.environ, {"CHART_VERSION": "0.5.2", "IMAGE_TAG": "sha-abc1234"}):
            data = json.loads(self.get("/api/report")[2])
        self.assertEqual(data["status"]["versions"], {"chart": "0.5.2", "image": "sha-abc1234"})
        with mock.patch.dict(os.environ, {"CHART_VERSION": "", "IMAGE_TAG": ""}):
            self.assertEqual(server.versions(), {"chart": None, "image": None})  # run without the chart

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

    def test_no_fleet_view_by_default(self):
        self.assertEqual(self.get("/api/clusters")[0], 404)
        self.assertIsNone(json.loads(self.get("/api/report")[2])["status"]["downstream"])

    def test_parse_downstream_report(self):
        self.assertEqual(server.parse_downstream_report("resource-report/resource-report-cluster-resource-report:80"),
                         ("resource-report", "resource-report-cluster-resource-report", 80))
        for bad in ("resource-report", "ns/svc", "ns/svc:http", ""):
            with self.assertRaises(ValueError):
                server.parse_downstream_report(bad)

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
        self.assertEqual(view["reference"]["onprem"]["rates"], {"cpu_rate": 10.60, "mem_rate": 1.23})

        plan = view["plan"]
        plan["clusters"] = {"c-m-1": {"env": "prod", "platform": "onprem"}}
        plan["settings"]["platforms"]["onprem"] = {"cpu_rate": 25, "mem_rate": 6.25}
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

        # history: save a second version, then restore the first as version 3
        plan2 = saved["plan"]
        plan2["projects"]["payments"]["allocations"]["c-m-1"]["cpu"] = 2
        self.assertEqual(self.put({"version": 1, "plan": plan2})[0], 200)
        hist = json.loads(self.req("/api/planner/history")[2])
        self.assertEqual((hist["current"], [v["version"] for v in hist["versions"]]), (2, [2, 1]))
        self.assertEqual(hist["versions"][0]["changes"]["projects"], ["payments"])
        code, _, body = self.req("/api/planner/history/1")
        self.assertEqual(code, 200)
        v1 = json.loads(body)["plan"]
        self.assertEqual(v1["projects"]["payments"]["allocations"]["c-m-1"]["cpu"], 4)
        self.assertEqual(self.req("/api/planner/history/99")[0], 404)
        self.assertEqual(self.req("/api/planner/history/x")[0], 404)
        code, _, body = self.put({"version": 2, "plan": v1, "restored_from": 1})
        self.assertEqual(code, 200, body)
        self.assertEqual((json.loads(body)["plan"]["version"], json.loads(body)["plan"]["restored_from"]), (3, 1))
        self.assertEqual(self.put({"version": 3, "plan": v1, "restored_from": "1"})[0], 400)

    def test_save_needs_header_json_and_valid_plan(self):
        self.assertEqual(self.put({"version": 0, "plan": {}}, **{"X-Planner": ""})[0], 400)
        self.assertEqual(self.put({"version": 0, "plan": {}}, **{"Content-Type": "text/plain"})[0], 400)
        code, _, body = self.put({"version": 0, "plan": {"settings": {"platforms": {}, "envs": {}}}})
        self.assertIn(code, (400, 409))  # 409 if test_flow already saved
        self.assertEqual(self.req("/api/planner", "PUT", None, {"X-Planner": "1", "Content-Type": "application/json"})[0], 413)


class PlannerServerConfigMap(unittest.TestCase):
    """The planner with its plan in a ConfigMap (fake API), and an unreachable one."""

    def serve(self, kubectl):
        import test_planner
        args = server.discover.build_parser().parse_args(["--rancher-local-self"])
        args.data_dir, args.interval, args.no_collect = "/nonexistent", "30m", True
        store = server.planner.ConfigMapPlanStore("rr", "rr-planner", kubectl=kubectl)
        backend = server.PlannerBackend(args, server.Collector(args), inventory_loader=test_planner.inventory,
                                        store=store)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(backend.collector, backend))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_port}"

    def call(self, base, method="GET", body=None):
        h = {"Content-Type": "application/json", "X-Planner": "1"}
        req = urllib.request.Request(base + "/api/planner", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_save_to_configmap(self):
        import test_planner
        api = test_planner.FakeConfigMaps()
        base = self.serve(api)
        code, view = self.call(base)
        self.assertEqual(code, 200)
        code, saved = self.call(base, "PUT", {"version": 0, "plan": view["plan"]})
        self.assertEqual((code, saved["plan"]["version"]), (200, 1))
        self.assertIn("plan.json.gz", api.objects["rr-planner"]["binaryData"])
        self.assertEqual(self.call(base, "PUT", {"version": 0, "plan": view["plan"]})[0], 409)

    def test_unreachable_configmap(self):
        def forbidden(*a, **kw):
            raise server.discover.KubectlError('configmaps "rr-planner" is forbidden')
        base = self.serve(forbidden)
        code, body = self.call(base)
        self.assertEqual(code, 502)
        self.assertIn("forbidden", body["error"])


class CapacityServer(unittest.TestCase):
    """Both planners side by side: /planner and /capacity with separate plans."""

    @classmethod
    def setUpClass(cls):
        import test_planner
        cls.tmp = tempfile.TemporaryDirectory()
        args = server.discover.build_parser().parse_args(["--rancher-local-self"])
        args.data_dir, args.interval, args.no_collect = cls.tmp.name, "30m", True
        args.planner = args.capacity_planner = True
        collector = server.Collector(args)
        budget = server.PlannerBackend(args, collector, inventory_loader=test_planner.inventory)
        capacity = server.PlannerBackend(args, collector, inventory_loader=test_planner.inventory, mode="capacity")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(collector, budget, capacity))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def req(self, path, method="GET", body=None):
        h = {"Content-Type": "application/json", "X-Planner": "1"} if body is not None else {}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_capacity_planner(self):
        code, page = self.req("/capacity")
        self.assertEqual(code, 200)
        self.assertIn(b"Allocation planner", page)  # same page, switches to capacity mode from the API
        view = json.loads(self.req("/api/capacity")[1])
        self.assertEqual(view["mode"], "capacity")
        self.assertNotIn("reference", view)
        self.assertEqual(set(view["plan"]["settings"]), {"envs"})
        plan = view["plan"]
        plan["clusters"] = {"c-m-1": {"env": "prod"}}
        plan["projects"] = {"payments": {"cpu_envelope": 2, "allocations": {"c-m-1": {"cpu": 3, "memory_gib": 4}}}}
        code, body = self.req("/api/capacity", "PUT", {"version": 0, "plan": plan})
        self.assertEqual(code, 200, body)
        saved = json.loads(body)
        self.assertTrue(any("above its envelope of 2" in i["text"] for i in saved["evaluation"]["issues"]))
        self.assertIn(b"by the capacity planner", self.req("/api/capacity/export.yaml")[1])
        hist = json.loads(self.req("/api/capacity/history")[1])
        self.assertEqual((hist["mode"], len(hist["versions"])), ("capacity", 1))
        self.assertNotIn("budget", hist["versions"][0])
        self.assertEqual(json.loads(self.req("/api/planner/history")[1])["versions"], [])
        # the budget planner has its own, untouched plan
        self.assertEqual(json.loads(self.req("/api/planner")[1])["plan"]["version"], 0)
        status = json.loads(self.req("/api/report")[1])["status"]
        self.assertTrue(status["planner"] and status["capacity_planner"])


if __name__ == "__main__":
    unittest.main()


class FleetView(unittest.TestCase):
    """/api/clusters on the Rancher local cluster, with a fake cluster list (no kubectl)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        args = server.discover.build_parser().parse_args(["--rancher-local-self"])
        args.data_dir, args.interval, args.no_collect = cls.tmp.name, "30m", True
        args.downstream_report = "resource-report/resource-report-cluster-resource-report:80"
        cls.clusters = [{"id": "local", "name": "local", "ready": True, "provider": "k3s", "local": True},
                        {"id": "c-m-abc12", "name": "onprem-prod-01", "ready": True, "provider": "rke2", "local": False}]
        cls.calls, cls.fail = 0, False

        def loader():
            cls.calls += 1
            if cls.fail:
                raise discover.KubectlError("kubectl get clusters.management.cattle.io: forbidden")
            return cls.clusters
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(
            server.Collector(args), rancher_clusters=server.RancherClusters(args, loader=loader)))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_clusters_listed_and_cached(self):
        code, body = self.get("/api/clusters")
        self.assertEqual((code, body["clusters"]), (200, self.clusters))
        self.get("/api/clusters")
        self.assertEqual(self.calls, 1)   # cached: one kubectl per minute at most
        self.assertEqual(self.get("/api/report")[1]["status"]["downstream"],
                         {"namespace": "resource-report", "service": "resource-report-cluster-resource-report", "port": 80})

    def test_kubectl_error_is_a_502(self):
        type(self).fail = True
        try:
            with mock.patch.object(server.RancherClusters, "TTL", 0):
                code, body = self.get("/api/clusters")
        finally:
            type(self).fail = False
        self.assertEqual(code, 502)
        self.assertIn("forbidden", body["error"])

    def test_option_needs_the_local_cluster(self):
        with mock.patch.object(sys, "argv", ["server.py", "--downstream-report", "ns/svc:80"]), \
                self.assertRaises(SystemExit):
            server.main()
        with mock.patch.object(sys, "argv", ["server.py", "--rancher-local-self", "--downstream-report", "nonsense"]), \
                self.assertRaises(SystemExit):
            server.main()


class LiveApi(unittest.TestCase):
    """/live and /api/live/* with an in-memory live state (no cluster)."""

    @classmethod
    def setUpClass(cls):
        import live
        cls.tmp = tempfile.TemporaryDirectory()
        args = server.discover.build_parser().parse_args([])
        args.data_dir, args.interval, args.no_collect = cls.tmp.name, "30m", True
        cls.state = live.LiveState(args)
        cls.state.replace_all("nodes", [{"metadata": {"name": "n1", "labels": {}}, "spec": {},
                                         "status": {"allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"},
                                                    "conditions": [{"type": "Ready", "status": "True"}]}}])
        cls.state.replace_all("pods", [{"metadata": {"name": "web-1", "namespace": "shop", "uid": "u1"},
                                        "spec": {"nodeName": "n1", "containers": [{"name": "web", "resources": {"requests": {"cpu": "250m"}}}]},
                                        "status": {"phase": "Running"}}])
        collector = server.Collector(args)
        collector.live = cls.state
        cls.collector = collector
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(collector, live_state=cls.state))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_page_and_link(self):
        code, headers, body = self.get("/live")
        self.assertEqual(code, 200)
        self.assertIn(b"Live resource view", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertTrue(json.loads(self.get("/api/report")[2])["status"]["live"])   # the report page shows the link

    def test_snapshot(self):
        import gzip
        code, headers, body = self.get("/api/live/snapshot")
        snap = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual([n["name"] for n in snap["nodes"]], ["n1"])
        self.assertEqual(snap["pods"][0]["cpu_req"], 0.25)
        self.assertEqual(snap["status"]["counts"], {"nodes": 1, "namespaces": 0, "pods": 1})
        code, headers, gz = self.get("/api/live/snapshot", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(gz))["seq"], snap["seq"])

    def test_changes(self):
        seq = self.state.seq
        self.assertEqual(json.loads(self.get(f"/api/live/changes?since={seq}")[2])["seq"], seq)
        self.assertTrue(json.loads(self.get("/api/live/changes")[2])["reset"])       # no since: start with the snapshot
        self.assertTrue(json.loads(self.get("/api/live/changes?since=abc")[2])["reset"])

    def test_stream(self):
        seq = self.state.seq
        with mock.patch.object(server, "LIVE_DEBOUNCE", 0), mock.patch.object(server, "LIVE_PING", 1):
            req = urllib.request.Request(f"{self.base}/api/live/stream?since={seq}")
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.headers["Content-Type"], "text/event-stream")
                self.assertEqual(r.readline(), b"retry: 5000\n")
                r.readline()
                self.assertEqual(r.readline(), b"event: ping\n")            # nothing changed within LIVE_PING
                self.assertEqual(json.loads(r.readline()[len(b"data: "):]), {"seq": seq})
                r.readline()
                self.state.apply("pods", "DELETED", {"metadata": {"name": "web-1", "namespace": "shop"}, "spec": {}, "status": {}})
                lines = [r.readline() for _ in range(3)]
                self.assertEqual(lines[0], f"id: {seq + 1}\n".encode())
                self.assertEqual(lines[1], b"event: changes\n")
                delta = json.loads(lines[2][len(b"data: "):])
                self.assertEqual(delta["pods"], {"shop/web-1": None})
        # put the pod back for the other tests
        self.state.apply("pods", "ADDED", {"metadata": {"name": "web-1", "namespace": "shop", "uid": "u1"},
                                           "spec": {"nodeName": "n1", "containers": [{"name": "web", "resources": {"requests": {"cpu": "250m"}}}]},
                                           "status": {"phase": "Running"}})

    def test_off_without_state(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.Collector(self.collector.args)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/live/snapshot")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 404)
            self.assertIn("live view is off", json.loads(cm.exception.read())["error"])
            with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/report", timeout=5) as r:
                self.assertFalse(json.loads(r.read())["status"]["live"])
        finally:
            httpd.shutdown()
