variable "compartment_id" {
  description = "Compartment OCID for the cluster and node pool."
  type        = string
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. 'kanto-dev'."
  type        = string
}

variable "vcn_id" {
  description = "VCN OCID."
  type        = string
}

variable "endpoint_subnet_id" {
  description = "Public subnet for the OKE Kubernetes API endpoint."
  type        = string
}

variable "nodes_subnet_id" {
  description = "Private subnet for worker node primary VNICs."
  type        = string
}

variable "pods_subnet_id" {
  description = "Private subnet for pod IPs (VCN-Native CNI)."
  type        = string
}

variable "lb_subnet_id" {
  description = "Public subnet OCI LoadBalancer Services should land in."
  type        = string
}

variable "nsg_oke_api_id" {
  description = "NSG attached to the API endpoint."
  type        = string
}

variable "nsg_workers_id" {
  description = "NSG attached to worker nodes (and pods)."
  type        = string
}

variable "kms_key_id" {
  description = "KMS key for Kubernetes secrets / etcd encryption at rest."
  type        = string
}

variable "kubernetes_version" {
  description = "Cluster Kubernetes version (e.g. 'v1.35.2'). Verify availability with `oci ce cluster-options get --cluster-option-id all`."
  type        = string
}

variable "node_shape" {
  description = "Worker node shape."
  type        = string
  default     = "VM.Standard.E5.Flex"
}

variable "node_ocpus" {
  description = "OCPUs per worker node."
  type        = number
}

variable "node_memory_gb" {
  description = "Memory per worker node, in GB."
  type        = number
}

variable "node_count" {
  description = "Initial worker node count. Cluster autoscaler (Helm-installed in a later task) handles dynamic scaling between min/max via the freeform tags below."
  type        = number
}

variable "node_count_min" {
  description = "Min size hint for the cluster autoscaler."
  type        = number
}

variable "node_count_max" {
  description = "Max size hint for the cluster autoscaler."
  type        = number
}

variable "max_pods_per_node" {
  description = "VCN-Native CNI: max pods per node. Bounded by available pod subnet IPs."
  type        = number
  default     = 31
}

variable "boot_volume_gb" {
  description = "Worker boot volume size."
  type        = number
  default     = 50
}

variable "ssh_public_key" {
  description = "Public SSH key authorized on worker nodes for emergency access."
  type        = string
}

variable "freeform_tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
