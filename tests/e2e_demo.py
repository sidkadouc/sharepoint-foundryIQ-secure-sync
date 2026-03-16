#!/usr/bin/env python3
"""
End-to-End Demo: SharePoint -> Blob Storage -> AI Search
=========================================================

Demonstrates the complete pipeline:
  Phase 0: Cleanup (delete delta tokens for a fresh start)
  Phase 1: Generate & upload test files to SharePoint
  Phase 2: Initial sync (SharePoint -> Blob + permissions)
  Phase 3: Verify blob contents and ACL metadata
  Phase 4: Modify a file in SharePoint -> delta sync
  Phase 5: Delete a file from SharePoint -> delta sync
  Phase 6: Deploy AI Search index + data source + indexer
  Phase 7: Run indexer & query with ACL filtering

Usage:
  python tests/e2e_demo.py                 # Run all phases
  python tests/e2e_demo.py --skip-search   # Skip AI Search (phases 6-7)
  python tests/e2e_demo.py --phase 3       # Run single phase

Environment variables (loaded from .env):
  # Already configured:
  AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID
  SHAREPOINT_SITE_URL, SHAREPOINT_DRIVE_NAME
  AZURE_STORAGE_ACCOUNT_NAME, AZURE_BLOB_CONTAINER_NAME

  # Add for AI Search (phases 6-7):
  SEARCH_SERVICE_NAME    - e.g. aisearchjtpoc
  SEARCH_API_KEY         - Admin key (or set SEARCH_RESOURCE_GROUP for az CLI lookup)
  SEARCH_RESOURCE_GROUP  - For az CLI key retrieval
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# Ensure sync/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sync"))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import httpx

# Suppress noisy Azure SDK / httpx HTTP-level logging so demo output is readable
import logging

for _logger_name in (
    "azure", "azure.core", "azure.identity", "azure.storage",
    "httpx", "httpcore", "msal", "urllib3",
):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

# ── Configuration ─────────────────────────────────────────────────────────────

TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
SITE_URL = os.getenv("SHAREPOINT_SITE_URL", "")
DRIVE_NAME = os.getenv("SHAREPOINT_DRIVE_NAME", "Documents")
STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
CONTAINER_NAME = os.getenv("AZURE_BLOB_CONTAINER_NAME", "")

# AI Search (optional — phases 6-7 skipped if not set)
SEARCH_SERVICE_NAME = os.getenv("SEARCH_SERVICE_NAME", "")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")
SEARCH_RESOURCE_GROUP = os.getenv("SEARCH_RESOURCE_GROUP", "")
INDEX_NAME = os.getenv("E2E_INDEX_NAME", "e2e-demo-index")
DATASOURCE_NAME = f"{INDEX_NAME}-datasource"
INDEXER_NAME = f"{INDEX_NAME}-indexer"
SEARCH_API_VERSION = "2024-07-01"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_SCOPE = "https://graph.microsoft.com/.default"

# ── Test Files (distinct searchable content) ──────────────────────────────────

TEST_FILES = {
    "e2e-demo/project-alpha-report.txt": (
        "Project Alpha - Quarterly Report Q4 2024\n"
        "===========================================\n\n"
        "Executive Summary:\n"
        "Project Alpha quarterly results exceeded targets with 45% growth.\n"
        "The team delivered the new authentication module on schedule.\n"
        "Customer satisfaction scores improved from 82% to 91%.\n\n"
        "Key Metrics:\n"
        "- Revenue: $3.2M (target: $2.2M)\n"
        "- Active users: 150,000 (+35% QoQ)\n"
        "- Uptime: 99.97%\n"
    ),
    "e2e-demo/hr-onboarding-guide.txt": (
        "Employee Onboarding Guide - 2024 Edition\n"
        "==========================================\n\n"
        "Welcome to the company! This guide covers the onboarding process.\n\n"
        "Step 1: Complete background check and security training\n"
        "Step 2: Set up your development environment\n"
        "Step 3: Review the code of conduct and compliance policies\n"
        "Step 4: Meet your team and attend orientation sessions\n"
        "Step 5: Complete mandatory cybersecurity awareness training\n\n"
        "Important: All employees must complete Steps 1-5 within 30 days.\n"
    ),
    "e2e-demo/finance-budget-2025.txt": (
        "Annual Budget Allocation - FY2025\n"
        "===================================\n\n"
        "Annual budget allocation for Q1 2025 includes $2.5M for infrastructure.\n"
        "Cloud spending is projected at $1.8M across Azure and AWS.\n"
        "Engineering headcount budget: 45 new positions.\n\n"
        "Department Breakdown:\n"
        "- Engineering: $4.2M\n"
        "- Marketing: $1.5M\n"
        "- Operations: $2.1M\n"
        "- Research: $0.8M\n\n"
        "All budget requests must be approved by VP-level or above.\n"
    ),
    "e2e-demo/engineering-architecture.txt": (
        "System Architecture Overview\n"
        "=============================\n\n"
        "Our microservices architecture uses Azure Kubernetes Service with Cosmos DB.\n"
        "The API gateway handles 50,000 requests per second at peak.\n\n"
        "Core Services:\n"
        "- Authentication Service (OAuth 2.0 / OIDC)\n"
        "- Document Processing Pipeline (Azure Functions)\n"
        "- Search Service (Azure AI Search with vector indexing)\n"
        "- Notification Hub (Azure Service Bus + SignalR)\n\n"
        "Data stores: Cosmos DB (operational), Azure SQL (analytics),\n"
        "Azure Blob Storage (documents), Redis Cache (sessions).\n"
    ),
}

# ── Output helpers ────────────────────────────────────────────────────────────


def _banner(phase: int, title: str):
    print(f"\n{'=' * 70}")
    print(f"  Phase {phase}: {title}")
    print(f"{'=' * 70}\n")


def _ok(msg: str):
    print(f"  [OK]   {msg}")


def _info(msg: str):
    print(f"  [..]   {msg}")


def _warn(msg: str):
    print(f"  [!!]   {msg}")


def _fail(msg: str):
    print(f"  [FAIL] {msg}")


# ── Graph API client ─────────────────────────────────────────────────────────


class GraphClient:
    """Thin wrapper around Microsoft Graph API using client credentials."""

    def __init__(self):
        self._token: str | None = None
        self._token_expires: float = 0

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "scope": GRAPH_TOKEN_SCOPE,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires = time.time() + data.get("expires_in", 3600)
            return self._token

    async def _headers(self) -> dict:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get(self, url: str) -> dict:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=headers)
            r.raise_for_status()
            return r.json()

    async def put_content(
        self, url: str, content: bytes, content_type: str = "text/plain"
    ) -> dict:
        headers = await self._headers()
        headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.put(url, headers=headers, content=content)
            r.raise_for_status()
            return r.json()

    async def delete(self, url: str) -> int:
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.delete(url, headers=headers)
            r.raise_for_status()
            return r.status_code

    async def resolve_drive_id(self) -> str:
        """Resolve SharePoint drive ID from site URL and drive name."""
        from urllib.parse import urlparse

        parsed = urlparse(SITE_URL)
        site_data = await self.get(
            f"{GRAPH_BASE}/sites/{parsed.netloc}:{parsed.path}"
        )
        drives_data = await self.get(
            f"{GRAPH_BASE}/sites/{site_data['id']}/drives"
        )
        for drive in drives_data.get("value", []):
            if drive["name"] == DRIVE_NAME:
                return drive["id"]
        raise ValueError(f"Drive '{DRIVE_NAME}' not found")


# ── Search REST helper ────────────────────────────────────────────────────────


def _get_search_api_key() -> str:
    """Get AI Search admin key from env or Azure CLI."""
    if SEARCH_API_KEY:
        return SEARCH_API_KEY
    if SEARCH_RESOURCE_GROUP:
        try:
            result = subprocess.run(
                [
                    "az", "search", "admin-key", "show",
                    "--service-name", SEARCH_SERVICE_NAME,
                    "--resource-group", SEARCH_RESOURCE_GROUP,
                    "--query", "primaryKey", "-o", "tsv",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return ""


def _search_request(method: str, path: str, body: dict | None = None) -> dict:
    """REST call to AI Search service. Returns response dict or {error:..., status:N}."""
    api_key = _get_search_api_key()
    if not api_key:
        raise RuntimeError(
            "No Search API key. Set SEARCH_API_KEY or SEARCH_RESOURCE_GROUP."
        )

    url = f"https://{SEARCH_SERVICE_NAME}.search.windows.net{path}"
    sep = "&" if "?" in url else "?"
    url += f"{sep}api-version={SEARCH_API_VERSION}"

    headers = {"api-key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            err_msg = json.loads(body_text).get("error", {}).get("message", body_text)
        except (json.JSONDecodeError, AttributeError):
            err_msg = f"HTTP {e.code}: {body_text[:300]}"
        return {"error": err_msg, "status": e.code}


# ══════════════════════════════════════════════════════════════════════════════
#  PHASES
# ══════════════════════════════════════════════════════════════════════════════


async def phase0_cleanup():
    """Delete delta tokens so the next sync starts fresh."""
    _banner(0, "Cleanup - reset delta tokens")

    from blob_client import BlobStorageClient
    from config import Config

    config = Config.from_environment()

    async with BlobStorageClient(
        config.blob_account_url, config.container_name, config.blob_prefix
    ) as blob_client:
        # Delete blob-stored delta token
        try:
            await blob_client.delete_blob(
                BlobStorageClient.DELTA_TOKEN_BLOB, dry_run=False
            )
            _ok("Deleted file-sync delta token")
        except Exception:
            _info("No file-sync delta token to delete")

    # Delete local permissions delta tokens
    local_token_dir = os.path.join(
        os.path.dirname(__file__), "..", ".delta_tokens"
    )
    if os.path.isdir(local_token_dir):
        import shutil

        shutil.rmtree(local_token_dir, ignore_errors=True)
        _ok("Deleted local permissions delta tokens")

    _ok("Cleanup complete — next sync will be delta-initial")


async def phase1_upload_files(graph: GraphClient, drive_id: str) -> list[str]:
    """Upload test files to SharePoint via Graph API."""
    _banner(1, "Upload test files to SharePoint")

    uploaded = []
    for rel_path, content in TEST_FILES.items():
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{rel_path}:/content"
        try:
            result = await graph.put_content(url, content.encode("utf-8"))
            _ok(f"Uploaded: {rel_path} ({len(content)} bytes)")
            uploaded.append(rel_path)
        except httpx.HTTPStatusError as e:
            _fail(
                f"Upload failed: {rel_path} — "
                f"{e.response.status_code} {e.response.text[:200]}"
            )

    print(f"\n  Uploaded {len(uploaded)}/{len(TEST_FILES)} files")

    if not uploaded:
        _warn(
            "File upload failed (app may lack write permission). "
            "Upload files manually to SharePoint and re-run from --phase 2."
        )

    return uploaded


async def phase2_initial_sync():
    """Run SharePoint -> Blob sync (delta-initial: syncs all files + saves token)."""
    _banner(2, "Initial sync (SharePoint -> Blob + permissions)")

    from config import Config
    from main import sync_sharepoint_to_blob

    # delta-initial: no FORCE_FULL_SYNC needed, no delta token exists yet
    os.environ["SYNC_PERMISSIONS"] = "true"
    os.environ["DELETE_ORPHANED_BLOBS"] = "true"
    os.environ.pop("FORCE_FULL_SYNC", None)

    config = Config.from_environment()
    config.validate()

    _info("Running sync (delta-initial)...")
    stats = await sync_sharepoint_to_blob(config)

    print(f"\n  --- Sync Results ---")
    print(f"  Mode:             {stats.sync_mode}")
    print(f"  Files scanned:    {stats.files_scanned}")
    print(f"  Files added:      {stats.files_added}")
    print(f"  Files updated:    {stats.files_updated}")
    print(f"  Files unchanged:  {stats.files_unchanged}")
    print(f"  Files deleted:    {stats.files_deleted}")
    print(f"  Files failed:     {stats.files_failed}")
    print(f"  Perms synced:     {stats.permissions_synced}")
    print(f"  Perms unchanged:  {stats.permissions_unchanged}")
    print(f"  Bytes transferred: {stats.bytes_transferred:,}")

    return stats


async def phase3_verify_blobs():
    """Verify blobs in storage have content and ACL metadata."""
    _banner(3, "Verify blob contents and ACL metadata")

    from blob_client import BlobStorageClient
    from config import Config

    config = Config.from_environment()

    async with BlobStorageClient(
        config.blob_account_url, config.container_name, config.blob_prefix
    ) as blob_client:
        total, acl_count = 0, 0
        async for blob in blob_client.list_blobs():
            total += 1
            meta = blob.metadata or {}
            has_acl = "user_ids" in meta or "group_ids" in meta
            if has_acl:
                acl_count += 1

            # Build info string
            parts = [f"{blob.name} ({blob.size:,} bytes)"]
            if has_acl:
                users = meta.get("user_ids", "")
                groups = meta.get("group_ids", "")
                nu = len(
                    [
                        u
                        for u in users.split("|")
                        if u and u != "00000000-0000-0000-0000-000000000000"
                    ]
                )
                ng = len(
                    [
                        g
                        for g in groups.split("|")
                        if g and g != "00000000-0000-0000-0000-000000000001"
                    ]
                )
                parts.append(f"ACL: {nu} users, {ng} groups")
            label = meta.get("purview_label_name", "")
            if label:
                parts.append(f"Label: {label}")

            _ok(" | ".join(parts))

        print(f"\n  Total blobs: {total}")
        print(f"  With ACL metadata: {acl_count}")


async def phase4_modify_and_sync(graph: GraphClient, drive_id: str):
    """Modify a file in SharePoint, then run delta sync to propagate the change."""
    _banner(4, "Modify file -> delta sync")

    target = "e2e-demo/project-alpha-report.txt"
    updated_content = (
        "Project Alpha - Quarterly Report Q4 2024 (UPDATED)\n"
        "=====================================================\n\n"
        "** UPDATE: Board approved expansion to APAC region **\n\n"
        "Executive Summary:\n"
        "Project Alpha quarterly results exceeded targets with 45% growth.\n"
        "Customer satisfaction scores improved from 82% to 94%.\n"
        "APAC launch planned for Q2 2025 with $5M additional budget.\n\n"
        "Key Metrics (Updated):\n"
        "- Revenue: $3.8M (revised from $3.2M after late deals)\n"
        "- Active users: 165,000 (+45% QoQ)\n"
        "- Uptime: 99.99%\n"
        "- NPS: 72 (up from 65)\n"
    )

    _info(f"Updating {target} in SharePoint...")
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{target}:/content"
    try:
        await graph.put_content(url, updated_content.encode("utf-8"))
        _ok(f"Updated: {target} ({len(updated_content)} bytes)")
    except httpx.HTTPStatusError as e:
        _fail(f"Update failed: {e.response.status_code}")
        return

    _info("Waiting 5s for Graph API delta consistency...")
    await asyncio.sleep(5)

    _info("Running delta-incremental sync...")
    from config import Config
    from main import sync_sharepoint_to_blob

    os.environ["SYNC_PERMISSIONS"] = "true"
    os.environ["DELETE_ORPHANED_BLOBS"] = "true"
    os.environ.pop("FORCE_FULL_SYNC", None)

    config = Config.from_environment()
    config.validate()
    stats = await sync_sharepoint_to_blob(config)

    print(f"\n  --- Delta Sync (Modify) ---")
    print(f"  Mode:      {stats.sync_mode}")
    print(f"  Scanned:   {stats.files_scanned}")
    print(f"  Added:     {stats.files_added}")
    print(f"  Updated:   {stats.files_updated}")
    print(f"  Unchanged: {stats.files_unchanged}")

    # Verify the modified blob
    from blob_client import BlobStorageClient

    async with BlobStorageClient(
        config.blob_account_url, config.container_name, config.blob_prefix
    ) as blob_client:
        blob_name = blob_client._get_blob_name(target)
        blob_info = await blob_client.get_blob_metadata(blob_name)
        if blob_info:
            _ok(f"Verified blob: {blob_name} ({blob_info.size:,} bytes)")
        else:
            _warn(f"Blob not found after update: {blob_name}")


async def phase5_delete_and_sync(graph: GraphClient, drive_id: str):
    """Delete a file from SharePoint, then run full sync to propagate via orphan cleanup."""
    _banner(5, "Delete file -> full sync (orphan cleanup)")

    target = "e2e-demo/finance-budget-2025.txt"

    _info(f"Deleting {target} from SharePoint...")
    try:
        item = await graph.get(
            f"{GRAPH_BASE}/drives/{drive_id}/root:/{target}"
        )
        item_id = item["id"]
        status = await graph.delete(
            f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
        )
        _ok(f"Deleted: {target} (HTTP {status})")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            _warn(f"File already absent: {target}")
        else:
            _fail(f"Delete failed: {e.response.status_code}")
            return

    _info("Waiting 5s for SharePoint consistency...")
    await asyncio.sleep(5)

    # Use full sync with orphan cleanup — this is more reliable than delta for
    # deletions because Graph delta API often omits path info for deleted items.
    _info("Running full sync with orphan cleanup (FORCE_FULL_SYNC)...")
    from config import Config
    from main import sync_sharepoint_to_blob

    os.environ["FORCE_FULL_SYNC"] = "true"
    os.environ["SYNC_PERMISSIONS"] = "true"
    os.environ["DELETE_ORPHANED_BLOBS"] = "true"

    config = Config.from_environment()
    config.validate()
    stats = await sync_sharepoint_to_blob(config)

    # Restore delta mode for any subsequent runs
    os.environ.pop("FORCE_FULL_SYNC", None)

    print(f"\n  --- Full Sync (Delete via orphan cleanup) ---")
    print(f"  Mode:      {stats.sync_mode}")
    print(f"  Scanned:   {stats.files_scanned}")
    print(f"  Deleted:   {stats.files_deleted}")
    print(f"  Unchanged: {stats.files_unchanged}")

    # Verify blob was removed
    from blob_client import BlobStorageClient

    async with BlobStorageClient(
        config.blob_account_url, config.container_name, config.blob_prefix
    ) as blob_client:
        blob_name = blob_client._get_blob_name(target)
        blob_info = await blob_client.get_blob_metadata(blob_name)
        if blob_info is None:
            _ok(f"Confirmed blob deleted: {blob_name}")
        else:
            _warn(f"Blob still present (SharePoint may need more time): {blob_name}")


def phase6_deploy_search() -> bool:
    """Deploy AI Search data source, index, and indexer via REST API."""
    _banner(6, "Deploy AI Search (data source + index + indexer)")

    if not SEARCH_SERVICE_NAME:
        _warn("SEARCH_SERVICE_NAME not set — skipping AI Search deployment")
        return False

    api_key = _get_search_api_key()
    if not api_key:
        _warn("No Search API key — skipping (set SEARCH_API_KEY or SEARCH_RESOURCE_GROUP)")
        return False

    _info(f"Service: {SEARCH_SERVICE_NAME}  Index: {INDEX_NAME}")

    # ── Resolve storage connection info ──
    subscription_id = _az_query("az account show --query id -o tsv")
    storage_rg = _az_query(
        f"az storage account show --name {STORAGE_ACCOUNT} --query resourceGroup -o tsv"
    ) or SEARCH_RESOURCE_GROUP

    # ── Data Source ──
    _info("Creating data source...")
    ds_def = {
        "name": DATASOURCE_NAME,
        "type": "azureblob",
        "credentials": {
            "connectionString": (
                f"ResourceId=/subscriptions/{subscription_id}"
                f"/resourceGroups/{storage_rg}"
                f"/providers/Microsoft.Storage/storageAccounts/{STORAGE_ACCOUNT};"
            )
        },
        "container": {"name": CONTAINER_NAME},
        "dataDeletionDetectionPolicy": {
            "@odata.type": "#Microsoft.Azure.Search.SoftDeleteColumnDeletionDetectionPolicy",
            "softDeleteColumnName": "IsDeleted",
            "softDeleteMarkerValue": "true",
        },
    }

    result = _search_request("PUT", f"/datasources/{DATASOURCE_NAME}", ds_def)
    if "error" in result:
        _warn(f"Managed-identity data source failed: {result['error']}")
        _info("Retrying with storage account key...")
        key = _az_query(
            f"az storage account keys list --account-name {STORAGE_ACCOUNT} "
            f"--query [0].value -o tsv"
        )
        if not key:
            _fail("Cannot retrieve storage key via Azure CLI")
            return False
        ds_def["credentials"]["connectionString"] = (
            f"DefaultEndpointsProtocol=https;AccountName={STORAGE_ACCOUNT};"
            f"AccountKey={key};EndpointSuffix=core.windows.net"
        )
        result = _search_request("PUT", f"/datasources/{DATASOURCE_NAME}", ds_def)
        if "error" in result:
            _fail(f"Data source: {result['error']}")
            return False
    _ok("Data source created")

    # ── Index ──
    _info("Creating index...")
    index_def = {
        "name": INDEX_NAME,
        "fields": [
            {
                "name": "id",
                "type": "Edm.String",
                "key": True,
                "filterable": True,
                "searchable": False,
                "retrievable": True,
            },
            {
                "name": "content",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "title",
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "acl_user_ids",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "acl_group_ids",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "purview_label_name",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            },
            {
                "name": "purview_is_encrypted",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "purview_protection_status",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
        ],
        "semantic": {
            "configurations": [
                {
                    "name": f"{INDEX_NAME}-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [],
                    },
                }
            ]
        },
    }

    result = _search_request("PUT", f"/indexes/{INDEX_NAME}", index_def)
    if "error" in result:
        _fail(f"Index: {result['error']}")
        return False
    _ok("Index created")

    # ── Indexer ──
    _info("Creating indexer...")
    indexer_def = {
        "name": INDEXER_NAME,
        "dataSourceName": DATASOURCE_NAME,
        "targetIndexName": INDEX_NAME,
        "parameters": {
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
            }
        },
        "fieldMappings": [
            {
                "sourceFieldName": "metadata_storage_path",
                "targetFieldName": "id",
                "mappingFunction": {"name": "base64Encode"},
            },
            {
                "sourceFieldName": "metadata_storage_name",
                "targetFieldName": "title",
            },
            {
                "sourceFieldName": "metadata_storage_metadata_user_ids",
                "targetFieldName": "acl_user_ids",
            },
            {
                "sourceFieldName": "metadata_storage_metadata_group_ids",
                "targetFieldName": "acl_group_ids",
            },
            {
                "sourceFieldName": "metadata_storage_metadata_purview_label_name",
                "targetFieldName": "purview_label_name",
            },
            {
                "sourceFieldName": "metadata_storage_metadata_purview_is_encrypted",
                "targetFieldName": "purview_is_encrypted",
            },
            {
                "sourceFieldName": "metadata_storage_metadata_purview_protection_status",
                "targetFieldName": "purview_protection_status",
            },
        ],
    }

    result = _search_request("PUT", f"/indexers/{INDEXER_NAME}", indexer_def)
    if "error" in result:
        _fail(f"Indexer: {result['error']}")
        return False
    _ok("Indexer created")

    return True


def phase7_query_search():
    """Run the indexer, wait for completion, then query with ACL filtering."""
    _banner(7, "Run indexer & query with ACL filtering")

    if not SEARCH_SERVICE_NAME or not _get_search_api_key():
        _warn("No Search configuration — skipping")
        return

    # Run the indexer
    _info("Running indexer...")
    result = _search_request("POST", f"/indexers/{INDEXER_NAME}/run")
    if isinstance(result, dict) and "error" in result and result.get("status") != 409:
        _fail(f"Run indexer: {result['error']}")
    else:
        _ok("Indexer triggered")

    # Poll for completion (up to ~2.5 minutes)
    _info("Waiting for indexer to complete...")
    for _ in range(30):
        time.sleep(5)
        status = _search_request("GET", f"/indexers/{INDEXER_NAME}/status")
        last = status.get("lastResult") or {}
        exec_status = last.get("status", "unknown")

        if exec_status in ("success", "transientFailure"):
            processed = last.get("itemsProcessed", 0)
            failed = last.get("itemsFailed", 0)
            _ok(f"Indexer done: {processed} processed, {failed} failed")
            for err in (last.get("errors") or [])[:3]:
                _warn(f"  Error: {err.get('errorMessage', '')[:120]}")
            break
        elif exec_status == "inProgress":
            pass  # keep waiting
    else:
        _warn("Indexer did not complete within timeout — continuing anyway")

    # Give the index a moment to refresh
    time.sleep(2)

    # Index stats
    idx_stats = _search_request("GET", f"/indexes/{INDEX_NAME}/stats")
    _info(f"Index '{INDEX_NAME}' has {idx_stats.get('documentCount', '?')} documents")

    # ── Query 1: Full-text search ──
    print()
    _info("Query 1: 'authentication module'")
    _run_search_query(
        {"search": "authentication module", "top": 5, "count": True}
    )

    # ── Query 2: Budget search ──
    _info("Query 2: 'budget allocation'")
    _run_search_query(
        {"search": "budget allocation", "top": 5, "count": True}
    )

    # ── Query 3: ACL-filtered search ──
    # Find a real user ID from the index to demonstrate filtering
    all_docs = _search_request(
        "POST",
        f"/indexes/{INDEX_NAME}/docs/search",
        {"search": "*", "select": "title,acl_user_ids", "top": 1},
    )
    sample_uid = ""
    for doc in all_docs.get("value", []):
        uid_str = doc.get("acl_user_ids", "")
        if uid_str and uid_str != "00000000-0000-0000-0000-000000000000":
            sample_uid = uid_str.split("|")[0]
            break

    if sample_uid:
        _info(f"Query 3: ACL filter — user {sample_uid[:12]}...")
        _run_search_query(
            {
                "search": "*",
                "filter": f"search.ismatch('{sample_uid}', 'acl_user_ids')",
                "top": 10,
                "count": True,
            }
        )
    else:
        _info("Query 3: Skipped — no user IDs in index")


def _run_search_query(body: dict):
    """Execute a search query and print results."""
    body.setdefault("select", "title,acl_user_ids,acl_group_ids,purview_label_name")
    results = _search_request(
        "POST", f"/indexes/{INDEX_NAME}/docs/search", body
    )
    if "error" in results:
        _fail(f"Search error: {results['error']}")
        return

    count = results.get("@odata.count", len(results.get("value", [])))
    print(f"    Matches: {count}")
    for doc in results.get("value", []):
        title = doc.get("title", "?")
        score = doc.get("@search.score", 0)
        parts = [f"{title} (score={score:.2f})"]

        uid_str = doc.get("acl_user_ids", "")
        if uid_str:
            real = [
                u for u in uid_str.split("|")
                if u and not u.startswith("00000000-0000-0000-0000")
            ]
            parts.append(f"users={len(real)}")

        gid_str = doc.get("acl_group_ids", "")
        if gid_str:
            real = [
                g for g in gid_str.split("|")
                if g and not g.startswith("00000000-0000-0000-0000")
            ]
            parts.append(f"groups={len(real)}")

        label = doc.get("purview_label_name", "")
        if label:
            parts.append(f"label={label}")

        print(f"    - {' | '.join(parts)}")
    print()


def _az_query(cmd: str) -> str:
    """Run an az CLI command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            cmd.split(), capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════


