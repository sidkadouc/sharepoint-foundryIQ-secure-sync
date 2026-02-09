# Tests — Azure AI Search Verification

Scripts to verify the Azure AI Search deployment. Tests index statistics, indexer status, and all search modes.

## Usage

```bash
# Install dependencies (uses only stdlib — no pip install needed)
python test_search.py

# Custom query
python test_search.py --query "your search query"

# Override service/index
python test_search.py --service my-search --index my-index --resource-group my-rg
```

## What It Tests

| # | Test | Description |
|---|------|-------------|
| 1 | Index statistics | Document count, storage size, vector index size |
| 2 | Keyword search | Basic full-text search |
| 3 | Vector search | Integrated vectorization (text → vector at query time) |
| 4 | Hybrid search | Keyword + vector combined |
| 5 | Semantic search | Semantic reranking with extractive answers |
| 6 | Indexer status | Last run status, items processed/failed, errors |
| 7 | Vectorizer config | Verify Azure OpenAI vectorizer setup |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SEARCH_SERVICE_NAME` | — | AI Search service name |
| `SEARCH_RESOURCE_GROUP` | — | Resource group (for CLI key retrieval) |
| `SEARCH_API_KEY` | — | Admin key (or auto-retrieved via `az` CLI) |
| `INDEX_NAME` | `sharepoint-index` | Index name |
| `API_VERSION` | `2025-11-01-preview` | Search API version |

## Authentication

The script tries `SEARCH_API_KEY` first, then falls back to retrieving the admin key via Azure CLI (`az search admin-key show`).

## Files

| File | Description |
|------|-------------|
| `test_search.py` | Search verification script (all modes) |
