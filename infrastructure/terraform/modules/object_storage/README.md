# object_storage module

A single Azure Storage Account per environment with three blob containers
named `kanto-{role}-{env}`:

| Role         | Lifecycle                                                  | Holds                                  |
| ------------ | ---------------------------------------------------------- | -------------------------------------- |
| `proteins`   | Hot → Cool at 30 d → Archive at 90 d                       | Protein FASTAs from Snorlax            |
| `embeddings` | Hot → Cool at 30 d → Archive at 90 d                       | Per-protein parquet from Ditto         |
| `metadata`   | Always Hot                                                 | NCBI metadata cache used by Growlithe  |

All containers: private (no public anonymous access), versioning + soft
delete enabled on the account, encrypted at rest with **platform-managed
keys** (FIPS-140-2 validated AES-256). The task spec asks for managed keys;
bringing-your-own-key would require granting the Storage service Crypto
User on our Key Vault, which is more setup than the spec asks for.

Lifecycle transitions are configured via a single
`azurerm_storage_management_policy` resource that iterates over the
containers map. The metadata container is excluded (`archive = false`).

The `tfstate` storage account is created by `bootstrap/`, not here, so a
destroy of this module cannot affect Terraform state.

## Inputs

| Name                  | Required | Description                                                                |
| --------------------- | -------- | -------------------------------------------------------------------------- |
| `resource_group_name` | yes      | Per-env resource group                                                     |
| `region`              | yes      | Azure region                                                               |
| `environment`         | yes      | Suffix for container names (`dev`, `prod`)                                 |
| `hot_to_cool_days`    | no       | Default 30                                                                 |
| `hot_to_archive_days` | no       | Default 90                                                                 |
| `tags`                | no       | Resource tags                                                              |

## Outputs

`storage_account_id`, `storage_account_name`, `blob_endpoint`,
`container_names` (map), `{proteins,embeddings,metadata}_container_id`.
