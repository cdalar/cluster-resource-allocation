import unittest

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
