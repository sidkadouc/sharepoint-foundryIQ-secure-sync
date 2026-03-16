#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Deploy the Foundry instance + private network infrastructure.
#
# Creates:  VNet, subnets, Storage, AI Search, Foundry Account (AIServices),
#           Cosmos DB — all with private endpoints + DNS zones.
#           Deploys gpt-4o + text-embedding-3-large models.
#
# Usage:  ./deploy-foundry.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source .env first (base), then .env.private (overrides). Exported env vars override both.
[[ -f "$ROOT_DIR/.env" ]]         && { set -a; source "$ROOT_DIR/.env"; set +a; }
[[ -f "$ROOT_DIR/.env.private" ]] && { set -a; source "$ROOT_DIR/.env.private"; set +a; }

# ── Config ──────────────────────────────────────────────────────────────────
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:?SUBSCRIPTION_ID is required}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-spsync-private}"
LOCATION="${LOCATION:-swedencentral}"

VNET_NAME="${VNET_NAME:-vnet-spsync}"
VNET_ADDRESS_PREFIX="${VNET_ADDRESS_PREFIX:-10.0.0.0/16}"
SUBNET_PE_PREFIX="${SUBNET_PE_PREFIX:-10.0.1.0/24}"
SUBNET_SYNC_PREFIX="${SUBNET_SYNC_PREFIX:-10.0.2.0/24}"
SUBNET_AGENT_PREFIX="${SUBNET_AGENT_PREFIX:-10.0.3.0/24}"

# Names (auto-generated from subscription prefix if blank)
SUFFIX="$(echo "$SUBSCRIPTION_ID" | cut -c1-8 | tr -d '-')"
FOUNDRY_ACCOUNT_NAME="${FOUNDRY_ACCOUNT_NAME:-foundry-spsync-${SUFFIX}}"
AZURE_STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:-stspsync${SUFFIX}}"
AZURE_STORAGE_ACCOUNT_NAME="${AZURE_STORAGE_ACCOUNT_NAME:0:24}"
SEARCH_SERVICE_NAME="${SEARCH_SERVICE_NAME:-srch-spsync-${SUFFIX}}"
COSMOSDB_ACCOUNT_NAME="${COSMOSDB_ACCOUNT_NAME:-cosmos-spsync-${SUFFIX}}"

AZURE_BLOB_CONTAINER_NAME="${AZURE_BLOB_CONTAINER_NAME:-sharepoint-sync}"
EMBEDDING_DEPLOYMENT_NAME="${EMBEDDING_DEPLOYMENT_NAME:-text-embedding-3-large}"
CHAT_DEPLOYMENT_NAME="${CHAT_DEPLOYMENT_NAME:-gpt-4o}"

command -v az >/dev/null 2>&1 || { echo "[ERROR] az CLI required" >&2; exit 1; }

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   Step 1: Deploy Foundry Instance + Private Infrastructure       ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Subscription:   $SUBSCRIPTION_ID"
echo "║  Resource Group:  $RESOURCE_GROUP"
echo "║  Location:        $LOCATION"
echo "║  VNet:            $VNET_NAME ($VNET_ADDRESS_PREFIX)"
echo "║  Foundry Account: $FOUNDRY_ACCOUNT_NAME"
echo "║  Storage:         $AZURE_STORAGE_ACCOUNT_NAME"
echo "║  AI Search:       $SEARCH_SERVICE_NAME"
echo "║  Cosmos DB:       $COSMOSDB_ACCOUNT_NAME"
echo "╚══════════════════════════════════════════════════════════════════╝"

az account set --subscription "$SUBSCRIPTION_ID"

# ── Register providers ────────────────────────────────────────────────────
echo ""
echo "═══ 0/8  Register providers ═══"
for ns in Microsoft.CognitiveServices Microsoft.App Microsoft.ContainerService \
           Microsoft.Network Microsoft.Search Microsoft.Storage \
           Microsoft.MachineLearningServices; do
    az provider register --namespace "$ns" -o none 2>/dev/null &
done
wait
echo "[OK] Provider registration initiated"

# ── 1. Resource Group ─────────────────────────────────────────────────────
echo ""
echo "═══ 1/8  Resource Group ═══"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none
echo "[OK] $RESOURCE_GROUP"

# ── 2. Virtual Network + Subnets ──────────────────────────────────────────
echo ""
echo "═══ 2/8  Virtual Network ═══"

az network vnet create \
    --name "$VNET_NAME" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --address-prefixes "$VNET_ADDRESS_PREFIX" -o none

