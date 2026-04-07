using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Logging;
using Microsoft.Graph;
using Polly;

namespace SharePointSync.Core;

/// <summary>
/// Represents a single SharePoint permission entry.
/// </summary>
public sealed record SharePointPermission(
    string Id,
    List<string> Roles,
    string IdentityType,   // "user", "group", "siteGroup"
    string DisplayName,
    string? Email = null,
    string? IdentityId = null,
    bool Inherited = false
);

/// <summary>
/// All permissions for one file, with helpers to produce blob metadata
/// compatible with Azure AI Search ACL filtering.
/// </summary>
public sealed class FilePermissions
{
    // Metadata keys (same as Python)
    public const string MetaPermissions = "sharepoint_permissions";
    public const string MetaPermissionsSyncedAt = "permissions_synced_at";
    public const string MetaAclUserIds = "user_ids";
    public const string MetaAclGroupIds = "group_ids";
    public const string PlaceholderNoUsers = "00000000-0000-0000-0000-000000000000";
    public const string PlaceholderNoGroups = "00000000-0000-0000-0000-000000000001";

    private static readonly Regex GuidRegex = new(
        @"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        RegexOptions.Compiled);

    public string FilePath { get; init; } = "";
    public string FileId { get; init; } = "";
    public List<SharePointPermission> Permissions { get; init; } = new();
    public DateTimeOffset? SyncedAt { get; init; }

    /// <summary>Convert permissions to blob metadata dictionary.</summary>
    public Dictionary<string, string> ToMetadata()
    {
        var meta = new Dictionary<string, string>
        {
            [MetaPermissions] = JsonSerializer.Serialize(Permissions),
            [MetaPermissionsSyncedAt] = (SyncedAt ?? DateTimeOffset.UtcNow).ToString("O"),
        };

        var userIds = Permissions
            .Where(p => p.IdentityType == "user" && !string.IsNullOrEmpty(p.IdentityId) && GuidRegex.IsMatch(p.IdentityId!))
            .Select(p => p.IdentityId!)
            .Distinct().ToList();

        var groupIds = Permissions
            .Where(p => p.IdentityType == "group" && !string.IsNullOrEmpty(p.IdentityId) && GuidRegex.IsMatch(p.IdentityId!))
            .Select(p => p.IdentityId!)
            .Distinct().ToList();

        meta[MetaAclUserIds] = userIds.Count > 0 ? string.Join("|", userIds) : PlaceholderNoUsers;
        meta[MetaAclGroupIds] = groupIds.Count > 0 ? string.Join("|", groupIds) : PlaceholderNoGroups;

        return meta;
    }

    public static string PermissionsToSummary(IEnumerable<SharePointPermission> perms)
    {
        return string.Join("; ", perms.Select(p =>
        {
            var roles = string.Join(",", p.Roles);
            return string.IsNullOrEmpty(p.Email) ? $"{p.DisplayName}:{roles}" : $"{p.DisplayName}<{p.Email}>:{roles}";
        }));
    }
}

/// <summary>
/// Client for fetching SharePoint permissions via Graph API.
/// Endpoint: GET /drives/{driveId}/items/{itemId}/permissions
/// Requires Sites.Read.All or Sites.Selected (read role).
/// </summary>
public sealed class PermissionsClient : IAsyncDisposable
{
    private readonly string _driveId;
    private readonly ILogger _logger;
    private readonly GraphServiceClient _graphClient;
    private readonly ResiliencePipeline _retry;

    public PermissionsClient(string driveId, SyncConfig config, ILogger logger)
    {
        _driveId = driveId;
        _logger = logger;
        var credential = CredentialFactory.ForSharePoint(logger);
        _graphClient = new GraphServiceClient(credential, new[] { "https://graph.microsoft.com/.default" });
        _retry = RetryPolicies.ForSdk(config, logger, "Permissions");
    }

    public async Task<FilePermissions> GetFilePermissionsAsync(
        string fileId, string filePath, CancellationToken ct = default)
    {
        _logger.LogInformation("Fetching permissions for {Path}", filePath);

        var response = await _retry.ExecuteAsync(async _ =>
            await _graphClient.Drives[_driveId].Items[fileId].Permissions
                .GetAsync(cancellationToken: ct), ct);

        var perms = new List<SharePointPermission>();
        if (response?.Value is not null)
        {
            foreach (var p in response.Value)
            {
                var parsed = ParsePermission(p);
                if (parsed is not null) perms.Add(parsed);
            }
        }

        _logger.LogInformation("Got {Count} permissions for {Path}", perms.Count, filePath);
        return new FilePermissions
        {
            FilePath = filePath,
            FileId = fileId,
            Permissions = perms,
            SyncedAt = DateTimeOffset.UtcNow,
        };
    }

    private static SharePointPermission? ParsePermission(Microsoft.Graph.Models.Permission p)
    {
        try
        {
            var roles = p.Roles?.ToList() ?? new List<string>();
            var inherited = p.InheritedFrom is not null;
            string identityType = "unknown", displayName = "";
            string? email = null, identityId = null;

            var gtv2 = p.GrantedToV2;
            if (gtv2 is not null)
            {
                if (gtv2.User is not null)
                {
                    identityType = "user";
                    displayName = gtv2.User.DisplayName ?? "";
                    identityId = gtv2.User.Id;
                }
                else if (gtv2.Group is not null)
                {
                    identityType = "group";
                    displayName = gtv2.Group.DisplayName ?? "";
                    identityId = gtv2.Group.Id;
                }
                else if (gtv2.SiteGroup is not null)
                {
                    identityType = "siteGroup";
                    displayName = gtv2.SiteGroup.DisplayName ?? "";
                    identityId = gtv2.SiteGroup.Id;
                }
                else if (gtv2.SiteUser is not null)
                {
                    identityType = "user";
                    displayName = gtv2.SiteUser.DisplayName ?? "";
                    identityId = gtv2.SiteUser.Id;
                }
            }
            else if (p.GrantedTo?.User is not null)
            {
                identityType = "user";
                displayName = p.GrantedTo.User.DisplayName ?? "";
                identityId = p.GrantedTo.User.Id;
            }

            return new SharePointPermission(
                p.Id ?? "", roles, identityType, displayName, email, identityId, inherited);
        }
        catch
        {
            return null;
        }
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
