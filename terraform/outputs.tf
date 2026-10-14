output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "foundry_project_endpoint" {
  value = local.foundry_endpoint
}

output "chat_model_deployment" {
  value = var.chat_model
}

output "registry_name" {
  value = azurerm_container_registry.this.name
}

output "registry_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "rai_policy_id" {
  value = azapi_resource.rai_policy.id
}

output "agent_name" {
  value = var.agent_name
}

output "agent_version" {
  description = "Latest hosted-agent version, or null before the first release."
  value       = local.deploy_agent ? azapi_data_plane_resource.agent[0].output.versions.latest.version : null
}

output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "foundry_project_id" {
  value = azapi_resource.ai_foundry_project.id
}

output "workiq_toolbox_endpoint" {
  value = local.toolbox_endpoint
}

output "workiq_toolbox_name" {
  value = var.workiq_toolbox_name
}

output "workiq_toolbox_client_id" {
  value = azuread_application.toolbox.client_id
}

output "workiq_toolbox_application_object_id" {
  value = azuread_application.toolbox.object_id
}

output "workiq_mcp_connection_id" {
  value = azapi_resource.workiq_mcp.id
}

output "workiq_mcp_endpoint" {
  value = local.workiq_mcp_endpoint
}

output "workiq_mcp_allowed_tools" {
  value = var.workiq_mcp_allowed_tools
}

output "workiq_mcp_require_approval" {
  value = var.workiq_mcp_require_approval
}

output "cosmos_endpoint" {
  value = azurerm_cosmosdb_account.this.endpoint
}

output "agent_principal_id" {
  value = local.deploy_agent ? azapi_data_plane_resource.agent[0].output.instance_identity.principal_id : null
}
