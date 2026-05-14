output "server_id" {
  value = azurerm_postgresql_flexible_server.this.id
}

output "fqdn" {
  description = "Private FQDN resolved via the VNet-linked private DNS zone."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "port" {
  value = 5432
}

output "database_name" {
  value = azurerm_postgresql_flexible_server_database.kanto.name
}

output "admin_username" {
  value = var.admin_username
}
