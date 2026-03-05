#!/usr/bin/env python3
"""
Demo: User login → Group claims → ACL-filtered AI Search

Flow:
1. User signs in via Entra ID (MSAL authorization code flow)
2. Group IDs are extracted from the ID token's "groups" claim
   (requires groupMembershipClaims=SecurityGroup in the app manifest)
3. Backend queries Azure AI Search with an OData filter on acl_group_ids
   so the user only sees documents they are authorized to view
4. Falls back to Graph /me/memberOf if groups claim is absent

This demonstrates document-level security trimming for a RAG pipeline.
"""

import os
import uuid
from urllib.parse import urlencode

import msal
import requests
from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ── App Registration for the demo (user-facing) ──────────────────────────────
CLIENT_ID = os.getenv("DEMO_CLIENT_ID")
CLIENT_SECRET = os.getenv("DEMO_CLIENT_SECRET")
TENANT_ID = os.getenv("DEMO_TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# Scopes requested during login:
#  - User.Read → access to /me
# Note: MSAL automatically adds openid, profile, offline_access — do NOT include them here.
# Group IDs come from the ID token claims (via groupMembershipClaims in the app manifest)
# so we do NOT need GroupMember.Read.All (which requires admin consent).
SCOPES = ["User.Read"]

# ── Azure AI Search ──────────────────────────────────────────────────────────
SEARCH_SERVICE_NAME = os.getenv("SEARCH_SERVICE_NAME")
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY")
INDEX_NAME = os.getenv("INDEX_NAME", "sharepoint-sync-index")
API_VERSION = os.getenv("API_VERSION", "2025-11-01-preview")
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE_NAME}.search.windows.net"

# ── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Use a stable secret key (not random per restart) so sessions survive restarts
app.secret_key = os.getenv("FLASK_SECRET_KEY", "demo-secret-key-change-in-prod")
# Trust proxy headers so request.url_root returns the real external URL
# (e.g. https://...app.github.dev instead of http://localhost:5000)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Cookie settings for cross-domain auth redirects (Entra ID → Codespace)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True

REDIRECT_PATH = "/auth/callback"

# Server-side store for auth flows, keyed by MSAL state parameter.
# This avoids relying on session cookies surviving the cross-domain redirect
# through Entra ID (Codespace proxies often drop cookies).
_auth_flows = {}

# Explicit external base URL (for Codespaces / reverse proxies)
# Set DEMO_BASE_URL in .env to override request.url_root
DEMO_BASE_URL = os.getenv("DEMO_BASE_URL", "")


def _get_redirect_uri():
    """Get the redirect URI, preferring explicit base URL over request.url_root."""
    base = DEMO_BASE_URL or request.url_root.rstrip("/")
    return base + REDIRECT_PATH


def _build_msal_app():
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )


# ── HTML Templates (inline for simplicity) ──────────────────────────────────
LOGIN_PAGE = """
<!DOCTYPE html>
<html><head><title>SharePoint ACL Search Demo</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
  .card { background: white; border-radius: 8px; padding: 30px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  h1 { color: #0078d4; }
  .btn { display: inline-block; padding: 12px 24px; background: #0078d4; color: white; text-decoration: none; border-radius: 4px; font-size: 16px; border: none; cursor: pointer; }
  .btn:hover { background: #106ebe; }
  code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; }
  .flow-diagram { background: #fafafa; padding: 20px; border-radius: 8px; border: 1px solid #e0e0e0; font-family: monospace; line-height: 1.8; }
</style></head>
<body>
  <div class="card">
    <h1>🔐 SharePoint ACL Search Demo</h1>
    <p>This demo shows the <strong>On-Behalf-Of (OBO)</strong> flow for document-level security in Azure AI Search.</p>
    <div class="flow-diagram">
      User signs in → App gets access token<br>
      → OBO exchange → Graph API token<br>
      → GET /me/memberOf → user's group IDs<br>
      → AI Search with OData filter on <code>acl_group_ids</code><br>
      → User sees ONLY documents they have access to
    </div>
    <br>
    <a class="btn" href="{{ login_url }}">Sign in with Microsoft →</a>
  </div>
</body></html>
"""

