variable "compartment_id" {
  description = "Compartment OCID for the buckets."
  type        = string
}

variable "environment" {
  description = "Environment name appended to bucket names (e.g. 'dev', 'prod')."
  type        = string
}

variable "kms_key_id" {
  description = "KMS key OCID used for bucket encryption."
  type        = string
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "hot_to_archive_days" {
  description = "Days before objects in proteins/embeddings buckets transition to Archive storage. Per design doc Section 17."
  type        = number
  default     = 90
}

# Bucket roles. The metadata bucket has different lifecycle expectations
# (small NCBI cache, we want to keep it warm), so we drive lifecycle from
# this map rather than coding three nearly-identical resources.
locals {
  buckets = {
    proteins   = { archive_days = var.hot_to_archive_days }
    embeddings = { archive_days = var.hot_to_archive_days }
    metadata   = { archive_days = 0 } # always-hot
  }
}
