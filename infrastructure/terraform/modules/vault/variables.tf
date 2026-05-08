variable "compartment_id" {
  description = "Compartment OCID for vault, key, and secrets."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

# Names of slots Terraform creates with placeholder content. The operator
# rotates the real value via `oci vault secret update-base64` post-apply.
# We use a fixed list rather than a variable because the slots are part of
# the platform contract — services in later tasks read these names from Vault.
locals {
  placeholder_secrets = [
    "modal-token",
    "slack-webhook-url",
    "pagerduty-integration-key",
  ]
}
