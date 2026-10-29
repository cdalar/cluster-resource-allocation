"""build_live_dashboard.py: the single-file live dashboard builds, imports and serves the live routes."""
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import test_live

HERE = os.path.dirname(os.path.abspath(__file__))


class SingleFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "live-dashboard.py")
        subprocess.run([sys.executable, os.path.join(HERE, "build_live_dashboard.py"), cls.path], check=True,
                       capture_output=True)
        spec = importlib.util.spec_from_file_location("live_dashboard", cls.path)
        cls.mod = sys.modules["live_dashboard"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("live_dashboard", None)
        cls.tmp.cleanup()

    def test_help_runs_isolated(self):
        out = subprocess.run([sys.executable, "-I", self.path, "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--rancher-local-kubeconfig", out.stdout)

    def test_modules_are_bound_to_the_file(self):
        m = self.mod
        self.assertIs(m.discover, m)
        self.assertIs(m.live, m)
        self.assertIs(m.textreport, m)
        self.assertNotIn("import discover", open(self.path).read())
        for name in ("LiveState", "Watcher", "MetricsPoller", "start", "render", "parse_quantity", "pod_effective",
                     "classify", "load_rancher_projects", "load_names_configmap", "context_label", "make_handler"):
            self.assertTrue(callable(getattr(m, name)), name)

    def test_pages_have_no_report_link(self):
        for html in (self.mod.LIVE_HTML, self.mod.TEXT_HTML):
            self.assertNotIn('href="."', html)
        self.assertIn('href="text"', self.mod.LIVE_HTML)
        self.assertIn('href="live"', self.mod.TEXT_HTML)
        self.assertEqual(self.mod.PAGES["/"], self.mod.LIVE_HTML)

    def test_serves_the_live_routes(self):
        m = self.mod
        st = m.LiveState(type("A", (), {"cluster_name": "prod-01"})())
        st.replace_all("nodes", [test_live.NODE])
        st.replace_all("namespaces", [test_live.NS])
        st.replace_all("pods", [test_live.pod("api-1", {"kind": "ReplicaSet", "name": "api-7d9f"},
                                              {"app": "api", "pod-template-hash": "7d9f"})])
        st.set_names({"c-m-abc:p-12345": "Payments"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), m.make_handler(st))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def get(path, headers=None):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                c.request("GET", path, headers=headers or {})
                r = c.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            for path in ("/", "/live"):
                status, headers, body = get(path)
                self.assertEqual(status, 200)
                self.assertIn("Content-Security-Policy", headers)
                self.assertEqual(body.decode(), m.LIVE_HTML)
            self.assertEqual(get("/text")[2].decode(), m.TEXT_HTML)
            status, _, body = get("/api/live/snapshot")
            snap = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(snap["status"]["cluster"], "prod-01")
            self.assertEqual([n["name"] for n in snap["nodes"]], ["worker-1"])
            self.assertEqual(snap["namespaces"][0]["project"], "Payments")
            status, _, body = get("/api/live/changes?since=0")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["seq"], st.seq)
            status, _, body = get("/api/live/text?report=nodes")
            self.assertEqual(status, 200)
            self.assertIn("worker-1", body.decode())
            status, _, body = get("/api/live/text?report=workloads")
            self.assertEqual(status, 400)
            self.assertEqual(get("/healthz")[0], 200)
            self.assertEqual(get("/api/report")[0], 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
