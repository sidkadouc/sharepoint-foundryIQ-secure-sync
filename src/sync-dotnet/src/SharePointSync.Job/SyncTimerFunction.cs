using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Logging;
using SharePointSync.Core;

namespace SharePointSync.Job;

/// <summary>
/// Azure Function timer trigger that runs the SharePoint-to-Blob sync.
/// Schedule is controlled by the SYNC_SCHEDULE env var (default: every 10 min).
/// </summary>
public class SyncTimerFunction
{
    private readonly SyncConfig _config;
    private readonly ILogger<SyncTimerFunction> _logger;

    public SyncTimerFunction(SyncConfig config, ILogger<SyncTimerFunction> logger)
    {
        _config = config;
        _logger = logger;
    }

    [Function("SharePointSyncTimer")]
    public async Task Run(
        [TimerTrigger("%SYNC_SCHEDULE%")] TimerInfo timerInfo)
    {
        _logger.LogInformation("SharePointSyncTimer triggered at {Now}", DateTime.UtcNow);

        if (timerInfo.IsPastDue)
            _logger.LogWarning("Timer is running late (past due).");

        _config.Validate();
        var job = new SyncJob(_config, _logger);
        var stats = await job.RunAsync();

        if (stats.HasFailures)
        {
            _logger.LogWarning(
                "Sync completed with failures: filesFailed={F}, permsFailed={P}",
                stats.FilesFailed, stats.PermissionsFailed);
        }
    }
}
