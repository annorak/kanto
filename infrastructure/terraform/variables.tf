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

variable "aks_region" {
  description = "Region override for AKS compute. Defaults to var.region. Use a different region when the project region disallows Free Trial AKS VM SKUs (eastus blocks Standard_B-series on Free Trial; eastus2 works). When set and different from var.region, the network module is instantiated a second time to provide a VNet in that region for AKS nodes/pods + Mew."
  type        = string
  default     = null
  nullable    = true
}

variable "aks_vnet_cidr" {
  description = "VNet CIDR for the AKS region when aks_region differs from var.region. Must not overlap var.vnet_cidr."
  type        = string
  default     = null
  nullable    = true
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

variable "mew_region" {
  description = "Region override for Mew. Defaults to var.region. Use a different region when the project region disallows Free Trial Postgres provisioning (eastus is restricted; eastus2 / westus2 typically work)."
  type        = string
  default     = null
  nullable    = true
}

variable "mew_vnet_integration_enabled" {
  description = "Use a delegated subnet + private DNS zone for Mew. Set false in dev when mew_region differs from var.region (VNet integration requires same-region)."
  type        = bool
  default     = true
}

variable "mew_allowed_cidrs" {
  description = "CIDRs allowed through the Postgres firewall when vnet integration is off. Operator IPs at minimum."
  type        = list(string)
  default     = []
}

variable "mew_allow_azure_services" {
  description = "Enable the AllowAllAzureServices firewall rule when vnet integration is off. Lets AKS pods reach Mew without wiring AKS egress IPs."
  type        = bool
  default     = false
}

variable "mew_name_suffix" {
  description = "Optional suffix on the Mew server name. Use to bypass the Azure DNS reservation that persists for ~24-72h after a failed create attempt (`kanto-dev-mew` taken in eastus -> rename to `kanto-dev-mew-e2`)."
  type        = string
  default     = ""
}

variable "deploy_mew" {
  description = "Skip Mew provisioning when false. Use when the subscription is restricted from creating Postgres Flexible Server in any reachable region (Free Trial offer restriction). Mew-dependent outputs become null; KV mew_password secret is kept so Mew can be provisioned later without rotating credentials."
  type        = bool
  default     = true
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
