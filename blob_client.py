"""
Azure Blob Storage client for sync operations.
Uses DefaultAzureCredential for authentication, which supports:
- Managed Identity (when running in Azure Container Apps)
- Client Credentials (when AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID are set)
- Azure CLI (when logged in via 'az login')
"""

import os
from datetime import datetime, timezone
from typing import Dict, AsyncIterator
from dataclasses import dataclass

import structlog
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient, ContainerClient

logger = structlog.get_logger(__name__)


def _get_credential():
    """
    Get the appropriate Azure credential for Blob Storage access.
    
    For Blob Storage, we check if a separate storage credential is configured.
    If not, we use Azure CLI credentials directly (useful when storage is in 
    a different tenant than SharePoint).
    
    Set AZURE_STORAGE_TENANT_ID, AZURE_STORAGE_CLIENT_ID, AZURE_STORAGE_CLIENT_SECRET
    if you want to use a separate App Registration for storage.
    """
    # Check if separate storage credentials are configured
    storage_tenant_id = os.environ.get("AZURE_STORAGE_TENANT_ID")
    storage_client_id = os.environ.get("AZURE_STORAGE_CLIENT_ID")
    storage_client_secret = os.environ.get("AZURE_STORAGE_CLIENT_SECRET")
    
    if all([storage_tenant_id, storage_client_id, storage_client_secret]):
        # Use explicit credentials for storage if configured
        logger.info("Using ClientSecretCredential for Blob Storage",
                   client_id=storage_client_id, tenant_id=storage_tenant_id)
        from azure.identity.aio import ClientSecretCredential
        return ClientSecretCredential(
            tenant_id=storage_tenant_id,
            client_id=storage_client_id,
            client_secret=storage_client_secret
        )
    elif os.environ.get("IDENTITY_ENDPOINT"):
        logger.info("Using Managed Identity authentication for Blob Storage")
        return DefaultAzureCredential()
    else:
        # Use Azure CLI credential directly to avoid picking up SharePoint App Registration
        # environment variables (AZURE_CLIENT_ID, etc.) which are for a different tenant
        logger.info("Using AzureCliCredential for Blob Storage")
        from azure.identity.aio import AzureCliCredential
        return AzureCliCredential()


@dataclass
class BlobFile:
    """Represents a blob in Azure Storage."""
    name: str
    size: int
    last_modified: datetime
    content_hash: str | None = None  # MD5 or ETag
    metadata: Dict[str, str] | None = None


