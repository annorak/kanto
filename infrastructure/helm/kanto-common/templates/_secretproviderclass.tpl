{{/*
SecretProviderClass for the Azure Key Vault provider of the Secrets
Store CSI Driver. Auth via workload identity (no static credentials).

This renders ONE SecretProviderClass per service. The list at
.Values.keyVault.secrets controls which vault secrets are pulled.

For each secret entry:
  objectName  — name in Key Vault (required).
  alias       — filename on the mounted volume (defaults to objectName).
  envVar      — when set, the secret value is also synced into a K8s
                Secret under this key, so deployments can mount it via
                `valueFrom.secretKeyRef.key`.
  file        — when true, the secret is treated as a file-only mount
                (no K8s Secret sync). Use for TLS certs/keys.

Mount the CSI volume in your deployment with:

  volumes:
    - name: secrets-store-inline
      csi:
        driver: secrets-store.csi.k8s.io
        readOnly: true
        volumeAttributes:
          secretProviderClass: <release>-akv

The K8s Secret created by the sync is named the same as the SPC
(see _names.tpl). The deployment helper in _deployment_env.tpl reads
this name to wire env vars.
*/}}

{{- define "kanto-common.secretProviderClass" -}}
{{- if .Values.keyVault.secrets -}}
{{- $envEntries := list -}}
{{- range .Values.keyVault.secrets -}}
{{- if .envVar -}}
{{- $envEntries = append $envEntries (dict "objectName" (default .objectName .alias) "key" .envVar) -}}
{{- end -}}
{{- end }}
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: {{ include "kanto-common.secretProviderClassName" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "kanto-common.labels" . | nindent 4 }}
spec:
  provider: azure
  parameters:
    usePodIdentity: "false"
    useVMManagedIdentity: "false"
    clientID: {{ required "keyVault: workloadIdentity.clientId is required" .Values.workloadIdentity.clientId | quote }}
    keyvaultName: {{ required "keyVault.name is required when keyVault.secrets is non-empty" .Values.keyVault.name | quote }}
    tenantId: {{ required "keyVault.tenantId is required when keyVault.secrets is non-empty" .Values.keyVault.tenantId | quote }}
    objects: |
      array:
      {{- range .Values.keyVault.secrets }}
        - |
          objectName: {{ .objectName }}
          objectType: secret
          {{- if .alias }}
          objectAlias: {{ .alias }}
          {{- end }}
      {{- end }}
  {{- if $envEntries }}
  secretObjects:
    - secretName: {{ include "kanto-common.syncedSecretName" . }}
      type: Opaque
      data:
        {{- range $envEntries }}
        - objectName: {{ .objectName }}
          key: {{ .key }}
        {{- end }}
  {{- end }}
{{- end -}}
{{- end -}}
