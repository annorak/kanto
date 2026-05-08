###############################################################################
# Variables — the full configuration surface of the Kanto stack.
#
# Values come from config/<env>.tfvars at apply time. Defaults below cover
# the common case; per-env overrides go in the tfvars file.
###############################################################################

# -----------------------------------------------------------------------------
# Identity / region
# -----------------------------------------------------------------------------

variable "tenancy_ocid" {
  description = "Tenancy OCID. Required for dynamic groups and IAM users."
  type        = string
}

variable "compartment_id" {
  description = "Env compartment OCID, output by bootstrap (compartment_dev_id or compartment_prod_id)."
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

variable "region" {
  description = "OCI region."
  type        = string
  default     = "us-sanjose-1"
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

variable "vcn_cidr" {
  description = "VCN CIDR. Dev and prod must not overlap."
  type        = string
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach the OKE Kubernetes API endpoint."
  type        = list(string)
  default     = []
}

variable "modal_egress_cidrs" {
  description = "Modal egress ranges allow-listed for the public Mew NLB. Empty unless mew_enable_public_endpoint is true."
  type        = list(string)
  default     = []
}

variable "ssh_public_key" {
  description = "Public SSH key authorized on OKE worker nodes for emergency access."
  type        = string
}

# -----------------------------------------------------------------------------
# Mew (Postgres + pgvector)
# -----------------------------------------------------------------------------

variable "mew_ocpu_count" {
  description = "OCPUs per Mew instance."
  type        = number
}

variable "mew_memory_gb" {
  description = "Memory per Mew instance, GB."
  type        = number
}

variable "mew_instance_count" {
  description = "Mew instance count. 1 = single instance (dev). 2+ = HA (prod)."
  type        = number
  default     = 1
}

variable "mew_backup_retention_days" {
  description = "Mew automatic backup retention, days."
  type        = number
  default     = 7
}

variable "mew_enable_public_endpoint" {
  description = "Provision the public NLB so Modal can reach Mew. Set true in prod, false in dev."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# OKE
# -----------------------------------------------------------------------------

variable "oke_kubernetes_version" {
  description = "OKE Kubernetes version. Verify with `oci ce cluster-options get --cluster-option-id all`."
  type        = string
  default     = "v1.35.2"
}

variable "oke_node_ocpus" {
  description = "OCPUs per OKE worker node."
  type        = number
}

variable "oke_node_memory_gb" {
  description = "Memory per OKE worker node, GB."
  type        = number
}

variable "oke_node_count" {
  description = "Initial OKE worker node count."
  type        = number
}

variable "oke_node_count_min" {
  description = "Cluster autoscaler min size hint."
  type        = number
}

variable "oke_node_count_max" {
  description = "Cluster autoscaler max size hint."
  type        = number
}

# -----------------------------------------------------------------------------
# Logging / Object Storage lifecycle
# -----------------------------------------------------------------------------

variable "app_log_retention_days" {
  description = "Application log retention. 30 dev / 90 prod is the convention."
  type        = number
  default     = 30
}

variable "hot_to_archive_days" {
  description = "Days before proteins/embeddings buckets transition to Archive. Per design doc Section 17."
  type        = number
  default     = 90
}
