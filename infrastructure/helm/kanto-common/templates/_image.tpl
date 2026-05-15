{{/*
Image reference helper. Enforces:
  * registry, repository, and tag all set.
  * tag is NOT "latest" (rejects accidental moving-tag use).

Tags should be Git SHAs in CI. For local dev, override with --set
image.tag=<sha-or-branch> when you build a one-off image.
*/}}

{{- define "kanto-common.image" -}}
{{- $reg := required "image.registry must be set (e.g. kantodevacr.azurecr.io)" .Values.image.registry -}}
{{- $repo := required "image.repository must be set (e.g. kanto-snorlax)" .Values.image.repository -}}
{{- $tag := required "image.tag must be set (use the Git SHA from CI)" .Values.image.tag -}}
{{- if eq $tag "latest" -}}
{{- fail "image.tag=latest is not allowed. Use the Git SHA from CI." -}}
{{- end -}}
{{- printf "%s/%s:%s" $reg $repo $tag -}}
{{- end -}}

{{/*
imagePullSecrets list. Empty when ACR is attached to AKS via managed
identity (the standard setup); set image.pullSecrets only when a chart
pulls from a non-ACR registry.
*/}}
{{- define "kanto-common.imagePullSecrets" -}}
{{- if .Values.image.pullSecrets -}}
imagePullSecrets:
{{- range .Values.image.pullSecrets }}
  - name: {{ . | quote }}
{{- end }}
{{- end -}}
{{- end -}}
