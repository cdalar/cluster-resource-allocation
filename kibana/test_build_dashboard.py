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


def report_panels():
    """Panels about Projects: every board but "Will it fit?", which is about nodes and has no Project column."""
    return [p for p in bd.REPORT_PANELS if p["board"] != "fit"]


class BuildDashboardTest(unittest.TestCase):
    def setUp(self):
        self.rebuild()

    def rebuild(self):
        bd.REPORT_PANELS.clear()
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
        self.assertNotIn("Projects", [p["title"] for p in bd.REPORT_PANELS])
        dashboard = bd.saved_objects(bd.DEFAULT_INDEX)[1]
        self.assertNotIn("ctrl-project", dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        self.assertEqual(len([r for r in dashboard["references"] if r["name"].startswith("controlGroup_")]), 2)
        # no "Project owner" dashboard without Projects, and no link to it
        ids = [d["id"] for d in bd.saved_objects(bd.DEFAULT_INDEX)[1:]]
        self.assertEqual(ids, [bd.REPORT_ID, bd.PROJECTS_ID, bd.LIMITS_ID, bd.FIT_ID])
        self.assertNotIn(bd.OWNER_ID, text)

    def test_saved_objects(self):
        data_view, *dashboards = bd.saved_objects(bd.DEFAULT_INDEX)
        self.assertEqual(data_view["type"], "index-pattern")
        self.assertEqual([d["id"] for d in dashboards],
                         [bd.REPORT_ID, bd.PROJECTS_ID, bd.OWNER_ID, bd.LIMITS_ID, bd.FIT_ID])
        self.check_dashboards()
        ids = [d["id"] for d in dashboards]
        for d in dashboards:  # every About links the other dashboards
            about = next(p for p in json.loads(d["attributes"]["panelsJSON"]) if p["type"] == "visualization")
            text = about["embeddableConfig"]["savedVis"]["params"]["markdown"]
            self.assertEqual(sorted(re.findall(r"\(#/view/([a-z-]+)\)", text)), sorted(set(ids) - {d["id"]}))

    def check_dashboards(self):
        boards = {did: board for board, did, *_ in bd.REPORT_BOARDS}
        for d in bd.saved_objects(bd.DEFAULT_INDEX)[1:]:
            self.check_dashboard(d, bd.board_panels(boards[d["id"]]))

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
        for p in bd.REPORT_PANELS:
            if p["esql"] and "BUCKET(" in p["esql"]:
                line = next(l for l in p["esql"].splitlines() if "BUCKET(" in l)
                self.assertIn("BY", line, p["title"])

    def test_table_columns(self):
        self.assertEqual(bd.table_columns("FROM x | STATS a = SUM(b), `c d` = MAX(e) BY f, g = h"),
                         ["a", "c d", "f", "g"])
        self.assertEqual(bd.table_columns("FROM x | KEEP a, `b, c` | SORT a"), ["a", "b, c"])
        tables = [p for p in bd.REPORT_PANELS if p["kind"] == "table"]
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
        self.assertIn('"payments"', report_panels()[-1]["esql"])

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
        # every query twice (the panel keeps it in two places), the "Project owner" ones twice more (their early
        # Project filter), and the Project control's options query
        owner = len([p for p in bd.board_panels("owner") if p["esql"]])
        self.assertEqual(changed, 2 * len([p for p in report_panels() if p["esql"]]) + 2 * owner + 1)
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
        self.assertNotIn("None", "".join(p["esql"] or "" for p in bd.REPORT_PANELS))
        self.check_dashboards()

    def test_project_name_field(self):
        bd.NAME_FIELD[0] = bd.DEFAULT_NAME_FIELD
        self.rebuild()
        field = "resource.attributes.rancher.project.name"
        report = [p["esql"] for p in report_panels() if p["esql"]]
        for q in report:  # the name is carried through both aggregations, then used
            self.assertIn(f"project_name = MAX({field})", q)
            self.assertIn("project_name = MAX(project_name)", q)
            self.assertIn("| EVAL pname = COALESCE(project_name, project)", q)
            self.assertNotIn("LOOKUP JOIN", q)
        self.assertIn("TO_LOWER(pname) RLIKE", bd.category_expr())
        for dashboard in bd.saved_objects(bd.DEFAULT_INDEX)[1:-1]:  # not "Will it fit?": no Project control
            if dashboard["id"] == bd.OWNER_ID:  # an ES|QL control with names in every variant
                continue
            control = json.loads(dashboard["attributes"]["controlGroupInput"]["panelsJSON"])["ctrl-project"]
            self.assertEqual(control["explicitInput"]["fieldName"], field)
            self.assertEqual(control["explicitInput"]["title"], "Rancher Project")
        self.check_dashboards()
        self.without_project()
        self.assertNotIn("rancher.project", self.generated())

    def test_control_plane_nodes_filtered_out(self):
        field = "resource.attributes.k8s.node.role.control_plane"
        for p in bd.REPORT_PANELS:  # a dashboard filter, so no query fails without the field
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

    def test_workload_ratio_table(self):
        p = report_panels()[-1]
        self.assertTrue(p["title"].startswith("Workloads vs. limit ≤ 2× request"))
        self.assertEqual(bd.table_columns(p["esql"])[:7],
                         ["Cluster", "Namespace", "Project", "Category", "Kind", "Workload", "Status"])
        q = p["esql"]
        self.assertIn("cpu_ratio > 2,", q)
        self.assertIn("mem_ratio > 2,", q)
        self.assertIn('"CronJob"', q)
        self.assertIn("| EVAL pname = project", q)  # names patched in like the other report tables
        self.assertIn("@timestamp >= TO_DATETIME(?_tend) - 10 minutes", q)

    def test_every_dashboard_leaves_out_control_plane_nodes(self):
        for d in bd.saved_objects(bd.DEFAULT_INDEX)[1:]:
            search = json.loads(d["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
            self.assertEqual([(f["meta"]["negate"], f["query"]) for f in search["filter"]],
                             [(True, {"match_phrase": {bd.CP_FIELD[0]: "true"}})], d["id"])

    def test_every_project_column_shows_names(self):
        bd.PROJECT_NAMES["p-bbjk7"] = "payments"
        self.rebuild()
        shown = [p for p in bd.REPORT_PANELS if p["esql"] and "Project = pname" in p["esql"]
                 or p["esql"] and "CONCAT(cluster, \" / \", pname)" in p["esql"]]
        self.assertEqual({p["board"] for p in shown}, {"projects", "owner", "limits"})
        for p in report_panels():
            if p["esql"]:
                self.assertIn('project == "p-bbjk7", "payments"', p["esql"], p["title"])
            if p["kind"] == "table" and "Project" in bd.table_columns(p["esql"]):
                self.assertIn("Project = pname", p["esql"], p["title"])

    def test_split_files_stay_small(self):
        # one dashboard per file stays under the 1 MiB request limit of a default ingress-nginx with 200 named
        # Projects (--rancher-projects adds ~2 KiB per Project to the capacity dashboard)
        bd.PROJECT_NAMES.update({f"p-{i:05d}": f"project-name-number-{i}" for i in range(200)})
        self.rebuild()
        data_view, *dashboards = bd.saved_objects(bd.DEFAULT_INDEX)
        for d in dashboards:
            size = len(json.dumps(data_view)) + len(json.dumps(d, separators=(",", ":")))
            self.assertLess(size, 900 * 1024, d["attributes"]["title"])

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
        queries = [p["esql"] for p in report_panels() if p["esql"]]
        self.assertTrue(all(q.count("LOOKUP JOIN") == 1 for q in queries))
        for q in queries:  # the join comes after the Project ID column exists and before the name is used
            self.assertLess(q.index("project = COALESCE("), q.index("LOOKUP JOIN"))
            self.assertLess(q.index("LOOKUP JOIN"), q.index("EVAL category"))
        self.check_dashboards()
        self.without_project()
        self.assertNotIn("LOOKUP JOIN", self.generated())

    def test_tiles_share_one_query_per_row(self):
        grids = [p for p in bd.REPORT_PANELS if p["kind"] == "metric_grid"]
        self.assertEqual([(p["board"], p["tiles"]) for p in grids],
                         [("capacity", 6), ("capacity", 2), ("capacity", 9), ("owner", 6), ("owner", 7),
                          ("fit", 5)])
        self.assertNotIn("metric", {p["kind"] for p in bd.REPORT_PANELS})
        for p in grids:
            self.assertTrue(p["esql"].endswith("| SORT ord\n| KEEP label, value" + (", max" if p["bar"] else "")
                                               + f"\n| RENAME value AS `{p['subtitle']}`"))
            self.assertEqual(p["esql"].count("TO_DOUBLE(v"), p["tiles"])
        vis = bd.lens_attributes(grids[1], bd.DEFAULT_INDEX)["state"]["visualization"]
        self.assertEqual((vis["breakdownByAccessor"], vis["maxAccessor"], vis["maxCols"]), ("label", "max", 2))

    def test_owner_board(self):
        owner = bd.board_panels("owner")
        self.assertEqual([p["kind"] for p in owner],
                         ["markdown", "metric_grid", "metric_grid", "bar", "bar", "line", "line", "table", "table"])
        for p in owner[1:]:  # namespaces of Projects only: node rows (allocatable) are left out
            self.assertIn('| WHERE namespace != ""', p["esql"], p["title"])
        clusters = next(p for p in owner if p["title"].startswith("Clusters"))
        self.assertEqual(bd.table_columns(clusters["esql"])[:3], ["Cluster", "Project", "NS"])
        self.assertIn("Project ID", bd.table_columns(clusters["esql"]))
        line = bd.lens_attributes(owner[5], bd.DEFAULT_INDEX)["state"]["visualization"]["layers"][0]
        self.assertEqual(line["seriesType"], "line")
        dashboard = next(d for d in bd.saved_objects(bd.DEFAULT_INDEX) if d["id"] == bd.OWNER_ID)
        controls = json.loads(dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        self.assertEqual([(cid, c["order"]) for cid, c in controls.items()],
                         [("ctrl-project-name", 0), ("ctrl-cluster", 1), ("ctrl-namespace", 2)])
        project = controls["ctrl-project-name"]["explicitInput"]
        self.assertEqual((project["controlType"], project["variableName"], project["selectedOptions"]),
                         ("VALUES_FROM_QUERY", "project", ["(all projects)"]))
        self.assertEqual(project["esqlQuery"], bd.OWNER_CONTROL[0])
        self.assertTrue(project["esqlQuery"].endswith("| MV_EXPAND pname\n| KEEP pname"))
        self.assertIn("| EVAL pname = project", project["esqlQuery"])  # the same label as the panels
        for p in owner[1:]:  # every panel filters on the picked Project's label
            self.assertIn('(?project == "(all projects)" OR pname == ?project)', p["esql"], p["title"])
        self.assertEqual([r["name"] for r in dashboard["references"]],
                         ["controlGroup_ctrl-cluster:optionsListDataView",
                          "controlGroup_ctrl-namespace:optionsListDataView", bd.CP_FILTER_REF])
        for p in owner[1:]:  # the picked Project is kept before any aggregation, so one Project is aggregated
            q = p["esql"]
            self.assertLess(q.index('| WHERE ?project == "(all projects)" OR pname == ?project'), q.index("| STATS"))
        # names in the control in every variant that has them
        bd.LOOKUP_INDEX[0] = bd.DEFAULT_LOOKUP_INDEX
        self.rebuild()
        self.assertIn("| LOOKUP JOIN rancher-projects ON project\n| EVAL pname = COALESCE(project_name, project)",
                      bd.OWNER_CONTROL[0])
        # the name comes from the join: no early filter, only the late one
        self.assertNotIn("OR pname == ?project\n", bd.board_panels("owner")[1]["esql"])
        bd.LOOKUP_INDEX[0], bd.NAME_FIELD[0] = None, bd.DEFAULT_NAME_FIELD
        self.rebuild()
        self.assertIn("| STATS project_name = MAX(resource.attributes.rancher.project.name) "
                      "BY project = resource.attributes.rancher.project.id", bd.OWNER_CONTROL[0])
        self.assertIn("| EVAL pname = COALESCE(project_name, project)", bd.OWNER_CONTROL[0])
        self.without_project()
        self.assertIsNone(bd.OWNER_CONTROL[0])
        self.assertEqual(dashboard["attributes"]["timeFrom"], "now-1h")

    def test_fit_board(self):
        fit = bd.board_panels("fit")
        variables = {name for name, *_ in bd.FIT_CONTROLS}
        for p in fit:
            if p["esql"]:  # every query reads every control and none names a Project
                self.assertEqual(set(re.findall(r"\?([a-z]+)\b", p["esql"])), variables, p["title"])
                self.assertNotIn("rancher.project", p["esql"])
        for name, _, values, default in bd.FIT_CONTROLS:
            self.assertIn(default, values)
        dashboard = next(d for d in bd.saved_objects(bd.DEFAULT_INDEX) if d["id"] == bd.FIT_ID)
        self.assertEqual(dashboard["attributes"]["timeFrom"], "now-24h")
        controls = json.loads(dashboard["attributes"]["controlGroupInput"]["panelsJSON"])
        self.assertEqual(list(controls), ["ctrl-cluster"] + [f"ctrl-{n}" for n, *_ in bd.FIT_CONTROLS])
        esql = controls["ctrl-replicas"]
        self.assertEqual(esql["type"], "esqlControl")
        self.assertEqual({k: esql["explicitInput"][k] for k in ("variableName", "variableType", "controlType",
                                                                "selectedOptions")},
                         {"variableName": "replicas", "variableType": "values", "controlType": "STATIC_VALUES",
                          "selectedOptions": ["3"]})
        self.assertEqual([r["name"] for r in dashboard["references"]],
                         ["controlGroup_ctrl-cluster:optionsListDataView", bd.CP_FILTER_REF])
        verdict = next(p for p in fit if p["title"].startswith("Does it fit"))["esql"]
        self.assertIn('TOP(a_cpu, 2, "desc")', verdict)
        self.assertIn("fails == 1, MV_MAX(top_fit)", verdict)


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


PROJECTS = {"items": [{"metadata": {"name": "p-bbjk7", "namespace": "c-m-1"}, "spec": {"displayName": "payments"}},
                      {"metadata": {"name": "p-9z9dv", "namespace": "local"}, "spec": {"displayName": "System"}}]}


class PublishProjectNamesTest(unittest.TestCase):
    DOCS = ppn.documents(PROJECTS)

    def test_documents_use_the_dashboard_columns(self):
        self.assertEqual(self.DOCS[0], {"project": "p-9z9dv", "project_name": "System", "cluster_id": "local",
                                        "resource.attributes.rancher.project.id": "p-9z9dv"})
        self.assertEqual([d["project"] for d in self.DOCS], ["p-9z9dv", "p-bbjk7"])
        # the report's join columns, the metrics field, and the same names as the generator reads
        self.assertEqual((ppn.KEY, ppn.NAME, ppn.METRICS_KEY), (bd.LOOKUP_KEY, bd.LOOKUP_NAME, bd.F["project"]))
        self.assertEqual(ppn.DEFAULT_LOOKUP_INDEX, bd.DEFAULT_LOOKUP_INDEX)
        self.assertEqual({d["project"]: d["project_name"] for d in self.DOCS}, bd.rancher_project_names(PROJECTS))
        self.assertEqual(set(ppn.INDEX_BODY["mappings"]["properties"]), set(ppn.FIELDS))
        self.assertEqual(ppn.INDEX_BODY["settings"]["index"]["mode"], "lookup")

    def test_kibana_proxy(self):
        es = ppn.Elastic("https://kibana.example/", kibana=True)
        method, url = es.target("GET", "/rancher-projects/_settings")
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://kibana.example/api/console/proxy?path=%2Francher-projects%2F_settings&method=GET")
        self.assertEqual(ppn.Elastic("https://es:9200").target("PUT", "/x"), ("PUT", "https://es:9200/x"))

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

    def test_devtools_text(self):
        text = ppn.devtools("rancher-projects", self.DOCS)
        requests = [l for l in text.splitlines() if re.match(r"(GET|PUT|POST) ", l)]
        self.assertEqual(requests, ["PUT rancher-projects", "PUT rancher-projects/_mapping",
                                    "POST rancher-projects/_bulk?refresh=true",
                                    "POST rancher-projects/_delete_by_query?refresh=true", "GET rancher-projects/_count"])
        bulk = text.split("POST rancher-projects/_bulk?refresh=true\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual([json.loads(l) for l in bulk[1::2]], self.DOCS)

    def test_publish_keeps_an_existing_lookup_index(self):
        es = FakeElastic("lookup")
        ppn.publish(es, "rancher-projects", self.DOCS)
        self.assertEqual([(m, p) for m, p, _, _ in es.calls if m == "PUT"], [("PUT", "/rancher-projects/_mapping")])

    def test_publish_refuses_an_ordinary_index(self):
        es = FakeElastic(None)
        with self.assertRaises(SystemExit):
            ppn.publish(es, "rancher-projects", self.DOCS)
        self.assertEqual(len(es.calls), 1)


if __name__ == "__main__":
    unittest.main()
