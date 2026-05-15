# Kanto Service Chart Conventions

Every Kanto service has its own Helm chart. This document defines the
shape every chart must take. Adhering to these rules keeps the fleet
homogeneous and lets the shared library chart, CI, and runbooks treat
all five services identically.

The library chart at `infrastructure/helm/kanto-common/` provides the
named templates these conventions rely on. Read it first before
writing a service chart.

> Tasks 5–9 (per-service charts) MUST follow this doc. Pull requests
> that introduce a service chart without these properties will be
> rejected at review.

---

## 1. Chart layout

```
services/<service>/helm/
  Chart.yaml
  values.yaml
  values-dev.yaml
  values-prod.yaml
  templates/
    deployment.yaml
    service.yaml
    serviceaccount.yaml
    networkpolicy.yaml
    secretproviderclass.yaml
    NOTES.txt
    _helpers.tpl          # service-specific, NOT the shared library
  tests/
    *_test.yaml           # helm-unittest assertions
  README.md
```

Each per-service chart is named `kanto-<service-name>`. The release
name in cluster matches the chart name. Example:

```
helm install kanto-snorlax services/snorlax/helm/ \
  -f services/snorlax/helm/values-dev.yaml \
  --namespace kanto-snorlax --create-namespace
```

## 2. Chart.yaml

```yaml
apiVersion: v2
name: kanto-<service>
description: <one-line description>
type: application
version: 0.1.0            # chart version — bump on chart changes
appVersion: "0.1.0"       # app version — bump on application code changes
kubeVersion: ">=1.30.0-0" # matches the cluster floor
dependencies:
  - name: kanto-common
    version: ">=0.1.0"
    repository: "file://../../../infrastructure/helm/kanto-common"
```

The repository path above resolves from the service's chart root. CI
fetches the dependency via `helm dependency update`. Do not commit
`charts/*.tgz` — it's listed in `.gitignore` and rebuilt in CI.

## 3. Image references

`image.tag` MUST be a Git SHA in production. The library refuses to
render when `image.tag=latest`. Allowed tag patterns:

| Env  | Allowed                       | Example                             |
|------|-------------------------------|-------------------------------------|
| dev  | Git SHA, branch-derived SHA   | `a1b2c3d4...`                       |
| prod | Git SHA only                  | `a1b2c3d4...`                       |

All images come from a single Azure Container Registry:

```
<acr-name>.azurecr.io/<service>:<sha>
```

The ACR is attached to AKS via the cluster's kubelet managed identity
(`AcrPull` role assignment), so `image.pullSecrets` stays empty in
both environments. Set it only when pulling from a non-ACR registry
for a one-off vendor image.

> **Convention reminder:** registry path is `<acr>.azurecr.io/kanto-<service>`,
> matching the K8s release name.

## 4. Required values surface

Every service `values.yaml` MUST set:

- `image.registry`, `image.repository`, `image.tag`, `image.pullPolicy`.
- `workloadIdentity.enabled: true`, `workloadIdentity.clientId`,
  `workloadIdentity.tenantId`. The clientId comes from the terraform
  output `aks_workload_identity_client_id`; tenantId is the AAD tenant.
- `keyVault.name`, `keyVault.tenantId`, `keyVault.secrets` (may be empty).
- `otel.endpoint` (defaulted to the in-cluster collector by the library).
- `resources.preset` (one of `small | medium | large | xlarge`).
- `probes.port` and `probes.{liveness,readiness}.path`.
- `networkPolicy.enabled: true` plus `allowFromPods` / `allowToCidrs`
  for the service's specific traffic.
- `service.port`, `service.metricsPort`.
- `kanto.environment` (`dev`/`prod` — sets `KANTO_ENV` and the
  `kanto.io/environment` label).

Two env-specific files override the defaults:

- `values-dev.yaml` — dev cluster overrides (lower replicas, single-AZ,
  debug logging).
- `values-prod.yaml` — prod cluster overrides (HPA targets, replicas,
  topology-spread, info-level logging).

Apply with `-f values-<env>.yaml` at install/upgrade.

## 5. Probes (required)

Every Deployment MUST set BOTH `livenessProbe` and `readinessProbe`,
sourced from the library helpers:

```yaml
livenessProbe:
  {{- include "kanto-common.livenessProbe" . | nindent 6 }}
readinessProbe:
  {{- include "kanto-common.readinessProbe" . | nindent 6 }}
```

kanto-commons exposes `/healthz` (liveness) and `/readyz` (readiness)
on the health port (`probes.port`, default 8081). The readiness check
must verify Mew and Event Hubs connectivity; liveness must not (a
transient downstream blip should not kill the pod).

## 6. Network policies (required)

Network policies are non-optional. The cluster-bootstrap chart applies
a default-deny `NetworkPolicy` to every `kanto-*` namespace; each
service then renders its own per-service policy via
`kanto-common.networkPolicy` listing explicit allows.

Permitted by default in the library:

- DNS egress to `kube-system / kube-dns` (UDP+TCP/53).
- IMDS for workload-identity (`169.254.169.254/32:80`).
- HTTPS egress to the public internet excluding RFC1918 (Azure AD,
  Key Vault, Storage, Event Hubs all live here).
