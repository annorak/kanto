# Tenancy-wide audit logs (OCI Audit) capture every IAM, Vault, and Object
# Storage operation by default — no resources are needed here for that.
# This module covers the application-level log group used by:
#   - the OKE Logging Operator (Task 5) for pod stdout/stderr
#   - Modal's log forwarder (configured Modal-side in Task 7)
resource "oci_logging_log_group" "app" {
  compartment_id = var.compartment_id
  display_name   = "${var.name_prefix}-app"
  description    = "Application logs from Kanto services on OKE plus Modal-forwarded logs."
  freeform_tags  = var.freeform_tags
}

resource "oci_logging_log" "app" {
  log_group_id       = oci_logging_log_group.app.id
  display_name       = "${var.name_prefix}-app"
  log_type           = "CUSTOM"
  is_enabled         = true
  retention_duration = var.app_log_retention_days
  freeform_tags      = var.freeform_tags
}
