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
- **Delta (incremental) sync**: Uses Microsoft Graph delta API to detect only changed files since the last run
- **Delta token persistence**: Stores the Graph delta token in blob storage between runs
- **Delete detection**: Automatically detects files deleted in SharePoint via delta and removes corresponding blobs
- **Folder recursion**: Syncs all files in nested folders
- **Permission sync**: Exports SharePoint permissions as blob metadata on every run (permission changes are invisible to delta)
- **Full sync fallback**: Set `FORCE_FULL_SYNC=true` to bypass delta and do a complete re-scan
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
| `demo/` | Web app demo: Entra ID login with ACL-filtered search |
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

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SHAREPOINT_SITE_URL` | Yes | - | SharePoint site URL (e.g., `https://contoso.sharepoint.com/sites/MySite`) |
| `SHAREPOINT_DRIVE_NAME` | No | `Documents` | Document library name |
| `SHAREPOINT_FOLDER_PATH` | No | `/` | Folder path to sync |
| `AZURE_STORAGE_ACCOUNT_NAME` | Yes | - | Storage account name |
| `AZURE_BLOB_CONTAINER_NAME` | No | `sharepoint-sync` | Container name |
| `AZURE_BLOB_PREFIX` | No | - | Prefix for all blobs |
| `DELETE_ORPHANED_BLOBS` | No | `false` | Delete blobs removed from SharePoint |
| `DRY_RUN` | No | `false` | Preview mode without changes |
| `SYNC_PERMISSIONS` | No | `false` | Sync SharePoint permissions to blob metadata |
| `FORCE_FULL_SYNC` | No | `false` | Skip delta and do a full re-scan of SharePoint |

## How Delta Sync Works

