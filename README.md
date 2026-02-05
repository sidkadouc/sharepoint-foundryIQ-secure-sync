# SharePoint to Azure Blob Storage Sync with AI Search Integration

This solution synchronizes files from SharePoint Online to Azure Blob Storage and integrates with Azure AI Search for intelligent document retrieval with access control.

## Architecture

```
┌──────────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│   SharePoint     │────▶│  Sync Job (Python)  │────▶│  Azure Blob      │
│   Online         │     │                     │     │  Storage         │
│                  │     │  - Files            │     │                  │
│  - Documents     │     │  - Permissions/ACLs │     │  + ACL Metadata  │
└──────────────────┘     └─────────────────────┘     └────────┬─────────┘
                                                              │
                         ┌─────────────────────┐              │
                         │  Azure AI Search    │◀─────────────┘
                         │                     │
                         │  - OCR Processing   │     ┌──────────────────┐
                         │  - Text Chunking    │────▶│  Azure OpenAI    │
                         │  - Vector Embeddings│     │  (Embeddings)    │
                         │  - ACL Filtering    │     └──────────────────┘
                         └─────────────────────┘
```

## Features

### SharePoint Sync
- **Incremental sync**: Only uploads new or modified files (based on timestamps and content hashes)
- **Delete detection**: Optionally removes blobs deleted from SharePoint
- **Folder recursion**: Syncs all files in nested folders
- **Permission sync**: Exports SharePoint permissions as blob metadata for search-time filtering
- **Dry run mode**: Preview changes without modifications

### Azure AI Search Integration
- **OCR processing**: Extracts text from images in documents
- **Text chunking**: Splits documents for better retrieval (2000 chars, 200 overlap)
- **Vector embeddings**: Generates embeddings using Azure OpenAI
- **Document-level security**: Filters search results based on user/group ACLs
- **Integrated vectorization**: Automatic query vectorization at search time

## Solution Components

| Directory | Description |
|-----------|-------------|
| `sync/` | SharePoint to Blob sync job (Python) including deployment scripts |
| `ai-search/` | Azure AI Search deployment artifacts (index, indexer, skillset) |
| `tests/` | Search testing scripts |

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Azure CLI (`az login`)
- Azure resources:
  - SharePoint Online site
  - Azure Storage Account (HNS-enabled)
  - Azure AI Search service (Basic tier+)
  - Azure OpenAI service (with embedding model)

### 2. Configure Environment

```bash
# Copy and edit the environment file
cp .env.example .env

# Edit .env with your values (all config in one file)
```

### 3. Run Everything

**Linux/macOS:**
```bash
./run-all.sh
```

**Windows (PowerShell):**
```powershell
.\run-all.ps1
```

This will:
1. Sync files from SharePoint to Blob Storage (with permissions)
2. Create AI Search components (datasource, index, skillset, indexer)
3. Wait for indexing to complete
4. Run search tests to verify

### 4. Run Individual Components

```bash
# Sync only
cd sync && python main.py

# Tests only
cd tests && python test_search.py -q "your query"
```

## Configuration

### Environment Variables

### Core Settings

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SHAREPOINT_SITE_URL` | Yes | The SharePoint site URL | `https://contoso.sharepoint.com/sites/MySite` |
| `SHAREPOINT_DRIVE_NAME` | No | Document library name (default: `Documents`) | `Documents`, `Shared Documents` |
| `SHAREPOINT_FOLDER_PATH` | No | Folder path to sync (default: `/` for root) | `/FAQ`, `/Docs/Policies` |
| `AZURE_STORAGE_ACCOUNT_NAME` | Yes | Azure Storage account name | `mystorageaccount` |
| `AZURE_BLOB_CONTAINER_NAME` | No | Container name (default: `sharepoint-sync`) | `sharepoint-docs` |
| `AZURE_BLOB_PREFIX` | No | Prefix for all blobs | `docs/` |
| `DELETE_ORPHANED_BLOBS` | No | Delete blobs removed from SharePoint (default: `false`) | `true` |
| `DRY_RUN` | No | Preview mode without changes (default: `false`) | `true` |
| `SYNC_PERMISSIONS` | No | Enable permissions synchronization (default: `false`) | `true` |

### Delta Sync Settings

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `PERMISSIONS_DELTA_MODE` | No | Mode for file and permission change detection (default: `hash`) | `hash`, `graph_delta` |
| `DELTA_TOKEN_STORAGE_PATH` | No | Path to store delta tokens for `graph_delta` mode (default: `.delta_tokens`) | `/data/tokens` |

