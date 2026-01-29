"""
Configuration settings for SharePoint to Blob sync job.
Uses environment variables for configuration.
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration for the sync job."""
    
    # SharePoint settings
    sharepoint_site_url: str  # e.g., "https://contoso.sharepoint.com/sites/MySite"
    sharepoint_drive_name: str  # Document library name, e.g., "Documents" or "Shared Documents"
    sharepoint_folder_path: str  # e.g., "/FAQ" or root "/"
    
    # Azure Blob settings
    storage_account_name: str
    container_name: str
    blob_prefix: str  # Prefix for blobs in the container
    
    # Sync settings
    delete_orphaned_blobs: bool  # Delete blobs that no longer exist in SharePoint
    dry_run: bool  # If True, only log what would be done without making changes
    
    # Resolved IDs (populated at runtime) - these have defaults so must come last
    sharepoint_site_id: str = ""
    sharepoint_drive_id: str = ""
    
    @classmethod
    def from_environment(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            # SharePoint
            sharepoint_site_url=os.environ.get("SHAREPOINT_SITE_URL", ""),
            sharepoint_drive_name=os.environ.get("SHAREPOINT_DRIVE_NAME", "Documents"),
            sharepoint_folder_path=os.environ.get("SHAREPOINT_FOLDER_PATH", "/"),
            
            # Azure Blob
            storage_account_name=os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", ""),
            container_name=os.environ.get("AZURE_BLOB_CONTAINER_NAME", "sharepoint-sync"),
            blob_prefix=os.environ.get("AZURE_BLOB_PREFIX", ""),
            
            # Sync settings
            delete_orphaned_blobs=os.environ.get("DELETE_ORPHANED_BLOBS", "false").lower() == "true",
            dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
        )
    
    def validate(self) -> None:
        """Validate that all required configuration is present."""
        errors = []
        
        if not self.sharepoint_site_url:
            errors.append("SHAREPOINT_SITE_URL is required (e.g., https://contoso.sharepoint.com/sites/MySite)")
        if not self.storage_account_name:
            errors.append("AZURE_STORAGE_ACCOUNT_NAME is required")
        if not self.container_name:
            errors.append("AZURE_BLOB_CONTAINER_NAME is required")
        
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
    
    @property
    def blob_account_url(self) -> str:
        """Get the blob storage account URL."""
        return f"https://{self.storage_account_name}.blob.core.windows.net"
    
    @property
    def sharepoint_host_and_path(self) -> tuple[str, str]:
        """
        Parse the SharePoint site URL into host and site path.
        
        Returns:
            Tuple of (hostname, site_path)
            e.g., ("contoso.sharepoint.com", "/sites/MySite")
        """
        from urllib.parse import urlparse
        parsed = urlparse(self.sharepoint_site_url)
        return parsed.netloc, parsed.path
