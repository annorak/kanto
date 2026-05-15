{{/*
Standard env vars surfaced to every kanto-commons-based service.

  KANTO_ENV, KANTO_LOG_LEVEL, KANTO_LOG_FORMAT — universal config.
  KANTO_SERVICE_NAME                          — from chart name.
  KANTO_OTEL_ENDPOINT, KANTO_OTEL_SAMPLE_RATE — OTel SDK targets.
  OTEL_EXPORTER_OTLP_*                        — also surfaced for libs
                                                that read upstream env.
  POD_NAME, POD_NAMESPACE, NODE_NAME          — k8s metadata via fieldRef.

Then, for every entry in .Values.keyVault.secrets that has `envVar`
set, render an env entry that pulls from the K8s Secret the
SecretProviderClass syncs.
*/}}

{{- define "kanto-common.standardEnv" -}}
- name: KANTO_ENV
  value: {{ .Values.kanto.environment | quote }}
- name: KANTO_LOG_LEVEL
  value: {{ .Values.kanto.logLevel | quote }}
- name: KANTO_LOG_FORMAT
  value: {{ .Values.kanto.logFormat | quote }}
- name: KANTO_SERVICE_NAME
  value: {{ include "kanto-common.name" . | quote }}
- name: KANTO_OTEL_ENDPOINT
  value: {{ .Values.otel.endpoint | quote }}
- name: KANTO_OTEL_SAMPLE_RATE
  value: {{ .Values.otel.sampleRate | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.otel.endpoint | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: {{ .Values.otel.protocol | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ include "kanto-common.name" . | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "service.namespace=kanto,deployment.environment={{ .Values.kanto.environment }}"
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
{{- range .Values.keyVault.secrets }}
{{- if and .envVar (not .file) }}
- name: {{ .envVar | quote }}
  valueFrom:
    secretKeyRef:
      name: {{ include "kanto-common.syncedSecretName" $ }}
      key: {{ .envVar | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Standard volume + volumeMount pair for the CSI secrets-store mount.
Only renders when .Values.keyVault.secrets is non-empty.
*/}}
{{- define "kanto-common.secretsVolume" -}}
{{- if .Values.keyVault.secrets }}
- name: kanto-secrets
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ include "kanto-common.secretProviderClassName" . }}
{{- end }}
{{- end -}}

{{- define "kanto-common.secretsVolumeMount" -}}
{{- if .Values.keyVault.secrets }}
- name: kanto-secrets
  mountPath: /var/run/secrets/kanto
  readOnly: true
{{- end }}
{{- end -}}
