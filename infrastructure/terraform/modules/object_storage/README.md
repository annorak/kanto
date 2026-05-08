# object_storage module

Three Object Storage buckets per environment, named `kanto-{role}-{env}`:

| Role         | Lifecycle                                  | Holds                                |
| ------------ | ------------------------------------------ | ------------------------------------ |
| `proteins`   | Archive after `hot_to_archive_days` (90d). | Protein FASTAs from Snorlax.         |
| `embeddings` | Archive after `hot_to_archive_days` (90d). | Per-protein parquet from Ditto.      |
| `metadata`   | Stays hot indefinitely.                    | NCBI metadata cache used by Growlithe. |

All buckets: `NoPublicAccess`, versioning on, encrypted with the per-env
KMS key passed in.

The `tfstate` bucket is created by `bootstrap/`, not here, so a destroy of
this module cannot affect Terraform state.

## Inputs

| Name                  | Type          | Required | Description                                                          |
| --------------------- | ------------- | -------- | -------------------------------------------------------------------- |
| `compartment_id`      | `string`      | yes      | Compartment for buckets.                                             |
| `environment`         | `string`      | yes      | Suffix for bucket names (`dev`, `prod`).                             |
| `kms_key_id`          | `string`      | yes      | KMS key OCID for bucket encryption (from the vault module).          |
| `freeform_tags`       | `map(string)` | no       | Tags applied to every resource.                                      |
| `hot_to_archive_days` | `number`      | no       | Days before proteins/embeddings transition to Archive. Default `90`. |

## Outputs

`namespace`, `bucket_names` (map), `bucket_proteins`, `bucket_embeddings`,
`bucket_metadata`.
