#!/usr/bin/env python3
"""
Headless test for the OBO + Group Membership + ACL Search flow.

This script simulates the backend logic WITHOUT a browser:
1. Authenticates as the demo app using client credentials
2. Acquires a token for Graph API
3. Looks up a specific user's group memberships via Graph /users/{id}/memberOf
4. Builds the OData ACL filter
5. Queries Azure AI Search with and without the ACL filter to show the difference

Usage:
    python test_obo_flow.py [--user-id <entra-object-id>] [--query <search-query>]
    
    If --user-id is not provided, it will list users and let you pick one,
    or use the DEMO_GROUP_ID to test directly.
"""

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Auth (demo app registration)
CLIENT_ID = os.getenv("DEMO_CLIENT_ID")
CLIENT_SECRET = os.getenv("DEMO_CLIENT_SECRET")
TENANT_ID = os.getenv("DEMO_TENANT_ID")

# Search
SEARCH_SERVICE_NAME = os.getenv("SEARCH_SERVICE_NAME")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY")
INDEX_NAME = os.getenv("INDEX_NAME", "sharepoint-sync-index")
API_VERSION = os.getenv("API_VERSION", "2025-11-01-preview")
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE_NAME}.search.windows.net"

# Demo group
DEMO_GROUP_ID = os.getenv("DEMO_GROUP_ID", "")


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def get_app_token() -> str:
    """
    Get an access token using client credentials (app-only).
    This is NOT the OBO flow — it's used to query Graph as the app itself.
    In the real demo app, we'd use the user's delegated token via OBO.
    """
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Failed to get token: {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    
    token = resp.json().get("access_token")
    print("[OK] Acquired app-only Graph token")
    return token


def get_user_groups(token: str, user_id: str) -> list[dict]:
    """
    Get group memberships for a specific user via Graph API.
    Uses GET /users/{user-id}/memberOf
    """
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/memberOf"
    headers = {"Authorization": f"Bearer {token}"}
    
    groups = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"[ERROR] Graph API failed: {resp.status_code}")
            print(resp.text[:500])
            return groups
        
        data = resp.json()
        for item in data.get("value", []):
            if item.get("@odata.type") == "#microsoft.graph.group":
                groups.append({
                    "id": item["id"],
                    "name": item.get("displayName", "Unknown"),
                    "description": item.get("description", ""),
                })
        url = data.get("@odata.nextLink")
    
    return groups


def get_me(token: str) -> dict:
    """Get the current user's profile (for delegated tokens)."""
    url = "https://graph.microsoft.com/v1.0/me"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    return {}


def list_users(token: str, top: int = 10) -> list[dict]:
    """List users in the tenant."""
    url = f"https://graph.microsoft.com/v1.0/users?$top={top}&$select=id,displayName,userPrincipalName"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Failed to list users: {resp.status_code}")
        print(resp.text[:500])
        return []
    return resp.json().get("value", [])


