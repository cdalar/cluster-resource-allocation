import json
import os
import stat
import sys
import tempfile
import time
import unittest

import live

NS = {"metadata": {"name": "payments-api", "annotations": {"field.cattle.io/projectId": "c-m-abc:p-12345"}}}
NODE = {"metadata": {"name": "worker-1", "labels": {"node-role.kubernetes.io/worker": "true",
                                                    "node.kubernetes.io/instance-type": "m5.xlarge"}},
        "spec": {}, "status": {"allocatable": {"cpu": "3900m", "memory": "15Gi", "pods": "110"},
                               "conditions": [{"type": "Ready", "status": "True"}],
                               "nodeInfo": {"kubeletVersion": "v1.36.5+k3s1"}}}


def pod(name, owner=None, labels=None, phase="Running", node="worker-1", containers=None, inits=None):
    return {
        "metadata": {"name": name, "namespace": "payments-api", "uid": f"uid-{name}", "labels": labels or {},
                     "ownerReferences": [dict(owner, controller=True)] if owner else []},
        "spec": {"nodeName": node, "initContainers": inits or [],
                 "containers": containers or [{"name": "app", "image": "app:1",
                                               "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                                             "limits": {"cpu": "1", "memory": "2Gi"}}}]},
        "status": {"phase": phase, "qosClass": "Burstable", "startTime": "2026-10-28T10:00:00Z",
                   "containerStatuses": [{"name": "app", "ready": True, "restartCount": 3}]},
    }


class Owners(unittest.TestCase):
    def test_owner_of(self):
        rs = {"kind": "ReplicaSet", "name": "api-7d9f8c6b5"}
        self.assertEqual(live.owner_of(pod("api-7d9f8c6b5-x1", rs, {"pod-template-hash": "7d9f8c6b5"})), ("Deployment", "api"))
        self.assertEqual(live.owner_of(pod("api-7d9f8c6b5-x1", rs)), ("ReplicaSet", "api-7d9f8c6b5"))   # no Deployment label
        self.assertEqual(live.owner_of(pod("db-0", {"kind": "StatefulSet", "name": "db"})), ("StatefulSet", "db"))
        self.assertEqual(live.owner_of(pod("agent-abc", {"kind": "DaemonSet", "name": "agent"})), ("DaemonSet", "agent"))
        self.assertEqual(live.owner_of(pod("etl-29300000-k9", {"kind": "Job", "name": "etl-29300000"})), ("Job", "etl-29300000"))
        self.assertEqual(live.owner_of(pod("etcd-worker-1", {"kind": "Node", "name": "worker-1"})), (live.STATIC_POD, "etcd"))
        self.assertEqual(live.owner_of(pod("debug")), ("Pod", "debug"))

    def test_pod_summary_uses_effective_requests(self):
        sidecar = {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}
        init = {"name": "init", "resources": {"requests": {"cpu": "2", "memory": "64Mi"}}}
        s = live.pod_summary(pod("api-1", inits=[sidecar, init]))
        self.assertEqual(s["cpu_req"], 2.1)                          # init step (2 + running sidecar 0.1) beats app + sidecar
        self.assertEqual(s["mem_req"], 2**30 + 128 * 2**20)         # app + sidecar
        self.assertEqual([c["name"] for c in s["containers"]], ["app", "proxy"])   # the init step is not long-running
        self.assertEqual(s["containers"][0]["restarts"], 3)
        self.assertEqual((s["owner_kind"], s["owner"], s["node"], s["phase"]), ("Pod", "api-1", "worker-1", "Running"))

    def test_node_and_namespace_summary(self):
        n = live.node_summary(NODE)
        self.assertEqual((n["roles"], n["ready"], n["cpu_alloc"], n["mem_alloc"], n["pods_alloc"]), (["worker"], True, 3.9, 15 * 2**30, 110))
        import re
        import discover
        ns = live.namespace_summary(NS, {"c-m-abc:p-12345": "payments"}, re.compile(discover.DEFAULT_SYSTEM_NS_REGEX),
                                    discover.DEFAULT_SYSTEM_PROJECT_RE)
        self.assertEqual((ns["project_id"], ns["project"], ns["category"]), ("c-m-abc:p-12345", "payments", "tenant"))
        ns = live.namespace_summary(NS, {}, re.compile(discover.DEFAULT_SYSTEM_NS_REGEX), discover.DEFAULT_SYSTEM_PROJECT_RE)
        self.assertEqual(ns["project"], "p-12345")   # no name known yet: the id


class State(unittest.TestCase):
    def test_changes_follow_the_state(self):
        st = live.LiveState()
        st.replace_all("nodes", [NODE])
        st.replace_all("namespaces", [NS])
        st.replace_all("pods", [pod("a"), pod("b"), pod("done", phase="Succeeded")])
        snap = st.snapshot()
        self.assertEqual(sorted(p["name"] for p in snap["pods"]), ["a", "b"])   # finished pods are not kept
        seq0 = snap["seq"]
        self.assertEqual(st.changes(seq0), {"seq": seq0, "status": snap["status"]})

        st.apply("pods", "MODIFIED", pod("a", phase="Pending", node=""))
        st.apply("pods", "DELETED", pod("b"))
        st.apply("pods", "MODIFIED", pod("a", phase="Pending", node=""))   # same summary again: no change
        st.set_usage({"payments-api/a": {"app": [0.25, 500 * 2**20]}}, {"worker-1": [1.5, 8 * 2**30]})
        d = st.changes(seq0)
        self.assertEqual(set(d["pods"]), {"payments-api/a", "payments-api/b"})
        self.assertIsNone(d["pods"]["payments-api/b"])
        self.assertEqual(d["pods"]["payments-api/a"]["phase"], "Pending")
        self.assertEqual(d["usage"]["payments-api/a"]["app"], [0.25, 500 * 2**20])
        self.assertNotIn("nodes", d)
        self.assertTrue(d["status"]["metrics"]["available"])
        # a second list without b and with a new pod only emits the differences
        seq1 = d["seq"]
        st.replace_all("pods", [pod("a", phase="Pending", node=""), pod("c")])
        d = st.changes(seq1)
        self.assertEqual(list(d["pods"]), ["payments-api/c"])
        self.assertNotIn("usage", d)   # usage did not change since seq1

    def test_reset_when_behind(self):
        st = live.LiveState()
        st.replace_all("pods", [pod("a")])
        self.assertEqual(st.changes(-1), {"seq": st.seq, "reset": True})       # a page without a snapshot
        self.assertEqual(st.changes(st.seq + 5), {"seq": st.seq, "reset": True})   # from another server life
        self.assertEqual(st.changes(0)["pods"]["payments-api/a"]["name"], "a")   # seq 0 = before the first change

    def test_project_names_from_the_report(self):
        st = live.LiveState()
        st.replace_all("namespaces", [NS])
        self.assertEqual(st.namespaces["payments-api"]["project"], "p-12345")
        seq = st.seq
        st.set_names_from_report({"namespaces": [{"project_id": "c-m-abc:p-12345", "project": "payments"},
                                                 {"project_id": "label:team-a", "project": "team-a"}]})
        self.assertEqual(st.changes(seq)["namespaces"]["payments-api"]["project"], "payments")
        self.assertEqual(st.names, {"c-m-abc:p-12345": "payments"})
        st.set_names_from_report({"namespaces": [{"project_id": "c-m-abc:p-12345", "project": "payments"}]})
        self.assertEqual(st.seq, seq + 1)   # same names: nothing emitted

    def test_snapshot_json_cached_per_seq(self):
        st = live.LiveState()
        st.replace_all("nodes", [NODE])
        body, gz = st.snapshot_json()
        self.assertIs(body, st.snapshot_json()[0])
        import gzip
        self.assertEqual(json.loads(gzip.decompress(gz))["nodes"][0]["name"], "worker-1")
        st.apply("nodes", "DELETED", NODE)
        self.assertEqual(json.loads(st.snapshot_json()[0])["nodes"], [])


FAKE_KUBECTL = r'''#!/usr/bin/env python3
"""kubectl stand-in: answers `get --raw <path>` from a JSON fixture file; a watch prints its events then waits."""
import json, os, sys, time
path = sys.argv[sys.argv.index("--raw") + 1]
with open(os.environ["FAKE_KUBECTL_DATA"]) as f:
    data = json.load(f)
res = path.split("?")[0].rsplit("/", 1)[1]
if "watch=1" in path:
    for ev in data["watch"].get(res, []):
        print(json.dumps(ev), flush=True)
    time.sleep(60)
    sys.exit(0)
if path.startswith("/apis/metrics.k8s.io"):
    if res not in data["metrics"]:
        sys.stderr.write("Error from server (NotFound): the server could not find the requested resource\n")
        sys.exit(1)
    print(json.dumps({"items": data["metrics"][res]}))
else:
    print(json.dumps({"metadata": {"resourceVersion": "100"}, "items": data["list"][res]}))
'''


class FakeKubectl(unittest.TestCase):
    """Watcher and MetricsPoller against a kubectl stand-in (no cluster)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = os.path.join(self.tmp.name, "kubectl")
        with open(self.script, "w") as f:
            f.write(FAKE_KUBECTL)
        os.chmod(self.script, stat.S_IRWXU)
        self.data = os.path.join(self.tmp.name, "data.json")
        os.environ["FAKE_KUBECTL_DATA"] = self.data
        self.threads = []

    def tearDown(self):
        for t in self.threads:
            t.close()
        for t in self.threads:
            t.join(timeout=5)
        self.tmp.cleanup()

    def write(self, fixture):
        with open(self.data, "w") as f:
            json.dump(fixture, f)

    def wait_for(self, fn, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.05)
        return False

    def test_list_then_watch(self):
        rs = {"kind": "ReplicaSet", "name": "api-7d9f8c6b5"}
        self.write({"list": {"pods": [pod("api-7d9f8c6b5-x1", rs, {"pod-template-hash": "7d9f8c6b5"}), pod("old")]},
                    "watch": {"pods": [
                        {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "101"}}},
                        {"type": "ADDED", "object": pod("api-7d9f8c6b5-x2", rs, {"pod-template-hash": "7d9f8c6b5"})},
                        {"type": "DELETED", "object": pod("old")}]},
                    "metrics": {}})
        st = live.LiveState()
        w = live.Watcher(st, "pods", kubectl_cmd=[sys.executable, self.script])
        self.threads.append(w)
        w.start()
        self.assertTrue(self.wait_for(lambda: set(st.pods) == {"payments-api/api-7d9f8c6b5-x1", "payments-api/api-7d9f8c6b5-x2"}),
                        f"state: {sorted(st.pods)} status: {st.status()}")
        self.assertEqual({p["owner"] for p in st.pods.values()}, {"api"})
        self.assertEqual(st.status()["watch"]["pods"]["state"], "watching")

    def test_watch_error_is_reported_and_retried(self):
        self.write({"list": {"pods": [pod("a")]},
                    "watch": {"pods": [{"type": "ERROR", "object": {"kind": "Status", "code": 410, "reason": "Expired",
                                                                     "message": "too old resource version"}}]},
                    "metrics": {}})
        st = live.LiveState()
        w = live.Watcher(st, "pods", kubectl_cmd=[sys.executable, self.script])
        self.threads.append(w)
        w.start()
        self.assertTrue(self.wait_for(lambda: st.status()["watch"]["pods"]["state"] == "error"))
        self.assertIn("too old resource version", st.status()["watch"]["pods"]["error"])
        self.assertEqual(list(st.pods), ["payments-api/a"])   # the list before the error is kept
        self.assertTrue(self.wait_for(lambda: st.status()["watch"]["pods"]["state"] != "error", timeout=5))   # relisted

    def test_metrics_poller(self):
        self.write({"list": {}, "watch": {}, "metrics": {
            "pods": [{"metadata": {"namespace": "payments-api", "name": "a"},
                      "containers": [{"name": "app", "usage": {"cpu": "250m", "memory": "500Mi"}}]}],
            "nodes": [{"metadata": {"name": "worker-1"}, "usage": {"cpu": "1500m", "memory": "8Gi"}}]}})
        st = live.LiveState()
        m = live.MetricsPoller(st, kubectl_cmd=[sys.executable, self.script], interval=60)
        self.threads.append(m)
        m.start()
        self.assertTrue(self.wait_for(lambda: st.usage))
        self.assertEqual(st.usage, {"payments-api/a": {"app": [0.25, 500 * 2**20]}})
        self.assertEqual(st.node_usage, {"worker-1": [1.5, 8 * 2**30]})
        self.assertTrue(st.status()["metrics"]["available"])

    def test_metrics_server_missing(self):
        self.write({"list": {}, "watch": {}, "metrics": {}})
        st = live.LiveState()
        m = live.MetricsPoller(st, kubectl_cmd=[sys.executable, self.script], interval=60)
        self.threads.append(m)
        m.start()
        self.assertTrue(self.wait_for(lambda: st.status()["metrics"]["available"] is False))
        self.assertIn("NotFound", st.status()["metrics"]["error"])
        self.assertEqual(st.usage, {})


if __name__ == "__main__":
    unittest.main()


def pdb(name, match, min_available=None, max_unavailable=None, ns="payments-api"):
    spec = {"selector": {"matchLabels": match}}
    if min_available is not None:
        spec["minAvailable"] = min_available
    if max_unavailable is not None:
        spec["maxUnavailable"] = max_unavailable
    return {"metadata": {"name": name, "namespace": ns}, "spec": spec,
            "status": {"currentHealthy": 2, "desiredHealthy": 1, "disruptionsAllowed": 1, "expectedPods": 2}}


class DisruptionBudgets(unittest.TestCase):
    def test_selector(self):
        self.assertTrue(live.selector_matches({"matchLabels": {"app": "web"}}, {"app": "web", "tier": "x"}))
        self.assertFalse(live.selector_matches({"matchLabels": {"app": "web"}}, {"app": "api"}))
        self.assertFalse(live.selector_matches({}, {"app": "web"}))   # a PDB without a selector matches nothing
        self.assertTrue(live.selector_matches({"matchExpressions": [{"key": "tier", "operator": "In", "values": ["a", "b"]},
                                                                    {"key": "canary", "operator": "DoesNotExist"}]}, {"tier": "b"}))
        self.assertFalse(live.selector_matches({"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["b"]}]}, {"tier": "b"}))
        self.assertFalse(live.selector_matches({"matchExpressions": [{"key": "tier", "operator": "Exists"}]}, {}))

    def test_pods_carry_their_pdbs(self):
        st = live.LiveState()
        st.replace_all("pods", [pod("web-1", labels={"app": "web"}), pod("api-1", labels={"app": "api"})])
        self.assertEqual(st.pods["payments-api/web-1"]["pdbs"], [])
        seq = st.seq
        st.apply("pdbs", "ADDED", pdb("web-pdb", {"app": "web"}, min_available=1))
        d = st.changes(seq)
        self.assertEqual(d["pdbs"]["payments-api/web-pdb"]["min_available"], 1)
        self.assertEqual(list(d["pods"]), ["payments-api/web-1"])          # only the matching pod was re-emitted
        self.assertEqual(d["pods"]["payments-api/web-1"]["pdbs"], ["web-pdb"])
        # a new pod matches the existing PDB; a relist of the PDBs without it clears the pods again
        st.apply("pods", "ADDED", pod("web-2", labels={"app": "web"}))
        self.assertEqual(st.pods["payments-api/web-2"]["pdbs"], ["web-pdb"])
        seq = st.seq
        st.replace_all("pdbs", [pdb("other", {"app": "api"}, max_unavailable="25%")])
        d = st.changes(seq)
        self.assertIsNone(d["pdbs"]["payments-api/web-pdb"])
        self.assertEqual({k: v["pdbs"] for k, v in d["pods"].items()},
                         {"payments-api/web-1": [], "payments-api/web-2": [], "payments-api/api-1": ["other"]})
        self.assertEqual(st.snapshot()["status"]["counts"]["pdbs"], 1)
        # a deleted pod is forgotten for later matching
        st.apply("pods", "DELETED", pod("api-1", labels={"app": "api"}))
        self.assertNotIn("payments-api/api-1", st.pod_labels)