az network vnet subnet create \
    --name "snet-private-endpoints" --vnet-name "$VNET_NAME" -g "$RESOURCE_GROUP" \
    --address-prefixes "$SUBNET_PE_PREFIX" -o none

az network vnet subnet create \
    --name "snet-sync" --vnet-name "$VNET_NAME" -g "$RESOURCE_GROUP" \
    --address-prefixes "$SUBNET_SYNC_PREFIX" \
    --delegations "Microsoft.Web/serverFarms" -o none

az network vnet subnet create \
    --name "snet-agent" --vnet-name "$VNET_NAME" -g "$RESOURCE_GROUP" \
    --address-prefixes "$SUBNET_AGENT_PREFIX" \
    --delegations "Microsoft.App/environments" -o none

VNET_ID=$(az network vnet show --name "$VNET_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)
AGENT_SUBNET_ID=$(az network vnet subnet show --name "snet-agent" --vnet-name "$VNET_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)
echo "[OK] VNet: $VNET_NAME (3 subnets)"

# ── 3. Storage Account ───────────────────────────────────────────────────
echo ""
echo "═══ 3/8  Storage Account ═══"

az storage account create \
    -n "$AZURE_STORAGE_ACCOUNT_NAME" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 \
    --allow-blob-public-access false --default-action Deny \
    --min-tls-version TLS1_2 -o none

STORAGE_ID=$(az storage account show -n "$AZURE_STORAGE_ACCOUNT_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)

az storage account network-rule update \
    -n "$AZURE_STORAGE_ACCOUNT_NAME" -g "$RESOURCE_GROUP" --bypass AzureServices -o none 2>/dev/null || true

az network private-endpoint create \
    --name "pe-${AZURE_STORAGE_ACCOUNT_NAME}-blob" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --vnet-name "$VNET_NAME" --subnet "snet-private-endpoints" \
    --private-connection-resource-id "$STORAGE_ID" \
    --group-ids blob --connection-name "pec-storage-blob" -o none

echo "[OK] Storage: $AZURE_STORAGE_ACCOUNT_NAME"

# ── 4. AI Search ─────────────────────────────────────────────────────────
echo ""
echo "═══ 4/8  AI Search ═══"

az search service create \
    --name "$SEARCH_SERVICE_NAME" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --sku standard --partition-count 1 --replica-count 1 \
    --public-access disabled --auth-options aadOrApiKey \
    --identity-type SystemAssigned -o none

SEARCH_ID=$(az search service show --name "$SEARCH_SERVICE_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)
SEARCH_PRINCIPAL=$(az search service show --name "$SEARCH_SERVICE_NAME" -g "$RESOURCE_GROUP" --query identity.principalId -o tsv)

az network private-endpoint create \
    --name "pe-${SEARCH_SERVICE_NAME}" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --vnet-name "$VNET_NAME" --subnet "snet-private-endpoints" \
    --private-connection-resource-id "$SEARCH_ID" \
    --group-ids searchService --connection-name "pec-search" -o none

# RBAC: Search → Storage
az role assignment create --assignee "$SEARCH_PRINCIPAL" \
    --role "Storage Blob Data Reader" --scope "$STORAGE_ID" -o none 2>/dev/null || true

echo "[OK] AI Search: $SEARCH_SERVICE_NAME"

# ── 5. Foundry Account (AIServices) ──────────────────────────────────────
echo ""
echo "═══ 5/8  Foundry Account (AIServices) ═══"

az cognitiveservices account create \
    -n "$FOUNDRY_ACCOUNT_NAME" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --kind AIServices --sku S0 \
    --custom-domain "$FOUNDRY_ACCOUNT_NAME" \
    --assign-identity -o none

FOUNDRY_ID=$(az cognitiveservices account show -n "$FOUNDRY_ACCOUNT_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)
FOUNDRY_ENDPOINT=$(az cognitiveservices account show -n "$FOUNDRY_ACCOUNT_NAME" -g "$RESOURCE_GROUP" --query properties.endpoint -o tsv)

# Configure network injection for Standard private Agent setup (BYO VNet)
set +e
az rest --method PATCH \
    --url "https://management.azure.com${FOUNDRY_ID}?api-version=2025-06-01" \
    --body "{\"properties\":{\"allowProjectManagement\":true,\"publicNetworkAccess\":\"Disabled\",\"networkInjections\":[{\"scenario\":\"agent\",\"subnetArmId\":\"${AGENT_SUBNET_ID}\",\"useMicrosoftManagedNetwork\":false}]}}" \
    -o none
PATCH_RC=$?
set -e
if [[ $PATCH_RC -ne 0 ]]; then
    echo "[WARN] Foundry network injection patch failed (API/version support may vary in tenant)."
fi

az network private-endpoint create \
    --name "pe-${FOUNDRY_ACCOUNT_NAME}" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --vnet-name "$VNET_NAME" --subnet "snet-private-endpoints" \
    --private-connection-resource-id "$FOUNDRY_ID" \
    --group-ids account --connection-name "pec-foundry" -o none

# Deploy models on the Foundry account
echo "[INFO] Deploying $EMBEDDING_DEPLOYMENT_NAME model"
az cognitiveservices account deployment create \
    -n "$FOUNDRY_ACCOUNT_NAME" -g "$RESOURCE_GROUP" \
    --deployment-name "$EMBEDDING_DEPLOYMENT_NAME" \
    --model-name text-embedding-3-large --model-version "1" \
    --model-format OpenAI --sku-capacity 10 --sku-name Standard \
    -o none 2>/dev/null || echo "[WARN] Embedding deployment may already exist"

echo "[INFO] Deploying $CHAT_DEPLOYMENT_NAME model"
az cognitiveservices account deployment create \
    -n "$FOUNDRY_ACCOUNT_NAME" -g "$RESOURCE_GROUP" \
    --deployment-name "$CHAT_DEPLOYMENT_NAME" \
    --model-name gpt-4o --model-version "2024-11-20" \
    --model-format OpenAI --sku-capacity 10 --sku-name GlobalStandard \
    -o none 2>/dev/null || echo "[WARN] Chat deployment may already exist"

# RBAC: Search → Foundry
az role assignment create --assignee "$SEARCH_PRINCIPAL" \
    --role "Cognitive Services OpenAI User" --scope "$FOUNDRY_ID" -o none 2>/dev/null || true

echo "[OK] Foundry: $FOUNDRY_ACCOUNT_NAME (AIServices, models deployed)"

# ── 6. Cosmos DB ─────────────────────────────────────────────────────────
echo ""
echo "═══ 6/8  Cosmos DB ═══"

az cosmosdb create \
    -n "$COSMOSDB_ACCOUNT_NAME" -g "$RESOURCE_GROUP" \
    --locations regionName="$LOCATION" failoverPriority=0 \
    --default-consistency-level Session \
    --public-network-access DISABLED \
    --enable-automatic-failover false -o none

COSMOS_ID=$(az cosmosdb show -n "$COSMOSDB_ACCOUNT_NAME" -g "$RESOURCE_GROUP" --query id -o tsv)

az network private-endpoint create \
    --name "pe-${COSMOSDB_ACCOUNT_NAME}" -g "$RESOURCE_GROUP" -l "$LOCATION" \
    --vnet-name "$VNET_NAME" --subnet "snet-private-endpoints" \
    --private-connection-resource-id "$COSMOS_ID" \
    --group-ids Sql --connection-name "pec-cosmos" -o none

echo "[OK] Cosmos DB: $COSMOSDB_ACCOUNT_NAME"

# ── 8. Private DNS Zones ─────────────────────────────────────────────────
echo ""
echo "═══ 7/8  Private DNS Zones ═══"

declare -A PE_DNS_MAP=(
    ["pe-${AZURE_STORAGE_ACCOUNT_NAME}-blob"]="privatelink.blob.core.windows.net"
    ["pe-${SEARCH_SERVICE_NAME}"]="privatelink.search.windows.net"
    ["pe-${COSMOSDB_ACCOUNT_NAME}"]="privatelink.documents.azure.com"
)

DNS_ZONES=(
    "privatelink.blob.core.windows.net"
    "privatelink.search.windows.net"
    "privatelink.cognitiveservices.azure.com"
    "privatelink.openai.azure.com"
    "privatelink.services.ai.azure.com"
    "privatelink.documents.azure.com"
    "privatelink.file.core.windows.net"
)

for ZONE in "${DNS_ZONES[@]}"; do
    ZONE_SAFE=$(echo "$ZONE" | tr '.' '-')
    az network private-dns zone create --name "$ZONE" -g "$RESOURCE_GROUP" -o none 2>/dev/null || true
    az network private-dns link vnet create \
        --name "link-${ZONE_SAFE}" --zone-name "$ZONE" -g "$RESOURCE_GROUP" \
        --virtual-network "$VNET_ID" --registration-enabled false -o none 2>/dev/null || true
done

for PE_NAME in "${!PE_DNS_MAP[@]}"; do
    DNS_ZONE="${PE_DNS_MAP[$PE_NAME]}"
    DNS_ZONE_ID=$(az network private-dns zone show --name "$DNS_ZONE" -g "$RESOURCE_GROUP" --query id -o tsv)
    az network private-endpoint dns-zone-group create \
        --name "default" --endpoint-name "$PE_NAME" -g "$RESOURCE_GROUP" \
        --private-dns-zone "$DNS_ZONE_ID" --zone-name "${DNS_ZONE//\./-}" \
        -o none 2>/dev/null || true
done

# Foundry PE requires 3 DNS zones: cognitiveservices + openai + services.ai
for ZONE in "privatelink.cognitiveservices.azure.com" "privatelink.openai.azure.com" "privatelink.services.ai.azure.com"; do
    DNS_ZONE_ID=$(az network private-dns zone show --name "$ZONE" -g "$RESOURCE_GROUP" --query id -o tsv)
    az network private-endpoint dns-zone-group add \
        --resource-group "$RESOURCE_GROUP" \
        --endpoint-name "pe-${FOUNDRY_ACCOUNT_NAME}" \
        --name "default" \
        --zone-name "${ZONE//\./-}" \
        --private-dns-zone "$DNS_ZONE_ID" \
        -o none 2>/dev/null || true
done

echo "[OK] DNS zones created and linked"

# ── 9. Blob container ────────────────────────────────────────────────────
echo ""
echo "═══ 8/8  Blob Container ═══"

DEPLOYER_IP=$(curl -s https://api.ipify.org || echo "")
if [[ -n "$DEPLOYER_IP" ]]; then
    az storage account network-rule add \
        -n "$AZURE_STORAGE_ACCOUNT_NAME" -g "$RESOURCE_GROUP" \
        --ip-address "$DEPLOYER_IP" -o none 2>/dev/null || true
    sleep 10
fi

az storage container create \
    --name "$AZURE_BLOB_CONTAINER_NAME" \
    --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
    --auth-mode login -o none 2>/dev/null || echo "[WARN] Container may already exist"

if [[ -n "$DEPLOYER_IP" ]]; then
    az storage account network-rule remove \
        -n "$AZURE_STORAGE_ACCOUNT_NAME" -g "$RESOURCE_GROUP" \
        --ip-address "$DEPLOYER_IP" -o none 2>/dev/null || true
fi

echo "[OK] Container: $AZURE_BLOB_CONTAINER_NAME"

# ── Outputs ──────────────────────────────────────────────────────────────
cat > "$SCRIPT_DIR/.foundry-outputs" <<EOF
SUBSCRIPTION_ID=$SUBSCRIPTION_ID
RESOURCE_GROUP=$RESOURCE_GROUP
LOCATION=$LOCATION
VNET_NAME=$VNET_NAME
VNET_ID=$VNET_ID
FOUNDRY_ACCOUNT_NAME=$FOUNDRY_ACCOUNT_NAME
FOUNDRY_ID=$FOUNDRY_ID
FOUNDRY_ENDPOINT=$FOUNDRY_ENDPOINT
AZURE_STORAGE_ACCOUNT_NAME=$AZURE_STORAGE_ACCOUNT_NAME
STORAGE_ID=$STORAGE_ID
SEARCH_SERVICE_NAME=$SEARCH_SERVICE_NAME
SEARCH_ID=$SEARCH_ID
SEARCH_PRINCIPAL=$SEARCH_PRINCIPAL
COSMOSDB_ACCOUNT_NAME=$COSMOSDB_ACCOUNT_NAME
COSMOS_ID=$COSMOS_ID
AGENT_SUBNET_ID=$AGENT_SUBNET_ID
EMBEDDING_DEPLOYMENT_NAME=$EMBEDDING_DEPLOYMENT_NAME
CHAT_DEPLOYMENT_NAME=$CHAT_DEPLOYMENT_NAME
AZURE_BLOB_CONTAINER_NAME=$AZURE_BLOB_CONTAINER_NAME
EOF

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   Foundry Instance Deployed Successfully                        ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Foundry Account: $FOUNDRY_ACCOUNT_NAME"
echo "║  Endpoint:        $FOUNDRY_ENDPOINT"
echo "║  VNet:            $VNET_NAME"
echo "║  Models:          $CHAT_DEPLOYMENT_NAME, $EMBEDDING_DEPLOYMENT_NAME"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Next: ./deploy-project.sh  (create project + agent)            ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
