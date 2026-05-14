# mew module

OCI Database for PostgreSQL ("Mew") with pgvector enabled.

The module creates a single `oci_psql_db_system` with daily backups
(PITR is automatic), TLS required (OCI default), and a private endpoint
inside the env's private subnet. **No `prevent_destroy`** on the DB system
— initial create can fail (shape unavailability, quota issues), and
`prevent_destroy` would block recovery. Data is protected via the
automated backups + PITR.

pgvector is preinstalled in OCI Database for PostgreSQL 16, so no custom
`oci_psql_configuration` is needed; the DB system uses the default
configuration for the shape and version. The Task 3 migrations run
`CREATE EXTENSION vector;` against the application database to enable it.

Optionally, when `enable_public_endpoint = true` (prod only), a public OCI
Network Load Balancer is created in the public subnet, forwarding TCP/5432
to the DB system's private IP. The NSG attached to both the DB and NLB
controls which CIDRs can reach 5432; populate
`mew_public_ingress_cidrs` on the network module with Modal's documented
egress ranges. **This is a deliberate v1 security tradeoff** documented in
the design doc.

## Sizing

| Env  | shape                  | OCPU | RAM   | instance_count | system_type            |
| ---- | ---------------------- | ---- | ----- | -------------- | ---------------------- |
| dev  | `VM.Standard.E4.Flex`  | 2    | 16 GB | 1              | `OCI_OPTIMIZED_STORAGE`|
| prod | `VM.Standard.E4.Flex`  | 16   | 128GB | 2 (HA)         | `OCI_OPTIMIZED_STORAGE`|

> **Storage size and BYOK:** OCI Database for PostgreSQL exposes neither
> a `data_storage_size_in_gbs` argument nor a `kms_key_id` for the storage
> layer. Capacity scales server-side based on `storage_details.system_type`
> + `iops`; encryption uses Oracle-managed keys. The design doc's
> "200 GB SSD" target maps to the default storage profile of the
> `OCI_OPTIMIZED_STORAGE` system type, which sizes capacity automatically.
> Revisit if Oracle adds explicit knobs.

## Inputs

See `variables.tf` for the full contract. Required: `compartment_id`,
`name_prefix`, `subnet_id`, `nsg_id`, `admin_password_secret_id`,
`ocpu_count`, `memory_gb`.

## Outputs

`db_system_id`, `private_endpoint_fqdn`, `private_endpoint_host`, `port`,
`database_name`, `admin_username`, `public_endpoint_ip` (null when no NLB).
