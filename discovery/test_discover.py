import unittest

import discover

from discover import GIB, aggregate_projects, classify, parse_quantity, pod_effective, NS_NUMERIC
import re


def c(cpu=None, mem=None, lcpu=None, lmem=None, restart=None):
    res = {"requests": {}, "limits": {}}
    if cpu:
        res["requests"]["cpu"] = cpu
    if mem:
        res["requests"]["memory"] = mem
    if lcpu:
        res["limits"]["cpu"] = lcpu
    if lmem:
        res["limits"]["memory"] = lmem
    out = {"name": "x", "resources": res}
    if restart:
        out["restartPolicy"] = restart
    return out


class ParseQuantity(unittest.TestCase):
    def test_values(self):
        cases = {
            "100m": 0.1, "2": 2.0, "0.5": 0.5, "1Gi": GIB, "512Mi": 512 * 2**20, "1G": 1e9,
            "1e3": 1000.0, "250000n": 0.00025, "1k": 1000.0, "128974848": 128974848.0,
        }
        for q, want in cases.items():
            self.assertAlmostEqual(parse_quantity(q), want, msg=q)
        self.assertEqual(parse_quantity(None), 0.0)
        self.assertEqual(parse_quantity(2), 2.0)


class PodEffective(unittest.TestCase):
    def test_sum_of_containers(self):
        spec = {"containers": [c("100m", "128Mi"), c("200m", "64Mi")]}
        self.assertAlmostEqual(pod_effective(spec, "requests", "cpu"), 0.3)
        self.assertAlmostEqual(pod_effective(spec, "requests", "memory"), 192 * 2**20)

    def test_large_init_container_dominates(self):
        spec = {"containers": [c("100m")], "initContainers": [c("1")]}
        self.assertAlmostEqual(pod_effective(spec, "requests", "cpu"), 1.0)

    def test_sidecar_init_is_added(self):
        spec = {"containers": [c("100m")], "initContainers": [c("50m", restart="Always")]}
        self.assertAlmostEqual(pod_effective(spec, "requests", "cpu"), 0.15)

    def test_init_after_sidecar_includes_sidecar(self):
        spec = {"containers": [c("100m")],
                "initContainers": [c("200m", restart="Always"), c("500m")]}
        self.assertAlmostEqual(pod_effective(spec, "requests", "cpu"), 0.7)

    def test_overhead(self):
        spec = {"containers": [c("100m")], "overhead": {"cpu": "250m"}}
        self.assertAlmostEqual(pod_effective(spec, "requests", "cpu"), 0.35)


