output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "vnet_name" {
  value = azurerm_virtual_network.this.name
}

output "subnet_nodes_id" {
  value = azurerm_subnet.nodes.id
}

output "subnet_pods_id" {
  value = azurerm_subnet.pods.id
}

output "subnet_lb_id" {
  value = azurerm_subnet.lb.id
}

output "subnet_mew_id" {
  value = azurerm_subnet.mew.id
}

output "private_dns_zone_postgres_id" {
  value = azurerm_private_dns_zone.postgres.id
}

output "private_dns_zone_postgres_name" {
  value = azurerm_private_dns_zone.postgres.name
}
