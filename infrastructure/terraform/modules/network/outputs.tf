output "vcn_id" {
  value = oci_core_vcn.this.id
}

output "subnet_public_id" {
  value = oci_core_subnet.public.id
}

output "subnet_nodes_id" {
  value = oci_core_subnet.nodes.id
}

output "subnet_pods_id" {
  value = oci_core_subnet.pods.id
}

output "subnet_mew_id" {
  value = oci_core_subnet.mew.id
}

output "nsg_oke_api_id" {
  value = oci_core_network_security_group.oke_api.id
}

output "nsg_oke_workers_id" {
  value = oci_core_network_security_group.oke_workers.id
}

output "nsg_lb_id" {
  value = oci_core_network_security_group.lb.id
}

output "nsg_mew_id" {
  value = oci_core_network_security_group.mew.id
}
