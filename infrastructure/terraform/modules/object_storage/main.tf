data "oci_objectstorage_namespace" "tenancy" {
  compartment_id = var.compartment_id
}

resource "oci_objectstorage_bucket" "this" {
  for_each = local.buckets

  compartment_id = var.compartment_id
  namespace      = data.oci_objectstorage_namespace.tenancy.namespace
  name           = "kanto-${each.key}-${var.environment}"

  access_type   = "NoPublicAccess"
  versioning    = "Enabled"
  kms_key_id    = var.kms_key_id
  freeform_tags = var.freeform_tags
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
}
