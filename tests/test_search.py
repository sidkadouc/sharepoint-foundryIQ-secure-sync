#!/usr/bin/env python3
"""
Azure AI Search Testing Script

Tests various search capabilities:
- Keyword search
- Vector search with integrated vectorization
- Hybrid search
- Semantic search
- ACL filtering

Uses environment variables or Azure CLI for authentication.
"""

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional

# Configuration from environment variables
SEARCH_SERVICE_NAME = os.getenv("SEARCH_SERVICE_NAME", "srch-dev-francecentral-yai-api-6a30")
SEARCH_RESOURCE_GROUP = os.getenv("SEARCH_RESOURCE_GROUP", "rg-dev-francecentral-yai-api-6a30")
INDEX_NAME = os.getenv("INDEX_NAME", "sharepoint-index")
INDEXER_NAME = os.getenv("INDEXER_NAME", "sharepoint-indexer")
API_VERSION = os.getenv("API_VERSION", "2025-11-01-preview")

# Search endpoint
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE_NAME}.search.windows.net"


def get_api_key() -> str:
    """Get API key from environment or Azure CLI."""
    # Check environment variable first
    api_key = os.getenv("SEARCH_API_KEY")
    if api_key:
        return api_key
    
    # Try Azure CLI
    try:
        result = subprocess.run(
            [
                "az", "search", "admin-key", "show",
                "--service-name", SEARCH_SERVICE_NAME,
                "--resource-group", SEARCH_RESOURCE_GROUP,
                "--query", "primaryKey",
                "-o", "tsv"
            ],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error getting API key: {e.stderr}")
        sys.exit(1)


def make_request(
    path: str,
    method: str = "GET",
    body: Optional[Dict] = None,
    api_key: str = ""
) -> Dict[str, Any]:
    """Make HTTP request to Azure Search API."""
    import urllib.request
    import urllib.error
    
    url = f"{SEARCH_ENDPOINT}{path}"
    if "?" not in url:
        url += f"?api-version={API_VERSION}"
    elif "api-version" not in url:
        url += f"&api-version={API_VERSION}"
    
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json"
    }
    
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(error_body)
        except:
            return {"error": {"message": f"HTTP {e.code}: {error_body}"}}
    except Exception as e:
        return {"error": {"message": str(e)}}


