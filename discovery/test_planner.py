import json
import os
import tempfile
import unittest

import planner

CLUSTERS = [
    {"metadata": {"name": "local"}, "spec": {"displayName": "local"},
     "status": {"allocatable": {"cpu": "2", "memory": "8Gi"}, "requested": {"cpu": "1010m", "memory": "1536Mi"},
                "nodeCount": 1}},
    {"metadata": {"name": "c-m-1"}, "spec": {"displayName": "prod-01"},
     "status": {"allocatable": {"cpu": "10", "memory": "40Gi"}, "requested": {"cpu": "2", "memory": "4Gi"},
                "nodeCount": 2}},
]
PROJECTS = [
    {"metadata": {"name": "p-aaaaa", "namespace": "c-m-1"},
     "spec": {"clusterName": "c-m-1", "displayName": "payments",
              "resourceQuota": {"limit": {"requestsCpu": "4000m", "requestsMemory": "16Gi", "limitsMemory": "16Gi"},
                                "usedLimit": {"requestsCpu": "1500m", "requestsMemory": "2Gi"}}}},
    {"metadata": {"name": "p-bbbbb", "namespace": "local"}, "spec": {"clusterName": "local", "displayName": "crm"}},
]


def node(cluster, name, cpu, mem, cordoned=False):
    return {"metadata": {"name": f"m-{name}", "namespace": cluster},
            "spec": {"internalNodeSpec": {"unschedulable": cordoned}},
            "status": {"nodeName": name, "internalNodeStatus": {"allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"}}}}


# prod-01: three schedulable nodes (4+4+2 CPU, 16+16+8 GiB = the 10 CPU / 40 GiB of CLUSTERS) and one cordoned
NODES = [node("c-m-1", "n1", 4, 16), node("c-m-1", "n2", 4, 16), node("c-m-1", "n3", 2, 8),
         node("c-m-1", "n4", 8, 32, cordoned=True), node("local", "cra-report", 2, 8)]


def inventory(nodes=NODES):
    return planner.parse_inventory(CLUSTERS, PROJECTS, nodes)


def plan_with(**projects):
    raw = planner.empty_state()
    raw["clusters"] = {"c-m-1": {"env": "prod", "platform": "onprem"}}
    raw["projects"] = projects
    return planner.validate_state(raw)


class Inventory(unittest.TestCase):
    def test_parse(self):
        inv = inventory()
        prod = next(c for c in inv["clusters"] if c["id"] == "c-m-1")
        self.assertEqual((prod["name"], prod["nodes"], prod["alloc_cpu"], prod["alloc_mem_gib"]), ("prod-01", 2, 10, 40))
        pay = next(p for p in inv["projects"] if p["name"] == "payments")
        self.assertEqual((pay["cluster_id"], pay["quota_cpu"], pay["quota_mem_gib"], pay["used_cpu"]),
                         ("c-m-1", 4, 16, 1.5))
        crm = next(p for p in inv["projects"] if p["name"] == "crm")
        self.assertIsNone(crm["quota_cpu"])

    def test_report_requests_only_for_the_scanned_cluster(self):
        report = {"projects": [{"project_id": "local:p-bbbbb", "cpu_requests": 0.35, "mem_requests_gib": 0.5}]}
        inv = planner.add_report_requests(inventory(), report)
        crm = next(p for p in inv["projects"] if p["name"] == "crm")
        pay = next(p for p in inv["projects"] if p["name"] == "payments")
        self.assertEqual((crm["requests_cpu"], crm["requests_mem_gib"]), (0.35, 0.5))
        self.assertIsNone(pay["requests_cpu"])


class Validate(unittest.TestCase):
    def test_defaults_are_valid(self):
        plan = planner.validate_state(planner.empty_state())
        self.assertIn("prod", plan["settings"]["envs"])
        self.assertEqual(plan["settings"]["envs"]["prod"]["mem_limit_factor"], 1.0)

    def test_rejects_bad_input(self):
        bad = [
            {"projects": {"payments": {"allocations": {"c-m-1": {"cpu": -1}}}}},
            {"projects": {"payments": {"monthly_budget": "lots"}}},
            {"projects": {"<script>": {}}},
            {"clusters": {"c-m-1": {"env": "nowhere"}}},
            {"projects": {"payments": {"cost_center": "a\nb"}}},
            {"projects": {"payments": {"allocations": {"c-m-1": {"cpu": float("nan")}}}}},
        ]
        for extra in bad:
            raw = planner.empty_state()
            raw.update(extra)
            with self.subTest(extra=extra), self.assertRaises(planner.PlanError):
                planner.validate_state(raw)

    def test_budget_may_be_empty(self):
        plan = plan_with(payments={"monthly_budget": None, "allocations": {}})
        self.assertIsNone(plan["projects"]["payments"]["monthly_budget"])


