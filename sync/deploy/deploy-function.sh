#!/bin/bash
# ==============================================================================
# Azure Function App Deployment Script for SharePoint Sync
# ==============================================================================
# This script deploys an Azure Function App that syncs SharePoint to Blob Storage
# on a daily schedule using a timer trigger.
#
# Prerequisites:
#   - Azure CLI installed and logged in (az login)
#   - Contributor access to the target subscription
# ==============================================================================

set -e

# ==============================================================================
# Configuration - Update these placeholders
# ==============================================================================

# Azure Resource Configuration
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-your-subscription-id}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-sharepoint-sync}"
LOCATION="${LOCATION:-francecentral}"

# Function App Configuration
FUNCTION_APP_NAME="${FUNCTION_APP_NAME:-func-sharepoint-sync}"
STORAGE_ACCOUNT_FUNC="${STORAGE_ACCOUNT_FUNC:-stfuncspsync$(openssl rand -hex 4)}"
APP_SERVICE_PLAN="${APP_SERVICE_PLAN:-asp-sharepoint-sync}"

# SharePoint Configuration (will be set as app settings)
SHAREPOINT_SITE_URL="${SHAREPOINT_SITE_URL:-https://your-tenant.sharepoint.com/sites/your-site}"
SHAREPOINT_DRIVE_NAME="${SHAREPOINT_DRIVE_NAME:-Documents}"
SHAREPOINT_FOLDER_PATH="${SHAREPOINT_FOLDER_PATH:-/}"

# Target Blob Storage
AZURE_STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:-your-storage-account}"
AZURE_BLOB_CONTAINER_NAME="${AZURE_BLOB_CONTAINER_NAME:-sharepoint-files}"

# Sync Settings
DELETE_ORPHANED_BLOBS="${DELETE_ORPHANED_BLOBS:-true}"
SYNC_PERMISSIONS="${SYNC_PERMISSIONS:-true}"

# Timer Schedule (CRON format) - Default: 2:00 AM UTC daily
TIMER_SCHEDULE="${TIMER_SCHEDULE:-0 0 2 * * *}"

# ==============================================================================
# Functions
# ==============================================================================

log_info() {
    echo "[INFO] $1"
}

log_success() {
    echo "[SUCCESS] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
}

check_prerequisites() {
    log_info "Checking prerequisites..."
    
    if ! command -v az &> /dev/null; then
        log_error "Azure CLI is not installed. Please install it first."
        exit 1
    fi
    
    if ! command -v func &> /dev/null; then
        log_info "Azure Functions Core Tools not found. Installing..."
        npm install -g azure-functions-core-tools@4 --unsafe-perm true || {
            log_error "Failed to install Azure Functions Core Tools"
            exit 1
        }
    fi
    
    # Check if logged in
    az account show &> /dev/null || {
        log_error "Not logged into Azure CLI. Please run 'az login' first."
        exit 1
    }
    
    log_success "Prerequisites check passed"
}

set_subscription() {
    log_info "Setting subscription to $SUBSCRIPTION_ID..."
    az account set --subscription "$SUBSCRIPTION_ID"
    log_success "Subscription set"
}

create_resource_group() {
    log_info "Creating resource group '$RESOURCE_GROUP' in '$LOCATION'..."
    az group create \
        --name "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --output none
    log_success "Resource group created"
}

create_storage_account() {
    log_info "Creating storage account '$STORAGE_ACCOUNT_FUNC' for Function App..."
    az storage account create \
        --name "$STORAGE_ACCOUNT_FUNC" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku Standard_LRS \
        --kind StorageV2 \
        --output none
    log_success "Storage account created"
}

create_app_service_plan() {
    log_info "Creating App Service Plan '$APP_SERVICE_PLAN'..."
    az appservice plan create \
        --name "$APP_SERVICE_PLAN" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku B1 \
        --is-linux \
        --output none
    log_success "App Service Plan created"
}