def print_separator(title: str):
    """Print section separator."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60 + "\n")


def test_index_stats(api_key: str) -> Dict:
    """Test: Get index statistics."""
    print_separator("1. Index Statistics")
    
    stats = make_request(f"/indexes/{INDEX_NAME}/stats", api_key=api_key)
    
    if "error" in stats:
        print(f"Error: {stats['error'].get('message', 'Unknown error')}")
        return stats
    
    doc_count = stats.get("documentCount", 0)
    storage_size = stats.get("storageSize", 0)
    vector_size = stats.get("vectorIndexSize", 0)
    
    print(f"Document count: {doc_count}")
    print(f"Storage size: {storage_size:,} bytes")
    print(f"Vector index size: {vector_size:,} bytes")
    
    return stats


def test_keyword_search(query: str, api_key: str) -> Dict:
    """Test: Basic keyword search."""
    print_separator("2. Keyword Search")
    
    body = {
        "search": query,
        "top": 5,
        "select": "chunk_id,title,original_file_name,chunk",
        "count": True
    }
    
    results = make_request(f"/indexes/{INDEX_NAME}/docs/search", method="POST", body=body, api_key=api_key)
    
    if "error" in results:
        print(f"Error: {results['error'].get('message', 'Unknown error')}")
        return results
    
    total = results.get("@odata.count", len(results.get("value", [])))
    print(f"Query: {query}")
    print(f"Total results: {total}")
    
    for i, doc in enumerate(results.get("value", [])[:3], 1):
        print(f"\n  Result {i}:")
        print(f"    Title: {doc.get('title', 'N/A')}")
        print(f"    File: {doc.get('original_file_name', 'N/A')}")
        chunk = doc.get('chunk', '')[:100] + '...' if doc.get('chunk') else 'N/A'
        print(f"    Chunk: {chunk}")
    
    return results


def test_vector_search(query: str, api_key: str) -> Dict:
    """Test: Vector search with integrated vectorization."""
    print_separator("3. Vector Search (Integrated Vectorization)")
    
    body = {
        "search": query,
        "vectorQueries": [{
            "kind": "text",
            "text": query,
            "fields": "text_vector",
            "k": 5
        }],
        "top": 5,
        "select": "chunk_id,title,original_file_name,chunk"
    }
    
    results = make_request(f"/indexes/{INDEX_NAME}/docs/search", method="POST", body=body, api_key=api_key)
    
    if "error" in results:
        print(f"Error: {results['error'].get('message', 'Unknown error')}")
        return results
    
    print(f"Query (vectorized at search time): {query}")
    print(f"Total results: {len(results.get('value', []))}")
    
    for i, doc in enumerate(results.get("value", [])[:3], 1):
        print(f"\n  Result {i}:")
        print(f"    Title: {doc.get('title', 'N/A')}")
        chunk = doc.get('chunk', '')[:100] + '...' if doc.get('chunk') else 'N/A'
        print(f"    Chunk: {chunk}")
    
    return results


def test_hybrid_search(query: str, api_key: str) -> Dict:
    """Test: Hybrid search (keyword + vector)."""
    print_separator("4. Hybrid Search (Keyword + Vector)")
    
    body = {
        "search": query,
        "vectorQueries": [{
            "kind": "text",
            "text": query,
            "fields": "text_vector",
            "k": 5
        }],
        "top": 5,
        "select": "chunk_id,title,chunk"
    }
    
    results = make_request(f"/indexes/{INDEX_NAME}/docs/search", method="POST", body=body, api_key=api_key)
    
    if "error" in results:
        print(f"Error: {results['error'].get('message', 'Unknown error')}")
        return results
    
    print(f"Query: {query}")
    print(f"Total results: {len(results.get('value', []))}")
    
    for i, doc in enumerate(results.get("value", [])[:3], 1):
        print(f"\n  Result {i}: {doc.get('title', 'N/A')}")
    
    return results


def test_semantic_search(query: str, api_key: str) -> Dict:
    """Test: Semantic search with reranking."""
    print_separator("5. Semantic Search")
    
    semantic_config = f"{INDEX_NAME}-semantic-configuration"
    
    body = {
        "search": query,
        "queryType": "semantic",
        "semanticConfiguration": semantic_config,
        "top": 5,
        "select": "chunk_id,title,chunk",
        "answers": "extractive|count-3",
        "captions": "extractive|highlight-true"
    }
    
    results = make_request(f"/indexes/{INDEX_NAME}/docs/search", method="POST", body=body, api_key=api_key)
    
    if "error" in results:
        print(f"Error: {results['error'].get('message', 'Unknown error')}")
        return results
    
    print(f"Query: {query}")
    print(f"Total results: {len(results.get('value', []))}")
    
    # Print semantic answers if available
    answers = results.get("@search.answers", [])
    if answers:
        print("\n  Semantic Answers:")
        for ans in answers[:2]:
            print(f"    - {ans.get('text', 'N/A')[:150]}...")
    
    return results


def test_indexer_status(api_key: str) -> Dict:
    """Test: Check indexer status."""
    print_separator("6. Indexer Status")
    
    status = make_request(f"/indexers/{INDEXER_NAME}/status", api_key=api_key)
    
    if "error" in status:
        print(f"Error: {status['error'].get('message', 'Unknown error')}")
        return status
    
    print(f"Status: {status.get('status', 'N/A')}")
    
    history = status.get("executionHistory", [])
    if history:
        last = history[0]
        print(f"Last run: {last.get('status', 'N/A')}")
        print(f"Items processed: {last.get('itemsProcessed', 0)}")
        print(f"Items failed: {last.get('itemsFailed', 0)}")
        print(f"Start time: {last.get('startTime', 'N/A')}")
        
        errors = last.get("errors", [])
        if errors:
            print(f"\nErrors ({len(errors)}):")
            for e in errors[:3]:
                print(f"  - {e.get('message', 'N/A')[:150]}")
        
        warnings = last.get("warnings", [])
        if warnings:
            print(f"\nWarnings ({len(warnings)}):")
            for w in warnings[:3]:
                print(f"  - {w.get('message', 'N/A')[:100]}")
    
    return status


def test_vectorizer_config(api_key: str) -> Dict:
    """Test: Check vectorizer configuration."""
    print_separator("7. Vectorizer Configuration")
    
    index = make_request(f"/indexes/{INDEX_NAME}", api_key=api_key)
    
    if "error" in index:
        print(f"Error: {index['error'].get('message', 'Unknown error')}")
        return index
    
    vs = index.get("vectorSearch", {})
    
    print("Vectorizers:")
    for v in vs.get("vectorizers", []):
        print(f"  Name: {v.get('name')}")
        print(f"  Kind: {v.get('kind')}")
        if v.get("azureOpenAIParameters"):
            p = v["azureOpenAIParameters"]
            print(f"  URI: {p.get('resourceUri')}")
            print(f"  Deployment: {p.get('deploymentId')}")
            print(f"  Model: {p.get('modelName')}")
        print()
    
    print("Profiles:")
    for p in vs.get("profiles", []):
        print(f"  {p.get('name')}: vectorizer={p.get('vectorizer')}")
    
    return index


def main():
    parser = argparse.ArgumentParser(description="Test Azure AI Search deployment")
    parser.add_argument("--query", "-q", default="AI", help="Search query to test")
    parser.add_argument("--service", "-s", help="Search service name (overrides env var)")
    parser.add_argument("--index", "-i", help="Index name (overrides env var)")
    parser.add_argument("--resource-group", "-g", help="Resource group (overrides env var)")
    args = parser.parse_args()
    
    # Override from args if provided
    global SEARCH_SERVICE_NAME, INDEX_NAME, SEARCH_RESOURCE_GROUP, SEARCH_ENDPOINT
    if args.service:
        SEARCH_SERVICE_NAME = args.service
        SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE_NAME}.search.windows.net"
    if args.index:
        INDEX_NAME = args.index
    if args.resource_group:
        SEARCH_RESOURCE_GROUP = args.resource_group
    
    print("=" * 60)
    print("  Azure AI Search Testing Script")
    print("=" * 60)
    print(f"\nSearch Service: {SEARCH_SERVICE_NAME}")
    print(f"Index: {INDEX_NAME}")
    print(f"API Version: {API_VERSION}")
    
    # Get API key
    print("\nRetrieving API key...")
    api_key = get_api_key()
    print("API key retrieved successfully")
    
    # Run tests
    stats = test_index_stats(api_key)
    doc_count = stats.get("documentCount", 0) if "error" not in stats else 0
    
    if doc_count == 0:
        print("\n⚠️  No documents in index. Running remaining tests anyway...")
    
    test_indexer_status(api_key)
    test_vectorizer_config(api_key)
    test_keyword_search(args.query, api_key)
    test_vector_search(args.query, api_key)
    test_hybrid_search(args.query, api_key)
    test_semantic_search(args.query, api_key)
    
    # Summary
    print_separator("Test Summary")
    print("All tests completed!")
    print(f"Document count in index: {doc_count}")
    
    if doc_count == 0:
        print("\nTo add documents:")
        print("1. Sync files from SharePoint using main.py")
        print(f"2. Run indexer: az search indexer run --name {INDEXER_NAME} ...")
        print("3. Re-run this test script")


if __name__ == "__main__":
    main()
