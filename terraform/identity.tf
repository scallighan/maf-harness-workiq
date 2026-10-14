# ---------------------------------------------------------------------------
# Project managed identity and deploying principal
# ---------------------------------------------------------------------------
resource "azurerm_role_assignment" "project_foundry_user" {
  scope                = azapi_resource.ai_foundry.id
  role_definition_name = "Foundry User"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "project_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "current_user_project_manager" {
  scope                = azapi_resource.ai_foundry_project.id
  role_definition_name = "Foundry Project Manager"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "current_user_foundry_user" {
  scope                = azapi_resource.ai_foundry_project.id
  role_definition_name = "Foundry User"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "current_user_acr_push" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "users_foundry_user" {
  for_each = var.user_object_ids

  scope                = azapi_resource.ai_foundry_project.id
  role_definition_name = "Foundry User"
  principal_id         = each.value
  principal_type       = "User"
}

resource "azurerm_role_assignment" "project_appinsights_log_reader" {
  scope                = azurerm_application_insights.this.id
  role_definition_name = "Log Analytics Reader"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "project_appinsights_monitoring_reader" {
  scope                = azurerm_application_insights.this.id
  role_definition_name = "Monitoring Data Reader"
  principal_id         = azapi_resource.ai_foundry_project.output.identity.principalId
  principal_type       = "ServicePrincipal"
}
