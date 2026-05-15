output "environment" {
  value = var.environment
}

output "region" {
  value = var.region
}

output "subscription_id" {
  value = var.subscription_id
}

output "resource_group_name" {
  value = data.azurerm_resource_group.this.name
}

output "vnet_id" {
  value = module.network.vnet_id
}

output "aks_cluster_name" {
  description = "Use with `az aks get-credentials -g <rg> -n <name>` to fetch a kubeconfig."
  value       = module.aks.cluster_name
}

output "aks_kubernetes_version" {
  value = module.aks.cluster_kubernetes_version
}

output "mew_fqdn" {
  description = "Postgres Flexible Server FQDN. Routable from inside the VNet. Null when deploy_mew is false."
  value       = length(module.mew) > 0 ? module.mew[0].fqdn : null
}

output "mew_database" {
  value = length(module.mew) > 0 ? module.mew[0].database_name : null
}

output "mew_admin_username" {
  value = length(module.mew) > 0 ? module.mew[0].admin_username : null
}

output "mew_password_secret_id" {
  description = "Key Vault secret ID. Resolve with `az keyvault secret show --id <value>`."
  value       = module.key_vault.mew_password_secret_id
}

output "event_hubs_namespace" {
  value = module.event_hubs.namespace_name
}

output "event_hubs_kafka_bootstrap" {
  description = "Kafka-compatible endpoint: <namespace>.servicebus.windows.net:9093"
  value       = module.event_hubs.kafka_bootstrap_servers
}

output "object_storage_account_name" {
  value = module.object_storage.storage_account_name
}

output "object_storage_containers" {
  value = module.object_storage.container_names
}

output "key_vault_uri" {
  value = module.key_vault.vault_uri
}

output "log_analytics_workspace_id" {
  value = module.logging.workspace_id
}

output "aks_workload_identity_client_id" {
  description = "Client ID of the AKS workload-identity user-assigned managed identity. Annotate K8s service accounts with this so pods can fetch Azure AD tokens."
  value       = module.iam.aks_workload_identity_client_id
}

output "modal_identity_client_id" {
  description = "Client ID of the Modal-side federated managed identity. Empty when modal_oidc_issuer is empty (dev)."
  value       = module.iam.modal_identity_client_id
}
