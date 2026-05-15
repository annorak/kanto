{{/*
Standard ClusterIP Service. Exposes the application port + metrics
port. Caller renders with:

  {{ include "kanto-common.service" . }}
*/}}

{{- define "kanto-common.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kanto-common.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "kanto-common.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  selector:
    {{- include "kanto-common.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .Values.service.port }}
      targetPort: http
      protocol: TCP
    - name: metrics
      port: {{ .Values.service.metricsPort }}
      targetPort: metrics
      protocol: TCP
{{- end -}}
