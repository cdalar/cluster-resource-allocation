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


if __name__ == "__main__":
    unittest.main()
