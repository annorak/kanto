{{/*
Service-specific helpers — flatten the Alakazam knobs from values.yaml
into env vars consumed by the Deployment and the CronJob.

Kept here (not in the library chart) so a future second consumer of
kanto-common doesn't inherit alakazam-shaped surface.
*/}}

{{- define "kanto-alakazam.serviceEnv" -}}
# Alakazam-specific configuration. See alakazam.config for what each
# var maps to.
- name: KANTO_ALAKAZAM_CONSUMER_GROUP
  value: {{ .Values.alakazam.consumerGroup | quote }}
- name: KANTO_ALAKAZAM_CONSUMER_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.name

- name: KANTO_ALAKAZAM_CANDIDATE_THRESHOLD
  value: {{ .Values.alakazam.candidateThreshold | quote }}
- name: KANTO_ALAKAZAM_NN_K
  value: {{ .Values.alakazam.nnK | quote }}

- name: KANTO_ALAKAZAM_WEIGHT_NN
  value: {{ .Values.alakazam.weightNn | quote }}
- name: KANTO_ALAKAZAM_WEIGHT_COVERAGE
  value: {{ .Values.alakazam.weightCoverage | quote }}
- name: KANTO_ALAKAZAM_WEIGHT_MAHALANOBIS
  value: {{ .Values.alakazam.weightMahalanobis | quote }}

- name: KANTO_ALAKAZAM_REFERENCE_SET_CONTAINER
  value: {{ .Values.alakazam.referenceSetContainer | quote }}
- name: KANTO_ALAKAZAM_REFERENCE_SET_KEY
  value: {{ .Values.alakazam.referenceSetKey | quote }}
- name: KANTO_ALAKAZAM_COVERAGE_MATCH_THRESHOLD
  value: {{ .Values.alakazam.coverageMatchThreshold | quote }}
- name: KANTO_ALAKAZAM_COVERAGE_MAX_PROTEINS
  value: {{ .Values.alakazam.coverageMaxProteins | quote }}

- name: KANTO_ALAKAZAM_MAHALANOBIS_MIN_SAMPLES
  value: {{ .Values.alakazam.mahalanobisMinSamples | quote }}
- name: KANTO_ALAKAZAM_MAHALANOBIS_REGULARIZATION
  value: {{ .Values.alakazam.mahalanobisRegularization | quote }}

- name: KANTO_ALAKAZAM_ALERT_THRESHOLD
  value: {{ .Values.alakazam.alertThreshold | quote }}

- name: KANTO_ALAKAZAM_CENTROID_REFRESH_SECONDS
  value: {{ .Values.alakazam.centroidRefreshSeconds | quote }}
- name: KANTO_ALAKAZAM_CENTROID_MIN_ISOLATES
  value: {{ .Values.alakazam.centroidMinIsolates | quote }}
{{- if .Values.alakazam.centroidOrganismFilter }}
- name: KANTO_ALAKAZAM_CENTROID_ORGANISM_FILTER
  value: {{ .Values.alakazam.centroidOrganismFilter | quote }}
{{- end }}

- name: KANTO_ALAKAZAM_MEW_MAX_ATTEMPTS
  value: {{ .Values.alakazam.mewMaxAttempts | quote }}

- name: KANTO_ALAKAZAM_HEALTH_PORT
  value: {{ .Values.probes.port | quote }}
- name: KANTO_ALAKAZAM_METRICS_PORT
  value: {{ .Values.service.metricsPort | quote }}
- name: KANTO_ALAKAZAM_RUN_ONCE
  value: {{ .Values.alakazam.runOnce | quote }}

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
- name: KANTO_STREAMING_EMBEDDED_TOPIC
  value: {{ .Values.streaming.embeddedTopic | quote }}
- name: KANTO_STREAMING_SCORED_TOPIC
  value: {{ .Values.streaming.scoredTopic | quote }}
- name: KANTO_STREAMING_SASL_USERNAME
  value: {{ .Values.streaming.saslUsername | quote }}
{{- end -}}


{{/*
CronJob resource block. Re-uses the kanto-common preset table; falls
back to "large" if the operator didn't pick one.
*/}}
{{- define "kanto-alakazam.cronjobResources" -}}
{{- $preset := default "large" .Values.centroidCronJob.resources.preset -}}
{{- $defaults := dict
  "small"   (dict "cpu" "100m"  "memory" "256Mi" "memoryLimit" "512Mi")
  "medium"  (dict "cpu" "250m"  "memory" "512Mi" "memoryLimit" "1Gi")
  "large"   (dict "cpu" "500m"  "memory" "1Gi"   "memoryLimit" "2Gi")
  "xlarge"  (dict "cpu" "1000m" "memory" "2Gi"   "memoryLimit" "4Gi")
-}}
{{- $values := get $defaults $preset -}}
{{- if not $values -}}
  {{- fail (printf "Unknown centroidCronJob.resources.preset %q. Use small|medium|large|xlarge." $preset) -}}
{{- end -}}
{{- $overrides := default (dict) .Values.centroidCronJob.resources.overrides -}}
{{- $reqOver := default (dict) (get $overrides "requests") -}}
{{- $limOver := default (dict) (get $overrides "limits") -}}
requests:
  cpu: {{ default $values.cpu (get $reqOver "cpu") | quote }}
  memory: {{ default $values.memory (get $reqOver "memory") | quote }}
limits:
  memory: {{ default $values.memoryLimit (get $limOver "memory") | quote }}
{{- end -}}
