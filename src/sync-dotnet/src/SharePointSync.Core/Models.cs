namespace SharePointSync.Core;

/// <summary>Represents a file from SharePoint.</summary>
public sealed record SharePointFile(
    string Id,
    string Name,
    string Path,
    long Size,
    DateTimeOffset? LastModified,
    string? DownloadUrl = null,
    string? ContentHash = null
);

/// <summary>Type of change reported by the delta API.</summary>
public enum DeltaChangeType { CreatedOrModified, Deleted }

/// <summary>A single change from a delta query.</summary>
public sealed record DeltaChange(
    DeltaChangeType ChangeType,
    SharePointFile? File,
    string ItemId,
    string ItemName,
    string ItemPath,
    bool IsFolder
);

/// <summary>Result of a delta query.</summary>
public sealed class DeltaResult
{
    public List<DeltaChange> Changes { get; set; } = new();
    public string DeltaToken { get; set; } = string.Empty;
    public bool IsInitialSync { get; set; }
}
