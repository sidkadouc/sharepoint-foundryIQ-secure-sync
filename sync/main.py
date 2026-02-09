"""
SharePoint to Azure Blob Storage synchronization job.
Syncs files from a SharePoint document library to Azure Blob Storage.

Supports two modes:
1. Delta (incremental) sync — uses Microsoft Graph delta API to process only
   changed files since the last run. The delta token is persisted in blob storage.
2. Full sync — falls back to a full recursive listing when no delta token exists
   (first run) or when explicitly requested via FORCE_FULL_SYNC=true.

Permissions are synced only for files that changed (delta-aware).
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, Set, List
# Load environment variables from .env file for local development
from dotenv import load_dotenv
load_dotenv()
import structlog

from config import Config
from sharepoint_client import SharePointClient, SharePointFile, DeltaChangeType
from blob_client import BlobStorageClient, BlobFile
from permissions_sync import (
    PermissionsClient, 
    is_permissions_sync_enabled, 
    permissions_to_summary
)

# Configure standard logging to output to console
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

# Add a handler for structlog output
formatter = structlog.stdlib.ProcessorFormatter(
    processor=structlog.dev.ConsoleRenderer()  # Use console renderer for readable output
)

handler = logging.StreamHandler()
handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.handlers = [handler]
root_logger.setLevel(logging.INFO)

logger = structlog.get_logger(__name__)


@dataclass
class SyncStats:
    """Statistics for the sync operation."""
    files_scanned: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    bytes_transferred: int = 0
    permissions_synced: int = 0
    permissions_failed: int = 0
    sync_mode: str = "full"  # "full" or "delta"


def _force_full_sync() -> bool:
    """Check if a full sync is explicitly requested via env var."""
    return os.environ.get("FORCE_FULL_SYNC", "false").lower() == "true"


async def _sync_permissions_for_files(
    sp_client: SharePointClient,
    blob_client: BlobStorageClient,
    drive_id: str,
    files: List[SharePointFile],
    stats: SyncStats,
    dry_run: bool,
) -> None:
    """
    Sync SharePoint permissions to blob metadata for a list of files.

    This is delta-aware: it only fetches permissions for the files that
    actually changed (instead of re-scanning the entire library).
    """
    if not files:
        return

    await logger.ainfo("Syncing permissions for changed files",
                      file_count=len(files))

    async with PermissionsClient(drive_id) as perm_client:
        for sp_file in files:
            blob_name = blob_client._get_blob_name(sp_file.path)
            try:
                file_permissions = await perm_client.get_file_permissions(
                    file_id=sp_file.id,
                    file_path=sp_file.path,
                )
                if file_permissions.permissions:
                    perm_metadata = file_permissions.to_metadata()
                    await logger.ainfo("Syncing permissions",
                        file_path=sp_file.path,
                        permission_count=len(file_permissions.permissions),
                        summary=permissions_to_summary(file_permissions.permissions),
                    )
                    await blob_client.update_blob_metadata(
                        blob_name=blob_name,
                        additional_metadata=perm_metadata,
                        dry_run=dry_run,
                    )
                    stats.permissions_synced += 1
                else:
                    await logger.adebug("No permissions to sync",
                                       file_path=sp_file.path)
            except Exception as e:
                await logger.aerror("Failed to sync permissions",
                    file_path=sp_file.path,
                    error=str(e),
                )
                stats.permissions_failed += 1


async def sync_sharepoint_to_blob(config: Config) -> SyncStats:
    """
    Synchronize files from SharePoint to Azure Blob Storage.

    Uses delta queries when a delta token is available (incremental sync).
    Falls back to a full listing on the first run or when FORCE_FULL_SYNC=true.

    Permissions are synced only for files that were created/modified during this
    sync cycle — not the entire library.

    Args:
        config: The sync configuration

    Returns:
        SyncStats with the operation results
    """
    stats = SyncStats()
    sync_permissions = is_permissions_sync_enabled()
    force_full = _force_full_sync()

    await logger.ainfo("Starting SharePoint to Blob sync",
        site_url=config.sharepoint_site_url,
        drive_name=config.sharepoint_drive_name,
        folder_path=config.sharepoint_folder_path,
        storage_account=config.storage_account_name,
        container=config.container_name,
        dry_run=config.dry_run,
        sync_permissions=sync_permissions,
        force_full_sync=force_full,
    )

    async with SharePointClient(config.sharepoint_site_url, config.sharepoint_drive_name) as sp_client:
        site_id, drive_id = sp_client.get_resolved_ids()
        await logger.ainfo("Resolved SharePoint IDs", site_id=site_id, drive_id=drive_id)

        async with BlobStorageClient(
            config.blob_account_url,
            config.container_name,
            config.blob_prefix,
        ) as blob_client:

            # ---- Try delta (incremental) sync ---- #
            delta_link: str | None = None
            if not force_full:
                delta_link = await blob_client.load_delta_token()

            if delta_link or not force_full:
                # Use delta API (delta_link=None → initial delta crawl,
                # delta_link=<url> → incremental delta)
                stats.sync_mode = "delta-initial" if delta_link is None else "delta-incremental"
                await logger.ainfo("Using delta sync", mode=stats.sync_mode)

                delta_result = await sp_client.get_delta(delta_link=delta_link)

                # Track files that were uploaded/updated so we can sync their permissions
                changed_files: List[SharePointFile] = []

                for change in delta_result.changes:
                    stats.files_scanned += 1

                    if change.change_type == DeltaChangeType.DELETED:
                        # File deleted in SharePoint → delete blob
                        blob_name = blob_client._get_blob_name(change.item_path)
                        await logger.ainfo("Delta: file deleted",
                            item_id=change.item_id,
                            path=change.item_path,
                        )
                        if config.delete_orphaned_blobs:
                            try:
                                await blob_client.delete_blob(blob_name, dry_run=config.dry_run)
                                stats.files_deleted += 1
                            except Exception as e:
                                await logger.aerror("Failed to delete blob",
                                    blob_name=blob_name, error=str(e))
                                stats.files_failed += 1

                    elif change.change_type == DeltaChangeType.CREATED_OR_MODIFIED and change.file:
                        sp_file = change.file
                        blob_name = blob_client._get_blob_name(sp_file.path)
                        try:
                            await logger.ainfo("Delta: file created/modified",
                                path=sp_file.path,
                                size=sp_file.size,
                            )
                            content = await sp_client.download_file(sp_file.id)
                            await blob_client.upload_blob(
                                sharepoint_path=sp_file.path,
                                content=content,
                                sharepoint_item_id=sp_file.id,
                                sharepoint_last_modified=sp_file.last_modified,
                                sharepoint_content_hash=sp_file.content_hash,
                                dry_run=config.dry_run,
                            )
                            stats.files_added += 1
                            stats.bytes_transferred += len(content)
                            changed_files.append(sp_file)
                        except Exception as e:
                            await logger.aerror("Failed to process file",
                                sharepoint_path=sp_file.path,
                                error=str(e),
                            )
                            stats.files_failed += 1

                # Persist the new delta token for next run
                if delta_result.delta_token:
                    await blob_client.save_delta_token(
                        delta_result.delta_token, dry_run=config.dry_run)

                # Permissions: always do a full scan when enabled.
                # The Graph delta API CAN detect permission changes via the
                # Prefer: deltashowsharingchanges header, but this requires
                # Sites.FullControl.All — we only use Sites.Read.All, so
                # delta-based permission tracking is not available to us.
                # Full re-scan is the correct approach at this permission level.
                # See: https://learn.microsoft.com/en-us/graph/api/driveitem-delta#scanning-permissions-hierarchies
                if sync_permissions:
                    await logger.ainfo(
                        "Syncing permissions for ALL files "
                        "(permission changes are invisible to delta API)...")
                    all_files_for_perms: List[SharePointFile] = []
                    async for sp_file in sp_client.list_files(config.sharepoint_folder_path):
                        all_files_for_perms.append(sp_file)
                    await _sync_permissions_for_files(
                        sp_client, blob_client, drive_id,
                        all_files_for_perms, stats, config.dry_run)

            else:
                # ---- Full sync (fallback) ---- #
                stats.sync_mode = "full"
                await logger.ainfo("Using full sync (FORCE_FULL_SYNC=true)")

                existing_blobs: Dict[str, BlobFile] = {}
                async for blob in blob_client.list_blobs():
                    existing_blobs[blob.name] = blob
                await logger.ainfo("Found existing blobs", count=len(existing_blobs))

                seen_blob_names: Set[str] = set()
                all_files: List[SharePointFile] = []

                async for sp_file in sp_client.list_files(config.sharepoint_folder_path):
                    stats.files_scanned += 1
                    blob_name = blob_client._get_blob_name(sp_file.path)
                    seen_blob_names.add(blob_name)

                    try:
                        existing_blob = existing_blobs.get(blob_name)
                        if existing_blob is None:
                            await logger.ainfo("New file detected",
                                sharepoint_path=sp_file.path, size=sp_file.size)
                            content = await sp_client.download_file(sp_file.id)
                            await blob_client.upload_blob(
                                sharepoint_path=sp_file.path,
                                content=content,
                                sharepoint_item_id=sp_file.id,
                                sharepoint_last_modified=sp_file.last_modified,
                                sharepoint_content_hash=sp_file.content_hash,
                                dry_run=config.dry_run,
                            )
                            stats.files_added += 1
                            stats.bytes_transferred += len(content)
                            all_files.append(sp_file)
                        elif blob_client.should_update(existing_blob, sp_file.last_modified, sp_file.content_hash):
                            await logger.ainfo("Modified file detected",
                                sharepoint_path=sp_file.path, size=sp_file.size)
                            content = await sp_client.download_file(sp_file.id)
                            await blob_client.upload_blob(
                                sharepoint_path=sp_file.path,
                                content=content,
                                sharepoint_item_id=sp_file.id,
                                sharepoint_last_modified=sp_file.last_modified,
                                sharepoint_content_hash=sp_file.content_hash,
                                dry_run=config.dry_run,
                            )
                            stats.files_updated += 1
                            stats.bytes_transferred += len(content)
                            all_files.append(sp_file)
                        else:
                            stats.files_unchanged += 1
                            all_files.append(sp_file)
                    except Exception as e:
                        await logger.aerror("Failed to process file",
                            sharepoint_path=sp_file.path, error=str(e))
                        stats.files_failed += 1

                # Orphan cleanup
                if config.delete_orphaned_blobs:
                    for blob_name in existing_blobs:
                        if blob_name not in seen_blob_names:
                            try:
                                await blob_client.delete_blob(blob_name, dry_run=config.dry_run)
                                stats.files_deleted += 1
                            except Exception as e:
                                await logger.aerror("Failed to delete orphaned blob",
                                    blob_name=blob_name, error=str(e))
                                stats.files_failed += 1

                # Sync permissions for ALL files in full-sync mode
                if sync_permissions:
                    await _sync_permissions_for_files(
                        sp_client, blob_client, drive_id,
                        all_files, stats, config.dry_run)

    return stats


async def main() -> int:
    """Main entry point for the sync job."""
    try:
        # Load and validate configuration
        config = Config.from_environment()
        config.validate()
        
        # Run the sync
        stats = await sync_sharepoint_to_blob(config)
        
        # Log final statistics
        await logger.ainfo("Sync completed",
            sync_mode=stats.sync_mode,
            files_scanned=stats.files_scanned,
            files_added=stats.files_added,
            files_updated=stats.files_updated,
            files_deleted=stats.files_deleted,
            files_unchanged=stats.files_unchanged,
            files_failed=stats.files_failed,
            bytes_transferred=stats.bytes_transferred,
            permissions_synced=stats.permissions_synced,
            permissions_failed=stats.permissions_failed
        )
        
        # Return non-zero exit code if there were failures
        if stats.files_failed > 0 or stats.permissions_failed > 0:
            await logger.awarning("Sync completed with failures", 
                                 files_failed=stats.files_failed,
                                 permissions_failed=stats.permissions_failed)
            return 1
        
        return 0
    
    except ValueError as e:
        await logger.aerror("Configuration error", error=str(e))
        return 2
    
    except Exception as e:
        await logger.aerror("Unexpected error during sync", error=str(e), exc_info=True)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
