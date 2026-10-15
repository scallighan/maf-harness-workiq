data "azuread_service_principal" "ui_foundry" {
  count     = local.deploy_ui ? 1 : 0
  client_id = "18a66f5f-dbdf-4c17-9dd7-1634712a9cbe"
}

data "azuread_service_principal" "ui_graph" {
  count     = local.deploy_ui ? 1 : 0
  client_id = "00000003-0000-0000-c000-000000000000"
}

resource "random_uuid" "ui_access_scope" {
  count = local.deploy_ui ? 1 : 0
}

resource "azuread_application" "ui_api" {
  count = local.deploy_ui ? 1 : 0

  display_name     = "${local.gh_repo}-${local.func_name}-ui-api"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  api {
    requested_access_token_version = 2
    oauth2_permission_scope {
      id                         = random_uuid.ui_access_scope[0].result
      value                      = "access_as_user"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Use the Work IQ chat UI"
      admin_consent_description  = "Call the chat UI API as the signed-in user."
      user_consent_display_name  = "Use the Work IQ chat UI"
      user_consent_description   = "Call the chat UI API as you."
    }
  }

  required_resource_access {
    resource_app_id = data.azuread_service_principal.ui_foundry[0].client_id
    resource_access {
      id   = data.azuread_service_principal.ui_foundry[0].oauth2_permission_scope_ids["user_impersonation"]
      type = "Scope"
    }
  }
  required_resource_access {
    resource_app_id = data.azuread_service_principal.ui_graph[0].client_id
    resource_access {
      id   = data.azuread_service_principal.ui_graph[0].oauth2_permission_scope_ids["Files.Read"]
      type = "Scope"
    }
    resource_access {
      id   = data.azuread_service_principal.ui_graph[0].oauth2_permission_scope_ids["Sites.Read.All"]
      type = "Scope"
    }
  }

  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application_identifier_uri" "ui_api" {
  count          = local.deploy_ui ? 1 : 0
  application_id = azuread_application.ui_api[0].id
  identifier_uri = "api://${azuread_application.ui_api[0].client_id}"
}

resource "azuread_service_principal" "ui_api" {
  count     = local.deploy_ui ? 1 : 0
  client_id = azuread_application.ui_api[0].client_id
}

