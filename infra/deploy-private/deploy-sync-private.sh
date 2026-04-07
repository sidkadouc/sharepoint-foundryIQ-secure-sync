#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Deploy SharePoint sync Function App with VNet integration.
# The function runs inside the private VNet and accesses Storage + Search
# through private endpoints (no public internet).
#
# Supports both Python and .NET sync implementations.
#
# Usage:  RUNTIME={python|dotnet}  ./deploy-sync-private.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load env files
# Source .env first (base), then .env.private (overrides), then infra outputs.
[[ -f "$ROOT_DIR/.env" ]]             && { set -a; source "$ROOT_DIR/.env"; set +a; }
[[ -f "$ROOT_DIR/.env.private" ]]     && { set -a; source "$ROOT_DIR/.env.private"; set +a; }
[[ -f "$SCRIPT_DIR/.foundry-outputs" ]] && { set -a; source "$SCRIPT_DIR/.foundry-outputs"; set +a; }

RUNTIME="${RUNTIME:-python}"
case "$RUNTIME" in
    python|dotnet) ;;
    *) echo "[ERROR] RUNTIME must be: python, dotnet" >&2; exit 1 ;;
esac

# ── Config ──────────────────────────────────────────────────────────────────
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-spsync-private}"
LOCATION="${LOCATION:-swedencentral}"

VNET_NAME="${VNET_NAME:?VNET_NAME required — run deploy-foundry.sh first}"
SUBNET_SYNC_NAME="${SUBNET_SYNC_NAME:-snet-sync}"
AZURE_STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:?AZURE_STORAGE_ACCOUNT_NAME required}"

TIMER_SCHEDULE="${TIMER_SCHEDULE:-0 0 2 * * *}"
AZURE_BLOB_CONTAINER_NAME="${AZURE_BLOB_CONTAINER_NAME:-sharepoint-sync}"

SUFFIX="$(date +%m%d%H%M)"
FUNCTION_APP_NAME="${FUNCTION_APP_NAME:-func-spsync-prv-${SUFFIX}}"
FUNCTION_STORAGE_ACCOUNT="${FUNCTION_STORAGE_ACCOUNT:-stfuncprv${SUFFIX}}"
FUNCTION_STORAGE_ACCOUNT="${FUNCTION_STORAGE_ACCOUNT:0:24}"

if [[ "$RUNTIME" = "python" ]]; then
    SYNC_DIR="$ROOT_DIR/sync"
    RUNTIME_FLAG="python"
    RUNTIME_VERSION="3.11"
else
    SYNC_DIR="$ROOT_DIR/sync-dotnet"
    RUNTIME_FLAG="dotnet-isolated"
    RUNTIME_VERSION="10.0"
fi

# ── Validate ────────────────────────────────────────────────────────────────
for var in SHAREPOINT_SITE_URL AZURE_BLOB_CONTAINER_NAME; do
    [[ -z "${!var:-}" ]] && { echo "[ERROR] $var is required" >&2; exit 1; }
done

command -v az   >/dev/null 2>&1 || { echo "[ERROR] az CLI required" >&2; exit 1; }
command -v func >/dev/null 2>&1 || { echo "[ERROR] func CLI required" >&2; exit 1; }

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Private Sync Function Deployment ($RUNTIME)               "
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Function App:  $FUNCTION_APP_NAME"
echo "║  VNet:          $VNET_NAME / $SUBNET_SYNC_NAME"
echo "║  Storage:       $AZURE_STORAGE_ACCOUNT_NAME (via PE)"
echo "╚══════════════════════════════════════════════════════════════╝"

az account set --subscription "$SUBSCRIPTION_ID"

# ── 1. Function storage account (needs to be accessible for deployment) ────
echo ""
echo "═══ 1/5  Function Storage Account ═══"

az storage account create \
    --name "$FUNCTION_STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 \
    --allow-shared-key-access true \
    -o none

DEPLOY_CONTAINER="app-package-${FUNCTION_APP_NAME}"
az storage container create \
    --name "$DEPLOY_CONTAINER" \
    --account-name "$FUNCTION_STORAGE_ACCOUNT" \
    --auth-mode login -o none

echo "[OK] Function storage: $FUNCTION_STORAGE_ACCOUNT"

# ── 2. Function App with VNet integration ──────────────────────────────────
echo ""
echo "═══ 2/5  Function App (VNet-integrated) ═══"

az functionapp create \
    --name "$FUNCTION_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --flexconsumption-location "$LOCATION" \
    --storage-account "$FUNCTION_STORAGE_ACCOUNT" \
    --deployment-storage-name "$FUNCTION_STORAGE_ACCOUNT" \
    --deployment-storage-container-name "$DEPLOY_CONTAINER" \
    --deployment-storage-auth-type SystemAssignedIdentity \
    --runtime "$RUNTIME_FLAG" --runtime-version "$RUNTIME_VERSION" \
    --functions-version 4 --os-type Linux \
    --assign-identity '[system]' \
    -o none

# VNet integration — route outbound traffic through the private VNet
echo "[INFO] Enabling VNet integration on subnet $SUBNET_SYNC_NAME"
az functionapp vnet-integration add \
    --name "$FUNCTION_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --vnet "$VNET_NAME" \
    --subnet "$SUBNET_SYNC_NAME" \
    -o none

# Route all traffic through VNet (not just RFC1918)
az functionapp config appsettings set \
    --name "$FUNCTION_APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --settings "WEBSITE_VNET_ROUTE_ALL=1" \
               "WEBSITE_DNS_SERVER=168.63.129.16" \
    -o none

