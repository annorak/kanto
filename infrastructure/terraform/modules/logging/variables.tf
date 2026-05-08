variable "compartment_id" {
  description = "Compartment OCID for the log group and logs."
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

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
