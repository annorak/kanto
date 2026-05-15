{{/*
Liveness/readiness probes. kanto-commons exposes /healthz and /readyz
on the health port (default 8081). Services may override the port and
paths in values.yaml.

Convention: BOTH probes are always defined. Omitting them causes pods
to be considered Ready as soon as the container starts — that's
incompatible with our deploy-with-zero-downtime rolling-update strategy.
*/}}

{{- define "kanto-common.livenessProbe" -}}
httpGet:
  path: {{ .Values.probes.liveness.path | quote }}
  port: {{ .Values.probes.port }}
  scheme: HTTP
initialDelaySeconds: {{ .Values.probes.liveness.initialDelaySeconds }}
periodSeconds: {{ .Values.probes.liveness.periodSeconds }}
timeoutSeconds: {{ .Values.probes.liveness.timeoutSeconds }}
failureThreshold: {{ .Values.probes.liveness.failureThreshold }}
{{- end -}}

{{- define "kanto-common.readinessProbe" -}}
httpGet:
  path: {{ .Values.probes.readiness.path | quote }}
  port: {{ .Values.probes.port }}
  scheme: HTTP
initialDelaySeconds: {{ .Values.probes.readiness.initialDelaySeconds }}
periodSeconds: {{ .Values.probes.readiness.periodSeconds }}
timeoutSeconds: {{ .Values.probes.readiness.timeoutSeconds }}
failureThreshold: {{ .Values.probes.readiness.failureThreshold }}
{{- end -}}
