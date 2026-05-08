# iam module

Identity layer for one environment. Three principals:

1. **OKE worker dynamic group** — instance-principal-based group containing
   every compute instance in the env compartment. Used by Snorlax,
   Growlithe, Alakazam, Chatot, the API service.
2. **Modal IAM user + group** — credentials for the Ditto function on Modal.
   Terraform creates the user and group; the operator generates the API
   key post-apply and ships it to Modal as the `oci-credentials` Secret.
3. (Implicit) **Audit logging.** OCI Audit logs every IAM operation
   tenancy-wide by default; nothing to provision here.

## Permission summary

| Principal     | Buckets                                | Streams                                          | Mew              | Vault           |
| ------------- | -------------------------------------- | ------------------------------------------------ | ---------------- | --------------- |
| OKE workers   | read all; write `proteins`, `metadata` | push + pull all                                  | postgres-connect | read secrets    |
| Modal         | read `proteins`; write `embeddings`    | push `kanto.embedded` only                       | postgres-connect | (none)          |

The OKE policy is broader than ideal because we're treating the cluster as
one trust boundary. Per-service policies (Growlithe restricted to metadata,
Chatot read-only, etc.) are the v2 hardening pass.

## Inputs

| Name              | Type     | Required | Description                                                |
| ----------------- | -------- | -------- | ---------------------------------------------------------- |
| `tenancy_ocid`    | `string` | yes      | Required; dynamic groups and users live at tenancy root.   |
| `compartment_id`  | `string` | yes      | Env compartment. Policies are scoped here.                 |
| `environment`     | `string` | yes      | `dev` / `prod`; used in identity resource names.           |
| `bucket_proteins` | `string` | yes      | Name of the proteins bucket.                               |
| `bucket_embeddings` | `string` | yes    | Name of the embeddings bucket.                             |
| `bucket_metadata` | `string` | yes      | Name of the metadata bucket.                               |
| `freeform_tags`   | `map`    | no       | Tags applied to every resource.                            |

## Outputs

`oke_workers_dynamic_group_id`, `oke_workers_dynamic_group_name`,
`modal_user_id`, `modal_group_id`.

## After apply

```bash
oci iam user api-key upload \
  --user-id <modal_user_id from outputs> \
  --key-file path/to/modal-public.pem
```

Then ship the matching private key + fingerprint into the `oci-credentials`
Modal Secret.
