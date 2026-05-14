resource "random_id" "namespace_suffix" {
  byte_length = 3
}

# Event Hubs namespace. Standard tier required for the Kafka protocol surface
# (Basic is AMQP-only). 1 throughput unit handles dev load comfortably; auto-
# inflate up to 2 in case of spikes. Costs ~$22/mo per TU.
resource "azurerm_eventhub_namespace" "this" {
  name                = "${var.name_prefix}-eh-${random_id.namespace_suffix.hex}"
  resource_group_name = var.resource_group_name
  location            = var.region

  sku                      = "Standard"
  capacity                 = 1
  auto_inflate_enabled     = true
  maximum_throughput_units = 2

  # Kafka surface lives at <namespace>.servicebus.windows.net:9093 by default
  # on Standard tier; no explicit toggle needed in current API. Clients use
  # SASL/PLAIN with the namespace's connection string OR Azure AD OAuth.

  public_network_access_enabled = true # Modal connects from outside the VNet
  minimum_tls_version           = "1.2"

  tags = var.tags
}

resource "azurerm_eventhub" "this" {
  for_each = local.event_hubs

  name              = each.key
  namespace_id      = azurerm_eventhub_namespace.this.id
  partition_count   = each.value.partitions
  message_retention = each.value.retention_days
}
