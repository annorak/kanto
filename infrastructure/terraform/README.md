# Kanto — Azure Terraform

Provisions every Azure resource Kanto needs: resource groups, VNet, AKS,
Mew (Postgres Flexible Server + pgvector), Event Hubs (Kafka surface),
Blob Storage, Key Vault, IAM (managed identities + RBAC), Log Analytics.

## Layout

```
infrastructure/terraform/
├── main.tf, variables.tf, outputs.tf, versions.tf  ← THE entrypoint
├── config/
│   ├── dev.tfvars.example                          ← per-env tunable values
│   ├── prod.tfvars.example
│   ├── dev-backend.hcl.example                     ← per-env state location
│   └── prod-backend.hcl.example
├── bootstrap/                                      ← one-time local-state config
└── modules/                                        ← reusable building blocks
    ├── network/         VNet, subnets, NSGs, private DNS for Postgres
    ├── key_vault/       Key Vault + etcd-encryption key + Mew password + placeholder secrets
    ├── object_storage/  Storage account + proteins/embeddings/metadata containers
    ├── event_hubs/      Event Hubs namespace + 7 hubs (Kafka surface)
    ├── iam/             User-assigned managed identities + RBAC + Modal OIDC federation
    ├── mew/             Postgres Flexible Server (VNet-integrated) + pgvector + kanto database
    ├── aks/             AKS cluster + system node pool + workload identity + diagnostics
    └── logging/         Log Analytics workspace
```

## Why one root with two configs (instead of two roots)

The root module defines one state file. We get **per-env state isolation**
by switching the backend at init time:

```bash
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform apply -var-file=config/dev.tfvars
```

The `key` differs (`envs/dev/terraform.tfstate` vs `envs/prod/...`), so
the two states never share a blob. Always `terraform init -reconfigure`
when switching envs.

## What's tunable, and where

| Lives in                    | Examples                                                                |
| --------------------------- | ----------------------------------------------------------------------- |
| `config/<env>.tfvars`       | Subscription/tenant IDs, CIDRs, AKS+Mew sizing, retention, Modal OIDC   |
| `config/<env>-backend.hcl`  | Storage account / container / state key for remote state                |
| `variables.tf`              | Full list of tunables, descriptions, defaults                           |
| `main.tf`                   | Wiring; module calls feed every variable into the right module          |

If you want to tune something we don't currently expose, **add a variable
to `variables.tf`** and pass it through the relevant `module "..."` call
in `main.tf`. The module itself likely already has a matching input.

## Bootstrap (run once per subscription)

`bootstrap/` creates per-env resource groups + the Storage Account that
holds remote state for the main config below. Local state — there's no
chicken-and-egg way to create the state container remotely.

```bash
cd infrastructure/terraform/bootstrap
az login                                    # if not already
cp terraform.tfvars.example terraform.tfvars
# Fill in subscription_id + tenant_id (`az account show`).
terraform init
terraform apply
```

Note the outputs — `resource_group_dev_name`, `resource_group_prod_name`,
`tfstate_storage_account_name`, `tfstate_container_name`. Paste them into
the main config files in the next step.

## Apply an environment

```bash
cd infrastructure/terraform

# 1. Per-env config files (gitignored).
cp config/dev.tfvars.example      config/dev.tfvars
cp config/dev-backend.hcl.example config/dev-backend.hcl
# Edit both: paste bootstrap outputs, SSH key, operator CIDRs.

# 2. Init the backend and apply.
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform plan  -var-file=config/dev.tfvars -out tfplan
terraform apply tfplan
```

Switch to prod by re-running step 2 with the prod files; `-reconfigure`
forces Terraform to forget the previous backend wiring.

## After apply

1. **Rotate Key Vault placeholders.** For each placeholder secret the
   key_vault module created (`<prefix>-modal-token`,
   `<prefix>-slack-webhook-url`, `<prefix>-pagerduty-integration-key`):
   ```bash
   az keyvault secret set \
     --vault-name <vault-name> \
     --name <secret-name> \
     --value "<real value>"
   ```
