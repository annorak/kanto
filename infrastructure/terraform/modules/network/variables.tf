variable "compartment_id" {
  description = "Compartment OCID where networking resources are created."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "vcn_cidr" {
  description = "VCN CIDR block. Must not overlap any other Kanto VCN."
  type        = string
}

variable "operator_cidrs" {
  description = "CIDRs that can reach the OKE Kubernetes API endpoint (operator laptops, CI). Empty list closes it down."
  type        = list(string)
  default     = []
}

variable "mew_public_ingress_cidrs" {
  description = "CIDRs allowed to reach the public Mew Postgres endpoint (Modal egress ranges in prod). Empty list keeps Mew private-only."
  type        = list(string)
  default     = []
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
