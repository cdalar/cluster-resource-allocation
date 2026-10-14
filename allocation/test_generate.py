import contextlib
import io
import json
import os
import shutil
import tempfile
import sys
import unittest
from unittest import mock

import generate as g

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "examples")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = g.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class YamlTest(unittest.TestCase):
    def test_subset(self):
        text = ('# c\na: 1\nb:\n  - x\n  - y: "q # not a comment"\n    z: [p, "r s", 3]\nc:\n- 1.5\n'
                'd: {}\ne: []\nf: null\ng: true\n---\nh: \'it\'\'s\'\n')
        self.assertEqual(g.load_yaml(text), [
            {"a": 1, "b": ["x", {"y": "q # not a comment", "z": ["p", "r s", 3]}], "c": [1.5], "d": {}, "e": [],
             "f": None, "g": True}, {"h": "it's"}])

    def test_errors(self):
        for bad in ("a: 1\n  b: 2\n", "a: {x: 1}\n", "a: 1\na: 2\n", "a:\n\t- 1\n", "just text\n"):
            with self.assertRaises(g.YamlError, msg=bad):
                g.load_yaml(bad)

    def test_round_trip_of_the_output(self):
        obj = {"a": {"b": ["x", {"c": "12000m", "d": "true"}], "e": 3}, "f": []}
        self.assertEqual(g.load_yaml(g.to_yaml(obj)), [obj])

    def test_planner_export(self):
        # discovery/planner.py export_yaml: header comments, ---, quota with Kubernetes names, env per allocation
        text = ('# Allocation plan version 3, exported ...\n# Not applied\n---\n# allocations/projects/payments.yaml\n'
                'project: "payments"\nowners: ["lead@corp"]\nallocations:\n  - cluster: "onprem-prod-01"\n'
                '    env: "prod"\n    quota:\n      requests.cpu: "40"\n      requests.memory: 160Gi\n'
                '      limits.memory: 160Gi\n')
        [doc] = g.load_yaml(text)
        ck = g.Checker()
        q = g.resources(doc["allocations"][0], "x", 1, ck)
        self.assertEqual(ck.errors, [])
        self.assertEqual(q, {"cpu": 40000, "memory": 160 * 2 ** 30, "memoryLimit": 160 * 2 ** 30})


