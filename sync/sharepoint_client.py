"""
SharePoint client using Microsoft Graph API.
Uses DefaultAzureCredential for authentication, which supports:
- Managed Identity (when running in Azure Container Apps)
- Client Credentials (when AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID are set)
- Azure CLI (when logged in via 'az login')
"""

import asyncio
import json
import os
from datetime import datetime
from typing import AsyncIterator, Optional, Dict, List
from dataclasses import dataclass
from enum import Enum

import structlog
from azure.identity.aio import DefaultAzureCredential, ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.models.drive_item import DriveItem

logger = structlog.get_logger(__name__)


class FileChangeType(Enum):
    """Type of change detected for a file."""
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


def _get_credential():
    """
    Get the appropriate Azure credential for SharePoint/Graph API access.
    
    For SharePoint, we use explicit ClientSecretCredential when App Registration 
    environment variables are set, as this ensures we authenticate to the correct
    tenant where SharePoint resides.
    
    When no App Registration is configured, falls back to DefaultAzureCredential.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    
    if all([client_id, client_secret, tenant_id]):
        logger.info("Using ClientSecretCredential for SharePoint (App Registration)",
                   client_id=client_id, tenant_id=tenant_id)
        # Use explicit ClientSecretCredential to ensure correct tenant
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret
        )
    elif os.environ.get("IDENTITY_ENDPOINT"):
        logger.info("Using Managed Identity authentication for SharePoint")
        return DefaultAzureCredential()
    else:
        logger.info("Using DefaultAzureCredential for SharePoint (Azure CLI, PowerShell, etc.)")
        return DefaultAzureCredential()


@dataclass
class SharePointFile:
    """Represents a file from SharePoint."""
    id: str
    name: str
    path: str  # Full path relative to the drive root
    size: int
    last_modified: datetime
    download_url: str | None = None
    content_hash: str | None = None  # eTag or cTag for change detection
    change_type: FileChangeType | None = None  # Only set when using delta mode


class SharePointClient:
    """Client for interacting with SharePoint via Microsoft Graph API."""
    
    # Microsoft Graph scope for SharePoint/OneDrive access
    GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
    
    def __init__(self, site_url: str, drive_name: str = "Documents"):
        """
        Initialize the SharePoint client.
        
        Args:
            site_url: The SharePoint site URL (e.g., https://contoso.sharepoint.com/sites/MySite)
            drive_name: The document library name (e.g., "Documents", "Shared Documents")
        """
        self.site_url = site_url
        self.drive_name = drive_name
        self.site_id: str | None = None
        self.drive_id: str | None = None
        self._credential: DefaultAzureCredential | None = None
        self._client: GraphServiceClient | None = None
    
    async def __aenter__(self) -> "SharePointClient":
        """Async context manager entry."""
        self._credential = _get_credential()
        self._client = GraphServiceClient(
            credentials=self._credential,
            scopes=self.GRAPH_SCOPES
        )
        # Resolve site and drive IDs from URL
        await self._resolve_ids()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._credential:
            await self._credential.close()
    
    async def _resolve_ids(self) -> None:
        """Resolve SharePoint site ID and drive ID from the site URL."""
        if not self._client:
            raise RuntimeError("Client not initialized.")
        
        from urllib.parse import urlparse
        parsed = urlparse(self.site_url)
        hostname = parsed.netloc  # e.g., "contoso.sharepoint.com"
        site_path = parsed.path   # e.g., "/sites/MySite"
        
        await logger.ainfo("Resolving SharePoint site", hostname=hostname, site_path=site_path)
        
        # Get site by hostname and path
        # Graph API: GET /sites/{hostname}:{site_path}
        site = await self._client.sites.by_site_id(f"{hostname}:{site_path}").get()
        
        if not site or not site.id:
            raise ValueError(f"Could not resolve SharePoint site: {self.site_url}")
        
        self.site_id = site.id
        await logger.ainfo("Resolved site ID", site_id=self.site_id, site_name=site.display_name)
        
        # Get the document library (drive) by name
        drives = await self._client.sites.by_site_id(self.site_id).drives.get()
        
        if not drives or not drives.value:
            raise ValueError(f"No document libraries found in site: {self.site_url}")
        
        # Find the drive by name
        for drive in drives.value:
            if drive.name and drive.name.lower() == self.drive_name.lower():
                self.drive_id = drive.id
                await logger.ainfo("Resolved drive ID", drive_id=self.drive_id, drive_name=drive.name)
                break
        
        if not self.drive_id:
            # List available drives for better error message
            available_drives = [d.name for d in drives.value if d.name]
            raise ValueError(
                f"Document library '{self.drive_name}' not found in site. "
                f"Available libraries: {', '.join(available_drives)}"
            )
    
    def get_resolved_ids(self) -> tuple[str, str]:
        """
        Get the resolved site and drive IDs.
        
        Returns:
            Tuple of (site_id, drive_id)
        """
        if not self.site_id or not self.drive_id:
            raise RuntimeError("IDs not resolved. Use async with context manager first.")
        return self.site_id, self.drive_id

    async def list_files(self, folder_path: str = "/") -> AsyncIterator[SharePointFile]:
        """
        List all files in a SharePoint folder recursively.
        
        Args:
            folder_path: The folder path relative to the drive root
            
        Yields:
            SharePointFile objects for each file found
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        await logger.ainfo("Listing SharePoint files", folder_path=folder_path)
        
        # Get the folder items - using the correct msgraph-sdk pattern
        try:
            if folder_path == "/" or folder_path == "":
                # Root folder - get root and expand children
                from msgraph.generated.drives.item.root.root_request_builder import RootRequestBuilder
                from kiota_abstractions.base_request_configuration import RequestConfiguration
                
                query_params = RootRequestBuilder.RootRequestBuilderGetQueryParameters(
                    expand=["children"]
                )
                request_config = RequestConfiguration(query_parameters=query_params)
                
                root = await self._client.drives.by_drive_id(self.drive_id).root.get(request_configuration=request_config)
                
                if root and root.children:
                    for item in root.children:
                        async for file in self._process_item(item, folder_path):
                            yield file
            else:
                # Specific folder path - get the folder item by path and expand children
                clean_path = folder_path.strip("/")
                
                from msgraph.generated.drives.item.items.item.drive_item_item_request_builder import DriveItemItemRequestBuilder
                from kiota_abstractions.base_request_configuration import RequestConfiguration
                
                # First get the folder item ID by path
                folder_item = await self._client.drives.by_drive_id(self.drive_id).root.item_with_path(clean_path).get()
                
                if folder_item and folder_item.id:
                    # Now get children of this folder
                    children = await self._client.drives.by_drive_id(self.drive_id).items.by_drive_item_id(folder_item.id).children.get()
                    
                    if children and children.value:
                        for item in children.value:
                            async for file in self._process_item(item, folder_path):
                                yield file
        except Exception as e:
            await logger.aerror("Error listing files", error=str(e), folder_path=folder_path)
            raise
    
    async def _process_item(self, item: DriveItem, parent_path: str) -> AsyncIterator[SharePointFile]:
        """
        Process a drive item, recursively handling folders.
        
        Args:
            item: The DriveItem to process
            parent_path: The parent folder path
            
        Yields:
            SharePointFile objects for files
        """
        if not self._client:
            return
        
        # Build the current path
        if parent_path == "/" or parent_path == "":
            current_path = f"/{item.name}"
        else:
            current_path = f"{parent_path.rstrip('/')}/{item.name}"
        
        if item.folder:
            # Recursively process folder contents
            await logger.adebug("Processing folder", path=current_path)
            
            children = await self._client.drives.by_drive_id(self.drive_id).items.by_drive_item_id(item.id).children.get()
            
            if children and children.value:
                for child in children.value:
                    async for file in self._process_item(child, current_path):
                        yield file
        
        elif item.file:
            # It's a file
            download_url = None
            if hasattr(item, 'microsoft_graph_download_url'):
                download_url = item.microsoft_graph_download_url
            
            file = SharePointFile(
                id=item.id,
                name=item.name,
                path=current_path,
                size=item.size or 0,
                last_modified=item.last_modified_date_time,
                download_url=download_url,
                content_hash=item.c_tag or item.e_tag
            )
            
            await logger.adebug("Found file", 
                name=file.name, 
                path=file.path, 
                size=file.size,
                last_modified=file.last_modified.isoformat() if file.last_modified else None
            )
            
            yield file
    
    async def download_file(self, item_id: str) -> bytes:
        """
        Download a file's content.
        
        Args:
            item_id: The drive item ID
            
        Returns:
            The file content as bytes
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        content = await self._client.drives.by_drive_id(self.drive_id).items.by_drive_item_id(item_id).content.get()
        
        if content is None:
            return b""
        
        return content


# =============================================================================
# Graph Delta API Implementation for File Change Detection
# =============================================================================

@dataclass 
class DeltaToken:
    """Stores delta token information for Graph API delta queries."""
    drive_id: str
    token: str
    last_updated: datetime
    token_type: str = "files"  # "files" or "permissions"
    
    def to_dict(self) -> dict:
        return {
            "drive_id": self.drive_id,
            "token": self.token,
            "last_updated": self.last_updated.isoformat(),
            "token_type": self.token_type
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "DeltaToken":
        return cls(
            drive_id=data["drive_id"],
            token=data["token"],
            last_updated=datetime.fromisoformat(data["last_updated"]),
            token_type=data.get("token_type", "files")
        )


class DeltaTokenStorage:
    """
    Manages storage and retrieval of delta tokens for Graph API delta queries.
    
    Tokens are stored as JSON files in a specified directory.
    """
    
    def __init__(self, storage_path: str):
        """
        Initialize the delta token storage.
        
        Args:
            storage_path: Directory path to store delta tokens
        """
        self.storage_path = storage_path
        os.makedirs(storage_path, exist_ok=True)
    
    def _get_token_file_path(self, drive_id: str, token_type: str = "files") -> str:
        """Get the file path for a specific drive's delta token."""
        # Sanitize drive_id for use as filename
        safe_id = drive_id.replace("!", "_").replace(",", "_")
        return os.path.join(self.storage_path, f"delta_token_{token_type}_{safe_id}.json")
    
    def get_token(self, drive_id: str, token_type: str = "files") -> Optional[DeltaToken]:
        """
        Retrieve the stored delta token for a drive.
        
        Args:
            drive_id: The SharePoint drive ID
            token_type: Type of token ("files" or "permissions")
            
        Returns:
            DeltaToken if exists, None otherwise
        """
        token_path = self._get_token_file_path(drive_id, token_type)
        if not os.path.exists(token_path):
            return None
        
        try:
            with open(token_path, 'r') as f:
                data = json.load(f)
                return DeltaToken.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to load delta token", error=str(e), path=token_path)
            return None
    
    def save_token(self, token: DeltaToken) -> None:
        """
        Save a delta token for a drive.
        
        Args:
            token: The DeltaToken to save
        """
        token_path = self._get_token_file_path(token.drive_id, token.token_type)
        with open(token_path, 'w') as f:
            json.dump(token.to_dict(), f, indent=2)
        logger.info("Saved delta token", drive_id=token.drive_id, token_type=token.token_type, path=token_path)
    
    def delete_token(self, drive_id: str, token_type: str = "files") -> None:
        """Delete the stored delta token for a drive."""
        token_path = self._get_token_file_path(drive_id, token_type)
        if os.path.exists(token_path):
            os.remove(token_path)
            logger.info("Deleted delta token", drive_id=drive_id, token_type=token_type)


class GraphDeltaFilesClient:
    """
    Client that uses Microsoft Graph delta API to detect file changes.
    
    This approach uses the following Graph API features:
    - Delta query: GET /drives/{drive-id}/root/delta
    - Returns only changed items since last sync (after initial enumeration)
    - Tracks deleted items with "deleted" facet
    
    See: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
    """
    
    GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
    
    def __init__(self, drive_id: str, token_storage: DeltaTokenStorage):
        """
        Initialize the Graph delta files client.
        
        Args:
            drive_id: The SharePoint drive ID
            token_storage: Storage for delta tokens
        """
        self.drive_id = drive_id
        self.token_storage = token_storage
        self._credential = None
        self._client: Optional[GraphServiceClient] = None
    
    async def __aenter__(self) -> "GraphDeltaFilesClient":
        """Async context manager entry."""
        self._credential = _get_credential()
        self._client = GraphServiceClient(
            credentials=self._credential,
            scopes=self.GRAPH_SCOPES
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._credential:
            await self._credential.close()
    
    async def get_changed_files(self, folder_path: str = "/") -> AsyncIterator[SharePointFile]:
        """
        Query Graph API delta to find files that have changed.
        
        Uses the delta API to track changes:
        - First call (no token): Returns all items, establishes baseline
        - Subsequent calls (with token): Returns only changed items
        - Deleted items have "deleted" facet set
        
        Args:
            folder_path: The folder path to filter (note: delta always starts from root)
            
        Yields:
            SharePointFile for each changed file with change_type set
        """
        if not self._credential:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        # Get existing token or None for initial sync
        existing_token = self.token_storage.get_token(self.drive_id, "files")
        
        await logger.ainfo(
            "Starting Graph delta query for file changes",
            drive_id=self.drive_id,
            has_existing_token=existing_token is not None,
            folder_path=folder_path
        )
        
        # Normalize folder path for filtering
        folder_filter = folder_path.strip("/").lower() if folder_path and folder_path != "/" else ""
        
        try:
            import httpx
            
            async with httpx.AsyncClient() as http_client:
                # Get access token
                token = await self._credential.get_token("https://graph.microsoft.com/.default")
                
                # Build delta URL
                if existing_token:
                    delta_url = f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root/delta?token={existing_token.token}"
                else:
                    delta_url = f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root/delta"
                
                headers = {
                    "Authorization": f"Bearer {token.token}",
                }
                
                new_delta_link = None
                items_processed = 0
                files_changed = 0
                files_deleted = 0
                
                # Track seen item IDs for initial sync to detect new vs existing
                seen_item_ids: set = set()
                
                # Page through results
                while delta_url:
                    response = await http_client.get(delta_url, headers=headers, timeout=60.0)
                    
                    if response.status_code == 410:
                        # Token expired, need to start fresh
                        await logger.awarning("Delta token expired, starting fresh enumeration")
                        self.token_storage.delete_token(self.drive_id, "files")
                        delta_url = f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root/delta"
                        existing_token = None
                        continue
                    
                    response.raise_for_status()
                    data = response.json()
                    
                    # Process items
                    for item in data.get("value", []):
                        items_processed += 1
                        
                        # Build path from parentReference
                        parent_ref = item.get("parentReference", {})
                        parent_path = parent_ref.get("path", "")
                        # Remove the /drives/{id}/root: prefix
                        if ":/" in parent_path:
                            parent_path = parent_path.split(":/", 1)[1]
                        elif parent_path:
                            parent_path = parent_path.lstrip("/")
                        
                        item_name = item.get("name", "")
                        item_path = f"/{parent_path}/{item_name}" if parent_path else f"/{item_name}"
                        item_path = item_path.replace("//", "/")
                        
                        # Filter by folder path if specified
                        if folder_filter:
                            item_path_lower = item_path.lower().lstrip("/")
                            if not item_path_lower.startswith(folder_filter):
                                continue
                        
                        # Skip folders (we only sync files)
                        if "folder" in item:
                            continue
                        
                        item_id = item.get("id", "")
                        
                        # Check if deleted
                        if "deleted" in item:
                            files_deleted += 1
                            await logger.ainfo(
                                "Deleted file detected via delta",
                                item_id=item_id,
                                path=item_path
                            )
                            
                            yield SharePointFile(
                                id=item_id,
                                name=item_name,
                                path=item_path,
                                size=0,
                                last_modified=datetime.utcnow(),
                                change_type=FileChangeType.DELETED
                            )
                            continue
                        
                        # Skip non-file items
                        if "file" not in item:
                            continue
                        
                        files_changed += 1
                        
                        # Determine change type
                        if existing_token:
                            # With a token, all returned items are modified
                            change_type = FileChangeType.MODIFIED
                        else:
                            # Initial sync - all items are "added" (new to us)
                            change_type = FileChangeType.ADDED
                        
                        # Parse last modified date
                        last_modified_str = item.get("lastModifiedDateTime")
                        last_modified = datetime.utcnow()
                        if last_modified_str:
                            try:
                                last_modified = datetime.fromisoformat(last_modified_str.replace("Z", "+00:00"))
                            except ValueError:
                                pass
                        
                        await logger.adebug(
                            "Changed file detected via delta",
                            item_id=item_id,
                            name=item_name,
                            path=item_path,
                            change_type=change_type.value
                        )
                        
                        yield SharePointFile(
                            id=item_id,
                            name=item_name,
                            path=item_path,
                            size=item.get("size", 0),
                            last_modified=last_modified,
                            content_hash=item.get("cTag") or item.get("eTag"),
                            change_type=change_type
                        )
                    
                    # Get next page or delta link
                    delta_url = data.get("@odata.nextLink")
                    if not delta_url:
                        new_delta_link = data.get("@odata.deltaLink")
                
                await logger.ainfo(
                    "Graph delta query completed",
                    items_processed=items_processed,
                    files_changed=files_changed,
                    files_deleted=files_deleted,
                    is_initial_sync=existing_token is None
                )
                
                # Save the new delta link for next run
                if new_delta_link:
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(new_delta_link)
                    query_params = parse_qs(parsed.query)
                    token_value = query_params.get("token", [None])[0]
                    
                    if token_value:
                        new_token = DeltaToken(
                            drive_id=self.drive_id,
                            token=token_value,
                            last_updated=datetime.utcnow(),
                            token_type="files"
                        )
                        self.token_storage.save_token(new_token)
        
        except Exception as e:
            await logger.aerror("Error during Graph delta query for files", error=str(e))
            raise
    
    async def download_file(self, item_id: str) -> bytes:
        """
        Download a file's content.
        
        Args:
            item_id: The drive item ID
            
        Returns:
            The file content as bytes
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        content = await self._client.drives.by_drive_id(self.drive_id).items.by_drive_item_id(item_id).content.get()
        
        if content is None:
            return b""
        
        return content