echo "[OK] Function App created with VNet integration"

# ── 3. RBAC ────────────────────────────────────────────────────────────────
echo ""
echo "═══ 3/5  RBAC Assignments ═══"

PRINCIPAL_ID=$(az functionapp identity show \
    --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --query principalId -o tsv)

TARGET_STORAGE_ID=$(az storage account show \
    --name "$AZURE_STORAGE_ACCOUNT_NAME" \
    --resource-group "$RESOURCE_GROUP" --query id -o tsv)
FUNC_STORAGE_ID=$(az storage account show \
    --name "$FUNCTION_STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" --query id -o tsv)

echo "[INFO] Storage Blob Data Contributor on target + function storage"
az role assignment create --assignee "$PRINCIPAL_ID" \
    --role "Storage Blob Data Contributor" --scope "$TARGET_STORAGE_ID" \
    -o none 2>/dev/null || true
az role assignment create --assignee "$PRINCIPAL_ID" \
    --role "Storage Blob Data Contributor" --scope "$FUNC_STORAGE_ID" \
    -o none 2>/dev/null || true

echo "[OK] RBAC assignments created"

# ── 4. App Settings ────────────────────────────────────────────────────────
echo ""
echo "═══ 4/5  App Settings ═══"

APP_SETTINGS=(
    "SHAREPOINT_SITE_URL=${SHAREPOINT_SITE_URL}"
    "SHAREPOINT_DRIVE_NAME=${SHAREPOINT_DRIVE_NAME:-Documents}"
    "SHAREPOINT_FOLDER_PATH=${SHAREPOINT_FOLDER_PATH:-/}"
    "AZURE_STORAGE_ACCOUNT_NAME=${AZURE_STORAGE_ACCOUNT_NAME}"
    "AZURE_BLOB_CONTAINER_NAME=${AZURE_BLOB_CONTAINER_NAME}"
    "AZURE_BLOB_PREFIX=${AZURE_BLOB_PREFIX:-}"
    "DELETE_ORPHANED_BLOBS=${DELETE_ORPHANED_BLOBS:-false}"
    "DRY_RUN=${DRY_RUN:-false}"
    "SYNC_PERMISSIONS=${SYNC_PERMISSIONS:-true}"
    "FORCE_FULL_SYNC=${FORCE_FULL_SYNC:-false}"
    "AZURE_CLIENT_ID=${AZURE_CLIENT_ID:-}"
    "AZURE_CLIENT_SECRET=${AZURE_CLIENT_SECRET:-}"
    "AZURE_TENANT_ID=${AZURE_TENANT_ID:-}"
)

if [[ "$RUNTIME" = "python" ]]; then
    APP_SETTINGS+=("TIMER_SCHEDULE=$TIMER_SCHEDULE")
else
    APP_SETTINGS+=("SYNC_SCHEDULE=$TIMER_SCHEDULE")
fi

az functionapp config appsettings set \
    --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --settings "${APP_SETTINGS[@]}" \
    -o none

echo "[OK] App settings configured"

# ── 5. Publish ─────────────────────────────────────────────────────────────
echo ""
echo "═══ 5/5  Publish Function Code ═══"

if [[ "$RUNTIME" = "python" ]]; then
    cd "$SYNC_DIR"
    PUBLISH_CMD="func azure functionapp publish $FUNCTION_APP_NAME --python"
else
    PUBLISH_DIR="$SYNC_DIR/.publish"
    dotnet publish "$SYNC_DIR/src/SharePointSync.Job/SharePointSync.Job.csproj" \
        -c Release -o "$PUBLISH_DIR" --nologo -v q
    cd "$PUBLISH_DIR"
    PUBLISH_CMD="func azure functionapp publish $FUNCTION_APP_NAME --dotnet-isolated"
fi

MAX_ATTEMPTS=5; ATTEMPT=1
while true; do
    set +e
    OUTPUT=$($PUBLISH_CMD 2>&1)
    RC=$?; set -e; echo "$OUTPUT"

    [[ $RC -eq 0 ]] && break

    if echo "$OUTPUT" | grep -q "Uploaded package to storage successfully"; then
        echo "[WARN] Package uploaded but health check unhealthy — continuing."
        break
    fi

    if echo "$OUTPUT" | grep -q "not authorized to perform this operation" && [[ $ATTEMPT -lt $MAX_ATTEMPTS ]]; then
        echo "[WARN] RBAC propagation delay, retry $ATTEMPT/$MAX_ATTEMPTS in 20s..."
        ATTEMPT=$((ATTEMPT + 1)); sleep 20; continue
    fi

    exit $RC
done

[[ "$RUNTIME" = "dotnet" ]] && rm -rf "$PUBLISH_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Sync Function Deployed (Private)                           ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Function App:  $FUNCTION_APP_NAME"
echo "║  Runtime:       $RUNTIME"
echo "║  VNet:          $VNET_NAME / $SUBNET_SYNC_NAME"
echo "║  Schedule:      $TIMER_SCHEDULE"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  The function routes ALL traffic through the VNet.           ║"
echo "║  Storage access is via private endpoint (no public).         ║"
echo "║  SharePoint Graph API calls go through VNet NAT gateway.     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Next: ./deploy-project.sh  (Foundry project + agent)         ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# Append to foundry outputs
cat >> "$SCRIPT_DIR/.foundry-outputs" <<EOF
FUNCTION_APP_NAME=$FUNCTION_APP_NAME
FUNCTION_STORAGE_ACCOUNT=$FUNCTION_STORAGE_ACCOUNT
SYNC_PRINCIPAL_ID=$PRINCIPAL_ID
EOF
