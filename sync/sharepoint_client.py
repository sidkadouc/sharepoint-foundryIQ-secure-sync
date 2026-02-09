"""
SharePoint client using Microsoft Graph API.
Uses DefaultAzureCredential for authentication, which supports:
- Managed Identity (when running in Azure Container Apps)
- Client Credentials (when AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID are set)
- Azure CLI (when logged in via 'az login')

Supports delta queries for incremental sync:
- First run: full crawl via delta endpoint, returns a delta token
- Subsequent runs: only changed/deleted items since last delta token
See: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
"""

import asyncio
import os
from datetime import datetime
from typing import AsyncIterator, List, Optional
from dataclasses import dataclass, field
from enum import Enum

import httpx
import structlog
from azure.identity.aio import DefaultAzureCredential, ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.models.drive_item import DriveItem

logger = structlog.get_logger(__name__)


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


class DeltaChangeType(Enum):
    """Type of change detected via delta query."""
    CREATED_OR_MODIFIED = "created_or_modified"
    DELETED = "deleted"


@dataclass
class DeltaChange:
    """Represents a single change from a delta query."""
    change_type: DeltaChangeType
    file: Optional[SharePointFile] = None  # Set for created/modified files (not folders or deletions)
    item_id: str = ""                       # Always set
    item_name: str = ""                     # Best-effort name
    item_path: str = ""                     # Best-effort path
    is_folder: bool = False                 # True if the item is a folder


@dataclass
class DeltaResult:
    """Result of a delta query including changes and the new delta token."""
    changes: List[DeltaChange] = field(default_factory=list)
    delta_token: str = ""    # The new deltaLink URL to persist for next run
    is_initial_sync: bool = False  # True when no previous token was supplied


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

    # ------------------------------------------------------------------ #
    #  Delta query support
    # ------------------------------------------------------------------ #

    async def _get_access_token(self) -> str:
        """Obtain a bearer token for Microsoft Graph using a fresh credential."""
        # Create a fresh credential to avoid conflicts with the GraphServiceClient's
        # internal HTTP transport (which may close the shared credential).
        credential = _get_credential()
        try:
            token = await credential.get_token("https://graph.microsoft.com/.default")
            return token.token
        finally:
            await credential.close()

    async def get_delta(
        self,
        delta_link: str | None = None,
    ) -> DeltaResult:
        """
        Use the Microsoft Graph delta API to get incremental changes.

        If *delta_link* is ``None`` (first run), a full-crawl delta is performed
        and a delta token is returned for subsequent calls.

        See https://learn.microsoft.com/en-us/graph/api/driveitem-delta

        Args:
            delta_link: The deltaLink URL returned by a previous call.
                        Pass ``None`` for the initial full sync.

        Returns:
            DeltaResult with the list of changes and a new delta token.
        """
        if not self._credential or not self.drive_id:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        is_initial = delta_link is None
        # Starting URL: either the saved deltaLink or the initial delta endpoint
        url = delta_link or f"https://graph.microsoft.com/v1.0/drives/{self.drive_id}/root/delta"

        await logger.ainfo("Starting delta query",
                          is_initial=is_initial,
                          url=url[:120])

        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        changes: List[DeltaChange] = []
        new_delta_link = ""

        async with httpx.AsyncClient(timeout=120) as http:
            next_url: str | None = url
            page = 0

            while next_url:
                page += 1
                resp = await http.get(next_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                items = data.get("value", [])
                await logger.ainfo("Delta page received",
                                  page=page, items_in_page=len(items))

                for item in items:
                    change = self._parse_delta_item(item)
                    if change:
                        changes.append(change)

                # Follow @odata.nextLink for paging, or capture @odata.deltaLink
                next_url = data.get("@odata.nextLink")
                if not next_url:
                    new_delta_link = data.get("@odata.deltaLink", "")

        file_changes = [c for c in changes if not c.is_folder]
        folder_changes = [c for c in changes if c.is_folder]
        deletions = [c for c in changes if c.change_type == DeltaChangeType.DELETED]

        await logger.ainfo("Delta query complete",
                          total_changes=len(changes),
                          file_changes=len(file_changes),
                          folder_changes=len(folder_changes),
                          deletions=len(deletions),
                          is_initial=is_initial)

        return DeltaResult(
            changes=file_changes,   # only file-level changes (callers don't need folder entries)
            delta_token=new_delta_link,
            is_initial_sync=is_initial,
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_delta_item(item: dict) -> Optional[DeltaChange]:
        """
        Parse a raw JSON item from the delta response into a DeltaChange.

        Deleted items carry a ``deleted`` facet; everything else is
        treated as created-or-modified.
        """
        item_id = item.get("id", "")
        item_name = item.get("name", "")

        # Build the path from parentReference.path + name
        parent_ref = item.get("parentReference", {})
        parent_path_raw = parent_ref.get("path", "")  # e.g. "/drives/{id}/root:/folder"
        # Strip the /drives/{id}/root: prefix to get the SharePoint-relative path
        if ":" in parent_path_raw:
            parent_path = parent_path_raw.split(":", 1)[1]  # "/folder"
        else:
            parent_path = ""
        if parent_path:
            item_path = f"{parent_path.rstrip('/')}/{item_name}"
        else:
            item_path = f"/{item_name}" if item_name else ""

        is_folder = "folder" in item

        # Deleted item
        if "deleted" in item:
            return DeltaChange(
                change_type=DeltaChangeType.DELETED,
                item_id=item_id,
                item_name=item_name,
                item_path=item_path,
                is_folder=is_folder,
            )

        # Folder (not a file) – still return so caller can filter
        if is_folder:
            return DeltaChange(
                change_type=DeltaChangeType.CREATED_OR_MODIFIED,
                item_id=item_id,
                item_name=item_name,
                item_path=item_path,
                is_folder=True,
            )

        # File
        if "file" in item:
            last_modified = None
            lm_str = item.get("lastModifiedDateTime")
            if lm_str:
                try:
                    last_modified = datetime.fromisoformat(lm_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            sp_file = SharePointFile(
                id=item_id,
                name=item_name,
                path=item_path,
                size=item.get("size", 0),
                last_modified=last_modified,
                download_url=item.get("@microsoft.graph.downloadUrl"),
                content_hash=item.get("cTag") or item.get("eTag"),
            )
            return DeltaChange(
                change_type=DeltaChangeType.CREATED_OR_MODIFIED,
                file=sp_file,
                item_id=item_id,
                item_name=item_name,
                item_path=item_path,
                is_folder=False,
            )

        # Unknown item type – skip
        return None
