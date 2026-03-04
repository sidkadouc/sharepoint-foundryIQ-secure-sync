# Sync Job Deployment (.NET)

Deploy the C# sync as an **Azure Function** (timer trigger) or **Azure Container Apps Job** (scheduled).

## Scripts

| Script | Purpose |
|--------|---------|
| `deploy-new.sh` | Create new Function App and/or ACA Job |
| `deploy-existing.sh` | Update code + config on existing resources |

Both scripts use `TARGET=func|aca|both` (default: `both`).

## Quick Start

### Create new resources

```bash
# Function only
TARGET=func ./deploy-new.sh

# ACA only (needs ACR for image build)
ACR_NAME=myacr TARGET=aca ./deploy-new.sh

# Both
ACR_NAME=myacr TARGET=both ./deploy-new.sh
```

### Deploy to existing resources

```bash
# Function
FUNCTION_APP_NAME=func-spsync TARGET=func ./deploy-existing.sh

# ACA
ACA_JOB_NAME=acaj-spsync ACR_NAME=myacr TARGET=aca ./deploy-existing.sh
```

### Validate without deploying

```bash
VALIDATE_ONLY=true TARGET=both ./deploy-new.sh
```

## Configuration

Set in `.env` (root or `sync-dotnet/`) or as env vars.

### Required

| Variable | Description |
|----------|-------------|
| `SUBSCRIPTION_ID` | Azure subscription ID |
| `SHAREPOINT_SITE_URL` | SharePoint site URL |
| `AZURE_STORAGE_ACCOUNT_NAME` | Target storage account |
| `AZURE_BLOB_CONTAINER_NAME` | Target blob container |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `RESOURCE_GROUP` | `rg-sharepoint-sync` | Resource group |
| `LOCATION` | `francecentral` | Azure region |
| `TIMER_SCHEDULE` | `0 0 2 * * *` | Cron schedule |
| `DELETE_ORPHANED_BLOBS` | `false` | Delete blobs removed from SharePoint |
| `SYNC_PERMISSIONS` | `false` | Sync ACL metadata |

### ACA-specific

| Variable | Default | Description |
|----------|---------|-------------|
| `ACR_NAME` | — | ACR registry name (builds image via ACR Tasks) |
| `IMAGE_NAME` | — | Pre-built image (skip ACR build) |
| `ACA_JOB_TRIGGER_TYPE` | `Schedule` | `Schedule` or `Manual` |

## Monitoring

```bash
# Function logs
az functionapp log tail --name <name> --resource-group <rg>

# ACA executions
az containerapp job execution list --name <name> --resource-group <rg> -o table
```
