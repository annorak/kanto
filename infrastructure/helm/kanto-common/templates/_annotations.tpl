{{/*
Pod annotations: Prometheus scrape config + workload-identity opt-in +
user overrides. OTel instrumentation is wired via env vars (not
annotations), since we use the SDK directly from kanto-commons.

Workload identity is opt-in via the label `azure.workload.identity/use: "true"`
on the *pod*, but the federated credential is attached at the
ServiceAccount level. See _serviceaccount.tpl.
*/}}

{{- define "kanto-common.podAnnotations" -}}
{{- with .Values.prometheus -}}
{{- if .scrape }}
prometheus.io/scrape: "true"
prometheus.io/port: "{{ .port }}"
prometheus.io/path: {{ .path | quote }}
{{- end }}
{{- end }}
kanto.io/chart-version: {{ $.Chart.Version | quote }}
{{- range $k, $v := .Values.pod.annotations }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}

{{/*
Pod-level labels added on top of selector labels. Workload-identity
opt-in goes here; the AAD pod identity webhook reads this label and
mutates the pod with an azwi-projected SA token.
*/}}
{{- define "kanto-common.podLabels" -}}
{{ include "kanto-common.selectorLabels" . }}
{{- if .Values.workloadIdentity.enabled }}
azure.workload.identity/use: "true"
{{- end }}
{{- range $k, $v := .Values.pod.labels }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}
