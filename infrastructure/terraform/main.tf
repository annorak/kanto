###############################################################################
# Kanto Azure infrastructure — root module.
#
# Per-env values come from config/<env>.tfvars. Backend (state) location
# comes from config/<env>-backend.hcl. See README.md for the apply workflow.
###############################################################################

provider "azurerm" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
  features {
    key_vault {
      # Hard-delete on terraform destroy; the prevent_destroy lifecycle is
      # what guards the vault itself, soft-delete just adds friction.
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
  }
}

provider "azuread" {
  tenant_id = var.tenant_id
}

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

locals {
  name_prefix = "kanto-${var.environment}"

  tags = {
    "Project"     = "Kanto"
    "Environment" = var.environment
    "ManagedBy"   = "Terraform"
  }
}

module "network" {
  source = "./modules/network"

  resource_group_name = data.azurerm_resource_group.this.name
  region              = var.region
  name_prefix         = local.name_prefix
  vnet_cidr           = var.vnet_cidr
  tags                = local.tags
}

module "key_vault" {
  source = "./modules/key_vault"

  resource_group_name = data.azurerm_resource_group.this.name
  region              = var.region
  name_prefix         = local.name_prefix
  tenant_id           = var.tenant_id
  operator_object_id  = data.azurerm_client_config.current.object_id
  operator_cidrs      = var.operator_cidrs
  tags                = local.tags
}

module "object_storage" {
  source = "./modules/object_storage"

  resource_group_name = data.azurerm_resource_group.this.name
  region              = var.region
  environment         = var.environment
  hot_to_cool_days    = var.hot_to_cool_days
  hot_to_archive_days = var.hot_to_archive_days
  tags                = local.tags
}

module "event_hubs" {
  source = "./modules/event_hubs"

  resource_group_name = data.azurerm_resource_group.this.name
  region              = var.region
  name_prefix         = local.name_prefix
  tags                = local.tags
}

module "logging" {
  source = "./modules/logging"

  resource_group_name    = data.azurerm_resource_group.this.name
  region                 = var.region
  name_prefix            = local.name_prefix
  app_log_retention_days = var.app_log_retention_days
  tags                   = local.tags
}

module "iam" {
  source = "./modules/iam"

  resource_group_name     = data.azurerm_resource_group.this.name
  region                  = var.region
  name_prefix             = local.name_prefix
  proteins_container_id   = module.object_storage.proteins_container_id
  embeddings_container_id = module.object_storage.embeddings_container_id
  metadata_container_id   = module.object_storage.metadata_container_id
  key_vault_id            = module.key_vault.id
  event_hubs_namespace_id = module.event_hubs.namespace_id
  event_hubs_embedded_id  = module.event_hubs.event_hub_ids["kanto.embedded"]
  modal_oidc_issuer       = var.modal_oidc_issuer
  modal_oidc_subject      = var.modal_oidc_subject
  tags                    = local.tags
}

module "mew" {
  source = "./modules/mew"

  resource_group_name   = data.azurerm_resource_group.this.name
  region                = var.region
  name_prefix           = local.name_prefix
  subnet_id             = module.network.subnet_mew_id
  private_dns_zone_id   = module.network.private_dns_zone_postgres_id
  admin_password        = module.key_vault.mew_password_value
  sku_name              = var.mew_sku_name
  storage_gb            = var.mew_storage_gb
  high_availability     = var.mew_high_availability_enabled
  backup_retention_days = var.mew_backup_retention_days
  tags                  = local.tags
}

module "aks" {
  source = "./modules/aks"

  resource_group_name        = data.azurerm_resource_group.this.name
  region                     = var.region
  name_prefix                = local.name_prefix
  nodes_subnet_id            = module.network.subnet_nodes_id
  pods_subnet_id             = module.network.subnet_pods_id
  authorized_ip_ranges       = var.operator_cidrs
  key_vault_id               = module.key_vault.id
  log_analytics_workspace_id = module.logging.workspace_id
  kubernetes_version         = var.aks_kubernetes_version
  node_vm_size               = var.aks_node_vm_size
  node_count                 = var.aks_node_count
  ssh_public_key             = var.ssh_public_key
  tags                       = local.tags

  # Ensure the AKS-workloads UAMI exists before the cluster, so the OIDC-
  # issued K8s service accounts can immediately federate against it.
  depends_on = [module.iam]
}
