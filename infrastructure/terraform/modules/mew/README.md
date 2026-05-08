# mew module

OCI Database for PostgreSQL ("Mew") with pgvector enabled.

The module creates two things:

1. A `oci_psql_configuration` that whitelists `pgvector` via the
   `oci.allowed_extensions` configuration override. The actual
   `CREATE EXTENSION vector;` runs in Task 3 migrations.
2. A `oci_psql_db_system` with daily backups (PITR is automatic), TLS
   required (OCI default), bring-your-own-key storage encryption, and a
   private endpoint inside the env's private subnet.

Optionally, when `enable_public_endpoint = true` (prod only), a public OCI
Network Load Balancer is created in the public subnet, forwarding TCP/5432
to the DB system's private IP. The NSG attached to both the DB and NLB
controls which CIDRs can reach 5432; populate
`mew_public_ingress_cidrs` on the network module with Modal's documented
egress ranges. **This is a deliberate v1 security tradeoff** documented in
the design doc.

## Sizing

| Env  | shape                  | OCPU | RAM   | Storage | instance_count | system_type            |
| ---- | ---------------------- | ---- | ----- | ------- | -------------- | ---------------------- |
| dev  | `VM.Standard.E4.Flex`  | 2    | 16 GB | 50 GB   | 1              | `OCI_OPTIMIZED_STORAGE`|
| prod | `VM.Standard.E4.Flex`  | 16   | 128GB | 200GB   | 2 (HA)         | `OCI_OPTIMIZED_STORAGE`|

## Inputs

See `variables.tf` for the full contract. Required: `compartment_id`,
`name_prefix`, `subnet_id`, `nsg_id`, `kms_key_id`,
`admin_password_secret_id`, `ocpu_count`, `memory_gb`, `storage_gb`.

## Outputs

`db_system_id`, `private_endpoint_fqdn`, `private_endpoint_host`, `port`,
`database_name`, `admin_username`, `public_endpoint_ip` (null when no NLB).
