#!/usr/bin/env python3
"""Live view (server.py /live): the cluster's nodes, namespaces and pods kept in memory from Kubernetes watches,
plus a usage snapshot from metrics-server every few seconds.

Read-only, through kubectl only, like discover.py:
  - one `kubectl get --raw <path>` list per resource, then `kubectl get --raw '<path>?watch=1&resourceVersion=...'`,
    a stream of one JSON watch event per line that kubectl copies to stdout as it arrives (nodes, namespaces, pods,
    PodDisruptionBudgets; the list and watch are repeated every RESYNC seconds, and after any error);
  - `kubectl get --raw /apis/metrics.k8s.io/v1beta1/{pods,nodes}` every few seconds (skipped, with "n/a" on the
    page, when metrics-server is missing).

The state is compact summaries, not the raw objects: pods carry their effective requests/limits (as the scheduler
computes them, discover.pod_effective), their owner workload (Deployment through the ReplicaSet's pod-template-hash,
StatefulSet, DaemonSet, Job, ReplicaSet, static pod, or the bare Pod), node, phase, per-container figures and the names of the PodDisruptionBudgets that
select them (matched here from the pod labels, so the page can tell whether a workload survives a node failure).
Pods that finished (Succeeded/Failed) are dropped.

Changes are numbered (seq) and kept in a bounded log, so a page can follow them incrementally
(LiveState.changes(since); the server streams them as Server-Sent Events) and reloads the whole snapshot when it
fell too far behind (reset).
"""

import collections
import gzip
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

import discover

# resource -> list/watch path (read-only: get/list/watch)
WATCHED = {"nodes": "/api/v1/nodes", "namespaces": "/api/v1/namespaces", "pods": "/api/v1/pods",
           "pdbs": "/apis/policy/v1/poddisruptionbudgets"}
METRICS_PATH = "/apis/metrics.k8s.io/v1beta1"
RESYNC = 1800          # seconds: a watch is closed by the server after this; the list is repeated then
CHANGELOG = 20000      # change log entries kept for changes(since); older = the page reloads the snapshot
DEFAULT_METRICS_INTERVAL = 15

# Workload kinds that own pods; a pod with no owner is its own "Pod" workload, a mirror pod (owner Node) a static pod.
STATIC_POD = "StaticPod"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def owner_of(pod):
    """(kind, name) of the workload a pod belongs to.

    Pod -> ReplicaSet -> Deployment is resolved from the ReplicaSet's name and the pod-template-hash label the
    Deployment controller sets (<deployment>-<hash>), so no ReplicaSet read is needed; a ReplicaSet without the
    label stays a ReplicaSet. A mirror pod (owner Node) is a static pod named without its node suffix.
    """
    md = pod["metadata"]
    refs = md.get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    if not ref:
        return "Pod", md["name"]
    kind, name = ref.get("kind") or "Pod", ref.get("name") or md["name"]
    if kind == "ReplicaSet":
        h = (md.get("labels") or {}).get("pod-template-hash")
        if h and name.endswith("-" + h) and len(name) > len(h) + 1:
            return "Deployment", name[: -len(h) - 1]
    if kind == "Node":
        suffix = "-" + name
        return STATIC_POD, md["name"][: -len(suffix)] if md["name"].endswith(suffix) else md["name"]
    return kind, name


def pod_summary(pod):
    md, spec, status = pod["metadata"], pod.get("spec") or {}, pod.get("status") or {}
    statuses = {c.get("name"): c for c in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])}
    containers = []
    for c in discover.long_running_containers(spec):
        st = statuses.get(c.get("name")) or {}
        containers.append({
            "name": c.get("name"), "image": c.get("image"),
            "cpu_req": discover._res(c, "requests", "cpu"), "mem_req": discover._res(c, "requests", "memory"),
            "cpu_lim": discover._res(c, "limits", "cpu"), "mem_lim": discover._res(c, "limits", "memory"),
            "restarts": st.get("restartCount", 0), "ready": bool(st.get("ready")),
        })
    kind, owner = owner_of(pod)
    return {
        "ns": md["namespace"], "name": md["name"], "uid": md.get("uid"), "node": spec.get("nodeName") or "",
        "phase": "Terminating" if md.get("deletionTimestamp") else status.get("phase") or "", "qos": status.get("qosClass") or "",
        "owner_kind": kind, "owner": owner, "started": status.get("startTime"),
        "cpu_req": discover.pod_effective(spec, "requests", "cpu"),
        "mem_req": discover.pod_effective(spec, "requests", "memory"),
        "cpu_lim": discover.pod_effective(spec, "limits", "cpu"),
        "mem_lim": discover.pod_effective(spec, "limits", "memory"),
        "containers": containers,
    }


