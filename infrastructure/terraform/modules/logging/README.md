# logging module

One Log Analytics workspace per environment (`<prefix>-logs`).

Log producers:

- **AKS container insights** — wired by the aks module's `oms_agent` block
  + a `diagnostic_setting` resource forwarding kube-apiserver, kube-audit-
  admin, controller-manager, scheduler, and cluster-autoscaler logs.
- **Modal log forwarder** (configured Modal-side in Task 7) — ships Ditto
  stdout/stderr to the workspace via the Azure Log Ingestion API.
- **Per-resource diagnostic settings** — Postgres / Event Hubs / Key Vault
  get their own `azurerm_monitor_diagnostic_setting` resources in later
  hardening passes.

Subscription-level **Azure Activity Log** captures every control-plane
operation automatically and is administered subscription-wide — not here.

## Retention

The task spec asks for 30 days in dev and 90 days in prod for application
logs. Activity Log follows subscription-wide retention (default 90 days),
which is administered separately.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`,
`name_prefix`, `app_log_retention_days`.

## Outputs

`workspace_id`, `workspace_name`, `workspace_customer_id`.
