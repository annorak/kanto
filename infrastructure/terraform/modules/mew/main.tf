# Azure Database for PostgreSQL Flexible Server with pgvector preinstalled.
# Task 3 migrations run `CREATE EXTENSION vector;` on the application DB.
#
# Two networking modes, switched by vnet_integration_enabled:
#   true  — private-only via delegated subnet + private DNS zone (prod default)
#   false — public endpoint guarded by allowed_cidrs firewall rules (dev fallback
#           when the chosen region disallows VNet-integrated Free Trial provisioning)
resource "azurerm_postgresql_flexible_server" "this" {
  name                = "${var.name_prefix}-mew${var.name_suffix}"
  resource_group_name = var.resource_group_name
  location            = var.region

  version    = var.postgres_version
  sku_name   = var.sku_name
  storage_mb = var.storage_gb * 1024

  administrator_login    = var.admin_username
  administrator_password = var.admin_password

  delegated_subnet_id           = var.vnet_integration_enabled ? var.subnet_id : null
  private_dns_zone_id           = var.vnet_integration_enabled ? var.private_dns_zone_id : null
  public_network_access_enabled = !var.vnet_integration_enabled

  backup_retention_days        = var.backup_retention_days
  geo_redundant_backup_enabled = false

  # Zone-redundant HA standby. Ignored on Burstable; uncomment behaviour
  # via dynamic block only when the operator enables it.
  dynamic "high_availability" {
    for_each = var.high_availability ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  tags = var.tags

  # No prevent_destroy: initial Flexible Server creation can fail (region
  # capacity, quota issues), and prevent_destroy blocks taint+replace.
  # Data-bearing protection lives in automated daily backups + PITR (7-35
  # days configurable above).
  lifecycle {
    # Auto-applied minor version upgrades during maintenance windows would
    # otherwise force-replace.
    ignore_changes = [
      zone, # AZ rebalancing post-maintenance
    ]
  }
}

# Logical database on the server. The Flexible Server creates a default
# 'postgres' database; we add 'kanto' for the application schema.
resource "azurerm_postgresql_flexible_server_database" "kanto" {
  name      = var.database_name
  server_id = azurerm_postgresql_flexible_server.this.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}

# Firewall rules — only created when the server is in public-access mode.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allowed" {
  for_each = var.vnet_integration_enabled ? toset([]) : toset(var.allowed_cidrs)

  name             = "allowed-${replace(replace(each.key, "/", "-"), ".", "-")}"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = cidrhost(each.key, 0)
  end_ip_address   = cidrhost(each.key, pow(2, 32 - tonumber(split("/", each.key)[1])) - 1)
}

# Special "Allow all Azure services" rule (start=end=0.0.0.0). Lets resources
# in any Azure subscription reach the server; auth and TLS still apply. Used
# in dev when AKS egress IPs are unknown at plan time.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure_services" {
  count = !var.vnet_integration_enabled && var.allow_azure_services ? 1 : 0

  name             = "AllowAllAzureServices"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