def node_summary(node):
    md, spec, status = node["metadata"], node.get("spec") or {}, node.get("status") or {}
    labels = md.get("labels") or {}
    alloc = status.get("allocatable") or {}
    conds = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    return {
        "name": md["name"],
        "roles": sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")) or ["worker"],
        "ready": conds.get("Ready") == "True", "unschedulable": bool(spec.get("unschedulable")),
        "cpu_alloc": discover.parse_quantity(alloc.get("cpu")), "mem_alloc": discover.parse_quantity(alloc.get("memory")),
        "pods_alloc": int(discover.parse_quantity(alloc.get("pods"))),
        "instance_type": labels.get("node.kubernetes.io/instance-type", ""),
        "zone": labels.get("topology.kubernetes.io/zone", ""),
        "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion", ""),
    }


def namespace_summary(ns, names, system_re, system_project_re, project_label=None):
    """Namespace with its Rancher Project (id and display name, like discover.collect_cluster) and category."""
    md = ns["metadata"]
    name = md["name"]
    annotations, labels = md.get("annotations") or {}, md.get("labels") or {}
    project_ref = annotations.get(discover.RANCHER_PROJECT_ANNOTATION, "")
    project_id, project = "", ""
    if project_ref:
        project_id, project = project_ref, names.get(project_ref) or project_ref.split(":")[-1]
    elif project_label and labels.get(project_label):
        project = labels[project_label]
        project_id = f"label:{project}"
    return {"name": name, "project_id": project_id, "project": project,
            "category": discover.classify(name, project, bool(project_id), system_re, system_project_re),
            "phase": (ns.get("status") or {}).get("phase") or ""}


def pod_key(ns, name):
    return f"{ns}/{name}"


def pdb_summary(pdb):
    """A PodDisruptionBudget: its rule (minAvailable / maxUnavailable, number or percentage), selector and status."""
    md, spec, status = pdb["metadata"], pdb.get("spec") or {}, pdb.get("status") or {}
    return {"ns": md["namespace"], "name": md["name"],
            "min_available": spec.get("minAvailable"), "max_unavailable": spec.get("maxUnavailable"),
            "selector": spec.get("selector") or {},
            "current_healthy": status.get("currentHealthy", 0), "desired_healthy": status.get("desiredHealthy", 0),
            "disruptions_allowed": status.get("disruptionsAllowed", 0), "expected_pods": status.get("expectedPods", 0)}


def selector_matches(selector, labels):
    """Does a label selector (matchLabels + matchExpressions) select these labels? An empty selector selects nothing
    (as for a PDB: a null selector matches no pods)."""
    if not selector:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get("matchExpressions") or []:
        k, op, values = e.get("key"), e.get("operator"), e.get("values") or []
        if op == "In" and labels.get(k) not in values:
            return False
        if op == "NotIn" and labels.get(k) in values:
            return False
        if op == "Exists" and k not in labels:
            return False
        if op == "DoesNotExist" and k in labels:
            return False
    return True


