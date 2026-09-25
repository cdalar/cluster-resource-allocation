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

    def test_parse_duration(self):
        self.assertEqual(server.parse_duration("15m"), 900)
        self.assertEqual(server.parse_duration("1h"), 3600)
        with self.assertRaises(ValueError):
            server.parse_duration("15")


if __name__ == "__main__":
    unittest.main()
