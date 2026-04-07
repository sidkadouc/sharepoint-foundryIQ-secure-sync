using System.Net.Http.Headers;
using System.Text.Json;
using Azure.Core;
using Microsoft.Extensions.Logging;
using Microsoft.Graph;
using Microsoft.Graph.Models;
using Polly;

namespace SharePointSync.Core;

/// <summary>
/// SharePoint client using Microsoft Graph API.
/// Supports Sites.Selected + full/delta sync — mirrors the Python version.
///
/// Graph endpoints called:
///   GET /sites/{hostname}:{sitePath}          — resolve site ID
///   GET /sites/{siteId}/drives                — list drives
///   GET /drives/{driveId}/root?$expand=children — list root children
///   GET /drives/{driveId}/root:/{path}        — resolve folder
///   GET /drives/{driveId}/items/{id}/children — list folder children
///   GET /drives/{driveId}/items/{id}/content  — download file
///   GET /drives/{driveId}/root/delta          — delta query
/// </summary>
public sealed class SharePointClient : IAsyncDisposable
{
    private readonly string _siteUrl;
    private readonly string _driveName;
    private readonly ILogger _logger;
    private readonly SyncConfig _config;
    private readonly TokenCredential _credential;
    private GraphServiceClient? _graphClient;
    private ResiliencePipeline _retry = ResiliencePipeline.Empty;

    public string? SiteId { get; private set; }
    public string? DriveId { get; private set; }

    public SharePointClient(SyncConfig config, ILogger logger)
    {
        _siteUrl = config.SharePointSiteUrl;
        _driveName = config.SharePointDriveName;
        _config = config;
        _logger = logger;
        _credential = CredentialFactory.ForSharePoint(logger);
    }

    /// <summary>Initialise the client: create Graph client and resolve site/drive IDs.</summary>
    public async Task InitializeAsync(CancellationToken ct = default)
    {
        _graphClient = new GraphServiceClient(_credential, new[] { "https://graph.microsoft.com/.default" });
        _retry = RetryPolicies.ForSdk(_config, _logger, "Graph");
        await ResolveIdsAsync(ct);
    }

    public (string SiteId, string DriveId) GetResolvedIds()
    {
        if (string.IsNullOrEmpty(SiteId) || string.IsNullOrEmpty(DriveId))
            throw new InvalidOperationException("IDs not resolved. Call InitializeAsync first.");
        return (SiteId!, DriveId!);
    }

    // ── Resolve site & drive ───────────────────────────────────────────

    private async Task ResolveIdsAsync(CancellationToken ct)
    {
        var (host, path) = _config.ParseSiteUrl();
        _logger.LogInformation("Resolving SharePoint site: host={Host} path={Path}", host, path);

        var site = await _retry.ExecuteAsync(async _ =>
            await _graphClient!.Sites[($"{host}:{path}")].GetAsync(cancellationToken: ct), ct);

        if (site?.Id is null) throw new InvalidOperationException($"Could not resolve site: {_siteUrl}");
        SiteId = site.Id;
        _logger.LogInformation("Resolved site ID={SiteId}, name={Name}", SiteId, site.DisplayName);

        var drives = await _retry.ExecuteAsync(async _ =>
            await _graphClient!.Sites[SiteId].Drives.GetAsync(cancellationToken: ct), ct);

        DriveId = drives?.Value?.FirstOrDefault(d =>
            string.Equals(d.Name, _driveName, StringComparison.OrdinalIgnoreCase))?.Id;

        if (DriveId is null)
        {
            var available = string.Join(", ", drives?.Value?.Select(d => d.Name ?? "?") ?? Array.Empty<string>());
            throw new InvalidOperationException($"Drive '{_driveName}' not found. Available: {available}");
        }

        _logger.LogInformation("Resolved drive ID={DriveId}", DriveId);
    }

    // ── List files (recursive) ─────────────────────────────────────────

