namespace SharePointSync.Core;

/// <summary>
/// Configuration for the SharePoint-to-Blob sync job.
/// Loaded from environment variables (same names as the Python version).
/// </summary>
public sealed class SyncConfig
{
    // SharePoint
    public string SharePointSiteUrl { get; set; } = string.Empty;
    public string SharePointDriveName { get; set; } = "Documents";
    public string SharePointFolderPath { get; set; } = "/";

    // Azure Blob
    public string StorageAccountName { get; set; } = string.Empty;
    public string ContainerName { get; set; } = "sharepoint-sync";
    public string BlobPrefix { get; set; } = string.Empty;

    // Sync behaviour
    public bool DeleteOrphanedBlobs { get; set; }
    public bool DryRun { get; set; }
    public bool SyncPermissions { get; set; }
    public bool ForceFullSync { get; set; }

    // Retry
    public int RetryMaxAttempts { get; set; } = 5;
    public double RetryBaseDelaySecs { get; set; } = 2.0;
    public double RetryMaxDelaySecs { get; set; } = 60.0;

    public string BlobAccountUrl => $"https://{StorageAccountName}.blob.core.windows.net";

    public (string Host, string Path) ParseSiteUrl()
    {
        var uri = new Uri(SharePointSiteUrl);
        return (uri.Host, uri.AbsolutePath);
    }

    public static SyncConfig FromEnvironment()
    {
        static bool EnvBool(string name) =>
            string.Equals(Environment.GetEnvironmentVariable(name), "true", StringComparison.OrdinalIgnoreCase);

        return new SyncConfig
        {
            SharePointSiteUrl = Environment.GetEnvironmentVariable("SHAREPOINT_SITE_URL") ?? string.Empty,
            SharePointDriveName = Environment.GetEnvironmentVariable("SHAREPOINT_DRIVE_NAME") ?? "Documents",
            SharePointFolderPath = Environment.GetEnvironmentVariable("SHAREPOINT_FOLDER_PATH") ?? "/",
            StorageAccountName = Environment.GetEnvironmentVariable("AZURE_STORAGE_ACCOUNT_NAME") ?? string.Empty,
            ContainerName = Environment.GetEnvironmentVariable("AZURE_BLOB_CONTAINER_NAME") ?? "sharepoint-sync",
            BlobPrefix = Environment.GetEnvironmentVariable("AZURE_BLOB_PREFIX") ?? string.Empty,
            DeleteOrphanedBlobs = EnvBool("DELETE_ORPHANED_BLOBS"),
            DryRun = EnvBool("DRY_RUN"),
            SyncPermissions = EnvBool("SYNC_PERMISSIONS"),
            ForceFullSync = EnvBool("FORCE_FULL_SYNC"),
            RetryMaxAttempts = int.TryParse(Environment.GetEnvironmentVariable("RETRY_MAX_ATTEMPTS"), out var r) ? r : 5,
            RetryBaseDelaySecs = double.TryParse(Environment.GetEnvironmentVariable("RETRY_BASE_DELAY_SECS"), out var b) ? b : 2.0,
            RetryMaxDelaySecs = double.TryParse(Environment.GetEnvironmentVariable("RETRY_MAX_DELAY_SECS"), out var m) ? m : 60.0,
        };
    }

    public void Validate()
    {
        var errors = new List<string>();
        if (string.IsNullOrWhiteSpace(SharePointSiteUrl))
            errors.Add("SHAREPOINT_SITE_URL is required");
        if (string.IsNullOrWhiteSpace(StorageAccountName))
            errors.Add("AZURE_STORAGE_ACCOUNT_NAME is required");
        if (string.IsNullOrWhiteSpace(ContainerName))
            errors.Add("AZURE_BLOB_CONTAINER_NAME is required");

        if (errors.Count > 0)
            throw new InvalidOperationException($"Configuration errors: {string.Join(", ", errors)}");
    }
}