create_function_app() {
    log_info "Creating Function App '$FUNCTION_APP_NAME'..."
    az functionapp create \
        --name "$FUNCTION_APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --storage-account "$STORAGE_ACCOUNT_FUNC" \
        --plan "$APP_SERVICE_PLAN" \
        --runtime python \
        --runtime-version 3.11 \
        --functions-version 4 \
        --os-type Linux \
        --output none
    log_success "Function App created"
}

enable_managed_identity() {
    log_info "Enabling system-assigned managed identity..."
    PRINCIPAL_ID=$(az functionapp identity assign \
        --name "$FUNCTION_APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --query "principalId" \
        --output tsv)
    log_success "Managed identity enabled. Principal ID: $PRINCIPAL_ID"
    echo "$PRINCIPAL_ID"
}

assign_storage_role() {
    local principal_id=$1
    log_info "Assigning 'Storage Blob Data Contributor' role to Function App..."
    
    STORAGE_RESOURCE_ID=$(az storage account show \
        --name "$AZURE_STORAGE_ACCOUNT_NAME" \
        --query "id" \
        --output tsv 2>/dev/null || echo "")
    
    if [ -z "$STORAGE_RESOURCE_ID" ]; then
        log_error "Storage account '$AZURE_STORAGE_ACCOUNT_NAME' not found. Please create it first or update the variable."
        return 1
    fi
    
    az role assignment create \
        --assignee "$principal_id" \
        --role "Storage Blob Data Contributor" \
        --scope "$STORAGE_RESOURCE_ID" \
        --output none 2>/dev/null || log_info "Role assignment may already exist"
    
    log_success "Storage role assigned"
}

configure_app_settings() {
    log_info "Configuring Function App settings..."
    
    az functionapp config appsettings set \
        --name "$FUNCTION_APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --settings \
            "SHAREPOINT_SITE_URL=$SHAREPOINT_SITE_URL" \
            "SHAREPOINT_DRIVE_NAME=$SHAREPOINT_DRIVE_NAME" \
            "SHAREPOINT_FOLDER_PATH=$SHAREPOINT_FOLDER_PATH" \
            "AZURE_STORAGE_ACCOUNT_NAME=$AZURE_STORAGE_ACCOUNT_NAME" \
            "AZURE_BLOB_CONTAINER_NAME=$AZURE_BLOB_CONTAINER_NAME" \
            "DELETE_ORPHANED_BLOBS=$DELETE_ORPHANED_BLOBS" \
            "SYNC_PERMISSIONS=$SYNC_PERMISSIONS" \
            "TIMER_SCHEDULE=$TIMER_SCHEDULE" \
            "PYTHONPATH=/home/site/wwwroot" \
        --output none
    
    log_success "App settings configured"
}

create_function_code() {
    log_info "Creating Azure Function code..."
    
    FUNC_DIR="$(dirname "$0")/function-app"
    mkdir -p "$FUNC_DIR/sharepoint_sync"
    
    # Create host.json
    cat > "$FUNC_DIR/host.json" << 'EOF'
{
    "version": "2.0",
    "logging": {
        "applicationInsights": {
            "samplingSettings": {
                "isEnabled": true,
                "excludedTypes": "Request"
            }
        },
        "logLevel": {
            "default": "Information",
            "Host.Results": "Information",
            "Function": "Information",
            "Host.Aggregator": "Information"
        }
    },
    "extensionBundle": {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.*, 5.0.0)"
    },
    "functionTimeout": "00:30:00"
}
EOF

    # Create requirements.txt
    cat > "$FUNC_DIR/requirements.txt" << 'EOF'
azure-functions
azure-identity
azure-storage-blob>=12.0.0
msgraph-sdk>=1.0.0
python-dotenv
EOF

    # Create function.json for timer trigger
    cat > "$FUNC_DIR/sharepoint_sync/function.json" << 'EOF'
{
    "scriptFile": "__init__.py",
    "bindings": [
        {
            "name": "timer",
            "type": "timerTrigger",
            "direction": "in",
            "schedule": "%TIMER_SCHEDULE%"
        }
    ]
}
EOF

    # Create the main function code
    cat > "$FUNC_DIR/sharepoint_sync/__init__.py" << 'PYTHON_EOF'
"""
SharePoint to Blob Storage Sync - Azure Function (Timer Trigger)

This function runs on a schedule to sync files from SharePoint to Azure Blob Storage.
Uses managed identity for authentication to both SharePoint (via Microsoft Graph) and Blob Storage.
"""

import azure.functions as func
import logging
import os
import sys
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from msgraph import GraphServiceClient
from msgraph.generated.models.o_data_errors.o_data_error import ODataError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_env_var(name: str, default: str = None, required: bool = False) -> str:
    """Get environment variable with optional default and required check."""
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Required environment variable '{name}' is not set")
    return value


class SharePointSyncFunction:
    """Handles SharePoint to Blob Storage synchronization."""
    
    def __init__(self):
        # Load configuration from environment
        self.sharepoint_site_url = get_env_var("SHAREPOINT_SITE_URL", required=True)
        self.drive_name = get_env_var("SHAREPOINT_DRIVE_NAME", "Documents")
        self.folder_path = get_env_var("SHAREPOINT_FOLDER_PATH", "/")
        self.storage_account = get_env_var("AZURE_STORAGE_ACCOUNT_NAME", required=True)
        self.container_name = get_env_var("AZURE_BLOB_CONTAINER_NAME", required=True)
        self.delete_orphaned = get_env_var("DELETE_ORPHANED_BLOBS", "false").lower() == "true"
        self.sync_permissions = get_env_var("SYNC_PERMISSIONS", "true").lower() == "true"
        
        # Initialize credentials (managed identity in Azure, DefaultAzure locally)
        try:
            self.credential = ManagedIdentityCredential()
            # Test the credential
            self.credential.get_token("https://graph.microsoft.com/.default")
            logger.info("Using Managed Identity credential")
        except Exception:
            logger.info("Falling back to DefaultAzureCredential")
            self.credential = DefaultAzureCredential()
        
        # Initialize clients
        self.graph_client = GraphServiceClient(self.credential)
        self.blob_service = BlobServiceClient(
            account_url=f"https://{self.storage_account}.blob.core.windows.net",
            credential=self.credential
        )
        self.container_client = self.blob_service.get_container_client(self.container_name)
        
        # Statistics
        self.stats = {
            "files_synced": 0,
            "files_skipped": 0,
            "files_deleted": 0,
            "errors": 0
        }
    
    def _parse_site_url(self) -> tuple:
        """Parse SharePoint site URL to get hostname and site path."""
        from urllib.parse import urlparse
        parsed = urlparse(self.sharepoint_site_url)
        hostname = parsed.netloc
        site_path = parsed.path.rstrip("/")
        return hostname, site_path
    
    async def get_site_id(self) -> str:
        """Get SharePoint site ID from URL."""
        hostname, site_path = self._parse_site_url()
        site = await self.graph_client.sites.by_site_id(f"{hostname}:{site_path}").get()
        return site.id
    
    async def get_drive_id(self, site_id: str) -> str:
        """Get drive ID for the specified document library."""
        drives = await self.graph_client.sites.by_site_id(site_id).drives.get()
        for drive in drives.value:
            if drive.name == self.drive_name:
                return drive.id
        raise ValueError(f"Drive '{self.drive_name}' not found in site")
    
    async def list_files(self, site_id: str, drive_id: str) -> list:
        """List all files in the specified folder."""
        files = []
        folder_path = self.folder_path.strip("/") or "root"
        
        if folder_path == "root":
            items = await self.graph_client.sites.by_site_id(site_id).drives.by_drive_id(drive_id).root.children.get()
        else:
            items = await self.graph_client.sites.by_site_id(site_id).drives.by_drive_id(drive_id).root.items_by_path(folder_path).children.get()
        
        for item in items.value:
            if item.file:
                files.append({
                    "id": item.id,
                    "name": item.name,
                    "size": item.size,
                    "modified": item.last_modified_date_time,
                    "web_url": item.web_url
                })
        
        return files
    
    async def download_file(self, site_id: str, drive_id: str, item_id: str) -> bytes:
        """Download file content from SharePoint."""
        content = await self.graph_client.sites.by_site_id(site_id).drives.by_drive_id(drive_id).items.by_drive_item_id(item_id).content.get()
        return content
    
    def upload_to_blob(self, blob_name: str, content: bytes, metadata: dict = None):
        """Upload content to blob storage."""
        blob_client = self.container_client.get_blob_client(blob_name)
        blob_client.upload_blob(content, overwrite=True, metadata=metadata)
    
    def blob_exists(self, blob_name: str) -> bool:
        """Check if blob exists."""
        blob_client = self.container_client.get_blob_client(blob_name)
        return blob_client.exists()
    
    def get_blob_metadata(self, blob_name: str) -> dict:
        """Get blob properties including last modified time."""
        blob_client = self.container_client.get_blob_client(blob_name)
        props = blob_client.get_blob_properties()
        return {
            "last_modified": props.last_modified,
            "size": props.size,
            "metadata": props.metadata or {}
        }
    
    async def sync_file(self, site_id: str, drive_id: str, file_info: dict):
        """Sync a single file from SharePoint to Blob Storage."""
        blob_name = file_info["name"]
        
        try:
            # Check if blob exists and compare timestamps
            if self.blob_exists(blob_name):
                blob_props = self.get_blob_metadata(blob_name)
                if blob_props["last_modified"] >= file_info["modified"]:
                    logger.debug(f"Skipping '{blob_name}' - already up to date")
                    self.stats["files_skipped"] += 1
                    return
            
            # Download from SharePoint
            logger.info(f"Downloading '{blob_name}' from SharePoint...")
            content = await self.download_file(site_id, drive_id, file_info["id"])
            
            # Prepare metadata
            metadata = {
                "source": "sharepoint",
                "original_url": file_info["web_url"],
                "sharepoint_id": file_info["id"]
            }
            
            # Upload to Blob Storage
            logger.info(f"Uploading '{blob_name}' to Blob Storage...")
            self.upload_to_blob(blob_name, content, metadata)
            
            self.stats["files_synced"] += 1
            logger.info(f"Successfully synced '{blob_name}'")
            
        except Exception as e:
            logger.error(f"Error syncing '{blob_name}': {str(e)}")
            self.stats["errors"] += 1
    
    async def run_sync(self):
        """Run the full synchronization process."""
        start_time = datetime.utcnow()
        logger.info(f"Starting SharePoint sync at {start_time.isoformat()}")
        logger.info(f"Site: {self.sharepoint_site_url}")
        logger.info(f"Drive: {self.drive_name}")
        logger.info(f"Folder: {self.folder_path}")
        logger.info(f"Target: {self.storage_account}/{self.container_name}")
        
        try:
            # Get site and drive IDs
            site_id = await self.get_site_id()
            drive_id = await self.get_drive_id(site_id)
            
            # List files
            files = await self.list_files(site_id, drive_id)
            logger.info(f"Found {len(files)} files in SharePoint")
            
            # Sync each file
            for file_info in files:
                await self.sync_file(site_id, drive_id, file_info)
            
            # Log summary
            end_time = datetime.utcnow()
            duration = (end_time - start_time).total_seconds()
            
            logger.info("=" * 50)
            logger.info("Sync completed!")
            logger.info(f"Duration: {duration:.2f} seconds")
            logger.info(f"Files synced: {self.stats['files_synced']}")
            logger.info(f"Files skipped: {self.stats['files_skipped']}")
            logger.info(f"Errors: {self.stats['errors']}")
            logger.info("=" * 50)
            
            return self.stats
            
        except ODataError as e:
            logger.error(f"Microsoft Graph API error: {e.error.message}")
            raise
        except Exception as e:
            logger.error(f"Sync failed: {str(e)}")
            raise


async def main(timer: func.TimerRequest) -> None:
    """Azure Function entry point - Timer Trigger."""
    
    if timer.past_due:
        logger.warning("Timer is running late!")
    
    logger.info("SharePoint Sync function triggered")
    
    try:
        sync = SharePointSyncFunction()
        stats = await sync.run_sync()
        
        logger.info(f"Sync completed successfully: {stats}")
        
    except Exception as e:
        logger.error(f"Sync failed with error: {str(e)}")
        raise
PYTHON_EOF

    log_success "Function code created at $FUNC_DIR"
    echo "$FUNC_DIR"
}

