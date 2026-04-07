using FluentAssertions;
using SharePointSync.Core;

namespace SharePointSync.Tests;

public sealed class FilePermissionsTests
{
    [Fact]
    public void ToMetadata_ShouldIncludeUserAndGroupIds_WhenValidGuidsExist()
    {
        var userId = "11111111-1111-1111-1111-111111111111";
        var groupId = "22222222-2222-2222-2222-222222222222";

        var fp = new FilePermissions
        {
            FileId = "f1",
            FilePath = "folder/file.txt",
            SyncedAt = DateTimeOffset.Parse("2025-01-01T00:00:00Z"),
            Permissions =
            [
                new SharePointPermission("p1", ["read"], "user", "Alice", IdentityId: userId),
                new SharePointPermission("p2", ["read"], "group", "Readers", IdentityId: groupId),
                new SharePointPermission("p3", ["read"], "user", "Bad", IdentityId: "not-a-guid")
            ]
        };

        var meta = fp.ToMetadata();

        meta.Should().ContainKey(FilePermissions.MetaPermissions);
        meta.Should().ContainKey(FilePermissions.MetaPermissionsSyncedAt);
        meta[FilePermissions.MetaAclUserIds].Should().Be(userId);
        meta[FilePermissions.MetaAclGroupIds].Should().Be(groupId);
    }

    [Fact]
    public void ToMetadata_ShouldUsePlaceholders_WhenNoValidIds()
    {
        var fp = new FilePermissions
        {
            FileId = "f1",
            FilePath = "folder/file.txt",
            Permissions = [new SharePointPermission("p1", ["read"], "unknown", "X")]
        };

        var meta = fp.ToMetadata();

        meta[FilePermissions.MetaAclUserIds].Should().Be(FilePermissions.PlaceholderNoUsers);
        meta[FilePermissions.MetaAclGroupIds].Should().Be(FilePermissions.PlaceholderNoGroups);
    }

    [Fact]
    public void PermissionsToSummary_ShouldFormat_WithEmailAndWithoutEmail()
    {
        var text = FilePermissions.PermissionsToSummary(
        [
            new SharePointPermission("p1", ["read", "write"], "user", "Alice", "alice@contoso.com"),
            new SharePointPermission("p2", ["read"], "group", "Readers")
        ]);

        text.Should().Be("Alice<alice@contoso.com>:read,write; Readers:read");
    }
}
