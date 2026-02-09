# Demo — ACL-Filtered Search with Entra ID Login

Flask web app demonstrating document-level security in Azure AI Search. Users sign in via Entra ID, and search results are filtered by their group memberships — they only see documents they have access to.

## How It Works

```
User signs in (Entra ID)
  → ID token includes "groups" claim (list of group Object IDs)
  → App builds OData filter: search.ismatch(group_id, 'acl_group_ids')
  → Azure AI Search returns only documents matching the user's groups
```

No Graph API call needed for groups. No admin consent required.

## Prerequisites

- Python 3.11+
- An Entra ID app registration (see setup below)
- Azure AI Search with indexed documents (including ACL fields)

## App Registration Setup (No Admin Consent)

### Step 1 — Create the app registration

```bash
az ad app create \
  --display-name "SharePoint Search ACL Demo" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "http://localhost:5000/auth/callback"
# Note the appId → DEMO_CLIENT_ID
```

### Step 2 — Add a client secret

```bash
az ad app credential reset --id <app-id> --display-name "demo-secret"
# Note the password → DEMO_CLIENT_SECRET
```

### Step 3 — API permissions

Only one permission needed:

| Permission | Type | Admin Consent? |
|------------|------|----------------|
| `User.Read` | Delegated | **No** |

### Step 4 — Enable group claims in the token

This is the key step that avoids needing `GroupMember.Read.All` (admin consent). Set `groupMembershipClaims` so Entra ID embeds group IDs directly in the ID token.

**Option A — Azure Portal (Manifest editor):**

1. Go to **Azure Portal → Microsoft Entra ID → App registrations**
2. Select your app
3. Click **Manifest** in the left menu
4. Find `"groupMembershipClaims"` (defaults to `null`)
5. Change to `"SecurityGroup"`:
   ```json
   "groupMembershipClaims": "SecurityGroup",
   ```
6. Click **Save**

**Option B — Azure Portal (Token configuration UI):**

1. Go to **App registrations → your app → Token configuration**
2. Click **+ Add groups claim**
3. Check **Security groups**
4. For **ID** tokens, select **Group ID**
5. Click **Add**

**Option C — Azure CLI:**

```bash
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications(appId='<app-id>')" \
  --headers "Content-Type=application/json" \
  --body '{"groupMembershipClaims": "SecurityGroup"}'
```

> **Verify:** Check **Manifest** → `"groupMembershipClaims": "SecurityGroup"` is set.

### Step 5 — Environment variables

```bash
# In your .env file
DEMO_CLIENT_ID=<app-id>
DEMO_CLIENT_SECRET=<client-secret>
DEMO_TENANT_ID=<your-tenant-id>
```

## Why This Works Without Admin Consent

```
Traditional approach (requires admin consent):
  Login → GroupMember.Read.All scope → Graph /me/memberOf → group IDs
  ⚠ GroupMember.Read.All requires admin consent

Our approach (user consent only):
  Login with User.Read only
  → Entra includes "groups" claim in the ID token
     (because groupMembershipClaims = "SecurityGroup")
  → App reads group IDs from id_token_claims["groups"]
  ✅ Only User.Read — any user can consent
```

The `groupMembershipClaims` manifest setting is a **tenant-level app configuration**, not a permission — it doesn't require admin consent at login time.

**ID token example:**
```json
{
  "name": "John Doe",
  "groups": ["0828d1e1-...", "170f33af-..."]
}
```

> **Groups overage:** If the user belongs to 200+ groups, Entra omits the `groups` claim. The app falls back to Graph `/me/memberOf` (requires `GroupMember.Read.All` + admin consent). Most orgs don't hit this limit.

## Run

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

For Codespaces, set `DEMO_BASE_URL` to the forwarded URL and register it as a redirect URI.

## Headless Test (no browser)

```bash
python test_obo_flow.py --query "*"
```

## Files

| File | Description |
|------|-------------|
| `app.py` | Flask web app with MSAL auth + ACL search |
| `test_obo_flow.py` | CLI test for ACL filtering |
| `requirements.txt` | Python dependencies |