- Prometheus scrape from a namespace labeled `kanto.io/scope=monitoring`.

Add service-specific allows via `networkPolicy.allowFromPods`,
`allowToPods`, `allowToCidrs`. Example:

```yaml
networkPolicy:
  enabled: true
  allowFromPods:
    - namespace: kanto-api
      podLabels:
        app.kubernetes.io/name: kanto-api
  allowToPods:
    - namespace: kanto-snorlax
      podLabels:
        app.kubernetes.io/name: kanto-snorlax
      ports:
        - { port: 8080, protocol: TCP }
```

## 7. NOTES.txt (required)

Every chart's `templates/NOTES.txt` MUST print:

- The release name and namespace.
- The application port (kubectl port-forward example).
- The image reference deployed.
- A pointer to the readiness URL.

Skeleton:

```
{{ .Chart.Name }} {{ .Chart.AppVersion }} installed in {{ .Release.Namespace }}.

Image: {{ include "kanto-common.image" . }}

Verify readiness:
  kubectl -n {{ .Release.Namespace }} port-forward svc/{{ include "kanto-common.fullname" . }} 8081:{{ .Values.probes.port }}
  curl http://localhost:8081{{ .Values.probes.readiness.path }}
```

---

## 8. Secret management — Azure Key Vault via Secrets Store CSI Driver

**Decision:** Secrets reach pods via the **Azure Key Vault provider
for the Secrets Store CSI Driver**, authenticated by **Microsoft
Entra Workload Identity** (federated K8s service-account tokens).
No client secrets, no static credentials, no AAD pod-identity
(deprecated).

This is the Microsoft-recommended pattern for AKS as of 2026 and is
shipped as an AKS managed addon, so the driver and provider are
upgraded by the AKS service rather than by us.

### Why not the alternatives

| Option                          | Why we didn't pick it |
|---------------------------------|-----------------------|
| External Secrets Operator (ESO) | Adds a third-party operator to maintain. Always materializes a K8s Secret (extra blast radius). Multi-cloud portability we don't need at v1. |
| Application-level SDK fetch     | Each service writes secret-loading code. No rotation without restart. No declarative drift detection. |
| AAD Pod Identity                | Deprecated by Microsoft as of mid-2024. |

### Cluster prerequisite (one-time)

The AKS module must enable the Key Vault Secrets Provider addon:

```hcl
key_vault_secrets_provider {
  secret_rotation_enabled  = true
  secret_rotation_interval = "2m"
}
```

This is tracked as a follow-up Terraform change in
`infrastructure/terraform/modules/aks/`. Until then, the operator
runs once:

```
az aks enable-addons -g <rg> -n <cluster> \
  --addons azure-keyvault-secrets-provider
az aks addon update -g <rg> -n <cluster> \
  --addon azure-keyvault-secrets-provider \
  --enable-secret-rotation --rotation-poll-interval 2m
```

The federated identity credential for each service's K8s service
account is also a one-time step per service, e.g.:

```
az identity federated-credential create \
  --name kanto-snorlax-fed \
  --identity-name kanto-dev-aks-workloads \
  --resource-group <rg> \
  --issuer "$(az aks show -g <rg> -n <cluster> --query oidcIssuerProfile.issuerUrl -o tsv)" \
  --subject system:serviceaccount:kanto-snorlax:kanto-snorlax \
  --audiences api://AzureADTokenExchange
```

We will fold these credentials into the Terraform IAM module when the
service tasks (5–9) land.

### How a chart references secrets

In `values.yaml`:

```yaml
keyVault:
  name: kanto-dev-kv-ab12cd        # vault name, NOT URI
  tenantId: 00000000-0000-0000-0000-000000000002
  secrets:
    # Postgres password — mounted as a file AND synced to K8s Secret
    # so kanto-commons reads it via KANTO_MEW_PASSWORD env var.
    - objectName: kanto-dev-mew-password
      alias: mew-password
      envVar: KANTO_MEW_PASSWORD
    # TLS cert + key — file-only mount; never sync to a K8s Secret.
    - objectName: kanto-dev-snorlax-tls-cert
      alias: tls.crt
      file: true
    - objectName: kanto-dev-snorlax-tls-key
      alias: tls.key
      file: true
```

Behavior:

- `envVar` set → the library renders a `secretObjects` block that the
  CSI driver materializes into a K8s Secret; deployment env helper
  injects a `valueFrom.secretKeyRef`. Use for env-var-style secrets
  (DB passwords, API tokens).
- `file: true` → no K8s Secret sync; the secret stays on the mounted
  volume only. Use for TLS cert/key materials read from disk.

The volume mount path is `/var/run/secrets/kanto/`.

### Rotation

`secret_rotation_enabled = true` polls Key Vault every 2 minutes. When
a secret rotates:

- **File-mounted secrets:** updated in place. Apps that read the file
  on every request pick up the new value automatically. Apps that
  cache should reread at a sensible interval.
