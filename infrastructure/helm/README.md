# Kanto Helm — Deployment Runbook

This directory holds the Helm charts that deploy Kanto on AKS.

```
infrastructure/helm/
  kanto-common/             # shared library chart (no resources)
  cluster-bootstrap/        # one-per-cluster shared infra (namespaces,
                            # default-deny, OTel collector)
  CONVENTIONS.md            # what every service chart must follow
  README.md                 # this file
```

Per-service charts live next to their service code under
`services/<service>/helm/` and are documented in CONVENTIONS.md.

---

## 1. Toolchain

```
helm        >= 4.0     # rendering / install / upgrade
kubectl     >= 1.30    # cluster interaction
kubeconform >= 0.7     # offline schema validation
helm-unittest plugin   # chart-level assertions
az CLI      >= 2.60    # AKS credentials + addon management
```

Install (macOS):

```bash
brew install helm kubeconform azure-cli
helm plugin install https://github.com/helm-unittest/helm-unittest.git --verify=false
```

## 2. Cluster prerequisites (one-time per environment)

The cluster-bootstrap chart depends on the AKS-managed Secrets Store
CSI Driver addon. Enable it once on a fresh cluster:

```bash
RG=<resource-group>
CLUSTER=<aks-name>

az aks enable-addons -g $RG -n $CLUSTER \
  --addons azure-keyvault-secrets-provider

az aks addon update -g $RG -n $CLUSTER \
  --addon azure-keyvault-secrets-provider \
  --enable-secret-rotation --rotation-poll-interval 2m
```

Workload-identity OIDC issuer + webhook are already enabled in
`infrastructure/terraform/modules/aks/main.tf`
(`oidc_issuer_enabled = true`, `workload_identity_enabled = true`).

Fetch a kubeconfig:

```bash
az aks get-credentials -g $RG -n $CLUSTER
kubectl get nodes      # sanity check
```

## 3. Install cluster-bootstrap on a fresh cluster

```bash
cd infrastructure/helm/cluster-bootstrap

# Dev cluster:
helm install bootstrap . \
  -f values-dev.yaml \
  --namespace kube-system

# Prod cluster:
helm install bootstrap . \
  -f values-prod.yaml \
  --namespace kube-system \
  --set otelCollector.applicationInsightsConnectionString="$AI_CONNECTION_STRING"
```

The release lives in `kube-system` but the resources it manages land
in `observability` and the per-service `kanto-*` namespaces.

Verify:

```bash
kubectl -n observability get deploy,svc,pod
kubectl -n observability get configmap otel-collector-config -o yaml | head
kubectl get networkpolicy -A | grep default-deny
```

Test traces flow end-to-end:

```bash
kubectl -n observability port-forward svc/otel-collector 4318:4318 &
curl -i http://localhost:4318/v1/traces \
  -H 'content-type: application/json' \
  -d '{"resourceSpans":[]}'
# expect HTTP 200 / 202
```

## 4. Install or upgrade a service chart

```bash
SVC=snorlax
cd services/$SVC/helm

helm dependency update .

# Dry-run first
helm install kanto-$SVC . \
  -f values-dev.yaml \
  --namespace kanto-$SVC \
  --dry-run --debug | tee /tmp/render.yaml

# Validate offline
kubeconform -kubernetes-version 1.34.0 -strict \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  /tmp/render.yaml

# Install (namespace must exist; bootstrap chart creates kanto-* nses)
helm install kanto-$SVC . \
  -f values-dev.yaml \
  --namespace kanto-$SVC

# Upgrade
helm upgrade kanto-$SVC . \
  -f values-dev.yaml \
  --namespace kanto-$SVC \
  --set image.tag=$(git rev-parse HEAD)
```

## 5. Roll back

```bash
helm history kanto-$SVC -n kanto-$SVC

# Roll back to a specific previous revision
helm rollback kanto-$SVC <revision> -n kanto-$SVC

# Or just one step back
helm rollback kanto-$SVC -n kanto-$SVC
```

Rollback preserves the Deployment's rolling-update strategy, so the
new pods come up before the old ones go down. Watch:

```bash
kubectl -n kanto-$SVC rollout status deploy/kanto-$SVC
```

If the rollback itself is wrong, roll forward to a known-good SHA:

