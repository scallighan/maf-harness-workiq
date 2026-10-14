# ---------------------------------------------------------------------------
# Entra-only conversation history
# ---------------------------------------------------------------------------
resource "azurerm_cosmosdb_account" "this" {
  name                          = "cosmos-${local.func_name}"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  offer_type                    = "Standard"
  kind                          = "GlobalDocumentDB"
  local_authentication_disabled = true
  public_network_access_enabled = false
  tags                          = local.tags

  capabilities {
    name = "EnableServerless"
  }
  consistency_policy {
    consistency_level = "Session"
  }
  geo_location {
    location          = azurerm_resource_group.this.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "this" {
  name                = "workplace"
  resource_group_name = azurerm_resource_group.this.name
  account_name        = azurerm_cosmosdb_account.this.name
}

resource "azurerm_cosmosdb_sql_container" "history" {
  name                  = "chatHistory"
  resource_group_name   = azurerm_resource_group.this.name
  account_name          = azurerm_cosmosdb_account.this.name
  database_name         = azurerm_cosmosdb_sql_database.this.name
  partition_key_paths   = ["/session_id"]
  partition_key_version = 2
  default_ttl           = var.history_retention_days * 86400
}

resource "azurerm_cosmosdb_sql_role_assignment" "agent_history" {
  count = local.deploy_agent ? 1 : 0

  resource_group_name = azurerm_resource_group.this.name
  account_name        = azurerm_cosmosdb_account.this.name
  role_definition_id  = "${azurerm_cosmosdb_account.this.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azapi_data_plane_resource.agent[0].output.instance_identity.principal_id
  scope               = "${azurerm_cosmosdb_account.this.id}/dbs/${azurerm_cosmosdb_sql_database.this.name}/colls/${azurerm_cosmosdb_sql_container.history.name}"
}
