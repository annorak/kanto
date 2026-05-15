{{/*
Default-deny network policy + per-service allows.

Pattern: a chart that imports this helper gets TWO policies:
  1. <release>-default-deny — denies all ingress/egress that isn't
     explicitly allowed below.
  2. <release>-allow — permits:
       * DNS egress to kube-system (cluster CoreDNS).
       * AKS metadata + Azure AD egress (workload identity token fetch).
       * The pod/CIDR entries the service declared in values.yaml.
       * Ingress from declared peer pods.

Cluster-wide default-deny for namespaces is rendered by the
cluster-bootstrap chart in each kanto-* namespace. This per-service
policy adds the service-specific allows.
*/}}

{{- define "kanto-common.networkPolicy" -}}
{{- if .Values.networkPolicy.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "kanto-common.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "kanto-common.labels" . | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      {{- include "kanto-common.selectorLabels" . | nindent 6 }}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    # Allow Prometheus scrape from the monitoring namespace.
    - from:
        - namespaceSelector:
            matchLabels:
              kanto.io/scope: monitoring
      ports:
        - port: {{ .Values.service.metricsPort }}
          protocol: TCP
    {{- range .Values.networkPolicy.allowFromPods }}
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .namespace | default $.Release.Namespace | quote }}
          podSelector:
            matchLabels:
              {{- toYaml .podLabels | nindent 14 }}
      ports:
        - port: {{ $.Values.service.port }}
          protocol: TCP
    {{- end }}
  egress:
    # CoreDNS in kube-system.
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    # IMDS for AKS workload identity (federated token issuer).
    - to:
        - ipBlock:
            cidr: 169.254.169.254/32
      ports:
        - port: 80
          protocol: TCP
    # Azure AD + Microsoft Graph + Key Vault data plane + ACR pull.
    # AzureCloud service tags aren't expressible in NetworkPolicy v1;
    # we use 0.0.0.0/0 minus RFC1918 instead so prod-bound services
    # can still reach Azure data planes. Lock further per service if
    # the egress surface is well-known.
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
      ports:
        - port: 443
          protocol: TCP
    {{- range .Values.networkPolicy.allowToPods }}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ .namespace | default $.Release.Namespace | quote }}
          podSelector:
            matchLabels:
              {{- toYaml .podLabels | nindent 14 }}
      {{- if .ports }}
      ports:
        {{- toYaml .ports | nindent 8 }}
      {{- end }}
    {{- end }}
    {{- range .Values.networkPolicy.allowToCidrs }}
    - to:
        - ipBlock:
            cidr: {{ .cidr | quote }}
      {{- if .ports }}
      ports:
        {{- toYaml .ports | nindent 8 }}
      {{- end }}
    {{- end }}
{{- end }}
{{- end -}}
