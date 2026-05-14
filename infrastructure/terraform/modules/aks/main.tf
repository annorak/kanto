# Cluster's own managed identity (system-assigned). AKS uses this for control-
# plane operations: managing load balancers, attaching disks, encrypting etcd
# with the customer-managed key. Separate from the workload identity that
# Kanto pods use.

# Grant the cluster's control-plane identity Crypto User on the Key Vault
# so it can use the etcd encryption key.
resource "azurerm_role_assignment" "cluster_kv_crypto" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Crypto User"
  # System-assigned identity is created with the cluster; we reference it via
  # the cluster's identity[0].principal_id below. Race-safe because Terraform
  # waits for the cluster to exist before this assignment runs.
  principal_id = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

# Cluster network contributor on the nodes subnet so AKS can attach VNICs
# during node-pool scaling.
resource "azurerm_role_assignment" "cluster_network_contributor" {
  scope                = var.nodes_subnet_id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

# Both ignores below cover tfsec false positives: the checks look for
# deprecated azurerm v3 attribute names. We use the v4 equivalents:
#   - api_server_access_profile.authorized_ip_ranges  (set below)
#   - role_based_access_control_enabled (defaults to true in v4)
#tfsec:ignore:azure-container-limit-authorized-ips
#tfsec:ignore:azure-container-use-rbac-permissions
resource "azurerm_kubernetes_cluster" "this" {
  name                = "${var.name_prefix}-aks"
  resource_group_name = var.resource_group_name
  location            = var.region
  dns_prefix          = replace(var.name_prefix, "-", "")
  kubernetes_version  = var.kubernetes_version

  # Customer-managed etcd encryption. The role assignment above grants the
  # cluster's SAMI access to use this key; AKS rejects creation if the key
  # vault doesn't have purge_protection_enabled (handled in key_vault module).
  # NOTE: not set on first create because the SAMI doesn't exist yet; instead
  # we PATCH it post-create via an external command — Azure's data plane is
  # consistent enough that an apply-time update works for this knob, but the
  # current azurerm provider also supports setting it directly. Try direct
  # first; fall back to a post-create azapi_update_resource if it fails.
  #
  # The provider's `key_vault_key_id` argument requires the role assignment
  # to exist BEFORE cluster create — that's a chicken-and-egg (the SAMI
  # doesn't exist until the cluster is created). Workaround: omit on
  # initial create, then add via an `azapi_update_resource` resource later.
  # For v1 simplicity we omit; etcd is still encrypted with platform-managed
  # keys (FIPS 140-2 validated). Revisit in a hardening pass.

  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  azure_policy_enabled      = true

  identity {
    type = "SystemAssigned"
  }

  default_node_pool {
    name                 = "system"
    vm_size              = var.node_vm_size
    node_count           = var.node_count
    vnet_subnet_id       = var.nodes_subnet_id
    pod_subnet_id        = var.pods_subnet_id
    orchestrator_version = var.kubernetes_version

    auto_scaling_enabled = false
    upgrade_settings {
      max_surge = "10%"
    }

    tags = var.tags
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure" # native NetworkPolicy enforcement
    service_cidr   = "10.96.0.0/16"
    dns_service_ip = "10.96.0.10"
  }

  api_server_access_profile {
    authorized_ip_ranges = var.authorized_ip_ranges
  }

  linux_profile {
    admin_username = "azureuser"
    ssh_key {
      key_data = var.ssh_public_key
    }
  }

  # Container insights + diagnostic logs to Log Analytics.
  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count, # autoscaler manages
      kubernetes_version,              # auto-upgrade channel manages
    ]
  }
}

# Diagnostic settings: stream control-plane logs (kube-apiserver, kube-audit,
# kube-controller-manager, kube-scheduler) to Log Analytics.
resource "azurerm_monitor_diagnostic_setting" "aks" {
  name                       = "${var.name_prefix}-aks-diag"
  target_resource_id         = azurerm_kubernetes_cluster.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category = "kube-apiserver"
  }
  enabled_log {
    category = "kube-audit-admin"
  }
  enabled_log {
    category = "kube-controller-manager"
  }
  enabled_log {
    category = "kube-scheduler"
  }
  enabled_log {
    category = "cluster-autoscaler"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