class LiveState:
    """In-memory nodes / namespaces / pods / usage, with a numbered change log. Thread-safe."""

    def __init__(self, args=None, metrics_interval=DEFAULT_METRICS_INTERVAL):
        self.args = args
        self.cond = threading.Condition()
        self.nodes, self.namespaces, self.pods, self.pdbs = {}, {}, {}, {}
        self.raw_namespaces = {}        # name -> namespace object (kept to recompute project names)
        self.pod_labels = {}            # "ns/pod" -> labels, to match PodDisruptionBudgets when one changes
        self.usage, self.node_usage = {}, {}   # "ns/pod" -> {container: [cpu, mem]}; node -> [cpu, mem]
        self.usage_seq, self.usage_at = 0, None
        self.seq = 0
        self.log = collections.deque(maxlen=CHANGELOG)   # (seq, kind, key, summary or None)
        self.names = {}                 # Rancher project ref -> display name (from the collector's report)
        self.cluster = getattr(args, "cluster_name", None) or None   # display name, else the report's once collected
        self.system_re = re.compile(getattr(args, "system_ns_regex", None) or discover.DEFAULT_SYSTEM_NS_REGEX)
        self.system_project_re = re.compile(getattr(args, "system_project_regex", None) or discover.DEFAULT_SYSTEM_PROJECT_REGEX,
                                            re.IGNORECASE)
        self.project_label = getattr(args, "project_label", None)
        self.metrics_interval = metrics_interval
        self.watch_status = {r: {"state": "starting", "error": None, "since": None} for r in WATCHED}
        self.metrics_status = {"available": None, "error": None, "at": None, "interval": metrics_interval}
        self.started = now_iso()
        self._snapshot_cache = (None, None)   # (seq, body)

    # ---- updates (called by the watchers / poller) ----
    def _emit(self, kind, key, value):
        self.seq += 1
        self.log.append((self.seq, kind, key, value))

    def _store(self, kind):
        return {"nodes": self.nodes, "namespaces": self.namespaces, "pods": self.pods, "pdbs": self.pdbs}[kind]

    def _pdbs_of(self, ns, labels):
        return sorted(p["name"] for p in self.pdbs.values() if p["ns"] == ns and selector_matches(p["selector"], labels))

    def _rematch_pdbs(self, ns):
        """A PDB in `ns` changed: pods whose PDB list changes are re-emitted."""
        for key, labels in self.pod_labels.items():
            pod = self.pods.get(key)
            if pod and pod["ns"] == ns:
                pdbs = self._pdbs_of(ns, labels)
                if pdbs != pod["pdbs"]:
                    self._upsert("pods", key, dict(pod, pdbs=pdbs))

    def _summarize(self, kind, obj):
        """(key, summary) of a watched object, or (key, None) when it must not be kept (finished pod)."""
        if kind == "pods":
            key = pod_key(obj["metadata"]["namespace"], obj["metadata"]["name"])
            if (obj.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                self.pod_labels.pop(key, None)
                return key, None
            labels = obj["metadata"].get("labels") or {}
            self.pod_labels[key] = labels
            summary = pod_summary(obj)
            summary["pdbs"] = self._pdbs_of(summary["ns"], labels)
            return key, summary
        if kind == "pdbs":
            return pod_key(obj["metadata"]["namespace"], obj["metadata"]["name"]), pdb_summary(obj)
        if kind == "nodes":
            return obj["metadata"]["name"], node_summary(obj)
        return obj["metadata"]["name"], namespace_summary(obj, self.names, self.system_re, self.system_project_re,
                                                           self.project_label)

    def apply(self, kind, event_type, obj):
        """One watch event."""
        with self.cond:
            key, summary = self._summarize(kind, obj)
            if kind == "namespaces":
                if event_type == "DELETED":
                    self.raw_namespaces.pop(key, None)
                else:
                    self.raw_namespaces[key] = obj
            if kind == "pods" and event_type == "DELETED":
                self.pod_labels.pop(key, None)
            self._upsert(kind, key, None if event_type == "DELETED" else summary)
            if kind == "pdbs":
                self._rematch_pdbs(obj["metadata"]["namespace"])
            self.cond.notify_all()

    def _upsert(self, kind, key, summary):
        store = self._store(kind)
        if summary is None:
            if key in store:
                del store[key]
                self._emit(kind, key, None)
        elif store.get(key) != summary:
            store[key] = summary
            self._emit(kind, key, summary)

    def replace_all(self, kind, objects):
        """A fresh list: keep the state identical to it, emitting only the differences."""
        with self.cond:
            seen = set()
            if kind == "namespaces":
                self.raw_namespaces = {o["metadata"]["name"]: o for o in objects}
            for obj in objects:
                key, summary = self._summarize(kind, obj)
                seen.add(key)
                self._upsert(kind, key, summary)
            for key in [k for k in self._store(kind) if k not in seen]:
                self._upsert(kind, key, None)
                if kind == "pods":
                    self.pod_labels.pop(key, None)
            if kind == "pdbs":
                for ns in {p["ns"] for p in self.pdbs.values()} | {k.split("/", 1)[0] for k in self.pod_labels}:
                    self._rematch_pdbs(ns)
            self.cond.notify_all()

    def set_watch_status(self, kind, state, error=None):
        with self.cond:
            st = self.watch_status[kind]
            if st["state"] != state or st["error"] != error:
                st.update(state=state, error=error, since=now_iso())
                self._emit("status", kind, None)
                self.cond.notify_all()

    def set_usage(self, usage, node_usage):
        with self.cond:
            self.usage, self.node_usage = usage, node_usage
            self.usage_at = now_iso()
            self.metrics_status.update(available=True, error=None, at=self.usage_at)
            self._emit("usage", "*", None)
            self.usage_seq = self.seq
            self.cond.notify_all()

    def set_metrics_error(self, error):
        with self.cond:
            self.metrics_status.update(available=False, error=error, at=now_iso())
            self._emit("status", "metrics", None)
            self.cond.notify_all()

    def set_names(self, names):
        """Rancher Project display names (ref -> name); namespaces whose project name changes are re-emitted."""
        with self.cond:
            if names == self.names:
                return
            self.names = dict(names)
            for key, obj in self.raw_namespaces.items():
                self._upsert("namespaces", key, namespace_summary(obj, self.names, self.system_re, self.system_project_re,
                                                                   self.project_label))
            self.cond.notify_all()

    def set_names_from_report(self, report):
        names = {}
        for row in (report or {}).get("namespaces") or []:
            if row.get("project_id") and row.get("project") and not row["project_id"].startswith("label:"):
                names[row["project_id"]] = row["project"]
        clusters = (report or {}).get("clusters") or []
        with self.cond:
            if clusters and not getattr(self.args, "cluster_name", None) and self.cluster != clusters[0].get("cluster"):
                self.cluster = clusters[0].get("cluster")
                self._emit("status", "cluster", None)
                self.cond.notify_all()
        self.set_names(names)

    # ---- reads (called by the server) ----
    def status(self):
        with self.cond:
            return self._status()

    def _status(self):
        return {"started": self.started, "cluster": self.cluster, "watch": {k: dict(v) for k, v in self.watch_status.items()},
                "metrics": dict(self.metrics_status), "usage_at": self.usage_at,
                "counts": {"nodes": len(self.nodes), "namespaces": len(self.namespaces), "pods": len(self.pods),
                           "pdbs": len(self.pdbs)}}

    def snapshot(self):
        with self.cond:
            return {"seq": self.seq, "at": now_iso(), "status": self._status(),
                    "nodes": list(self.nodes.values()), "namespaces": list(self.namespaces.values()),
                    "pods": list(self.pods.values()), "pdbs": list(self.pdbs.values()),
                    "usage": self.usage, "node_usage": self.node_usage}

    def snapshot_json(self):
        """(body, gzip body) of the snapshot, serialized once per seq (many pages may open at once)."""
        with self.cond:
            seq, cached = self._snapshot_cache
            if seq == self.seq and cached is not None:
                return cached
        snap = self.snapshot()
        body = json.dumps(snap).encode()
        cached = (body, gzip.compress(body, compresslevel=4, mtime=0))
        with self.cond:
            self._snapshot_cache = (snap["seq"], cached)
        return cached

    def changes(self, since):
        """What changed after seq `since`: {"seq", "nodes"|"namespaces"|"pods"|"pdbs": {key: summary or null}, "usage"?,
        "status"}, or {"reset": true} when `since` is older than the log (the page then reloads the snapshot)."""
        with self.cond:
            if since == self.seq:
                return {"seq": self.seq, "status": self._status()}
            oldest = self.log[0][0] if self.log else self.seq + 1
            if since > self.seq or since < oldest - 1:
                return {"seq": self.seq, "reset": True}
            out = {"seq": self.seq, "nodes": {}, "namespaces": {}, "pods": {}, "pdbs": {}, "status": self._status()}
            for seq, kind, key, value in self.log:
                if seq > since and kind in ("nodes", "namespaces", "pods", "pdbs"):
                    out[kind][key] = value
            for kind in ("nodes", "namespaces", "pods", "pdbs"):
                if not out[kind]:
                    del out[kind]
            if self.usage_seq > since:
                out["usage"], out["node_usage"], out["usage_at"] = self.usage, self.node_usage, self.usage_at
            return out

    def wait(self, since, timeout):
        """Block until seq > since (or timeout). Returns the current seq."""
        with self.cond:
            if self.seq <= since:
                self.cond.wait(timeout)
            return self.seq


# ---------------------------------------------------------------------------
# kubectl watch / poll threads
# ---------------------------------------------------------------------------

class Watcher(threading.Thread):
    """List + watch one resource through kubectl and feed a LiveState, forever."""

    def __init__(self, state, kind, context=None, kubeconfig=None, kubectl_cmd=("kubectl",), resync=RESYNC):
        super().__init__(daemon=True, name=f"watch-{kind}")
        self.state, self.kind, self.path = state, kind, WATCHED[kind]
        self.cmd = list(kubectl_cmd) + (["--kubeconfig", kubeconfig] if kubeconfig else []) + (["--context", context] if context else [])
        self.resync = resync
        self.stop = threading.Event()
        self.proc = None

    def run(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                self.state.set_watch_status(self.kind, "listing")
                rv = self.relist()
                self.state.set_watch_status(self.kind, "watching")
                self.watch(rv)
                backoff = 1
            except Exception as e:  # kubectl error, bad JSON, API error: report it and retry
                msg = discover.first_line(e)
                discover.log(f"live: {self.kind} watch: {msg}")
                self.state.set_watch_status(self.kind, "error", msg)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def relist(self):
        out = subprocess.run(self.cmd + ["get", "--raw", self.path], capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise discover.KubectlError(out.stderr.strip() or f"exit {out.returncode}")
        body = json.loads(out.stdout)
        self.state.replace_all(self.kind, body.get("items") or [])
        return (body.get("metadata") or {}).get("resourceVersion") or "0"

    def watch(self, rv):
        """Follow the watch stream from resourceVersion rv until the server closes it (resync) or an error ends it."""
        url = f"{self.path}?watch=1&resourceVersion={rv}&allowWatchBookmarks=true&timeoutSeconds={self.resync}"
        self.proc = subprocess.Popen(self.cmd + ["get", "--raw", url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        try:
            while not self.stop.is_set():
                line = self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                etype, obj = event.get("type"), event.get("object") or {}
                if etype == "ERROR":
                    raise discover.KubectlError(f"watch error: {obj.get('message') or obj.get('reason') or obj}")
                if etype in ("ADDED", "MODIFIED", "DELETED"):
                    self.state.apply(self.kind, etype, obj)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()
            err = (self.proc.stderr.read() or "").strip()
            self.proc.stdout.close()
            self.proc.stderr.close()
        if self.proc.returncode not in (0, -9) and err:
            raise discover.KubectlError(err)

    def close(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class MetricsPoller(threading.Thread):
    """metrics.k8s.io usage of pods (per container) and nodes every `interval` seconds."""

    def __init__(self, state, context=None, kubeconfig=None, kubectl_cmd=("kubectl",), interval=DEFAULT_METRICS_INTERVAL):
        super().__init__(daemon=True, name="metrics-poll")
        self.state = state
        self.cmd = list(kubectl_cmd) + (["--kubeconfig", kubeconfig] if kubeconfig else []) + (["--context", context] if context else [])
        self.interval = interval
        self.stop = threading.Event()

    def _raw(self, path):
        out = subprocess.run(self.cmd + ["get", "--raw", path], capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise discover.KubectlError(out.stderr.strip() or f"exit {out.returncode}")
        return json.loads(out.stdout).get("items") or []

    def poll_once(self):
        pods = self._raw(f"{METRICS_PATH}/pods")
        try:
            nodes = self._raw(f"{METRICS_PATH}/nodes")
        except (discover.KubectlError, json.JSONDecodeError):
            nodes = []
        usage = {}
        for item in pods:
            md = item.get("metadata") or {}
            usage[pod_key(md.get("namespace", ""), md.get("name", ""))] = {
                c.get("name"): [discover.parse_quantity((c.get("usage") or {}).get("cpu")),
                                discover.parse_quantity((c.get("usage") or {}).get("memory"))]
                for c in item.get("containers") or []}
        node_usage = {(n.get("metadata") or {}).get("name", ""): [discover.parse_quantity((n.get("usage") or {}).get("cpu")),
                                                                  discover.parse_quantity((n.get("usage") or {}).get("memory"))]
                      for n in nodes}
        self.state.set_usage(usage, node_usage)

    def run(self):
        while not self.stop.is_set():
            try:
                self.poll_once()
                wait = self.interval
            except Exception as e:  # metrics-server missing or slow: the page shows n/a, we keep trying
                self.state.set_metrics_error(discover.first_line(e))
                wait = max(self.interval * 4, 60)
            self.stop.wait(wait)

    def close(self):
        self.stop.set()


def start(state, context=None, kubeconfig=None, kubectl_cmd=("kubectl",), metrics_interval=None):
    """Start the three watchers and the metrics poller for one cluster. Returns the threads."""
    threads = [Watcher(state, kind, context, kubeconfig, kubectl_cmd) for kind in WATCHED]
    threads.append(MetricsPoller(state, context, kubeconfig, kubectl_cmd, metrics_interval or state.metrics_interval))
    for t in threads:
        t.start()
    return threads
