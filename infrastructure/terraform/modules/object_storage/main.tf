# Storage account names: 3-24 lowercase-alphanumeric, globally unique.
# Random suffix avoids the operator inventing one.
resource "random_id" "suffix" {
  byte_length = 4
}

resource "azurerm_storage_account" "this" {
  name                = "kanto${var.environment}data${random_id.suffix.hex}"
  resource_group_name = var.resource_group_name
  location            = var.region

  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  access_tier              = "Hot"
  min_tls_version          = "TLS1_2"

  public_network_access_enabled   = true # Modal reaches blobs from outside the VNet
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  tags = var.tags
}

resource "azurerm_storage_container" "this" {
  for_each = local.containers

  name                  = "kanto-${each.key}-${var.environment}"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

# Lifecycle: hot -> cool at hot_to_cool_days, cool -> archive at hot_to_archive_days
# for proteins + embeddings. The metadata container stays Hot indefinitely.
resource "azurerm_storage_management_policy" "this" {
  storage_account_id = azurerm_storage_account.this.id

  dynamic "rule" {
    for_each = { for k, v in local.containers : k => v if v.archive }

    content {
      name    = "archive-${rule.key}"
      enabled = true

      filters {
        prefix_match = ["kanto-${rule.key}-${var.environment}/"]
        blob_types   = ["blockBlob"]
      }

      actions {
        base_blob {
          tier_to_cool_after_days_since_modification_greater_than    = var.hot_to_cool_days
          tier_to_archive_after_days_since_modification_greater_than = var.hot_to_archive_days
        }
      }
    }
  }
}
