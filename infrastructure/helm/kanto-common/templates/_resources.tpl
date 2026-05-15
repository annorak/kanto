{{/*
Resource presets. Pick a preset name in values.yaml; override
individual fields via `resources.overrides`.

Convention: CPU REQUEST only, no CPU limit. CPU limits cause throttling
under load even when the node has spare capacity. Memory has both
request and limit set (memory is incompressible — OOM-kill is the
correct response).

Presets reflect the smallest reasonable footprint for each tier; bump
up only when measured load justifies it.
*/}}

{{- define "kanto-common.resources" -}}
{{- $preset := default "small" .Values.resources.preset -}}
{{- $defaults := dict
  "small"   (dict "cpu" "100m"  "memory" "256Mi" "memoryLimit" "512Mi")
  "medium"  (dict "cpu" "250m"  "memory" "512Mi" "memoryLimit" "1Gi")
  "large"   (dict "cpu" "500m"  "memory" "1Gi"   "memoryLimit" "2Gi")
  "xlarge"  (dict "cpu" "1000m" "memory" "2Gi"   "memoryLimit" "4Gi")
-}}
{{- $values := get $defaults $preset -}}
{{- if not $values -}}
  {{- fail (printf "Unknown resources.preset %q. Use one of: small, medium, large, xlarge." $preset) -}}
{{- end -}}
{{- $overrides := default (dict) .Values.resources.overrides -}}
{{- $reqOver := default (dict) (get $overrides "requests") -}}
{{- $limOver := default (dict) (get $overrides "limits") -}}
requests:
  cpu: {{ default $values.cpu (get $reqOver "cpu") | quote }}
  memory: {{ default $values.memory (get $reqOver "memory") | quote }}
limits:
  memory: {{ default $values.memoryLimit (get $limOver "memory") | quote }}
{{- end -}}
