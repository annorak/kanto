{{/*
Name helpers. Service charts call these to derive consistent object
names. Kept short on purpose so they fit within the 63-char K8s name
limit even when combined with a release prefix.
*/}}

{{- define "kanto-common.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified app name: release name + chart name, deduped if the
release name already contains the chart name (common for charts
installed with --name-template "kanto-<service>").
*/}}
{{- define "kanto-common.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Service account name, derived from fullname unless overridden via .Values.serviceAccount.name. */}}
{{- define "kanto-common.serviceAccountName" -}}
{{- $sa := default (dict) .Values.serviceAccount -}}
{{- default (include "kanto-common.fullname" .) (get $sa "name") -}}
{{- end -}}

{{/* SecretProviderClass name. */}}
{{- define "kanto-common.secretProviderClassName" -}}
{{- printf "%s-akv" (include "kanto-common.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* K8s Secret name synced from the SecretProviderClass. */}}
{{- define "kanto-common.syncedSecretName" -}}
{{- printf "%s-akv" (include "kanto-common.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common chart label value (chart-name-version). */}}
{{- define "kanto-common.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}
