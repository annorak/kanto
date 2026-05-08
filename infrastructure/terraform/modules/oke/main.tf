data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_id
}

# OKE-approved worker images for the chosen Kubernetes version + shape.
# The provider returns one entry per (image, version, shape) tuple; we pick
# the first match. Updates to OKE images during the maintenance window
# cycle will not force-replace the node pool because we ignore_changes
# on node_source_details (see below).
data "oci_containerengine_node_pool_option" "workers" {
  node_pool_option_id = "all"
  compartment_id      = var.compartment_id
}

locals {
  # Filter OKE-published images to ones matching our Kubernetes version
  # and worker shape. OKE image display names follow the pattern
  # "Oracle-Linux-8.<n>-...-OKE-<k8s-version>-<build>".
  worker_images = [
    for src in data.oci_containerengine_node_pool_option.workers.sources :
    src.image_id
    if can(regex("OKE-${replace(var.kubernetes_version, "v", "")}-", src.source_name))
  ]
}

resource "oci_containerengine_cluster" "this" {
  compartment_id     = var.compartment_id
  name               = "${var.name_prefix}-oke"
  vcn_id             = var.vcn_id
  kubernetes_version = var.kubernetes_version
  type               = "ENHANCED_CLUSTER"
  kms_key_id         = var.kms_key_id

  endpoint_config {
    subnet_id            = var.endpoint_subnet_id
    is_public_ip_enabled = true
    nsg_ids              = [var.nsg_oke_api_id]
  }

  options {
    service_lb_subnet_ids = [var.lb_subnet_id]

    kubernetes_network_config {
      # VCN-Native CNI uses dedicated subnets, not these CIDRs, but the
      # control plane still requires a service CIDR.
      services_cidr = "10.96.0.0/16"
      pods_cidr     = "10.244.0.0/16"
    }

    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }

    admission_controller_options {
      is_pod_security_policy_enabled = false # PSP is removed in K8s 1.25+
    }
  }

  cluster_pod_network_options {
    cni_type = "OCI_VCN_IP_NATIVE"
  }

  freeform_tags = var.freeform_tags

  lifecycle {
    prevent_destroy = true
  }
}

# OKE-managed NetworkPolicies add-on. Required for Kubernetes NetworkPolicy
# enforcement on top of the OCI-Native CNI.
resource "oci_containerengine_addon" "network_policies" {
  cluster_id                       = oci_containerengine_cluster.this.id
  addon_name                       = "NetworkPolicies"
  remove_addon_resources_on_delete = true
}

resource "oci_containerengine_node_pool" "cpu" {
  cluster_id         = oci_containerengine_cluster.this.id
  compartment_id     = var.compartment_id
  name               = "${var.name_prefix}-oke-cpu"
  node_shape         = var.node_shape
  kubernetes_version = var.kubernetes_version
  ssh_public_key     = var.ssh_public_key

  node_shape_config {
    ocpus         = var.node_ocpus
    memory_in_gbs = var.node_memory_gb
  }

  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.worker_images[0]
    boot_volume_size_in_gbs = var.boot_volume_gb
  }

  node_config_details {
    size    = var.node_count
    nsg_ids = [var.nsg_workers_id]

    # Spread nodes across all availability domains in the region.
    dynamic "placement_configs" {
      for_each = data.oci_identity_availability_domains.ads.availability_domains
      content {
        availability_domain = placement_configs.value.name
        subnet_id           = var.nodes_subnet_id
      }
    }

    node_pool_pod_network_option_details {
      cni_type          = "OCI_VCN_IP_NATIVE"
      pod_subnet_ids    = [var.pods_subnet_id]
      pod_nsg_ids       = [var.nsg_workers_id]
      max_pods_per_node = var.max_pods_per_node
    }

    # Tags the cluster-autoscaler (deployed via Helm later) reads to learn
    # the min/max bounds for this node pool.
    freeform_tags = merge(var.freeform_tags, {
      "k8s.io/cluster-autoscaler/enabled"            = "true"
      "k8s.io/cluster-autoscaler/node-pool-min-size" = tostring(var.node_count_min)
      "k8s.io/cluster-autoscaler/node-pool-max-size" = tostring(var.node_count_max)
    })
  }

  freeform_tags = var.freeform_tags

  # OKE images update with each maintenance cycle; let the cluster-autoscaler
  # roll nodes naturally instead of recreating the pool on every plan.
  lifecycle {
    ignore_changes = [
      node_source_details,
      kubernetes_version,
      node_config_details[0].size,
    ]
  }
}
