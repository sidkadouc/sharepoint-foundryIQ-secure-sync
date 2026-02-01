# Azure AI Search Deployment Artifacts

This directory contains JSON artifacts and deployment scripts for setting up Azure AI Search with:
- **OCR processing** for images in documents
- **Azure OpenAI embeddings** via Foundry for text vectorization
- **Integrated vectorization** for search-time query vectorization
- **ACL fields** for document-level security (optional)

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Azure Blob     │────▶│  Azure AI Search │────▶│  Azure OpenAI   │
│  Storage        │     │  (Indexer)       │     │  (Foundry)      │
│  (SharePoint    │     │                  │     │                 │
│   synced files) │     │  - OCR Skill     │     │  - text-embed-  │
│                 │     │  - Merge Skill   │     │    ada-002      │
│                 │     │  - Split Skill   │     │                 │
│                 │     │  - Embedding     │     │                 │
│                 │     │    Skill         │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

## Prerequisites

1. **Azure CLI** installed and logged in
2. **PowerShell 7+** installed
3. **Azure Resources**:
   - Azure AI Search service (Basic tier or higher)
   - Azure OpenAI resource with deployed models:
     - `text-embedding-ada-002` (or similar embedding model)
   - Azure Cognitive Services (for OCR)
   - Azure Blob Storage container with documents

4. **Role Assignments** (managed identity):
   - Search Service → Storage: `Storage Blob Data Reader`
   - Search Service → OpenAI: `Cognitive Services OpenAI User`
   - Search Service → Cognitive Services: `Cognitive Services User`

## Environment Variables

Set these environment variables before running:

```bash
# Azure AI Search
export SEARCH_SERVICE_NAME="your-search-service"
export SEARCH_RESOURCE_GROUP="your-resource-group"

# Storage
export STORAGE_ACCOUNT_NAME="your-storage-account"
export STORAGE_RESOURCE_GROUP="your-storage-rg"  # if different from search
export STORAGE_CONTAINER_NAME="your-container"

# Azure OpenAI (Foundry)
export OPENAI_RESOURCE_URI="https://your-openai.openai.azure.com"
export OPENAI_EMBEDDING_DEPLOYMENT="text-embedding-ada-002"
export OPENAI_EMBEDDING_MODEL="text-embedding-ada-002"
export OPENAI_EMBEDDING_DIMENSIONS="1536"

# Cognitive Services (for OCR)
export COGNITIVE_SERVICES_URI="https://your-cognitive-services.cognitiveservices.azure.com"

# Optional: Component names
export INDEX_NAME="sharepoint-index"
export INDEXER_NAME="sharepoint-indexer"
export SKILLSET_NAME="sharepoint-skillset"
export DATASOURCE_NAME="sharepoint-datasource"
```

## Deployment

### Using PowerShell Script

```powershell
./script.ps1 `
    -ResourceGroupName $env:SEARCH_RESOURCE_GROUP `
    -SearchServiceName $env:SEARCH_SERVICE_NAME `
    -StorageAccountName $env:STORAGE_ACCOUNT_NAME `
    -StorageResourceGroupName $env:STORAGE_RESOURCE_GROUP `
    -StorageContainerName $env:STORAGE_CONTAINER_NAME `
    -OpenAIResourceUri $env:OPENAI_RESOURCE_URI `
    -OpenAIDeploymentId $env:OPENAI_EMBEDDING_DEPLOYMENT `
    -OpenAIModelName $env:OPENAI_EMBEDDING_MODEL `
    -OpenAIEmbeddingDimensions $env:OPENAI_EMBEDDING_DIMENSIONS `
    -CognitiveServicesResourceUri $env:COGNITIVE_SERVICES_URI `
    -IndexName $env:INDEX_NAME `
    -IndexerName $env:INDEXER_NAME `
    -SkillsetName $env:SKILLSET_NAME `
    -DataSourceName $env:DATASOURCE_NAME
```

### Manual Deployment

Deploy components in order using Azure REST API:
1. Data Source (`datasource.json`)
2. Index (`index.json`)
3. Skillset (`skillset.json`)
4. Indexer (`indexer.json`)

## Components

### Data Source (`datasource.json`)
Connects to Azure Blob Storage using managed identity (ResourceId format).

### Index (`index.json`)
Search index with:
- `chunk_id`: Document key
- `chunk`: Text content (searchable)
- `title`: Document title
- `text_vector`: 1536-dimensional vector for semantic search
- `acl_user_ids`, `acl_group_ids`: Access control lists (filterable)

### Skillset (`skillset.json`)
AI enrichment pipeline:
1. **OcrSkill**: Extracts text from images
2. **MergeSkill**: Combines OCR text with document content
3. **SplitSkill**: Chunks text into pages (2000 chars, 200 overlap)
4. **AzureOpenAIEmbeddingSkill**: Generates embeddings via Foundry

### Indexer (`indexer.json`)
Orchestrates the indexing process with:
- Image extraction (`generateNormalizedImages`)
- Field mappings for metadata
- Index projections for chunked documents

## Testing

Run the test script to verify the deployment:

```bash
python3 test_search.py --query "your search query"
```

Or run individual tests:

```bash
# Set environment variables
export SEARCH_SERVICE_NAME="your-search-service"
export SEARCH_RESOURCE_GROUP="your-resource-group"
export INDEX_NAME="sharepoint-index"

# Run tests
python3 test_search.py
```

## Integrated Vectorization

The index is configured with an Azure OpenAI vectorizer that automatically vectorizes text queries at search time:

```json
{
  "vectorQueries": [{
    "kind": "text",
    "text": "your natural language query",
    "fields": "text_vector",
    "k": 5
  }]
}
```

This uses the same embedding model configured in the index vectorizer, eliminating the need to pre-vectorize queries.

## Troubleshooting

### Indexer shows 0 items processed
- Verify data source connection string has correct ResourceId
- Check role assignments for managed identity
- Ensure blobs exist in the container

### Search returns 0 results
- Check if `permissionFilterOption` is enabled (requires ACL context)
- Verify documents were indexed (check index stats)
- Try wildcard search `"search": "*"`

### Vector search not working
- Verify vectorizer is configured in index
- Check Azure OpenAI deployment is accessible
- Ensure `text_vector` field has correct dimensions

## License

MIT
