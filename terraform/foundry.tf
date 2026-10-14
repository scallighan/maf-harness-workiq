# ---------------------------------------------------------------------------
# Entra-only Foundry account and project
# ---------------------------------------------------------------------------
resource "azapi_resource" "ai_foundry" {
  type                      = "Microsoft.CognitiveServices/accounts@2026-05-01"
  name                      = local.foundry_name
  parent_id                 = azurerm_resource_group.this.id
  location                  = azurerm_resource_group.this.location
  schema_validation_enabled = false
  tags                      = local.tags

  body = {
    kind     = "AIServices"
    sku      = { name = "S0" }
    identity = { type = "SystemAssigned" }
    properties = {
      disableLocalAuth       = true
      allowProjectManagement = true
      customSubDomainName    = local.foundry_name
      publicNetworkAccess    = "Enabled"
      networkAcls            = { defaultAction = "Allow" }
      networkInjections = [{
        scenario                   = "agent"
        subnetArmId                = ""
        useMicrosoftManagedNetwork = true
      }]
    }
  }

  response_export_values = ["identity.principalId"]
}

resource "azurerm_role_assignment" "foundry_network_connection_approver" {
  for_each = {
    foundry = azapi_resource.ai_foundry.id
    cosmos  = azurerm_cosmosdb_account.this.id
  }

  scope                = each.value
  role_definition_name = "Azure AI Enterprise Network Connection Approver"
  principal_id         = azapi_resource.ai_foundry.output.identity.principalId
  principal_type       = "ServicePrincipal"
}

# This settings API returns no ARM ID, so configure it with an explicit PUT.
resource "azapi_resource_action" "foundry_managed_network" {
  type        = "Microsoft.CognitiveServices/accounts/managedNetworks@2025-10-01-preview"
  resource_id = "${azapi_resource.ai_foundry.id}/managedNetworks/default"
  method      = "PUT"
  when        = "apply"
  retry       = local.foundry_operation_retry

  body = {
    properties = {
      managedNetwork = {
        isolationMode      = "AllowInternetOutbound"
        managedNetworkKind = "V2"
      }
    }
  }

  depends_on = [azurerm_role_assignment.foundry_network_connection_approver]
}

resource "azapi_resource" "foundry_cosmos_outbound_rule" {
  type                      = "Microsoft.CognitiveServices/accounts/managedNetworks/outboundRules@2025-10-01-preview"
  name                      = "cosmos-history"
  parent_id                 = azapi_resource_action.foundry_managed_network.resource_id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    properties = {
      type     = "PrivateEndpoint"
      category = "UserDefined"
      destination = {
        serviceResourceId = azurerm_cosmosdb_account.this.id
        subresourceTarget = "Sql"
      }
    }
  }
}

resource "azapi_resource" "ai_foundry_project" {
  type                      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name                      = local.foundry_project
  parent_id                 = azapi_resource.ai_foundry.id
  location                  = azurerm_resource_group.this.location
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    sku      = { name = "S0" }
    identity = { type = "SystemAssigned" }
    properties = {
      displayName = "Workplace Productivity"
      description = "Agent Harness with delegated Work IQ and verified document-page evidence."
    }
  }

  response_export_values = ["identity.principalId"]

  depends_on = [azapi_resource.foundry_cosmos_outbound_rule]
}

# ---------------------------------------------------------------------------
# Blocking guardrail and chat model
# ---------------------------------------------------------------------------
resource "azapi_resource" "rai_policy" {
  type                      = "Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01"
  name                      = "workplace-guardrail"
  parent_id                 = azapi_resource.ai_foundry.id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    properties = {
      basePolicyName = "Microsoft.Default"
      mode           = "Blocking"
      contentFilters = flatten([
        for source in ["Prompt", "Completion"] : [
          for name in ["Hate", "Sexual", "Violence", "Selfharm"] : {
            name              = name
            source            = source
            enabled           = true
            blocking          = true
            severityThreshold = "Medium"
          }
        ]
      ])
    }
  }

  # Cognitive Services serializes mutations across an account and its children.
  depends_on = [azapi_resource.ai_foundry_project]
}

resource "azapi_resource" "chat_deployment" {
  type                      = "Microsoft.CognitiveServices/accounts/deployments@2024-10-01"
  name                      = var.chat_model
  parent_id                 = azapi_resource.ai_foundry.id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.chat_model_capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.chat_model
        version = var.chat_model_version
      }
      versionUpgradeOption = "OnceNewDefaultVersionAvailable"
      raiPolicyName        = azapi_resource.rai_policy.name
    }
  }
}

# ---------------------------------------------------------------------------
# Project managed-identity image pulls
# ---------------------------------------------------------------------------
resource "azapi_resource" "registry_connection" {
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = "agent-images"
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    properties = {
      category      = "ContainerRegistry"
      target        = azurerm_container_registry.this.login_server
      authType      = "ManagedIdentity"
      isSharedToAll = false
      credentials = {
        clientId   = azapi_resource.ai_foundry_project.output.identity.principalId
        resourceId = azurerm_container_registry.this.id
      }
      metadata = {
        ApiType    = "Azure"
        ResourceId = azurerm_container_registry.this.id
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.project_acr_pull,
    azapi_resource.project_appinsights_connection,
  ]
}

# ---------------------------------------------------------------------------
# Foundry portal observability connections
# ---------------------------------------------------------------------------
resource "azapi_resource" "foundry_appinsights_connection" {
  type                      = "Microsoft.CognitiveServices/accounts/connections@2025-06-01"
  name                      = "${local.foundry_name}-appinsights"
  parent_id                 = azapi_resource.ai_foundry.id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    properties = {
      category      = "AppInsights"
      target        = azurerm_application_insights.this.id
      authType      = "ApiKey"
      isSharedToAll = true
      credentials   = { key = azurerm_application_insights.this.connection_string }
      metadata = {
        ApiType    = "Azure"
        ResourceId = azurerm_application_insights.this.id
      }
    }
  }

  depends_on = [azapi_resource.chat_deployment]
}

resource "azapi_resource" "project_appinsights_connection" {
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name                      = "appi-connection"
  parent_id                 = azapi_resource.ai_foundry_project.id
  schema_validation_enabled = false
  retry                     = local.foundry_operation_retry

  body = {
    properties = {
      category      = "AppInsights"
      target        = azurerm_application_insights.this.id
      authType      = "ApiKey"
      isSharedToAll = false
      credentials   = { key = azurerm_application_insights.this.connection_string }
      metadata = {
        ApiType    = "Azure"
        ResourceId = azurerm_application_insights.this.id
      }
    }
  }

  depends_on = [azapi_resource.foundry_appinsights_connection]
}
