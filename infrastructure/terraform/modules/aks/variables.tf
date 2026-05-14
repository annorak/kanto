variable "resource_group_name" {
  description = "Resource group for the AKS cluster."
  type        = string
}

variable "region" {
  description = "Azure region."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "nodes_subnet_id" {
  description = "Subnet hosting worker node VNICs."
  type        = string
}

variable "pods_subnet_id" {
  description = "Subnet hosting pod IPs (Azure CNI Overlay)."
  type        = string
}

variable "authorized_ip_ranges" {
  description = "CIDRs allowed to hit the public Kubernetes API endpoint. Same as operator_cidrs at the root."
  type        = list(string)
}

variable "key_vault_id" {
  description = "Key Vault granted to the AKS cluster's system-assigned identity for future etcd-CMK enablement."
  type        = string
}

variable "log_analytics_workspace_id" {
  description = "Log Analytics workspace AKS forwards container insights + diagnostic logs to."
  type        = string
}

variable "kubernetes_version" {
  description = "AKS Kubernetes version."
  type        = string
}

variable "node_vm_size" {
  description = "VM size for the default node pool."
  type        = string
  default     = "Standard_B2s"
}

variable "node_count" {
  description = "Initial worker node count."
  type        = number
  default     = 2
}

variable "ssh_public_key" {
  description = "Public SSH key authorized on worker nodes for emergency access."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
