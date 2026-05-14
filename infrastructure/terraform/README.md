# Kanto — OCI Terraform

Provisions every OCI resource Kanto needs: compartments, VCN, OKE cluster,
Mew (Postgres + pgvector), OCI Streaming, Object Storage buckets, KMS Vault,
IAM, Logging.

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
    ├── network/         VCN, subnets, gateways, NSGs
    ├── vault/           KMS vault, master key, secret slots
    ├── object_storage/  proteins / embeddings / metadata buckets
    ├── streaming/       stream pool + 7 streams
    ├── iam/             dynamic groups, IAM users, policies
    ├── mew/             OCI Database for PostgreSQL + pgvector + optional public NLB
    ├── oke/             OKE cluster + CPU node pool
    └── logging/         log group + custom application log
```

## Why one root with two configs (instead of two roots)

The root module — the directory you run `terraform apply` from — defines
one state file. We get **per-env state isolation** by switching the
backend at init time:

```bash
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform apply -var-file=config/dev.tfvars
```

The `key` differs (`envs/dev/terraform.tfstate` vs
`envs/prod/terraform.tfstate`), so the two states never share a file. A
typo'd `terraform apply -var-file=config/prod.tfvars` against a
dev-initialised backend lands in dev's state — the resource names then
clash with what's already there and the apply fails loudly. Always
`terraform init -reconfigure` when switching envs.

## What's tunable, and where

| Lives in        | Examples                                                    |
| --------------- | ----------------------------------------------------------- |
| `config/<env>.tfvars`     | OCIDs, CIDRs, sizing (Mew/OKE), retention, the public-NLB toggle, SSH key, operator and Modal CIDRs |
| `config/<env>-backend.hcl`| Bucket / namespace / state key for remote state              |
| `variables.tf`            | The full list of tunables, with descriptions and defaults    |
| `main.tf`                 | The wiring; module calls feed every variable into the right module |

If you want to tune something we don't currently expose, **add a variable
to `variables.tf`** and pass it through the relevant `module "..."` call
in `main.tf`. The module itself probably already has a matching input.

## Bootstrap (run once)

`bootstrap/` creates the compartment hierarchy and the Object Storage
bucket that holds remote state for the main config below. It uses local
state — there's no chicken-and-egg way to create the state bucket
remotely.

```bash
cd infrastructure/terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars
# Fill in tenancy_ocid (from ~/.oci/config: the `tenancy=` line).
terraform init
terraform apply
```

Save the outputs — you need `compartment_dev_id`, `compartment_prod_id`,
`tfstate_bucket_name`, and `tfstate_namespace` for the next step.

## Apply an environment

```bash
cd infrastructure/terraform

# 1. Per-env config files (gitignored).
cp config/dev.tfvars.example      config/dev.tfvars
cp config/dev-backend.hcl.example config/dev-backend.hcl
# Edit both: paste OCIDs, namespace, SSH key, etc.

# 2. Customer Secret Key for the S3-compat backend. NOT your OCI API key.
#    Identity → Users → <your user> → Customer Secret Keys → Generate.
#
#    AWS_ACCESS_KEY_ID    = the OCI "Access key" (alphanumeric, no slashes).
#    AWS_SECRET_ACCESS_KEY = the OCI "Secret key" (base64-ish, may contain /).
#    Swapping them yields an "IncompleteSignature" error because the `/` in
#    a swapped key breaks AWS SigV4 credential parsing.
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Disable the AWS SDK v2 default of "always checksum with aws-chunked"; OCI's
# S3-compat returns 501 NotImplemented on aws-chunked uploads.
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

