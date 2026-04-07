variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
  default     = "rg-spsync-private"
}

variable "location" {
  description = "Azure region (must support Foundry agents)"
  type        = string
  default     = "swedencentral"
}

variable "use_existing_vnet" {
  description = "If true, use existing VNet/subnet IDs instead of creating a new VNet"
  type        = bool
  default     = false
}

variable "existing_vnet_id" {
  description = "Existing VNet resource ID (required when use_existing_vnet=true)"
  type        = string
  default     = ""
}

variable "existing_subnet_pe_id" {
  description = "Existing private endpoint subnet resource ID (required when use_existing_vnet=true)"
  type        = string
  default     = ""
}

variable "existing_subnet_agent_id" {
  description = "Existing agent subnet resource ID delegated to Microsoft.App/environments (required when use_existing_vnet=true)"
  type        = string
  default     = ""
}

variable "existing_subnet_sync_id" {
  description = "Optional existing sync subnet resource ID delegated to Microsoft.Web/serverFarms"
  type        = string
  default     = ""
}

# ── Networking ──────────────────────────────────────────────────────────────

variable "vnet_name" {
  description = "Virtual network name"
  type        = string
  default     = "vnet-spsync"
}

variable "vnet_address_prefix" {
  description = "VNet CIDR"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_pe_prefix" {
  description = "Private endpoint subnet CIDR"
  type        = string
  default     = "10.0.1.0/24"
}

variable "subnet_sync_prefix" {
  description = "Sync function subnet CIDR (delegated to Microsoft.Web/serverFarms)"
  type        = string
  default     = "10.0.2.0/24"
}

variable "subnet_agent_prefix" {
  description = "Agent subnet CIDR (delegated to Microsoft.App/environments)"
  type        = string
  default     = "10.0.3.0/24"
}

# ── Resource Names ──────────────────────────────────────────────────────────

variable "storage_account_name" {
  description = "Storage account name (must be globally unique, max 24 chars)"
  type        = string
}

variable "search_service_name" {
  description = "AI Search service name"
  type        = string
}

variable "openai_resource_name" {
  description = "Foundry AIServices account name"
  type        = string
}

variable "cosmosdb_account_name" {
  description = "Cosmos DB account name"
  type        = string
}

# ── AI Models ───────────────────────────────────────────────────────────────

variable "embedding_deployment_name" {
  description = "Name for the embedding model deployment"
  type        = string
  default     = "text-embedding-3-large"
}

variable "chat_deployment_name" {
  description = "Name for the chat model deployment"
  type        = string
  default     = "gpt-4o"
}

# ── Blob ────────────────────────────────────────────────────────────────────

variable "blob_container_name" {
  description = "Blob container for synced SharePoint files"
  type        = string
  default     = "sharepoint-sync"
}
