output "id" {
  value = azurerm_key_vault.this.id
}

output "name" {
  value = azurerm_key_vault.this.name
}

output "vault_uri" {
  value = azurerm_key_vault.this.vault_uri
}

output "aks_etcd_key_id" {
  description = "Versionless key ID for AKS etcd encryption."
  value       = azurerm_key_vault_key.aks_etcd.versionless_id
}

output "mew_password_secret_id" {
  description = "Key Vault secret resource ID for the Mew admin password."
  value       = azurerm_key_vault_secret.mew_password.id
}

output "mew_password_value" {
  description = "Plaintext Mew admin password. Consumed by the mew module to bootstrap the Flexible Server admin user. Sensitive."
  value       = random_password.mew.result
  sensitive   = true
}
