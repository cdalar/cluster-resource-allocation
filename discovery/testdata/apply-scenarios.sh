#!/usr/bin/env bash
# Put the test scenarios (scenarios.yaml, README.md) on a throwaway cluster, or remove them again.
#
#   ./apply-scenarios.sh apply      # namespaces + workloads on the current kubectl context (CONTEXT=... to choose)
#   ./apply-scenarios.sh delete     # remove everything labelled resource-report/scenario=true
#   ./apply-scenarios.sh projects   # Rancher local cluster only: look up / create the Projects, print their IDs
#   ./apply-scenarios.sh print      # the rendered namespaces and workloads, nothing applied
#   ./apply-scenarios.sh status     # scenario namespaces with their pods and quota events
#
# How namespaces are grouped into projects (PROJECT_MODE):
#   rancher  Rancher Projects payments, crm, search (with a Rancher Project quota), platform-tools and Default.
#            Needs CLUSTER_ID (or CLUSTER_NAME) and the Project IDs, either as PROJECT_PAYMENTS=p-xxxxx ... or looked
#            up and created on the Rancher local cluster: LOCAL_CONTEXT=<context of the local cluster> (or
#            LOCAL_KUBECONFIG=<file>). Without access to both clusters from one place, run `projects` against the
#            local cluster first and paste the exports it prints before `apply`.
#   label    Namespace label PROJECT_LABEL=<name> (default "project"), for clusters outside Rancher; install the
#            chart with collection.projectLabel set to the same label.
#   none     No grouping: every namespace is unassigned.
#   Default: rancher when CLUSTER_ID, CLUSTER_NAME or LOCAL_CONTEXT is set, otherwise label.
#
# Other settings: OVERHEAD=0 skips the RuntimeClass scenario (needs the runtime handler "runc"); N1_BREACH=0 skips
# sizing the N+1 filler; DELETE_PROJECTS=1 with `delete` also removes the Rancher Projects this script created.
#
# Writes to the cluster (and with LOCAL_CONTEXT, Rancher Projects on the local cluster): use a test cluster, never
# production.
set -euo pipefail

ACTION="${1:-apply}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="resource-report/scenario"
KC=(kubectl ${CONTEXT:+--context "$CONTEXT"})
LK=(kubectl ${LOCAL_CONTEXT:+--context "$LOCAL_CONTEXT"} ${LOCAL_KUBECONFIG:+--kubeconfig "$LOCAL_KUBECONFIG"})
PROJECT_LABEL="${PROJECT_LABEL:-project}"
if [ -z "${PROJECT_MODE:-}" ]; then
    if [ -n "${CLUSTER_ID:-}${CLUSTER_NAME:-}${LOCAL_CONTEXT:-}${LOCAL_KUBECONFIG:-}" ]; then PROJECT_MODE=rancher; else PROJECT_MODE=label; fi
fi

# namespace -> project ("-" = in no project)
NAMESPACES="
payments-api payments
payments-batch payments
payments-worker payments
payments-sidecar payments
payments-overhead payments
payments-hpa payments
payments-cron payments
payments-bigjob payments
payments-failed payments
payments-agent payments
payments-stateful payments
payments-guaranteed payments
payments-overcommit payments
payments-reserve payments
crm-web crm
crm-search crm
crm-reports crm
crm-legacy crm
crm-quota crm
crm-quota-full crm
crm-quota-strict crm
crm-idle crm
search-app search
search-batch search
platform-dns platform-tools
platform-logging platform-tools
demo-ingress -
legacy-app -
sandbox Default
"
CREATED_PROJECTS="payments crm search platform-tools"   # created when missing; Default must exist

die() { echo "error: $*" >&2; exit 1; }
local_access() { [ "$ACTION" = projects ] || [ -n "${LOCAL_CONTEXT:-}${LOCAL_KUBECONFIG:-}" ]; }
project_var() { echo "PROJECT_$(echo "$1" | tr 'a-z-' 'A-Z_')"; }
by_display_name() {  # <resource> <display name> [namespace] -> metadata.name
    "${LK[@]}" ${3:+-n "$3"} get "$1" -o json | python3 -c '
import json, sys
print(next((o["metadata"]["name"] for o in json.load(sys.stdin)["items"]
            if (o.get("spec") or {}).get("displayName") == sys.argv[1]), ""))' "$2"
}

resolve_cluster() {
    [ -n "${CLUSTER_ID:-}" ] && return
    [ -n "${CLUSTER_NAME:-}" ] || die "PROJECT_MODE=rancher needs CLUSTER_ID or CLUSTER_NAME"
    local_access || die "CLUSTER_NAME needs LOCAL_CONTEXT (or LOCAL_KUBECONFIG) to look up the cluster ID"
    CLUSTER_ID="$(by_display_name clusters.management.cattle.io "$CLUSTER_NAME")"
    [ -n "$CLUSTER_ID" ] || die "no Rancher cluster named $CLUSTER_NAME"
}

