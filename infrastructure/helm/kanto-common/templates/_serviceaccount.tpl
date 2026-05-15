{{/*
ServiceAccount template. Annotated for AKS workload identity so pods
get a federated AAD token without static credentials.

Usage in a service chart:

  {{- include "kanto-common.serviceAccount" . }}

The token is requested by the Azure Workload Identity mutating webhook
(installed by the AKS oidc/workload-identity addon). The webhook reads
the `azure.workload.identity/client-id` annotation here and projects a
service-account token into the pod's filesystem; the Azure SDK in
kanto-commons picks it up via the AZURE_FEDERATED_TOKEN_FILE env var
that the webhook also injects.
*/}}

{{- define "kanto-common.serviceAccount" -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "kanto-common.serviceAccountName" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "kanto-common.labels" . | nindent 4 }}
  annotations:
    {{- if .Values.workloadIdentity.enabled }}
    azure.workload.identity/client-id: {{ required "workloadIdentity.clientId is required when workloadIdentity.enabled" .Values.workloadIdentity.clientId | quote }}
    azure.workload.identity/tenant-id: {{ required "workloadIdentity.tenantId is required when workloadIdentity.enabled" .Values.workloadIdentity.tenantId | quote }}
    {{- end }}
automountServiceAccountToken: true
{{- end -}}
