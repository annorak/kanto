# object_storage module

Three Object Storage buckets per environment, named `kanto-{role}-{env}`:

| Role         | Lifecycle                                  | Holds                                |
| ------------ | ------------------------------------------ | ------------------------------------ |
| `proteins`   | Archive after `hot_to_archive_days` (90d). | Protein FASTAs from Snorlax.         |
| `embeddings` | Archive after `hot_to_archive_days` (90d). | Per-protein parquet from Ditto.      |
| `metadata`   | Stays hot indefinitely.                    | NCBI metadata cache used by Growlithe. |

All buckets: `NoPublicAccess`, versioning on, encrypted at rest with
**OCI-managed keys** (FIPS-140-2 validated AES-256). The task spec section 8
explicitly asks for OCI-managed keys here; bring-your-own-key would require
granting the Object Storage service principal use-keys access to our Vault,
which is more setup than the spec asks for.

The module also creates an IAM policy granting the Object Storage service
principal (`objectstorage-<region>`) `manage object-family` in this
compartment. OCI requires this grant for the lifecycle engine to apply
archive/delete transitions; without it lifecycle policy creation fails
with `400-InsufficientServicePermissions`.

The `tfstate` bucket is created by `bootstrap/`, not here, so a destroy of
this module cannot affect Terraform state.

## Inputs

| Name                  | Type          | Required | Description                                                          |
| --------------------- | ------------- | -------- | -------------------------------------------------------------------- |
| `compartment_id`      | `string`      | yes      | Compartment for buckets.                                             |
| `region`              | `string`      | yes      | OCI region; used in the Object Storage service-principal name.       |
| `environment`         | `string`      | yes      | Suffix for bucket names (`dev`, `prod`).                             |
| `freeform_tags`       | `map(string)` | no       | Tags applied to every resource.                                      |
| `hot_to_archive_days` | `number`      | no       | Days before proteins/embeddings transition to Archive. Default `90`. |

## Outputs

`namespace`, `bucket_names` (map), `bucket_proteins`, `bucket_embeddings`,
`bucket_metadata`.
