# Two user-assigned managed identities:
#   1. aks_workloads — fed to AKS pods via Workload Identity Federation (the
#      cluster's OIDC issuer + a federated credential per ServiceAccount).
#      This is the principal Kanto services (Snorlax, Growlithe, Alakazam,
#      Chatot, the API) use to call Azure data planes.
#   2. modal — federated to Modal's OIDC issuer. Modal-side functions present
#      a short-lived OIDC token from Modal; Azure AD exchanges it for an AAD
#      token scoped to this identity's role assignments. Replaces the long-
#      lived API-key pattern from OCI.

resource "azurerm_user_assigned_identity" "aks_workloads" {
  name                = "${var.name_prefix}-aks-workloads"
  resource_group_name = var.resource_group_name
  location            = var.region
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "modal" {
  name                = "${var.name_prefix}-modal"
  resource_group_name = var.resource_group_name
  location            = var.region
  tags                = var.tags
}

# -----------------------------------------------------------------------------
# AKS workloads — blob, secrets, event hubs, K8s federation
# -----------------------------------------------------------------------------

# Read proteins (Snorlax reads back), embeddings (Alakazam reads parquet).
resource "azurerm_role_assignment" "aks_blob_reader" {
  for_each = {
    proteins   = var.proteins_container_id
    embeddings = var.embeddings_container_id
  }

  scope                = each.value
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.aks_workloads.principal_id
}

# Snorlax writes proteins; Growlithe writes metadata. Embeddings is written
# by Modal, not AKS pods — intentionally not granted here.
resource "azurerm_role_assignment" "aks_blob_contributor" {
  for_each = {
    proteins = var.proteins_container_id
    metadata = var.metadata_container_id
  }

  scope                = each.value
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.aks_workloads.principal_id
}

# Read secrets from Key Vault (Mew password + placeholders).
resource "azurerm_role_assignment" "aks_kv_secrets_user" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.aks_workloads.principal_id
}

# Produce + consume on every event hub in the namespace.
resource "azurerm_role_assignment" "aks_eh_sender" {
  scope                = var.event_hubs_namespace_id
  role_definition_name = "Azure Event Hubs Data Sender"
  principal_id         = azurerm_user_assigned_identity.aks_workloads.principal_id
}

resource "azurerm_role_assignment" "aks_eh_receiver" {
  scope                = var.event_hubs_namespace_id
  role_definition_name = "Azure Event Hubs Data Receiver"
  principal_id         = azurerm_user_assigned_identity.aks_workloads.principal_id
}

# -----------------------------------------------------------------------------
# Modal — minimum scope per the design doc
# -----------------------------------------------------------------------------

resource "azurerm_role_assignment" "modal_proteins_reader" {
  scope                = var.proteins_container_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.modal.principal_id
}

resource "azurerm_role_assignment" "modal_embeddings_contributor" {
  scope                = var.embeddings_container_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.modal.principal_id
}

# Modal sends to kanto.embedded only — scoped to that single event hub.
resource "azurerm_role_assignment" "modal_embedded_sender" {
  scope                = var.event_hubs_embedded_id
  role_definition_name = "Azure Event Hubs Data Sender"
  principal_id         = azurerm_user_assigned_identity.modal.principal_id
}

# Federated credential for Modal: Azure AD trusts tokens that Modal's OIDC
# issuer signs with the given subject claim. Only created when the operator
# has wired Modal's issuer URL into tfvars (prod; dev leaves these empty).
resource "azurerm_federated_identity_credential" "modal" {
  count = var.modal_oidc_issuer == "" ? 0 : 1

  name                = "${var.name_prefix}-modal-federation"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.modal.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.modal_oidc_issuer
  subject             = var.modal_oidc_subject
}