```bash
helm upgrade kanto-$SVC . -n kanto-$SVC \
  -f values-dev.yaml \
  --set image.tag=<known-good-sha>
```

## 6. Debug a failing chart deployment

1. `helm install --dry-run --debug` to see the rendered manifests and
   any template error.

2. `helm install --debug --wait` to print events from kubectl while
   the install is in flight.

3. `kubectl describe pod -n kanto-$SVC <pod>` for image-pull, probe
   failure, or volume-mount errors.

4. For Secrets Store CSI Driver issues:

   ```bash
   kubectl describe secretproviderclass -n kanto-$SVC kanto-$SVC-akv
   kubectl -n kube-system logs ds/aks-secrets-store-csi-driver -c secrets-store
   kubectl -n kube-system logs ds/aks-secrets-store-provider-azure
   ```

   Most failures here are missing role assignments on the workload
   identity, or a missing federated credential. Validate:

   ```bash
   az identity federated-credential list \
     --identity-name kanto-<env>-aks-workloads \
     --resource-group $RG
   ```

5. For traces not appearing in App Insights / Container Insights:

   ```bash
   kubectl -n observability logs deploy/otel-collector
   kubectl -n observability port-forward svc/otel-collector 8888:8888
   curl http://localhost:8888/metrics | grep otelcol_exporter
   ```

6. To inspect rendered manifests for a live release:

   ```bash
   helm get manifest kanto-$SVC -n kanto-$SVC
   helm get values kanto-$SVC -n kanto-$SVC --all
   ```

## 7. Release naming

| Resource         | Convention                              |
|------------------|-----------------------------------------|
| Chart name       | `kanto-<service>` (e.g. kanto-snorlax)  |
| Release name     | Same as chart name                      |
| Namespace        | Same as chart name                      |
| ServiceAccount   | Same as chart name                      |
| K8s Secret (sync)| `<release>-akv`                         |
| SecretProviderClass | `<release>-akv`                      |
| Image tag        | Git SHA (full 40-char)                  |

The cluster-bootstrap release is named `bootstrap` and lives in
`kube-system`.

## 8. Values precedence

Helm composes values in this order; later wins:

1. The chart's own `values.yaml` (defaults).
2. `-f values-<env>.yaml` (environment overrides).
3. `--set key=val` flags (one-off overrides on the command line).

Where each lives:

| File                        | Owns                                 |
|-----------------------------|--------------------------------------|
| `chart/values.yaml`         | Schema + safe defaults               |
| `chart/values-dev.yaml`     | Dev cluster overrides (replicas, log level, sample rate) |
| `chart/values-prod.yaml`    | Prod cluster overrides (replicas, HA, prod endpoints) |
| `--set image.tag=<sha>`     | Per-deploy image bump                |
| `--set keyVault.secrets[*]` | One-off secret testing only          |

Do NOT put secrets in any values file — reference them by name via
`keyVault.secrets`.

## 9. Adding a new chart to the repo

1. Create the chart under `services/<service>/helm/`.
2. Set `Chart.yaml` with `apiVersion: v2`, `type: application`,
   `kubeVersion: ">=1.30.0-0"`, and a `kanto-common` dependency.
3. Mirror the values structure from CONVENTIONS.md §4. Provide
   `values-dev.yaml` and `values-prod.yaml`.
4. Render every required helper (`serviceAccount`, `service`,
   `secretProviderClass`, `networkPolicy`) from kanto-common.
5. Write `templates/NOTES.txt` per the skeleton in CONVENTIONS.md §7.
6. Add `tests/<name>_test.yaml` with helm-unittest assertions.
7. Add the service's namespace to
   `infrastructure/helm/cluster-bootstrap/values.yaml`
   under `namespaces.kantoServices`, plus the federated credential
   to the Terraform IAM module.
8. CI will lint, template, kubeconform, and unit-test the new chart
   automatically.

## 10. CI

`.github/workflows/helm.yml` runs on every PR that touches
`infrastructure/helm/` or `services/*/helm/`:

* `helm lint` per chart.
* `helm dependency update && helm template` per chart, with
  values-dev.yaml and values-prod.yaml.
* `kubeconform -kubernetes-version 1.34.0 -strict` on the rendered
  output.
* `helm unittest` per chart that has a `tests/` directory.

Failures block the merge. Run locally before pushing:

```bash
make -C infrastructure/helm lint
make -C infrastructure/helm test
```