- **Env-var secrets (via K8s Secret sync):** the K8s Secret is
  updated; env vars are NOT re-read because Kubernetes injects them
  at pod start. The driver fires a `secrets-store.csi.x-k8s.io/rotated`
  event; we rely on a periodic Deployment rollout (or HPA-induced
  pod churn) to pick them up. Document the rotation cadence per
  secret. For Mew passwords, plan a rolling restart after rotation.

### Fail-fast on missing secrets

If Key Vault is unreachable at pod startup, the CSI driver fails to
mount the volume; the pod stays in `ContainerCreating` rather than
running with missing config. That's the correct behavior — alerting
fires on pods that fail to become Ready.

---

## 9. Observability

### Logs — Azure Container Insights (no custom agent)

The AKS cluster has the `oms_agent` addon enabled in
`modules/aks/main.tf` (`oms_agent { log_analytics_workspace_id = ... }`).
Container Insights streams all container stdout/stderr to the
Log Analytics workspace at `module.logging.workspace_id`.

**Service responsibility:** write JSON to stdout. kanto-commons does
this by default when `KANTO_LOG_FORMAT=json`.

**No FluentBit DaemonSet.** The bootstrap chart does NOT install
FluentBit because Container Insights already collects pod logs. If a
future need emerges (vendor-neutral collection, log filtering before
Log Analytics), we'll add a FluentBit DaemonSet toggle to the
bootstrap chart.

### Traces — OpenTelemetry Collector (in-cluster Deployment)

Traces flow:

```
service pod (kanto-commons OTel SDK)
  --OTLP/HTTP--> otel-collector Service in observability ns
  --OTLP--> Application Insights (Azure Monitor)
```

The collector is deployed by the cluster-bootstrap chart as a
Deployment (NOT a DaemonSet — DaemonSet is overkill at our scale and
makes pod-affinity scaling harder).

Service charts set `otel.endpoint` to the in-cluster collector
service DNS:

```
http://otel-collector.observability.svc.cluster.local:4318
```

kanto-commons reads this via `OTEL_EXPORTER_OTLP_ENDPOINT` (already
wired in the library's `standardEnv` helper).

### Metrics — Azure Monitor managed Prometheus

v1 uses **Azure Monitor managed Prometheus** with auto-discovery on
pod annotations:

```
prometheus.io/scrape: "true"
prometheus.io/port:   "9464"
prometheus.io/path:   "/metrics"
```

The library's `kanto-common.podAnnotations` helper sets these
automatically from `.Values.prometheus.*`. Each service exposes a
`/metrics` endpoint on port 9464 (config in `KantoBaseSettings`).

We will revisit self-hosted Prometheus only when a specific need
emerges (e.g., metrics not surfaced by Azure Monitor, custom
scraping cadence).

---

## 10. Resource requests and limits

Set via `resources.preset`. Presets in the library helper:

| Preset   | CPU req | Memory req | Memory limit |
|----------|---------|------------|--------------|
| small    | 100m    | 256Mi      | 512Mi        |
| medium   | 250m    | 512Mi      | 1Gi          |
| large    | 500m    | 1Gi        | 2Gi          |
| xlarge   | 1000m   | 2Gi        | 4Gi          |

**No CPU limits.** CPU limits cause throttling under burst load even
when nodes are idle. We set CPU requests (which inform scheduling and
HPA) but let pods burst above on available CPU. Memory has both
request and limit because memory is incompressible — OOM-kill is the
correct response, throttling isn't an option.

Override individual fields via `resources.overrides` when a service
needs a non-preset combination (e.g. small CPU but large memory).

## 11. Probes (already covered) and security context

Library `kanto-common.podSpec` renders the secure baseline:

```yaml
runAsNonRoot: true
runAsUser: 65532              # nobody
fsGroup: 65532
seccompProfile.type: RuntimeDefault
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities.drop: [ALL]
```

Service charts may override `runAsUser` if the container image bakes
a different non-root UID, but the other settings are required. Pods
needing a writable temp dir should mount an `emptyDir` rather than
disabling `readOnlyRootFilesystem`.

## 12. Linting and validation (enforced by CI)

Every PR that touches `infrastructure/helm/` or `services/*/helm/`
runs:

1. `helm lint` on every chart.
2. `helm dependency update && helm template` on every chart (renders
   with both `values-dev.yaml` and `values-prod.yaml`).
3. `kubeconform -kubernetes-version 1.34.0 -strict` on the rendered
   output, with the Datree CRD catalog for `SecretProviderClass`.
4. `helm unittest` on the library chart's fixture.

See `.github/workflows/helm.yml`.

## 13. Anti-patterns (rejected at review)

- `image.tag: latest` or any moving tag. Use Git SHA only.
- Probes omitted, or both `httpGet` and `tcpSocket` left as placeholders.
- `securityContext.privileged: true`.
- Hardcoded namespaces in templates. Use `.Release.Namespace`.
- Plaintext secrets in `values.yaml` or env. Reference Key Vault.
- Bypassing the library chart by copy-pasting helpers locally.
- Sidecar-per-pod logging agents. Container Insights covers stdout.
- Cluster-wide network policy created from a service chart. Cluster
  policies live in the bootstrap chart only.