deploy_function() {
    local func_dir=$1
    log_info "Deploying function to Azure..."
    
    cd "$func_dir"
    
    # Install dependencies locally first
    pip install -r requirements.txt -q
    
    # Deploy using Azure Functions Core Tools
    func azure functionapp publish "$FUNCTION_APP_NAME" --python
    
    log_success "Function deployed to $FUNCTION_APP_NAME"
}

print_summary() {
    echo ""
    echo "============================================================"
    echo "  Deployment Complete!"
    echo "============================================================"
    echo ""
    echo "Function App: $FUNCTION_APP_NAME"
    echo "Resource Group: $RESOURCE_GROUP"
    echo "Schedule: $TIMER_SCHEDULE (CRON)"
    echo ""
    echo "Environment Variables configured:"
    echo "  - SHAREPOINT_SITE_URL: $SHAREPOINT_SITE_URL"
    echo "  - SHAREPOINT_DRIVE_NAME: $SHAREPOINT_DRIVE_NAME"
    echo "  - AZURE_STORAGE_ACCOUNT_NAME: $AZURE_STORAGE_ACCOUNT_NAME"
    echo "  - AZURE_BLOB_CONTAINER_NAME: $AZURE_BLOB_CONTAINER_NAME"
    echo ""
    echo "Next steps:"
    echo "1. Grant the Function App's managed identity access to SharePoint"
    echo "   - Go to Azure AD > Enterprise Applications"
    echo "   - Find the Function App's managed identity"
    echo "   - Grant Microsoft Graph API permissions: Sites.Read.All"
    echo ""
    echo "2. Verify the timer trigger schedule"
    echo "   az functionapp config appsettings list --name $FUNCTION_APP_NAME --resource-group $RESOURCE_GROUP"
    echo ""
    echo "3. Monitor function execution"
    echo "   az functionapp log tail --name $FUNCTION_APP_NAME --resource-group $RESOURCE_GROUP"
    echo ""
}

# ==============================================================================
# Main Execution
# ==============================================================================

main() {
    echo "============================================================"
    echo "  SharePoint Sync - Azure Function Deployment"
    echo "============================================================"
    echo ""
    
    check_prerequisites
    
    if [ "$SUBSCRIPTION_ID" = "your-subscription-id" ]; then
        log_error "Please update SUBSCRIPTION_ID in the script or set it as an environment variable"
        exit 1
    fi
    
    set_subscription
    create_resource_group
    create_storage_account
    create_app_service_plan
    create_function_app
    
    PRINCIPAL_ID=$(enable_managed_identity)
    assign_storage_role "$PRINCIPAL_ID"
    
    configure_app_settings
    
    FUNC_DIR=$(create_function_code)
    
    # Deploy if not in dry-run mode
    if [ "${DRY_RUN:-false}" != "true" ]; then
        deploy_function "$FUNC_DIR"
    else
        log_info "Dry run mode - skipping deployment"
        log_info "Function code created at: $FUNC_DIR"
    fi
    
    print_summary
}

# Run main function
main "$@"
