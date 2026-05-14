###############################################################################
# Variables — full configuration surface of the Azure Kanto stack.
#
# Values come from config/<env>.tfvars at apply time. Defaults below cover
# the common case; per-env overrides go in the tfvars file.
###############################################################################

# -----------------------------------------------------------------------------
# Identity / region / scope
# -----------------------------------------------------------------------------

variable "subscription_id" {
  description = "Azure subscription ID hosting every Kanto resource."
  type        = string
}

variable "tenant_id" {
  description = "Azure AD tenant ID. Used for managed-identity OIDC issuers."
  type        = string
}

variable "region" {
  description = "Azure region for every resource. eastus is the project default."
  type        = string
  default     = "eastus"
}

variable "resource_group_name" {
  description = "Per-env resource group (bootstrap output `resource_group_<env>_name`)."
  type        = string
}

variable "environment" {
  description = "Environment name. Drives resource naming and the few env-conditional choices below."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be 'dev' or 'prod'."
  }
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

variable "vnet_cidr" {
  description = "Virtual Network CIDR. Dev and prod must not overlap."
  type        = string
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach the AKS Kubernetes API endpoint."
  type        = list(string)
  default     = []
}

variable "ssh_public_key" {
  description = "Public SSH key authorized on AKS worker nodes for emergency access."
  type        = string
}

# -----------------------------------------------------------------------------
# Mew (Postgres Flexible Server + pgvector)
# -----------------------------------------------------------------------------

variable "mew_sku_name" {
  description = "Flexible Server SKU. `B_Standard_B1ms` is the 12-month free tier; `GP_Standard_D2s_v3` is the smallest General Purpose. Prod sizing in design doc Section 17."
  type        = string
}

variable "mew_storage_gb" {
  description = "Mew storage in GB. Flexible Server steps at 32, 64, 128, 256, 512..."
  type        = number
  default     = 32
}

variable "mew_high_availability_enabled" {
  description = "Zone-redundant HA standby (Flexible Server requires GP tier or higher). Use false in dev, true in prod."
  type        = bool
  default     = false
}

variable "mew_backup_retention_days" {
  description = "Flexible Server backup retention. Min 7 / max 35."
  type        = number
  default     = 7
}

# -----------------------------------------------------------------------------
# AKS
# -----------------------------------------------------------------------------

variable "aks_kubernetes_version" {
  description = "AKS Kubernetes version. Verify with `az aks get-versions --location <region>`."
  type        = string
}

variable "aks_node_vm_size" {
  description = "VM size for worker nodes. Standard_B2s is the smallest reasonable choice for dev."
  type        = string
  default     = "Standard_B2s"
}

variable "aks_node_count" {
  description = "Initial node count in the default node pool."
  type        = number
}

# -----------------------------------------------------------------------------
# Logging / storage lifecycle
# -----------------------------------------------------------------------------

variable "app_log_retention_days" {
  description = "Application log retention in Log Analytics. 30 dev / 90 prod is the convention."
  type        = number
  default     = 30
}

variable "hot_to_cool_days" {
  description = "Days before proteins/embeddings blobs transition Hot -> Cool tier."
  type        = number
  default     = 30
}

variable "hot_to_archive_days" {
  description = "Days before proteins/embeddings blobs transition to Archive. Per design doc Section 17."
  type        = number
  default     = 90
}

# -----------------------------------------------------------------------------
# Modal workload-identity federation
# -----------------------------------------------------------------------------

variable "modal_oidc_issuer" {
  description = "Modal's OIDC issuer URL. The Modal-side function presents a token signed by this issuer; Azure trusts it via the federated credential created in the iam module. Leave empty in dev — federation is only wired in prod."
  type        = string
  default     = ""
}

variable "modal_oidc_subject" {
  description = "Subject claim Modal sends in the OIDC token (e.g. workspace/function identifier). Required when modal_oidc_issuer is non-empty."
  type        = string
  default     = ""
}
