output "aks_workload_identity_id" {
  description = "Resource ID of the AKS-workloads user-assigned managed identity."
  value       = azurerm_user_assigned_identity.aks_workloads.id
}

output "aks_workload_identity_client_id" {
  description = "Client ID of the AKS-workloads UAMI. Annotate K8s service accounts with this so pods can fetch AAD tokens via workload identity."
  value       = azurerm_user_assigned_identity.aks_workloads.client_id
}

output "aks_workload_identity_principal_id" {
  description = "Object ID of the AKS-workloads UAMI (used in additional role assignments)."
  value       = azurerm_user_assigned_identity.aks_workloads.principal_id
}

output "modal_identity_id" {
  value = azurerm_user_assigned_identity.modal.id
}

output "modal_identity_client_id" {
  description = "Client ID of the Modal UAMI. Modal-side function uses this with workload identity federation."
  value       = azurerm_user_assigned_identity.modal.client_id
}