RESULTS_PAGE = """
<!DOCTYPE html>
<html><head><title>Search Results – ACL Demo</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
  .card { background: white; border-radius: 8px; padding: 30px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  h1, h2, h3 { color: #0078d4; }
  .user-info { background: #e8f4e8; padding: 15px; border-radius: 6px; margin-bottom: 20px; }
  .groups { background: #fff3cd; padding: 15px; border-radius: 6px; margin-bottom: 20px; }
  .filter-box { background: #f0f0f0; padding: 15px; border-radius: 6px; margin-bottom: 20px; font-family: monospace; font-size: 13px; word-break: break-all; }
  .result { border-left: 4px solid #0078d4; padding: 10px 15px; margin: 10px 0; background: #fafafa; }
  .no-results { color: #d83b01; font-weight: bold; }
  .btn { display: inline-block; padding: 8px 16px; background: #0078d4; color: white; text-decoration: none; border-radius: 4px; font-size: 14px; border: none; cursor: pointer; }
  .btn-small { padding: 6px 12px; font-size: 13px; background: #666; }
  form { display: inline; }
  input[type=text] { padding: 8px; width: 400px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; }
  td, th { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }
  th { background: #f0f0f0; }
  .step { counter-increment: step; padding: 10px; margin: 5px 0; background: #f8f8f8; border-radius: 4px; }
  .step::before { content: "Step " counter(step) ": "; font-weight: bold; color: #0078d4; }
  .steps { counter-reset: step; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: bold; }
  .badge-success { background: #d4edda; color: #155724; }
  .badge-info { background: #cce5ff; color: #004085; }
</style></head>
<body>
  <div class="card">
    <h1>🔍 ACL-Filtered Search Results</h1>
    
    <div class="user-info">
      <strong>👤 Signed in as:</strong> {{ user_name }} ({{ user_id[:8] }}…)<br>
      <strong>Token type:</strong> <span class="badge badge-info">OBO (On-Behalf-Of)</span>
    </div>

    <div class="groups">
      <strong>🏷 Your Entra Group Memberships ({{ group_count }} groups):</strong>
      <table>
        <tr><th>Group Name</th><th>Object ID</th></tr>
        {% for g in groups %}
        <tr><td>{{ g.name }}</td><td><code>{{ g.id }}</code></td></tr>
        {% endfor %}
        {% if group_count == 0 %}
        <tr><td colspan="2" class="no-results">No groups found</td></tr>
        {% endif %}
      </table>
    </div>

    <h2>Search</h2>
    <form method="get" action="{{ url_for('search') }}">
      <input type="text" name="q" value="{{ query }}" placeholder="Enter search query…">
      <button class="btn" type="submit">Search</button>
    </form>

    {% if filter_used %}
    <div class="filter-box">
      <strong>OData filter applied:</strong><br>{{ filter_used }}
    </div>
    {% endif %}

    <h3>Results ({{ result_count }} hits)</h3>
    {% if results %}
      {% for doc in results %}
      <div class="result">
        <strong>{{ doc.title or 'Untitled' }}</strong>
        {% if doc.original_file_name %}<br><small>📄 {{ doc.original_file_name }}</small>{% endif %}
        <br><small>{{ doc.chunk[:200] }}…</small>
        <br><small style="color:#888">acl_group_ids: {{ doc.acl_group_ids or 'n/a' }}</small>
      </div>
      {% endfor %}
    {% else %}
      <p class="no-results">No results found for "{{ query }}" with your permissions.</p>
    {% endif %}

    <hr>
    <h3>How This Works</h3>
    <div class="steps">
      <div class="step">User signed in via Entra ID (authorization code flow) with scope <code>User.Read</code></div>
      <div class="step">Extracted <strong>{{ group_count }} group IDs</strong> from the ID token <code>groups</code> claim (groupMembershipClaims=SecurityGroup)</div>
      <div class="step">Built an OData filter: <code>search.ismatch(group_id, 'acl_group_ids')</code> for each group</div>
      <div class="step">Sent the search query + filter to Azure AI Search → only matching documents returned</div>
    </div>

    <br>
    <a class="btn btn-small" href="{{ url_for('logout') }}">Sign Out</a>
  </div>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """Landing page with sign-in button."""
    if session.get("user"):
        return redirect(url_for("search"))
    login_url = url_for("login")
    return render_template_string(LOGIN_PAGE, login_url=login_url)


@app.route("/login")
def login():
    """Initiate the Entra ID authorization code flow."""
    msal_app = _build_msal_app()

    # Build the auth URL
    redirect_uri = _get_redirect_uri()
    print(f"[INFO] Redirect URI: {redirect_uri}")
    flow = msal_app.initiate_auth_code_flow(
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    # Store the flow server-side keyed by the state param (survives cookie loss)
    state = flow["state"]
    _auth_flows[state] = flow
    print(f"[INFO] Stored auth flow with state: {state}")
    return redirect(flow["auth_uri"])


@app.route(REDIRECT_PATH)
def auth_callback():
    """
    Handle the Entra ID callback after user signs in.
    Completes the auth code flow, then reads group IDs from the ID token claims.
    
    Prereq: The app registration must have groupMembershipClaims set to "SecurityGroup"
    in its manifest. This makes Entra include a "groups" claim in the ID token
    containing the object IDs of all security groups the user belongs to.
    No Graph API call needed — no admin consent required.
    """
    msal_app = _build_msal_app()
    
    # Retrieve the flow from server-side store using the state parameter
    state = request.args.get("state", "")
    flow = _auth_flows.pop(state, {})
    if not flow:
        return "Auth error: session lost (state not found). Please <a href='/'>try again</a>.", 400
    
    print(f"[INFO] Retrieved auth flow for state: {state}")
    result = msal_app.acquire_token_by_auth_code_flow(flow, request.args)
    if "error" in result:
        return f"Auth error: {result.get('error_description', result.get('error'))}", 400

    id_token_claims = result.get("id_token_claims", {})

    # ── Step 1: Get user info from the ID token claims ───────────────────
    user_name = id_token_claims.get("name", id_token_claims.get("preferred_username", "Unknown"))
    user_oid = id_token_claims.get("oid", "unknown")  # Entra Object ID

    # ── Step 2: Extract group IDs from the ID token ──────────────────────
    # The "groups" claim is a list of group object IDs (GUIDs) that Entra
    # includes when groupMembershipClaims = "SecurityGroup" in the manifest.
    # This avoids calling Graph API and needing GroupMember.Read.All consent.
    group_ids_from_token = id_token_claims.get("groups", [])
    
    print(f"[INFO] ID token claims keys: {list(id_token_claims.keys())}")
    print(f"[INFO] Groups from token: {group_ids_from_token}")
    
    groups = [{"id": gid, "name": f"Group {gid[:8]}…"} for gid in group_ids_from_token]

    # If no groups in token, also try calling Graph as fallback (if token has scope)
    access_token = result.get("access_token", "")
    if not groups and access_token:
        print("[INFO] No groups in token claims, trying Graph /me/memberOf as fallback…")
        groups = _get_user_groups(access_token)

    # Store in session
    session["user"] = {
        "name": user_name,
        "oid": user_oid,
        "access_token": access_token,
        "groups": groups,
    }

    return redirect(url_for("search"))


@app.route("/search")
def search():
    """
    Search Azure AI Search with ACL filtering based on the user's group memberships.
    """
    user = session.get("user")
    if not user:
        return redirect(url_for("index"))

    query = request.args.get("q", "")
    groups = user.get("groups", [])
    group_ids = [g["id"] for g in groups]

    results = []
    filter_used = ""
    if query:
        results, filter_used = _search_with_acl(query, group_ids)

    return render_template_string(
        RESULTS_PAGE,
        user_name=user["name"],
        user_id=user["oid"],
        groups=groups,
        group_count=len(groups),
        query=query,
        results=results,
        result_count=len(results),
        filter_used=filter_used,
    )


@app.route("/logout")
def logout():
    session.clear()
    # Redirect to Entra ID sign-out then back to our app
    post_logout = request.url_root
    logout_url = (
        f"{AUTHORITY}/oauth2/v2.0/logout?"
        + urlencode({"post_logout_redirect_uri": post_logout})
    )
    return redirect(logout_url)


# ══════════════════════════════════════════════════════════════════════════════
# Graph API helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_user_groups(access_token: str) -> list[dict]:
    """
    Call Microsoft Graph /me/memberOf to get the user's Entra group memberships.
    
    Uses the access token obtained during login (which already has 
    GroupMember.Read.All scope).
    
    Returns:
        List of dicts with 'id' and 'name' for each group.
    """
    url = "https://graph.microsoft.com/v1.0/me/memberOf"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    
    groups = []
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"[WARN] Graph /me/memberOf failed: {resp.status_code} {resp.text[:300]}")
            break
        
        data = resp.json()
        for item in data.get("value", []):
            # Filter to only groups (not roles, admin units, etc.)
            odata_type = item.get("@odata.type", "")
            if odata_type == "#microsoft.graph.group":
                groups.append({
                    "id": item.get("id", ""),
                    "name": item.get("displayName", "Unknown"),
                })
        
        # Pagination
        url = data.get("@odata.nextLink")
    
    print(f"[INFO] Found {len(groups)} groups for user")
    for g in groups:
        print(f"  - {g['name']} ({g['id']})")
    
    return groups


# ══════════════════════════════════════════════════════════════════════════════
# Azure AI Search helpers
# ══════════════════════════════════════════════════════════════════════════════

def _search_with_acl(query: str, group_ids: list[str]) -> tuple[list[dict], str]:
    """
    Execute a search against Azure AI Search with ACL filtering.
    
    Builds an OData $filter that matches documents where acl_group_ids 
    contains any of the user's group IDs.
    
    The acl_group_ids field stores pipe-delimited group IDs (e.g., "id1|id2|id3").
    We use search.ismatch() to find documents containing any of the user's groups.
    
    Args:
        query: The search text
        group_ids: List of Entra group object IDs the user belongs to
    
    Returns:
        Tuple of (results list, filter string used)
    """
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={API_VERSION}"
    headers = {
        "api-key": SEARCH_API_KEY,
        "Content-Type": "application/json",
    }

    # Build ACL filter:
    # acl_group_ids is a pipe-delimited string. We use search.ismatch to do a 
    # full-text search within the field for any of the user's group IDs.
    # Each group ID is OR'd together: user sees doc if ANY of their groups match.
    if group_ids:
        # Use search.ismatch to find docs where acl_group_ids contains any user group
        group_filters = " or ".join(
            f"search.ismatch('{gid}', 'acl_group_ids')"
            for gid in group_ids
        )
        acl_filter = f"({group_filters})"
    else:
        # No groups → no documents visible (strict deny-by-default)
        acl_filter = "acl_group_ids eq '00000000-0000-0000-0000-000000000000'"

    body = {
        "search": query,
        "filter": acl_filter,
        "top": 10,
        "select": "chunk_id,title,original_file_name,chunk,acl_group_ids,acl_user_ids",
        "count": True,
    }

    print(f"[INFO] Search query: {query}")
    print(f"[INFO] ACL filter: {acl_filter}")

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] Search failed: {resp.status_code} {resp.text[:500]}")
        return [], f"ERROR: {resp.status_code}"

    data = resp.json()
    results = []
    for doc in data.get("value", []):
        results.append({
            "chunk_id": doc.get("chunk_id", ""),
            "title": doc.get("title", ""),
            "original_file_name": doc.get("original_file_name", ""),
            "chunk": doc.get("chunk", ""),
            "acl_group_ids": doc.get("acl_group_ids", ""),
            "acl_user_ids": doc.get("acl_user_ids", ""),
        })

    print(f"[INFO] Results: {len(results)} documents")
    return results, acl_filter


# ══════════════════════════════════════════════════════════════════════════════
# Run
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  SharePoint ACL Search Demo")
    print("=" * 60)
    print(f"  Client ID:      {CLIENT_ID}")
    print(f"  Tenant ID:      {TENANT_ID}")
    print(f"  Search Service: {SEARCH_SERVICE_NAME}")
    print(f"  Index:          {INDEX_NAME}")
    redirect_uri = DEMO_BASE_URL + REDIRECT_PATH if DEMO_BASE_URL else f"http://localhost:5000{REDIRECT_PATH}"
    print(f"  Redirect URI:   {redirect_uri}")
    print()
    print(f"  Make sure {redirect_uri} is registered")
    print("  as a redirect URI in your Entra app registration.")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=5000, debug=True)