class Classify(unittest.TestCase):
    def test_categories(self):
        from discover import DEFAULT_SYSTEM_NS_REGEX
        rx = re.compile(DEFAULT_SYSTEM_NS_REGEX)
        self.assertEqual(classify("kube-system", "", False, rx), "system")
        self.assertEqual(classify("cattle-monitoring-system", "", False, rx), "system")
        self.assertEqual(classify("anything", "System", True, rx), "system")
        self.assertEqual(classify("payments-api", "payments", True, rx), "tenant")
        self.assertEqual(classify("default", "", False, rx), "unassigned")

    def test_rancher_bookkeeping_namespaces_are_system(self):
        from discover import DEFAULT_SYSTEM_NS_REGEX
        rx = re.compile(DEFAULT_SYSTEM_NS_REGEX)
        for name in ("local", "p-7678f", "local-p-7678f", "c-m-474nq8vf", "c-m-474nq8vf-p-lcjjj", "user-lvdk5",
                     "u-37ezc6vbwo"):
            self.assertEqual(classify(name, "", False, rx), "system", name)
        self.assertEqual(classify("local-payments", "", False, rx), "unassigned")

    def test_installation_guide_has_the_current_list(self):
        # The guide has a copy-paste value that starts from the built-in list.
        import os
        from discover import DEFAULT_SYSTEM_NS_REGEX
        with open(os.path.join(os.path.dirname(__file__), "..", "guides", "installation.md")) as f:
            guide = f.read()
        self.assertIn(DEFAULT_SYSTEM_NS_REGEX, guide)
        self.assertIn(f"systemNamespaceRegex: '{DEFAULT_SYSTEM_NS_REGEX[:-2]}|", guide)

    def test_chart_default_is_the_built_in_list(self):
        import os
        from discover import DEFAULT_SYSTEM_NS_REGEX
        with open(os.path.join(os.path.dirname(__file__), "..", "charts", "cluster-resource-report", "values.yaml")) as f:
            values = f.read()
        self.assertIn(f"  systemNamespaceRegex: '{DEFAULT_SYSTEM_NS_REGEX}'\n", values)

    def test_platform_projects_are_system(self):
        from discover import DEFAULT_SYSTEM_NS_REGEX
        rx = re.compile(DEFAULT_SYSTEM_NS_REGEX)
        for project in ("System", "Platform", "platform-services", "Shared Platform Team"):
            self.assertEqual(classify("app", project, True, rx), "system", project)
        for project in ("payments", "Default", "p-7678f", "systems-integration"):
            self.assertEqual(classify("app", project, True, rx), "tenant", project)
        custom = re.compile(r"^(system|infra)$", re.IGNORECASE)
        self.assertEqual(classify("app", "Infra", True, rx, custom), "system")
        self.assertEqual(classify("app", "Platform", True, rx, custom), "tenant")

    def test_chart_default_project_regex(self):
        import os
        from discover import DEFAULT_SYSTEM_PROJECT_REGEX
        with open(os.path.join(os.path.dirname(__file__), "..", "charts", "cluster-resource-report", "values.yaml")) as f:
            self.assertIn(f"  systemProjectRegex: '{DEFAULT_SYSTEM_PROJECT_REGEX}'\n", f.read())

    def test_system_and_ingress_keywords(self):
        from discover import DEFAULT_SYSTEM_NS_REGEX
        rx = re.compile(DEFAULT_SYSTEM_NS_REGEX)
        for name in ("longhorn-system", "gatekeeper-system", "cis-operator-system", "app-routing-system",
                     "ingress-nginx", "haproxy-ingress", "my-ingress-controller", "systemd-exporter", "otel",
                     "monitoring", "local-path-storage", "rook-ceph", "openebs", "csi-driver-nfs", "vsphere-csi",
                     "nfs-subdir-external-provisioner", "storage"):
            self.assertEqual(classify(name, "", False, rx), "system", name)
        for name in ("payments-api", "otel-demo", "monitoring-app", "legacy"):
            self.assertEqual(classify(name, "", False, rx), "unassigned", name)


