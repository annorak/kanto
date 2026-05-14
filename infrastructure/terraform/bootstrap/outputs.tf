output "resource_group_shared_name" {
  description = "Resource group holding the tfstate storage account."
  value       = azurerm_resource_group.shared.name
}

output "resource_group_dev_name" {
  description = "Resource group for the dev environment. Passed into the root module as `resource_group_name`."
  value       = azurerm_resource_group.dev.name
}

output "resource_group_prod_name" {
  description = "Resource group for the prod environment."
  value       = azurerm_resource_group.prod.name
}

output "tfstate_storage_account_name" {
  description = "Storage account holding Terraform state. Paste into config/<env>-backend.hcl."
  value       = azurerm_storage_account.tfstate.name
}

output "tfstate_container_name" {
  description = "Blob container inside the tfstate storage account."
  value       = azurerm_storage_container.tfstate.name
}
