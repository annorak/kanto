output "environment" {
  value = var.environment
}

output "region" {
  value = var.region
}

output "tenancy_ocid" {
  value = var.tenancy_ocid
}

output "compartment_id" {
  value = var.compartment_id
}

output "vcn_id" {
  value = module.network.vcn_id
}

output "oke_cluster_id" {
  description = "Use with `oci ce cluster create-kubeconfig` to get a kubeconfig."
  value       = module.oke.cluster_id
}

output "oke_kubernetes_version" {
  value = module.oke.cluster_kubernetes_version
}

output "mew_host" {
  description = "Private IP of the Mew Postgres primary."
  value       = module.mew.private_endpoint_host
}

output "mew_port" {
  value = module.mew.port
}

output "mew_database" {
  value = module.mew.database_name
}

output "mew_admin_username" {
  value = module.mew.admin_username
}

output "mew_password_secret_id" {
  description = "Vault secret OCID; resolve with `oci vault secret get`."
  value       = module.vault.mew_password_secret_id
}

output "mew_public_endpoint" {
  description = "Public NLB IP (prod). Null when mew_enable_public_endpoint is false."
  value       = module.mew.public_endpoint_ip
}

output "kafka_bootstrap_servers" {
  value = module.streaming.kafka_bootstrap_servers
}

output "object_storage_namespace" {
  value = module.object_storage.namespace
}

output "object_storage_buckets" {
  value = module.object_storage.bucket_names
}

output "vault_id" {
  value = module.vault.vault_id
}

output "modal_user_id" {
  description = "OCI IAM user for the Modal-side Ditto function. Operator uploads an API key to this user."
  value       = module.iam.modal_user_id
}

output "log_group_id" {
  value = module.logging.log_group_id
}