class QuantityTest(unittest.TestCase):
    def test_cpu(self):
        self.assertEqual([g.millicores(v, "x") for v in (4, "0.5", "500m", 94.5, "1.25")], [4000, 500, 500, 94500, 1250])
        for bad in ("1Gi", "-1", "", True, "m"):
            with self.assertRaises(ValueError):
                g.millicores(bad, "x")

    def test_memory(self):
        self.assertEqual([g.mem_bytes(v, "x") for v in ("160Gi", "512Mi", "1G", 1024, "1.5Gi")],
                         [160 * 2 ** 30, 512 * 2 ** 20, 10 ** 9, 1024, 3 * 2 ** 29])
        self.assertEqual((g.mem_str(160 * 2 ** 30), g.mem_str(1536 * 2 ** 20), g.mem_str(10 ** 9)),
                         ("160Gi", "1536Mi", "1000000000"))
        self.assertEqual((g.cpu_str(40000), g.cpu_str(500)), ("40", "500m"))


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.alloc = os.path.join(self.tmp, "allocations")
        shutil.copytree(EXAMPLES, self.alloc)
        self.out = os.path.join(self.tmp, "build")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, text):
        with open(os.path.join(self.alloc, rel), "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.out, rel)) as f:
            return g.load_yaml(f.read())

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.out) for d, _, fs in os.walk(self.out) for f in fs)

    def test_examples_pass(self):
        code, out, err = run(EXAMPLES, "--check")
        self.assertEqual(code, 0, err)
        self.assertIn("prod-01                  64 / 94.5", out)

    def test_generate(self):
        code, out, err = run(self.alloc, "--out", self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files(), ["prod-01/data-api.yaml", "prod-01/data-ingest.yaml", "prod-01/payments-api.yaml",
                                        "prod-01/payments-worker.yaml", "test-01/payments-api.yaml",
                                        "test-01/payments-e2e.yaml", "test-01/payments-worker.yaml"])
        quota, limits = self.read("prod-01/payments-api.yaml")
        self.assertEqual((quota["kind"], quota["metadata"]["name"], quota["metadata"]["namespace"]),
                         ("ResourceQuota", "allocation", "payments-api"))
        self.assertEqual(quota["spec"]["hard"], {"requests.cpu": "24", "requests.memory": "96Gi",
                                                 "limits.memory": "96Gi", "pods": "200"})  # prod: limit = request
        self.assertEqual(quota["metadata"]["labels"]["allocation.cluster-resource-allocation/project"], "payments")
        self.assertEqual(limits["kind"], "LimitRange")
        self.assertEqual(limits["spec"]["limits"][0], {"type": "Container",
                                                      "defaultRequest": {"cpu": "100m", "memory": "256Mi"},
                                                      "default": {"memory": "256Mi"}})
        quota, limits = self.read("test-01/payments-worker.yaml")  # namespaceDefault, test: 2x, own defaults
        self.assertEqual(quota["spec"]["hard"], {"requests.cpu": "4", "requests.memory": "16Gi", "limits.memory": "32Gi"})
        self.assertEqual(limits["spec"]["limits"][0]["defaultRequest"], {"cpu": "50m", "memory": "128Mi"})
        self.assertEqual(self.read("test-01/payments-e2e.yaml")[0]["spec"]["hard"]["limits.memory"], "12Gi")

    def test_second_run_replaces_only_generated_files(self):
        self.assertEqual(run(self.alloc, "--out", self.out)[0], 0)
        with open(os.path.join(self.out, "keep.yaml"), "w") as f:
            f.write("# my own file\n")
        self.write("projects/data-platform.yaml", "project: data-platform\nallocations: []\n")
        self.assertEqual(run(self.alloc, "--out", self.out)[0], 0)
        self.assertNotIn("prod-01/data-api.yaml", self.files())
        self.assertIn("keep.yaml", self.files())

    def assert_error(self, text):
        code, _, err = run(self.alloc, "--check")
        self.assertEqual(code, 1)
        self.assertIn(text, err)

    def test_over_capacity(self):
        self.write("projects/big.yaml", "project: big\nallocations:\n  - cluster: prod-01\n    namespaces:\n"
                                        "      - name: big\n        cpu: 40\n        memory: 100Gi\n")
        self.assert_error("prod-01: the namespaces get 104 cpu in total, above its capacity of 94.5")

    def test_prod_memory_limit_equals_request(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: prod-01\n    namespaces:\n"
                                      "      - name: x\n        cpu: 1\n        memory: 1Gi\n        memoryLimit: 2Gi\n")
        self.assert_error("memoryLimit 2Gi is above 1x memory 1Gi")

    def test_namespaces_within_the_project_total(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: test-01\n    cpu: 1\n    memory: 1Gi\n"
                                      "    namespaces:\n      - name: a\n        cpu: 1\n        memory: 1Gi\n"
                                      "      - name: b\n        cpu: 1\n        memory: 1Gi\n")
        self.assert_error("x on test-01: the namespaces add up to 2 cpu, above the project's 1")

    def test_a_namespace_has_one_project(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: prod-01\n    namespaces:\n"
                                      "      - name: data-api\n        cpu: 1\n        memory: 1Gi\n")
        self.assert_error("namespace data-api: already allocated to data-platform")

    def test_wrong_input(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: nowhere\n    namespaces: [a]\n"
                                      "  - cluster: test-01\n    namespaces:\n      - name: a\n"
                                      "  - cluster: prod-01\n    namespaces:\n      - name: b\n        cpu: 1Gi\n"
                                      "        memory: 1\n")
        self.assert_error("cluster 'nowhere' is not in settings.yaml")
        self.assert_error("x on test-01: namespace a: no cpu/memory and no namespaceDefault")
        self.assert_error("x on prod-01: namespace b.cpu: not a CPU quantity: '1Gi'")
        self.write("projects/y.yaml", "project: payments\nallocations: []\n")
        self.assert_error("project payments is also defined in projects/")
        self.write("projects/z.yaml", "project: z\nallocations:\n  - cluster: prod-01\n    cpu: 1\n    memory: 1Gi\n")
        self.assert_error("z on prod-01: list its namespaces")


def node(name, cpu, mem, taints=None, cordoned=False, labels=None):
    spec = {"taints": taints or []}
    if cordoned:
        spec["unschedulable"] = True
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": spec,
            "status": {"allocatable": {"cpu": cpu, "memory": mem}}}


def pod(ns, node_name, cpu, mem, phase="Running", init=None):
    spec = {"nodeName": node_name, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": mem}}}]}
    if init:
        spec["initContainers"] = init
    return {"metadata": {"namespace": ns}, "spec": spec, "status": {"phase": phase}}


NODES = {"items": [
    node("cp-1", "4", "16Gi", taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]),
    node("w-1", "16", "64Gi"), node("w-2", "16", "64Gi"), node("w-3", "8", "32Gi"),
    node("w-4", "16", "64Gi", cordoned=True)]}
