# key_vault module

Azure Key Vault (RBAC mode) holding:

- `<prefix>-aks-etcd` — RSA-2048 key used by AKS for etcd encryption at rest.
- `<prefix>-mew-password` — randomly generated Postgres admin password,
  exported back to the root so the mew module can bootstrap the server.
- 3 placeholder secrets — `modal-token`, `slack-webhook-url`,
  `pagerduty-integration-key`. Created with literal `REPLACE_ME` value;
  operator rotates via `az keyvault secret set` post-apply.

Purge protection is **on** (90-day soft-delete retention). AKS rejects
etcd-encryption keys whose vault doesn't have purge protection enabled.

RBAC vs. access policies: we use RBAC mode (`enable_rbac_authorization`).
The operator running Terraform is granted Key Vault Administrator on this
vault; subsequent apply'ers can read and write everything. The IAM module
grants Key Vault Crypto User (for AKS etcd) and Key Vault Secrets User
(for AKS workloads) on this vault.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`,
`name_prefix`, `tenant_id`, `operator_object_id`.

## Outputs

`id`, `name`, `vault_uri`, `aks_etcd_key_id`, `mew_password_secret_id`,
`mew_password_value` (sensitive).
