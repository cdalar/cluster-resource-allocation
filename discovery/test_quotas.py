import csv
import io
import unittest

import discover
import quotas


def ns(name, project="payments", category="tenant", **kw):
    row = {k: 0.0 for k in discover.NS_NUMERIC}
    row.update({k: "" for k in discover.NS_QUOTA_FIELDS})
    row.update(cluster="c1", namespace=name, project=project, project_id=f"c-1:p-{project}" if project else "",
               category=category, quota_rancher=False, limitrange_rancher=False)
    row.update(kw)
    return row


def report(namespaces, **cluster):
    c = {"cluster": "c1", "nodes": 3, "nodes_schedulable": 3, "alloc_cpu_schedulable": 48.0, "alloc_mem_gib_schedulable": 192.0,
         "largest_node_cpu": 16.0, "largest_node_mem_gib": 64.0, "cpu_requests_system": 4.0, "mem_requests_gib_system": 8.0,
         "cpu_requests_excluded_system": 0.0, "mem_requests_excluded_system_gib": 0.0,
         "cpu_for_projects_n1": 28.0, "mem_gib_for_projects_n1": 120.0, "prometheus_days_of_data": 9.5}
    c.update(cluster)
    return {"collected_at": "2026-10-21T10:00:00+00:00", "settings": {"window": "7d"}, "clusters": [c],
            "namespaces": namespaces, "projects": [], "log": []}


S = quotas.parse_settings({})


class Settings(unittest.TestCase):
    def test_defaults_limits_and_bad_values(self):
        s = quotas.parse_settings({"headroom": ["1.5"], "max_pct": ["999"], "pods_factor": ["x"], "include_system": ["1"]})
        self.assertEqual((s["headroom"], s["max_pct"], s["pods_factor"], s["include_system"]), (1.5, 300.0, 2.0, True))
        self.assertEqual(quotas.parse_settings({"headroom": "nan"})["headroom"], 1.3)
        self.assertFalse(quotas.parse_settings({})["include_system"])


