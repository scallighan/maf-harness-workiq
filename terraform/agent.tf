# ---------------------------------------------------------------------------
# Hosted agent release (skipped until the image is published)
# ---------------------------------------------------------------------------
resource "azapi_data_plane_resource" "agent" {
  count = local.deploy_agent ? 1 : 0

  type      = "Microsoft.Foundry/agents@v1"
  name      = var.agent_name
  parent_id = trimprefix(local.foundry_endpoint, "https://")

  body = {
    name = var.agent_name
    definition = {
      kind = "hosted"
      container_configuration = {
        image = var.agent_image_uri
      }
      cpu    = var.agent_cpu
      memory = var.agent_memory
      protocol_versions = [{
        protocol = "responses"
        version  = "2.0.0"
      }]
      environment_variables = {
        AZURE_AI_PROJECT_ENDPOINT      = local.foundry_endpoint
        AZURE_AI_MODEL_DEPLOYMENT_NAME = var.chat_model
        AZURE_TENANT_ID                = data.azurerm_client_config.current.tenant_id
        TOOLBOX_ENDPOINT               = local.toolbox_endpoint
        COSMOS_ENDPOINT                = azurerm_cosmosdb_account.this.endpoint
        COSMOS_DATABASE                = azurerm_cosmosdb_sql_database.this.name
        COSMOS_HISTORY_CONTAINER       = azurerm_cosmosdb_sql_container.history.name
        ENABLE_SENSITIVE_DATA          = "false"
      }
      rai_config = {
        rai_policy_name = azapi_resource.rai_policy.id
      }
    }
  }

  response_export_values = ["versions.latest.version", "instance_identity.principal_id", "instance_identity.client_id"]

  depends_on = [
    azapi_resource.ai_foundry_project,
    azapi_resource.chat_deployment,
    azapi_resource.registry_connection,
    azapi_resource.foundry_cosmos_outbound_rule,
    azurerm_role_assignment.project_foundry_user,
    azurerm_role_assignment.current_user_project_manager,
    azurerm_role_assignment.current_user_foundry_user,
    azuread_service_principal.toolbox,
  ]
}