class Aggregate(unittest.TestCase):
    def test_projects_and_efficiency(self):
        def ns(name, pid, cpu_req, cpu_p95):
            r = {k: 0.0 for k in NS_NUMERIC}
            r.update(cluster="c1", category="tenant", project_id=pid, project="payments", namespace=name,
                     cpu_requests=cpu_req, cpu_usage_p95=cpu_p95)
            return r
        out = aggregate_projects([ns("a", "c-1:p-1", 2.0, 0.5), ns("b", "c-1:p-1", 2.0, 0.5)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["namespaces"], 2)
        self.assertAlmostEqual(out[0]["cpu_requests"], 4.0)
        self.assertEqual(out[0]["cpu_request_efficiency_pct"], 25.0)
        self.assertEqual(out[0]["project_key"], "c-1:p-1")

    def test_namespace_without_project_is_its_own_project(self):
        r = {k: 0.0 for k in NS_NUMERIC}
        r.update(cluster="c1", category="unassigned", project_id="", project="", namespace="legacy")
        out = aggregate_projects([r])
        self.assertEqual(out[0]["project_key"], "ns:legacy")
        self.assertEqual(out[0]["project"], "(no project) legacy")


class NodeFailure(unittest.TestCase):
    def test_n_plus_one_capacity_after_platform(self):
        from discover import summarize_cluster
        def node(cpu, mem, taint=None):
            return {"spec": {"taints": [{"effect": taint}] if taint else []},
                    "status": {"allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"}}}
        nodes = [node(4, 16), node(2, 8), node(8, 32, taint="NoSchedule")]  # the tainted one can't run pods
        rows = []
        for cat, cpu, mem in (("system", 0.5, 1.0), ("tenant", 1.0, 2.0)):
            r = {k: 0.0 for k in NS_NUMERIC}
            r.update(category=cat, cpu_requests=cpu, mem_requests_gib=mem)
            rows.append(r)
        c = summarize_cluster("c1", "ctx", nodes, rows, None)
        self.assertEqual((c["largest_node_cpu"], c["largest_node_mem_gib"]), (4, 16))
        self.assertEqual((c["alloc_cpu_n1"], c["alloc_mem_gib_n1"]), (2, 8))  # 6 - 4, 24 - 16
        self.assertEqual((c["cpu_for_projects_n1"], c["mem_gib_for_projects_n1"]), (1.5, 7))  # minus system

    def test_requests_on_excluded_nodes_are_not_on_capacity(self):
        # system pods on a tainted control-plane node (apiserver, etcd, DaemonSets that tolerate everything) are
        # requests, but not on the schedulable nodes: they don't shrink the room for projects or raise "% requested"
        from discover import summarize_cluster
        def node(cpu, mem, taint=None):
            return {"spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": taint}] if taint else []},
                    "status": {"allocatable": {"cpu": str(cpu), "memory": f"{mem}Gi"}}}
        nodes = [node(4, 16), node(4, 16), node(8, 32, taint="NoSchedule")]
        rows = []
        for cat, cpu, mem, cpu_off, mem_off in (("system", 2.0, 4.0, 1.5, 3.0), ("tenant", 1.0, 2.0, 0.0, 0.0)):
            r = {k: 0.0 for k in NS_NUMERIC}
            r.update(category=cat, cpu_requests=cpu, mem_requests_gib=mem,
                     cpu_requests_excluded_nodes=cpu_off, mem_requests_excluded_nodes_gib=mem_off)
            rows.append(r)
        c = summarize_cluster("c1", "ctx", nodes, rows, None)
        self.assertEqual((c["alloc_cpu_schedulable"], c["largest_node_cpu"]), (8, 4))
        self.assertEqual((c["cpu_requests_system"], c["cpu_requests_excluded_system"]), (2.0, 1.5))
        self.assertEqual((c["cpu_for_projects_n1"], c["mem_gib_for_projects_n1"]), (3.5, 15))  # 4 - 0.5, 16 - 1
        self.assertEqual(c["cpu_requests_pct_of_schedulable"], 18.8)  # (3 - 1.5) / 8

    def test_single_node_leaves_nothing(self):
        from discover import summarize_cluster
        c = summarize_cluster("c1", "ctx", [{"status": {"allocatable": {"cpu": "2", "memory": "8Gi"}}}], [], None)
        self.assertEqual((c["alloc_cpu_n1"], c["cpu_for_projects_n1"]), (0, 0))


class RancherLocalSelf(unittest.TestCase):
    def run_collect(self, argv):
        from unittest import mock
        import discover
        args = discover.build_parser().parse_args(argv)
        with mock.patch.object(discover, "load_rancher_projects", return_value=({}, {})) as load, \
                mock.patch.object(discover, "collect_cluster", return_value=([], {"cluster": "x"})):
            discover.collect(args)
        return load

    def test_reads_names_from_the_scanned_cluster(self):
        self.run_collect(["--rancher-local-self"]).assert_called_once_with(None, None)
        self.run_collect(["--rancher-local-self", "--context", "rancher"]).assert_called_once_with("rancher", None)

    def test_not_loaded_without_a_source(self):
        self.run_collect([]).assert_not_called()

    def test_rejects_ambiguous_combinations(self):
        for argv in (["--rancher-local-self", "--context", "a", "--context", "b"],
                     ["--rancher-local-self", "--rancher-local-context", "local"]):
            with self.assertRaises(ValueError):
                self.run_collect(argv)