create_project() {  # <display name> -> id
    local quota=""
    if [ "$1" = search ]; then  # Rancher Project quota: 1 CPU for the project, 500m per namespace, container defaults
        quota="  resourceQuota: {limit: {requestsCpu: 1000m, requestsMemory: 4096Mi, limitsMemory: 8192Mi}}
  namespaceDefaultResourceQuota: {limit: {requestsCpu: 500m, requestsMemory: 2048Mi, limitsMemory: 4096Mi}}
  containerDefaultResourceLimit: {requestsCpu: 50m, requestsMemory: 64Mi, limitsMemory: 128Mi}"
    fi
    "${LK[@]}" create -o name -f - <<EOF | sed 's|.*/||'
apiVersion: management.cattle.io/v3
kind: Project
metadata:
  generateName: p-
  namespace: $CLUSTER_ID
  labels: {$LABEL: "true"}
spec:
  clusterName: $CLUSTER_ID
  displayName: $1
  description: resource-report test scenarios
$quota
EOF
}

resolve_projects() {
    resolve_cluster
    local p var id
    for p in $CREATED_PROJECTS Default; do
        var="$(project_var "$p")"
        id="${!var:-}"
        if [ -z "$id" ]; then
            local_access || die "missing $var (or set LOCAL_CONTEXT to look it up)"
            id="$(by_display_name projects.management.cattle.io "$p" "$CLUSTER_ID")"
            if [ -z "$id" ]; then
                [ "$p" != Default ] || die "no Default project on cluster $CLUSTER_ID"
                id="$(create_project "$p")"
                echo "created Rancher Project $p ($id)" >&2
            fi
        fi
        printf -v "$var" '%s' "$id"
    done
    if [ -z "${RANCHER_QUOTA:-}" ]; then  # does Rancher manage the search namespaces' quota?
        RANCHER_QUOTA=0
        if local_access && [ -n "$("${LK[@]}" -n "$CLUSTER_ID" get projects.management.cattle.io "$PROJECT_SEARCH" \
                -o jsonpath='{.spec.namespaceDefaultResourceQuota.limit}')" ]; then RANCHER_QUOTA=1; fi
    fi
}

namespaces_yaml() {
    local ns p var
    while read -r ns p; do
        [ -n "$ns" ] || continue
        [ "${OVERHEAD:-1}" != 0 ] || [ "$ns" != payments-overhead ] || continue
        echo "---"
        echo "apiVersion: v1"
        echo "kind: Namespace"
        echo "metadata:"
        echo "  name: $ns"
        case "$PROJECT_MODE:$p" in
            *:-|none:*) echo "  labels: {$LABEL: \"true\"}" ;;
            rancher:*)
                var="$(project_var "$p")"
                echo "  labels: {$LABEL: \"true\", field.cattle.io/projectId: ${!var}}"
                echo "  annotations: {field.cattle.io/projectId: \"$CLUSTER_ID:${!var}\"}" ;;
            label:*) echo "  labels: {$LABEL: \"true\", $PROJECT_LABEL: $p}" ;;
        esac
    done <<< "$NAMESPACES"
}

# Namespace quota for the search project when Rancher doesn't manage it: what Rancher would create from the
# project's namespace default quota and container defaults.
search_quota_yaml() {
    local ns
    for ns in search-app search-batch; do
        cat <<EOF
---
apiVersion: v1
kind: ResourceQuota
metadata: {name: quota, namespace: $ns}
spec:
  hard: {requests.cpu: 500m, requests.memory: 2Gi, limits.memory: 4Gi}
---
apiVersion: v1
kind: LimitRange
metadata: {name: defaults, namespace: $ns}
spec:
  limits:
    - type: Container
      defaultRequest: {cpu: 50m, memory: 64Mi}
      default: {memory: 128Mi}
EOF
    done
}

cluster_py() {  # run python with discover.py importable, cluster JSON on stdin
    PYTHONPATH="$DIR/..${PYTHONPATH:+:$PYTHONPATH}" python3 -c "$1" "${@:2}"
}

big_cpu() {  # 1 CPU more than the largest schedulable node
    [ "$ACTION" = print ] && { echo "${BIG_CPU:-64}"; return; }
    "${KC[@]}" get nodes -o json | cluster_py '
import json, math, sys, discover
nodes = [n for n in json.load(sys.stdin)["items"] if discover.node_schedulable(n)]
print(math.floor(max(discover.parse_quantity(n["status"]["allocatable"]["cpu"]) for n in nodes)) + 1)'
}

workloads_yaml() {
    python3 - "$DIR/scenarios.yaml" "$1" "${OVERHEAD:-1}" <<'EOF'
import sys
text, big, overhead = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3] != "0"
docs = text.replace("${BIG_CPU}", big).split("\n---\n")
keep = [d for d in docs if overhead or ("scenario-overhead" not in d and "payments-overhead" not in d)]
print("\n---\n".join(keep))
EOF
}

