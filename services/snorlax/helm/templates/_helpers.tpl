{{/*
Service-specific helpers — flatten the Snorlax knobs from values.yaml
into env vars consumed by the StatefulSet.
*/}}

{{- define "kanto-snorlax.replicaCount" -}}
{{- mul (int .Values.snorlax.partitionCount) (int .Values.snorlax.replicasPerPartition) -}}
{{- end -}}

{{- define "kanto-snorlax.serviceEnv" -}}
# Snorlax-specific configuration. See snorlax.config for what each
# var maps to.
- name: KANTO_SNORLAX_CONSUMER_GROUP
  value: {{ .Values.snorlax.consumerGroup | quote }}
# POD_NAME doubles as the consumer id — see the config alias.
- name: KANTO_SNORLAX_CONSUMER_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.name

- name: KANTO_SNORLAX_NCBI_ASSEMBLY_BASE_URL
  value: {{ .Values.snorlax.ncbiAssemblyBaseUrl | quote }}
- name: KANTO_SNORLAX_DOWNLOAD_TIMEOUT_SECONDS
  value: {{ .Values.snorlax.downloadTimeoutSeconds | quote }}
- name: KANTO_SNORLAX_DOWNLOAD_MAX_ATTEMPTS
  value: {{ .Values.snorlax.downloadMaxAttempts | quote }}
- name: KANTO_SNORLAX_DOWNLOAD_CHUNK_BYTES
  value: {{ .Values.snorlax.downloadChunkBytes | quote }}
- name: KANTO_SNORLAX_MIN_GENOME_BYTES
  value: {{ .Values.snorlax.minGenomeBytes | quote }}
- name: KANTO_SNORLAX_MAX_GENOME_BYTES
  value: {{ .Values.snorlax.maxGenomeBytes | quote }}

- name: KANTO_SNORLAX_PRODIGAL_BINARY
  value: {{ .Values.snorlax.prodigalBinary | quote }}
- name: KANTO_SNORLAX_PRODIGAL_MODE
  value: {{ .Values.snorlax.prodigalMode | quote }}
- name: KANTO_SNORLAX_PRODIGAL_TIMEOUT_SECONDS
  value: {{ .Values.snorlax.prodigalTimeoutSeconds | quote }}
- name: KANTO_SNORLAX_WORK_DIR
  value: {{ .Values.snorlax.workDir | quote }}

- name: KANTO_SNORLAX_MODAL_FUNCTION_REF
  value: {{ .Values.snorlax.modalFunctionRef | quote }}
- name: KANTO_SNORLAX_MODAL_ENVIRONMENT
  value: {{ .Values.snorlax.modalEnvironment | quote }}
- name: KANTO_SNORLAX_MODAL_MAX_ATTEMPTS
  value: {{ .Values.snorlax.modalMaxAttempts | quote }}
- name: KANTO_SNORLAX_MODAL_ENABLED
  value: {{ .Values.snorlax.modalEnabled | quote }}

- name: KANTO_SNORLAX_MEW_MAX_ATTEMPTS
  value: {{ .Values.snorlax.mewMaxAttempts | quote }}

- name: KANTO_SNORLAX_HEALTH_PORT
  value: {{ .Values.probes.port | quote }}
- name: KANTO_SNORLAX_METRICS_PORT
  value: {{ .Values.service.metricsPort | quote }}
- name: KANTO_SNORLAX_RUN_ONCE
  value: {{ .Values.snorlax.runOnce | quote }}

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

# Event Hubs (Kafka API)
- name: KANTO_STREAMING_BOOTSTRAP_SERVERS
  value: {{ required "streaming.bootstrapServers is required" .Values.streaming.bootstrapServers | quote }}
- name: KANTO_STREAMING_DISCOVERED_TOPIC
  value: {{ .Values.streaming.discoveredTopic | quote }}
- name: KANTO_STREAMING_SASL_USERNAME
  value: {{ .Values.streaming.saslUsername | quote }}
{{- end -}}
