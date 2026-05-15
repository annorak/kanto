# mew module

Azure Database for PostgreSQL Flexible Server ("Mew") with pgvector
enabled. Two networking modes, switched by `vnet_integration_enabled`:

- **VNet-integrated** (default, prod): delegated subnet + private DNS zone,
  no public endpoint, no IP allowlist. Modal reaches Mew via workload-
  identity federation and AAD-issued Postgres connection tokens (no
  long-lived credentials).
- **Public-access** (dev fallback): the server exposes a public endpoint;
  `allowed_cidrs` populates firewall rules and `allow_azure_services = true`
  adds the `AllowAllAzureServices` special rule so cross-region AKS pods
  can connect. TLS + auth still required. Used when the chosen region
  disallows VNet-integrated Free Trial provisioning.

`name_suffix` lets you bypass the global Azure DNS reservation that lingers
~24-72h after a failed create attempt under the same server name.

The module creates the Flexible Server plus the `kanto` logical database.
**No `prevent_destroy`** on the server — initial create can fail (region
capacity, quota), and `prevent_destroy` would block taint+replace. Data
protection lives in the automated daily backups + PITR (configurable
retention 7–35 days).

pgvector binaries ship with Postgres Flexible Server (PG 14+), but the
extension is not loadable until it is added to the server-level
`azure.extensions` allow-list. The module configures
`azure.extensions = "VECTOR"`; without it, `CREATE EXTENSION vector`
returns "extension not allow-listed for azure_pg_admin users". Task 3
migrations then run `CREATE EXTENSION vector;` against the kanto
database.

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

See `variables.tf`. Required in every mode: `resource_group_name`,
`region`, `name_prefix`, `admin_password`, `sku_name`. When
`vnet_integration_enabled = true` (default): `subnet_id`,
`private_dns_zone_id`. When `false`: `allowed_cidrs` (typically the
operator IP) and optionally `allow_azure_services` for cross-region AKS.

## Outputs

`server_id`, `fqdn`, `port`, `database_name`, `admin_username`.
