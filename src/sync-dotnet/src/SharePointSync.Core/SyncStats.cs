namespace SharePointSync.Core;

/// <summary>
/// Statistics collected during a sync run.
/// </summary>
public sealed class SyncStats
{
    public int FilesScanned { get; set; }
    public int FilesAdded { get; set; }
    public int FilesUpdated { get; set; }
    public int FilesDeleted { get; set; }
    public int FilesUnchanged { get; set; }
    public int FilesFailed { get; set; }
    public long BytesTransferred { get; set; }
    public int PermissionsSynced { get; set; }
    public int PermissionsFailed { get; set; }
    public string SyncMode { get; set; } = "full"; // "full", "delta-initial", "delta-incremental"

    public bool HasFailures => FilesFailed > 0 || PermissionsFailed > 0;
}