size_filler() {
    # Scale payments-reserve so that project requests (tenant + unassigned, pending included, as the dashboard counts
    # them) end ~5 % above the N+1 room: schedulable allocatable - largest node - system requests.
    local replicas count last="" i
    for i in $(seq 1 24); do  # wait until the controllers have created their pods (count stable for 10 s)
        count="$("${KC[@]}" get pods -A --no-headers 2>/dev/null | wc -l)"
        [ "$count" = "$last" ] && [ "$i" -gt 2 ] && break
        last="$count"; sleep 5
    done
    replicas="$("${KC[@]}" get nodes,pods -A -o json | cluster_py '
import json, math, re, sys, discover
items = json.load(sys.stdin)["items"]
nodes = [o for o in items if o["kind"] == "Node" and discover.node_schedulable(o)]
pods = [o for o in items if o["kind"] == "Pod" and o["status"].get("phase") not in ("Succeeded", "Failed")]
system_ns = re.compile(discover.DEFAULT_SYSTEM_NS_REGEX)
platform = set(sys.argv[1].split())
cpu = lambda n: discover.parse_quantity(n["status"]["allocatable"]["cpu"])
alloc, largest = sum(map(cpu, nodes)), max(map(cpu, nodes))
system = projects = scheduled = 0.0
for p in pods:
    ns, req = p["metadata"]["namespace"], discover.pod_effective(p["spec"], "requests", "cpu")
    if ns == "payments-reserve":
        continue
    if system_ns.match(ns) or ns in platform:
        system += req
    else:
        projects += req
    if p["spec"].get("nodeName"):
        scheduled += req
room = alloc - largest - system
want = max(0.0, room * 1.05 + 0.25 - projects)
fits = max(0.0, alloc - scheduled - 0.5)          # keep the filler itself schedulable
n = math.ceil(min(want, fits) / 0.25)
print(f"N+1 room {room:.2f} CPU, projects request {projects:.2f}, filler {n} x 250m", file=sys.stderr)
if want > fits:
    print("  (capped by free capacity: the breach may come from Pending requests only)", file=sys.stderr)
print(n)' "platform-dns platform-logging")"
    "${KC[@]}" -n payments-reserve scale deployment reserve --replicas="$replicas" >/dev/null
}

wait_rancher_quota() {
    local i
    for i in $(seq 1 30); do
        if [ -n "$("${KC[@]}" -n search-batch get resourcequota -o name 2>/dev/null)" ] && \
           [ -n "$("${KC[@]}" -n search-app get resourcequota -o name 2>/dev/null)" ]; then return; fi
        sleep 3
    done
    echo "warning: Rancher did not create the search namespaces' quota within 90 s; the quota scenario may not apply" >&2
}

case "$ACTION" in
    projects)
        PROJECT_MODE=rancher resolve_projects
        echo "export CLUSTER_ID=$CLUSTER_ID"
        for p in $CREATED_PROJECTS Default; do var="$(project_var "$p")"; echo "export $var=${!var}"; done
        echo "export RANCHER_QUOTA=$RANCHER_QUOTA"
        ;;
    print|apply)
        [ "$PROJECT_MODE" != rancher ] || resolve_projects
        RANCHER_QUOTA="${RANCHER_QUOTA:-0}"
        [ "$PROJECT_MODE" = rancher ] || RANCHER_QUOTA=0
        big="$(big_cpu)"
        if [ "$ACTION" = print ]; then
            namespaces_yaml; [ "$RANCHER_QUOTA" = 1 ] || search_quota_yaml; workloads_yaml "$big"
            exit 0
        fi
        echo "project mode: $PROJECT_MODE; Pending workload asks for $big CPU" >&2
        namespaces_yaml | "${KC[@]}" apply -f -
        if [ "$RANCHER_QUOTA" = 1 ]; then wait_rancher_quota; else search_quota_yaml | "${KC[@]}" apply -f -; fi
        workloads_yaml "$big" | "${KC[@]}" apply -f -
        [ "${N1_BREACH:-1}" = 0 ] || size_filler
        ;;
    delete)
        "${KC[@]}" delete namespace -l "$LABEL=true" --ignore-not-found --timeout=5m
        "${KC[@]}" delete runtimeclass -l "$LABEL=true" --ignore-not-found
        if [ "${DELETE_PROJECTS:-0}" = 1 ]; then
            resolve_cluster
            "${LK[@]}" -n "$CLUSTER_ID" delete projects.management.cattle.io -l "$LABEL=true" --ignore-not-found
        fi
        ;;
    status)
        for ns in $("${KC[@]}" get ns -l "$LABEL=true" -o jsonpath='{.items[*].metadata.name}'); do
            printf '%-22s %s\n' "$ns" "$("${KC[@]}" -n "$ns" get pods --no-headers 2>/dev/null | \
                awk '{s[$3]++} END {for (k in s) printf "%s=%d ", k, s[k]}')"
        done
        echo "--- quota refusals:"
        "${KC[@]}" get events -A --field-selector reason=FailedCreate -o custom-columns=NS:.metadata.namespace,MESSAGE:.message \
            --no-headers 2>/dev/null | grep -E "^(crm|search)-" | cut -c1-160 | sort -u || true
        ;;
    *) echo "usage: $0 [apply|delete|projects|print|status]" >&2; exit 1 ;;
esac
