"""
SharePoint to Azure Blob Storage permissions synchronization.

This module retrieves file permissions from SharePoint via Microsoft Graph API
and stores them as blob metadata in Azure Blob Storage.

Recommended approach options:
1. Blob Metadata (default): Store permissions as JSON in blob metadata
2. Azure Data Lake Storage Gen2 ACLs: Apply POSIX-like ACLs (requires HNS enabled)
3. Blob Index Tags: Store as searchable tags (limited to simple key-value pairs)

This implementation uses the Blob Metadata approach as it:
- Works with standard Blob Storage (no HNS required)
- Supports complex permission structures (users, groups, roles)
- Allows application-level permission enforcement
- Preserves the full SharePoint permission model
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, AsyncIterator

import structlog
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from msgraph import GraphServiceClient

logger = structlog.get_logger(__name__)

# Metadata keys for storing permissions
# Legacy: Full permissions JSON (for debugging/reference)
METADATA_PERMISSIONS = "sharepoint_permissions"
METADATA_PERMISSIONS_SYNCED_AT = "permissions_synced_at"

# Azure AI Search ACL-compatible metadata keys for POSIX-like permissions preview feature
# When stored in blob metadata, the indexer sees them prefixed with "metadata_"
# e.g., "user_ids" in blob becomes "metadata_user_ids" in indexer
# 
# IMPORTANT: For the permissionFilter preview feature, fields MUST use these specific names:
# - "user_ids" → becomes "metadata_user_ids" → maps to index field with permissionFilter: "userIds"
# - "group_ids" → becomes "metadata_group_ids" → maps to index field with permissionFilter: "groupIds"
# See: https://learn.microsoft.com/en-us/azure/search/search-security-document-level-access-control
METADATA_ACL_USER_IDS = "user_ids"     # JSON array of user object IDs (Entra IDs)
METADATA_ACL_GROUP_IDS = "group_ids"   # JSON array of group object IDs (Entra IDs)


@dataclass
class SharePointPermission:
    """Represents a permission entry from SharePoint."""
    id: str
    roles: List[str]  # e.g., ["read"], ["write"], ["owner"]
    identity_type: str  # "user", "group", "siteGroup"
    display_name: str
    email: Optional[str] = None
    identity_id: Optional[str] = None  # Microsoft Entra Object ID
    inherited: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "roles": self.roles,
            "identity_type": self.identity_type,
            "display_name": self.display_name,
            "email": self.email,
            "identity_id": self.identity_id,
            "inherited": self.inherited
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "SharePointPermission":
        """Create from dictionary."""
        return cls(
            id=data.get("id", ""),
            roles=data.get("roles", []),
            identity_type=data.get("identity_type", "unknown"),
            display_name=data.get("display_name", ""),
            email=data.get("email"),
            identity_id=data.get("identity_id"),
            inherited=data.get("inherited", False)
        )


@dataclass
class FilePermissions:
    """Represents all permissions for a file."""
    file_path: str
    file_id: str
    permissions: List[SharePointPermission] = field(default_factory=list)
    synced_at: Optional[datetime] = None

    def to_metadata(self) -> Dict[str, str]:
        """
        Convert permissions to blob metadata format.
        
        Returns:
            Dictionary of metadata key-value pairs (values must be strings)
            
        Note:
            This method produces ACL-compatible metadata for Azure AI Search:
            - acl_user_ids: Comma-separated list of Entra user object IDs
            - acl_group_ids: Comma-separated list of Entra group object IDs
            
            Special values supported by Azure AI Search:
            - "all": Any user can access the document
            - "none": No user can access (must match other ACL type)
        """
        permissions_json = json.dumps([p.to_dict() for p in self.permissions])
        
        # Extract user and group IDs for Azure AI Search ACL enforcement
        user_ids = self._extract_user_ids()
        group_ids = self._extract_group_ids()
        
        metadata = {
            METADATA_PERMISSIONS: permissions_json,
            METADATA_PERMISSIONS_SYNCED_AT: self.synced_at.isoformat() if self.synced_at else datetime.utcnow().isoformat(),
        }
        
        # Add ACL fields - store as pipe-delimited strings for Azure AI Search skillset compatibility
        # The indexer will read these as "metadata_user_ids" and "metadata_group_ids"
        # A SplitSkill in the skillset will convert them to Collection(Edm.String)
        #
        # We use pipe (|) as delimiter since GUIDs don't contain pipes
        # When there are no specific users/groups, we use a placeholder GUID that won't match any real ID
        PLACEHOLDER_NO_USERS = "00000000-0000-0000-0000-000000000000"
        PLACEHOLDER_NO_GROUPS = "00000000-0000-0000-0000-000000000001"
        
        if user_ids:
            metadata[METADATA_ACL_USER_IDS] = "|".join(user_ids)
        else:
            # No specific users - use placeholder (access controlled by groups)
            metadata[METADATA_ACL_USER_IDS] = PLACEHOLDER_NO_USERS
            
        if group_ids:
            metadata[METADATA_ACL_GROUP_IDS] = "|".join(group_ids)
        else:
            # No specific groups - use placeholder (access controlled by users)
            metadata[METADATA_ACL_GROUP_IDS] = PLACEHOLDER_NO_GROUPS
        
        return metadata
    
    def _extract_user_ids(self) -> List[str]:
        """
        Extract unique Entra user object IDs from permissions.
        
        Returns:
            List of user object IDs (GUIDs) that have access to this file
        """
        user_ids = []
        for perm in self.permissions:
            if perm.identity_type == "user" and perm.identity_id:
                # Only include Entra object IDs (GUIDs), not SharePoint-specific IDs
                if self._is_valid_guid(perm.identity_id):
                    user_ids.append(perm.identity_id)
        return list(set(user_ids))  # Remove duplicates
    
    def _extract_group_ids(self) -> List[str]:
        """
        Extract unique Entra group object IDs from permissions.
        
        Returns:
            List of group object IDs (GUIDs) that have access to this file
        """
        group_ids = []
        for perm in self.permissions:
            if perm.identity_type == "group" and perm.identity_id:
                # Only include Entra object IDs (GUIDs)
                if self._is_valid_guid(perm.identity_id):
                    group_ids.append(perm.identity_id)
        return list(set(group_ids))  # Remove duplicates
    
    @staticmethod
    def _is_valid_guid(value: str) -> bool:
        """
        Check if a string looks like a valid GUID/UUID.
        Entra Object IDs are GUIDs like: 00aa00aa-bb11-cc22-dd33-44ee44ee44ee
        """
        if not value:
            return False
        import re
        guid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        return bool(re.match(guid_pattern, value))
    
    @classmethod
    def from_metadata(cls, file_path: str, file_id: str, metadata: Dict[str, str]) -> "FilePermissions":
        """Create FilePermissions from blob metadata."""
        permissions_json = metadata.get(METADATA_PERMISSIONS, "[]")
        synced_at_str = metadata.get(METADATA_PERMISSIONS_SYNCED_AT)
        
        try:
            permissions_data = json.loads(permissions_json)
            permissions = [SharePointPermission.from_dict(p) for p in permissions_data]
        except json.JSONDecodeError:
            permissions = []
        
        synced_at = None
        if synced_at_str:
            try:
                synced_at = datetime.fromisoformat(synced_at_str)
            except ValueError:
                pass
        
        return cls(
            file_path=file_path,
            file_id=file_id,
            permissions=permissions,
            synced_at=synced_at
        )


class PermissionsClient:
    """Client for fetching and syncing SharePoint permissions."""
    
    GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
    
    def __init__(self, drive_id: str):
        """
        Initialize the permissions client.
        
        Args:
            drive_id: The SharePoint drive ID
        """
        self.drive_id = drive_id
        self._credential = None
        self._client: Optional[GraphServiceClient] = None
    
    async def __aenter__(self) -> "PermissionsClient":
        """Async context manager entry."""
        self._credential = _get_sharepoint_credential()
        self._client = GraphServiceClient(
            credentials=self._credential,
            scopes=self.GRAPH_SCOPES
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._credential:
            await self._credential.close()
    
    async def get_file_permissions(self, file_id: str, file_path: str) -> FilePermissions:
        """
        Get all permissions for a specific file.
        
        Args:
            file_id: The SharePoint drive item ID
            file_path: The file path (for logging/reference)
            
        Returns:
            FilePermissions object containing all permissions
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        await logger.ainfo("Fetching permissions", file_path=file_path, file_id=file_id)
        
        try:
            # Get permissions from Graph API
            # GET /drives/{drive-id}/items/{item-id}/permissions
            permissions_response = await self._client.drives.by_drive_id(
                self.drive_id
            ).items.by_drive_item_id(
                file_id
            ).permissions.get()
            
            if not permissions_response or not permissions_response.value:
                await logger.ainfo("No permissions found", file_path=file_path)
                return FilePermissions(
                    file_path=file_path,
                    file_id=file_id,
                    permissions=[],
                    synced_at=datetime.utcnow()
                )
            
            permissions = []
            for perm in permissions_response.value:
                sp_perm = self._parse_permission(perm)
                if sp_perm:
                    permissions.append(sp_perm)
            
            await logger.ainfo("Fetched permissions", 
                              file_path=file_path, 
                              permission_count=len(permissions))
            
            return FilePermissions(
                file_path=file_path,
                file_id=file_id,
                permissions=permissions,
                synced_at=datetime.utcnow()
            )
            
        except Exception as e:
            await logger.aerror("Failed to fetch permissions", 
                               file_path=file_path, 
                               error=str(e))
            raise
    
    def _parse_permission(self, perm) -> Optional[SharePointPermission]:
        """
        Parse a Graph API permission object into SharePointPermission.
        
        Args:
            perm: The permission object from Graph API
            
        Returns:
            SharePointPermission or None if parsing fails
        """
        try:
            perm_id = perm.id or ""
            roles = perm.roles or []
            inherited = perm.inherited_from is not None
            
            # Determine identity type and details from grantedToV2
            identity_type = "unknown"
            display_name = ""
            email = None
            identity_id = None
            
            if perm.granted_to_v2:
                gtv2 = perm.granted_to_v2
                
                if gtv2.user:
                    identity_type = "user"
                    display_name = gtv2.user.display_name or ""
                    email = getattr(gtv2.user, 'email', None)
                    identity_id = gtv2.user.id
                    
                elif gtv2.group:
                    identity_type = "group"
                    display_name = gtv2.group.display_name or ""
                    email = getattr(gtv2.group, 'email', None)
                    identity_id = gtv2.group.id
                    
                elif gtv2.site_group:
                    identity_type = "siteGroup"
                    display_name = gtv2.site_group.display_name or ""
                    identity_id = str(gtv2.site_group.id) if gtv2.site_group.id else None
                    
                elif gtv2.site_user:
                    identity_type = "user"
                    display_name = gtv2.site_user.display_name or ""
                    email = getattr(gtv2.site_user, 'email', None)
                    identity_id = gtv2.site_user.id
            
            # Fallback to grantedTo if grantedToV2 is not available
            elif perm.granted_to and perm.granted_to.user:
                identity_type = "user"
                display_name = perm.granted_to.user.display_name or ""
                email = getattr(perm.granted_to.user, 'email', None)
                identity_id = perm.granted_to.user.id
            
            return SharePointPermission(
                id=perm_id,
                roles=roles,
                identity_type=identity_type,
                display_name=display_name,
                email=email,
                identity_id=identity_id,
                inherited=inherited
            )
            
        except Exception as e:
            logger.warning("Failed to parse permission", error=str(e))
            return None


