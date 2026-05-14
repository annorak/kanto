# Azure Database for PostgreSQL Flexible Server, VNet-integrated, with
# pgvector preinstalled. Task 3 migrations run `CREATE EXTENSION vector;`
# against the application database to enable it.
#
# Public endpoint is intentionally disabled (delegated_subnet_id +
# private_dns_zone_id together force private-only access). Modal reaches
# Mew via workload-identity federation + AAD-issued Postgres connection
# tokens; no IP allowlist required.
resource "azurerm_postgresql_flexible_server" "this" {
  name                = "${var.name_prefix}-mew"
  resource_group_name = var.resource_group_name
  location            = var.region

  version    = var.postgres_version
  sku_name   = var.sku_name
  storage_mb = var.storage_gb * 1024

  administrator_login    = var.admin_username
  administrator_password = var.admin_password

  delegated_subnet_id = var.subnet_id
  private_dns_zone_id = var.private_dns_zone_id

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
