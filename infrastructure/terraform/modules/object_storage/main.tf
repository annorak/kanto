data "oci_objectstorage_namespace" "tenancy" {
  compartment_id = var.compartment_id
}

resource "oci_objectstorage_bucket" "this" {
  for_each = local.buckets

  compartment_id = var.compartment_id
  namespace      = data.oci_objectstorage_namespace.tenancy.namespace
  name           = "kanto-${each.key}-${var.environment}"

  # Per Task 2 spec section 8: encryption at rest with OCI-managed keys.
  # Bring-your-own-key would require granting the Object Storage service
  # principal use-keys access to our Vault, which is more setup than the
  # spec asks for. Default encryption is FIPS-140-2 validated AES-256.
  access_type   = "NoPublicAccess"
  versioning    = "Enabled"
  freeform_tags = var.freeform_tags
}

# OCI Object Storage runs lifecycle transitions under a service principal
# ("objectstorage-<region>"). Without this IAM grant, creating a lifecycle
# policy on a bucket returns 400-InsufficientServicePermissions. Scoped to
# this compartment only.
resource "oci_identity_policy" "lifecycle_service_access" {
  compartment_id = var.compartment_id
  name           = "kanto-${var.environment}-objectstorage-lifecycle"
  description    = "Allows the Object Storage service principal to manage objects so lifecycle policies (archive/delete) can run."
  freeform_tags  = var.freeform_tags

  statements = [
    "Allow service objectstorage-${var.region} to manage object-family in compartment id ${var.compartment_id}",
  ]
}

# IAM propagation is eventually-consistent; without a wait the lifecycle
# resource can fire before the policy is enforced and the API returns
# InsufficientServicePermissions. Mirrors the vault.kms_policy_propagation
# pattern.
resource "time_sleep" "lifecycle_policy_propagation" {
  depends_on      = [oci_identity_policy.lifecycle_service_access]
  create_duration = "60s"
}

# Lifecycle: archive after N days for buckets that opt in. The metadata
# bucket sets archive_days=0 and gets no rule.
resource "oci_objectstorage_object_lifecycle_policy" "archive" {
  for_each = { for k, v in local.buckets : k => v if v.archive_days > 0 }

  namespace = data.oci_objectstorage_namespace.tenancy.namespace
  bucket    = oci_objectstorage_bucket.this[each.key].name

  rules {
    name        = "archive-after-${each.value.archive_days}d"
    action      = "ARCHIVE"
    is_enabled  = true
    target      = "objects"
    time_amount = each.value.archive_days
    time_unit   = "DAYS"
  }

  depends_on = [time_sleep.lifecycle_policy_propagation]
}
