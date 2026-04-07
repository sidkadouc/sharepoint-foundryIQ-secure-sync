#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Deploy a Foundry project with capability host + create agent (v2).
#
# Creates:
#   1. Foundry project under the account
#   2. Account-level connections (Storage, Search, Cosmos DB)
#   3. Account-level capability host (Agents kind)
#   4. Project-level capability host (with BYO connections)
#   5. Agent via .NET SDK (Azure.AI.Projects v2 API)
#
# Prerequisites: ./deploy-foundry.sh completed (.foundry-outputs exists)
#
# Usage:
#   PROJECT_NAME=my-project  ./deploy-project.sh
#
# For shared capability host (account-level defaults):
#   SHARED_CAPHOST=true  ./deploy-project.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_TOOL_DIR="$SCRIPT_DIR/agent-tool"

# Load env + foundry outputs
# Source .env first (base), then .env.private (overrides), then foundry outputs.
[[ -f "$ROOT_DIR/.env" ]]               && { set -a; source "$ROOT_DIR/.env"; set +a; }
[[ -f "$ROOT_DIR/.env.private" ]]       && { set -a; source "$ROOT_DIR/.env.private"; set +a; }
[[ -f "$SCRIPT_DIR/.foundry-outputs" ]] && { set -a; source "$SCRIPT_DIR/.foundry-outputs"; set +a; }

# ── Config ──────────────────────────────────────────────────────────────────
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:?SUBSCRIPTION_ID required}"
RESOURCE_GROUP="${RESOURCE_GROUP:?RESOURCE_GROUP required}"
LOCATION="${LOCATION:?LOCATION required}"
FOUNDRY_ACCOUNT_NAME="${FOUNDRY_ACCOUNT_NAME:?FOUNDRY_ACCOUNT_NAME required — run deploy-foundry.sh first}"

PROJECT_NAME="${PROJECT_NAME:-spsync-project}"
SHARED_CAPHOST="${SHARED_CAPHOST:-false}"

AZURE_STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:?required}"
SEARCH_SERVICE_NAME="${SEARCH_SERVICE_NAME:?required}"
COSMOSDB_ACCOUNT_NAME="${COSMOSDB_ACCOUNT_NAME:?required}"

CHAT_DEPLOYMENT_NAME="${CHAT_DEPLOYMENT_NAME:-gpt-4o}"
EMBEDDING_DEPLOYMENT_NAME="${EMBEDDING_DEPLOYMENT_NAME:-text-embedding-3-large}"
INDEX_NAME="${INDEX_NAME:-sharepoint-index}"

MGMT_API="https://management.azure.com"
API_VERSION="2025-06-01"

command -v az >/dev/null 2>&1    || { echo "[ERROR] az CLI required" >&2; exit 1; }
command -v dotnet >/dev/null 2>&1 || { echo "[ERROR] dotnet CLI required (for agent creation)" >&2; exit 1; }

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   Step 2: Deploy Project + Capability Host + Agent               ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Foundry Account: $FOUNDRY_ACCOUNT_NAME"
echo "║  Project:         $PROJECT_NAME"
echo "║  Shared CapHost:  $SHARED_CAPHOST"
echo "║  Chat Model:      $CHAT_DEPLOYMENT_NAME"
echo "╚══════════════════════════════════════════════════════════════════╝"

az account set --subscription "$SUBSCRIPTION_ID"

# Helper: call ARM REST API
arm_rest() {
    local method="$1" path="$2" body="${3:-}"
    local url="${MGMT_API}${path}?api-version=${API_VERSION}"
    if [[ -n "$body" ]]; then
        az rest --method "$method" --url "$url" --body "$body" -o json 2>&1
    else
        az rest --method "$method" --url "$url" -o json 2>&1
    fi
}

ACCOUNT_PATH="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${FOUNDRY_ACCOUNT_NAME}"

# ── 1. Create Foundry Project ────────────────────────────────────────────
echo ""
echo "═══ 1/5  Create Project: $PROJECT_NAME ═══"

PROJECT_BODY=$(cat <<EOF
{
  "location": "$LOCATION",
  "properties": {},
  "kind": "Project",
  "sku": { "name": "S0" }
}
EOF
)

