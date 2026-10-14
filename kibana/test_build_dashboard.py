import json
import os
import re
import sys
import tempfile
import unittest

import add_project_names as apn
import build_collector_values as bcv
import build_dashboard as bd
import publish_project_names as ppn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "discovery"))

PROJECT_FIELD = bd.F["project"]
FIELDS = dict(bd.F)
CP_FIELD = bd.CP_FIELD[0]


class BuildDashboardTest(unittest.TestCase):
    def setUp(self):
        self.rebuild()

    def rebuild(self):
        bd.PANELS.clear()
        bd.REPORT_PANELS.clear()
        bd.build_panels(bd.DEFAULT_INDEX)
        bd.build_report_panels(bd.DEFAULT_INDEX)

    def tearDown(self):
        bd.F.update(FIELDS)
        bd.PROJECT_NAMES.clear()
        bd.LOOKUP_INDEX[0] = None
        bd.NAME_FIELD[0] = None
        bd.CP_FIELD[0] = CP_FIELD

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

    def test_committed_project_names_ndjson_is_current(self):
        bd.LOOKUP_INDEX[0] = bd.DEFAULT_LOOKUP_INDEX
        self.rebuild()
        with open(bd.default_out(lookup=True)) as f:
            self.assertEqual(f.read(), self.generated(),
                             "run ./build_dashboard.py --project-lookup-index rancher-projects and commit the .ndjson")

    def test_no_project_never_names_the_field(self):
        self.without_project()
        text = self.generated()
        self.assertNotIn("rancher.project.id", text)
        self.assertNotIn("Rancher Projects", [p["title"] for p in bd.PANELS])
        self.assertNotIn("Projects", [p["title"] for p in bd.REPORT_PANELS])
        dashboard = bd.saved_objects(bd.DEFAULT_INDEX)[1]
        self.assertNotIn("ctrl-project", dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        self.assertEqual(len([r for r in dashboard["references"] if r["name"].startswith("controlGroup_")]), 2)

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

    def test_add_project_names_matches_the_generator(self):
        projects = {"items": [
            {"metadata": {"name": "p-bbjk7"}, "spec": {"displayName": "payments"}},
            {"metadata": {"name": "p-9z9dv"}, "spec": {"displayName": 'Sys "tem" \\ $1'}},
            {"metadata": {"name": "p-noname"}}]}
        names = apn.project_names(projects)
        self.assertEqual(names, bd.rancher_project_names(projects))
        with open(bd.default_out()) as f:
            ids_only = f.readlines()
        patched, changed = apn.add_names(ids_only, names)
        bd.PROJECT_NAMES.update(names)
        self.rebuild()
        self.assertEqual("".join(patched), self.generated())
        self.assertEqual(changed, 2 * len([p for p in bd.REPORT_PANELS if p["esql"]]))
        # again, with other names and on a file that already has names: replaced, not nested
        again, _ = apn.add_names(patched, {"p-bbjk7": "billing"})
        bd.PROJECT_NAMES.clear()
        bd.PROJECT_NAMES["p-bbjk7"] = "billing"
        self.rebuild()
        self.assertEqual("".join(again), self.generated())
        with open(bd.default_out(no_project=True)) as f:
            self.assertEqual(apn.add_names(f.readlines(), names)[1], 0)

    def test_add_project_names_on_a_kibana_export(self):
        path = os.path.join(os.path.dirname(bd.default_out()), "cluster-resources-cra-elastic.ndjson")
        with open(path) as f:
            exported = f.readlines()
        patched, changed = apn.add_names(exported, {"p-bbjk7": "billing"})
        self.assertEqual(changed, 30)
        text = "".join(patched)
        self.assertIn("billing", text)
        self.assertNotIn("platform-tools", text)
        self.assertEqual(len(patched), len(exported))
        self.assertEqual(text.count("TO_LOWER(pname) RLIKE"), "".join(exported).count("TO_LOWER(pname) RLIKE"))

    def test_without_quota_and_hpa_metrics(self):
        for kind in bd.OPTIONAL_FIELDS:
            bd.F.update(dict.fromkeys(bd.OPTIONAL_FIELDS[kind]))
        self.rebuild()
        text = self.generated()
        for word in ("resource_quota", "resourcequota", "k8s.hpa"):
            self.assertNotIn(word, text)
        self.assertIn("NULL IS NOT NULL", text)
        self.assertNotIn("None", "".join(p["esql"] or "" for p in bd.PANELS + bd.REPORT_PANELS))
        self.check_dashboard(bd.saved_objects(bd.DEFAULT_INDEX)[2], bd.REPORT_PANELS)

    def test_project_name_field(self):
        bd.NAME_FIELD[0] = bd.DEFAULT_NAME_FIELD
        self.rebuild()
        field = "resource.attributes.rancher.project.name"
        report = [p["esql"] for p in bd.REPORT_PANELS if p["esql"]]
        for q in report:  # the name is carried through both aggregations, then used
            self.assertIn(f"project_name = MAX({field})", q)
            self.assertIn("project_name = MAX(project_name)", q)
            self.assertIn("| EVAL pname = COALESCE(project_name, project)", q)
            self.assertNotIn("LOOKUP JOIN", q)
        self.assertIn("TO_LOWER(pname) RLIKE", bd.category_expr())
        as_is = "".join(p["esql"] or "" for p in bd.PANELS)
        self.assertIn(f'project = COALESCE({field}, resource.attributes.rancher.project.id, "(no project)")', as_is)
        for dashboard in bd.saved_objects(bd.DEFAULT_INDEX)[1:]:
            control = json.loads(dashboard["attributes"]["controlGroupInput"]["panelsJSON"])["ctrl-project"]
            self.assertEqual(control["explicitInput"]["fieldName"], field)
            self.assertEqual(control["explicitInput"]["title"], "Rancher Project")
        self.check_dashboard(bd.saved_objects(bd.DEFAULT_INDEX)[2], bd.REPORT_PANELS)
        self.without_project()
        self.assertNotIn("rancher.project", self.generated())

    def test_control_plane_nodes_filtered_out(self):
        field = "resource.attributes.k8s.node.role.control_plane"
        for p in bd.PANELS + bd.REPORT_PANELS:  # a dashboard filter, so no query fails without the field
            self.assertNotIn("control_plane", p["esql"] or "", p["title"])
        for dashboard in bd.saved_objects(bd.DEFAULT_INDEX)[1:]:
            search = json.loads(dashboard["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
            [f] = search["filter"]
            self.assertTrue(f["meta"]["negate"])
            self.assertEqual(f["query"], {"match_phrase": {field: "true"}})
            self.assertIn({"type": "index-pattern", "id": bd.DATA_VIEW_ID, "name": f["meta"]["indexRefName"]},
                          dashboard["references"])
            self.assertNotIn("control-plane", dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        bd.CP_FIELD[0] = None
        for dashboard in bd.saved_objects(bd.DEFAULT_INDEX)[1:]:
            self.assertEqual(json.loads(dashboard["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])["filter"], [])
            self.assertNotIn("searchSourceJSON", json.dumps(dashboard["references"]))
        self.rebuild()
        self.assertNotIn("Control-plane", bd.REPORT_PANELS[0]["markdown"])

    def test_collector_values(self):
        names = {"p-bbjk7": "payments", "p-9z9dv": 'Sys "tem" $x'}
        self.assertEqual(bcv.statements(names), [
            'set(attributes["rancher.project.name"], "Sys \\"tem\\" $$x") where attributes["rancher.project.id"] == "p-9z9dv"',
            'set(attributes["rancher.project.name"], "payments") where attributes["rancher.project.id"] == "p-bbjk7"'])
        self.assertTrue(bd.DEFAULT_NAME_FIELD.endswith("." + bcv.NAME_ATTR))
        self.assertTrue(bd.F["project"].endswith("." + bcv.ID_ATTR))
        text = bcv.render(names, "metrics")
        self.assertIn("Add transform/rancher_project_names to service.pipelines.metrics.processors", text)
        config = json.loads(text[text.index("{"):])["collectors"]["metrics"]["config"]
        self.assertNotIn("service", config)
        self.assertEqual(config["processors"][bcv.PROCESSOR]["metric_statements"][0]["context"], "resource")
        pipeline = ["k8s_attributes", bcv.PROCESSOR, "batch"]
        config = bcv.values(names, pipeline=pipeline)["collectors"]["daemon"]["config"]
        self.assertEqual(config["service"]["pipelines"]["metrics"]["processors"], pipeline)

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
