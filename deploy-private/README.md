# Private Network Deployment

Deploy all resources behind a private VNet with private endpoints, plus a Foundry Agent (v2) connected to a knowledge base.
This is an **alternative** to the public deployment in `sync/deploy/` — existing scripts remain unchanged.
The sync runtime uses **managed identity / federated identity** for secretless authentication.

## Architecture

![Private network architecture and flows](../docs/diagrams/private-network-flows.svg)

Editable source: [docs/diagrams/private-network-flows.mmd](../docs/diagrams/private-network-flows.mmd)

- Inbound to AI Search is private only through the AI Search Private Endpoint.
- Outbound from the sync subnet to SharePoint Online is forced through UDR and Azure Firewall for controlled egress.
- SharePoint connection uses Entra workload identity federation between the Function runtime and an App Registration (no client secret).
- Foundry Agent Service uses Standard setup with private networking (BYO VNet) and injects runtime networking into the delegated agent subnet.
- Public network access is disabled for Foundry, AI Search, Storage, and Cosmos DB.
- Private DNS coverage should include `privatelink.cognitiveservices.azure.com`, `privatelink.openai.azure.com`, `privatelink.services.ai.azure.com`, `privatelink.search.windows.net`, `privatelink.blob.core.windows.net`, and `privatelink.documents.azure.com`.

## Scripts

| Script | Purpose |
|---|---|
| `deploy-foundry.sh` | **Step 1**: Deploy Foundry account + VNet + Storage + Search + CosmosDB (all private) |
| `deploy-project.sh` | **Step 2**: Create project + capability host + agent (v2 .NET SDK) |
| `deploy-sync-private.sh` | **Optional**: Deploy sync Function App with VNet integration |

## Terraform BYO VNet Support

Private endpoints are supported with Terraform for this deployment model.
The implementation is aligned with the Foundry standard private networking BYO VNet pattern, including:

- Foundry AIServices account with agent subnet network injection
- Private endpoints for Foundry, AI Search, Storage, and Cosmos DB
- Private DNS zone configuration including `privatelink.cognitiveservices.azure.com`, `privatelink.openai.azure.com`, and `privatelink.services.ai.azure.com`

Reference sample: https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-terraform/15b-private-network-standard-agent-setup-byovnet

## Quick Start

```bash
# 1. Deploy the Foundry instance + all private infrastructure
export SUBSCRIPTION_ID=<your-sub>
export LOCATION=swedencentral
export FOUNDRY_ACCOUNT_NAME=my-foundry
./deploy-foundry.sh

# 2. Deploy a project with capability host + create agent
PROJECT_NAME=my-project ./deploy-project.sh

# 3. (Optional) Deploy sync into the VNet
RUNTIME=python ./deploy-sync-private.sh
```

### Multiple projects (shared vs dedicated capability hosts)

```bash
# Shared: all projects inherit account-level connections
SHARED_CAPHOST=true PROJECT_NAME=project-a ./deploy-project.sh

# Dedicated: each project gets its own capability host
PROJECT_NAME=project-b ./deploy-project.sh
```

## Agent Tool (.NET v2 SDK)

The `agent-tool/` directory contains a .NET console app that creates agents using the v2 API
(`Azure.AI.Projects` 2.0.0-beta.1, `PromptAgentDefinition`, `CreateAgentVersionAsync`).

```bash
cd agent-tool

# Create an agent with AI Search grounding
dotnet run -- \
  --endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" \
  --model gpt-4o \
  --search-connection conn-search \
  --index-name sharepoint-index

# Test an existing agent
dotnet run -- \
  --endpoint "https://<account>.services.ai.azure.com/api/projects/<project>" \
  --test "What documents are available?"
```

## What deploy-foundry.sh creates

| Resource | Private | Purpose |
|---|---|---|
| VNet + 3 subnets | — | Network isolation |
| Storage Account + PE | default-action Deny | Blob storage for synced files |
| AI Search + PE | public-access disabled | Vector index with ACL filtering |
| Foundry Account (AIServices) + PE | PE via cognitiveservices | Models + agent host |
| Cosmos DB + PE | public disabled | Thread/file storage for agents |
| Private DNS zones | — | Internal name resolution |
| gpt-4o + text-embedding-3-large | — | Model deployments |

## What deploy-project.sh creates

| Resource | Purpose |
|---|---|
| Foundry Project | Scoped project under the account |
| Connections (Storage, Search, CosmosDB) | Resource connections for capability host |
| Account Capability Host | Enables Agent Service at account level |
| Project Capability Host | BYO resources for agent data (threads, files, vectors) |
| Agent (v2, .NET SDK) | SharePoint knowledge agent with AI Search grounding |

## Prerequisites

- Azure CLI (`az`) with active subscription
- .NET 10 SDK (for agent-tool)
- **Owner** or **Role Based Access Administrator** on the subscription
- Registered providers:
  ```bash
  for ns in Microsoft.CognitiveServices Microsoft.App Microsoft.ContainerService \
             Microsoft.Network Microsoft.Search Microsoft.Storage \
             Microsoft.MachineLearningServices; do
      az provider register --namespace "$ns"
  done
  ```

## Configuration

All settings are read from env vars. Source order: `.env` → `.env.private` → `.foundry-outputs` → explicit exports.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SUBSCRIPTION_ID` | Yes | — | Azure subscription |
| `RESOURCE_GROUP` | — | `rg-spsync-private` | Resource group name |
| `LOCATION` | — | `swedencentral` | Region (must support Foundry agents) |
| `FOUNDRY_ACCOUNT_NAME` | — | auto | Foundry (AIServices) account name |
| `AZURE_STORAGE_ACCOUNT_NAME` | — | auto | Storage account name |
| `SEARCH_SERVICE_NAME` | — | auto | AI Search service name |
| `COSMOSDB_ACCOUNT_NAME` | — | auto | Cosmos DB account name |
| `VNET_NAME` | — | `vnet-spsync` | Virtual network name |
| `CHAT_DEPLOYMENT_NAME` | — | `gpt-4o` | Chat model deployment |
| `EMBEDDING_DEPLOYMENT_NAME` | — | `text-embedding-3-large` | Embedding model |
| `PROJECT_NAME` | — | `spsync-project` | Foundry project name |
| `SHARED_CAPHOST` | — | `false` | Use shared (account-level) capability host |
| `INDEX_NAME` | — | `sharepoint-index` | AI Search index name |

Copy `.env.template` to `../.env.private` and fill in your values, or pass variables as exports.

## Relation to existing deployments

This deployment is independent of `sync/deploy/` and `sync-dotnet/deploy/`.
The sync code is **unchanged** — `deploy-sync-private.sh` deploys the same code
into a VNet-integrated Function App that routes traffic through private endpoints.