class NamesConfigMap(unittest.TestCase):
    def test_round_trip_through_the_published_bundle(self):
        import json
        from discover import names_from_configmap
        from publish_rancher_names import build_bundle
        projects = {"c-m-1:p-abcde": "payments"}
        clusters = {"c-m-1": "onprem-test-01"}
        bundle = build_bundle(projects, clusters, "fleet-default", "resource-report", {})
        self.assertEqual(bundle["kind"], "Bundle")
        self.assertEqual(bundle["metadata"]["namespace"], "fleet-default")
        self.assertEqual(bundle["spec"]["targets"], [{"clusterSelector": {}}])
        cm = json.loads(bundle["spec"]["resources"][0]["content"])
        self.assertEqual(cm["metadata"], {"name": "rancher-project-names", "namespace": "resource-report",
                                          "labels": {"app.kubernetes.io/part-of": "cluster-resource-report"}})
        self.assertEqual(names_from_configmap(cm), (projects, clusters))

    def test_bundle_with_targets(self):
        from publish_rancher_names import build_bundle
        targets = [{"clusterName": "a"}, {"clusterGroup": "resource-report"}]
        bundle = build_bundle({}, {}, "fleet-default", "resource-report", {}, targets)
        self.assertEqual(bundle["spec"]["targets"], targets)

    def test_empty_configmap(self):
        from discover import names_from_configmap
        self.assertEqual(names_from_configmap({}), ({}, {}))

    def test_names_used_per_cluster(self):
        from unittest import mock
        import discover
        args = discover.build_parser().parse_args(["--rancher-names-configmap", "resource-report/rancher-project-names"])
        seen = {}
        def fake_collect(ctx, a, projects, clusters):
            seen.update(projects=projects, clusters=clusters)
            return [], {"cluster": "x"}
        with mock.patch.object(discover, "load_names_configmap", return_value=({"c:p-1": "crm"}, {"c": "x"})) as load, \
                mock.patch.object(discover, "collect_cluster", side_effect=fake_collect):
            discover.collect(args)
        load.assert_called_once_with(None, "resource-report/rancher-project-names")
        self.assertEqual(seen, {"projects": {"c:p-1": "crm"}, "clusters": {"c": "x"}})

    def test_bad_reference(self):
        import discover
        args = discover.build_parser().parse_args(["--rancher-names-configmap", "no-namespace"])
        with self.assertRaises(ValueError):
            discover.collect(args)


class PrometheusHistory(unittest.TestCase):
    """The usage history comes from one range query per series, summarized here like Prometheus would."""

    def test_stats_match_prometheus_functions(self):
        import discover
        self.assertEqual(discover.series_stats([]), {"avg": 0.0, "p95": 0.0, "max": 0.0})
        self.assertEqual(discover.series_stats([3.0]), {"avg": 3.0, "p95": 3.0, "max": 3.0})
        st = discover.series_stats([5.0, 1.0, 4.0, 2.0, 3.0])
        # quantile_over_time(0.95) of 1..5: rank 3.8 -> between 4 and 5
        self.assertEqual(st["avg"], 3.0)
        self.assertAlmostEqual(st["p95"], 4.8)
        self.assertEqual(st["max"], 5.0)

    def test_range_split_at_the_point_limit(self):
        import discover
        self.assertEqual(discover.range_chunks(0, 100, 10, 11000), [(0, 100)])
        self.assertEqual(discover.range_chunks(0, 100, 10, 4), [(0, 30), (40, 70), (80, 100)])
        self.assertEqual(discover.range_chunks(1000, 1000, 60, 4), [(1000, 1000)])
        self.assertEqual(discover.range_chunks(0, 25, 10, 2), [(0, 10), (20, 20)])

    def test_range_query_per_namespace(self):
        from unittest import mock
        import discover
        prom = discover.Prometheus(None, service="monitoring/prometheus:9090")
        calls = []

        def fake_api(endpoint, params):
            calls.append((endpoint, params))
            return [{"metric": {"namespace": "a"}, "values": [[params["start"], "1"], [params["end"], "2"]]},
                    {"metric": {"namespace": "b"}, "values": [[params["start"], "0.5"]]}]

        with mock.patch.object(prom, "_api", side_effect=fake_api):
            out = prom.range_by_namespace("sum by (namespace) (x)", 0, 30, 10, max_points=2)
        self.assertEqual([c[0] for c in calls], ["query_range", "query_range"])
        self.assertEqual([(c[1]["start"], c[1]["end"], c[1]["step"]) for c in calls], [(0, 10, 10), (20, 30, 10)])
        self.assertEqual(out, {"a": [1.0, 2.0, 1.0, 2.0], "b": [0.5, 0.5]})
        with mock.patch.object(prom, "_api", side_effect=fake_api) as api:   # never above Prometheus' own limit
            prom.range_by_namespace("x", 0, 20000 * 10, 10, max_points=10**6)
        self.assertEqual(api.call_count, 2)

    def test_columns_from_series(self):
        import discover
        cols = [c for stats in discover.PROM_COLUMNS.values() for _, c in stats]
        self.assertEqual(set(cols), {"cpu_usage_avg", "cpu_usage_p95", "cpu_usage_max", "mem_usage_avg_gib",
                                     "mem_usage_p95_gib", "mem_usage_max_gib", "cpu_requests_peak",
                                     "mem_requests_peak_gib"})
        self.assertTrue(set(cols) <= set(discover.NS_NUMERIC))
        self.assertEqual(set(discover.PROM_COLUMNS), set(discover.PROM_SERIES))

    def test_durations(self):
        import discover
        self.assertEqual(discover.duration_seconds("7d"), 7 * 86400)
        self.assertEqual(discover.duration_seconds("2w"), 14 * 86400)
        self.assertEqual(discover.window_days("12h"), 0.5)
        for bad in ("7", "5 m", "", "1x"):
            with self.assertRaises(ValueError):
                discover.duration_seconds(bad)
        args = discover.build_parser().parse_args(["--window", "7days"])
        with self.assertRaises(ValueError):
            discover.collect(args)