def _get_sharepoint_credential():
    """
    Get the appropriate Azure credential for SharePoint/Graph API access.
    Uses explicit ClientSecretCredential when App Registration environment 
    variables are set.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    
    if all([client_id, client_secret, tenant_id]):
        logger.info("Using ClientSecretCredential for permissions sync",
                   client_id=client_id, tenant_id=tenant_id)
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret
        )
    elif os.environ.get("IDENTITY_ENDPOINT"):
        logger.info("Using Managed Identity for permissions sync")
        return DefaultAzureCredential()
    else:
        logger.info("Using DefaultAzureCredential for permissions sync")
        return DefaultAzureCredential()


def is_permissions_sync_enabled() -> bool:
    """Check if permissions sync is enabled via environment variable."""
    return os.environ.get("SYNC_PERMISSIONS", "false").lower() == "true"


def get_permissions_from_metadata(metadata: Dict[str, str]) -> Optional[FilePermissions]:
    """
    Extract permissions from blob metadata.
    
    Args:
        metadata: The blob metadata dictionary
        
    Returns:
        FilePermissions if present, None otherwise
    """
    if METADATA_PERMISSIONS not in metadata:
        return None
    
    return FilePermissions.from_metadata(
        file_path="",
        file_id="",
        metadata=metadata
    )


def permissions_to_summary(permissions: List[SharePointPermission]) -> str:
    """
    Create a human-readable summary of permissions.
    
    Args:
        permissions: List of SharePointPermission objects
        
    Returns:
        Summary string for logging
    """
    if not permissions:
        return "No permissions"
    
    summary_parts = []
    for perm in permissions:
        roles_str = ",".join(perm.roles)
        if perm.email:
            summary_parts.append(f"{perm.display_name}<{perm.email}>:{roles_str}")
        else:
            summary_parts.append(f"{perm.display_name}:{roles_str}")
    
    return "; ".join(summary_parts)
