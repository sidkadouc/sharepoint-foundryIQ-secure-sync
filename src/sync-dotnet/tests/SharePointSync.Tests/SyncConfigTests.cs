using FluentAssertions;
using SharePointSync.Core;

namespace SharePointSync.Tests;

public sealed class SyncConfigTests
{
    [Fact]
    public void Validate_ShouldThrow_WhenRequiredValuesMissing()
    {
        var cfg = new SyncConfig();

        var act = () => cfg.Validate();

        act.Should().Throw<InvalidOperationException>()
            .WithMessage("*SHAREPOINT_SITE_URL is required*")
            .WithMessage("*AZURE_STORAGE_ACCOUNT_NAME is required*");
    }

    [Fact]
    public void Validate_ShouldPass_WhenRequiredValuesPresent()
    {
        var cfg = new SyncConfig
        {
            SharePointSiteUrl = "https://contoso.sharepoint.com/sites/demo",
            StorageAccountName = "stdemo",
            ContainerName = "sync"
        };

        var act = () => cfg.Validate();

        act.Should().NotThrow();
    }

    [Fact]
    public void ParseSiteUrl_ShouldReturnHostAndPath()
    {
        var cfg = new SyncConfig
        {
            SharePointSiteUrl = "https://contoso.sharepoint.com/sites/my-site"
        };

        var (host, path) = cfg.ParseSiteUrl();

        host.Should().Be("contoso.sharepoint.com");
        path.Should().Be("/sites/my-site");
    }

    [Fact]
    public void FromEnvironment_ShouldMapBooleansAndDefaults()
    {
        var scope = new EnvScope(new Dictionary<string, string?>
        {
            ["SHAREPOINT_SITE_URL"] = "https://contoso.sharepoint.com/sites/demo",
            ["AZURE_STORAGE_ACCOUNT_NAME"] = "stdemo",
            ["AZURE_BLOB_CONTAINER_NAME"] = "sync",
            ["DELETE_ORPHANED_BLOBS"] = "true",
            ["DRY_RUN"] = "TrUe",
            ["SYNC_PERMISSIONS"] = "false",
            ["FORCE_FULL_SYNC"] = null,
            ["RETRY_MAX_ATTEMPTS"] = "7",
            ["RETRY_BASE_DELAY_SECS"] = "1.5",
            ["RETRY_MAX_DELAY_SECS"] = "120"
        });

        var cfg = SyncConfig.FromEnvironment();

        cfg.DeleteOrphanedBlobs.Should().BeTrue();
        cfg.DryRun.Should().BeTrue();
        cfg.SyncPermissions.Should().BeFalse();
        cfg.ForceFullSync.Should().BeFalse();
        cfg.RetryMaxAttempts.Should().Be(7);
        cfg.RetryBaseDelaySecs.Should().Be(1.5);
        cfg.RetryMaxDelaySecs.Should().Be(120);
        cfg.BlobAccountUrl.Should().Be("https://stdemo.blob.core.windows.net");

        scope.Dispose();
    }

    private sealed class EnvScope : IDisposable
    {
        private readonly Dictionary<string, string?> _previousValues = new();
        private readonly List<string> _keys;

        public EnvScope(Dictionary<string, string?> values)
        {
            _keys = values.Keys.ToList();
            foreach (var key in _keys)
            {
                _previousValues[key] = Environment.GetEnvironmentVariable(key);
                Environment.SetEnvironmentVariable(key, values[key]);
            }
        }

        public void Dispose()
        {
            foreach (var key in _keys)
                Environment.SetEnvironmentVariable(key, _previousValues[key]);
        }
    }
}
