# SharePoint to Azure Blob Storage Sync Job

This Azure Container Apps Job synchronizes files from a SharePoint document library to Azure Blob Storage using Microsoft Graph API.

## Features

- **Incremental sync**: Only uploads new or modified files based on last modified dates and content hashes
- **Delete detection**: Optionally removes blobs that no longer exist in SharePoint
- **Dry run mode**: Preview changes without making actual modifications
- **DefaultAzureCredential**: Uses managed identity for authentication (no secrets required)
- **Recursive folder sync**: Syncs all files in nested folders
- **Metadata tracking**: Stores SharePoint metadata in blob properties for change detection

## Prerequisites

### Azure Resources

1. **Azure Container Apps Environment** with a managed identity
2. **Azure Blob Storage Account**
3. **SharePoint Online site** with a document library

### Managed Identity Permissions

The Container App's managed identity needs the following permissions:

#### For SharePoint (Microsoft Graph API) - Site-Specific Access

Use `Sites.Selected` permission to limit access to **only** the specific SharePoint site needed. This approach uses the ACA Job's managed identity directly without creating a separate app registration.

##### Step 1: Get the Managed Identity Application ID

When you create a system-assigned managed identity, Azure automatically creates an Enterprise Application (Service Principal). You need its **Application ID** (not the Object ID):

```powershell
# Get the managed identity's principal ID (Object ID)
$PRINCIPAL_ID = az containerapp job show `
  --name sharepoint-sync-job `
  --resource-group SmartSupportCompanion-dev-rg `
  --query identity.principalId -o tsv

# Get the Application ID from the service principal
$APP_ID = az ad sp show --id $PRINCIPAL_ID --query appId -o tsv

Write-Host "Principal ID (Object ID): $PRINCIPAL_ID"
Write-Host "Application ID: $APP_ID"
```

##### Step 2: Add Sites.Selected Permission to the Managed Identity

Grant the `Sites.Selected` Microsoft Graph application permission to the managed identity's service principal:

```powershell
# Microsoft Graph App ID (constant)
$GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# Sites.Selected Application Permission ID (constant)
$SITES_SELECTED_ID = "883ea226-0bf2-4a8f-9f9d-92c9162a727d"

# Get the Microsoft Graph service principal ID
$GRAPH_SP_ID = az ad sp show --id $GRAPH_APP_ID --query id -o tsv

# Grant the Sites.Selected permission using Microsoft Graph API
az rest --method POST `
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL_ID/appRoleAssignments" `
  --headers "Content-Type=application/json" `
  --body "{
    `"principalId`": `"$PRINCIPAL_ID`",
    `"resourceId`": `"$GRAPH_SP_ID`",
    `"appRoleId`": `"$SITES_SELECTED_ID`"
  }"
```

> **Note**: This operation requires **Global Administrator** or **Privileged Role Administrator** permissions in Azure AD.

##### Step 3: Grant Access to the Specific SharePoint Site

Now grant the managed identity read access to the specific SharePoint site:

```powershell
# Your SharePoint site ID (get this from SharePoint or Graph Explorer)
$SITE_ID = "<your-tenant>.sharepoint.com,<site-guid>,<web-guid>"

# Or use the site URL format to get the site ID:
# GET https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/<site-name>

# Grant read permission to the specific site
az rest --method POST `
  --url "https://graph.microsoft.com/v1.0/sites/$SITE_ID/permissions" `
  --headers "Content-Type=application/json" `
  --body "{
    `"roles`": [`"read`"],
    `"grantedToIdentities`": [{
      `"application`": {
        `"id`": `"$APP_ID`",
        `"displayName`": `"SharePoint Sync Job Managed Identity`"
      }
    }]
  }"
```

##### Alternative: Using PnP PowerShell

You can also use PnP PowerShell to grant site-specific permissions:

```powershell
# Install PnP PowerShell if needed
# Install-Module -Name PnP.PowerShell -Scope CurrentUser

# Connect to SharePoint Admin Center
Connect-PnPOnline -Url "https://<tenant>-admin.sharepoint.com" -Interactive

# Grant the managed identity access to the specific site
Grant-PnPAzureADAppSitePermission `
  -AppId $APP_ID `
  -DisplayName "SharePoint Sync Job Managed Identity" `
  -Site "https://<tenant>.sharepoint.com/sites/<site-name>" `
  -Permissions Read
```

##### How to Find Your SharePoint Site ID

```powershell
# Using Azure CLI with Graph API
az rest --method GET `
  --url "https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/<site-name>" `
  --query id -o tsv