# 3. Init the backend and apply.
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform plan  -var-file=config/dev.tfvars -out tfplan
terraform apply tfplan
```

Switch to prod by re-running step 3 with the prod files:

```bash
terraform init -reconfigure -backend-config=config/prod-backend.hcl
terraform plan  -var-file=config/prod.tfvars -out tfplan
terraform apply tfplan
```

`-reconfigure` forces Terraform to forget the previous backend wiring; if
you skip it after switching, Terraform will refuse to proceed.

## After apply

1. **Rotate Vault placeholders.** For each placeholder secret the vault
   module created (`modal-token`, `slack-webhook-url`,
   `pagerduty-integration-key`):
   ```bash
   oci vault secret update-base64 \
     --secret-id <ocid> \
     --secret-content-content "$(printf '<real value>' | base64)"
   ```
2. **Issue a Modal API key for the Modal IAM user** (see `modal_user_id`
   output). Upload the public half to OCI; ship the private half +
   fingerprint into the `oci-credentials` Modal Secret:
   ```bash
   oci iam user api-key upload \
     --user-id <modal_user_id> \
     --key-file path/to/modal-public.pem
   ```
3. **Pull a kubeconfig:**
   ```bash
   oci ce cluster create-kubeconfig \
     --cluster-id <oke_cluster_id> \
     --file $HOME/.kube/config-kanto-dev \
     --region us-sanjose-1 \
     --token-version 2.0.0
   ```

## Adding a new resource

- **A new bucket / stream / log group:** add it to the relevant module
  (`modules/object_storage/`, `modules/streaming/`, etc.). Modules use
  `for_each` over a local map so adding usually means appending one line.
- **A new tunable:** add a `variable` to `variables.tf`, pass it through
  the matching `module "..."` block in `main.tf`, and document it in
  `config/<env>.tfvars.example`.
- **A new module entirely:** drop a folder under `modules/` with
  `versions.tf`, `variables.tf`, `main.tf`, `outputs.tf`, `README.md`,
  then add a `module "..." {}` block to `main.tf`.

Every change must pass `terraform fmt`, `terraform validate`, `tflint`,
and `tfsec` — these run automatically in CI on every PR.

## Tearing down

Reverse the provisioning order. Most modules have `prevent_destroy` on
load-bearing resources (vault, KMS key, OKE cluster, Mew DB system, state
bucket, compartments) — explicitly remove that lifecycle block before a
real `terraform destroy`, and only do that in dev.

```bash
# Main env destroy:
terraform init -reconfigure -backend-config=config/dev-backend.hcl
terraform destroy -var-file=config/dev.tfvars

# Bootstrap destroy (only after every env is destroyed):
cd bootstrap
terraform destroy
```

In prod, do not destroy. If you need to retire prod, take a final backup
of Mew + the `kanto-embeddings-prod` bucket first; the `prevent_destroy`
blocks make this delay unavoidable.

## Cost expectations

Rough monthly spend per environment, excluding Modal (Task 7) and any
backfill burst. Numbers from public OCI list pricing in May 2026; verify
before relying on them.

| Component                              | Dev       | Prod        |
| -------------------------------------- | --------- | ----------- |
| OKE cluster (Enhanced)                 | $73       | $73         |
| OKE worker nodes (E5.Flex, on-demand)  | ~$70      | ~$310       |
| Mew (PostgreSQL DB system)             | ~$100     | ~$1,150     |
| Mew storage (block, regionally durable)| ~$5       | ~$22        |
| OCI Streaming pool + streams           | ~$10      | ~$30        |
| Object Storage (buckets, std + archive)| ~$5       | ~$50–250    |
| Network LB (Mew public, prod only)     | $0        | ~$15        |
| NAT gateway egress                     | ~$5       | ~$30        |
| KMS Vault + key                        | ~$3       | ~$3         |
| Logging                                | ~$5       | ~$20        |
| **Estimated total**                    | **~$280** | **~$1,700+**|

Object Storage and egress numbers grow with backfill volume.

## Testing

Local:

```bash
cd infrastructure/terraform
terraform fmt -recursive
terraform init -backend=false
terraform validate
```

CI: see `.github/workflows/terraform.yml`. Every PR touching
`infrastructure/terraform/**` runs `fmt`, `validate` per directory,
`tflint` (with the OCI ruleset), and `tfsec` (HIGH+).
