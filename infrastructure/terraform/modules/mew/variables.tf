variable "compartment_id" {
  description = "Compartment OCID for the DB system, custom config, and (optional) public NLB."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "subnet_id" {
  description = "Private subnet for the DB endpoint."
  type        = string
}

variable "public_subnet_id" {
  description = "Public subnet for the optional NLB. Required when enable_public_endpoint is true."
  type        = string
  default     = null
}

variable "nsg_id" {
  description = "NSG attached to the DB endpoint (and the NLB if enabled)."
  type        = string
}

variable "kms_key_id" {
  description = "KMS key OCID for storage encryption. Bring-your-own-key."
  type        = string
}

variable "admin_username" {
  description = "Postgres admin user name."
  type        = string
  default     = "kanto"
}

variable "admin_password_secret_id" {
  description = "OCID of the Vault secret holding the admin password."
  type        = string
}

variable "db_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "16"
}

variable "shape" {
  description = "OCI Database for PostgreSQL shape."
  type        = string
  default     = "VM.Standard.E4.Flex"
}

variable "ocpu_count" {
  description = "OCPU count per instance."
  type        = number
}

variable "memory_gb" {
  description = "Memory per instance, in GB."
  type        = number
}

variable "storage_gb" {
  description = "Storage size, in GB."
  type        = number
}

variable "system_type" {
  description = "OCI_OPTIMIZED_STORAGE (default) is recommended; the alternative OCI_REGIONAL_STORAGE costs more."
  type        = string
  default     = "OCI_OPTIMIZED_STORAGE"
}

variable "instance_count" {
  description = "1 = single-instance (dev). Prod uses 2 or 3 for HA."
  type        = number
  default     = 1
}

variable "backup_retention_days" {
  description = "Automatic backup retention in days. PITR is implied by automatic backups."
  type        = number
  default     = 7
}

variable "enable_public_endpoint" {
  description = "When true, provision an NLB in public_subnet_id forwarding TCP/5432 to the DB. Used for Modal access in prod."
  type        = bool
  default     = false
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