#### Delta Modes

The `PERMISSIONS_DELTA_MODE` setting controls how both **file changes** and **permission changes** are detected:

**`hash` (Default)**: 
- **File Sync**: Full scan of SharePoint - lists all files and compares with blob metadata (last_modified, content_hash)
- **Permissions**: Computes SHA256 hash of permissions, only syncs if hash differs
- Works independently, no external state needed
- Best for: Most scenarios, simpler setup, smaller document libraries

**`graph_delta`**: 
- **File Sync**: Uses Microsoft Graph delta API to track changes since last sync
  - First run: Enumerates all files (initial baseline)
  - Subsequent runs: Only processes files that have been added, modified, or deleted
  - Handles deletions automatically via delta response
- **Permissions**: Uses Graph delta API with `Prefer: deltashowsharingchanges` header
  - Only syncs files with `@microsoft.graph.sharedChanged` annotation
- Stores delta tokens locally to track sync state between runs
- More efficient for large document libraries (only queries changed items)
- Requires `Sites.FullControl.All` permission for proper operation
- Best for: Large document libraries with frequent changes

> **Note**: The blob metadata format remains the same regardless of delta mode, ensuring no breaking changes when switching modes.

### Authentication

The solution supports multiple authentication methods via `DefaultAzureCredential`:

| Method | Use Case | Configuration |
|--------|----------|---------------|
| App Registration | Local development, specific permissions | Set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` |
| Managed Identity | Production (Azure Container Apps) | No configuration needed |
| Azure CLI | Quick local testing | Run `az login` first |

## SharePoint Permissions Setup

### Using Sites.Selected (Recommended)

Grant minimal permissions using `Sites.Selected`:

```powershell
# Get managed identity Application ID
$APP_ID = az ad sp show --id <principal-id> --query appId -o tsv

# Grant Sites.Selected permission
az rest --method POST `
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/<principal-id>/appRoleAssignments" `
  --body '{
    "principalId": "<principal-id>",
    "resourceId": "<graph-sp-id>",
    "appRoleId": "883ea226-0bf2-4a8f-9f9d-92c9162a727d"
  }'

# Grant access to specific site
az rest --method POST `
  --url "https://graph.microsoft.com/v1.0/sites/<site-id>/permissions" `
  --body '{
    "roles": ["read"],
    "grantedToIdentities": [{
      "application": { "id": "<app-id>" }
    }]
  }'
```

### Storage Account Permissions

Assign `Storage Blob Data Contributor` role:

```bash
az role assignment create \
  --assignee <identity-id> \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>
```

## Azure AI Search Deployment

### Prerequisites

1. Azure AI Search service (Basic tier+)
2. Azure OpenAI with embedding model deployment
3. Azure Cognitive Services (for OCR)

### Role Assignments for Search Service

The Search service managed identity needs:
- **Storage**: `Storage Blob Data Reader`
- **OpenAI**: `Cognitive Services OpenAI User`
- **Cognitive Services**: `Cognitive Services User`

### Deploy AI Search Components

```powershell
cd ai-search

./script.ps1 `
  -ResourceGroupName "your-rg" `
  -SearchServiceName "your-search" `
  -StorageAccountName "yourstorage" `
  -StorageContainerName "sharepoint-sync" `
  -OpenAIResourceUri "https://your-openai.openai.azure.com" `
  -OpenAIDeploymentId "text-embedding-3-large" `
  -CognitiveServicesResourceUri "https://your-cognitive.cognitiveservices.azure.com"
```

### AI Search Components

| Component | Description |
|-----------|-------------|
| **Data Source** | Connects to blob storage with managed identity |
| **Index** | Search index with vector field, ACL fields, and semantic config |
| **Skillset** | OCR → Merge → Chunk → Embed pipeline |
| **Indexer** | Orchestrates document processing with index projections |

### Index Fields

| Field | Type | Purpose |
|-------|------|---------|
| `chunk_id` | String (key) | Unique chunk identifier |
| `chunk` | String | Text content |
| `title` | String | Document title |
| `text_vector` | Collection(Single) | Embedding vector (3072 dims for text-embedding-3-large) |
| `text_parent_id` | String | Parent document identifier |
| `acl_user_ids` | String | Pipe-delimited user Entra IDs (for ACL filtering) |
| `acl_group_ids` | String | Pipe-delimited group Entra IDs (for ACL filtering) |