arm_rest PUT "${ACCOUNT_PATH}/projects/${PROJECT_NAME}" "$PROJECT_BODY" > /dev/null 2>&1 || true

# Wait for project to be ready
echo "[INFO] Waiting for project provisioning..."
for i in $(seq 1 12); do
    STATE=$(az rest --method GET \
        --url "${MGMT_API}${ACCOUNT_PATH}/projects/${PROJECT_NAME}?api-version=${API_VERSION}" \
        --query "properties.provisioningState" -o tsv 2>/dev/null || echo "Creating")
    [[ "$STATE" = "Succeeded" ]] && break
    echo "[INFO] State: $STATE (attempt $i/12)..."
    sleep 10
done

echo "[OK] Project: $PROJECT_NAME"

# ── 2. Create Connections (on the account) ───────────────────────────────
echo ""
echo "═══ 2/5  Create Connections ═══"

# Storage connection
STORAGE_ENDPOINT="https://${AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
STORAGE_RESOURCE_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${AZURE_STORAGE_ACCOUNT_NAME}"

STORAGE_CONN_BODY=$(cat <<EOF
{
  "properties": {
    "category": "AzureBlobStorage",
    "target": "$STORAGE_ENDPOINT",
    "authType": "AAD",
    "metadata": {
      "ResourceId": "$STORAGE_RESOURCE_ID"
    }
  }
}
EOF
)
echo "[INFO] Creating Storage connection..."
arm_rest PUT "${ACCOUNT_PATH}/connections/conn-storage" "$STORAGE_CONN_BODY" > /dev/null 2>&1 || echo "[WARN] Storage connection may already exist"

# AI Search connection
SEARCH_ENDPOINT="https://${SEARCH_SERVICE_NAME}.search.windows.net"
SEARCH_RESOURCE_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.Search/searchServices/${SEARCH_SERVICE_NAME}"

SEARCH_CONN_BODY=$(cat <<EOF
{
  "properties": {
    "category": "CognitiveSearch",
    "target": "$SEARCH_ENDPOINT",
    "authType": "AAD",
    "metadata": {
      "ResourceId": "$SEARCH_RESOURCE_ID"
    }
  }
}
EOF
)
echo "[INFO] Creating AI Search connection..."
arm_rest PUT "${ACCOUNT_PATH}/connections/conn-search" "$SEARCH_CONN_BODY" > /dev/null 2>&1 || echo "[WARN] Search connection may already exist"

# Cosmos DB connection
COSMOS_ENDPOINT="https://${COSMOSDB_ACCOUNT_NAME}.documents.azure.com:443/"
COSMOS_RESOURCE_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.DocumentDB/databaseAccounts/${COSMOSDB_ACCOUNT_NAME}"

COSMOS_CONN_BODY=$(cat <<EOF
{
  "properties": {
    "category": "CosmosDB",
    "target": "$COSMOS_ENDPOINT",
    "authType": "AAD",
    "metadata": {
      "ResourceId": "$COSMOS_RESOURCE_ID"
    }
  }
}
EOF
)
echo "[INFO] Creating Cosmos DB connection..."
arm_rest PUT "${ACCOUNT_PATH}/connections/conn-cosmosdb" "$COSMOS_CONN_BODY" > /dev/null 2>&1 || echo "[WARN] Cosmos DB connection may already exist"

echo "[OK] Connections created (conn-storage, conn-search, conn-cosmosdb)"

# ── 3. Create Account-Level Capability Host ──────────────────────────────
echo ""
echo "═══ 3/5  Account Capability Host ═══"

if [[ "$SHARED_CAPHOST" = "true" ]]; then
    # Shared: account-level caphost has all the connections
    ACCOUNT_CAPHOST_BODY=$(cat <<EOF
{
  "properties": {
    "capabilityHostKind": "Agents",
    "storageConnections": ["conn-storage"],
    "vectorStoreConnections": ["conn-search"],
    "threadStorageConnections": ["conn-cosmosdb"]
  }
}
EOF
    )
else
    # Minimal: just enable Agents at account level
    ACCOUNT_CAPHOST_BODY=$(cat <<EOF
{
  "properties": {
    "capabilityHostKind": "Agents"
  }
}
EOF
    )
fi