    public async IAsyncEnumerable<SharePointFile> ListFilesAsync(
        string folderPath = "/",
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct = default)
    {
        _logger.LogInformation("Listing SharePoint files, folder={Folder}", folderPath);

        if (folderPath is "/" or "")
        {
            var root = await _retry.ExecuteAsync(async _ =>
                await _graphClient!.Drives[DriveId].Root.GetAsync(r =>
                    r.QueryParameters.Expand = new[] { "children" }, ct), ct);

            if (root?.Children is not null)
                foreach (var item in root.Children)
                    await foreach (var f in ProcessItemAsync(item, folderPath, ct))
                        yield return f;
        }
        else
        {
            var clean = folderPath.Trim('/');
            var folder = await _retry.ExecuteAsync(async _ =>
                await _graphClient!.Drives[DriveId].Root.ItemWithPath(clean).GetAsync(cancellationToken: ct), ct);

            if (folder?.Id is not null)
            {
                var children = await _retry.ExecuteAsync(async _ =>
                    await _graphClient!.Drives[DriveId].Items[folder.Id].Children.GetAsync(cancellationToken: ct), ct);

                if (children?.Value is not null)
                    foreach (var item in children.Value)
                        await foreach (var f in ProcessItemAsync(item, folderPath, ct))
                            yield return f;
            }
        }
    }

    private async IAsyncEnumerable<SharePointFile> ProcessItemAsync(
        DriveItem item, string parentPath,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
    {
        var currentPath = parentPath is "/" or ""
            ? $"/{item.Name}"
            : $"{parentPath.TrimEnd('/')}/{item.Name}";

        if (item.Folder is not null)
        {
            var children = await _retry.ExecuteAsync(async _ =>
                await _graphClient!.Drives[DriveId].Items[item.Id].Children.GetAsync(cancellationToken: ct), ct);

            if (children?.Value is not null)
                foreach (var child in children.Value)
                    await foreach (var f in ProcessItemAsync(child, currentPath, ct))
                        yield return f;
        }
        else if (item.File is not null)
        {
            yield return new SharePointFile(
                Id: item.Id ?? "",
                Name: item.Name ?? "",
                Path: currentPath,
                Size: item.Size ?? 0,
                LastModified: item.LastModifiedDateTime,
                ContentHash: item.CTag ?? item.ETag
            );
        }
    }

    // ── Download ───────────────────────────────────────────────────────

    public async Task<byte[]> DownloadFileAsync(string itemId, CancellationToken ct = default)
    {
        var stream = await _retry.ExecuteAsync(async _ =>
            await _graphClient!.Drives[DriveId].Items[itemId].Content.GetAsync(cancellationToken: ct), ct);

        if (stream is null) return Array.Empty<byte>();

        using var ms = new MemoryStream();
        await stream.CopyToAsync(ms, ct);
        return ms.ToArray();
    }

    // ── Delta query ────────────────────────────────────────────────────

    public async Task<DeltaResult> GetDeltaAsync(string? deltaLink = null, CancellationToken ct = default)
    {
        var isInitial = deltaLink is null;
        var url = deltaLink ?? $"https://graph.microsoft.com/v1.0/drives/{DriveId}/root/delta";

        _logger.LogInformation("Starting delta query, initial={IsInitial}", isInitial);

        // Use raw HTTP for delta because the SDK doesn't expose deltaLink/nextLink easily.
        var token = await GetAccessTokenAsync(ct);
        var httpRetry = RetryPolicies.ForHttp(_config, _logger);

        var changes = new List<DeltaChange>();
        string newDeltaLink = "";
        string? nextUrl = url;
        int page = 0;

        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };

        while (nextUrl is not null)
        {
            page++;
            using var req = new HttpRequestMessage(HttpMethod.Get, nextUrl);
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);

            var resp = await httpRetry.ExecuteAsync(async _ =>
            {
                // Clone request for retry (original may have been consumed).
                using var r = new HttpRequestMessage(HttpMethod.Get, nextUrl);
                r.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                var response = await http.SendAsync(r, ct);
                response.EnsureSuccessStatusCode();
                return response;
            }, ct);

            var json = await resp.Content.ReadAsStringAsync(ct);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (root.TryGetProperty("value", out var items))
            {
                _logger.LogInformation("Delta page {Page}, items={Count}", page, items.GetArrayLength());
                foreach (var item in items.EnumerateArray())
                {
                    var change = ParseDeltaItem(item);
                    if (change is not null) changes.Add(change);
                }
            }

            nextUrl = root.TryGetProperty("@odata.nextLink", out var nl) ? nl.GetString() : null;
            if (nextUrl is null && root.TryGetProperty("@odata.deltaLink", out var dl))
                newDeltaLink = dl.GetString() ?? "";
        }

