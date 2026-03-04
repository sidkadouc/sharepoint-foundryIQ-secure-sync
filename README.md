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
| [sync/](sync/) | SharePoint → Blob sync job — Python (delta API, permissions) | [sync/README.md](sync/README.md) |
| [sync-dotnet/](sync-dotnet/) | SharePoint → Blob sync job — C# .NET (same features) | [sync-dotnet/README.md](sync-dotnet/README.md) |
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
# Python sync
cd sync && python main.py

# .NET sync (build first, then run DLL directly)
cd sync-dotnet && dotnet build
dotnet src/SharePointSync.Job/bin/Debug/net10.0/SharePointSync.Job.dll

# Deploy search
cd ai-search && ./script.ps1 ...

# Demo app
cd demo && python app.py

# Tests
cd tests && python test_search.py
```

## Configuration

Create a `.env` file in the root (see each component's README for full details):

| Variable | Required | Description |
|----------|----------|-------------|
| `SHAREPOINT_SITE_URL` | Yes | e.g. `https://contoso.sharepoint.com/sites/MySite` |
| `AZURE_STORAGE_ACCOUNT_NAME` | Yes | Storage account name |
| `AZURE_BLOB_CONTAINER_NAME` | No | Default: `sharepoint-sync` |
| `SYNC_PERMISSIONS` | No | `true` to sync ACLs to blob metadata |
| `SEARCH_SERVICE_NAME` | Yes | AI Search service name |
| `SEARCH_API_KEY` | Yes | AI Search admin key |

## Authentication

| Method | Use Case | Configuration |
|--------|----------|---------------|
| App Registration | Local dev | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` |
| Managed Identity | Production (Container Apps) | No config needed |
| Azure CLI | Quick local testing | `az login` |

## Production Deployment

Both `sync/` (Python) and `sync-dotnet/` (C#) include deploy scripts for Azure Function App and Azure Container Apps Job. Pick one implementation.

**Docker (Python):**
```bash
cd sync && docker build -t sharepoint-sync:latest .
docker run --env-file .env sharepoint-sync:latest
```

**Docker (.NET):**
```bash
cd sync-dotnet && docker build -t sharepoint-sync-dotnet:latest .
docker run --env-file ../.env sharepoint-sync-dotnet:latest
```

**Azure Function App or ACA Job:** See [sync/deploy/README.md](sync/deploy/README.md) or [sync-dotnet/deploy/README.md](sync-dotnet/deploy/README.md)

## License

MIT
