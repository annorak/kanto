# mew module

Azure Database for PostgreSQL Flexible Server ("Mew") with pgvector
enabled. VNet-integrated via subnet delegation + private DNS zone — no
public endpoint, no IP allowlist. Modal reaches Mew via workload-identity
federation and AAD-issued Postgres connection tokens (no long-lived
credentials).

The module creates the Flexible Server plus the `kanto` logical database.
**No `prevent_destroy`** on the server — initial create can fail (region
capacity, quota), and `prevent_destroy` would block taint+replace. Data
protection lives in the automated daily backups + PITR (configurable
retention 7–35 days).

pgvector is preinstalled in Postgres Flexible Server (PG 14+); no custom
`azurerm_postgresql_flexible_server_configuration` resource is needed. The
Task 3 migrations run `CREATE EXTENSION vector;` against the kanto
database to enable it.

## Sizing

| Env  | sku_name                   | vCPU | RAM    | storage | HA              |
| ---- | -------------------------- | ---- | ------ | ------- | --------------- |
| dev  | `B_Standard_B1ms` (free)   | 1    | 2 GB   | 32 GB   | no              |
| prod | `GP_Standard_D16s_v3`      | 16   | 64 GB  | 256 GB  | zone-redundant  |

The dev SKU is the 12-month Azure free-tier line; after the free period
expires it bills at ~$15/mo. For prod, design-doc Section 17 calls for
16 vCPU / 128 GB. The 64 GB ceiling on `D16s_v3` is below the design
target; switch to `MO_Standard_E16ds_v5` (16 vCPU / 128 GB memory-
optimised) before going to production.

## Inputs

See `variables.tf`. Required: `resource_group_name`, `region`,
`name_prefix`, `subnet_id`, `private_dns_zone_id`, `admin_password`,
`admin_password_secret_id`, `sku_name`.

## Outputs

`server_id`, `fqdn`, `port`, `database_name`, `admin_username`.
