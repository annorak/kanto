variable "resource_group_name" {
  description = "Azure resource group for the storage account."
  type        = string
}

variable "region" {
  description = "Azure region for the storage account."
  type        = string
}

variable "environment" {
  description = "Environment name (e.g. 'dev', 'prod'). Suffixes container names."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "hot_to_cool_days" {
  description = "Days before proteins/embeddings blobs transition Hot -> Cool."
  type        = number
  default     = 30
}

variable "hot_to_archive_days" {
  description = "Days before proteins/embeddings blobs transition to Archive. Per design doc Section 17."
  type        = number
  default     = 90
}

# Container roles. The metadata container has different lifecycle expectations
# (small NCBI cache, we want to keep it warm), so we drive lifecycle from this
# map rather than three near-identical resources.
locals {
  containers = {
    proteins   = { archive = true }
    embeddings = { archive = true }
    metadata   = { archive = false } # always-hot
  }
}
