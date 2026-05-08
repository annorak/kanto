output "oke_workers_dynamic_group_id" {
  value = oci_identity_dynamic_group.oke_workers.id
}

output "oke_workers_dynamic_group_name" {
  value = oci_identity_dynamic_group.oke_workers.name
}

output "modal_user_id" {
  description = "OCID of the Modal IAM user. Operator uploads an API key to this user post-apply."
  value       = oci_identity_user.modal.id
}

output "modal_group_id" {
  value = oci_identity_group.modal.id
}
