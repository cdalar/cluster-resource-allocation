import unittest

import live
import textreport

GI = 2 ** 30


def node(name, cpu="4", mem="16Gi", ready=True):
    return {"metadata": {"name": name, "labels": {}}, "spec": {},
            "status": {"allocatable": {"cpu": cpu, "memory": mem, "pods": "110"},
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def pod(ns, name, node_name, cpu, mem, mem_lim=None, owner=None, labels=None, phase="Running"):
    res = {"requests": {"cpu": cpu, "memory": mem}}
    if mem_lim:
        res["limits"] = {"memory": mem_lim}
    return {"metadata": {"name": name, "namespace": ns, "uid": f"u-{ns}-{name}", "labels": labels or {},
                         "ownerReferences": [dict(owner, controller=True)] if owner else []},
            "spec": {"nodeName": node_name, "containers": [{"name": "c", "resources": res}]},
            "status": {"phase": phase}}


RS = {"kind": "ReplicaSet", "name": "api-abc12"}
HASH = {"pod-template-hash": "abc12"}


def state():
    s = live.LiveState()
    s.replace_all("nodes", [node("n1"), node("n2", mem="8Gi")])
    s.replace_all("namespaces", [{"metadata": {"name": "shop", "annotations": {"field.cattle.io/projectId": "c-1:p-1"}}},
                                 {"metadata": {"name": "batch"}}])
    s.set_names({"c-1:p-1": "retail"})
    s.replace_all("pods", [
        pod("shop", "api-abc12-a", "n1", "500m", "2Gi", "4Gi", RS, HASH),
        pod("shop", "api-abc12-b", "n2", "500m", "2Gi", "4Gi", RS, HASH),
        pod("shop", "db-0", "n1", "2", "4Gi", "20Gi", {"kind": "StatefulSet", "name": "db"}),
        pod("batch", "job-x", "n2", "100m", "256Mi"),
        pod("batch", "pending", "", "100m", "1Gi", phase="Pending"),
        pod("batch", "done", "n2", "1", "8Gi", phase="Succeeded"),   # finished: not kept
    ])
    return s


class Reports(unittest.TestCase):
    def setUp(self):
        self.snap = state().snapshot()

    def test_pods_sorted_with_node(self):
        lines = textreport.render(self.snap, "pods", top=2).splitlines()
        self.assertIn("Top 2 of 5 pods by memory request", lines[0])
        self.assertIn("db-0", lines[4])
        self.assertIn("n1", lines[4])
        self.assertIn("4,096 Mi", lines[4])
        cpu = textreport.render(self.snap, "pods", sort="cpu", top=1)
        self.assertIn("2,000 m", cpu)
        self.assertNotIn("done", textreport.render(self.snap, "pods"))

    def test_namespaces_totals_and_project(self):
        text = textreport.render(self.snap, "namespaces")
        rows = [l for l in text.splitlines() if l.startswith(("shop", "batch", "TOTAL"))]
        self.assertTrue(rows[0].startswith("shop") and "retail" in rows[0] and "8,192 Mi" in rows[0])
        self.assertTrue(rows[1].startswith("batch"))
        self.assertIn("9,472 Mi", rows[2])          # 8Gi + 256Mi + 1Gi
        self.assertIn("usage n/a", text)

    def test_workloads(self):
        text = textreport.render(self.snap, "workloads", namespace="shop")
        rows = [l.split() for l in text.splitlines() if l.startswith(("Deployment", "StatefulSet"))]
        self.assertEqual(rows[0][:3], ["Deployment/api", "2", "2"])   # two pods on two nodes
        self.assertEqual(rows[1][:3], ["StatefulSet/db", "1", "1"])
        with self.assertRaises(textreport.ReportError):
            textreport.render(self.snap, "workloads")
        with self.assertRaises(textreport.ReportError):
            textreport.render(self.snap, "workloads", namespace="nope")

    def test_nodes_with_namespace(self):
        text = textreport.render(self.snap, "nodes", namespace="shop")
        rows = {l.split()[0]: l for l in text.splitlines() if l.startswith(("n1", "n2"))}
        self.assertIn("38%", rows["n1"])              # 6Gi of 16Gi requested
        self.assertIn("150%!", rows["n1"])            # 24Gi of limits on 16Gi
        self.assertTrue(rows["n2"].rstrip().endswith("2.0 Gi"))   # one shop pod with 2Gi on n2
        self.assertIn("NS_PODS", text)
        self.assertNotIn("NS_PODS", textreport.render(self.snap, "nodes"))

    def test_usage(self):
        s = state()
        s.set_usage({"shop/db-0": {"c": [0.5, 3 * GI]}}, {"n1": [1.0, 4 * GI]})
        snap = s.snapshot()
        self.assertIn("3,072 Mi", textreport.render(snap, "pods"))
        self.assertIn("25%", [l for l in textreport.render(snap, "nodes").splitlines() if l.startswith("n1")][0])

    def test_bad_requests(self):
        for kw in ({"report": "x"}, {"report": "pods", "sort": "disk"}, {"report": "pods", "top": 0}):
            with self.assertRaises(textreport.ReportError):
                textreport.render(self.snap, **kw)


if __name__ == "__main__":
    unittest.main()
