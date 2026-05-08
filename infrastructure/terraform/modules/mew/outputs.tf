output "db_system_id" {
  value = oci_psql_db_system.this.id
}

output "private_endpoint_fqdn" {
  description = "Private DNS endpoint OKE workers connect to."
  value       = oci_psql_db_system.this.network_details[0].primary_db_endpoint_private_ip
}

output "private_endpoint_host" {
  description = "Convenience: the primary instance private IP."
  value       = oci_psql_db_system.this.instances_details[0].private_ip
}

output "port" {
  value = 5432
}

output "database_name" {
  description = "Default DB created by OCI; migrations run against this."
  value       = "postgres"
}

output "admin_username" {
  value = var.admin_username
}

output "public_endpoint_ip" {
  description = "Public IP of the NLB (when enabled); null otherwise."
  value       = var.enable_public_endpoint ? oci_network_load_balancer_network_load_balancer.mew_public[0].ip_addresses[0].ip_address : null
}
