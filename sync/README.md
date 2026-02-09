# SharePoint Sync Job

Python job that syncs files from a SharePoint document library to Azure Blob Storage using the Microsoft Graph delta API. Optionally exports SharePoint permissions as blob metadata for downstream ACL filtering.

## Features

- **Delta (incremental) sync** — only downloads files changed since the last run
- **Delta token persistence** — stores the Graph delta token in blob storage
- **Delete detection** — removes blobs for files deleted in SharePoint
- **Permission sync** — exports SharePoint ACLs as blob metadata (`user_ids`, `group_ids`)
- **Full sync fallback** — set `FORCE_FULL_SYNC=true` to bypass delta
- **Dry run mode** — preview changes without modifications

## How Delta Sync Works

```
First Run:
  GET /drives/{id}/root/delta → returns ALL items + deltaLink token
  → Upload all files, save token to .sync-state/delta-token.json

Subsequent Runs:
  GET {deltaLink} → returns ONLY changed items since last token
  → Process creates/updates/deletes, save new token
  → Always re-sync permissions (delta doesn't track permission changes)
```

| Change | Delta Reports It? | Action |
|--------|-------------------|--------|
| File created/modified | Yes | Download & upload |
| File renamed/moved | Yes | Upload to new path |
| File deleted | Yes | Delete blob |
| **Permission changed** | **No** | Always fully re-synced |

> Permissions are always fully re-synced because the Graph delta API does not report permission changes.

## Files

| File | Description |
|------|-------------|
| `main.py` | Entry point — orchestrates the sync |
| `config.py` | Configuration from environment variables |
| `sharepoint_client.py` | Microsoft Graph API client |
| `blob_client.py` | Azure Blob Storage client |
| `permissions_sync.py` | SharePoint permission export |
| `Dockerfile` | Container build file |
| `requirements.txt` | Python dependencies |
| `deploy/` | Azure Function deployment ([README](deploy/README.md)) |

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python main.py
```

## Environment Variables

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

Uses `DefaultAzureCredential`. Set `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` for app registration, or just `az login` for CLI auth.

### SharePoint Permissions (Sites.Selected)

```bash
# Grant Sites.Selected to your app registration
az rest --method POST \
  --url "https://graph.microsoft.com/v1.0/sites/<site-id>/permissions" \
  --body '{"roles":["read"],"grantedToIdentities":[{"application":{"id":"<app-id>"}}]}'
```

### Storage Permissions

```bash
az role assignment create \
  --assignee <identity-id> \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>
```

## Docker

```bash
docker build -t sharepoint-sync:latest .
docker run --env-file .env sharepoint-sync:latest
```

## Delta Token

Stored at `.sync-state/delta-token.json` in the blob container. Delete it or set `FORCE_FULL_SYNC=true` to force a full re-crawl.
