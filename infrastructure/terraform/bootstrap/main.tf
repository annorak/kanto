###############################################################################
# Kanto bootstrap — Azure side.
#
# Run ONCE per Azure subscription. Local state (no remote backend yet). Creates:
#   - One resource group per env (dev, prod) where downstream Terraform lands
#     all per-env resources.
#   - One "shared" resource group holding the tfstate storage account.
#   - A globally-unique storage account + container that the root module's
#     `azurerm` backend writes to.
#
# After apply: copy the outputs into config/<env>-backend.hcl and
# config/<env>.tfvars in the root module, then `terraform init`.
###############################################################################

provider "azurerm" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
  features {}
}

locals {
  base_tags = {
    "Project"   = "Kanto"
    "ManagedBy" = "Terraform"
    "Layer"     = "Bootstrap"
  }
}

# Storage account names: 3-24 lowercase-alphanumeric, globally unique across
# all of Azure. The random suffix makes the name globally available without
# the operator having to invent one. Keep `<prefix>tfstate` short so the
# suffix can fit.
resource "random_id" "tfstate_suffix" {
  byte_length = 4
}

# Shared resource group: tfstate lives here. Separate from dev/prod so a bug
# in either env's Terraform cannot accidentally destroy state.
resource "azurerm_resource_group" "shared" {
  name     = "${var.name_prefix}-shared-rg"
  location = var.region
  tags     = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_resource_group" "dev" {
  name     = "${var.name_prefix}-dev-rg"
  location = var.region
  tags     = merge(local.base_tags, { "Environment" = "dev" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_resource_group" "prod" {
  name     = "${var.name_prefix}-prod-rg"
  location = var.region
  tags     = merge(local.base_tags, { "Environment" = "prod" })

  lifecycle {
    prevent_destroy = true
  }
}

# Storage account holding remote Terraform state for the root module.
# Versioning + soft-delete are enabled so a bad write can be rolled back.
resource "azurerm_storage_account" "tfstate" {
  name                = "${var.name_prefix}tfstate${random_id.tfstate_suffix.hex}"
  resource_group_name = azurerm_resource_group.shared.name
  location            = azurerm_resource_group.shared.location

  account_tier             = "Standard"
  account_replication_type = "LRS" # locally-redundant; state is recreatable
  account_kind             = "StorageV2"
  min_tls_version          = "TLS1_2"

  # Public network access is required for `terraform init` from operator
  # laptops + CI; restrict via the network_rules block once we have a
  # known set of allow-listed CIDRs.
  public_network_access_enabled   = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # required by azurerm backend
  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  tags = merge(local.base_tags, { "Purpose" = "tfstate" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}
