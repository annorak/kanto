resource "oci_streaming_stream_pool" "this" {
  compartment_id = var.compartment_id
  name           = "kanto-${var.environment}"
  freeform_tags  = var.freeform_tags

  kafka_settings {
    auto_create_topics_enable = false
    log_retention_hours       = 168
    num_partitions            = 1
  }

  custom_encryption_key {
    kms_key_id = var.kms_key_id
  }
}

resource "oci_streaming_stream" "this" {
  for_each = local.streams

  name               = each.key
  partitions         = each.value.partitions
  retention_in_hours = each.value.retention_hours
  stream_pool_id     = oci_streaming_stream_pool.this.id
  freeform_tags      = var.freeform_tags
}
