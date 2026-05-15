{{/*
ConfigMap helper for non-secret service config. Optional — services
that only need env vars can skip this and inline values in their
deployment env.

Caller usage:

  {{ include "kanto-common.configMap" (dict "ctx" . "data" .Values.config) }}

The map at .data is rendered verbatim into .data on the ConfigMap.
Values are stringified by Helm.
*/}}

{{- define "kanto-common.configMap" -}}
{{- $ctx := .ctx -}}
{{- $data := .data -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "kanto-common.fullname" $ctx }}-config
  namespace: {{ $ctx.Release.Namespace }}
  labels:
    {{- include "kanto-common.labels" $ctx | nindent 4 }}
data:
{{- range $k, $v := $data }}
  {{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}