class BlobStorageClient:
    """Client for Azure Blob Storage operations."""
    
    # Metadata key for storing SharePoint file info
    METADATA_SP_ITEM_ID = "sharepoint_item_id"
    METADATA_SP_LAST_MODIFIED = "sharepoint_last_modified"
    METADATA_SP_CONTENT_HASH = "sharepoint_content_hash"
    
    def __init__(self, account_url: str, container_name: str, blob_prefix: str = ""):
        """
        Initialize the Blob Storage client.
        
        Args:
            account_url: The blob storage account URL
            container_name: The container name
            blob_prefix: Optional prefix for all blobs
        """
        self.account_url = account_url
        self.container_name = container_name
        self.blob_prefix = blob_prefix.strip("/")
        self._credential: DefaultAzureCredential | None = None
        self._service_client: BlobServiceClient | None = None
        self._container_client: ContainerClient | None = None
    
    async def __aenter__(self) -> "BlobStorageClient":
        """Async context manager entry."""
        self._credential = _get_credential()
        self._service_client = BlobServiceClient(
            account_url=self.account_url,
            credential=self._credential
        )
        self._container_client = self._service_client.get_container_client(self.container_name)
        
        # Ensure container exists
        try:
            await self._container_client.create_container()
            await logger.ainfo("Created container", container=self.container_name)
        except Exception:
            # Container likely already exists
            pass
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._service_client:
            await self._service_client.close()
        if self._credential:
            await self._credential.close()
    
    def _get_blob_name(self, sharepoint_path: str) -> str:
        """
        Convert a SharePoint path to a blob name.
        
        Args:
            sharepoint_path: The SharePoint file path
            
        Returns:
            The blob name with optional prefix
        """
        # Remove leading slash and clean up the path
        clean_path = sharepoint_path.lstrip("/")
        
        if self.blob_prefix:
            return f"{self.blob_prefix}/{clean_path}"
        return clean_path
    
    async def list_blobs(self) -> AsyncIterator[BlobFile]:
        """
        List all blobs in the container with the configured prefix.
        
        Yields:
            BlobFile objects for each blob
        """
        if not self._container_client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        prefix = self.blob_prefix if self.blob_prefix else None
        
        await logger.ainfo("Listing blobs", container=self.container_name, prefix=prefix)
        
        async for blob in self._container_client.list_blobs(name_starts_with=prefix, include=['metadata']):
            yield BlobFile(
                name=blob.name,
                size=blob.size,
                last_modified=blob.last_modified,
                content_hash=blob.etag,
                metadata=blob.metadata
            )
    
    async def get_blob_metadata(self, blob_name: str) -> BlobFile | None:
        """
        Get metadata for a specific blob.
        
        Args:
            blob_name: The blob name
            
        Returns:
            BlobFile if exists, None otherwise
        """
        if not self._container_client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        try:
            blob_client = self._container_client.get_blob_client(blob_name)
            properties = await blob_client.get_blob_properties()
            
            return BlobFile(
                name=blob_name,
                size=properties.size,
                last_modified=properties.last_modified,
                content_hash=properties.etag,
                metadata=properties.metadata
            )
        except Exception:
            return None
    
    async def upload_blob(
        self,
        sharepoint_path: str,
        content: bytes,
        sharepoint_item_id: str,
        sharepoint_last_modified: datetime,
        sharepoint_content_hash: str | None = None,
        dry_run: bool = False
    ) -> str:
        """
        Upload content to a blob.
        
        Args:
            sharepoint_path: The SharePoint file path (used to derive blob name)
            content: The file content
            sharepoint_item_id: The SharePoint item ID (stored in metadata)
            sharepoint_last_modified: The SharePoint last modified date
            sharepoint_content_hash: Optional content hash from SharePoint
            dry_run: If True, only log without making changes
            
        Returns:
            The blob name
        """
        if not self._container_client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        blob_name = self._get_blob_name(sharepoint_path)
        
        metadata = {
            self.METADATA_SP_ITEM_ID: sharepoint_item_id,
            self.METADATA_SP_LAST_MODIFIED: sharepoint_last_modified.isoformat(),
        }
        if sharepoint_content_hash:
            metadata[self.METADATA_SP_CONTENT_HASH] = sharepoint_content_hash
        
        if dry_run:
            await logger.ainfo("[DRY RUN] Would upload blob", 
                blob_name=blob_name, 
                size=len(content),
                sharepoint_path=sharepoint_path
            )
        else:
            blob_client = self._container_client.get_blob_client(blob_name)
            await blob_client.upload_blob(content, overwrite=True, metadata=metadata)
            await logger.ainfo("Uploaded blob", 
                blob_name=blob_name, 
                size=len(content),
                sharepoint_path=sharepoint_path
            )
        
        return blob_name
    
    async def delete_blob(self, blob_name: str, dry_run: bool = False) -> None:
        """
        Delete a blob.
        
        Args:
            blob_name: The blob name to delete
            dry_run: If True, only log without making changes
        """
        if not self._container_client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        if dry_run:
            await logger.ainfo("[DRY RUN] Would delete blob", blob_name=blob_name)
        else:
            blob_client = self._container_client.get_blob_client(blob_name)
            await blob_client.delete_blob()
            await logger.ainfo("Deleted blob", blob_name=blob_name)
    
    def should_update(self, blob: BlobFile, sp_last_modified: datetime, sp_content_hash: str | None) -> bool:
        """
        Determine if a blob should be updated based on SharePoint file changes.
        
        Args:
            blob: The existing blob
            sp_last_modified: SharePoint file last modified date
            sp_content_hash: SharePoint content hash
            
        Returns:
            True if the blob should be updated
        """
        if not blob.metadata:
            return True
        
        # Check content hash first (most reliable)
        stored_hash = blob.metadata.get(self.METADATA_SP_CONTENT_HASH)
        if stored_hash and sp_content_hash and stored_hash != sp_content_hash:
            return True
        
        # Fall back to date comparison
        stored_date_str = blob.metadata.get(self.METADATA_SP_LAST_MODIFIED)
        if stored_date_str:
            try:
                stored_date = datetime.fromisoformat(stored_date_str.replace('Z', '+00:00'))
                # Ensure both dates are timezone-aware for comparison
                if sp_last_modified.tzinfo is None:
                    sp_last_modified = sp_last_modified.replace(tzinfo=timezone.utc)
                if stored_date.tzinfo is None:
                    stored_date = stored_date.replace(tzinfo=timezone.utc)
                
                return sp_last_modified > stored_date
            except (ValueError, TypeError):
                return True
        
        return True

    async def update_blob_metadata(
        self,
        blob_name: str,
        additional_metadata: Dict[str, str],
        dry_run: bool = False
    ) -> None:
        """
        Update metadata on an existing blob (merges with existing metadata).
        
        Args:
            blob_name: The blob name
            additional_metadata: New metadata to add/update
            dry_run: If True, only log without making changes
        """
        if not self._container_client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        blob_client = self._container_client.get_blob_client(blob_name)
        
        if dry_run:
            await logger.ainfo("[DRY RUN] Would update blob metadata", 
                blob_name=blob_name,
                metadata_keys=list(additional_metadata.keys())
            )
        else:
            # Get existing metadata
            try:
                properties = await blob_client.get_blob_properties()
                existing_metadata = properties.metadata or {}
            except Exception:
                existing_metadata = {}
            
            # Merge metadata
            merged_metadata = {**existing_metadata, **additional_metadata}
            
            await blob_client.set_blob_metadata(merged_metadata)
            await logger.ainfo("Updated blob metadata", 
                blob_name=blob_name,
                metadata_keys=list(additional_metadata.keys())
            )
