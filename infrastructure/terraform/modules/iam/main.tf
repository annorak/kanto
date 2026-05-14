# Dynamic group for OKE worker nodes. Membership is automatic: any compute
# instance launched into the env compartment becomes a member, which means
# OKE worker pods can use the instance principal to call OCI APIs.
resource "oci_identity_dynamic_group" "oke_workers" {
  compartment_id = var.tenancy_ocid
  name           = "kanto-${var.environment}-oke-workers"
  description    = "OKE worker node instance principals for ${var.environment}."
  matching_rule  = "ALL {instance.compartment.id = '${var.compartment_id}'}"
  freeform_tags  = var.freeform_tags
}

# Modal needs OCI credentials to push to Object Storage and read/write Mew.
# We create the user + group; the operator generates an API key for this
# user post-apply (`oci iam user api-key upload ...`) and stores it in the
# `oci-credentials` Modal Secret. Per the design doc Section 12.
resource "oci_identity_user" "modal" {
  compartment_id = var.tenancy_ocid
  name           = "kanto-${var.environment}-modal"
  description    = "Modal-side credentials for the Ditto function (${var.environment})."
  email          = "modal-${var.environment}@kanto.invalid"
  freeform_tags  = var.freeform_tags
}

resource "oci_identity_group" "modal" {
  compartment_id = var.tenancy_ocid
  name           = "kanto-${var.environment}-modal"
  description    = "Group whose policies grant Modal the OCI access it needs."
  freeform_tags  = var.freeform_tags
}

resource "oci_identity_user_group_membership" "modal" {
  group_id = oci_identity_group.modal.id
  user_id  = oci_identity_user.modal.id
}

# OKE worker policies. Scoped to the env compartment so a buggy dev policy
# cannot grant access to prod.
resource "oci_identity_policy" "oke_workers" {
  compartment_id = var.compartment_id
  name           = "kanto-${var.environment}-oke-workers"
  description    = "Permissions for OKE worker pods running Kanto services."
  freeform_tags  = var.freeform_tags

  statements = [
    # Read protein FASTAs (Snorlax has to read them back if it ever needs to;
    # also Alakazam reads embeddings parquet via OS GET).
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to read buckets in compartment id ${var.compartment_id}",
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to read objects in compartment id ${var.compartment_id} where any {target.bucket.name='${var.bucket_proteins}', target.bucket.name='${var.bucket_embeddings}', target.bucket.name='${var.bucket_metadata}'}",
    # Snorlax writes proteins; Growlithe writes metadata. Embeddings is
    # written by Modal not OKE; intentionally not granted here.
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to manage objects in compartment id ${var.compartment_id} where any {target.bucket.name='${var.bucket_proteins}', target.bucket.name='${var.bucket_metadata}'}",
    # Vault: read secrets so services can fetch the Mew password and any
    # other credentials Helm charts inject at runtime.
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to read secret-family in compartment id ${var.compartment_id}",
    # Streaming: produce/consume on every stream in the pool.
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to use stream-push in compartment id ${var.compartment_id}",
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to use stream-pull in compartment id ${var.compartment_id}",
    # Mew (OCI Database for PostgreSQL) has no IAM-side "connect" verb:
    # reachability is gated by the Mew NSG (network module) and DB auth by
    # Postgres credentials fetched from Vault. No IAM statement needed here.
    # Logging: emit logs to the env's log groups.
    "Allow dynamic-group ${oci_identity_dynamic_group.oke_workers.name} to use log-content in compartment id ${var.compartment_id}",
  ]
}

# Modal policies. Strictly: read proteins, write embeddings, push to
# kanto.embedded, connect to Mew. Nothing else.
resource "oci_identity_policy" "modal" {
  compartment_id = var.compartment_id
  name           = "kanto-${var.environment}-modal"
  description    = "Permissions Modal (Ditto) needs to read FASTAs, write embeddings, write to Mew."
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow group ${oci_identity_group.modal.name} to read buckets in compartment id ${var.compartment_id} where target.bucket.name='${var.bucket_proteins}'",
    "Allow group ${oci_identity_group.modal.name} to read objects in compartment id ${var.compartment_id} where target.bucket.name='${var.bucket_proteins}'",
    "Allow group ${oci_identity_group.modal.name} to read buckets in compartment id ${var.compartment_id} where target.bucket.name='${var.bucket_embeddings}'",
    "Allow group ${oci_identity_group.modal.name} to manage objects in compartment id ${var.compartment_id} where target.bucket.name='${var.bucket_embeddings}'",
    "Allow group ${oci_identity_group.modal.name} to use stream-push in compartment id ${var.compartment_id} where target.stream.name='kanto.embedded'",
    # No IAM grant for Mew connection — Modal hits the public NLB (allow-listed
    # in the Mew NSG) and authenticates with Postgres credentials Modal-side.
  ]
}
