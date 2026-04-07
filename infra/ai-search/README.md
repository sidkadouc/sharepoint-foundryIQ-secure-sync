# Azure AI Search Deployment

JSON artifacts and scripts for deploying Azure AI Search with OCR, chunking, vector embeddings, and ACL filtering.

## Architecture

```
Azure Blob Storage ──▶ Indexer ──▶ Skillset Pipeline ──▶ Search Index
  (synced files +          │        1. OCR (images)         - text chunks
   ACL metadata)           │        2. Merge text           - vector embeddings
                           │        3. Split (2000 chars)   - ACL fields
                           │        4. Embed (OpenAI)
                           ▼
                      Azure OpenAI
                      (text-embedding-3-large)
```

## Prerequisites

- Azure CLI (`az login`)
- PowerShell 7+
- Azure AI Search (Basic tier+)
- Azure OpenAI with embedding model deployed
- Azure Cognitive Services (for OCR)
- Storage account with synced blobs

### Role Assignments (Search managed identity)

| Target Resource | Role |
|----------------|------|
| Storage Account | `Storage Blob Data Reader` |
| Azure OpenAI | `Cognitive Services OpenAI User` |
| Cognitive Services | `Cognitive Services User` |

## Components

| File | Description |
|------|-------------|
| `datasource.json` | Blob storage connection (managed identity) |
| `index.json` | Search index schema with vector + ACL fields |
| `skillset.json` | OCR → Merge → Split → Embed pipeline |
| `indexer.json` | Orchestration with index projections for chunks |
| `script.ps1` | PowerShell deployment script |

## Index Fields

| Field | Type | Purpose |
|-------|------|---------|
| `chunk_id` | String (key) | Unique chunk identifier |
| `chunk` | String | Text content |
| `title` | String | Document title |
| `text_vector` | Collection(Single) | Embedding vector (3072 dims) |
| `acl_user_ids` | String | Pipe-delimited user Entra IDs |
| `acl_group_ids` | String | Pipe-delimited group Entra IDs |

## Deploy

```powershell
./script.ps1 `
  -ResourceGroupName "your-rg" `
  -SearchServiceName "your-search" `
  -StorageAccountName "yourstorage" `
  -StorageContainerName "sharepoint-sync" `
  -OpenAIResourceUri "https://your-openai.openai.azure.com" `
  -OpenAIDeploymentId "text-embedding-3-large" `
  -CognitiveServicesResourceUri "https://your-cognitive.cognitiveservices.azure.com"
```

Or deploy manually in order: datasource → index → skillset → indexer.

## Environment Variables

```bash
export SEARCH_SERVICE_NAME="your-search-service"
export SEARCH_RESOURCE_GROUP="your-resource-group"
export STORAGE_ACCOUNT_NAME="your-storage-account"
export STORAGE_CONTAINER_NAME="your-container"
export OPENAI_RESOURCE_URI="https://your-openai.openai.azure.com"
export OPENAI_EMBEDDING_DEPLOYMENT="text-embedding-3-large"
export COGNITIVE_SERVICES_URI="https://your-cognitive.cognitiveservices.azure.com"
```

## ACL Propagation

The skillset propagates blob metadata ACLs to each chunk:

1. Blob has metadata: `user_ids=id1|id2`, `group_ids=g1|g2`
2. Skillset reads `/document/user_ids` and `/document/group_ids`
3. Index projections copy ACLs to every chunk of that document

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Indexer: 0 items | Check datasource connection, verify blobs exist |
| Vector search fails | Verify OpenAI deployment, check dimensions match |
| ACLs not filtering | Ensure `SYNC_PERMISSIONS=true` during sync, then reindex |