PODS = {"items": [
    pod("kube-system", "w-1", "500m", "1Gi"), pod("cattle-system", "w-2", "1", "2Gi"),
    pod("kube-system", "cp-1", "2", "4Gi"),          # on a node that doesn't count
    pod("payments", "w-1", "4", "8Gi"),              # tenant: not platform
    pod("ingress-nginx", "w-3", "500m", "512Mi")]}


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(EXAMPLES, self.tmp, dirs_exist_ok=True)
        self.settings = os.path.join(self.tmp, "settings.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_capacity(self):
        ck = g.Checker()
        d = g.discovery_settings({}, ck)
        cap = g.cluster_capacity(NODES, PODS, {"nodeFailures": 1, "maxQuotaPercent": 80}, d)
        self.assertEqual(cap["nodes"], 3)
        self.assertEqual(cap["skipped"], [("cp-1", "taint node-role.kubernetes.io/control-plane:NoSchedule"),
                                          ("w-4", "cordoned")])
        self.assertEqual(cap["alloc"], [40.0, 160 * 2 ** 30])
        self.assertEqual(cap["largest"], [16.0, 64 * 2 ** 30])
        self.assertEqual(cap["platform"], [2.0, 3.5 * 2 ** 30])
        # (40 - 16 - 2) x 80 % = 17.6 vCPU; (160 - 64 - 3.5) GiB x 80 % = 74 GiB
        self.assertEqual((cap["cpu"], cap["memory"]), (17.6, 74))
        d = g.discovery_settings({"discovery": {"skipTaintEffects": [], "skipCordoned": False}}, ck)
        self.assertEqual(g.cluster_capacity(NODES, PODS, {"nodeFailures": 0, "maxQuotaPercent": 100}, d)["nodes"], 5)

    def test_same_rules_as_discover(self):
        sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
        import discover
        self.assertEqual(g.DEFAULT_SYSTEM_NS_REGEX, discover.DEFAULT_SYSTEM_NS_REGEX)
        spec = {"containers": [{"resources": {"requests": {"cpu": "250m"}}}],
                "initContainers": [{"resources": {"requests": {"cpu": "1"}}},
                                   {"restartPolicy": "Always", "resources": {"requests": {"cpu": "100m"}}}],
                "overhead": {"cpu": "50m"}}
        self.assertAlmostEqual(g.pod_request(spec, "cpu"), discover.pod_effective(spec, "requests", "cpu"))

    def test_discover_rewrites_only_these_clusters(self):
        with open(self.settings) as f:
            before = f.read()
        ck = g.Checker()
        get = lambda context, args: NODES if args[1] == "nodes" else PODS
        results = g.discover(self.tmp, [("prod-01", "prod"), ("new-01", "dev")], ck, get, today="2026-10-14")
        self.assertEqual(ck.errors, [])
        self.assertEqual([r[0] for r in results], ["prod-01", "new-01"])
        with open(self.settings) as f:
            after = f.read()
        [settings] = g.load_yaml(after)
        self.assertEqual(settings["clusters"]["prod-01"], {"env": "prod", "capacity": {"cpu": 17.6, "memory": "74Gi"}})
        self.assertEqual(settings["clusters"]["test-01"], {"env": "test", "capacity": {"cpu": 60, "memory": "240Gi"}})
        self.assertEqual(settings["clusters"]["new-01"]["env"], "dev")
        self.assertEqual(list(settings["clusters"]), ["prod-01", "test-01", "new-01"])
        self.assertIn("# discovered 2026-10-14 from context prod-01: 3 node(s) counted", after)
        self.assertEqual(after.split("clusters:")[0], before.split("clusters:")[0])  # the rest is untouched
        code, _, err = run(self.tmp, "--check")  # the examples ask more than the small test cluster has
        self.assertEqual(code, 1)
        self.assertIn("prod-01: the namespaces get 64 cpu in total, above its capacity of 17.6", err)

    def test_errors_leave_settings_alone(self):
        with open(self.settings) as f:
            before = f.read()
        ck = g.Checker()
        g.discover(self.tmp, [("prod-01", "prod"), ("x", "nope")], ck, lambda c, a: NODES if a[1] == "nodes" else PODS)
        self.assertIn("--discover x=nope: no environment nope in settings.yaml", ck.errors)
        with open(self.settings) as f:
            self.assertEqual(f.read(), before)

    def test_kubeconfig_is_passed_to_kubectl(self):
        kubeconfig = os.path.join(self.tmp, "kc.yaml")
        open(kubeconfig, "w").close()
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout=json.dumps(NODES if "nodes" in cmd else PODS))
        with mock.patch.object(g.subprocess, "run", fake_run):
            code, out, err = run(self.tmp, "--kubeconfig", kubeconfig, "--discover", "prod-01=prod")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][:5], ["kubectl", "--kubeconfig", kubeconfig, "--context", "prod-01"])
        self.assertIn("prod-01 (prod): 3 node(s), capacity 17.6 vCPU / 74 GiB", out)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(self.tmp, "--kubeconfig", kubeconfig)  # only with --discover

    def test_replace_clusters_without_a_section(self):
        self.assertEqual(g.replace_clusters("environments: {}\n", {"a": "  a:\n    env: x\n"}),
                         "environments: {}\nclusters:\n  a:\n    env: x\n")


if __name__ == "__main__":
    unittest.main()