```

Or use [Microsoft Graph Explorer](https://developer.microsoft.com/graph/graph-explorer):
```
GET https://graph.microsoft.com/v1.0/sites/{tenant}.sharepoint.com:/sites/{site-name}
```

##### Verify Permissions

Check the permissions granted to the managed identity:

```powershell
# List app role assignments for the managed identity
az rest --method GET `
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$PRINCIPAL_ID/appRoleAssignments"

# List site permissions (verify the managed identity has access)
az rest --method GET `
  --url "https://graph.microsoft.com/v1.0/sites/$SITE_ID/permissions"
```

##### Permissions Summary

| Permission | Scope | Description |
|------------|-------|-------------|
| `Sites.Selected` | Specific site(s) only | Principle of least privilege - only accesses the configured SharePoint site |

> **⚠️ Important**: Unlike `Sites.Read.All`, the `Sites.Selected` permission does **not** grant access to any site by default. You must explicitly grant access to each site via the site permissions API (Step 3).

#### For Azure Blob Storage
Assign the `Storage Blob Data Contributor` role:
```bash
az role assignment create \
  --assignee $IDENTITY_OBJECT_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/<storage-account>
```

## Configuration

The job is configured via environment variables:

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

> **Note**: The job automatically resolves the SharePoint Site ID and Drive ID from the URL at runtime. You don't need to look up GUIDs manually.

## Deployment

### Build and Push Docker Image

```bash
cd SmartSupport.SharePointSync

# Build image
docker build -t smartsupportcompanionacrdev.azurecr.io/sharepoint-sync:latest .

# Push to ACR
az acr login --name smartsupportcompanionacrdev
docker push smartsupportcompanionacrdev.azurecr.io/sharepoint-sync:latest
```

### Create Container Apps Job

```powershell
# Create the job with managed identity
az containerapp job create `
  --name sharepoint-sync-job `
  --resource-group SmartSupportCompanion-dev-rg `
  --environment smartsupportacaenvdev `
  --image smartsupportcompanionacrdev.azurecr.io/sharepoint-sync:latest `
  --cpu 0.5 `
  --memory 1Gi `
  --trigger-type Schedule `
  --cron-expression "0 */6 * * *" `
  --replica-timeout 1800 `
  --replica-retry-limit 1 `
  --mi-system-assigned `
  --registry-server smartsupportcompanionacrdev.azurecr.io `
  --env-vars `
    SHAREPOINT_SITE_URL=https://contoso.sharepoint.com/sites/MySite `
    SHAREPOINT_DRIVE_NAME=Documents `
    SHAREPOINT_FOLDER_PATH=/FAQ `
    AZURE_STORAGE_ACCOUNT_NAME=mystorageaccount `
    AZURE_BLOB_CONTAINER_NAME=sharepoint-sync `
    DELETE_ORPHANED_BLOBS=true
```

### Run Job Manually

```powershell
az containerapp job start `
  --name sharepoint-sync-job `
  --resource-group SmartSupportCompanion-dev-rg
```

### View Job Execution History

```powershell
az containerapp job execution list `
  --name sharepoint-sync-job `
  --resource-group SmartSupportCompanion-dev-rg `
  --output table
```

## Local Development

### Setup

```powershell
cd SmartSupport.SharePointSync
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### Authentication Options

For local development, you have two authentication options:

#### Option 1: Azure CLI Authentication (Recommended for quick testing)

```powershell
az login

# Set environment variables
$env:SHAREPOINT_SITE_URL = "https://contoso.sharepoint.com/sites/MySite"
$env:SHAREPOINT_DRIVE_NAME = "Documents"
$env:SHAREPOINT_FOLDER_PATH = "/FAQ"
$env:AZURE_STORAGE_ACCOUNT_NAME = "mystorageaccount"
$env:DRY_RUN = "true"

python main.py
```

#### Option 2: App Registration with Client Secret (Recommended for local testing with specific permissions)

For testing with `Sites.Selected` permission (principle of least privilege), use an App Registration with client credentials:

##### Step 1: Create the App Registration

```powershell
# Login to Azure
az login

# Create the App Registration
$app = az ad app create --display-name "testragsharepoint" --sign-in-audience "AzureADMyOrg" | ConvertFrom-Json
$APP_ID = $app.appId
$APP_OBJECT_ID = $app.id

Write-Host "App ID (Client ID): $APP_ID"
Write-Host "App Object ID: $APP_OBJECT_ID"

