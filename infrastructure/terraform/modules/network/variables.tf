variable "resource_group_name" {
  description = "Azure resource group for every network resource."
  type        = string
}

variable "region" {
  description = "Azure region for VNet and subnets."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "vnet_cidr" {
  description = "Virtual Network CIDR. Must not overlap any other Kanto VNet."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
