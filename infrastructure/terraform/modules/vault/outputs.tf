output "vault_id" {
  value = oci_kms_vault.this.id
}

output "master_key_id" {
  value = oci_kms_key.master.id
}

output "mew_password" {
  description = "Initial Mew admin password (rotate after apply)."
  value       = random_password.mew.result
  sensitive   = true
}

output "mew_password_secret_id" {
  value = oci_vault_secret.mew_password.id
}

output "placeholder_secret_ids" {
  description = "Map of placeholder name -> OCID."
  value       = { for k, v in oci_vault_secret.placeholder : k => v.id }
}
