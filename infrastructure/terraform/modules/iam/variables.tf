variable "tenancy_ocid" {
  description = "Tenancy OCID. Dynamic groups, IAM users, and IAM groups must live at the tenancy root."
  type        = string
}

variable "compartment_id" {
  description = "Env compartment OCID. Policies are scoped to this compartment so a dev policy cannot grant access to prod."
  type        = string
}

variable "environment" {
  description = "Environment name (e.g. 'dev', 'prod'). Used in identity resource names; must be unique per env."
  type        = string
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "bucket_proteins" {
  description = "Name of the proteins bucket. Modal gets read-only on this."
  type        = string
}

variable "bucket_embeddings" {
  description = "Name of the embeddings bucket. Modal gets write here; OKE workers read."
  type        = string
}

variable "bucket_metadata" {
  description = "Name of the metadata bucket. Growlithe (on OKE) writes; Modal does not access."
  type        = string
}
