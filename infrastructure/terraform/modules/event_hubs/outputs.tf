output "namespace_id" {
  value = azurerm_eventhub_namespace.this.id
}

output "namespace_name" {
  value = azurerm_eventhub_namespace.this.name
}

output "kafka_bootstrap_servers" {
  description = "Kafka-compatible endpoint. Clients connect with SASL/PLAIN (username = '$ConnectionString', password = the namespace connection string) or SASL/OAUTHBEARER (via Azure AD)."
  value       = "${azurerm_eventhub_namespace.this.name}.servicebus.windows.net:9093"
}

output "event_hub_ids" {
  description = "Map of topic name -> Event Hub resource ID. Consumed by IAM module for per-topic data-plane role assignments."
  value       = { for k, h in azurerm_eventhub.this : k => h.id }
}

output "event_hub_names" {
  value = [for h in azurerm_eventhub.this : h.name]
}
