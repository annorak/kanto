{{/*
Service-specific helpers.

The kanto-common library chart provides every named template we
need for the pod baseline. The only thing this helper file does is
flatten the Growlithe-specific knobs from values.yaml into a list of
env vars consumed by the deployment. Keeping it here (not in the
library) means a future second consumer of kanto-common doesn't
inherit growlithe-shaped surface.
*/}}

{{- define "kanto-growlithe.serviceEnv" -}}
# Growlithe-specific configuration block. See growlithe.config for
# what each var maps to.
{{- if .Values.growlithe.organisms }}
- name: KANTO_GROWLITHE_ORGANISMS
  value: {{ .Values.growlithe.organisms | quote }}
{{- end }}
- name: KANTO_GROWLITHE_POLL_INTERVAL_SECONDS
  value: {{ .Values.growlithe.pollIntervalSeconds | quote }}
- name: KANTO_GROWLITHE_POLL_JITTER_SECONDS
  value: {{ .Values.growlithe.pollJitterSeconds | quote }}
- name: KANTO_GROWLITHE_NCBI_BASE_URL
  value: {{ .Values.growlithe.ncbiBaseUrl | quote }}
- name: KANTO_GROWLITHE_NCBI_REQUEST_TIMEOUT_SECONDS
  value: {{ .Values.growlithe.ncbiRequestTimeoutSeconds | quote }}
- name: KANTO_GROWLITHE_NCBI_MAX_ATTEMPTS
  value: {{ .Values.growlithe.ncbiMaxAttempts | quote }}
- name: KANTO_GROWLITHE_NCBI_MAX_CONCURRENT
  value: {{ .Values.growlithe.ncbiMaxConcurrent | quote }}
- name: KANTO_GROWLITHE_RUN_ONCE
  value: {{ .Values.growlithe.runOnce | quote }}
- name: KANTO_GROWLITHE_HEALTH_PORT
  value: {{ .Values.probes.port | quote }}
- name: KANTO_GROWLITHE_METRICS_PORT
  value: {{ .Values.service.metricsPort | quote }}

# Object Storage (Azure Blob)
- name: KANTO_OS_ACCOUNT_URL
  value: {{ required "objectStorage.accountUrl is required" .Values.objectStorage.accountUrl | quote }}
- name: KANTO_OS_PROTEINS_CONTAINER
  value: {{ .Values.objectStorage.proteinsContainer | quote }}
- name: KANTO_OS_EMBEDDINGS_CONTAINER
  value: {{ .Values.objectStorage.embeddingsContainer | quote }}
- name: KANTO_OS_METADATA_CONTAINER
  value: {{ .Values.objectStorage.metadataContainer | quote }}

# Mew (Azure Postgres Flexible Server)
- name: KANTO_MEW_HOST
  value: {{ required "mew.host is required" .Values.mew.host | quote }}
- name: KANTO_MEW_PORT
  value: {{ .Values.mew.port | quote }}
- name: KANTO_MEW_DATABASE
  value: {{ .Values.mew.database | quote }}
- name: KANTO_MEW_USER
  value: {{ .Values.mew.user | quote }}
- name: KANTO_MEW_SSLMODE
  value: {{ .Values.mew.sslmode | quote }}
# KANTO_MEW_PASSWORD comes from the K8s Secret synced by the
# SecretProviderClass; see _env.tpl in kanto-common.

# Event Hubs (Kafka API)
- name: KANTO_STREAMING_BOOTSTRAP_SERVERS
  value: {{ required "streaming.bootstrapServers is required" .Values.streaming.bootstrapServers | quote }}
- name: KANTO_STREAMING_DISCOVERED_TOPIC
  value: {{ .Values.streaming.discoveredTopic | quote }}
- name: KANTO_STREAMING_SASL_USERNAME
  value: {{ .Values.streaming.saslUsername | quote }}
# KANTO_STREAMING_SASL_PASSWORD comes from the K8s Secret synced by
# the SecretProviderClass.
{{- end -}}
