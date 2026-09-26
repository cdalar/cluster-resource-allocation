#!/usr/bin/env python3
"""Publish Rancher Project and cluster display names to downstream clusters, without a token on them.

Runs against the Rancher local cluster: reads projects/clusters.management.cattle.io and applies one Fleet
Bundle whose only resource is a ConfigMap (default: resource-report/rancher-project-names) with two keys,
projects.json ("<cluster-id>:<project-id>" -> name) and clusters.json (cluster-id -> name). Fleet delivers it
to the targeted downstream clusters, where the report reads it with --rancher-names-configmap.

This is the only part of the tooling that writes, and only that one Bundle. No GitRepo is involved.

    ./publish_rancher_names.py --context rancher-local            # apply
    ./publish_rancher_names.py --context rancher-local --dry-run  # print the Bundle
"""
import argparse
import json
import subprocess
import sys

import discover

BUNDLE_NAME = discover.NAMES_CONFIGMAP


def build_bundle(projects, clusters, workspace, target_namespace, cluster_selector):
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        # not app.kubernetes.io/managed-by: Fleet deploys with Helm, which sets that label itself
        "metadata": {"name": discover.NAMES_CONFIGMAP, "namespace": target_namespace,
                     "labels": {"app.kubernetes.io/part-of": "cluster-resource-report"}},
        "data": {"projects.json": json.dumps(projects, sort_keys=True),
                 "clusters.json": json.dumps(clusters, sort_keys=True)},
    }
    return {
        "apiVersion": "fleet.cattle.io/v1alpha1",
        "kind": "Bundle",
        "metadata": {"name": BUNDLE_NAME, "namespace": workspace,
                     "labels": {"app.kubernetes.io/managed-by": "cluster-resource-report"}},
        "spec": {
            "defaultNamespace": target_namespace,
            "resources": [{"name": "configmap.yaml", "content": json.dumps(configmap, indent=1)}],
            "targets": [{"clusterSelector": cluster_selector}],
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", help="context of the Rancher local cluster (default: current, or in-cluster)")
    ap.add_argument("--workspace", default="fleet-default",
                    help="Fleet workspace (namespace) for the Bundle (default: fleet-default = all downstream clusters)")
    ap.add_argument("--target-namespace", default="resource-report",
                    help="namespace of the ConfigMap on the downstream clusters (default: resource-report)")
    ap.add_argument("--cluster-selector", default="{}",
                    help='Fleet clusterSelector as JSON (default: {} = every cluster in the workspace), '
                         'e.g. \'{"matchLabels": {"env": "test"}}\'')
    ap.add_argument("--dry-run", action="store_true", help="print the Bundle instead of applying it")
    args = ap.parse_args()

    try:
        selector = json.loads(args.cluster_selector)
    except json.JSONDecodeError as e:
        sys.exit(f"--cluster-selector: invalid JSON: {e}")
    try:
        projects, clusters = discover.load_rancher_projects(args.context)
    except discover.KubectlError as e:
        sys.exit(f"could not read Rancher projects: {discover.first_line(e)}")
    if not projects:
        sys.exit("no Rancher projects found; is this the Rancher local cluster?")

    bundle = json.dumps(build_bundle(projects, clusters, args.workspace, args.target_namespace, selector), indent=1)
    if args.dry_run:
        print(bundle)
        return
    cmd = ["kubectl"] + (["--context", args.context] if args.context else []) + ["apply", "-f", "-"]
    p = subprocess.run(cmd, input=bundle, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        sys.exit(f"kubectl apply failed: {p.stderr.strip()}")
    print(f"{p.stdout.strip()} ({len(projects)} projects, {len(clusters)} clusters -> "
          f"{args.target_namespace}/{discover.NAMES_CONFIGMAP} in workspace {args.workspace})", flush=True)


if __name__ == "__main__":
    main()
