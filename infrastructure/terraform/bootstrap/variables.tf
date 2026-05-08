variable "tenancy_ocid" {
  description = "Tenancy OCID. Top-level kanto compartment is created directly under this."
  type        = string
}

variable "region" {
  description = "OCI home region."
  type        = string
  default     = "us-sanjose-1"
}

variable "name_prefix" {
  description = "Prefix used for every resource name. Keep short."
  type        = string
  default     = "kanto"
}

variable "tfstate_bucket_name" {
  description = "Object Storage bucket that will hold remote state for all environments."
  type        = string
  default     = "kanto-tfstate-shared"
}
