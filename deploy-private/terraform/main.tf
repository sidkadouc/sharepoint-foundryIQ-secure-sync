# ─────────────────────────────────────────────────────────────────────────────
# Terraform: Private network infrastructure for SharePoint Sync + Foundry Agent
#
# This deploys all Azure resources behind a VNet with private endpoints:
#   - VNet + 3 subnets (PE, sync, agent)
#   - Storage Account + Private Endpoint
#   - AI Search + Private Endpoint
#   - Foundry account (AIServices) + network injection + Private Endpoint + model deployments
#   - Cosmos DB + Private Endpoint
#   - Private DNS Zones
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

provider "azapi" {
  subscription_id = var.subscription_id
}

# ── Resource Group ──────────────────────────────────────────────────────────

resource "azurerm_resource_group" "rg" {
  name     = var.resource_group_name
  location = var.location
}

# ── Virtual Network + Subnets ──────────────────────────────────────────────

resource "azurerm_virtual_network" "vnet" {
  name                = var.vnet_name
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  address_space       = [var.vnet_address_prefix]
}

resource "azurerm_subnet" "pe" {
  name                 = "snet-private-endpoints"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [var.subnet_pe_prefix]
}

resource "azurerm_subnet" "sync" {
  name                 = "snet-sync"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [var.subnet_sync_prefix]

  delegation {
    name = "serverFarms"
    service_delegation {
      name = "Microsoft.Web/serverFarms"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/action",
      ]
    }
  }
}

resource "azurerm_subnet" "agent" {
  name                 = "snet-agent"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [var.subnet_agent_prefix]

  delegation {
    name = "containerApps"
    service_delegation {
      name = "Microsoft.App/environments"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/action",
      ]
    }
  }
}

# ── Storage Account ────────────────────────────────────────────────────────

resource "azurerm_storage_account" "storage" {
  name                          = var.storage_account_name
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  account_kind                  = "StorageV2"
  min_tls_version               = "TLS1_2"
  public_network_access_enabled = false

  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }
}

resource "azurerm_storage_container" "sync" {
  name                 = var.blob_container_name
  storage_account_id   = azurerm_storage_account.storage.id
}

# ── AI Search ──────────────────────────────────────────────────────────────

resource "azurerm_search_service" "search" {
  name                          = var.search_service_name
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  sku                           = "standard"
  partition_count               = 1
  replica_count                 = 1
  public_network_access_enabled = false
  local_authentication_enabled  = true

  identity {
    type = "SystemAssigned"
  }
}

# Search → Storage RBAC (Storage Blob Data Reader)
resource "azurerm_role_assignment" "search_storage_reader" {
  scope                = azurerm_storage_account.storage.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_search_service.search.identity[0].principal_id
}

# ── Foundry Account (AIServices) ───────────────────────────────────────────

resource "azapi_resource" "foundry" {
  type      = "Microsoft.CognitiveServices/accounts@2025-06-01"
  name      = var.openai_resource_name
  parent_id = azurerm_resource_group.rg.id
  location  = azurerm_resource_group.rg.location

  body = {
    kind = "AIServices"
    sku = {
      name = "S0"
    }
    identity = {
      type = "SystemAssigned"
    }
    properties = {
      allowProjectManagement = true
      customSubDomainName    = var.openai_resource_name
      publicNetworkAccess    = "Disabled"
      disableLocalAuth       = false
      networkAcls = {
        defaultAction = "Allow"
      }
      networkInjections = [
        {
          scenario                   = "agent"
          subnetArmId                = azurerm_subnet.agent.id
          useMicrosoftManagedNetwork = false
        }
      ]
    }
  }
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = var.embedding_deployment_name
  cognitive_account_id = azapi_resource.foundry.id

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-large"
    version = "1"
  }

  sku {
    name     = "Standard"
    capacity = 10
  }
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.chat_deployment_name
  cognitive_account_id = azapi_resource.foundry.id

  model {
    format  = "OpenAI"
    name    = "gpt-4o"
    version = "2024-11-20"
  }

  sku {
    name     = "GlobalStandard"
    capacity = 10
  }

  depends_on = [azurerm_cognitive_deployment.embedding]
}

# Search → OpenAI RBAC
resource "azurerm_role_assignment" "search_openai_user" {
  scope                = azapi_resource.foundry.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_search_service.search.identity[0].principal_id
}

# ── Cosmos DB ──────────────────────────────────────────────────────────────

resource "azurerm_cosmosdb_account" "cosmos" {
  name                          = var.cosmosdb_account_name
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  offer_type                    = "Standard"
  kind                          = "GlobalDocumentDB"
  public_network_access_enabled = false

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.rg.location
    failover_priority = 0
  }
}

# ── Private Endpoints ──────────────────────────────────────────────────────

locals {
  private_endpoints = {
    storage = {
      name                         = "pe-${var.storage_account_name}-blob"
      private_connection_resource_id = azurerm_storage_account.storage.id
      subresource_names             = ["blob"]
      dns_zones                     = ["privatelink.blob.core.windows.net"]
    }
    search = {
      name                         = "pe-${var.search_service_name}"
      private_connection_resource_id = azurerm_search_service.search.id
      subresource_names             = ["searchService"]
      dns_zones                     = ["privatelink.search.windows.net"]
    }
    foundry = {
      name                         = "pe-${var.openai_resource_name}"
      private_connection_resource_id = azapi_resource.foundry.id
      subresource_names             = ["account"]
      dns_zones                     = [
        "privatelink.cognitiveservices.azure.com",
        "privatelink.openai.azure.com",
        "privatelink.services.ai.azure.com"
      ]
    }
    cosmos = {
      name                         = "pe-${var.cosmosdb_account_name}"
      private_connection_resource_id = azurerm_cosmosdb_account.cosmos.id
      subresource_names             = ["Sql"]
      dns_zones                     = ["privatelink.documents.azure.com"]
    }
  }
}

resource "azurerm_private_endpoint" "pe" {
  for_each = local.private_endpoints

  name                = each.value.name
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  subnet_id           = azurerm_subnet.pe.id

  private_service_connection {
    name                           = "psc-${each.key}"
    private_connection_resource_id = each.value.private_connection_resource_id
    subresource_names              = each.value.subresource_names
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [for zone in each.value.dns_zones : azurerm_private_dns_zone.zones[zone].id]
  }
}

# ── Private DNS Zones ──────────────────────────────────────────────────────

locals {
  dns_zones = toset([
    "privatelink.blob.core.windows.net",
    "privatelink.search.windows.net",
    "privatelink.cognitiveservices.azure.com",
    "privatelink.openai.azure.com",
    "privatelink.services.ai.azure.com",
    "privatelink.documents.azure.com",
    "privatelink.file.core.windows.net",
  ])
}

resource "azurerm_private_dns_zone" "zones" {
  for_each = local.dns_zones

  name                = each.value
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "links" {
  for_each = local.dns_zones

  name                  = "link-${replace(each.value, ".", "-")}"
  resource_group_name   = azurerm_resource_group.rg.name
  private_dns_zone_name = azurerm_private_dns_zone.zones[each.value].name
  virtual_network_id    = azurerm_virtual_network.vnet.id
  registration_enabled  = false
}
