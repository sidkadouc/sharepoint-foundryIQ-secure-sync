using System.Text;
using System.Text.Json;
using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using Microsoft.Extensions.Logging;

namespace SharePointSync.Core;

/// <summary>
/// Azure Blob Storage client for sync operations.
/// Mirrors the Python blob_client.py — same metadata keys, delta-token persistence, etc.
/// </summary>
public sealed class BlobStorageClient : IAsyncDisposable
{
    // Metadata keys (same as Python)
    public const string MetaSPItemId = "sharepoint_item_id";
    public const string MetaSPLastModified = "sharepoint_last_modified";
    public const string MetaSPContentHash = "sharepoint_content_hash";
    public const string DeltaTokenBlob = ".sync-state/delta-token.json";

    private readonly string _blobPrefix;
    private readonly ILogger _logger;
    private readonly BlobContainerClient _container;

    public BlobStorageClient(SyncConfig config, ILogger logger)
    {
        _blobPrefix = config.BlobPrefix.Trim('/');
        _logger = logger;

        var credential = CredentialFactory.ForBlobStorage(logger);
        var serviceClient = new BlobServiceClient(new Uri(config.BlobAccountUrl), credential,
            new BlobClientOptions
            {
                Retry =
                {
                    MaxRetries = config.RetryMaxAttempts,
                    Delay = TimeSpan.FromSeconds(config.RetryBaseDelaySecs),
                    MaxDelay = TimeSpan.FromSeconds(config.RetryMaxDelaySecs),
                    Mode = Azure.Core.RetryMode.Exponential,
                }
            });
        _container = serviceClient.GetBlobContainerClient(config.ContainerName);
    }

    public async Task EnsureContainerAsync(CancellationToken ct = default)
    {
        await _container.CreateIfNotExistsAsync(cancellationToken: ct);
    }

    // ── Path helpers ───────────────────────────────────────────────────

    public string GetBlobName(string sharepointPath)
    {
        var clean = sharepointPath.TrimStart('/');
        return string.IsNullOrEmpty(_blobPrefix) ? clean : $"{_blobPrefix}/{clean}";
    }

    // ── List blobs ─────────────────────────────────────────────────────

    public async IAsyncEnumerable<BlobItem> ListBlobsAsync(
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct = default)
    {
        var prefix = string.IsNullOrEmpty(_blobPrefix) ? null : _blobPrefix;

        await foreach (var blob in _container.GetBlobsAsync(
            BlobTraits.Metadata, BlobStates.None, prefix, ct))
        {
            if (blob.Name.EndsWith('/')) continue;
            if ((blob.Properties.ContentLength ?? 0) == 0 && !blob.Name.Split('/').Last().Contains('.')) continue;
            yield return blob;
        }
    }

    // ── Upload ─────────────────────────────────────────────────────────

    public async Task<string> UploadBlobAsync(
        string sharepointPath, byte[] content,
        string sharepointItemId, DateTimeOffset lastModified,
        string? contentHash = null, bool dryRun = false,
        CancellationToken ct = default)
    {
        var blobName = GetBlobName(sharepointPath);
        var metadata = new Dictionary<string, string>
        {
            [MetaSPItemId] = sharepointItemId,
            [MetaSPLastModified] = lastModified.ToString("O"),
        };
        if (!string.IsNullOrEmpty(contentHash))
            metadata[MetaSPContentHash] = contentHash;

        if (dryRun)
        {
            _logger.LogInformation("[DRY RUN] Would upload {Blob} ({Bytes} bytes)", blobName, content.Length);
        }
        else
        {
            var client = _container.GetBlobClient(blobName);
            using var ms = new MemoryStream(content);
            await client.UploadAsync(ms, new BlobUploadOptions { Metadata = metadata }, ct);
            _logger.LogInformation("Uploaded {Blob} ({Bytes} bytes)", blobName, content.Length);
        }

        return blobName;
    }

    // ── Delete ─────────────────────────────────────────────────────────

