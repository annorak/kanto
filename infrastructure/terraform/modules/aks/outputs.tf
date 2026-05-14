output "cluster_id" {
  value = azurerm_kubernetes_cluster.this.id
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "cluster_kubernetes_version" {
  value = azurerm_kubernetes_cluster.this.kubernetes_version
}

output "oidc_issuer_url" {
  description = "OIDC issuer URL of the AKS cluster. Used when creating federated identity credentials that trust K8s service accounts."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "kubelet_identity_object_id" {
  description = "Object ID of the kubelet identity (system-managed by AKS). Used for AcrPull / blob-data role assignments scoped to the kubelet."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}
