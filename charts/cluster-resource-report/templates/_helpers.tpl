{{/* The name used in object names and selector labels. Fixed to cluster-resource-report: the chart was published
     as cluster-resource-report up to 0.6.2 and is cluster-resource-report-chart since 0.6.3 (one tile in Rancher
     Apps with the Docker Hub repository). Keeping this name keeps object names, the Deployment selector (immutable)
     and the plan ConfigMaps of existing installs unchanged. */}}
{{- define "crr.name" -}}
cluster-resource-report
{{- end -}}

{{- define "crr.fullname" -}}
{{- $name := include "crr.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "crr.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{ include "crr.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "crr.selectorLabels" -}}
app.kubernetes.io/name: {{ include "crr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* true when Prometheus is queried through the API server service proxy */}}
{{- define "crr.promProxy" -}}
{{- if and .Values.prometheus.enabled (not .Values.prometheus.url) -}}true{{- end -}}
{{- end -}}

{{- define "crr.promNamespace" -}}
{{- $parts := splitList "/" .Values.prometheus.service -}}
{{- if ne (len $parts) 2 -}}{{- fail "prometheus.service must be <namespace>/<scheme>:<service>:<port>" -}}{{- end -}}
{{- first $parts -}}
{{- end -}}

{{- define "crr.promServiceName" -}}
{{- last (splitList "/" .Values.prometheus.service) -}}
{{- end -}}

{{- define "crr.namesNamespace" -}}
{{- dig "namesConfigMap" "namespace" "" .Values.rancher | default .Release.Namespace -}}
{{- end -}}

{{/* registry without scheme or trailing slash; "" = Docker Hub */}}
{{- define "crr.registry" -}}
{{- .Values.registry | default "" | trimPrefix "oci://" | trimPrefix "https://" | trimPrefix "http://" | trimSuffix "/" -}}
{{- end }}

{{/* Image repository: image.repository when set, else <registry>/cdalar/cluster-resource-report (Docker Hub path) */}}
{{- define "crr.imageRepository" -}}
{{- .Values.image.repository | default (printf "%s/cdalar/cluster-resource-report" (include "crr.registry" . | default "docker.io")) -}}
{{- end }}

{{/* Image with tag (default: the chart's appVersion) */}}
{{- define "crr.image" -}}
{{- printf "%s:%s" (include "crr.imageRepository" .) (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end }}

{{/* Chart for the Fleet HelmOp: rancher.deployDownstream.chart.repo when set, else
     oci://<registry>/cdalar/cluster-resource-report-chart (Docker Hub's registry host is registry-1.docker.io) */}}
{{- define "crr.chartRepo" -}}
{{- $r := include "crr.registry" . -}}
{{- if or (not $r) (eq $r "docker.io") (eq $r "index.docker.io") }}{{ $r = "registry-1.docker.io" }}{{ end -}}
{{- dig "deployDownstream" "chart" "repo" "" .Values.rancher | default (printf "oci://%s/cdalar/cluster-resource-report-chart" $r) -}}
{{- end }}

{{/* iconSrc of a NavLink: rancher.navLink.icons.<name> when set (an image URL or data: URI), else the chart's
     icons/<name>.svg as a data: URI, so it also works on air-gapped clusters. Call with (list . "<name>"). */}}
{{- define "crr.navLinkIcon" -}}
{{- $root := index . 0 }}{{- $name := index . 1 -}}
{{- dig "navLink" "icons" $name "" $root.Values.rancher | default (printf "data:image/svg+xml;base64,%s" ($root.Files.Get (printf "icons/%s.svg" $name) | b64enc)) -}}
{{- end }}

{{/* Non-empty on the Rancher local cluster. rancher.isLocalCluster: true / false, or auto (the default): local when
     the cluster serves Rancher's Project API and runs the Rancher server (Deployment cattle-system/rancher).
     `lookup` sees nothing in `helm template` or a client-side dry run, so auto then means "not local". */}}
{{- define "crr.isLocal" -}}
{{- $v := toString .Values.rancher.isLocalCluster -}}
{{- if eq $v "true" -}}
true
{{- else if eq $v "auto" -}}
{{- if and (.Capabilities.APIVersions.Has "management.cattle.io/v3/Project") (lookup "apps/v1" "Deployment" "cattle-system" "rancher") -}}
true
{{- end -}}
{{- end -}}
{{- end }}

{{/* Non-empty when the Rancher inventory is readable: on the local cluster, or through a local kubeconfig */}}
{{- define "crr.hasInventory" -}}
{{- if or (include "crr.isLocal" .) (dig "localKubeconfigSecret" "name" "" .Values.rancher) -}}
true
{{- end -}}
{{- end }}

{{/* Non-empty when the allocation planner runs: enabled (the default) and the Rancher inventory is readable */}}
{{- define "crr.planner" -}}
{{- if and (dig "enabled" true (.Values.planner | default dict)) (include "crr.hasInventory" .) -}}
true
{{- end -}}
{{- end }}

{{/* Non-empty when the capacity planner runs: same conditions */}}
{{- define "crr.capacityPlanner" -}}
{{- if and (dig "enabled" true (.Values.capacityPlanner | default dict)) (include "crr.hasInventory" .) -}}
true
{{- end -}}
{{- end }}

{{/* Non-empty when the allocation planner or the capacity planner runs */}}
{{- define "crr.planners" -}}
{{- if or (include "crr.planner" .) (include "crr.capacityPlanner" .) -}}
true
{{- end -}}
{{- end }}

{{/* Fleet targets of the downstream HelmOp, without values, as JSON (empty when the chart renders no HelmOp):
     the clusters it lists, its clusterSelector and its ClusterGroup. The names Bundle uses them by default, so the
     names reach the same clusters as the dashboard. Keep in sync with the targets in fleet-helmop.yaml. */}}
{{- define "crr.downstreamTargets" -}}
{{- if and (dig "deployDownstream" "enabled" true .Values.rancher) (include "crr.isLocal" .) (.Capabilities.APIVersions.Has "fleet.cattle.io/v1alpha1/HelmOp") -}}
{{- $d := .Values.rancher.deployDownstream | default dict -}}
{{- $targets := list -}}
{{- range ($d.clusters | default list) -}}
{{- if kindIs "string" . -}}
{{- $targets = append $targets (dict "clusterName" .) -}}
{{- else if and (kindIs "map" .) .name -}}
{{- $targets = append $targets (dict "clusterName" .name) -}}
{{- end -}}
{{- end -}}
{{- if kindIs "map" $d.clusterSelector -}}
{{- $targets = append $targets (dict "clusterSelector" $d.clusterSelector) -}}
{{- end -}}
{{- $group := "" -}}
{{- if kindIs "map" $d.clusterGroup -}}
{{- $group = $d.clusterGroup.name | default "resource-report" -}}
{{- else if $d.clusterGroup -}}
{{- $group = $d.clusterGroup -}}
{{- end -}}
{{- if $group -}}
{{- $targets = append $targets (dict "clusterGroup" $group) -}}
{{- end -}}
{{- if $targets -}}
{{- toJson $targets -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/* PriorityClass of the dashboard pod: priorityClassName, else the chart's own (priorityClass.create), else none */}}
{{- define "crr.priorityClassName" -}}
{{- if .Values.priorityClassName -}}
{{- .Values.priorityClassName -}}
{{- else if .Values.priorityClass.create -}}
{{- include "crr.fullname" . -}}
{{- end -}}
{{- end }}