class Store(unittest.TestCase):
    def test_versioning_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            store = planner.PlanStore(d)
            self.assertEqual(store.load()["version"], 0)
            saved = store.save(planner.empty_state(), 0)
            self.assertEqual(saved["version"], 1)
            with self.assertRaises(planner.PlanConflict):
                store.save(planner.empty_state(), 0)  # someone saved in between
            self.assertEqual(store.save(planner.empty_state(), 1)["version"], 2)
            self.assertEqual(len(os.listdir(os.path.join(d, planner.HISTORY_DIR))), 2)

    def test_old_plan_file_gets_new_defaults_on_load(self):
        # a plan saved before node failures / platform reserve existed must still evaluate
        with tempfile.TemporaryDirectory() as d:
            store = planner.PlanStore(d)
            old = planner.empty_state()
            for e in old["settings"]["envs"].values():
                e.pop("node_failures")
            old.update(version=7, updated_at="2026-09-26T10:00:00+00:00",
                       clusters={"c-m-1": {"env": "prod", "platform": "onprem"}})
            with open(store.path, "w") as f:
                json.dump(old, f)
            plan = store.load()
            self.assertEqual((plan["version"], plan["settings"]["envs"]["prod"]["node_failures"]), (7, 1))
            self.assertIsNone(plan["clusters"]["c-m-1"]["platform_cpu"])
            planner.evaluate(plan, inventory())  # no KeyError

    def test_invalid_plan_is_not_written(self):
        with tempfile.TemporaryDirectory() as d:
            store = planner.PlanStore(d)
            raw = planner.empty_state()
            raw["projects"] = {"payments": {"monthly_budget": -5}}
            with self.assertRaises(planner.PlanError):
                store.save(raw, 0)
            self.assertFalse(os.path.exists(store.path))


