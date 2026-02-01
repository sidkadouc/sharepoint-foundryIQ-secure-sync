# SharePoint Sync to Azure Blob Storage - Function App Deployment

This folder contains the deployment script for running the SharePoint sync as an Azure Function with a daily timer trigger.

## Overview

The deployment creates:
- **Azure Function App** (Python 3.11, Linux)
- **Timer Trigger** function that runs once per day (configurable)
- **Managed Identity** for secure authentication to both SharePoint and Blob Storage

## Prerequisites

1. **Azure CLI** - Install from https://docs.microsoft.com/cli/azure/install-azure-cli
2. **Azure Functions Core Tools v4** - Will be auto-installed if missing
3. **Python 3.11+** - For local development
4. **Azure subscription** with Contributor access

## Quick Start

1. **Copy and configure environment file:**
   ```bash
   cp .env.example .env
   # Edit .env with your values
   ```

2. **Login to Azure:**
   ```bash
   az login
   ```

3. **Run the deployment:**
   ```bash
   chmod +x deploy-function.sh
   ./deploy-function.sh
   ```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SUBSCRIPTION_ID` | Azure subscription ID | `12345678-1234-1234-1234-123456789abc` |
| `SHAREPOINT_SITE_URL` | Full SharePoint site URL | `https://contoso.sharepoint.com/sites/docs` |
| `AZURE_STORAGE_ACCOUNT_NAME` | Target storage account | `mystorageaccount` |
| `AZURE_BLOB_CONTAINER_NAME` | Target container name | `sharepoint-files` |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RESOURCE_GROUP` | `rg-sharepoint-sync` | Resource group name |
| `LOCATION` | `francecentral` | Azure region |
| `FUNCTION_APP_NAME` | `func-sharepoint-sync` | Function App name |
| `SHAREPOINT_DRIVE_NAME` | `Documents` | SharePoint document library |
| `SHAREPOINT_FOLDER_PATH` | `/` | Folder path in library |
| `DELETE_ORPHANED_BLOBS` | `true` | Delete blobs removed from SharePoint |
| `SYNC_PERMISSIONS` | `true` | Sync ACL metadata |
| `TIMER_SCHEDULE` | `0 0 2 * * *` | CRON schedule (2 AM daily) |

### Timer Schedule Examples

| Schedule | CRON Expression |
|----------|-----------------|
| Every hour | `0 0 * * * *` |
| Every 6 hours | `0 0 */6 * * *` |
| Daily at 2 AM UTC | `0 0 2 * * *` |
| Daily at midnight UTC | `0 0 0 * * *` |
| Weekly on Sunday at 3 AM | `0 0 3 * * 0` |

## Post-Deployment Setup

### 1. Grant SharePoint Access to Managed Identity

The Function App's managed identity needs access to read SharePoint:

**Option A: Admin Consent (Recommended for production)**
```bash
# Get the Function App's managed identity object ID
OBJECT_ID=$(az functionapp identity show \
    --name func-sharepoint-sync \
    --resource-group rg-sharepoint-sync \
    --query principalId -o tsv)

# Grant Sites.Read.All permission (requires Global Admin)
az ad app permission admin-consent --id $OBJECT_ID
```

**Option B: Use App Registration with Sites.Selected**
If you need granular permissions, use the existing App Registration credentials:
```bash
az functionapp config appsettings set \
    --name func-sharepoint-sync \
    --resource-group rg-sharepoint-sync \
    --settings \
        "AZURE_CLIENT_ID=your-client-id" \
        "AZURE_CLIENT_SECRET=your-client-secret" \
        "AZURE_TENANT_ID=your-tenant-id"
```

### 2. Verify Storage Access

The script automatically assigns `Storage Blob Data Contributor` to the managed identity. Verify with:
```bash
az role assignment list \
    --assignee $(az functionapp identity show --name func-sharepoint-sync --resource-group rg-sharepoint-sync --query principalId -o tsv) \
    --all
```

## Monitoring

### View Function Logs
```bash
az functionapp log tail \
    --name func-sharepoint-sync \
    --resource-group rg-sharepoint-sync
```

### Check Function Status
```bash
az functionapp show \
    --name func-sharepoint-sync \
    --resource-group rg-sharepoint-sync \
    --query "state"
```

### Monitor Executions in Portal
1. Go to Azure Portal > Function App > Functions > sharepoint_sync
2. Click "Monitor" to see execution history

## Troubleshooting

### Function Not Triggering
1. Check the timer schedule syntax in app settings
2. Verify the Function App is running: `az functionapp show --name func-sharepoint-sync --resource-group rg-sharepoint-sync --query state`
3. Check for errors in logs

### Authentication Errors
1. Verify managed identity is enabled
2. Check role assignments on storage account
3. For SharePoint, ensure Graph API permissions are granted

### Graph API Permission Issues
```bash
# Check what permissions the managed identity has
az ad sp show --id $(az functionapp identity show --name func-sharepoint-sync --resource-group rg-sharepoint-sync --query principalId -o tsv)
```

## Architecture

```
┌─────────────────────┐    Timer Trigger     ┌──────────────────────┐
│  Azure Functions    │◄────(Daily 2AM)──────│   Timer Service      │
│  (Python 3.11)      │                      └──────────────────────┘
└─────────┬───────────┘
          │
          │ Managed Identity
          ▼
┌─────────────────────┐                      ┌──────────────────────┐
│  Microsoft Graph    │──── List Files ─────►│   SharePoint         │
│  API                │◄─── Download ────────│   Document Library   │
└─────────────────────┘                      └──────────────────────┘
          │
          │ Upload with metadata
          ▼
┌─────────────────────┐
│  Azure Blob Storage │
│  (with ACL metadata)│
└─────────────────────┘
```

## Cleanup

To remove all deployed resources:
```bash
az group delete --name rg-sharepoint-sync --yes --no-wait
```

## Local Development

To test the function locally:

1. Install dependencies:
   ```bash
   cd function-app
   pip install -r requirements.txt
   ```

2. Set environment variables or create local.settings.json:
   ```json
   {
       "IsEncrypted": false,
       "Values": {
           "AzureWebJobsStorage": "UseDevelopmentStorage=true",
           "FUNCTIONS_WORKER_RUNTIME": "python",
           "SHAREPOINT_SITE_URL": "https://...",
           "AZURE_STORAGE_ACCOUNT_NAME": "...",
           "AZURE_BLOB_CONTAINER_NAME": "...",
           "TIMER_SCHEDULE": "0 0 2 * * *"
       }
   }
   ```

3. Run locally:
   ```bash
   func start
   ```
