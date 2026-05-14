resource "oci_kms_vault" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.name_prefix}-vault"
  vault_type     = "DEFAULT"
  freeform_tags  = var.freeform_tags

  # Vault deletion is a 7-90 day soft-delete; never want to lose this by accident.
  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_kms_key" "master" {
  compartment_id      = var.compartment_id
  display_name        = "${var.name_prefix}-master-key"
  management_endpoint = oci_kms_vault.this.management_endpoint
  freeform_tags       = var.freeform_tags

  key_shape {
    algorithm = "AES"
    length    = 32 # 256 bit
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Mew database password. Generated locally; the value is stored in Terraform
# state (encrypted in the OCI Object Storage state bucket) and as a Vault
# secret. The operator can rotate via `oci vault secret update-base64` and
# Terraform will not overwrite the rotation (see lifecycle below).
resource "random_password" "mew" {
  length      = 32
  special     = true
  min_lower   = 4
  min_upper   = 4
  min_numeric = 4
  min_special = 4
  # OCI Postgres rejects these in passwords.
  override_special = "!#$%&*()-_=+[]{}<>?"
}

resource "oci_vault_secret" "mew_password" {
  compartment_id = var.compartment_id
  vault_id       = oci_kms_vault.this.id
  key_id         = oci_kms_key.master.id
  secret_name    = "${var.name_prefix}-mew-password"
  description    = "Initial Mew (Postgres) admin password. Rotate post-apply."
  freeform_tags  = var.freeform_tags

  secret_content {
    content_type = "BASE64"
    content      = base64encode(random_password.mew.result)
  }

  lifecycle {
    ignore_changes = [secret_content]
  }
}

# Placeholder slots populated manually by the operator. We create them so the
# secret OCIDs are stable and Helm charts in later tasks can reference them
# by name; the operator runs `oci vault secret update-base64` to set values.
resource "oci_vault_secret" "placeholder" {
  for_each = toset(local.placeholder_secrets)

  compartment_id = var.compartment_id
  vault_id       = oci_kms_vault.this.id
  key_id         = oci_kms_key.master.id
  secret_name    = "${var.name_prefix}-${each.key}"
  description    = "Placeholder; rotate value via OCI CLI before service startup."
  freeform_tags  = var.freeform_tags

  secret_content {
    content_type = "BASE64"
    content      = base64encode("REPLACE_ME")
  }

  lifecycle {
    ignore_changes = [secret_content]
  }
}

# Master-key access for the principals that encrypt with it. Two principal
# kinds matter here:
#   - `service oke` / `service streaming` cover service-managed operations
#     (e.g. stream pool encryption).
#   - The cluster's own resource principal is what ENHANCED_CLUSTER uses
#     during the "Provisioning Kubernetes components" phase to encrypt etcd.
#     Without this grant CreateCluster fails with "Cluster resource principal
#     does not have permission to use KMS key" — and OCI's CLI surfaces give
#     no lifecycle-details or work-request-error for this failure mode; only
#     the Console's Work Requests log shows the message.
# Every grant is scoped to this exact key via target.key.id.
resource "oci_identity_policy" "kms_service_access" {
  compartment_id = var.compartment_id
  name           = "${var.name_prefix}-kms-service-access"
  description    = "Allows the OKE cluster resource principal and OCI services (oke, streaming) to use our master key."
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow service oke to use keys in compartment id ${var.compartment_id} where target.key.id = '${oci_kms_key.master.id}'",
    "Allow service streaming to use keys in compartment id ${var.compartment_id} where target.key.id = '${oci_kms_key.master.id}'",
    "Allow any-user to use keys in compartment id ${var.compartment_id} where ALL {request.principal.type = 'cluster', target.key.id = '${oci_kms_key.master.id}'}",
  ]
}

# IAM propagation in OCI is eventually-consistent. Without a wait, downstream
# resources (OKE cluster, streaming pool) can fire their KMS-using API calls
# before the policy is enforced. 60 seconds is conservative; in practice OCI
# propagates in 10-30. The `triggers` block forces this sleep to re-run whenever
# the policy statements change, so a future statement edit (e.g. for a new
# principal) doesn't silently race the propagation.
resource "time_sleep" "kms_policy_propagation" {
  depends_on      = [oci_identity_policy.kms_service_access]
  create_duration = "60s"

  triggers = {
    policy_id       = oci_identity_policy.kms_service_access.id
    statements_hash = sha256(join("\n", oci_identity_policy.kms_service_access.statements))
  }
}
