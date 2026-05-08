provider "oci" {
  region = var.region
}

data "oci_objectstorage_namespace" "tenancy" {
  compartment_id = var.tenancy_ocid
}

locals {
  base_tags = {
    "Project"   = "Kanto"
    "ManagedBy" = "Terraform"
    "Layer"     = "Bootstrap"
  }
}

# Top-level compartment under tenancy root. Holds nothing directly; everything
# lives in one of the four sub-compartments below.
resource "oci_identity_compartment" "kanto" {
  compartment_id = var.tenancy_ocid
  name           = var.name_prefix
  description    = "Top-level Kanto compartment. Children: shared, network, dev, prod."
  freeform_tags  = local.base_tags

  # Compartments hold IAM tags and audit history; never auto-destroy.
  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_identity_compartment" "shared" {
  compartment_id = oci_identity_compartment.kanto.id
  name           = "${var.name_prefix}-shared"
  description    = "Holds Terraform state and other tenancy-wide shared resources."
  freeform_tags  = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_identity_compartment" "network" {
  compartment_id = oci_identity_compartment.kanto.id
  name           = "${var.name_prefix}-network"
  description    = "Reserved for shared networking primitives if we ever peer dev <-> prod."
  freeform_tags  = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_identity_compartment" "dev" {
  compartment_id = oci_identity_compartment.kanto.id
  name           = "${var.name_prefix}-dev"
  description    = "Development environment."
  freeform_tags  = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_identity_compartment" "prod" {
  compartment_id = oci_identity_compartment.kanto.id
  name           = "${var.name_prefix}-prod"
  description    = "Production environment."
  freeform_tags  = local.base_tags

  lifecycle {
    prevent_destroy = true
  }
}

# Bucket holding remote Terraform state. Lives in shared compartment so a bug
# in dev or prod cannot destroy the state bucket alongside the resources.
# Encryption at rest with the OCI-managed key is the default; access control
# is via IAM policy on the shared compartment.
resource "oci_objectstorage_bucket" "tfstate" {
  compartment_id = oci_identity_compartment.shared.id
  namespace      = data.oci_objectstorage_namespace.tenancy.namespace
  name           = var.tfstate_bucket_name

  access_type   = "NoPublicAccess"
  versioning    = "Enabled"
  freeform_tags = merge(local.base_tags, { "Purpose" = "tfstate" })

  lifecycle {
    prevent_destroy = true
  }
}
