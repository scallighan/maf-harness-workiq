resource "azuread_service_principal" "workiq" {
  client_id = local.workiq_resource_app_id

  lifecycle {
    prevent_destroy = true
  }
}

resource "azuread_application" "toolbox" {
  display_name     = "${local.gh_repo}-${local.func_name}-toolbox"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  web {
    redirect_uris = var.workiq_toolbox_redirect_uri == "" ? [] : [var.workiq_toolbox_redirect_uri]
  }
  required_resource_access {
    resource_app_id = local.workiq_resource_app_id
    resource_access {
      id   = local.workiq_ask_scope_id
      type = "Scope"
    }
  }

  depends_on = [azuread_service_principal.workiq]
}

resource "azuread_service_principal" "toolbox" {
  client_id = azuread_application.toolbox.client_id
}

resource "azurerm_role_assignment" "agent_toolbox_user" {
  count = local.deploy_agent ? 1 : 0

  scope                = azapi_resource.ai_foundry_project.id
  role_definition_name = "Foundry User"
  principal_id         = azapi_data_plane_resource.agent[0].output.instance_identity.principal_id
  principal_type       = "ServicePrincipal"
}
