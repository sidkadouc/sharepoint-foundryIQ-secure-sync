output "resource_group_name" {
  value = azurerm_resource_group.rg.name
}

output "vnet_name" {
  value = azurerm_virtual_network.vnet.name
}

output "vnet_id" {
  value = azurerm_virtual_network.vnet.id
}

output "subnet_pe_id" {
  value = azurerm_subnet.pe.id
}

output "subnet_sync_id" {
  value = azurerm_subnet.sync.id
}

output "subnet_agent_id" {
  value = azurerm_subnet.agent.id
}

output "storage_account_name" {
  value = azurerm_storage_account.storage.name
}

output "storage_account_id" {
  value = azurerm_storage_account.storage.id
}

output "search_service_name" {
  value = azurerm_search_service.search.name
}

output "search_service_id" {
  value = azurerm_search_service.search.id
}

output "search_principal_id" {
  value = azurerm_search_service.search.identity[0].principal_id
}

output "openai_endpoint" {
  value = "https://${azapi_resource.foundry.name}.cognitiveservices.azure.com/"
}

output "openai_resource_name" {
  value = azapi_resource.foundry.name
}

output "cosmosdb_endpoint" {
  value = azurerm_cosmosdb_account.cosmos.endpoint
}

output "foundry_endpoint" {
  value = "https://${azapi_resource.foundry.name}.services.ai.azure.com"
}