echo "[INFO] Creating account capability host..."
arm_rest PUT "${ACCOUNT_PATH}/capabilityHosts/default" "$ACCOUNT_CAPHOST_BODY" > /dev/null 2>&1 || true

# Wait for provisioning
for i in $(seq 1 12); do
    CAPHOST_STATE=$(az rest --method GET \
        --url "${MGMT_API}${ACCOUNT_PATH}/capabilityHosts/default?api-version=${API_VERSION}" \
        --query "properties.provisioningState" -o tsv 2>/dev/null || echo "Creating")
    [[ "$CAPHOST_STATE" = "Succeeded" ]] && break
    echo "[INFO] CapHost state: $CAPHOST_STATE (attempt $i/12)..."
    sleep 15
done

echo "[OK] Account capability host: $CAPHOST_STATE"

# ── 4. Create Project-Level Capability Host ──────────────────────────────
echo ""
echo "═══ 4/5  Project Capability Host ═══"

if [[ "$SHARED_CAPHOST" = "true" ]]; then
    echo "[INFO] Using shared (account-level) capability host — skipping project caphost"
else
    PROJECT_CAPHOST_BODY=$(cat <<EOF
{
  "properties": {
    "capabilityHostKind": "Agents",
    "storageConnections": ["conn-storage"],
    "vectorStoreConnections": ["conn-search"],
    "threadStorageConnections": ["conn-cosmosdb"]
  }
}
EOF
    )

    echo "[INFO] Creating project capability host..."
    arm_rest PUT "${ACCOUNT_PATH}/projects/${PROJECT_NAME}/capabilityHosts/default" "$PROJECT_CAPHOST_BODY" > /dev/null 2>&1 || true

    for i in $(seq 1 12); do
        PROJ_CAPHOST_STATE=$(az rest --method GET \
            --url "${MGMT_API}${ACCOUNT_PATH}/projects/${PROJECT_NAME}/capabilityHosts/default?api-version=${API_VERSION}" \
            --query "properties.provisioningState" -o tsv 2>/dev/null || echo "Creating")
        [[ "$PROJ_CAPHOST_STATE" = "Succeeded" ]] && break
        echo "[INFO] Project CapHost state: $PROJ_CAPHOST_STATE (attempt $i/12)..."
        sleep 15
    done

    echo "[OK] Project capability host: $PROJ_CAPHOST_STATE"
fi

# ── 5. Create Agent via .NET v2 SDK ──────────────────────────────────────
echo ""
echo "═══ 5/5  Create Agent (v2 .NET SDK) ═══"

PROJECT_ENDPOINT="https://${FOUNDRY_ACCOUNT_NAME}.services.ai.azure.com/api/projects/${PROJECT_NAME}"

echo "[INFO] Project endpoint: $PROJECT_ENDPOINT"
echo "[INFO] Building agent-tool..."

cd "$AGENT_TOOL_DIR"
dotnet restore --verbosity quiet
dotnet build --verbosity quiet --configuration Release --no-restore

echo "[INFO] Creating agent..."
dotnet run --configuration Release --no-build -- \
    --endpoint "$PROJECT_ENDPOINT" \
    --model "$CHAT_DEPLOYMENT_NAME" \
    --search-connection "conn-search" \
    --index-name "$INDEX_NAME" \
    --embedding-model "$EMBEDDING_DEPLOYMENT_NAME" \
    --agent-name "sharepoint-knowledge-agent"

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   Project + Agent Deployed Successfully                         ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Project:         $PROJECT_NAME"
echo "║  Project Endpoint: $PROJECT_ENDPOINT"
echo "║  Agent:           sharepoint-knowledge-agent"
echo "║  Model:           $CHAT_DEPLOYMENT_NAME"
echo "║  Search Index:    $INDEX_NAME"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Test:  dotnet run --project agent-tool -- \\                    ║"
echo "║           --endpoint $PROJECT_ENDPOINT \\    "
echo "║           --test \"What documents are available?\"                ║"
echo "╚══════════════════════════════════════════════════════════════════╝"

# Append to outputs
cat >> "$SCRIPT_DIR/.foundry-outputs" <<EOF
PROJECT_NAME=$PROJECT_NAME
PROJECT_ENDPOINT=$PROJECT_ENDPOINT
EOF