        var fileChanges = changes.Where(c => !c.IsFolder).ToList();
        _logger.LogInformation("Delta complete: total={Total}, files={Files}, deleted={Del}",
            changes.Count, fileChanges.Count, changes.Count(c => c.ChangeType == DeltaChangeType.Deleted));

        return new DeltaResult
        {
            Changes = fileChanges,
            DeltaToken = newDeltaLink,
            IsInitialSync = isInitial,
        };
    }

    private static DeltaChange? ParseDeltaItem(JsonElement item)
    {
        var itemId = item.TryGetProperty("id", out var id) ? id.GetString() ?? "" : "";
        var itemName = item.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";

        // Build path from parentReference
        string parentPath = "";
        if (item.TryGetProperty("parentReference", out var pr) && pr.TryGetProperty("path", out var pp))
        {
            var raw = pp.GetString() ?? "";
            var colonIdx = raw.IndexOf(':');
            parentPath = colonIdx >= 0 ? raw[(colonIdx + 1)..] : "";
        }

        var itemPath = string.IsNullOrEmpty(parentPath)
            ? (string.IsNullOrEmpty(itemName) ? "" : $"/{itemName}")
            : $"{parentPath.TrimEnd('/')}/{itemName}";

        bool isFolder = item.TryGetProperty("folder", out _);

        // Deleted
        if (item.TryGetProperty("deleted", out _))
            return new DeltaChange(DeltaChangeType.Deleted, null, itemId, itemName, itemPath, isFolder);

        // Folder (caller filters these out)
        if (isFolder)
            return new DeltaChange(DeltaChangeType.CreatedOrModified, null, itemId, itemName, itemPath, true);

        // File
        if (item.TryGetProperty("file", out _))
        {
            DateTimeOffset? lastMod = null;
            if (item.TryGetProperty("lastModifiedDateTime", out var lm))
                lastMod = DateTimeOffset.TryParse(lm.GetString(), out var d) ? d : null;

            var downloadUrl = item.TryGetProperty("@microsoft.graph.downloadUrl", out var du) ? du.GetString() : null;
            var cTag = item.TryGetProperty("cTag", out var ct2) ? ct2.GetString() : null;
            var eTag = item.TryGetProperty("eTag", out var et) ? et.GetString() : null;

            var file = new SharePointFile(itemId, itemName, itemPath,
                item.TryGetProperty("size", out var sz) ? sz.GetInt64() : 0,
                lastMod, downloadUrl, cTag ?? eTag);

            return new DeltaChange(DeltaChangeType.CreatedOrModified, file, itemId, itemName, itemPath, false);
        }

        return null; // Unknown type
    }

    private async Task<string> GetAccessTokenAsync(CancellationToken ct)
    {
        var context = new TokenRequestContext(new[] { "https://graph.microsoft.com/.default" });
        var token = await _credential.GetTokenAsync(context, ct);
        return token.Token;
    }

    public ValueTask DisposeAsync()
    {
        // GraphServiceClient doesn't implement IDisposable; credential has no async disposal.
        return ValueTask.CompletedTask;
    }
}