The sync job uses the [Microsoft Graph delta API](https://learn.microsoft.com/en-us/graph/api/driveitem-delta) to efficiently detect changes in SharePoint without listing and comparing every file on each run.

### Sync Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          First Run                                     │
│                                                                        │
│  1. No delta token found in blob storage                               │
│  2. Call GET /drives/{id}/root/delta (full crawl via delta endpoint)   │
│  3. Graph returns ALL items + a deltaLink token                        │
│  4. Upload all files to blob, sync permissions                         │
│  5. Save deltaLink to .sync-state/delta-token.json in blob storage    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                       Subsequent Runs                                  │
│                                                                        │
│  1. Load saved delta token from .sync-state/delta-token.json          │
│  2. Call GET {deltaLink} (returns ONLY items changed since last token) │
│  3. Process changes:                                                   │
│     • Created/modified files → download & upload to blob               │
│     • Deleted files → remove from blob storage                         │
│  4. Save new deltaLink for next run                                    │
│  5. Always re-sync permissions for all files (see note below)          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Delta Change Types

| Change | Delta Reports It? | Action |
|--------|-------------------|--------|
| File created | Yes | Download and upload to blob |
| File content modified | Yes | Re-download and overwrite blob |
| File renamed/moved | Yes | Upload to new path (old path cleaned by orphan detection) |
| File deleted | Yes (`deleted` facet) | Delete blob |
| **Permission changed** | **No** | Handled separately (see below) |

### Why Permissions Are Always Fully Synced

The Graph delta API tracks **file content and metadata changes** (name, size, modified date, etc.) but does **not** report permission changes. A file can have its sharing settings modified without the delta API ever returning it as a changed item.

To ensure permissions stay in sync, the job **always re-fetches permissions for all files** from the Graph API when `SYNC_PERMISSIONS=true`, regardless of whether delta detected any file changes.

### Delta Token Persistence

The delta token is stored as a JSON blob at `.sync-state/delta-token.json` in the same container:

```json
{
  "delta_link": "https://graph.microsoft.com/v1.0/drives/{id}/root/delta?token=...",
  "saved_at": "2026-02-09T21:35:08.772753+00:00"
}
```

To force a full re-crawl, either:
- Set `FORCE_FULL_SYNC=true` in your `.env`
- Delete the `.sync-state/delta-token.json` blob manually

### Sync Modes Summary

| Mode | When | Files Downloaded | Permissions |
|------|------|-----------------|-------------|
| `delta-initial` | First run (no token) | All files | All files |
| `delta-incremental` | Token exists | Only changed files | All files (always) |
| `full` | `FORCE_FULL_SYNC=true` | All files (with hash comparison) | All files |

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

### Demo App

The `demo/` directory contains a Flask web app that demonstrates the full end-to-end ACL flow:

1. User signs in via Entra ID (MSAL authorization code flow)
2. Group IDs are extracted from the ID token `groups` claim
3. Search queries are filtered by the user's group memberships
4. Users only see documents they have access to

#### App Registration Setup (Minimal Permissions — No Admin Consent)

The demo uses a **dedicated app registration** (separate from the sync job's app) that requires only **user-level consent** — no admin approval needed.

**Step 1: Create the app registration**

```bash
# Create the app registration
az ad app create \
  --display-name "SharePoint Search ACL Demo" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "http://localhost:5000/auth/callback"

# Note the appId from the output — this is your DEMO_CLIENT_ID
```

**Step 2: Add a client secret**

```bash
az ad app credential reset \
  --id <app-id> \
  --display-name "demo-secret"

# Note the password — this is your DEMO_CLIENT_SECRET
```

**Step 3: Configure API permissions (delegated only)**

| Permission | Type | Admin Consent? | Purpose |
|------------|------|----------------|---------|
| `User.Read` | Delegated | **No** | Read signed-in user's profile |

That's it — only **one delegated permission** that any user can consent to themselves.

> **Why no `GroupMember.Read.All`?** That scope requires admin consent. Instead, we use `groupMembershipClaims` (Step 4) which embeds group IDs directly in the ID token — no extra Graph API call needed.

**Step 4: Enable group claims in the token**

This is the key configuration that avoids admin consent. Set `groupMembershipClaims` in the app manifest so Entra ID includes the user's security group Object IDs directly in the ID token.

**Option A — Azure Portal (Manifest editor):**

1. Go to **Azure Portal → Microsoft Entra ID → App registrations**
2. Select your app (e.g. "SharePoint Search ACL Demo")
3. In the left menu, click **Manifest**
4. Find the `"groupMembershipClaims"` property (it defaults to `null`)
5. Change it to `"SecurityGroup"`:
   ```json
   "groupMembershipClaims": "SecurityGroup",
   ```
6. Click **Save** at the top of the Manifest editor

**Option B — Azure Portal (Token configuration UI):**

1. Go to **App registrations → your app → Token configuration**
2. Click **+ Add groups claim**
3. In the dialog, check **Security groups**
4. Under "Customize token properties by type", for **ID** tokens select **Group ID**
5. Click **Add**

**Option C — Azure CLI:**

```bash
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications(appId='<app-id>')" \
  --headers "Content-Type=application/json" \
  --body '{"groupMembershipClaims": "SecurityGroup"}'
```

> **Verify it worked:** Go to **App registrations → your app → Manifest** and confirm `"groupMembershipClaims": "SecurityGroup"` is set. You can also check **Token configuration** — it should show a Groups claim configured for Security groups.

**Step 5: Set environment variables**

```bash
# In your .env file
DEMO_CLIENT_ID=<app-id>
DEMO_CLIENT_SECRET=<client-secret>
DEMO_TENANT_ID=<your-tenant-id>
```

#### How It Works (No Admin Consent Required)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Traditional approach (requires admin consent):                        │
│                                                                        │
│  Login → get access token with GroupMember.Read.All scope              │
│        → call Graph /me/memberOf → get group IDs                       │
│        ⚠ GroupMember.Read.All requires admin consent                   │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  Our approach (user consent only):                                     │
│                                                                        │
│  Login with User.Read scope only                                       │
│        → Entra ID includes "groups" claim in the ID token              │
│           (because groupMembershipClaims = "SecurityGroup")            │
│        → App reads group IDs directly from id_token_claims["groups"]   │
│        → No Graph API call needed for groups                           │
│        ✅ Only User.Read (delegated) — any user can consent            │
└─────────────────────────────────────────────────────────────────────────┘
```

The `groupMembershipClaims` manifest setting tells Entra ID to embed the user's security group Object IDs as a `groups` array in the ID token. This is a **tenant-level app configuration**, not a permission — so it doesn't require admin consent at login time. Any user in the tenant can sign in and the token will automatically contain their groups.

**ID token example with groups claim:**
```json
{
  "aud": "59b61f80-...",
  "name": "John Doe",
  "oid": "abc123...",
  "groups": [
    "0828d1e1-a0ba-4a6d-b4e8-8106fca0a281",
    "170f33af-f694-469e-8a33-6a9b5d115d3d"
  ]
}
```

These group IDs are then matched against the `acl_group_ids` field in the search index using an OData filter:

```
search.ismatch('0828d1e1-...', 'acl_group_ids') or search.ismatch('170f33af-...', 'acl_group_ids')
```

> **Note:** If the user belongs to more than 200 groups, Entra ID omits the `groups` claim and includes a `_claim_names` field instead (the "groups overage" scenario). In that case, the app falls back to calling Graph `/me/memberOf` — which would then require the `GroupMember.Read.All` scope and admin consent. For most organizations this limit is not hit.

#### Running the Demo

```bash
cd demo
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

For headless CLI testing without a browser:
```bash
python test_obo_flow.py --query "*"
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
├── demo/                       # ACL search demo app
│   ├── app.py                  # Flask web app (Entra login + ACL search)
│   ├── test_obo_flow.py        # Headless CLI test for ACL flow
│   └── requirements.txt        # Python dependencies
├── tests/                      # Testing
│   ├── test_search.py          # AI Search testing script
│   └── .env.example            # Environment template
└── README.md                   # This file
```

## License

MIT
