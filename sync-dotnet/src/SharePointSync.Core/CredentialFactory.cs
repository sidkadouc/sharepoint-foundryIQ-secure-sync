using Azure.Identity;
using Microsoft.Extensions.Logging;

namespace SharePointSync.Core;

/// <summary>
/// Factory for Azure credentials, with separate credential chains
/// for SharePoint (Graph API) and Blob Storage — mirrors the Python version.
/// </summary>
public static class CredentialFactory
{
    /// <summary>
    /// Credential for Microsoft Graph / SharePoint access.
    /// Prefers ClientSecretCredential when AZURE_CLIENT_ID/SECRET/TENANT_ID are set.
    /// </summary>
    public static Azure.Core.TokenCredential ForSharePoint(ILogger? logger = null)
    {
        var clientId = Environment.GetEnvironmentVariable("AZURE_CLIENT_ID");
        var clientSecret = Environment.GetEnvironmentVariable("AZURE_CLIENT_SECRET");
        var tenantId = Environment.GetEnvironmentVariable("AZURE_TENANT_ID");

        if (!string.IsNullOrEmpty(clientId) && !string.IsNullOrEmpty(clientSecret) && !string.IsNullOrEmpty(tenantId))
        {
            logger?.LogInformation("Using ClientSecretCredential for SharePoint (AppReg clientId={ClientId})", clientId);
            return new ClientSecretCredential(tenantId, clientId, clientSecret);
        }

        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("IDENTITY_ENDPOINT")))
        {
            logger?.LogInformation("Using ManagedIdentityCredential for SharePoint");
            return new DefaultAzureCredential();
        }

        logger?.LogInformation("Using DefaultAzureCredential for SharePoint");
        return new DefaultAzureCredential();
    }

    /// <summary>
    /// Credential for Blob Storage access.
    /// Uses ManagedIdentity in Azure, AzureCliCredential locally.
    /// Supports separate storage credentials via AZURE_STORAGE_* env vars.
    /// </summary>
    public static Azure.Core.TokenCredential ForBlobStorage(ILogger? logger = null)
    {
        var storageTenantId = Environment.GetEnvironmentVariable("AZURE_STORAGE_TENANT_ID");
        var storageClientId = Environment.GetEnvironmentVariable("AZURE_STORAGE_CLIENT_ID");
        var storageClientSecret = Environment.GetEnvironmentVariable("AZURE_STORAGE_CLIENT_SECRET");

        if (!string.IsNullOrEmpty(storageTenantId) && !string.IsNullOrEmpty(storageClientId) && !string.IsNullOrEmpty(storageClientSecret))
        {
            logger?.LogInformation("Using ClientSecretCredential for Blob Storage");
            return new ClientSecretCredential(storageTenantId, storageClientId, storageClientSecret);
        }

        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("IDENTITY_ENDPOINT")))
        {
            logger?.LogInformation("Using ManagedIdentityCredential for Blob Storage");
            return new ManagedIdentityCredential(ManagedIdentityId.SystemAssigned);
        }

        logger?.LogInformation("Using AzureCliCredential for Blob Storage");
        return new AzureCliCredential();
    }
}
