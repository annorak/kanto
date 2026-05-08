# logging module

One log group per env (`kanto-<env>-app`) plus one custom log inside it.

Two log producers write here:

- The **OKE Logging Operator** (deployed via Helm in Task 5) ships pod
  stdout/stderr from every namespace.
- The **Modal log forwarder** (configured Modal-side in Task 7) ships
  Ditto logs.

Tenancy-level **OCI Audit** captures every IAM, Vault, and Object Storage
control-plane operation automatically; no resources are needed here for
that.

## Retention

The task spec asks for 30 days in dev and 90 days in prod for application
logs, longer for audit. Audit logs follow the tenancy-wide retention
(default 365 days), which is administered tenancy-wide and is not
controlled here.

## Inputs

| Name                     | Type     | Required | Description                              |
| ------------------------ | -------- | -------- | ---------------------------------------- |
| `compartment_id`         | `string` | yes      | Compartment for the log group and log.   |
| `name_prefix`            | `string` | yes      | Resource name prefix.                    |
| `app_log_retention_days` | `number` | yes      | Retention in days; 30 dev / 90 prod.     |
| `freeform_tags`          | `map`    | no       | Tags applied to every resource.          |

## Outputs

`log_group_id`, `app_log_id`.
