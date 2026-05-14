# pgvector is preinstalled in OCI Database for PostgreSQL 16 and enabled
# per-database with `CREATE EXTENSION vector;` — that statement is part of
# the Task 3 migrations. No custom oci_psql_configuration is needed; the
# DB system uses the default configuration for the shape and version.
resource "oci_psql_db_system" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.name_prefix}-mew"
  db_version     = var.db_version
  shape          = var.shape
  instance_count = var.instance_count

  instance_ocpu_count         = var.ocpu_count
  instance_memory_size_in_gbs = var.memory_gb

  storage_details {
    is_regionally_durable = true
    system_type           = var.system_type
  }

  network_details {
    subnet_id = var.subnet_id
    nsg_ids   = [var.nsg_id]
  }

  credentials {
    username = var.admin_username
    password_details {
      password_type = "VAULT_SECRET"
      secret_id     = var.admin_password_secret_id
      # secret_version is optional; OCI defaults to current.
    }
  }

  management_policy {
    backup_policy {
      kind           = "DAILY"
      retention_days = var.backup_retention_days
      backup_start   = "02:00"
    }
    maintenance_window_start = "Sun 03:00"
  }

  source {
    source_type = "NONE"
  }

  freeform_tags = var.freeform_tags

  # No prevent_destroy: when initial DB creation fails partway, Terraform
  # needs to taint+replace, and prevent_destroy blocks that. Data is
  # protected via the automated daily backups + PITR configured above.
  lifecycle {
    # OCI auto-applies minor version upgrades during the maintenance window;
    # a plan diff on db_version after such an upgrade should not force-replace.
    ignore_changes = [db_version]
  }
}

# Optional public NLB. Only created when enable_public_endpoint = true (prod).
# Forwards TCP/5432 from the public subnet to the DB system's private IP.
# NSG ingress filtering on the public subnet (in the network module) is what
# restricts which IPs can reach this listener.
resource "oci_network_load_balancer_network_load_balancer" "mew_public" {
  count = var.enable_public_endpoint ? 1 : 0

  compartment_id = var.compartment_id
  display_name   = "${var.name_prefix}-mew-nlb"
  subnet_id      = var.public_subnet_id

  is_private                     = false
  is_preserve_source_destination = false
  network_security_group_ids     = [var.nsg_id]

  freeform_tags = var.freeform_tags
}

resource "oci_network_load_balancer_backend_set" "mew_public" {
  count = var.enable_public_endpoint ? 1 : 0

  network_load_balancer_id = oci_network_load_balancer_network_load_balancer.mew_public[0].id
  name                     = "mew"
  policy                   = "FIVE_TUPLE"
  is_preserve_source       = false

  health_checker {
    protocol = "TCP"
    port     = 5432
  }
}

resource "oci_network_load_balancer_backend" "mew_public" {
  count = var.enable_public_endpoint ? 1 : 0

  network_load_balancer_id = oci_network_load_balancer_network_load_balancer.mew_public[0].id
  backend_set_name         = oci_network_load_balancer_backend_set.mew_public[0].name
  ip_address               = oci_psql_db_system.this.instances_details[0].private_ip
  port                     = 5432
}

resource "oci_network_load_balancer_listener" "mew_public" {
  count = var.enable_public_endpoint ? 1 : 0

  network_load_balancer_id = oci_network_load_balancer_network_load_balancer.mew_public[0].id
  name                     = "postgres"
  default_backend_set_name = oci_network_load_balancer_backend_set.mew_public[0].name
  port                     = 5432
  protocol                 = "TCP"
}
