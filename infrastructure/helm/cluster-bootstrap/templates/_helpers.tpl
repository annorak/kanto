{{- define "bootstrap.commonLabels" -}}
app.kubernetes.io/name: kanto-cluster-bootstrap
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: kanto
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
kanto.io/environment: {{ .Values.environment | quote }}
{{- end -}}