class Parallel(unittest.TestCase):
    def test_results_in_order_and_logs_captured(self):
        import discover
        lines = []
        discover._log_local.sink = lines
        try:
            def work(i):
                discover.log(f"job {i}")
                return i * i
            self.assertEqual(discover.parallel([lambda i=i: work(i) for i in range(5)], limit=2), [0, 1, 4, 9, 16])
        finally:
            discover._log_local.sink = None
        self.assertEqual(sorted(lines), [f"job {i}" for i in range(5)])

    def test_exception_propagates(self):
        import discover

        def boom():
            raise discover.KubectlError("nope")
        with self.assertRaises(discover.KubectlError):
            discover.parallel([lambda: 1, boom])

    def test_clusters_collected_concurrently(self):
        import threading
        from unittest import mock
        import discover
        args = discover.build_parser().parse_args(["--context", "a", "--context", "b", "--context", "c"])
        gate = threading.Barrier(3, timeout=5)   # only passes if all three scans run at the same time

        def fake_collect(ctx, a, projects, clusters):
            gate.wait()
            if ctx == "b":
                raise discover.KubectlError("unreachable")
            return [{"cluster": ctx, "category": "tenant", "project": "", "namespace": "n", "project_id": "",
                     **{k: 0.0 for k in discover.NS_NUMERIC}}], {"cluster": ctx}
        with mock.patch.object(discover, "collect_cluster", side_effect=fake_collect):
            report = discover.collect(args)
        self.assertEqual([c["cluster"] for c in report["clusters"]], ["a", "c"])
        self.assertIn("[b] FAILED: unreachable", report["log"])


class LogCapture(unittest.TestCase):
    def test_collect_captures_only_its_own_thread(self):
        import threading
        import discover
        lines = []
        discover._log_local.sink = lines
        t = threading.Thread(target=discover.log, args=("from another thread",))
        t.start(); t.join()
        discover.log("from this thread")
        discover._log_local.sink = None
        self.assertEqual(lines, ["from this thread"])


if __name__ == "__main__":
    unittest.main()


