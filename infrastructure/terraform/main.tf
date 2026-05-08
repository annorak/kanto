###############################################################################
# Kanto OCI infrastructure — root module.
#
# This is THE entrypoint. It wires every per-env module together. Per-env
# values come from config/<env>.tfvars; backend (state) location comes from
# config/<env>-backend.hcl. See README.md for the apply workflow.
###############################################################################

provider "oci" {
  region = var.region
}

locals {
  name_prefix = "kanto-${var.environment}"

  freeform_tags = {
    "Project"     = "Kanto"
    "Environment" = var.environment
    "ManagedBy"   = "Terraform"
  }
}

module "network" {
  source = "./modules/network"

  compartment_id           = var.compartment_id
  name_prefix              = local.name_prefix
  vcn_cidr                 = var.vcn_cidr
  operator_cidrs           = var.operator_cidrs
  mew_public_ingress_cidrs = var.mew_enable_public_endpoint ? var.modal_egress_cidrs : []
  freeform_tags            = local.freeform_tags
}

module "vault" {
  source = "./modules/vault"

  compartment_id = var.compartment_id
  name_prefix    = local.name_prefix
  freeform_tags  = local.freeform_tags
}

module "object_storage" {
  source = "./modules/object_storage"

  compartment_id      = var.compartment_id
  environment         = var.environment
  kms_key_id          = module.vault.master_key_id
  hot_to_archive_days = var.hot_to_archive_days
  freeform_tags       = local.freeform_tags
}

module "streaming" {
  source = "./modules/streaming"

  compartment_id = var.compartment_id
  environment    = var.environment
  kms_key_id     = module.vault.master_key_id
  freeform_tags  = local.freeform_tags
}

module "iam" {
  source = "./modules/iam"

  tenancy_ocid      = var.tenancy_ocid
  compartment_id    = var.compartment_id
  environment       = var.environment
  bucket_proteins   = module.object_storage.bucket_proteins
  bucket_embeddings = module.object_storage.bucket_embeddings
  bucket_metadata   = module.object_storage.bucket_metadata
  freeform_tags     = local.freeform_tags
}

module "mew" {
  source = "./modules/mew"

  compartment_id           = var.compartment_id
  name_prefix              = local.name_prefix
  subnet_id                = module.network.subnet_mew_id
  public_subnet_id         = module.network.subnet_public_id
  nsg_id                   = module.network.nsg_mew_id
  admin_password_secret_id = module.vault.mew_password_secret_id
  ocpu_count               = var.mew_ocpu_count
  memory_gb                = var.mew_memory_gb
  instance_count           = var.mew_instance_count
  backup_retention_days    = var.mew_backup_retention_days
  enable_public_endpoint   = var.mew_enable_public_endpoint
  freeform_tags            = local.freeform_tags
}

module "oke" {
  source = "./modules/oke"

  compartment_id     = var.compartment_id
  name_prefix        = local.name_prefix
  vcn_id             = module.network.vcn_id
  endpoint_subnet_id = module.network.subnet_public_id
  nodes_subnet_id    = module.network.subnet_nodes_id
  pods_subnet_id     = module.network.subnet_pods_id
  lb_subnet_id       = module.network.subnet_public_id
  nsg_oke_api_id     = module.network.nsg_oke_api_id
  nsg_workers_id     = module.network.nsg_oke_workers_id
  kms_key_id         = module.vault.master_key_id
  kubernetes_version = var.oke_kubernetes_version
  node_ocpus         = var.oke_node_ocpus
  node_memory_gb     = var.oke_node_memory_gb
  node_count         = var.oke_node_count
  node_count_min     = var.oke_node_count_min
  node_count_max     = var.oke_node_count_max
  ssh_public_key     = var.ssh_public_key
  freeform_tags      = local.freeform_tags
}

module "logging" {
  source = "./modules/logging"

  compartment_id         = var.compartment_id
  name_prefix            = local.name_prefix
  app_log_retention_days = var.app_log_retention_days
  freeform_tags          = local.freeform_tags
}