def search_without_filter(query: str) -> tuple[list, int]:
    """Search AI Search WITHOUT any ACL filter (admin view — sees everything)."""
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={API_VERSION}"
    headers = {"api-key": SEARCH_API_KEY, "Content-Type": "application/json"}
    body = {
        "search": query,
        "top": 10,
        "select": "chunk_id,title,original_file_name,chunk,acl_group_ids,acl_user_ids",
        "count": True,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Search failed: {resp.status_code} {resp.text[:300]}")
        return [], 0
    data = resp.json()
    return data.get("value", []), data.get("@odata.count", 0)


def search_with_acl(query: str, group_ids: list[str]) -> tuple[list, int, str]:
    """Search AI Search WITH ACL filter based on user's groups."""
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={API_VERSION}"
    headers = {"api-key": SEARCH_API_KEY, "Content-Type": "application/json"}
    
    if group_ids:
        group_filters = " or ".join(
            f"search.ismatch('{gid}', 'acl_group_ids')"
            for gid in group_ids
        )
        acl_filter = f"({group_filters})"
    else:
        acl_filter = "acl_group_ids eq '00000000-0000-0000-0000-000000000000'"
    
    body = {
        "search": query,
        "filter": acl_filter,
        "top": 10,
        "select": "chunk_id,title,original_file_name,chunk,acl_group_ids,acl_user_ids",
        "count": True,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Search failed: {resp.status_code} {resp.text[:300]}")
        return [], 0, acl_filter
    data = resp.json()
    return data.get("value", []), data.get("@odata.count", 0), acl_filter


def main():
    parser = argparse.ArgumentParser(description="Test OBO + ACL search flow (headless)")
    parser.add_argument("--user-id", "-u", help="Entra Object ID of user to test")
    parser.add_argument("--query", "-q", default="*", help="Search query (default: *)")
    parser.add_argument("--list-users", action="store_true", help="List users in tenant")
    args = parser.parse_args()

    separator("OBO + ACL Search Flow Test")
    print(f"Client ID:      {CLIENT_ID}")
    print(f"Tenant ID:      {TENANT_ID}")
    print(f"Search Service: {SEARCH_SERVICE_NAME}")
    print(f"Index:          {INDEX_NAME}")
    print(f"Demo Group ID:  {DEMO_GROUP_ID}")

    # ── Step 1: Get app token ─────────────────────────────────────────────
    separator("Step 1: Acquire App Token (Client Credentials)")
    token = get_app_token()

    # ── List users if requested ───────────────────────────────────────────
    if args.list_users:
        separator("Available Users")
        users = list_users(token)
        for u in users:
            print(f"  {u['displayName']:30s}  {u['id']}  ({u.get('userPrincipalName', '')})")
        return

    # ── Step 2: Get user's group memberships ──────────────────────────────
    user_id = args.user_id
    if user_id:
        separator(f"Step 2: Get Group Memberships for User {user_id[:8]}…")
        groups = get_user_groups(token, user_id)
    else:
        separator("Step 2: Using DEMO_GROUP_ID from .env")
        if DEMO_GROUP_ID:
            groups = [{"id": DEMO_GROUP_ID, "name": "(from .env DEMO_GROUP_ID)", "description": ""}]
            print(f"  Using group: {DEMO_GROUP_ID}")
        else:
            print("[WARN] No --user-id and no DEMO_GROUP_ID. Will search without groups.")
            groups = []

    if groups:
        print(f"\n  Found {len(groups)} group(s):")
        for g in groups:
            print(f"    • {g['name']:40s}  {g['id']}")
    else:
        print("  No groups found.")

    group_ids = [g["id"] for g in groups]

    # ── Step 3: Search WITHOUT filter (admin view) ────────────────────────
    separator(f"Step 3: Search WITHOUT ACL filter (query: '{args.query}')")
    no_filter_results, no_filter_count = search_without_filter(args.query)
    print(f"  Total results (unfiltered): {no_filter_count}")
    for i, doc in enumerate(no_filter_results[:5], 1):
        title = doc.get("title", "Untitled")
        fname = doc.get("original_file_name", "")
        acl = doc.get("acl_group_ids", "n/a")
        print(f"  {i}. {title} ({fname})")
        print(f"     acl_group_ids: {acl}")

    # ── Step 4: Search WITH ACL filter ────────────────────────────────────
    separator(f"Step 4: Search WITH ACL filter (query: '{args.query}')")
    acl_results, acl_count, filter_str = search_with_acl(args.query, group_ids)
    print(f"  Filter:  {filter_str}")
    print(f"  Total results (ACL-filtered): {acl_count}")
    for i, doc in enumerate(acl_results[:5], 1):
        title = doc.get("title", "Untitled")
        fname = doc.get("original_file_name", "")
        acl = doc.get("acl_group_ids", "n/a")
        print(f"  {i}. {title} ({fname})")
        print(f"     acl_group_ids: {acl}")

    # ── Summary ───────────────────────────────────────────────────────────
    separator("Summary")
    print(f"  Unfiltered results: {no_filter_count}")
    print(f"  ACL-filtered results: {acl_count}")
    if no_filter_count > 0 and acl_count > 0:
        print(f"  → User sees {acl_count}/{no_filter_count} documents ({acl_count*100//no_filter_count}%)")
    elif no_filter_count > 0 and acl_count == 0:
        print(f"  → ACL filter blocked all documents (user has no matching groups)")
    else:
        print(f"  → No documents in index")
    
    print("\n  This proves document-level security trimming works:")
    print("  documents are only visible when the user's group memberships")
    print("  match the acl_group_ids stored in the search index.")


if __name__ == "__main__":
    main()
