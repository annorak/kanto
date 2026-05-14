variable "resource_group_name" {
  description = "Resource group for the Log Analytics workspace."
  type        = string
}

variable "region" {
  description = "Azure region for the workspace."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "app_log_retention_days" {
  description = "Retention for application/Modal-forwarded logs (30 dev, 90 prod)."
  type        = number
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
