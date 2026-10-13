import json
import os
import re
import sys
import tempfile
import unittest

import build_dashboard as bd
import publish_project_names as ppn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "discovery"))

PROJECT_FIELD = bd.F["project"]


class BuildDashboardTest(unittest.TestCase):
    def setUp(self):
        self.rebuild()

    def rebuild(self):
        bd.PANELS.clear()
        bd.REPORT_PANELS.clear()
        bd.build_panels(bd.DEFAULT_INDEX)
        bd.build_report_panels(bd.DEFAULT_INDEX)

    def tearDown(self):
        bd.F["project"] = PROJECT_FIELD
        bd.PROJECT_NAMES.clear()
        bd.LOOKUP_INDEX[0] = None

    def generated(self):
        return "".join(json.dumps(obj, separators=(",", ":")) + "\n" for obj in bd.saved_objects(bd.DEFAULT_INDEX))

    def test_committed_ndjson_is_current(self):
        with open(bd.default_out()) as f:
            self.assertEqual(f.read(), self.generated(), "run ./build_dashboard.py and commit the .ndjson")

    def without_project(self):
        bd.F["project"] = None
        self.rebuild()

    def test_committed_no_project_ndjson_is_current(self):
        self.without_project()
        with open(bd.default_out(no_project=True)) as f:
            self.assertEqual(f.read(), self.generated(),
                             "run ./build_dashboard.py --no-project and commit the .ndjson")

    def test_no_project_never_names_the_field(self):
        self.without_project()
        text = self.generated()
        self.assertNotIn("rancher.project.id", text)
        self.assertNotIn("Rancher Projects", [p["title"] for p in bd.PANELS])
        self.assertNotIn("Projects", [p["title"] for p in bd.REPORT_PANELS])
        dashboard = bd.saved_objects(bd.DEFAULT_INDEX)[1]
        self.assertNotIn("ctrl-project", dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        self.assertEqual(len(dashboard["references"]), 2)

    def test_saved_objects(self):
        data_view, as_is, report = bd.saved_objects(bd.DEFAULT_INDEX)
        self.assertEqual(data_view["type"], "index-pattern")
        self.assertEqual([as_is["id"], report["id"]], [bd.DASHBOARD_ID, bd.REPORT_ID])
        for dashboard, source in ((as_is, bd.PANELS), (report, bd.REPORT_PANELS)):
            self.check_dashboard(dashboard, source)

    def check_dashboard(self, dashboard, source):
        self.assertEqual(dashboard["typeMigrationVersion"], "10.3.0")
        panels = json.loads(dashboard["attributes"]["panelsJSON"])
        self.assertEqual(len(panels), len(source))
        ids = [p["panelIndex"] for p in panels]
        self.assertEqual(len(ids), len(set(ids)))
        for p in panels:
            self.assertLessEqual(p["gridData"]["x"] + p["gridData"]["w"], 48)
            if p["type"] == "lens":
                state = p["embeddableConfig"]["attributes"]["state"]
                layer = state["datasourceStates"]["textBased"]["layers"]["layer_0"]
                self.assertIn(layer["index"], state["adHocDataViews"])
                self.assertEqual(layer["query"], state["query"])
                self.assertNotRegex(layer["query"]["esql"], r"\{[A-Za-z_]+\}", "unformatted placeholder")

    def test_every_query_buckets_inside_stats(self):
        for p in bd.PANELS + bd.REPORT_PANELS:
            if p["esql"] and "BUCKET(" in p["esql"]:
                line = next(l for l in p["esql"].splitlines() if "BUCKET(" in l)
                self.assertIn("BY", line, p["title"])

    def test_table_columns(self):
        self.assertEqual(bd.table_columns("FROM x | STATS a = SUM(b), `c d` = MAX(e) BY f, g = h"),
                         ["a", "c d", "f", "g"])
        self.assertEqual(bd.table_columns("FROM x | KEEP a, `b, c` | SORT a"), ["a", "b, c"])
        tables = [p for p in bd.PANELS + bd.REPORT_PANELS if p["kind"] == "table"]
        self.assertTrue(tables)
        for p in tables:
            self.assertTrue(bd.table_columns(p["esql"]), p["title"])

    def test_system_classification_matches_discover(self):
        from discover import DEFAULT_SYSTEM_NS_REGEX, DEFAULT_SYSTEM_PROJECT_REGEX
        self.assertEqual(DEFAULT_SYSTEM_NS_REGEX, "^(" + bd.SYSTEM_NS_LUCENE + ")$")
        self.assertEqual(DEFAULT_SYSTEM_PROJECT_REGEX, "^(" + bd.SYSTEM_PROJECT_LUCENE + ")$")
        expr = bd.system_ns_expr()
        self.assertIn('namespace LIKE "kube-*"', expr)
        self.assertIn('namespace RLIKE "p-[a-z0-9]{5}"', expr)
        self.assertEqual(expr.count(" OR ") + 1, len(bd.SYSTEM_NS_LUCENE.split("|")))

    def test_rancher_project_names(self):
        items = {"items": [
            {"metadata": {"name": "p-bbjk7", "namespace": "c-m-1"}, "spec": {"displayName": "payments"}},
            {"metadata": {"name": "p-9z9dv", "namespace": "c-m-1"}, "spec": {"displayName": 'Sys "tem"'}}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(items, f)
        try:
            names = bd.load_rancher_projects(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(names, {"p-bbjk7": "payments", "p-9z9dv": 'Sys "tem"'})
        bd.PROJECT_NAMES.update(names)
        self.assertEqual(bd.pname_expr(),
                         'CASE(project == "p-9z9dv", "Sys \\"tem\\"", project == "p-bbjk7", "payments", project)')
        self.assertIn("TO_LOWER(pname) RLIKE", bd.category_expr())
        self.rebuild()
        self.assertIn('"payments"', bd.REPORT_PANELS[-1]["esql"])

    def test_project_lookup_index(self):
        bd.LOOKUP_INDEX[0] = "rancher-projects"
        self.rebuild()
        self.assertEqual(bd.pname_stage(), "| LOOKUP JOIN rancher-projects ON project\n"
                                           "| EVAL pname = COALESCE(project_name, project)")
        self.assertIn("TO_LOWER(pname) RLIKE", bd.category_expr())
        queries = [p["esql"] for p in bd.REPORT_PANELS if p["esql"]]
        self.assertTrue(all(q.count("LOOKUP JOIN") == 1 for q in queries))
        self.assertNotIn("LOOKUP JOIN", "".join(p["esql"] or "" for p in bd.PANELS))
        for q in queries:  # the join comes after the Project ID column exists and before the name is used
            self.assertLess(q.index("project = COALESCE("), q.index("LOOKUP JOIN"))
            self.assertLess(q.index("LOOKUP JOIN"), q.index("EVAL category"))
        self.check_dashboard(bd.saved_objects(bd.DEFAULT_INDEX)[2], bd.REPORT_PANELS)
        self.without_project()
        self.assertNotIn("LOOKUP JOIN", self.generated())


class FakeElastic:
    def __init__(self, mode="missing"):
        self.mode, self.calls = mode, []

    def request(self, method, path, body=None, content_type="application/json"):
        self.calls.append((method, path, body, content_type))
        if method == "GET":
            if self.mode == "missing":
                raise ppn.urllib.error.HTTPError(path, 404, "Not Found", None, None)
            return {"rancher-projects": {"settings": {"index": {"mode": self.mode} if self.mode else {}}}}
        if path.endswith("/_bulk?refresh=true"):
            return {"errors": False, "items": []}
        return {"deleted": 2} if "_delete_by_query" in path else {}


class PublishProjectNamesTest(unittest.TestCase):
    DOCS = ppn.documents({"p-bbjk7": "payments", "p-9z9dv": "System"})

    def test_documents_use_the_dashboard_columns(self):
        self.assertEqual(self.DOCS, [{"project": "p-9z9dv", "project_name": "System"},
                                     {"project": "p-bbjk7", "project_name": "payments"}])
        self.assertEqual(set(ppn.INDEX_BODY["mappings"]["properties"]), {bd.LOOKUP_KEY, bd.LOOKUP_NAME})
        self.assertEqual(ppn.INDEX_BODY["settings"]["index"]["mode"], "lookup")

    def test_publish_creates_writes_and_removes(self):
        es = FakeElastic()
        self.assertEqual(ppn.publish(es, "rancher-projects", self.DOCS), (2, 2))
        self.assertEqual([(m, p) for m, p, _, _ in es.calls], [
            ("GET", "/rancher-projects/_settings"), ("PUT", "/rancher-projects"),
            ("POST", "/rancher-projects/_bulk?refresh=true"),
            ("POST", "/rancher-projects/_delete_by_query?refresh=true")])
        lines = [json.loads(l) for l in es.calls[2][2].splitlines()]
        self.assertEqual(lines[0], {"index": {"_index": "rancher-projects", "_id": "p-9z9dv"}})
        self.assertEqual(lines[1], self.DOCS[0])
        self.assertEqual(es.calls[2][3], "application/x-ndjson")
        self.assertEqual(es.calls[3][2]["query"]["bool"]["must_not"]["ids"]["values"], ["p-9z9dv", "p-bbjk7"])

    def test_publish_keeps_an_existing_lookup_index(self):
        es = FakeElastic("lookup")
        ppn.publish(es, "rancher-projects", self.DOCS)
        self.assertNotIn("PUT", [m for m, _, _, _ in es.calls])

    def test_publish_refuses_an_ordinary_index(self):
        es = FakeElastic(None)
        with self.assertRaises(SystemExit):
            ppn.publish(es, "rancher-projects", self.DOCS)
        self.assertEqual(len(es.calls), 1)


if __name__ == "__main__":
    unittest.main()
