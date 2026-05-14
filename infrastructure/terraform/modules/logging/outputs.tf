output "workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}

output "workspace_name" {
  value = azurerm_log_analytics_workspace.this.name
}

output "workspace_customer_id" {
  description = "Workspace's customer/cluster-id GUID, the value agents use to authenticate."
  value       = azurerm_log_analytics_workspace.this.workspace_id
}
