output "compartment_kanto_id" {
  description = "OCID of the top-level kanto compartment."
  value       = oci_identity_compartment.kanto.id
}

output "compartment_shared_id" {
  description = "OCID of the shared compartment (Terraform state lives here)."
  value       = oci_identity_compartment.shared.id
}

output "compartment_network_id" {
  description = "OCID of the network compartment."
  value       = oci_identity_compartment.network.id
}

output "compartment_dev_id" {
  description = "OCID of the dev compartment."
  value       = oci_identity_compartment.dev.id
}

output "compartment_prod_id" {
  description = "OCID of the prod compartment."
  value       = oci_identity_compartment.prod.id
}

output "tfstate_bucket_name" {
  description = "Name of the Object Storage bucket holding Terraform state."
  value       = oci_objectstorage_bucket.tfstate.name
}

output "tfstate_namespace" {
  description = "Object Storage namespace for the tenancy."
  value       = data.oci_objectstorage_namespace.tenancy.namespace
}