async def run_demo(skip_search: bool = False, phase: int | None = None):
    """Run the E2E demo (all phases or a single phase)."""
    print("\n" + "=" * 70)
    print("  SharePoint -> Blob -> AI Search : End-to-End Demo")
    print("=" * 70)
    print(f"  SharePoint:  {SITE_URL}")
    print(f"  Storage:     {STORAGE_ACCOUNT}/{CONTAINER_NAME}")
    print(f"  AI Search:   {SEARCH_SERVICE_NAME or '(not configured)'}")
    print(f"  Timestamp:   {datetime.now().isoformat()}")
    print("=" * 70)

    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, SITE_URL, STORAGE_ACCOUNT]):
        _fail("Missing required env vars — check .env")
        return

    graph = GraphClient()

    _info("Resolving SharePoint drive ID...")
    drive_id = await graph.resolve_drive_id()
    _ok(f"Drive ID: {drive_id[:16]}...")

    phases_to_run = [phase] if phase is not None else list(range(8))

    if 0 in phases_to_run:
        await phase0_cleanup()

    if 1 in phases_to_run:
        uploaded = await phase1_upload_files(graph, drive_id)
        if not uploaded and phase is None:
            _warn("Continuing with existing SharePoint files...")
        if uploaded:
            _info("Waiting 5s for SharePoint to index new files...")
            await asyncio.sleep(5)

    if 2 in phases_to_run:
        await phase2_initial_sync()

    if 3 in phases_to_run:
        await phase3_verify_blobs()

    if 4 in phases_to_run:
        await phase4_modify_and_sync(graph, drive_id)

    if 5 in phases_to_run:
        await phase5_delete_and_sync(graph, drive_id)

    if not skip_search:
        if 6 in phases_to_run:
            search_ok = phase6_deploy_search()
            if 7 in phases_to_run and search_ok:
                phase7_query_search()
    else:
        _info("AI Search phases skipped (--skip-search)")

    # Summary
    print("\n" + "=" * 70)
    print("  Demo Complete!")
    print("=" * 70)
    print("  Pipeline demonstrated:")
    print("    1. File upload to SharePoint via Graph API")
    print("    2. Full sync to Blob Storage with permissions")
    print("    3. ACL metadata verification (user_ids, group_ids)")
    print("    4. File modification -> delta sync propagation")
    print("    5. File deletion -> delta sync + orphan cleanup")
    if not skip_search and SEARCH_SERVICE_NAME:
        print("    6. AI Search index with ACL field mappings")
        print("    7. Security-trimmed search queries")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="E2E SharePoint Sync Demo")
    parser.add_argument(
        "--phase", type=int, choices=range(8), help="Run a single phase (0-7)"
    )
    parser.add_argument(
        "--skip-search", action="store_true", help="Skip AI Search phases (6-7)"
    )
    args = parser.parse_args()

    asyncio.run(run_demo(skip_search=args.skip_search, phase=args.phase))


if __name__ == "__main__":
    main()