class Proposal(unittest.TestCase):
    def test_basis_is_the_largest_of_now_peak_and_hpa(self):
        r = quotas.evaluate_namespace(ns("api", cpu_requests=1.0, cpu_requests_peak=1.6, cpu_requests_hpa_max=2.0,
                                         mem_requests_gib=4.0, mem_requests_peak_gib=6.0, mem_requests_hpa_max_gib=5.0,
                                         pods_running=3, containers=3, mem_limits_gib=4.0), S, True)
        self.assertAlmostEqual(r["basis_cpu"], 2.0)
        self.assertAlmostEqual(r["basis_mem_gib"], 6.0)
        self.assertEqual(r["proposed"], {"cpu": 2.6, "memory_gib": 8.0, "memory_limit_gib": 8.0, "pods": 10})
        self.assertEqual(r["status"], "ready")
        self.assertEqual({i["code"] for i in r["issues"]}, {"hpa", "peak"})

    def test_rounding_and_minimums(self):
        r = quotas.evaluate_namespace(ns("idle"), S, False)
        self.assertEqual(r["proposed"], {"cpu": 0.1, "memory_gib": 1.0, "memory_limit_gib": 1.0, "pods": 10})
        self.assertIn("idle", {i["code"] for i in r["issues"]})
        r = quotas.evaluate_namespace(ns("x", cpu_requests=0.123, mem_requests_gib=1.01, pods_running=11), S, False)
        self.assertEqual((r["proposed"]["cpu"], r["proposed"]["memory_gib"], r["proposed"]["pods"]), (0.2, 2.0, 30))

    def test_defaults_add_to_the_basis_and_need_the_limitrange(self):
        r = quotas.evaluate_namespace(ns("legacy", cpu_requests=1.0, mem_requests_gib=1.0, pods_running=2, containers=4,
                                         containers_no_cpu_request=2, containers_no_mem_request=2, containers_no_mem_limit=3),
                                      S, False)
        self.assertAlmostEqual(r["basis_cpu"], 1.2)       # 2 x 0.1 default
        self.assertAlmostEqual(r["basis_mem_gib"], 1.5)   # 2 x 0.25 default
        self.assertEqual(r["status"], "needs_defaults")
        text = next(i["text"] for i in r["issues"] if i["code"] == "needs_limitrange")
        self.assertIn("2 without a CPU request", text)
        self.assertIn("3 without a memory limit", text)
        # with a LimitRange in the namespace its defaults are used and nothing is missing
        r = quotas.evaluate_namespace(ns("legacy", cpu_requests=1.0, mem_requests_gib=1.0, pods_running=2, containers=4,
                                         containers_no_cpu_request=2, containers_no_mem_request=2, containers_no_mem_limit=3,
                                         limitrange_names="defaults", lr_default_cpu_request=0.5, lr_default_mem_request_gib=1.0,
                                         lr_default_mem_limit_gib=1.0), S, False)
        self.assertAlmostEqual(r["basis_cpu"], 2.0)
        self.assertEqual(r["status"], "ready")

    def test_memory_limit_quota_covers_todays_limits(self):
        r = quotas.evaluate_namespace(ns("over", cpu_requests=1, mem_requests_gib=2.0, mem_limits_gib=20.0, pods_running=1, containers=1), S, True)
        self.assertEqual(r["proposed"]["memory_gib"], 3.0)
        self.assertEqual(r["proposed"]["memory_limit_gib"], 26.0)   # 20 x 1.3, not 3 x 1
        self.assertIn("limits_above", {i["code"] for i in r["issues"]})
        s2 = dict(S, mem_limit_factor=2.0)
        r = quotas.evaluate_namespace(ns("fine", cpu_requests=1, mem_requests_gib=2.0, mem_limits_gib=4.0, pods_running=1, containers=1), s2, True)
        self.assertEqual(r["proposed"]["memory_limit_gib"], 6.0)
        self.assertNotIn("limits_above", {i["code"] for i in r["issues"]})

    def test_existing_quota(self):
        base = dict(cpu_requests=3.0, mem_requests_gib=4.0, pods_running=2, containers=2, mem_limits_gib=4.0,
                    quota_names="quota", quota_cpu_hard=2.0, quota_cpu_used=3.0, quota_mem_hard_gib=8.0, quota_mem_used_gib=4.0)
        r = quotas.evaluate_namespace(ns("full", **base), S, True)
        self.assertEqual(r["status"], "blocked")
        self.assertTrue(any("below today's requests" in i["text"] for i in r["issues"]))
        r = quotas.evaluate_namespace(ns("ok", **dict(base, quota_cpu_hard=3.2, quota_rancher=True)), S, True)
        self.assertEqual(r["status"], "has_quota")
        self.assertTrue(any("Rancher Project quota" in i["text"] for i in r["issues"]))
        self.assertTrue(any(i["code"] == "quota_nearly_used" for i in r["issues"]))
        self.assertEqual(r["quota"]["cpu_hard"], 3.2)
        self.assertIsNone(r["quota"]["pods_hard"])

    def test_warnings(self):
        r = quotas.evaluate_namespace(ns("legacy", project="", category="unassigned", cpu_requests=1, mem_requests_gib=1, pods_running=2,
                                         pods_pending=1, pods_besteffort=1, containers=2, containers_defaulted=1,
                                         nolimit_mem_max_now_gib=0.6, nolimit_mem_max_container="app-1/main"), S, False)
        codes = {i["code"] for i in r["issues"]}
        self.assertEqual(codes, {"unassigned", "oom_risk", "pending", "besteffort", "defaulted"})
        self.assertEqual(r["status"], "ready")
        self.assertIn("app-1/main", next(i["text"] for i in r["issues"] if i["code"] == "oom_risk"))
        # a higher default limit removes the OOM risk
        r = quotas.evaluate_namespace(ns("legacy", cpu_requests=1, mem_requests_gib=1, pods_running=2, containers=2,
                                         nolimit_mem_max_now_gib=0.6), dict(S, default_mem_limit_gib=1.0), False)
        self.assertNotIn("oom_risk", {i["code"] for i in r["issues"]})

    def test_usage_column_follows_prometheus(self):
        row = ns("a", cpu_usage_now=0.5, cpu_usage_p95=0.9, mem_usage_now_gib=1.0, mem_usage_p95_gib=2.0)
        self.assertEqual(quotas.evaluate_namespace(row, S, True)["cpu_usage"], 0.9)
        self.assertEqual(quotas.evaluate_namespace(row, S, False)["mem_usage_gib"], 1.0)


