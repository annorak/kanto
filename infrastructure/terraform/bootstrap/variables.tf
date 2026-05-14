variable "subscription_id" {
  description = "Azure subscription OCID. The Kanto resource groups, the tfstate storage account, and every downstream resource live in this subscription."
  type        = string
}

variable "tenant_id" {
  description = "Azure AD tenant ID."
  type        = string
}

variable "region" {
  description = "Azure region for every Kanto resource. eastus is the default — lowest latency to Modal."
  type        = string
  default     = "eastus"
}

variable "name_prefix" {
  description = "Prefix used in every resource name. Keep short — Azure storage accounts cap at 24 chars total."
  type        = string
  default     = "kanto"
}
