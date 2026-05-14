# Key Vault names: globally unique across all of Azure, 3-24 chars,
# alphanumeric + hyphens. Random suffix lets the operator skip the
# "is this name taken" dance.
resource "random_id" "vault_suffix" {
  byte_length = 3
}

resource "azurerm_key_vault" "this" {
  name                = "${var.name_prefix}-kv-${random_id.vault_suffix.hex}"
  resource_group_name = var.resource_group_name
  location            = var.region
  tenant_id           = var.tenant_id
  tags                = var.tags

  sku_name = "standard"

  # RBAC mode (vs. access-policy mode): Azure AD roles control access, with
  # the modern Microsoft.Authorization/roleAssignments resource model. The
  # legacy access_policy block is intentionally NOT used.
  rbac_authorization_enabled = true

  # Purge protection prevents permanent deletion within the soft-delete window
  # (7-90 days). Default 90 days. AKS etcd-encryption keys REQUIRE purge
  # protection (the AKS service rejects the key reference otherwise).
  soft_delete_retention_days = 90
  purge_protection_enabled   = true

  public_network_access_enabled = true # operator + Modal reach this over the internet
  network_acls {
    # Default deny + explicit operator allow-list. AKS workloads inside the
    # VNet read secrets via the AzureServices bypass; Modal authenticates via
    # workload-identity federation through Azure AD, not via this data plane.
    bypass         = "AzureServices"
    default_action = "Deny"
    ip_rules       = var.operator_cidrs
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Grant the principal running Terraform Key Vault Administrator so subsequent
# apply'ers can read/write secrets and keys. Scoped to this vault only.
resource "azurerm_role_assignment" "operator_admin" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = var.operator_object_id
}

# Role propagation in Azure AD takes ~30s. Without this wait, the secret /
# key resources below can race the role and fail with 403 Forbidden.
resource "time_sleep" "role_propagation" {
  depends_on      = [azurerm_role_assignment.operator_admin]
  create_duration = "30s"
}

# Encryption key for AKS etcd. AKS requires:
#   - 2048-bit RSA or higher (we use 2048 for the free tier; bump to 4096 in prod)
#   - Key in a vault with purge_protection_enabled (set above)
#   - The AKS managed identity has Key Vault Crypto User on the key (wired
#     in the iam module).
resource "azurerm_key_vault_key" "aks_etcd" {
  name         = "${var.name_prefix}-aks-etcd"
  key_vault_id = azurerm_key_vault.this.id
  key_type     = "RSA"
  key_size     = 2048
  key_opts     = ["decrypt", "encrypt", "sign", "unwrapKey", "verify", "wrapKey"]
  tags         = var.tags

  depends_on = [time_sleep.role_propagation]

  lifecycle {
    prevent_destroy = true
  }
}

# Mew database password. Generated locally; the value is stored in Terraform
# state (encrypted in the azurerm backend storage account) and as a Key Vault
# secret. The operator can rotate via `az keyvault secret set` and Terraform
# will not overwrite the rotation (see ignore_changes below).
resource "random_password" "mew" {
  length      = 32
  special     = true
  min_lower   = 4
  min_upper   = 4
  min_numeric = 4
  min_special = 4
  # Postgres Flexible Server rejects these in passwords.
  override_special = "!#$%&*()-_=+[]{}<>?"
}

resource "azurerm_key_vault_secret" "mew_password" {
  name         = "${var.name_prefix}-mew-password"
  value        = random_password.mew.result
  key_vault_id = azurerm_key_vault.this.id
  content_type = "text/plain"
  tags         = var.tags

  depends_on = [time_sleep.role_propagation]

  lifecycle {
    ignore_changes = [value]
  }
}

# Placeholder slots populated manually by the operator after apply. We create
# them so the secret IDs are stable and Helm charts in later tasks can
# reference them by name. Operator runs `az keyvault secret set` to set values.
locals {
  placeholder_secrets = ["modal-token", "slack-webhook-url", "pagerduty-integration-key"]
}

resource "azurerm_key_vault_secret" "placeholder" {
  for_each = toset(local.placeholder_secrets)

  name         = "${var.name_prefix}-${each.key}"
  value        = "REPLACE_ME"
  key_vault_id = azurerm_key_vault.this.id
  content_type = "text/plain"
  tags         = var.tags

  depends_on = [time_sleep.role_propagation]

  lifecycle {
    ignore_changes = [value]
  }
}
