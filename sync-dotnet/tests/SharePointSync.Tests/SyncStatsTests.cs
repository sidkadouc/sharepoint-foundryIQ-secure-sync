using FluentAssertions;
using SharePointSync.Core;

namespace SharePointSync.Tests;

public sealed class SyncStatsTests
{
    [Theory]
    [InlineData(0, 0, false)]
    [InlineData(1, 0, true)]
    [InlineData(0, 1, true)]
    [InlineData(2, 3, true)]
    public void HasFailures_ShouldReflectFailedCounters(int filesFailed, int permissionsFailed, bool expected)
    {
        var stats = new SyncStats
        {
            FilesFailed = filesFailed,
            PermissionsFailed = permissionsFailed
        };

        stats.HasFailures.Should().Be(expected);
    }
}
