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
