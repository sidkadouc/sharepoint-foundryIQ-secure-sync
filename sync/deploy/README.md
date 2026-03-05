# Sync — Azure Function Deployment

Deploy the SharePoint sync job as an Azure Function with a daily timer trigger and managed identity.

## What Gets Created

- Azure Function App (Python 3.11, Linux)
- Timer trigger (daily at 2 AM UTC by default)
- System-assigned managed identity

## Quick Start

```bash
# Configure
cp .env.example .env   # edit with your values

# Login & deploy
az login
chmod +x deploy-function.sh
./deploy-function.sh
```

## Configuration

### Required

| Variable | Description |
|----------|-------------|
| `SUBSCRIPTION_ID` | Azure subscription ID |
| `SHAREPOINT_SITE_URL` | SharePoint site URL |
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
| `AZURE_BLOB_CONTAINER_NAME` | Target container |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `RESOURCE_GROUP` | `rg-sharepoint-sync` | Resource group |
| `LOCATION` | `francecentral` | Azure region |
| `FUNCTION_APP_NAME` | `func-sharepoint-sync` | Function App name |
| `TIMER_SCHEDULE` | `0 0 2 * * *` | CRON (2 AM daily) |
| `DELETE_ORPHANED_BLOBS` | `true` | Delete removed blobs |
| `SYNC_PERMISSIONS` | `true` | Sync ACL metadata |

## Post-Deployment

### Grant SharePoint Access

The managed identity needs access to read SharePoint. Choose one:

**Option A — Sites.Read.All (admin consent):**
```bash
OBJECT_ID=$(az functionapp identity show --name func-sharepoint-sync --resource-group rg-sharepoint-sync --query principalId -o tsv)
az ad app permission admin-consent --id $OBJECT_ID
```

**Option B — Sites.Selected (granular, recommended):**
Use an app registration with `Sites.Selected` and grant access to the specific site.

### Storage Access

The script auto-assigns `Storage Blob Data Contributor`. Verify:
```bash
az role assignment list --assignee <principal-id> --all
```

## Monitoring

```bash
# Tail logs
az functionapp log tail --name func-sharepoint-sync --resource-group rg-sharepoint-sync

# Check status
az functionapp show --name func-sharepoint-sync --resource-group rg-sharepoint-sync --query state
```

## Cleanup

```bash
az group delete --name rg-sharepoint-sync --yes --no-wait
```
