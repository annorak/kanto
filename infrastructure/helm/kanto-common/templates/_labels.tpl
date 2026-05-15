{{/*
Kubernetes recommended labels.
https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/

Two flavors:
  kanto-common.labels         — full metadata.labels set (chart + selector + version + part-of).
  kanto-common.selectorLabels — pod-stable subset for matchLabels.

Selector labels MUST be stable across upgrades. Don't add app.kubernetes.io/version
to the selector — bumping the image tag would orphan all pods.
*/}}

{{- define "kanto-common.labels" -}}
helm.sh/chart: {{ include "kanto-common.chart" . }}
{{ include "kanto-common.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: kanto
kanto.io/service: {{ include "kanto-common.name" . }}
kanto.io/environment: {{ .Values.kanto.environment | quote }}
{{- end -}}

{{- define "kanto-common.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kanto-common.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