### Document Chunking

Documents are automatically chunked using the SplitSkill:
- **Chunk size**: 2000 characters
- **Overlap**: 200 characters  
- **Mode**: Pages (semantic boundaries)

### ACL Propagation to Chunks

The skillset propagates ACL metadata from blob storage to each chunk via index projections:

1. **Blob metadata**: `user_ids` and `group_ids` stored as pipe-delimited strings (e.g., `user1|user2`)
2. **Enrichment tree**: Skillset reads from `/document/user_ids` and `/document/group_ids`
3. **Index projections**: Each chunk receives `acl_user_ids` and `acl_group_ids` fields

## Document-Level Security

When `SYNC_PERMISSIONS=true`, the sync job exports SharePoint permissions to blob metadata:

1. **During sync**: Permissions are fetched from SharePoint Graph API
2. **Stored as metadata**: `user_ids` and `group_ids` (pipe-delimited Entra Object IDs)
3. **Indexed by Search**: ACL fields propagated to each chunk via skillset projections
4. **Query filtering**: Use OData filters with `search.ismatch` for access control

### Example Search with ACL Filter

```python
from azure.search.documents import SearchClient

user_id = "user-entra-object-id"
group_ids = ["group-id-1", "group-id-2"]

# Filter using search.ismatch for pipe-delimited string fields
group_filter = " or ".join([f"search.ismatch('{g}', 'acl_group_ids')" for g in group_ids])
filter = f"search.ismatch('{user_id}', 'acl_user_ids') or {group_filter}"

results = client.search(query="...", filter=filter)
```

## Running in Production

### Docker

```bash
# Build
docker build -t sharepoint-sync:latest .

# Run
docker run --env-file .env sharepoint-sync:latest
```

### Azure Function App (Timer Trigger)

Deploy as an Azure Function with daily timer trigger:

```bash
cd sync/deploy
export SUBSCRIPTION_ID="your-subscription-id"
export SHAREPOINT_SITE_URL="https://contoso.sharepoint.com/sites/MySite"
export AZURE_STORAGE_ACCOUNT_NAME="yourstorageaccount"
export AZURE_BLOB_CONTAINER_NAME="sharepoint-sync"
./deploy-function.sh
```

See [sync/deploy/README.md](sync/deploy/README.md) for detailed configuration options.

### Azure Container Apps Job

```bash
az containerapp job create \
  --name sharepoint-sync-job \
  --resource-group your-rg \
  --environment your-env \
  --image your-acr.azurecr.io/sharepoint-sync:latest \
  --trigger-type Schedule \
  --cron-expression "0 0 * * *" \
  --cpu 0.5 --memory 1Gi \
  --mi-system-assigned
```

## Troubleshooting

### Sync Issues

| Issue | Solution |
|-------|----------|
| Authentication failed | Verify credentials/permissions in `.env` |
| Site not found | Check `SHAREPOINT_SITE_URL` format |
| Permission denied on blob | Verify `Storage Blob Data Contributor` role |

### AI Search Issues

| Issue | Solution |
|-------|----------|
| Indexer 0 items | Check data source connection, verify blobs exist |
| Vector search fails | Verify OpenAI deployment, check dimensions match |
| ACLs not filtering | Ensure `SYNC_PERMISSIONS=true` and reindex |

## Project Structure

```
├── sync/                       # SharePoint to Blob sync
│   ├── main.py                 # Sync job entry point
│   ├── config.py               # Configuration management
│   ├── sharepoint_client.py    # SharePoint/Graph API client
│   ├── blob_client.py          # Azure Blob Storage client
│   ├── permissions_sync.py     # Permission sync logic
│   ├── requirements.txt        # Python dependencies
│   ├── Dockerfile              # Container build file
│   ├── .env.example            # Environment template
│   └── deploy/                 # Azure Function deployment
│       ├── deploy-function.sh  # Function App deployment script
│       └── README.md           # Deployment documentation
├── ai-search/                  # Azure AI Search indexing
│   ├── script.ps1              # Deployment script
│   ├── datasource.json         # Blob data source definition
│   ├── index.json              # Search index schema
│   ├── indexer.json            # Indexer with field mappings
│   ├── skillset.json           # AI enrichment pipeline
│   └── .env.example            # Environment template
├── tests/                      # Testing
│   ├── test_search.py          # AI Search testing script
│   └── .env.example            # Environment template
└── README.md                   # This file
```

## License

MIT