# Create Service Principal
az ad sp create --id $APP_ID

# Create a client secret (valid for 1 year)
$creds = az ad app credential reset --id $APP_ID --display-name "LocalTestingSecret" --years 1 | ConvertFrom-Json
Write-Host "Client Secret: $($creds.password)"
Write-Host "Tenant ID: $($creds.tenant)"
```

##### Step 2: Add Microsoft Graph API Permissions

```powershell
# Add Sites.Selected permission (principle of least privilege)
$GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
$SITES_SELECTED_ID = "883ea226-0bf2-4a8f-9f9d-92c9162a727d"

az ad app permission add --id $APP_OBJECT_ID --api $GRAPH_APP_ID --api-permissions "$SITES_SELECTED_ID=Role"

# Grant admin consent
az ad app permission admin-consent --id $APP_OBJECT_ID
```

##### Step 3: Grant Access to the Specific SharePoint Site

```powershell
# Get the SharePoint site ID
$SITE_URL = "m365x33469201.sharepoint.com:/sites/demorag"  # Adjust to your site
$siteInfo = az rest --method GET --url "https://graph.microsoft.com/v1.0/sites/$SITE_URL" | ConvertFrom-Json
$SITE_ID = $siteInfo.id

Write-Host "Site ID: $SITE_ID"

# Get an access token using the app credentials
$tokenBody = @{
    grant_type    = "client_credentials"
    client_id     = $APP_ID
    client_secret = "<your-client-secret>"
    scope         = "https://graph.microsoft.com/.default"
}
$tokenResponse = Invoke-RestMethod -Uri "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" -Method POST -Body $tokenBody
$accessToken = $tokenResponse.access_token

# Grant read permission to the site (requires Sites.FullControl.All temporarily, or use PnP PowerShell)
$headers = @{ Authorization = "Bearer $accessToken"; "Content-Type" = "application/json" }
$permBody = @{
    roles = @("read")
    grantedToIdentities = @(
        @{ application = @{ id = $APP_ID; displayName = "testragsharepoint" } }
    )
} | ConvertTo-Json -Depth 4

Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/sites/$SITE_ID/permissions" -Method POST -Headers $headers -Body $permBody
```

##### Step 4: Run Locally with App Registration

```powershell
# Set environment variables for client credentials authentication
$env:AZURE_CLIENT_ID = "<your-client-id>"
$env:AZURE_CLIENT_SECRET = "<your-client-secret>"
$env:AZURE_TENANT_ID = "<your-tenant-id>"

# Set SharePoint configuration
$env:SHAREPOINT_SITE_URL = "https://m365x33469201.sharepoint.com/sites/demorag"
$env:SHAREPOINT_DRIVE_NAME = "Documents"
$env:SHAREPOINT_FOLDER_PATH = "/"
$env:AZURE_STORAGE_ACCOUNT_NAME = "mystorageaccount"
$env:DRY_RUN = "true"

python main.py
```

> **Note**: When `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and `AZURE_TENANT_ID` environment variables are set, `DefaultAzureCredential` will automatically use client credentials authentication instead of Azure CLI.

##### Verify Permissions

```powershell
# List site permissions to verify access
Invoke-RestMethod -Uri "https://graph.microsoft.com/v1.0/sites/$SITE_ID/permissions" -Method GET -Headers $headers
```

## How It Works

1. **Authentication**: Uses `DefaultAzureCredential` which automatically uses:
   - Managed Identity in Azure Container Apps
   - Azure CLI credentials for local development

2. **ID Resolution**: Automatically resolves SharePoint Site ID and Drive ID from the provided URL using Microsoft Graph API

3. **File Discovery**: Lists all files recursively from the specified SharePoint folder

4. **Change Detection**: For each SharePoint file:
   - If blob doesn't exist → Upload (new file)
   - If blob exists but SharePoint file is newer → Upload (modified file)
   - If blob exists and dates match → Skip (unchanged)

5. **Orphan Cleanup** (optional): Deletes blobs that no longer have a corresponding SharePoint file

5. **Metadata Storage**: Stores SharePoint item ID, last modified date, and content hash in blob metadata for reliable change detection

## Logging

The job outputs structured JSON logs with:
- Sync progress and statistics
- Individual file operations
- Errors and warnings

View logs in Azure Portal or via CLI:
```bash
az containerapp job execution logs show \
  --name sharepoint-sync-job \
  --resource-group SmartSupportCompanion-dev-rg \
  --execution <execution-name>
```
