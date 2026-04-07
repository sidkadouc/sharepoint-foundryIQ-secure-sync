# SharePoint Sync Job (.NET)

C# implementation of the SharePoint-to-Blob sync job. Functionally identical to the [Python version](../sync/), using the same environment variables and blob metadata format.

## Features

- **Delta (incremental) sync** — only downloads files changed since the last run
- **Delta token persistence** — stores the Graph delta token in blob storage
- **Delete detection** — removes blobs for files deleted in SharePoint
- **Permission sync** — exports SharePoint ACLs as blob metadata (`user_ids`, `group_ids`)
- **Full sync fallback** — set `FORCE_FULL_SYNC=true` to bypass delta
- **Dual mode** — runs as Azure Function (timer trigger) or console app (ACA Job / local)

## Project Structure

| Path | Description |
|------|-------------|
| `src/SharePointSync.Core/` | Core sync logic (clients, models, config) |
| `src/SharePointSync.Job/` | Entry point — Function host or console runner |
| `tests/SharePointSync.Tests/` | Unit tests |
| `deploy/` | Deployment scripts for Function App + ACA Job |
| `Dockerfile` | Multi-stage container build |

## Run Locally

```bash
# Set environment variables (from root .env or export them)
set -a && source ../.env && set +a

# Build and run in console mode
dotnet build
dotnet src/SharePointSync.Job/bin/Debug/net10.0/SharePointSync.Job.dll
```

> **Note:** `dotnet run` is intercepted by the Azure Functions Worker SDK and tries to launch `func` CLI. Use the built DLL directly for local console testing.

## Environment Variables

Same as the [Python sync](../sync/README.md#environment-variables):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SHAREPOINT_SITE_URL` | Yes | — | e.g. `https://contoso.sharepoint.com/sites/MySite` |
| `SHAREPOINT_DRIVE_NAME` | No | `Documents` | Document library name |
| `SHAREPOINT_FOLDER_PATH` | No | `/` | Folder path to sync |
| `AZURE_STORAGE_ACCOUNT_NAME` | Yes | — | Storage account name |
| `AZURE_BLOB_CONTAINER_NAME` | No | `sharepoint-sync` | Container name |
| `AZURE_BLOB_PREFIX` | No | — | Prefix for all blobs |
| `DELETE_ORPHANED_BLOBS` | No | `false` | Delete blobs removed from SharePoint |
| `DRY_RUN` | No | `false` | Preview without changes |
| `SYNC_PERMISSIONS` | No | `false` | Export SharePoint permissions to blob metadata |
| `FORCE_FULL_SYNC` | No | `false` | Skip delta, do full re-scan |

## Authentication

- **SharePoint (Graph API)**: `ClientSecretCredential` when `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` are set, otherwise `DefaultAzureCredential`.
- **Blob Storage**: `ManagedIdentityCredential` in Azure, `AzureCliCredential` locally.

## Deployment

Deploy as an **Azure Function** (timer trigger) or **Azure Container Apps Job**:

```bash
# Function App
TARGET=func ./deploy/deploy-new.sh

# ACA Job
ACR_NAME=myacr TARGET=aca ./deploy/deploy-new.sh

# Both
ACR_NAME=myacr TARGET=both ./deploy/deploy-new.sh
```

See [deploy/README.md](deploy/README.md) for details.

## Docker

```bash
docker build -t sharepoint-sync-dotnet:latest .
docker run --env-file ../.env sharepoint-sync-dotnet:latest
```

## Tests

```bash
dotnet test
```
