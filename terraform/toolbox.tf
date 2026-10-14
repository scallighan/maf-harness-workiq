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

  lifecycle {
    # Setup and Foundry consent manage these URIs; platform applies must not clear them.
    ignore_changes = [identifier_uris, web[0].redirect_uris]
  }

  depends_on = [azuread_service_principal.workiq]
}

resource "azuread_service_principal" "toolbox" {
  client_id = azuread_application.toolbox.client_id
}

resource "azapi_resource" "workiq_mcp" {
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = var.workiq_mcp_connection_name
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false

  body = {
    properties = {
      authType         = "OAuth2"
      category         = "RemoteTool"
      group            = "GenericProtocol"
      target           = local.workiq_mcp_endpoint
      isSharedToAll    = false
      authorizationUrl = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/oauth2/v2.0/authorize"
      tokenUrl         = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/oauth2/v2.0/token"
      refreshUrl       = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/oauth2/v2.0/token"
      scopes           = ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask", "offline_access"]
      metadata = {
        oAuthProvider = "custom"
        type          = "custom_MCP"
      }
    }
  }

  # Import preserves Foundry's stored secret. Supply an ephemeral value only for creation/rotation.
  sensitive_body = var.workiq_mcp_client_secret == null ? {} : {
    properties = {
      credentials = {
        clientId     = azuread_application.toolbox.client_id
        clientSecret = var.workiq_mcp_client_secret
      }
    }
  }
  sensitive_body_version = var.workiq_mcp_secret_version == "0" ? {} : {
    "properties.credentials" = var.workiq_mcp_secret_version
  }
  response_export_values = ["properties.redirectUrl"]

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [azuread_service_principal.toolbox]
}

resource "azurerm_role_assignment" "agent_toolbox_user" {
  count = local.deploy_agent ? 1 : 0

  scope                = azapi_resource.ai_foundry_project.id
  role_definition_name = "Foundry User"
  principal_id         = azapi_data_plane_resource.agent[0].output.instance_identity.principal_id
  principal_type       = "ServicePrincipal"
}
