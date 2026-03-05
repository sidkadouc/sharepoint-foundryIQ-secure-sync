# SharePoint to Azure Blob Storage Sync with AI Search

Sync files from SharePoint Online to Azure Blob Storage with permissions (ACLs), index them in Azure AI Search, and query with document-level security.

## Architecture

```
SharePoint Online ──▶ Sync Job (Python) ──▶ Azure Blob Storage ──▶ Azure AI Search
     files &               delta API              + ACL metadata       + OCR / chunking
     permissions                                                       + vector embeddings
                                                                       + ACL filtering
```

## Components

| Directory | Description | README |
|-----------|-------------|--------|
| [sync/](sync/) | SharePoint → Blob sync job (delta API, permissions) | [sync/README.md](sync/README.md) |
| [ai-search/](ai-search/) | Search index, skillset, indexer deployment | [ai-search/README.md](ai-search/README.md) |
| [demo/](demo/) | Flask web app — Entra ID login + ACL-filtered search | [demo/README.md](demo/README.md) |
| [tests/](tests/) | Search verification scripts | [tests/README.md](tests/README.md) |

## Quick Start

### Prerequisites

- Python 3.11+
- Azure CLI (`az login`)
- Azure resources: SharePoint site, Storage Account, AI Search, Azure OpenAI

### Run Everything

```bash
# Linux/macOS
./run-all.sh

# Windows (PowerShell)
.\run-all.ps1
```

This syncs files, deploys search components, waits for indexing, and runs tests.

### Run Individual Components

```bash
cd sync && python main.py          # Sync only
cd ai-search && ./script.ps1 ...   # Deploy search only
cd demo && python app.py            # Run demo app
cd tests && python test_search.py   # Run tests
```

## Configuration

Create a `.env` file in the root (see each component's README for full details):

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
| `SEARCH_SERVICE_NAME` | Yes | AI Search service name | `my-search-service` |
| `SEARCH_API_KEY` | Yes | AI Search admin key | |

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

## Authentication

| Method | Use Case | Configuration |
|--------|----------|---------------|
| App Registration | Local dev | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` |
| Managed Identity | Production (Container Apps) | No config needed |
| Azure CLI | Quick local testing | `az login` |

## Production Deployment

**Docker:**
```bash
docker build -t sharepoint-sync:latest .
docker run --env-file .env sharepoint-sync:latest
```

**Azure Function App:** See [sync/deploy/README.md](sync/deploy/README.md)

**Azure Container Apps Job:**
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

## License

MIT