    public async Task DeleteBlobAsync(string blobName, bool dryRun = false, CancellationToken ct = default)
    {
        if (dryRun)
        {
            _logger.LogInformation("[DRY RUN] Would delete {Blob}", blobName);
            return;
        }

        try
        {
            await _container.GetBlobClient(blobName).DeleteIfExistsAsync(cancellationToken: ct);
            _logger.LogInformation("Deleted {Blob}", blobName);
        }
        catch (Azure.RequestFailedException ex) when (ex.ErrorCode == "DirectoryIsNotEmpty")
        {
            _logger.LogInformation("Deleting directory recursively: {Blob}", blobName);
            var prefix = blobName.TrimEnd('/') + "/";
            await foreach (var child in _container.GetBlobsAsync(BlobTraits.None, BlobStates.None, prefix, ct))
                await _container.GetBlobClient(child.Name).DeleteIfExistsAsync(cancellationToken: ct);
            await _container.GetBlobClient(blobName).DeleteIfExistsAsync(cancellationToken: ct);
        }
    }

    // ── Update metadata ────────────────────────────────────────────────

    public async Task UpdateBlobMetadataAsync(
        string blobName, IDictionary<string, string> additionalMetadata,
        bool dryRun = false, CancellationToken ct = default)
    {
        if (dryRun)
        {
            _logger.LogInformation("[DRY RUN] Would update metadata on {Blob}", blobName);
            return;
        }

        var client = _container.GetBlobClient(blobName);
        var props = await client.GetPropertiesAsync(cancellationToken: ct);
        var merged = new Dictionary<string, string>(props.Value.Metadata);

        // Remove deprecated fields
        foreach (var key in new[] { "metadata_user_ids", "metadata_group_ids",
            "acl_user_ids_list", "acl_group_ids_list",
            "metadata_acl_user_ids", "metdata_acl_group_ids" })
            merged.Remove(key);

        foreach (var kv in additionalMetadata)
            merged[kv.Key] = kv.Value;

        await client.SetMetadataAsync(merged, cancellationToken: ct);
        _logger.LogInformation("Updated metadata on {Blob}", blobName);
    }

    // ── Change detection ───────────────────────────────────────────────

    public static bool ShouldUpdate(BlobItem blob, DateTimeOffset? spLastModified, string? spContentHash)
    {
        var meta = blob.Metadata;
        if (meta is null || meta.Count == 0) return true;

        if (meta.TryGetValue(MetaSPContentHash, out var storedHash)
            && !string.IsNullOrEmpty(storedHash)
            && !string.IsNullOrEmpty(spContentHash)
            && storedHash != spContentHash)
            return true;

        if (meta.TryGetValue(MetaSPLastModified, out var storedDateStr)
            && DateTimeOffset.TryParse(storedDateStr, out var storedDate)
            && spLastModified.HasValue
            && spLastModified.Value > storedDate)
            return true;

        return false;
    }

    // ── Delta token persistence ────────────────────────────────────────

    public async Task<string?> LoadDeltaTokenAsync(CancellationToken ct = default)
    {
        try
        {
            var client = _container.GetBlobClient(DeltaTokenBlob);
            var download = await client.DownloadContentAsync(ct);
            var json = download.Value.Content.ToString();
            using var doc = JsonDocument.Parse(json);
            var link = doc.RootElement.TryGetProperty("delta_link", out var dl) ? dl.GetString() : null;
            var savedAt = doc.RootElement.TryGetProperty("saved_at", out var sa) ? sa.GetString() : "unknown";
            _logger.LogInformation("Loaded delta token (saved at {SavedAt})", savedAt);
            return link;
        }
        catch
        {
            _logger.LogInformation("No existing delta token — will do full initial sync");
            return null;
        }
    }

    public async Task SaveDeltaTokenAsync(string deltaLink, bool dryRun = false, CancellationToken ct = default)
    {
        if (dryRun) { _logger.LogInformation("[DRY RUN] Would save delta token"); return; }

        var payload = JsonSerializer.Serialize(new
        {
            delta_link = deltaLink,
            saved_at = DateTimeOffset.UtcNow.ToString("O"),
        });

        var client = _container.GetBlobClient(DeltaTokenBlob);
        using var ms = new MemoryStream(Encoding.UTF8.GetBytes(payload));
        await client.UploadAsync(ms, overwrite: true, ct);
        _logger.LogInformation("Saved delta token");
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
