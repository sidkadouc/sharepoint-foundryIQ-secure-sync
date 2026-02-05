#!/bin/bash
# ==============================================================================
# SharePoint to Blob Sync with AI Search - Complete Pipeline
# ==============================================================================
# This script runs the full pipeline:
# 1. Sync files from SharePoint to Blob Storage
# 2. Create AI Search components (datasource, index, skillset, indexer)
# 3. Wait for indexing and run tests
#
# Usage: ./run-all.sh
# ==============================================================================

set -e

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "✓ Loaded environment variables from .env"
else
    echo "ERROR: .env file not found"
    exit 1
fi

# Validate required variables
REQUIRED_VARS=(
    "SHAREPOINT_SITE_URL"
    "AZURE_STORAGE_ACCOUNT_NAME"
    "AZURE_BLOB_CONTAINER_NAME"
    "SEARCH_SERVICE_NAME"
    "SEARCH_API_KEY"
    "OPENAI_RESOURCE_URI"
    "EMBEDDING_DEPLOYMENT_ID"
    "SUBSCRIPTION_ID"
    "SEARCH_RESOURCE_GROUP"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        echo "ERROR: Required variable $var is not set"
        exit 1
    fi
done
echo "✓ All required variables present"

# ==============================================================================
# Step 1: Sync SharePoint to Blob
# ==============================================================================
echo ""
echo "============================================================"
echo "  Step 1: Syncing SharePoint to Blob Storage"
echo "============================================================"

cd sync
pip install -q -r requirements.txt
python main.py 2>&1 | tail -5
cd ..

echo "✓ SharePoint sync completed"

# ==============================================================================
# Step 2: Create AI Search Components
# ==============================================================================
echo ""
echo "============================================================"
echo "  Step 2: Creating AI Search Components"
echo "============================================================"

SEARCH_ENDPOINT="https://${SEARCH_SERVICE_NAME}.search.windows.net"
API_VERSION="${API_VERSION:-2025-11-01-preview}"

# Helper function to create component
create_component() {
    local type=$1
    local name=$2
    local file=$3
    
    echo "Creating $type: $name..."
    
    # Build sed command for variable substitution
    local result=$(sed \
        -e "s|\${dataSourceName}|${DATASOURCE_NAME}|g" \
        -e "s|\${subscriptionId}|${SUBSCRIPTION_ID}|g" \
        -e "s|\${resourceGroup}|${SEARCH_RESOURCE_GROUP}|g" \
        -e "s|\${storageAccount}|${AZURE_STORAGE_ACCOUNT_NAME}|g" \
        -e "s|\${containerName}|${AZURE_BLOB_CONTAINER_NAME}|g" \
        -e "s|\${indexName}|${INDEX_NAME}|g" \
        -e "s|\${indexerName}|${INDEXER_NAME}|g" \
        -e "s|\${skillsetName}|${SKILLSET_NAME}|g" \
        -e "s|\${embeddingDimensions}|${EMBEDDING_DIMENSIONS}|g" \
        -e "s|\${openAIResourceUri}|${OPENAI_RESOURCE_URI}|g" \
        -e "s|\${embeddingDeploymentId}|${EMBEDDING_DEPLOYMENT_ID}|g" \
        -e "s|\${embeddingModelName}|${EMBEDDING_MODEL_NAME}|g" \
        "$file" | \
    curl -s -X PUT "${SEARCH_ENDPOINT}/${type}/${name}?api-version=${API_VERSION}" \
        -H "api-key: ${SEARCH_API_KEY}" \
        -H "Content-Type: application/json" \
        -d @-)
    
    local created_name=$(echo "$result" | jq -r '.name // empty')
    local error=$(echo "$result" | jq -r '.error.message // empty')
    
    if [ -n "$created_name" ]; then
        echo "  ✓ Created: $created_name"
    elif [ -n "$error" ]; then
        echo "  ✗ Error: $error"
        return 1
    fi
}

# Create components in order
create_component "datasources" "${DATASOURCE_NAME}" "ai-search/datasource.json"
create_component "indexes" "${INDEX_NAME}" "ai-search/index.json"
create_component "skillsets" "${SKILLSET_NAME}" "ai-search/skillset.json"
create_component "indexers" "${INDEXER_NAME}" "ai-search/indexer.json"

echo "✓ AI Search components created"

# ==============================================================================
# Step 3: Wait for Indexing
# ==============================================================================
echo ""
echo "============================================================"
echo "  Step 3: Waiting for Indexer"
echo "============================================================"

for i in {1..12}; do
    sleep 5
    status=$(curl -s "${SEARCH_ENDPOINT}/indexers/${INDEXER_NAME}/status?api-version=${API_VERSION}" \
        -H "api-key: ${SEARCH_API_KEY}" | jq -r '.lastResult.status // "running"')
    items=$(curl -s "${SEARCH_ENDPOINT}/indexers/${INDEXER_NAME}/status?api-version=${API_VERSION}" \
        -H "api-key: ${SEARCH_API_KEY}" | jq -r '.lastResult.itemsProcessed // 0')
    echo "  Indexer status: $status (items processed: $items)"
    if [ "$status" = "success" ]; then
        break
    fi
done

# Get document count
doc_count=$(curl -s "${SEARCH_ENDPOINT}/indexes/${INDEX_NAME}/docs/\$count?api-version=${API_VERSION}" \
    -H "api-key: ${SEARCH_API_KEY}")
echo "✓ Documents indexed: $doc_count"

# ==============================================================================
# Step 4: Run Tests
# ==============================================================================
echo ""
echo "============================================================"
echo "  Step 4: Running Search Tests"
echo "============================================================"

cd tests
python test_search.py -q "demo" 2>&1 | grep -E "(Document count|Vector Search|Total results)"
cd ..

echo ""
echo "============================================================"
echo "  ✓ Pipeline Complete!"
echo "============================================================"
echo ""
echo "Summary:"
echo "  - SharePoint files synced to: ${AZURE_STORAGE_ACCOUNT_NAME}/${AZURE_BLOB_CONTAINER_NAME}"
echo "  - AI Search index: ${INDEX_NAME}"
echo "  - Documents indexed: $doc_count"
