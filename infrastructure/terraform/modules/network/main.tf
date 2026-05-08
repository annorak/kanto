# Subnet plan inside the /16:
#   public  -> .0.0/24    OKE API endpoint, public load balancers
#   nodes   -> .1.0/24    OKE worker node primary IPs
#   pods    -> .16.0/20   OKE pod IPs (VCN-Native CNI)
#   mew     -> .32.0/28   OCI Database for PostgreSQL endpoint(s)
locals {
  cidr_public = cidrsubnet(var.vcn_cidr, 8, 0)
  cidr_nodes  = cidrsubnet(var.vcn_cidr, 8, 1)
  cidr_pods   = cidrsubnet(var.vcn_cidr, 4, 1)
  cidr_mew    = cidrsubnet(var.vcn_cidr, 12, 512)
}

resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.name_prefix}-vcn"
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = replace(var.name_prefix, "-", "")
  freeform_tags  = var.freeform_tags
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-igw"
  enabled        = true
  freeform_tags  = var.freeform_tags
}

resource "oci_core_nat_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-nat"
  freeform_tags  = var.freeform_tags
}

# Service gateway keeps Object Storage traffic on the OCI backbone instead of
# routing it out via NAT (cheaper and faster).
data "oci_core_services" "all_services" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_service_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-svc"
  freeform_tags  = var.freeform_tags

  services {
    service_id = data.oci_core_services.all_services.services[0].id
  }
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-rt-public"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-rt-private"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.this.id
  }

  route_rules {
    destination       = data.oci_core_services.all_services.services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.this.id
  }
}

resource "oci_core_subnet" "public" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  display_name               = "${var.name_prefix}-subnet-public"
  cidr_block                 = local.cidr_public
  route_table_id             = oci_core_route_table.public.id
  prohibit_public_ip_on_vnic = false
  dns_label                  = "public"
  freeform_tags              = var.freeform_tags
}

resource "oci_core_subnet" "nodes" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  display_name               = "${var.name_prefix}-subnet-nodes"
  cidr_block                 = local.cidr_nodes
  route_table_id             = oci_core_route_table.private.id
  prohibit_public_ip_on_vnic = true
  dns_label                  = "nodes"
  freeform_tags              = var.freeform_tags
}

resource "oci_core_subnet" "pods" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  display_name               = "${var.name_prefix}-subnet-pods"
  cidr_block                 = local.cidr_pods
  route_table_id             = oci_core_route_table.private.id
  prohibit_public_ip_on_vnic = true
  dns_label                  = "pods"
  freeform_tags              = var.freeform_tags
}

resource "oci_core_subnet" "mew" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  display_name               = "${var.name_prefix}-subnet-mew"
  cidr_block                 = local.cidr_mew
  route_table_id             = oci_core_route_table.private.id
  prohibit_public_ip_on_vnic = true
  dns_label                  = "mew"
  freeform_tags              = var.freeform_tags
}

# NSG: OKE control-plane endpoint (public). Restricted to operator CIDRs.
resource "oci_core_network_security_group" "oke_api" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-nsg-oke-api"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "oke_api_ingress_operators" {
  for_each = toset(var.operator_cidrs)

  network_security_group_id = oci_core_network_security_group.oke_api.id
  direction                 = "INGRESS"
  protocol                  = "6" # TCP
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  description               = "kubectl access from operator network"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "oke_api_ingress_workers" {
  network_security_group_id = oci_core_network_security_group.oke_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.oke_workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Workers calling the API server"

  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "oke_api_egress_workers" {
  network_security_group_id = oci_core_network_security_group.oke_api.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = oci_core_network_security_group.oke_workers.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "API server -> kubelet"

  tcp_options {
    destination_port_range {
      min = 10250
      max = 10250
    }
  }
}

# NSG: OKE workers. Allow all egress (NAT + service gateway), allow same-NSG
# pod-to-pod and intra-cluster traffic. Postgres ingress is handled on Mew NSG.
resource "oci_core_network_security_group" "oke_workers" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-nsg-oke-workers"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "workers_ingress_self" {
  network_security_group_id = oci_core_network_security_group.oke_workers.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source                    = oci_core_network_security_group.oke_workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Pod-to-pod and node-to-node"
}

resource "oci_core_network_security_group_security_rule" "workers_egress_all" {
  network_security_group_id = oci_core_network_security_group.oke_workers.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "All egress (via NAT / service gateway)"
}

# NSG: public load balancer. Internet -> 443; egress to workers.
resource "oci_core_network_security_group" "lb" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-nsg-lb"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "lb_ingress_https" {
  network_security_group_id = oci_core_network_security_group.lb.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  description               = "Public HTTPS"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "lb_egress_workers" {
  network_security_group_id = oci_core_network_security_group.lb.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination               = oci_core_network_security_group.oke_workers.id
  destination_type          = "NETWORK_SECURITY_GROUP"
  description               = "LB -> workers"
}

# NSG: Mew Postgres. Always accept from workers; conditionally accept from
# Modal egress ranges (prod). Default-deny otherwise.
resource "oci_core_network_security_group" "mew" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-nsg-mew"
  freeform_tags  = var.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "mew_ingress_workers" {
  network_security_group_id = oci_core_network_security_group.mew.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = oci_core_network_security_group.oke_workers.id
  source_type               = "NETWORK_SECURITY_GROUP"
  description               = "Postgres from OKE workers"

  tcp_options {
    destination_port_range {
      min = 5432
      max = 5432
    }
  }
}

resource "oci_core_network_security_group_security_rule" "mew_ingress_modal" {
  for_each = toset(var.mew_public_ingress_cidrs)

  network_security_group_id = oci_core_network_security_group.mew.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  description               = "Postgres from Modal egress range"

  tcp_options {
    destination_port_range {
      min = 5432
      max = 5432
    }
  }
}