class View(unittest.TestCase):
    def rows(self):
        return [
            ns("payments-api", cpu_requests=4.0, mem_requests_gib=8.0, mem_limits_gib=8.0, pods_running=4, containers=4),
            ns("payments-worker", cpu_requests=2.0, mem_requests_gib=4.0, mem_limits_gib=4.0, pods_running=2, containers=2,
               containers_no_cpu_request=1, containers_no_mem_request=1),
            ns("crm-web", project="crm", cpu_requests=1.0, mem_requests_gib=2.0, mem_limits_gib=2.0, pods_running=1, containers=1,
               quota_names="default-abcde", quota_rancher=True, quota_cpu_hard=2.0, quota_cpu_used=1.0, quota_mem_hard_gib=4.0,
               quota_mem_used_gib=2.0, limitrange_names="default-xyz", limitrange_rancher=True, lr_default_cpu_request=0.1,
               lr_default_mem_request_gib=0.125, lr_default_mem_limit_gib=0.125),
            ns("legacy", project="", category="unassigned", cpu_requests=0.5, mem_requests_gib=1.0, mem_limits_gib=1.0, pods_running=1, containers=1),
            ns("kube-system", project="System", category="system", cpu_requests=4.0, mem_requests_gib=8.0, pods_running=10, containers=10),
        ]

    def test_view(self):
        v = quotas.evaluate(report(self.rows()), None, S)
        self.assertEqual(v["clusters"], ["c1"])
        self.assertTrue(v["has_prometheus"])
        self.assertEqual([r["namespace"] for r in v["namespaces"]], ["crm-web", "payments-api", "payments-worker", "legacy"])
        self.assertEqual([r["status"] for r in v["namespaces"]], ["has_quota", "ready", "needs_defaults", "ready"])
        projects = {p["project"]: p for p in v["projects"]}
        self.assertEqual(set(projects), {"payments", "crm", "(no project) legacy"})
        pay = projects["payments"]
        self.assertEqual(pay["namespaces"], 2)
        self.assertAlmostEqual(pay["proposed_cpu"], 5.2 + 2.8)   # 4 x 1.3; (2 + 0.1) x 1.3 = 2.73 -> 2.8
        self.assertEqual(pay["statuses"], {"ready": 1, "needs_defaults": 1, "has_quota": 0, "blocked": 0})
        self.assertEqual((projects["crm"]["namespaces_with_quota"], projects["crm"]["quota_cpu_hard"]), (1, 2.0))
        c = v["cluster"]
        # sellable = min(room after N+1 28, 80 % x (48 - 4) = 35.2) = 28
        self.assertEqual((c["cpu"]["room_n1"], c["cpu"]["max_pct_capacity"], c["cpu"]["sellable"], c["cpu"]["binding"]), (28.0, 35.2, 28.0, "n1"))
        self.assertAlmostEqual(c["cpu"]["proposed"], 5.2 + 2.8 + 1.3 + 0.7)
        self.assertEqual(c["cpu"]["sold_now"], 2.0)
        self.assertEqual(c["cpu"]["status"], "green")
        steps = {s["step"]: s for s in v["steps"]}
        self.assertEqual((steps["1.6"]["count"], steps["1.6"]["items"]), (1, ["legacy"]))
        self.assertEqual(steps["2.3"]["items"], ["payments-worker"])
        self.assertEqual(steps["2.3"]["count"], 3)   # namespaces without a LimitRange
        self.assertEqual(steps["4.3"]["count"], 1)
        self.assertEqual(sorted(steps["4.4"]["items"]), ["legacy", "payments-api"])
        self.assertTrue(steps["4.2"]["done"])

    def test_include_system_and_cluster_choice(self):
        v = quotas.evaluate(report(self.rows()), "c1", dict(S, include_system=True))
        self.assertIn("kube-system", [r["namespace"] for r in v["namespaces"]])
        v = quotas.evaluate(report(self.rows()), "nope", S)   # unknown cluster: the first one
        self.assertEqual(v["cluster"]["cluster"], "c1")
        self.assertEqual(quotas.evaluate({"clusters": [], "namespaces": []}, None, S)["cluster"], None)

    def test_sold_vs_sellable_status(self):
        rows = [ns("big", cpu_requests=30.0, mem_requests_gib=10.0, mem_limits_gib=10.0, pods_running=1, containers=1)]
        c = quotas.evaluate(report(rows), None, S)["cluster"]
        self.assertEqual(c["cpu"]["status"], "red")        # 39 of 28
        self.assertEqual(c["mem"]["status"], "green")
        rows = [ns("mid", cpu_requests=16.0, mem_requests_gib=10.0, mem_limits_gib=10.0, pods_running=1, containers=1)]
        self.assertEqual(quotas.evaluate(report(rows), None, S)["cluster"]["cpu"]["status"], "amber")   # 20.8 of 28 = 74 %
        # max % binding when it is below N+1; a report from before the N+1 fields computes the room itself
        c = quotas.evaluate(report(rows, cpu_for_projects_n1=None), None, dict(S, max_pct=50))["cluster"]
        self.assertEqual((c["cpu"]["room_n1"], c["cpu"]["sellable"], c["cpu"]["binding"]), (28.0, 22.0, "max_pct"))

    def test_exports(self):
        v = quotas.evaluate(report(self.rows()), None, S)
        alloc = quotas.export_allocations(v)
        self.assertIn("project: payments\n", alloc)
        self.assertIn("      - name: payments-api\n        cpu: 5.2", alloc)
        self.assertIn("memory: 11Gi", alloc)                      # 8 x 1.3 = 10.4 -> 11
        self.assertIn("# has a quota already", alloc)
        self.assertIn("project: legacy\n", alloc)
        self.assertIn("not in a Rancher Project", alloc)
        self.assertNotIn("memoryLimit", alloc)                    # factor 1: same as memory
        self.assertIn("containerDefault:\n  cpu: 0.1\n  memory: 256Mi", alloc)
        man = quotas.export_manifests(v)
        self.assertIn("Left out, they have a quota already: crm-web (default-abcde)", man)
        self.assertNotIn("namespace: crm-web", man)
        self.assertIn('kind: ResourceQuota\nmetadata:\n  name: allocation\n  namespace: payments-api', man)
        self.assertIn('    requests.cpu: "5.2"\n    requests.memory: 11Gi\n    limits.memory: 11Gi\n    pods: "10"', man)
        self.assertIn('name: allocation-defaults\n  namespace: payments-worker', man)
        self.assertIn('allocation.cluster-resource-allocation/project: "payments"', man)
        self.assertEqual(man.count("kind: LimitRange"), 3)        # every namespace without one
        rows = list(csv.DictReader(io.StringIO(quotas.export_csv(v))))
        self.assertEqual([r["namespace"] for r in rows], ["crm-web", "payments-api", "payments-worker", "legacy"])
        crm = rows[0]
        self.assertEqual((crm["status"], crm["quota_rancher"], crm["quota_cpu_hard"], crm["proposed_cpu"], crm["lr_default_mem_request_gib"]),
                         ("has_quota", "yes", "2", "1.3", "0.125"))
        self.assertIn("Rancher Project quota", crm["issues"])
        self.assertEqual(rows[2]["quota_cpu_hard"], "")

    def test_memory_limit_factor_in_export(self):
        v = quotas.evaluate(report(self.rows()), None, dict(S, mem_limit_factor=2.0))
        self.assertIn("        memory: 11Gi", quotas.export_allocations(v))
        self.assertIn("        memoryLimit: 22Gi", quotas.export_allocations(v))


if __name__ == "__main__":
    unittest.main()
