output "vault_id" {
  value = oci_kms_vault.this.id
}

output "master_key_id" {
  description = "Master KMS key OCID. Consumers wait until the service-principal grant has propagated before they get this value."
  value       = oci_kms_key.master.id
  # depends_on forces downstream modules (oke, streaming) to wait for the
  # IAM policy + propagation sleep before they reference the key.
  depends_on = [time_sleep.kms_policy_propagation]
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
