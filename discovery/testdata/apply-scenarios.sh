#!/usr/bin/env bash
# Fill in the Rancher IDs in scenarios.yaml and apply it (or delete it again).
#
#   CLUSTER_ID=c-m-xxxxxxxx PAYMENTS_PROJECT=p-aaaaa CRM_PROJECT=p-bbbbb DEFAULT_PROJECT=p-ccccc \
#     ./apply-scenarios.sh [apply|delete|print]
#
# The IDs are Rancher's: the downstream cluster's ID and the IDs of its Projects "payments", "crm" and "Default"
# (Rancher UI, or on the local cluster: kubectl -n <cluster-id> get projects.management.cattle.io). Uses the
# current kubectl context -- point it at a throwaway test cluster, never at production.
set -euo pipefail

ACTION="${1:-apply}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for v in CLUSTER_ID PAYMENTS_PROJECT CRM_PROJECT DEFAULT_PROJECT; do
    [ -n "${!v:-}" ] || { echo "missing $v (see the header of $0)" >&2; exit 1; }
done

render() {
    sed -e "s/\${CLUSTER_ID}/${CLUSTER_ID}/g" \
        -e "s/\${PAYMENTS_PROJECT}/${PAYMENTS_PROJECT}/g" \
        -e "s/\${CRM_PROJECT}/${CRM_PROJECT}/g" \
        -e "s/\${DEFAULT_PROJECT}/${DEFAULT_PROJECT}/g" \
        "$DIR/scenarios.yaml"
}

case "$ACTION" in
    print) render ;;
    apply)
        # namespaces first: the objects in them can't be created before
        render | kubectl apply -f - 2>/dev/null || true
        render | kubectl apply -f -
        ;;
    delete) render | kubectl delete --ignore-not-found -f - ;;
    *) echo "usage: $0 [apply|delete|print]" >&2; exit 1 ;;
esac
