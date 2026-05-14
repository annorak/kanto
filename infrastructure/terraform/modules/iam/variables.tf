variable "resource_group_name" {
  description = "Resource group for the managed identities."
  type        = string
}

variable "region" {
  description = "Azure region for the managed identities."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "proteins_container_id" {
  description = "Resource Manager ID of the proteins blob container."
  type        = string
}

variable "embeddings_container_id" {
  description = "Resource Manager ID of the embeddings blob container."
  type        = string
}

variable "metadata_container_id" {
  description = "Resource Manager ID of the metadata blob container."
  type        = string
}

variable "key_vault_id" {
  description = "Key Vault holding etcd-encryption key, Mew password, and placeholder secrets."
  type        = string
}

variable "event_hubs_namespace_id" {
  description = "Event Hubs namespace ID. AKS pods get Data Sender + Receiver across the whole namespace."
  type        = string
}

variable "event_hubs_embedded_id" {
  description = "Event Hub ID for `kanto.embedded`. Modal gets Data Sender scoped to this hub only."
  type        = string
}

variable "modal_oidc_issuer" {
  description = "Modal's OIDC issuer URL. Empty string disables federation (dev). When non-empty, the modal federated credential is created."
  type        = string
  default     = ""
}

variable "modal_oidc_subject" {
  description = "Modal's OIDC subject claim. Required when modal_oidc_issuer is non-empty."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
