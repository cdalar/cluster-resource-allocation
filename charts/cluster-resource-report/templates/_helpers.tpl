{{- define "crr.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "crr.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
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
