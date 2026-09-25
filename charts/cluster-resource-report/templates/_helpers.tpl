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