resource "azuread_application" "ui_browser" {
  count = local.deploy_ui ? 1 : 0

  display_name     = "${local.gh_repo}-${local.func_name}-ui-browser"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]
  single_page_application {
    redirect_uris = concat(
      ["${local.ui_url}/", "${local.ui_url}/auth.html"],
      var.ui_additional_redirect_uris,
      [for uri in var.ui_additional_redirect_uris : "${regex("^https?://[^/]+", uri)}/auth.html"]
    )
  }
  required_resource_access {
    resource_app_id = azuread_application.ui_api[0].client_id
    resource_access {
      id   = random_uuid.ui_access_scope[0].result
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "ui_browser" {
  count     = local.deploy_ui ? 1 : 0
  client_id = azuread_application.ui_browser[0].client_id
}

resource "azuread_application_pre_authorized" "ui_browser" {
  count                = local.deploy_ui ? 1 : 0
  application_id       = azuread_application.ui_api[0].id
  authorized_client_id = azuread_application.ui_browser[0].client_id
  permission_ids       = [random_uuid.ui_access_scope[0].result]
}

resource "azurerm_user_assigned_identity" "ui" {
  count               = local.deploy_ui ? 1 : 0
  name                = "mi-${local.ui_name}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azuread_application_federated_identity_credential" "ui" {
  count          = local.deploy_ui ? 1 : 0
  application_id = azuread_application.ui_api[0].id
  display_name   = "container-app-managed-identity"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
  subject        = azurerm_user_assigned_identity.ui[0].principal_id
}

resource "azurerm_virtual_network" "ui" {
  count               = local.deploy_ui ? 1 : 0
  name                = "vnet-${local.ui_name}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = ["10.50.0.0/24"]
  tags                = local.tags
}

resource "azurerm_subnet" "ui_container_apps" {
  count                = local.deploy_ui ? 1 : 0
  name                 = "container-apps"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.ui[0].name
  address_prefixes     = ["10.50.0.0/27"]
  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "ui_private_endpoints" {
  count                             = local.deploy_ui ? 1 : 0
  name                              = "private-endpoints"
  resource_group_name               = azurerm_resource_group.this.name
  virtual_network_name              = azurerm_virtual_network.ui[0].name
  address_prefixes                  = ["10.50.0.32/27"]
  private_endpoint_network_policies = "Disabled"
}

resource "azurerm_private_dns_zone" "ui_cosmos" {
  count               = local.deploy_ui ? 1 : 0
  name                = "privatelink.documents.azure.com"
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "ui_cosmos" {
  count                 = local.deploy_ui ? 1 : 0
  name                  = "ui-cosmos"
  resource_group_name   = azurerm_resource_group.this.name
  private_dns_zone_name = azurerm_private_dns_zone.ui_cosmos[0].name
  virtual_network_id    = azurerm_virtual_network.ui[0].id
  tags                  = local.tags
}

resource "azurerm_private_endpoint" "ui_cosmos" {
  count               = local.deploy_ui ? 1 : 0
  name                = "pe-${local.ui_name}-cosmos"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  subnet_id           = azurerm_subnet.ui_private_endpoints[0].id
  tags                = local.tags
  private_service_connection {
    name                           = "cosmos"
    private_connection_resource_id = azurerm_cosmosdb_account.this.id
    subresource_names              = ["Sql"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "cosmos"
    private_dns_zone_ids = [azurerm_private_dns_zone.ui_cosmos[0].id]
  }
}

resource "azurerm_cosmosdb_sql_container" "ui_sessions" {
  count                 = local.deploy_ui ? 1 : 0
  name                  = "uiSessions"
  resource_group_name   = azurerm_resource_group.this.name
  account_name          = azurerm_cosmosdb_account.this.name
  database_name         = azurerm_cosmosdb_sql_database.this.name
  partition_key_paths   = ["/owner"]
  partition_key_version = 2
  default_ttl           = var.history_retention_days * 86400
}

resource "azurerm_cosmosdb_sql_role_assignment" "ui_sessions" {
  count               = local.deploy_ui ? 1 : 0
  resource_group_name = azurerm_resource_group.this.name
  account_name        = azurerm_cosmosdb_account.this.name
  role_definition_id  = "${azurerm_cosmosdb_account.this.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.ui[0].principal_id
  scope               = "${azurerm_cosmosdb_account.this.id}/dbs/${azurerm_cosmosdb_sql_database.this.name}/colls/${azurerm_cosmosdb_sql_container.ui_sessions[0].name}"
}

resource "azurerm_role_assignment" "ui_acr_pull" {
  count                = local.deploy_ui ? 1 : 0
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.ui[0].principal_id
}

resource "azurerm_container_app_environment" "ui" {
  count                              = local.deploy_ui ? 1 : 0
  name                               = "cae-${local.ui_name}"
  location                           = azurerm_resource_group.this.location
  resource_group_name                = azurerm_resource_group.this.name
  log_analytics_workspace_id         = azurerm_log_analytics_workspace.this.id
  infrastructure_subnet_id           = azurerm_subnet.ui_container_apps[0].id
  infrastructure_resource_group_name = "ME_cae-${local.ui_name}_${azurerm_resource_group.this.name}_${lower(azurerm_resource_group.this.location)}"
  tags                               = local.tags
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
    minimum_count         = 0
    maximum_count         = 0
  }
}

resource "azurerm_container_app" "ui" {
  count                        = local.deploy_ui ? 1 : 0
  name                         = local.ui_name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.ui[0].id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.tags
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.ui[0].id]
  }
  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.ui[0].id
  }
  ingress {
    external_enabled           = true
    allow_insecure_connections = false
    target_port                = 8080
    transport                  = "http"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = var.ui_min_replicas
    max_replicas = 2
    container {
      name   = "ui"
      image  = var.ui_image_uri
      cpu    = 0.5
      memory = "1Gi"
      dynamic "env" {
        for_each = {
          ENABLE_SENSITIVE_DATA     = "false"
          AZURE_CLIENT_ID           = azurerm_user_assigned_identity.ui[0].client_id
          UI_TENANT_ID              = data.azurerm_client_config.current.tenant_id
          UI_API_CLIENT_ID          = azuread_application.ui_api[0].client_id
          UI_BROWSER_CLIENT_ID      = azuread_application.ui_browser[0].client_id
          UI_ORIGIN                 = local.ui_url
          UI_ADDITIONAL_ORIGINS     = join(",", distinct([for uri in var.ui_additional_redirect_uris : regex("^https?://[^/]+", uri)]))
          COSMOS_ENDPOINT           = azurerm_cosmosdb_account.this.endpoint
          COSMOS_DATABASE           = azurerm_cosmosdb_sql_database.this.name
          UI_COSMOS_CONTAINER       = azurerm_cosmosdb_sql_container.ui_sessions[0].name
          AZURE_AI_PROJECT_ENDPOINT = local.foundry_endpoint
          UI_AGENT_NAME             = var.agent_name
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/readyz"
      }
    }
  }
  depends_on = [
    azuread_application_identifier_uri.ui_api,
    azuread_application_pre_authorized.ui_browser,
    azuread_application_federated_identity_credential.ui,
    azurerm_cosmosdb_sql_role_assignment.ui_sessions,
    azurerm_role_assignment.ui_acr_pull,
    azurerm_private_endpoint.ui_cosmos,
    azurerm_private_dns_zone_virtual_network_link.ui_cosmos,
  ]
}
