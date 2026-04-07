const { app } = require("@azure/functions");
const { DefaultAzureCredential } = require("@azure/identity");

app.http("verifySitesSelected", {
    methods: ["GET"],
    authLevel: "anonymous",
    handler: async (request, context) => {
        context.log("Verifying Sites.Selected scope on Managed Identity...");

        const credential = new DefaultAzureCredential();
        const graphScope = "https://graph.microsoft.com/.default";

        // 1. Get token for Microsoft Graph (claims forces cache bypass for a fresh token)
        const tokenResponse = await credential.getToken(graphScope, {
            claims: '{"access_token":{"nbf":{"essential":true}}}',
            requestOptions: { timeout: 10000 }
        });
        const token = tokenResponse.token;

        // 2. Decode the JWT to inspect roles/scopes
        const parts = token.split(".");
        const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));

        // 3. Extract app roles (for Managed Identity, roles are in the "roles" claim)
        const roles = payload.roles || [];
        const hasSitesSelected = roles.includes("Sites.Selected");

        // 4. Test actual Graph API call — get specific site (Sites.Selected only allows access to granted sites)
        const siteId = request.query.get("siteId");
        let graphTestResult;
        if (!siteId) {
            graphTestResult = { error: "Pass ?siteId=contoso.sharepoint.com,guid1,guid2 to test site access" };
        } else {
            try {
                const response = await fetch(`https://graph.microsoft.com/v1.0/sites/${siteId}?$select=id,displayName,webUrl`, {
                    headers: { Authorization: `Bearer ${token}` }
                });
                const data = await response.json();
                graphTestResult = {
                    status: response.status,
                    site: data.error ? null : { id: data.id, displayName: data.displayName, webUrl: data.webUrl },
                    error: data.error ? data.error.message : null
                };
            } catch (err) {
                graphTestResult = { error: err.message };
            }
        }

        const result = {
            managedIdentity: {
                appId: payload.appid || payload.azp,
                objectId: payload.oid,
                tenantId: payload.tid
            },
            token: {
                audience: payload.aud,
                issuer: payload.iss,
                expiresAt: new Date(payload.exp * 1000).toISOString(),
                allRoles: roles
            },
            verification: {
                hasSitesSelected,
                sitesSelectedFound: hasSitesSelected
                    ? "Sites.Selected IS present in token roles"
                    : "Sites.Selected NOT found — check app role assignment"
            },
            graphApiTest: graphTestResult
        };

        return {
            jsonBody: result,
            headers: { "Content-Type": "application/json" }
        };
    }
});