class Evaluate(unittest.TestCase):
    """prod-01 in prod: lose the largest node (4 CPU / 16 GiB), platform reserve 1 CPU / 4 GiB.
    CPU limit = min(10 - 4 - 1, 80 % x (10 - 1)) = min(5, 7.2) = 5; memory = min(40 - 16 - 4, 80 % x 36) = 20."""

    def plan(self, env="prod", reserve=(1, 4), **projects):
        raw = planner.empty_state()
        raw["clusters"] = {"c-m-1": {"env": env, "platform": "onprem"}}
        if reserve:
            raw["clusters"]["c-m-1"].update(platform_cpu=reserve[0], platform_mem_gib=reserve[1])
        raw["projects"] = projects
        return planner.validate_state(raw)

    def test_node_failure_rule_sets_the_limit(self):
        plan = self.plan(payments={"monthly_budget": 200, "allocations": {"c-m-1": {"cpu": 4, "memory_gib": 16}}})
        ev = planner.evaluate(plan, inventory())
        row = ev["clusters"]["c-m-1"]
        self.assertEqual(ev["projects"]["payments"]["cost"], 200)  # 4 * 25 + 16 * 6.25
        self.assertEqual((row["failover_cpu"], row["reserve_cpu"], row["limit_cpu"], row["binding_cpu"]),
                         (4, 1, 5, "failures"))
        self.assertEqual((row["failover_mem"], row["limit_mem"]), (16, 20))
        self.assertEqual(row["node_count"], 3)  # the cordoned node doesn't count
        self.assertEqual(row["cpu_pct"], 80)  # 4 of the 5 CPU limit
        self.assertEqual(ev["issues"], [])

    def test_over_the_limit_and_over_budget(self):
        plan = self.plan(payments={"monthly_budget": 100, "allocations": {"c-m-1": {"cpu": 6, "memory_gib": 8}}})
        texts = [i["text"] for i in planner.evaluate(plan, inventory())["issues"]]
        self.assertTrue(any("over its budget" in t for t in texts), texts)
        self.assertIn("prod-01 (prod): planned CPU quota 6 is above its limit for projects of 5 (allocatable minus "
                      "its 1 largest node(s) and the platform reserve)", texts)

    def test_percentage_rule_without_node_failures(self):
        plan = self.plan(env="test", payments={"allocations": {"c-m-1": {"cpu": 9.5, "memory_gib": 1}}})
        ev = planner.evaluate(plan, inventory())
        row = ev["clusters"]["c-m-1"]
        self.assertEqual((row["node_failures"], row["limit_cpu"], row["binding_cpu"]), (0, 9, "pct"))
        self.assertTrue(any("100 % of allocatable minus the platform reserve" in i["text"] for i in ev["issues"]))

    def test_platform_reserve_missing(self):
        plan = self.plan(reserve=None, payments={"allocations": {"c-m-1": {"cpu": 1, "memory_gib": 1}}})
        ev = planner.evaluate(plan, inventory())
        self.assertEqual(ev["clusters"]["c-m-1"]["reserve_source"], "none")
        self.assertTrue(any("platform reserve not set" in i["text"] for i in ev["issues"]))

    def test_platform_reserve_measured_on_the_scanned_cluster(self):
        report = {"clusters": [{"cluster": "local"}],
                  "projects": [{"project_id": "local:p-sys", "category": "system", "cpu_requests": 0.4,
                                "mem_requests_gib": 1.5},
                               {"project_id": "", "category": "system", "cpu_requests": 0.1, "mem_requests_gib": 0.5},
                               {"project_id": "local:p-bbbbb", "category": "tenant", "cpu_requests": 1,
                                "mem_requests_gib": 1}]}
        inv = planner.add_report_requests(inventory(), report)
        local = next(c for c in inv["clusters"] if c["id"] == "local")
        prod = next(c for c in inv["clusters"] if c["id"] == "c-m-1")
        self.assertEqual((local["platform_measured_cpu"], local["platform_measured_mem_gib"]), (0.5, 2))
        self.assertIsNone(prod["platform_measured_cpu"])
        raw = planner.empty_state()
        raw["clusters"] = {"local": {"env": "test", "platform": "onprem"}}
        row = planner.evaluate(planner.validate_state(raw), inv)["clusters"]["local"]
        self.assertEqual((row["reserve_source"], row["limit_cpu"]), ("measured", 1.5))  # 100 % x (2 - 0.5)

    def test_single_node_prod_cluster(self):
        raw = planner.empty_state()
        raw["clusters"] = {"local": {"env": "prod", "platform": "onprem", "platform_cpu": 0, "platform_mem_gib": 0}}
        raw["projects"] = {"crm": {"allocations": {"local": {"cpu": 0.5, "memory_gib": 1}}}}
        ev = planner.evaluate(planner.validate_state(raw), inventory())
        self.assertEqual(ev["clusters"]["local"]["limit_cpu"], 0)
        self.assertTrue(any("1 schedulable node(s) can't tolerate 1 node failure(s)" in i["text"] for i in ev["issues"]))

    def test_node_sizes_unknown_still_checks_the_percentage(self):
        plan = self.plan(payments={"allocations": {"c-m-1": {"cpu": 9, "memory_gib": 1}}})
        texts = " ".join(i["text"] for i in planner.evaluate(plan, inventory(nodes=[]))["issues"])
        self.assertIn("Rancher reports no node sizes", texts)
        self.assertIn("above its limit for projects of 7.2 (80 % of allocatable", texts)

    def test_cluster_without_env_or_platform(self):
        raw = planner.empty_state()
        raw["projects"] = {"crm": {"allocations": {"local": {"cpu": 1, "memory_gib": 1}}}}
        ev = planner.evaluate(planner.validate_state(raw), inventory())
        texts = " ".join(i["text"] for i in ev["issues"])
        self.assertIn("no platform set for local", texts)
        self.assertIn("local: no environment set", texts)
        self.assertEqual(ev["projects"]["crm"]["cost"], 0)

    def test_project_missing_in_rancher(self):
        plan = self.plan(newstream={"allocations": {"c-m-1": {"cpu": 1, "memory_gib": 1}}})
        texts = " ".join(i["text"] for i in planner.evaluate(plan, inventory())["issues"])
        self.assertIn("no Rancher Project of that name on prod-01", texts)

    def test_old_plans_get_the_default_node_failures(self):
        raw = planner.empty_state()
        for e in raw["settings"]["envs"].values():
            e.pop("node_failures")
        plan = planner.validate_state(raw)
        self.assertEqual(plan["settings"]["envs"]["prod"]["node_failures"], 1)
        self.assertEqual(plan["settings"]["envs"]["dev"]["node_failures"], 0)


class Export(unittest.TestCase):
    def test_allocation_file_format(self):
        raw = planner.empty_state()
        raw["clusters"] = {"c-m-1": {"env": "test", "platform": "onprem"}}
        raw["projects"] = {"payments": {"cost_center": "CC-1234", "owners": ["lead@corp"], "monthly_budget": 6000,
                                        "allocations": {"c-m-1": {"cpu": 2.5, "memory_gib": 8},
                                                        "local": {"cpu": 0, "memory_gib": 0}}},
                           "crm": {"allocations": {}}}
        out = planner.export_yaml(planner.validate_state(raw), inventory())
        self.assertIn("# allocations/projects/payments.yaml\nproject: \"payments\"\ncostCenter: \"CC-1234\"\n", out)
        self.assertIn('owners: ["lead@corp"]\nbudget:\n  currency: "EUR"\n  monthly: 6000\n', out)
        self.assertIn('  - cluster: "prod-01"\n    env: "test"\n    quota:\n      requests.cpu: "2.5"\n'
                      '      requests.memory: 8Gi\n      limits.memory: 16Gi\n', out)  # test: limit = 2x request
        self.assertNotIn('cluster: "local"', out)  # zero allocations are left out
        self.assertIn("project: \"crm\"\nallocations: []\n", out)
        self.assertEqual(out.count("\n---\n") + out.startswith("---\n"), 2)


if __name__ == "__main__":
    unittest.main()
