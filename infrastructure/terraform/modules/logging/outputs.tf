output "log_group_id" {
  value = oci_logging_log_group.app.id
}

output "app_log_id" {
  value = oci_logging_log.app.id
}
