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
| **Permission changed** | **No** ¹ | Always fully re-synced |

> ¹ Permissions are always fully re-synced on every run. See [Why permissions are fully re-synced](#why-permissions-are-fully-re-synced) below for details.

## Why Permissions Are Fully Re-synced

The Microsoft Graph **driveItem: delta** API _can_ detect permission changes, but
only when **all** of the following conditions are met:

1. The request includes the `Prefer` header:
   ```
   Prefer: deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges
   ```
   Items whose permissions changed are then annotated with
   `"@microsoft.graph.sharedChanged": "True"` in the delta response.

2. The `Prefer: hierarchicalsharing` header is also added to get sharing info
   only for items with explicit sharing changes (not inherited).

3. The app registration holds **`Sites.FullControl.All`** application permission
   — the docs state: _"In order to process permissions correctly your application
   will need to request Sites.FullControl.All permissions."_

**Our app uses `Sites.Read.All` (or the scoped `Sites.Selected` with read
role).** This is intentional — we follow the principle of least privilege and only
need read access to enumerate files and their permissions. Because we do not (and
should not) request `Sites.FullControl.All`, the delta-based permission change
tracking is **not available** to us.

### Current approach: full permission re-scan

On every sync run the job:
1. Uses the delta API to efficiently detect **file** content changes (adds, edits, deletes).
2. Lists **all** files in the library and re-fetches their permissions via
   `GET /drives/{driveId}/items/{itemId}/permissions` — this endpoint only
   requires `Files.Read.All` / `Sites.Read.All`.
3. Writes the permissions as blob metadata (`user_ids`, `group_ids`) so that
   downstream AI Search can apply ACL filters.

This is the **recommended approach** when you want to stay on `Sites.Read.All`:
- It is simple and correct — no permission change is ever missed.
- The per-file `/permissions` call is lightweight (small JSON, no file download).
- For libraries with a few hundred files the overhead is minimal (a few seconds).

### Alternative: delta-aware permission sync

If your library has **thousands of files** and permission changes are frequent,
you could switch to the delta-aware approach by:

1. Adding `Sites.FullControl.All` to the app registration.
2. Sending the three `Prefer` headers with the delta query.
3. Checking for `@microsoft.graph.sharedChanged` on each item and only
   re-fetching permissions for those items.

This trades a broader permission scope for reduced API calls.

### References

- [driveItem: delta — Scanning permissions hierarchies](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0#scanning-permissions-hierarchies)
- [Best practices for discovering files and detecting changes at scale](https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/scan-guidance)
- [List driveItem permissions](https://learn.microsoft.com/en-us/graph/api/driveitem-list-permissions?view=graph-rest-1.0)

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
