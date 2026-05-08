output "cluster_id" {
  value = oci_containerengine_cluster.this.id
}

output "cluster_name" {
  value = oci_containerengine_cluster.this.name
}

output "cluster_kubernetes_version" {
  value = oci_containerengine_cluster.this.kubernetes_version
}

output "cluster_endpoint" {
  description = "Public Kubernetes API endpoint."
  value       = oci_containerengine_cluster.this.endpoints[0].public_endpoint
}

output "node_pool_id" {
  value = oci_containerengine_node_pool.cpu.id
}