2. **Wire Modal workload-identity federation.** In prod only:
   ```bash
   az identity federated-credential create \
     --identity-name <prefix>-modal \
     --resource-group <prefix>-rg \
     --name modal-federation \
     --issuer <Modal OIDC issuer URL> \
     --subject <Modal OIDC subject claim> \
     --audiences "api://AzureADTokenExchange"
   ```
   (The iam module creates this resource when `modal_oidc_issuer` is
   non-empty; the CLI command above is the manual fallback.)
3. **Pull a kubeconfig:**
   ```bash
   az aks get-credentials \
     --resource-group $(terraform output -raw resource_group_name) \
     --name $(terraform output -raw aks_cluster_name) \
     --file $HOME/.kube/kanto-dev
   ```

## Adding a new resource

- **A new container / event hub / log table:** add it to the relevant
  module. Modules use `for_each` over a local map; usually appending one
  line is enough.
- **A new tunable:** add a `variable` to `variables.tf`, pass it through
  the matching `module "..."` block in `main.tf`, document it in
  `config/<env>.tfvars.example`.
- **A new module entirely:** drop a folder under `modules/` with
  `versions.tf`, `variables.tf`, `main.tf`, `outputs.tf`, `README.md`,
  then add a `module "..." {}` block to `main.tf`.

Every change must pass `terraform fmt`, `terraform validate`, `tflint`,
and `tfsec` — these run automatically in CI on every PR.

## Tearing down

Reverse the provisioning order. Most modules have `prevent_destroy` on
load-bearing resources (Key Vault, etcd key, state storage account,
resource groups). Remove the lifecycle block before a real `destroy`,
and only do that in dev.

```bash
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform destroy -var-file=config/dev.tfvars

cd bootstrap
terraform destroy
```

In prod, do not destroy. If you need to retire prod, take a final backup
of Mew + the kanto-embeddings container first; `prevent_destroy` makes
this delay unavoidable.

## Cost expectations

Rough monthly spend per environment, in eastus, May 2026 list prices.
12-month Azure free tier credits apply for the first year on some lines.

| Component                                   | Dev (within free tier)     | Prod        |
| ------------------------------------------- | -------------------------- | ----------- |
| AKS control plane                           | $0                         | $0          |
| AKS worker nodes (2× Standard_B2s)          | ~$60                       | n/a         |
| AKS worker nodes (3× Standard_D4s_v5)       | n/a                        | ~$420       |
| Mew Flexible Server (B_Standard_B1ms)       | $0 (first 12 mo) / ~$15    | n/a         |
| Mew Flexible Server (GP_Standard_D16s_v3)   | n/a                        | ~$1,100     |
| Mew storage (32 GB dev / 256 GB prod)       | $0 (first 12 mo) / ~$4     | ~$32        |
| Event Hubs Standard (1 TU, auto-inflate 2)  | ~$22                       | ~$44        |
| Storage Account (LRS, < 5 GB dev)           | $0 (first 12 mo) / ~$1     | ~$50–250    |
| Key Vault Standard                          | ~$5                        | ~$5         |
| Log Analytics ingestion (< 5 GB free / mo)  | $0                         | ~$20        |
| **Estimated total**                         | **~$90/mo (~$0 first 12 mo)** | **~$1,700+** |

Storage egress + ingestion grow with backfill volume.

## Testing

Local:

```bash
cd infrastructure/terraform
terraform fmt -recursive
terraform init -backend=false
terraform validate
tflint --recursive --minimum-failure-severity=warning
tfsec --minimum-severity HIGH .
```

CI: see `.github/workflows/terraform.yml`. Every PR touching
`infrastructure/terraform/**` runs `fmt`, `validate` per directory,
`tflint`, and `tfsec` (HIGH+).
