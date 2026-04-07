using Azure.Storage.Blobs.Models;
using Microsoft.Extensions.Logging;

namespace SharePointSync.Core;

/// <summary>
/// Orchestrates the SharePoint-to-Blob sync — mirrors Python main.py logic exactly.
/// Supports delta (incremental) and full sync modes.
/// Permissions are fully re-scanned each run (delta API can't track permission changes
/// without Sites.FullControl.All — we only use Sites.Read.All / Sites.Selected read).
/// </summary>
public sealed class SyncJob
{
    private readonly SyncConfig _config;
    private readonly ILogger _logger;

    public SyncJob(SyncConfig config, ILogger logger)
    {
        _config = config;
        _logger = logger;
    }

    public async Task<SyncStats> RunAsync(CancellationToken ct = default)
    {
        var stats = new SyncStats();

        _logger.LogInformation("Starting sync: site={Site}, drive={Drive}, folder={Folder}, " +
            "dryRun={DryRun}, permissions={Perms}, forceFullSync={Force}",
            _config.SharePointSiteUrl, _config.SharePointDriveName,
            _config.SharePointFolderPath, _config.DryRun,
            _config.SyncPermissions, _config.ForceFullSync);

        // ── SharePoint client ──
        var spClient = new SharePointClient(_config, _logger);
        await spClient.InitializeAsync(ct);
        var (siteId, driveId) = spClient.GetResolvedIds();
        _logger.LogInformation("Resolved IDs: site={SiteId}, drive={DriveId}", siteId, driveId);

        // ── Blob client ──
        var blobClient = new BlobStorageClient(_config, _logger);
        await blobClient.EnsureContainerAsync(ct);

        // ── Delta or full? ──
        string? deltaLink = null;
        if (!_config.ForceFullSync)
            deltaLink = await blobClient.LoadDeltaTokenAsync(ct);

        if (deltaLink is not null || !_config.ForceFullSync)
        {
            // ── Delta sync ──
            stats.SyncMode = deltaLink is null ? "delta-initial" : "delta-incremental";
            _logger.LogInformation("Using delta sync, mode={Mode}", stats.SyncMode);

            var delta = await spClient.GetDeltaAsync(deltaLink, ct);
            var changedFiles = new List<SharePointFile>();

            foreach (var change in delta.Changes)
            {
                stats.FilesScanned++;

                if (change.ChangeType == DeltaChangeType.Deleted)
                {
                    var blobName = blobClient.GetBlobName(change.ItemPath);
                    _logger.LogInformation("Delta: deleted {Path}", change.ItemPath);
                    if (_config.DeleteOrphanedBlobs)
                    {
                        try
                        {
                            await blobClient.DeleteBlobAsync(blobName, _config.DryRun, ct);
                            stats.FilesDeleted++;
                        }
                        catch (Exception ex)
                        {
                            _logger.LogError(ex, "Failed to delete blob {Blob}", blobName);
                            stats.FilesFailed++;
                        }
                    }
                }
                else if (change.ChangeType == DeltaChangeType.CreatedOrModified && change.File is not null)
                {
                    var spFile = change.File;
                    try
                    {
                        _logger.LogInformation("Delta: created/modified {Path} ({Size} bytes)", spFile.Path, spFile.Size);
                        var content = await spClient.DownloadFileAsync(spFile.Id, ct);
                        await blobClient.UploadBlobAsync(spFile.Path, content,
                            spFile.Id, spFile.LastModified ?? DateTimeOffset.UtcNow,
                            spFile.ContentHash, _config.DryRun, ct);
                        stats.FilesAdded++;
                        stats.BytesTransferred += content.Length;
                        changedFiles.Add(spFile);
                    }
                    catch (Exception ex)
                    {
                        _logger.LogError(ex, "Failed to process {Path}", spFile.Path);
                        stats.FilesFailed++;
                    }
                }
            }

            // Persist new delta token
            if (!string.IsNullOrEmpty(delta.DeltaToken))
                await blobClient.SaveDeltaTokenAsync(delta.DeltaToken, _config.DryRun, ct);

            // Permissions: always full re-scan (delta can't track permission changes)
            if (_config.SyncPermissions)
            {
                _logger.LogInformation("Syncing permissions for ALL files (permission changes invisible to delta)");
                var allFiles = new List<SharePointFile>();
                await foreach (var f in spClient.ListFilesAsync(_config.SharePointFolderPath, ct))
                    allFiles.Add(f);
                await SyncPermissionsAsync(blobClient, driveId, allFiles, stats, ct);
            }
        }
        else
        {
            // ── Full sync ──
            stats.SyncMode = "full";
            _logger.LogInformation("Using full sync (FORCE_FULL_SYNC=true)");

            // Snapshot existing blobs
            var existingBlobs = new Dictionary<string, BlobItem>();
            await foreach (var blob in blobClient.ListBlobsAsync(ct))
                existingBlobs[blob.Name] = blob;
            _logger.LogInformation("Found {Count} existing blobs", existingBlobs.Count);

            var seenBlobNames = new HashSet<string>();
            var allFiles = new List<SharePointFile>();

            await foreach (var spFile in spClient.ListFilesAsync(_config.SharePointFolderPath, ct))
            {
                stats.FilesScanned++;
                var blobName = blobClient.GetBlobName(spFile.Path);
                seenBlobNames.Add(blobName);

                try
                {
                    if (!existingBlobs.TryGetValue(blobName, out var existingBlob))
                    {
                        _logger.LogInformation("New file: {Path} ({Size} bytes)", spFile.Path, spFile.Size);
                        var content = await spClient.DownloadFileAsync(spFile.Id, ct);
                        await blobClient.UploadBlobAsync(spFile.Path, content,
                            spFile.Id, spFile.LastModified ?? DateTimeOffset.UtcNow,
                            spFile.ContentHash, _config.DryRun, ct);
                        stats.FilesAdded++;
                        stats.BytesTransferred += content.Length;
                    }
                    else if (BlobStorageClient.ShouldUpdate(existingBlob, spFile.LastModified, spFile.ContentHash))
                    {
                        _logger.LogInformation("Modified file: {Path} ({Size} bytes)", spFile.Path, spFile.Size);
                        var content = await spClient.DownloadFileAsync(spFile.Id, ct);
                        await blobClient.UploadBlobAsync(spFile.Path, content,
                            spFile.Id, spFile.LastModified ?? DateTimeOffset.UtcNow,
                            spFile.ContentHash, _config.DryRun, ct);
                        stats.FilesUpdated++;
                        stats.BytesTransferred += content.Length;
                    }
                    else
                    {
                        stats.FilesUnchanged++;
                    }

                    allFiles.Add(spFile);
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Failed to process {Path}", spFile.Path);
                    stats.FilesFailed++;
                }
            }

            // Orphan cleanup
            if (_config.DeleteOrphanedBlobs)
            {
                foreach (var blobName in existingBlobs.Keys)
                {
                    if (!seenBlobNames.Contains(blobName))
                    {
                        try
                        {
                            await blobClient.DeleteBlobAsync(blobName, _config.DryRun, ct);
                            stats.FilesDeleted++;
                        }
                        catch (Exception ex)
                        {
                            _logger.LogError(ex, "Failed to delete orphaned blob {Blob}", blobName);
                            stats.FilesFailed++;
                        }
                    }
                }
            }

            // Permissions
            if (_config.SyncPermissions)
                await SyncPermissionsAsync(blobClient, driveId, allFiles, stats, ct);
        }

        _logger.LogInformation("Sync completed: mode={Mode}, scanned={Scanned}, added={Added}, " +
            "updated={Updated}, deleted={Deleted}, unchanged={Unchanged}, failed={Failed}, " +
            "bytes={Bytes}, permsSynced={PermsSynced}, permsFailed={PermsFailed}",
            stats.SyncMode, stats.FilesScanned, stats.FilesAdded, stats.FilesUpdated,
            stats.FilesDeleted, stats.FilesUnchanged, stats.FilesFailed,
            stats.BytesTransferred, stats.PermissionsSynced, stats.PermissionsFailed);

        return stats;
    }

    private async Task SyncPermissionsAsync(
        BlobStorageClient blobClient, string driveId,
        List<SharePointFile> files, SyncStats stats,
        CancellationToken ct)
    {
        if (files.Count == 0) return;

        _logger.LogInformation("Syncing permissions for {Count} files", files.Count);

        await using var permClient = new PermissionsClient(driveId, _config, _logger);

        foreach (var spFile in files)
        {
            var blobName = blobClient.GetBlobName(spFile.Path);
            try
            {
                var perms = await permClient.GetFilePermissionsAsync(spFile.Id, spFile.Path, ct);
                if (perms.Permissions.Count > 0)
                {
                    _logger.LogInformation("Syncing permissions for {Path}: {Summary}",
                        spFile.Path, FilePermissions.PermissionsToSummary(perms.Permissions));
                    await blobClient.UpdateBlobMetadataAsync(blobName, perms.ToMetadata(), _config.DryRun, ct);
                    stats.PermissionsSynced++;
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to sync permissions for {Path}", spFile.Path);
                stats.PermissionsFailed++;
            }
        }
    }
}
