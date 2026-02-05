"""
SharePoint to Azure Blob Storage synchronization job.
Syncs files from a SharePoint document library to Azure Blob Storage.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Dict, Set
# Load environment variables from .env file for local development
from dotenv import load_dotenv
load_dotenv()
import structlog

from config import Config, PermissionsDeltaMode
from sharepoint_client import (
    SharePointClient, 
    SharePointFile, 
    GraphDeltaFilesClient,
    DeltaTokenStorage,
    FileChangeType,
)
from blob_client import BlobStorageClient, BlobFile
from permissions_sync import (
    PermissionsClient, 
    is_permissions_sync_enabled, 
    permissions_to_summary,
    should_sync_permissions,
    GraphDeltaPermissionsClient,
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
    permissions_unchanged: int = 0  # Permissions skipped due to no changes (delta)
    permissions_failed: int = 0


async def sync_sharepoint_to_blob(config: Config) -> SyncStats:
    """
    Synchronize files from SharePoint to Azure Blob Storage.
    
    Args:
        config: The sync configuration
        
    Returns:
        SyncStats with the operation results
    """
    stats = SyncStats()
    sync_permissions = is_permissions_sync_enabled()
    
    await logger.ainfo("Starting SharePoint to Blob sync",
        site_url=config.sharepoint_site_url,
        drive_name=config.sharepoint_drive_name,
        folder_path=config.sharepoint_folder_path,
        storage_account=config.storage_account_name,
        container=config.container_name,
        dry_run=config.dry_run,
        sync_permissions=sync_permissions,
        delta_mode=config.permissions_delta_mode.value
    )
    
    async with SharePointClient(config.sharepoint_site_url, config.sharepoint_drive_name) as sp_client:
        # Log resolved IDs for debugging
        site_id, drive_id = sp_client.get_resolved_ids()
        await logger.ainfo("Resolved SharePoint IDs", site_id=site_id, drive_id=drive_id)
        
        async with BlobStorageClient(
            config.blob_account_url,
            config.container_name,
            config.blob_prefix
        ) as blob_client:
            
            # Step 1: Get all existing blobs
            await logger.ainfo("Loading existing blobs from storage...")
            existing_blobs: Dict[str, BlobFile] = {}
            async for blob in blob_client.list_blobs():
                existing_blobs[blob.name] = blob
            
            await logger.ainfo("Found existing blobs", count=len(existing_blobs))
            
            # Step 2: Track which blobs we've seen (for orphan detection)
            seen_blob_names: Set[str] = set()
            
            # Step 3: Process SharePoint files (using selected mode)
            if config.permissions_delta_mode == PermissionsDeltaMode.GRAPH_DELTA:
                # Use Graph delta API for file sync
                await _sync_files_graph_delta(
                    config, drive_id, sp_client, blob_client, 
                    existing_blobs, seen_blob_names, stats
                )
            else:
                # Use traditional full-scan mode (default)
                await _sync_files_full_scan(
                    config, sp_client, blob_client,
                    existing_blobs, seen_blob_names, stats
                )
            
            # Step 4: Handle orphaned blobs (files deleted from SharePoint)
            # Note: In graph_delta mode, deletions are handled within the delta sync
            if config.delete_orphaned_blobs and config.permissions_delta_mode != PermissionsDeltaMode.GRAPH_DELTA:
                await logger.ainfo("Checking for orphaned blobs...")
                
                for blob_name, blob in existing_blobs.items():
                    if blob_name not in seen_blob_names:
                        await logger.ainfo("Orphaned blob detected (deleted from SharePoint)",
                            blob_name=blob_name
                        )
                        
                        try:
                            await blob_client.delete_blob(blob_name, dry_run=config.dry_run)
                            stats.files_deleted += 1
                        except Exception as e:
                            await logger.aerror("Failed to delete orphaned blob",
                                blob_name=blob_name,
                                error=str(e)
                            )
                            stats.files_failed += 1
            
            # Step 5: Sync permissions (if enabled)
            if sync_permissions:
                if config.permissions_delta_mode == PermissionsDeltaMode.GRAPH_DELTA:
                    # Use Microsoft Graph delta API with deltashowsharingchanges
                    await _sync_permissions_graph_delta(
                        config, drive_id, sp_client, blob_client, existing_blobs, stats
                    )
                else:
                    # Use hash-based delta detection (default)
                    await _sync_permissions_hash_mode(
                        config, drive_id, sp_client, blob_client, existing_blobs, stats
                    )
    
    return stats


async def _sync_files_full_scan(
    config: Config,
    sp_client: SharePointClient,
    blob_client: BlobStorageClient,
    existing_blobs: Dict[str, BlobFile],
    seen_blob_names: Set[str],
    stats: SyncStats
) -> None:
    """
    Sync files using traditional full-scan mode.
    
    Scans all files in SharePoint and compares with existing blobs to detect
    new, modified, and unchanged files. This is the default mode.
    """
    await logger.ainfo(
        "Syncing SharePoint files using FULL SCAN mode",
        mode="full_scan"
    )
    
    async for sp_file in sp_client.list_files(config.sharepoint_folder_path):
        stats.files_scanned += 1
        
        blob_name = blob_client._get_blob_name(sp_file.path)
        seen_blob_names.add(blob_name)
        
        try:
            existing_blob = existing_blobs.get(blob_name)
            
            if existing_blob is None:
                # New file - upload it
                await logger.ainfo("New file detected", 
                    sharepoint_path=sp_file.path,
                    size=sp_file.size
                )
                
                content = await sp_client.download_file(sp_file.id)
                await blob_client.upload_blob(
                    sharepoint_path=sp_file.path,
                    content=content,
                    sharepoint_item_id=sp_file.id,
                    sharepoint_last_modified=sp_file.last_modified,
                    sharepoint_content_hash=sp_file.content_hash,
                    dry_run=config.dry_run
                )
                
                stats.files_added += 1
                stats.bytes_transferred += len(content)
            
            elif blob_client.should_update(existing_blob, sp_file.last_modified, sp_file.content_hash):
                # File has been modified - update it
                await logger.ainfo("Modified file detected",
                    sharepoint_path=sp_file.path,
                    size=sp_file.size,
                    sp_modified=sp_file.last_modified.isoformat() if sp_file.last_modified else None
                )
                
                content = await sp_client.download_file(sp_file.id)
                await blob_client.upload_blob(
                    sharepoint_path=sp_file.path,
                    content=content,
                    sharepoint_item_id=sp_file.id,
                    sharepoint_last_modified=sp_file.last_modified,
                    sharepoint_content_hash=sp_file.content_hash,
                    dry_run=config.dry_run
                )
                
                stats.files_updated += 1
                stats.bytes_transferred += len(content)
            
            else:
                # File unchanged
                await logger.adebug("File unchanged", sharepoint_path=sp_file.path)
                stats.files_unchanged += 1
        
        except Exception as e:
            await logger.aerror("Failed to process file",
                sharepoint_path=sp_file.path,
                error=str(e)
            )
            stats.files_failed += 1


async def _sync_files_graph_delta(
    config: Config,
    drive_id: str,
    sp_client: SharePointClient,
    blob_client: BlobStorageClient,
    existing_blobs: Dict[str, BlobFile],
    seen_blob_names: Set[str],
    stats: SyncStats
) -> None:
    """
    Sync files using Microsoft Graph delta API.
    
    Uses the Graph delta API to detect changed files:
    - First run (no token): Returns all files, establishes baseline
    - Subsequent runs (with token): Returns only changed/deleted files
    
    This approach is more efficient for large document libraries as it only
    processes files that have changed since the last sync.
    
    Note: Blob metadata format remains the same as full-scan mode for compatibility.
    """
    await logger.ainfo(
        "Syncing SharePoint files using GRAPH DELTA API",
        mode="graph_delta",
        token_storage_path=config.delta_token_storage_path
    )
    
    token_storage = DeltaTokenStorage(config.delta_token_storage_path)
    
    async with GraphDeltaFilesClient(drive_id, token_storage) as delta_client:
        async for sp_file in delta_client.get_changed_files(config.sharepoint_folder_path):
            stats.files_scanned += 1
            
            blob_name = blob_client._get_blob_name(sp_file.path)
            seen_blob_names.add(blob_name)
            
            try:
                if sp_file.change_type == FileChangeType.DELETED:
                    # File was deleted in SharePoint
                    if config.delete_orphaned_blobs:
                        existing_blob = existing_blobs.get(blob_name)
                        if existing_blob:
                            await logger.ainfo("Deleted file detected via delta",
                                sharepoint_path=sp_file.path,
                                blob_name=blob_name
                            )
                            await blob_client.delete_blob(blob_name, dry_run=config.dry_run)
                            stats.files_deleted += 1
                    continue
                
                elif sp_file.change_type == FileChangeType.ADDED:
                    # New file
                    await logger.ainfo("New file detected via delta",
                        sharepoint_path=sp_file.path,
                        size=sp_file.size
                    )
                    
                    content = await delta_client.download_file(sp_file.id)
                    await blob_client.upload_blob(
                        sharepoint_path=sp_file.path,
                        content=content,
                        sharepoint_item_id=sp_file.id,
                        sharepoint_last_modified=sp_file.last_modified,
                        sharepoint_content_hash=sp_file.content_hash,
                        dry_run=config.dry_run
                    )
                    
                    stats.files_added += 1
                    stats.bytes_transferred += len(content)
                
                elif sp_file.change_type == FileChangeType.MODIFIED:
                    # Modified file
                    await logger.ainfo("Modified file detected via delta",
                        sharepoint_path=sp_file.path,
                        size=sp_file.size
                    )
                    
                    content = await delta_client.download_file(sp_file.id)
                    await blob_client.upload_blob(
                        sharepoint_path=sp_file.path,
                        content=content,
                        sharepoint_item_id=sp_file.id,
                        sharepoint_last_modified=sp_file.last_modified,
                        sharepoint_content_hash=sp_file.content_hash,
                        dry_run=config.dry_run
                    )
                    
                    stats.files_updated += 1
                    stats.bytes_transferred += len(content)
            
            except Exception as e:
                await logger.aerror("Failed to process file",
                    sharepoint_path=sp_file.path,
                    error=str(e)
                )
                stats.files_failed += 1


async def _sync_permissions_hash_mode(
    config: Config,
    drive_id: str,
    sp_client: SharePointClient,
    blob_client: BlobStorageClient,
    existing_blobs: Dict[str, BlobFile],
    stats: SyncStats
) -> None:
    """
    Sync permissions using hash-based delta detection.
    
    Computes a hash of permissions and only syncs if the hash has changed.
    This is the default mode and works well for most scenarios.
    """
    await logger.ainfo(
        "Syncing SharePoint permissions using HASH-based delta detection",
        mode="hash"
    )
    
    async with PermissionsClient(drive_id) as perm_client:
        # Re-scan files to get their permissions
        async for sp_file in sp_client.list_files(config.sharepoint_folder_path):
            blob_name = blob_client._get_blob_name(sp_file.path)
            
            try:
                # Get permissions from SharePoint
                file_permissions = await perm_client.get_file_permissions(
                    file_id=sp_file.id,
                    file_path=sp_file.path
                )
                
                if file_permissions.permissions:
                    # Get existing blob metadata to check for permission changes
                    existing_blob = existing_blobs.get(blob_name)
                    existing_metadata = existing_blob.metadata if existing_blob else None
                    
                    # Check if permissions have actually changed (delta detection)
                    if should_sync_permissions(file_permissions, existing_metadata):
                        # Convert to metadata and update blob
                        perm_metadata = file_permissions.to_metadata()
                        
                        await logger.ainfo("Syncing permissions (changed)",
                            file_path=sp_file.path,
                            permission_count=len(file_permissions.permissions),
                            summary=permissions_to_summary(file_permissions.permissions)
                        )
                        
                        await blob_client.update_blob_metadata(
                            blob_name=blob_name,
                            additional_metadata=perm_metadata,
                            dry_run=config.dry_run
                        )
                        
                        stats.permissions_synced += 1
                    else:
                        # Permissions unchanged, skip update
                        await logger.adebug("Permissions unchanged (skipped)", file_path=sp_file.path)
                        stats.permissions_unchanged += 1
                else:
                    await logger.adebug("No permissions to sync", file_path=sp_file.path)
                    
            except Exception as e:
                await logger.aerror("Failed to sync permissions",
                    file_path=sp_file.path,
                    error=str(e)
                )
                stats.permissions_failed += 1


async def _sync_permissions_graph_delta(
    config: Config,
    drive_id: str,
    sp_client: SharePointClient,
    blob_client: BlobStorageClient,
    existing_blobs: Dict[str, BlobFile],
    stats: SyncStats
) -> None:
    """
    Sync permissions using Microsoft Graph delta API.
    
    Uses the Graph delta API with special headers to detect permission changes:
    - Prefer: deltashowsharingchanges - Annotates items with @microsoft.graph.sharedChanged
    - Prefer: hierarchicalsharing - Efficient permission hierarchy tracking
    
    This approach is more efficient for large document libraries as it only
    queries items that have changed since the last sync.
    
    Note: Requires Sites.FullControl.All permission for proper operation.
    """
    await logger.ainfo(
        "Syncing SharePoint permissions using GRAPH DELTA API",
        mode="graph_delta",
        token_storage_path=config.delta_token_storage_path
    )
    
    # Initialize delta token storage and client
    token_storage = DeltaTokenStorage(config.delta_token_storage_path)
    
    # Build a map of item_id to SharePoint file info (we need this to map delta items back to files)
    file_id_to_info: Dict[str, SharePointFile] = {}
    await logger.ainfo("Building file ID index for delta mapping...")
    async for sp_file in sp_client.list_files(config.sharepoint_folder_path):
        file_id_to_info[sp_file.id] = sp_file
    await logger.ainfo("File ID index built", file_count=len(file_id_to_info))
    
    async with GraphDeltaPermissionsClient(drive_id, token_storage) as delta_client:
        async with PermissionsClient(drive_id) as perm_client:
            items_to_sync = []
            
            # Collect items with permission changes from delta API
            async for changed_item in delta_client.get_items_with_permission_changes():
                items_to_sync.append(changed_item)
            
            await logger.ainfo(
                "Delta query completed",
                items_to_sync=len(items_to_sync)
            )
            
            # Process each item that has permission changes
            for changed_item in items_to_sync:
                # Look up the file info
                sp_file = file_id_to_info.get(changed_item.item_id)
                
                if not sp_file:
                    # Item might be in a subfolder we haven't indexed, skip it
                    await logger.adebug(
                        "Skipping item not in file index",
                        item_id=changed_item.item_id,
                        path=changed_item.path
                    )
                    continue
                
                blob_name = blob_client._get_blob_name(sp_file.path)
                
                try:
                    # Get current permissions from SharePoint
                    file_permissions = await perm_client.get_file_permissions(
                        file_id=sp_file.id,
                        file_path=sp_file.path
                    )
                    
                    if file_permissions.permissions:
                        # Convert to metadata and update blob
                        perm_metadata = file_permissions.to_metadata()
                        
                        await logger.ainfo("Syncing permissions (delta changed)",
                            file_path=sp_file.path,
                            permission_count=len(file_permissions.permissions),
                            summary=permissions_to_summary(file_permissions.permissions),
                            sharing_changed=changed_item.sharing_changed
                        )
                        
                        await blob_client.update_blob_metadata(
                            blob_name=blob_name,
                            additional_metadata=perm_metadata,
                            dry_run=config.dry_run
                        )
                        
                        stats.permissions_synced += 1
                    else:
                        await logger.adebug("No permissions to sync", file_path=sp_file.path)
                        
                except Exception as e:
                    await logger.aerror("Failed to sync permissions",
                        file_path=sp_file.path,
                        error=str(e)
                    )
                    stats.permissions_failed += 1
            
            # Calculate unchanged (items not in delta = unchanged)
            stats.permissions_unchanged = len(file_id_to_info) - len(items_to_sync)


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
            files_scanned=stats.files_scanned,
            files_added=stats.files_added,
            files_updated=stats.files_updated,
            files_deleted=stats.files_deleted,
            files_unchanged=stats.files_unchanged,
            files_failed=stats.files_failed,
            bytes_transferred=stats.bytes_transferred,
            permissions_synced=stats.permissions_synced,
            permissions_unchanged=stats.permissions_unchanged,
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
