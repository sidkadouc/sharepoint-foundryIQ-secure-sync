# Purview / RMS Protection in the SharePoint-to-Blob Sync Pipeline

## Table of Contents

- [Overview](#overview)
- [How RMS Encryption Works](#how-rms-encryption-works)
- [How Graph API Handles Decryption](#how-graph-api-handles-decryption)
- [Why RMS Permissions Still Matter](#why-rms-permissions-still-matter)
- [The Two Permission Layers](#the-two-permission-layers)
- [Which Labels Encrypt Files?](#which-labels-encrypt-files)
- [Impact of `Sites.Selected` Permission](#impact-of-sitesselected-permission)
- [Solutions for `Sites.Selected` + RMS](#solutions-for-sitesselected--rms)
- [Pipeline Flow Summary](#pipeline-flow-summary)
- [Required App Permissions](#required-app-permissions)

---

## Overview

This pipeline syncs SharePoint files to Azure Blob Storage and indexes them in Azure AI Search with **security trimming** — meaning search results only show documents a user is allowed to see.

Some SharePoint files have **Microsoft Purview sensitivity labels** that apply **Azure Rights Management (RMS) encryption**. These files add an extra layer of access control that must be accounted for in the search ACLs.

---

## How RMS Encryption Works

When a Purview sensitivity label **with encryption** is applied to a file (e.g., "Highly Confidential"):

1. **The file content is encrypted at rest** in SharePoint using Azure Rights Management (RMS)
2. A **publishing license** is embedded in the file — a signed XML blob that lists:
   - **WHO** can access the file (users, groups)
   - **WHAT** they can do (View, Edit, Print, Copy, etc.)
3. When a regular user opens the file in Word/Excel, the Office client authenticates the user and checks the publishing license to verify they have rights before decrypting

### RMS Usage Rights

| Right | Description |
|-------|-------------|
| `VIEW` | View/Read content |
| `EDIT` | Edit content |
| `SAVE` | Save the document |
| `EXPORT` | Export / Save As |
| `PRINT` | Print |
| `COPY` | Copy content |
| `EXTRACT` | Extract/copy programmatically |
| `OWNER` | Full control |
| `DOCEDIT` | Edit content in Office apps |
| `OBJMODEL` | Access via object model (macros) |

> Reference: [Configure usage rights for Azure Information Protection](https://learn.microsoft.com/en-us/azure/information-protection/configure-usage-rights)

---

## How Graph API Handles Decryption

### With `Files.Read.All` (broad permission)

Graph API **decrypts the file server-side on download**:

1. The app calls `GET /drives/{drive-id}/items/{item-id}/content`
2. Graph sees the app has `Files.Read.All` — an admin-consented application permission with full tenant authority
3. Graph decrypts the file **on the server side** and returns **plaintext content** in the HTTP response
4. The script receives already-decrypted bytes and uploads them to Blob Storage

**The app does NOT need to be listed in the RMS publishing license.** The `Files.Read.All` permission effectively bypasses RMS encryption for reading. This is the **"super user" behavior** for application permissions.

### With `Sites.Selected` (scoped permission)

`Sites.Selected` grants the app access **only to specific SharePoint sites** an admin has explicitly authorized.

- **Downloading unprotected files**: Works fine for authorized sites
- **Downloading RMS-encrypted files**: **Does NOT automatically decrypt**

What happens when downloading an RMS-encrypted file with `Sites.Selected`:

| Scenario | Result |
|----------|--------|
| App's service principal is listed in the RMS policy | Graph decrypts and returns plaintext (rare) |
| App is NOT in the RMS policy | Graph returns encrypted blob or `403 Forbidden` |

The behavior varies by tenant:
- Some tenants return the raw encrypted `.pfile` content (garbled bytes)
- Some return a `403` with error: `"Access denied. The caller does not have permission to decrypt this file."`

**Bottom line**: With only `Sites.Selected`, unprotected files sync fine, but RMS-encrypted files will fail or produce unusable content.

---

## Why RMS Permissions Still Matter

Even when Graph gives the app decrypted content (via `Files.Read.All` or super user), you still need to know **who should be allowed to see the file in search results**.

The pipeline extracts RMS permissions to build **security-trimmed ACLs** for Azure AI Search. Without this, an encrypted "Highly Confidential" file meant for 3 people could appear in everyone's search results.

### How Detection Works in the Pipeline

1. **Read the sensitivity label** from the driveItem via Graph API:
   ```
   GET /drives/{drive-id}/items/{item-id}?$select=sensitivityLabel
   ```
2. **Check if the label has encryption** by looking it up in the tenant's label definitions:
   ```
   GET /security/informationProtection/sensitivityLabels
   ```
3. **Extract RMS permissions** (who can access the encrypted content):
   - Primary: `POST /drives/{drive-id}/items/{item-id}/extractSensitivityLabels`
   - Fallback: `GET /drives/{drive-id}/items/{item-id}/permissions` (SharePoint permissions as approximation)

---

## The Two Permission Layers

For security trimming in AI Search, there are **two independent permission layers**:

| Layer | Source | What it controls |
|-------|--------|-----------------|
| **SharePoint permissions** | File sharing settings | Who can access the file in SharePoint |
| **RMS permissions** | Sensitivity label encryption policy | Who the label grants content access to |

### Effective Access = INTERSECTION

A user must appear in **BOTH** permission sets to see the document in search results.

```
Example:
  SharePoint permissions:  Alice, Bob, Charlie can access the file
  RMS permissions:         Alice, Bob can View the decrypted content
                           ────────────────────────────────────────
  Effective (intersection): Alice, Bob  → these go into AI Search ACLs
```

Charlie has SharePoint access but is NOT in the RMS policy, so even though she can see the file listed in SharePoint, she can't decrypt/open it — so she's excluded from search results.

### Edge Cases

| Situation | Behavior |
|-----------|----------|
| No sensitivity label | SharePoint permissions only |
| Label without encryption (classification only) | SharePoint permissions only |
| Label with encryption, permissions extracted | Intersection of SP ∩ RMS |
| Label with encryption, extraction fails | Falls back to SharePoint permissions only (with warning) |
| RMS grants "All Authenticated Users" | Falls back to SharePoint permissions only |

---

## Which Labels Encrypt Files?

**No label name inherently forces encryption.** Encryption is a **configuration toggle** set by the tenant admin in the Microsoft Purview compliance portal when creating/editing a label.

### Label Creation Flow (Admin)

1. **Name the label** — e.g., "Confidential", "Public" (any name)
2. **Define the scope** — Files, Emails, Meetings
3. **Choose protection actions**:
   - **No encryption** → label is metadata-only (classification tag, no RMS)
   - **Apply encryption** → label triggers RMS with:
     - **Assign permissions now** (admin-defined): admin picks users/groups and rights
     - **Let users assign permissions**: users decide who can access

### Common Patterns (Tenant-Specific)

| Label | Typically encrypted? |
|-------|---------------------|
| Public | No |
| General | No |
| Internal | Sometimes |
| Confidential | Often yes |
| Confidential \ All Employees | Yes — all org users can View |
| Confidential \ Specific People | Yes — only named users |
| Highly Confidential | Almost always yes |
| Highly Confidential \ All Employees | Yes — stricter rights (no copy/print) |

> **Important**: These are conventions, not rules. The same label name "Confidential" could be encryption-enabled in one tenant and metadata-only in another.

### How to Check Your Tenant's Labels

**Via Graph API:**
```http
GET https://graph.microsoft.com/v1.0/security/informationProtection/sensitivityLabels
```

**Via PowerShell:**
```powershell
Connect-IPPSSession
Get-Label | Select-Object Name, DisplayName, Guid, ContentType, EncryptionEnabled | Format-Table
```

### How the Pipeline Detects Encryption

The code uses two approaches (see `_label_has_encryption()` in `purview_client.py`):

1. **Explicit API field**: If `isEncryptingContent: true` is present → definitive
2. **Heuristic fallback**: Name/tooltip matching for keywords like "encrypt", "confidential", "restricted"

> ⚠️ The heuristic is fragile — a label named "Confidential" without encryption would be a false positive. The explicit API field is preferred.

---

## Impact of `Sites.Selected` Permission

### What Works

| Operation | Works with `Sites.Selected`? |
|-----------|------------------------------|
| Download unprotected files | ✅ Yes (for authorized sites) |
| Download label-only files (no encryption) | ✅ Yes |
| Read sensitivity label property on driveItem | ✅ Yes |
| Read file permissions | ✅ Yes |
| **Download RMS-encrypted files (decrypted)** | ❌ **No** — get encrypted blob or 403 |
| Read tenant sensitivity label definitions | ❌ No — requires `InformationProtectionPolicy.Read.All` |

### Consequences for the Pipeline

With `Sites.Selected` only:
- ✅ Unprotected files and label-only files sync correctly
- ❌ RMS-encrypted files: upload encrypted/garbled bytes to Blob → AI Search indexes gibberish
- ❌ Or get `403` and fail to sync the file entirely

---

## Solutions for `Sites.Selected` + RMS

### Option 1: Add `Files.Read.All` Permission

| Aspect | Detail |
|--------|--------|
| **How** | Grant `Files.Read.All` application permission to the app |
| **Decryption** | Graph decrypts everything automatically |
| **Trade-off** | Overly permissive — app can read ALL files in ALL sites |

### Option 2: RMS Super User (⭐ Recommended)

| Aspect | Detail |
|--------|--------|
| **How** | Add the app's service principal as an RMS super user |
| **Decryption** | Graph decrypts RMS content server-side for the app |
| **SharePoint scope** | Still limited to `Sites.Selected` authorized sites (least privilege) |
| **Trade-off** | Best balance of security and functionality |

**Setup (one-time):**
```powershell
# Connect to Azure Information Protection service
Connect-AipService

# Option A: Add the app directly as a super user
Set-AipServiceSuperUser -ServicePrincipalId "<your-app-client-id>"

# Option B: Create a super user group and add the app to it
# (easier to manage multiple apps)
$group = New-AzureADGroup -DisplayName "RMS Super Users" -SecurityEnabled $true -MailEnabled $false -MailNickname "rmssuperusers"
Add-AzureADGroupMember -ObjectId $group.ObjectId -RefObjectId "<app-service-principal-object-id>"
Set-AipServiceSuperUserGroup -GroupObjectId $group.ObjectId
```

**Why this is best**: `Sites.Selected` keeps SharePoint access scoped (least privilege), while the super user role lets Graph decrypt RMS content server-side when the app downloads files.

### Option 3: Skip Encrypted Files

| Aspect | Detail |
|--------|--------|
| **How** | Detect `ProtectionStatus.PROTECTED` and skip the file upload |
| **Trade-off** | Safe and simple, but encrypted content is missing from search |

**Implementation**: After `get_file_protection()` returns `PROTECTED`, log a warning and skip to the next file.

### Option 4: Delegated Permissions (Not Recommended)

| Aspect | Detail |
|--------|--------|
| **How** | Use delegated auth flow with a user who has RMS access |
| **Trade-off** | Not practical for a background sync job (needs user sign-in) |

### Comparison Matrix

| | `Files.Read.All` | RMS Super User | Skip Encrypted | Delegated |
|---|---|---|---|---|
| Decrypts RMS files | ✅ | ✅ | ❌ (skipped) | ✅ |
| Least privilege for SharePoint | ❌ | ✅ | ✅ | ✅ |
| Works for background jobs | ✅ | ✅ | ✅ | ❌ |
| Setup complexity | Low | Medium | Low | High |
| All content in search | ✅ | ✅ | ❌ | ✅ |

---

## Pipeline Flow Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                    SharePoint File Sync Pipeline                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1. List files in SharePoint drive                                   │
│     GET /drives/{drive-id}/root/children                             │
│                                                                      │
│  2. For each file:                                                   │
│     ┌───────────────────────────────────────────────────────────┐    │
│     │ a. Get sensitivity label                                   │    │
│     │    GET /drives/{id}/items/{id}?$select=sensitivityLabel    │    │
│     │                                                            │    │
│     │ b. Check if label has encryption                           │    │
│     │    (lookup in pre-cached label definitions)                │    │
│     │                                                            │    │
│     │ c. If encrypted → extract RMS permissions                  │    │
│     │    POST .../extractSensitivityLabels (or fallback)         │    │
│     │                                                            │    │
│     │ d. Get SharePoint sharing permissions                      │    │
│     │    GET /drives/{id}/items/{id}/permissions                 │    │
│     │                                                            │    │
│     │ e. Merge permissions:                                      │    │
│     │    effective = SP_permissions ∩ RMS_permissions             │    │
│     │                                                            │    │
│     │ f. Download file content (decrypted by Graph)              │    │
│     │    GET /drives/{id}/items/{id}/content                     │    │
│     │                                                            │    │
│     │ g. Upload to Blob Storage with:                            │    │
│     │    - File content                                          │    │
│     │    - Effective ACLs as metadata                            │    │
│     │    - Protection status metadata                            │    │
│     └───────────────────────────────────────────────────────────┘    │
│                                                                      │
│  3. AI Search indexer reads blobs + metadata → security-trimmed index│
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Required App Permissions

### Minimum for unprotected files
| Permission | Type | Purpose |
|------------|------|---------|
| `Sites.Selected` | Application | Read files from specific SharePoint sites |

### Full pipeline (with RMS support)
| Permission | Type | Purpose |
|------------|------|---------|
| `Sites.Selected` | Application | Read files from specific SharePoint sites |
| `InformationProtectionPolicy.Read.All` | Application | Read sensitivity label definitions |
| **RMS Super User** (via `Set-AipServiceSuperUser`) | AIP Config | Decrypt RMS-encrypted files server-side |

### Alternative (broader, simpler)
| Permission | Type | Purpose |
|------------|------|---------|
| `Files.Read.All` | Application | Read + decrypt all files (bypasses RMS) |
| `InformationProtectionPolicy.Read.All` | Application | Read sensitivity label definitions |
