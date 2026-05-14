# Log Analytics workspace. Single workspace per env receives:
#   - AKS container insights + control-plane diagnostics (wired by aks module)
#   - Modal-forwarded application logs (Modal-side config in Task 7)
#   - Azure Monitor metrics for the Postgres server, Event Hubs, etc.
#     (per-resource diagnostic_setting bindings are added by their owners.)
resource "azurerm_log_analytics_workspace" "this" {
  name                = "${var.name_prefix}-logs"
  resource_group_name = var.resource_group_name
  location            = var.region

  sku               = "PerGB2018"
  retention_in_days = var.app_log_retention_days
  daily_quota_gb    = -1 # unbounded; free tier ingestion stays under 5 GB/day

  tags = var.tags
}
