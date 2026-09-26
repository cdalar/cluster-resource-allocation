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


def inventory():
    return planner.parse_inventory(CLUSTERS, PROJECTS)


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

    def test_invalid_plan_is_not_written(self):
        with tempfile.TemporaryDirectory() as d:
            store = planner.PlanStore(d)
            raw = planner.empty_state()
            raw["projects"] = {"payments": {"monthly_budget": -5}}
            with self.assertRaises(planner.PlanError):
                store.save(raw, 0)
            self.assertFalse(os.path.exists(store.path))


class Evaluate(unittest.TestCase):
    def test_within_budget_and_headroom(self):
        # 4 CPU * 25 + 16 GiB * 6.25 = 200
        plan = plan_with(payments={"monthly_budget": 200, "allocations": {"c-m-1": {"cpu": 4, "memory_gib": 16}}})
        ev = planner.evaluate(plan, inventory())
        self.assertEqual(ev["projects"]["payments"]["cost"], 200)
        self.assertEqual(ev["clusters"]["c-m-1"]["cpu_pct"], 40)
        self.assertEqual(ev["issues"], [])

    def test_over_budget_and_over_headroom(self):
        plan = plan_with(payments={"monthly_budget": 100, "allocations": {"c-m-1": {"cpu": 9, "memory_gib": 8}}})
        texts = [i["text"] for i in planner.evaluate(plan, inventory())["issues"]]
        self.assertTrue(any("over its budget" in t for t in texts), texts)
        self.assertTrue(any("90 % of allocatable, above the 80 %" in t for t in texts), texts)

    def test_cluster_without_env_or_platform(self):
        plan = plan_with(crm={"allocations": {"local": {"cpu": 1, "memory_gib": 1}}})
        ev = planner.evaluate(plan, inventory())
        texts = " ".join(i["text"] for i in ev["issues"])
        self.assertIn("no platform set for local", texts)
        self.assertIn("local: no environment set", texts)
        self.assertEqual(ev["projects"]["crm"]["cost"], 0)

    def test_project_missing_in_rancher(self):
        plan = plan_with(newstream={"allocations": {"c-m-1": {"cpu": 1, "memory_gib": 1}}})
        texts = " ".join(i["text"] for i in planner.evaluate(plan, inventory())["issues"])
        self.assertIn("no Rancher Project of that name on prod-01", texts)


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
