import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

import generate as g

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "examples")
RANCHER_PROJECTS = {"items": [
    {"metadata": {"name": "p-pay01", "namespace": "c-m-abcd1234"},
     "spec": {"clusterName": "c-m-abcd1234", "displayName": "payments"}},
    {"metadata": {"name": "p-pay02", "namespace": "c-m-efgh5678"},
     "spec": {"clusterName": "c-m-efgh5678", "displayName": "payments"}},
    {"metadata": {"name": "p-dat01", "namespace": "c-m-abcd1234"},
     "spec": {"clusterName": "c-m-abcd1234", "displayName": "data-platform"}}]}


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
        self.assertEqual((g.mem_str(160 * 2 ** 30), g.mem_str(1536 * 2 ** 20), g.mem_mi(10 ** 9)),
                         ("160Gi", "1536Mi", "954Mi"))
        self.assertEqual((g.cpu_str(40000), g.cpu_str(500)), ("40", "500m"))


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.alloc = os.path.join(self.tmp, "allocations")
        shutil.copytree(EXAMPLES, self.alloc)
        self.projects = os.path.join(self.tmp, "projects.json")
        with open(self.projects, "w") as f:
            json.dump(RANCHER_PROJECTS, f)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, text):
        with open(os.path.join(self.alloc, rel), "w") as f:
            f.write(text)

    def test_examples_pass(self):
        code, out, err = run(EXAMPLES, "--check")
        self.assertEqual(code, 0, err)
        self.assertIn("onprem-prod-01           64 / 94.5", out)

    def test_generate(self):
        outdir = os.path.join(self.tmp, "build")
        code, out, err = run(self.alloc, "--out", outdir, "--rancher-projects", self.projects)
        self.assertEqual(code, 0, err)
        files = sorted(os.path.relpath(os.path.join(d, f), outdir) for d, _, fs in os.walk(outdir) for f in fs)
        self.assertEqual(files, ["kubernetes/aks-prod-weu/data-api.yaml", "kubernetes/aks-prod-weu/data-ingest.yaml",
                                 "rancher/onprem-prod-01/data-platform.yaml", "rancher/onprem-prod-01/payments.yaml",
                                 "rancher/onprem-test-01/payments.yaml"])
        with open(os.path.join(outdir, "rancher/onprem-test-01/payments.yaml")) as f:
            [project] = g.load_yaml(f.read())
        self.assertEqual(project["metadata"]["name"], "p-pay02")
        self.assertEqual(project["metadata"]["namespace"], "c-m-efgh5678")
        self.assertEqual(project["spec"]["resourceQuota"]["limit"],
                         {"requestsCpu": "12000m", "requestsMemory": "49152Mi", "limitsMemory": "98304Mi"})  # test: 2x
        self.assertEqual(project["spec"]["containerDefaultResourceLimit"],
                         {"requestsCpu": "50m", "requestsMemory": "128Mi", "limitsMemory": "256Mi"})
        with open(os.path.join(outdir, "rancher/onprem-prod-01/payments.yaml")) as f:
            [project] = g.load_yaml(f.read())
        self.assertEqual(project["spec"]["resourceQuota"]["limit"],
                         {"requestsCpu": "40000m", "requestsMemory": "163840Mi", "limitsMemory": "163840Mi",
                          "pods": "300"})  # prod: limit = request
        with open(os.path.join(outdir, "kubernetes/aks-prod-weu/data-api.yaml")) as f:
            quota, limits = g.load_yaml(f.read())
        self.assertEqual(quota["kind"], "ResourceQuota")
        self.assertEqual(quota["spec"]["hard"], {"requests.cpu": "2", "requests.memory": "8Gi", "limits.memory": "8Gi"})
        self.assertEqual(limits["spec"]["limits"][0]["default"], {"memory": "256Mi"})
        # a second run replaces the files: a removed allocation disappears
        self.write("projects/data-platform.yaml", "project: data-platform\nallocations: []\n")
        self.assertEqual(run(self.alloc, "--out", outdir, "--rancher-projects", self.projects)[0], 0)
        self.assertFalse(os.path.exists(os.path.join(outdir, "kubernetes")))
        self.assertFalse(os.path.exists(os.path.join(outdir, "rancher/onprem-prod-01/data-platform.yaml")))

    def assert_error(self, text, *argv):
        code, _, err = run(self.alloc, *(argv or ("--check",)))
        self.assertEqual(code, 1)
        self.assertIn(text, err)

    def test_over_capacity(self):
        self.write("projects/big.yaml", "project: big\nallocations:\n  - cluster: onprem-prod-01\n    cpu: 40\n"
                                        "    memory: 100Gi\n    namespaceDefault:\n      cpu: 1\n      memory: 1Gi\n")
        self.assert_error("onprem-prod-01: projects get 104 cpu in total, above its capacity of 94.5")

    def test_prod_memory_limit_equals_request(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: onprem-prod-01\n    cpu: 1\n"
                                      "    memory: 1Gi\n    memoryLimit: 2Gi\n    namespaceDefault:\n      cpu: 1\n"
                                      "      memory: 1Gi\n")
        self.assert_error("memoryLimit 2Gi is above 1x memory 1Gi")

    def test_namespace_default_and_namespaces_within_the_project(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: onprem-prod-01\n    cpu: 1\n"
                                      "    memory: 1Gi\n    namespaceDefault:\n      cpu: 2\n      memory: 1Gi\n"
                                      "  - cluster: aks-prod-weu\n    cpu: 1\n    memory: 1Gi\n    namespaces:\n"
                                      "      - name: a\n        cpu: 1\n        memory: 1Gi\n      - name: b\n"
                                      "        cpu: 1\n        memory: 1Gi\n")
        self.assert_error("x on onprem-prod-01: namespaceDefault cpu is above the project's")
        self.assert_error("x on aks-prod-weu: the namespaces add up to 2 cpu, above the project's 1")

    def test_wrong_input(self):
        self.write("projects/x.yaml", "project: x\nallocations:\n  - cluster: nowhere\n    cpu: 1\n    memory: 1Gi\n"
                                      "  - cluster: onprem-test-01\n    cpu: 1\n    memory: 1Gi\n    namespaces:\n"
                                      "      - name: a\n  - cluster: onprem-prod-01\n    cpu: 1Gi\n    memory: 1\n")
        self.assert_error("cluster 'nowhere' is not in settings.yaml")
        self.assert_error("namespaces are only listed for managedBy: kubernetes clusters")
        self.assert_error("x on onprem-prod-01.cpu: not a CPU quantity: '1Gi'")
        self.write("projects/y.yaml", "project: payments\nallocations: []\n")
        self.assert_error("project payments is also defined in projects/")

    def test_missing_rancher_project(self):
        with open(self.projects, "w") as f:
            json.dump({"items": RANCHER_PROJECTS["items"][:1]}, f)
        self.assert_error("Rancher has no Project payments on onprem-test-01 (c-m-efgh5678)",
                          "--out", os.path.join(self.tmp, "b"), "--rancher-projects", self.projects)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "b")))  # nothing written on errors


if __name__ == "__main__":
    unittest.main()
