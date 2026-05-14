variable "resource_group_name" {
  description = "Azure resource group for the vault."
  type        = string
}

variable "region" {
  description = "Azure region for the vault."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'. Combined with a random suffix to satisfy Key Vault's global-uniqueness rule."
  type        = string
}

variable "tenant_id" {
  description = "Azure AD tenant ID. Required on the vault resource."
  type        = string
}

variable "operator_object_id" {
  description = "Object ID of the principal applying this Terraform (operator or CI service principal). Receives Key Vault Administrator so it can create secrets/keys."
  type        = string
}

variable "operator_cidrs" {
  description = "CIDRs allowed to reach the vault's data plane from outside the Microsoft backbone (operator laptops, CI). AKS workloads bypass via 'AzureServices' so they don't need a CIDR rule."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
