{{/*
Reusable pod spec. Service deployments call this from their
spec.template.spec to get a consistent baseline: kanto-commons env,
secrets volume mount, probes, resource preset, security context.

The IMAGE container name defaults to the chart name; override via
.Values.containerName.

This is NOT a full deployment template — service charts still define
their own Deployment kind. The helper provides only the inner
PodSpec block from `serviceAccountName:` down through `containers:`.
*/}}

{{- define "kanto-common.podSpec" -}}
serviceAccountName: {{ include "kanto-common.serviceAccountName" . }}
{{- $pullSecrets := include "kanto-common.imagePullSecrets" . }}
{{- if $pullSecrets }}
{{ $pullSecrets }}
{{- end }}
securityContext:
  runAsNonRoot: true
  runAsUser: 65532
  runAsGroup: 65532
  fsGroup: 65532
  seccompProfile:
    type: RuntimeDefault
{{- with .Values.pod.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.pod.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.pod.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.pod.topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with (include "kanto-common.secretsVolume" .) }}
volumes:
  {{- . | nindent 2 }}
{{- end }}
containers:
  - name: {{ default (include "kanto-common.name" .) .Values.containerName }}
    image: {{ include "kanto-common.image" . | quote }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    ports:
      - name: http
        containerPort: {{ .Values.service.port }}
        protocol: TCP
      - name: metrics
        containerPort: {{ .Values.service.metricsPort }}
        protocol: TCP
      - name: health
        containerPort: {{ .Values.probes.port }}
        protocol: TCP
    env:
      {{- include "kanto-common.standardEnv" . | nindent 6 }}
    livenessProbe:
      {{- include "kanto-common.livenessProbe" . | nindent 6 }}
    readinessProbe:
      {{- include "kanto-common.readinessProbe" . | nindent 6 }}
    resources:
      {{- include "kanto-common.resources" . | nindent 6 }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop:
          - ALL
    {{- with (include "kanto-common.secretsVolumeMount" .) }}
    volumeMounts:
      {{- . | nindent 6 }}
    {{- end }}
{{- end -}}
