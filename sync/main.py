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

from config import Config
from sharepoint_client import SharePointClient, SharePointFile
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
        sync_permissions=sync_permissions
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
            
            # Step 3: Process SharePoint files
            await logger.ainfo("Scanning SharePoint files...")
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
            
            # Step 4: Handle orphaned blobs (files deleted from SharePoint)
            if config.delete_orphaned_blobs:
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
                await logger.ainfo("Syncing SharePoint permissions to blob metadata...")
                
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
                                # Convert to metadata and update blob
                                perm_metadata = file_permissions.to_metadata()
                                
                                await logger.ainfo("Syncing permissions",
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
                                await logger.adebug("No permissions to sync", file_path=sp_file.path)
                                
                        except Exception as e:
                            await logger.aerror("Failed to sync permissions",
                                file_path=sp_file.path,
                                error=str(e)
                            )
                            stats.permissions_failed += 1
    
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
