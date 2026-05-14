output "storage_account_id" {
  value = azurerm_storage_account.this.id
}

output "storage_account_name" {
  value = azurerm_storage_account.this.name
}

output "blob_endpoint" {
  value = azurerm_storage_account.this.primary_blob_endpoint
}

output "container_names" {
  description = "Map of role -> container name."
  value       = { for k, c in azurerm_storage_container.this : k => c.name }
}

output "proteins_container_id" {
  value = azurerm_storage_container.this["proteins"].resource_manager_id
}

output "embeddings_container_id" {
  value = azurerm_storage_container.this["embeddings"].resource_manager_id
}

output "metadata_container_id" {
  value = azurerm_storage_container.this["metadata"].resource_manager_id
}
