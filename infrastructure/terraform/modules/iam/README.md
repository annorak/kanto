# iam module

Identity layer for one environment. Two user-assigned managed identities,
both consumed via Workload Identity Federation (no long-lived secrets):

1. **AKS workloads UAMI** (`<prefix>-aks-workloads`) — bound to K8s
   ServiceAccounts via the AKS cluster's OIDC issuer. Pods running Kanto
   services (Snorlax, Growlithe, Alakazam, Chatot, the API) project a
   service account token, exchange it at Azure AD for an AAD token scoped
   to this identity's role assignments.
2. **Modal UAMI** (`<prefix>-modal`) — federated to Modal's OIDC issuer
   when `modal_oidc_issuer` is non-empty (prod only). Modal-side functions
   present a Modal-signed token; Azure AD trusts it via the federated
   credential resource and issues a scoped AAD token.

## Permission summary

| Principal      | Blob containers                       | Event Hubs                             | Key Vault           |
| -------------- | ------------------------------------- | -------------------------------------- | ------------------- |
| AKS workloads  | read `proteins`+`embeddings`; write `proteins`+`metadata` | Sender + Receiver, namespace-wide      | Secrets User        |
| Modal          | read `proteins`; write `embeddings`   | Sender on `kanto.embedded` only        | (none)              |

The AKS workloads scope is broader than ideal because we're treating the
cluster as one trust boundary. Per-service scoping (Growlithe restricted to
metadata only, Chatot read-only, etc.) is the v2 hardening pass.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`,
`name_prefix`, `storage_account_id`, `{proteins,embeddings,metadata}_container_id`,
`key_vault_id`, `event_hubs_namespace_id`, `event_hubs_embedded_id`. Optional:
`modal_oidc_issuer`, `modal_oidc_subject` — empty disables Modal federation.

## Outputs

`aks_workload_identity_{id,client_id,principal_id}`,
`modal_identity_{id,client_id}`.

## After apply (prod)

If you set `modal_oidc_issuer` + `modal_oidc_subject` at apply time, the
federated credential is created automatically. To wire additional Modal
functions to the same identity manually:

```bash
az identity federated-credential create \
  --identity-name <prefix>-modal \
  --resource-group <prefix>-rg \
  --name <function-name> \
  --issuer <Modal OIDC issuer URL> \
  --subject <Modal OIDC subject claim> \
  --audiences "api://AzureADTokenExchange"
```