class QuotaDetails(unittest.TestCase):
    """ResourceQuota, LimitRange and HPA details collected for the namespace quotas page."""

    def test_quota_summary(self):
        from discover import quota_summary
        quotas = [
            {"metadata": {"name": "b-quota"}, "spec": {"hard": {"requests.cpu": "4", "requests.memory": "8Gi", "limits.memory": "16Gi",
                                                               "pods": "50", "count/secrets": "10"}},
             "status": {"used": {"requests.cpu": "1500m", "requests.memory": "2Gi", "limits.memory": "3Gi", "pods": "7"}}},
            {"metadata": {"name": "a-tight", "labels": {"resourcequota.management.cattle.io/default-resource-quota": "true"}},
             "spec": {"hard": {"cpu": "2"}}, "status": {"used": {"cpu": "1"}}},
            {"metadata": {"name": "c-scoped"}, "spec": {"hard": {"pods": "1"}, "scopes": ["BestEffort"]}, "status": {}},
        ]
        s = quota_summary(quotas)
        self.assertEqual(s["quota_names"], "a-tight, b-quota, c-scoped (scoped)")
        self.assertTrue(s["quota_rancher"])
        self.assertEqual((s["quota_cpu_hard"], s["quota_cpu_used"]), (2.0, 1.0))     # the tightest, with its own used
        self.assertEqual((s["quota_mem_hard_gib"], s["quota_mem_used_gib"]), (8.0, 2.0))
        self.assertEqual((s["quota_mem_limit_hard_gib"], s["quota_mem_limit_used_gib"]), (16.0, 3.0))
        self.assertEqual((s["quota_pods_hard"], s["quota_pods_used"]), (50.0, 7.0))   # the scoped quota is not summed
        self.assertEqual(s["quota_cpu_limit_hard"], "")
        empty = quota_summary([], {"field.cattle.io/resourceQuota": "{}"})
        self.assertEqual((empty["quota_names"], empty["quota_rancher"], empty["quota_cpu_hard"]), ("", False, ""))
        self.assertTrue(quota_summary(quotas[:1], {"field.cattle.io/resourceQuota": "{}"})["quota_rancher"])
        self.assertFalse(quota_summary(quotas[:1])["quota_rancher"])

    def test_limitrange_summary(self):
        from discover import limitrange_summary
        lrs = [{"metadata": {"name": "defaults"}, "spec": {"limits": [
                    {"type": "Container", "defaultRequest": {"cpu": "100m", "memory": "256Mi"}, "default": {"memory": "512Mi"},
                     "max": {"cpu": "8", "memory": "32Gi"}},
                    {"type": "Pod", "max": {"cpu": "1"}}]}},
               {"metadata": {"name": "strict"}, "spec": {"limits": [{"type": "Container", "max": {"cpu": "4"}}]}}]
        s = limitrange_summary(lrs, {"field.cattle.io/containerDefaultResourceLimit": "{}"})
        self.assertEqual(s["limitrange_names"], "defaults, strict")
        self.assertTrue(s["limitrange_rancher"])
        self.assertAlmostEqual(s["lr_default_cpu_request"], 0.1)
        self.assertAlmostEqual(s["lr_default_mem_request_gib"], 0.25)
        self.assertEqual(s["lr_default_cpu_limit"], "")
        self.assertAlmostEqual(s["lr_default_mem_limit_gib"], 0.5)
        self.assertEqual((s["lr_max_cpu"], s["lr_max_mem_gib"]), (4.0, 32.0))      # the smallest max wins
        self.assertEqual(limitrange_summary([])["limitrange_names"], "")

    def test_container_defaulted(self):
        from discover import container_defaulted
        lr = {"lr_default_cpu_request": 0.1, "lr_default_mem_request_gib": 0.25}
        self.assertTrue(container_defaulted(c("100m", "1Gi"), lr))
        self.assertTrue(container_defaulted(c("2", "256Mi"), lr))
        self.assertFalse(container_defaulted(c("200m", "1Gi"), lr))
        self.assertFalse(container_defaulted(c(), lr))
        self.assertFalse(container_defaulted(c("100m"), {"lr_default_cpu_request": "", "lr_default_mem_request_gib": ""}))

    def test_hpa_scaleout(self):
        from discover import hpa_scaleout, hpa_targets_pod
        hpa = {"metadata": {"name": "web"}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "maxReplicas": 5}}
        self.assertTrue(hpa_targets_pod(hpa, ("ReplicaSet", "web-7d9f8b6c")))
        self.assertFalse(hpa_targets_pod(hpa, ("ReplicaSet", "web-api-7d9f8b6c")))
        self.assertFalse(hpa_targets_pod(hpa, ("StatefulSet", "web")))
        sts = {"spec": {"scaleTargetRef": {"kind": "StatefulSet", "name": "db"}, "maxReplicas": 3}}
        self.assertTrue(hpa_targets_pod(sts, ("StatefulSet", "db")))
        pods = [(("ReplicaSet", "web-7d9f8b6c"), 0.2, 1.0 * GIB), (("ReplicaSet", "web-7d9f8b6c"), 0.4, 1.0 * GIB),
                (("StatefulSet", "db"), 1.0, 4.0 * GIB), (("", ""), 0.1, 0.0)]
        cpu, mem = hpa_scaleout([hpa, sts], pods)
        self.assertAlmostEqual(cpu, 3 * 0.3 + 2 * 1.0)   # web: 3 more at the average 0.3; db: 2 more at 1.0
        self.assertAlmostEqual(mem, (3 * 1.0 + 2 * 4.0) * GIB)
        self.assertEqual(hpa_scaleout([hpa], [(("StatefulSet", "db"), 1.0, 1.0)]), (0.0, 0.0))   # no pods matched
        self.assertEqual(hpa_scaleout([dict(hpa, spec={"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "maxReplicas": 1})], pods), (0.0, 0.0))

    def test_project_aggregation_of_the_new_fields(self):
        rows = []
        for name, hpa, nolimit in (("a", 1.0, 0.5), ("b", 2.0, 0.7)):
            r = {k: 0.0 for k in NS_NUMERIC}
            r.update(cluster="c", category="tenant", project_id="p", project="P", namespace=name,
                     cpu_requests_hpa_max=hpa, nolimit_mem_max_now_gib=nolimit)
            rows.append(r)
        p = aggregate_projects(rows)[0]
        self.assertEqual((p["cpu_requests_hpa_max"], p["nolimit_mem_max_now_gib"]), (3.0, 0.7))


class CollectCluster(unittest.TestCase):
    """collect_cluster with a fake kubectl: the quota, LimitRange, HPA and snapshot details end up in the rows."""

    def fake_kget(self, context, resource, all_namespaces=True, kubeconfig=None):
        def pod(ns, name, owner, phase="Running", node="n1", containers=None, qos="Burstable"):
            return {"metadata": {"namespace": ns, "name": name, "ownerReferences": [{"kind": owner[0], "name": owner[1]}] if owner else []},
                    "spec": {"nodeName": node, "containers": containers or [c("100m", "256Mi", lmem="256Mi")]},
                    "status": {"phase": phase, "qosClass": qos}}
        data = {
            "nodes": [{"metadata": {"name": "n1"}, "status": {"allocatable": {"cpu": "8", "memory": "32Gi"}}},
                      {"metadata": {"name": "n2"}, "status": {"allocatable": {"cpu": "8", "memory": "32Gi"}}}],
            "namespaces": [
                {"metadata": {"name": "web", "annotations": {"field.cattle.io/projectId": "c-1:p-1"}}},
                {"metadata": {"name": "quoted", "annotations": {"field.cattle.io/projectId": "c-1:p-1", "field.cattle.io/resourceQuota": "{}",
                                                                 "field.cattle.io/containerDefaultResourceLimit": "{}"}}},
                {"metadata": {"name": "kube-system"}}],
            "pods": [
                pod("web", "web-abc12-x1", ("ReplicaSet", "web-abc12"), containers=[c("200m", "512Mi")]),
                pod("web", "web-abc12-x2", ("ReplicaSet", "web-abc12"), containers=[c("200m", "512Mi")]),
                pod("web", "other-1", ("StatefulSet", "other"), containers=[c()], qos="BestEffort"),
                pod("quoted", "app-1", ("ReplicaSet", "app-1a2b3"), containers=[c("100m", "128Mi", lmem="256Mi")]),
                pod("quoted", "done-1", None, phase="Succeeded"),
            ],
            "resourcequotas": [{"metadata": {"namespace": "quoted", "name": "default-zzzzz"},
                                "spec": {"hard": {"requests.cpu": "1", "requests.memory": "2Gi", "limits.memory": "4Gi"}},
                                "status": {"used": {"requests.cpu": "100m", "requests.memory": "128Mi", "limits.memory": "256Mi"}}}],
            "limitranges": [{"metadata": {"namespace": "quoted", "name": "default-yyyyy"},
                             "spec": {"limits": [{"type": "Container", "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                                                  "default": {"memory": "256Mi"}}]}}],
            "horizontalpodautoscalers.autoscaling": [{"metadata": {"namespace": "web", "name": "web"},
                                                      "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "maxReplicas": 6}}],
        }
        return data[resource]

    def fake_kubectl(self, context, args, timeout=120, kubeconfig=None, stdin=None):
        import json
        if args[:2] == ["config", "current-context"]:
            return "test"
        if args == ["get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"]:
            return json.dumps({"items": [
                {"metadata": {"namespace": "web", "name": "web-abc12-x1"}, "containers": [{"name": "x", "usage": {"cpu": "50m", "memory": "100Mi"}}]},
                {"metadata": {"namespace": "web", "name": "other-1"}, "containers": [{"name": "x", "usage": {"cpu": "10m", "memory": "700Mi"}}]},
            ]})
        raise discover.KubectlError("not in the fixture: " + " ".join(args))

    def test_collect_cluster(self):
        from unittest import mock
        args = discover.build_parser().parse_args(["--prometheus", "none"])
        with mock.patch.object(discover, "kget", self.fake_kget), mock.patch.object(discover, "kubectl", self.fake_kubectl):
            ns_rows, cluster = discover.collect_cluster(None, args, {"c-1:p-1": "payments"}, {"c-1": "test-01"})
        rows = {r["namespace"]: r for r in ns_rows}
        web, quoted = rows["web"], rows["quoted"]
        self.assertEqual((cluster["cluster"], web["project"], web["category"]), ("test-01", "payments", "tenant"))
        self.assertAlmostEqual(web["cpu_requests"], 0.4)
        self.assertAlmostEqual(web["cpu_requests_hpa_max"], 0.4 + 4 * 0.2)       # 2 of 6 replicas run
        self.assertAlmostEqual(web["mem_requests_hpa_max_gib"], 1.0 + 4 * 0.5)
        self.assertEqual((web["quota_names"], web["quota_rancher"], web["quota_cpu_hard"], web["limitrange_names"]), ("", False, "", ""))
        self.assertEqual(web["containers_no_mem_limit"], 3)
        self.assertAlmostEqual(web["nolimit_mem_max_now_gib"], 700 / 1024)
        self.assertEqual(web["nolimit_mem_max_container"], "other-1/x")
        self.assertEqual(web["containers_defaulted"], 0)
        self.assertEqual((quoted["quota_names"], quoted["quota_rancher"], quoted["limitrange_rancher"]), ("default-zzzzz", True, True))
        self.assertEqual((quoted["quota_cpu_hard"], quoted["quota_cpu_used"], quoted["quota_mem_hard_gib"], quoted["quota_mem_limit_hard_gib"]),
                         (1.0, 0.1, 2.0, 4.0))
        self.assertEqual((quoted["lr_default_cpu_request"], quoted["lr_default_mem_request_gib"], quoted["lr_default_mem_limit_gib"]),
                         (0.1, 0.125, 0.25))
        self.assertEqual(quoted["containers_defaulted"], 1)                        # 100m / 128Mi = the defaults
        self.assertEqual(quoted["pods_running"], 1)                                # the Succeeded pod is not counted
        self.assertEqual(quoted["nolimit_mem_max_now_gib"], 0.0)
        self.assertEqual(quoted["cpu_requests_hpa_max"], quoted["cpu_requests"])
        # the CSV has the new columns
        import os, tempfile, csv
        with tempfile.TemporaryDirectory() as d:
            discover.write_csv(os.path.join(d, "ns.csv"), discover.NS_FIELDS, ns_rows)
            with open(os.path.join(d, "ns.csv")) as f:
                got = {r["namespace"]: r for r in csv.DictReader(f)}
        self.assertEqual((got["quoted"]["quota_cpu_hard"], got["quoted"]["quota_rancher"], got["web"]["quota_cpu_hard"]), ("1", "True", ""))
