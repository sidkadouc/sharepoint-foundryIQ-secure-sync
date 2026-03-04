using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using SharePointSync.Core;

// ──────────────────────────────────────────────────────────────────────
// This executable works in two modes:
//
// 1. Console / ACA Job:   dotnet run  (or  docker run)
//    → Runs the sync once and exits.
//
// 2. Azure Function:  deployed with Azure Functions Worker
//    → Timer trigger invokes SyncTimerFunction on schedule.
//
// The mode is auto-detected via the FUNCTIONS_WORKER_RUNTIME env var.
// ──────────────────────────────────────────────────────────────────────

var isAzureFunction = !string.IsNullOrEmpty(
    Environment.GetEnvironmentVariable("FUNCTIONS_WORKER_RUNTIME"));

if (isAzureFunction)
{
    // ── Azure Function host ──
    var host = new HostBuilder()
        .ConfigureFunctionsWorkerDefaults()
        .ConfigureServices(services =>
        {
            services.AddSingleton(_ => SyncConfig.FromEnvironment());
        })
        .Build();

    await host.RunAsync();
}
else
{
    // ── Console / ACA Job mode ──
    using var loggerFactory = LoggerFactory.Create(b =>
        b.AddConsole().SetMinimumLevel(LogLevel.Information));
    var logger = loggerFactory.CreateLogger("SharePointSync");

    try
    {
        var config = SyncConfig.FromEnvironment();
        config.Validate();

        var job = new SyncJob(config, logger);
        var stats = await job.RunAsync();

        if (stats.HasFailures)
        {
            logger.LogWarning("Sync completed with failures: filesFailed={F}, permsFailed={P}",
                stats.FilesFailed, stats.PermissionsFailed);
            Environment.ExitCode = 1;
        }
    }
    catch (InvalidOperationException ex)
    {
        logger.LogError("Configuration error: {Error}", ex.Message);
        Environment.ExitCode = 2;
    }
    catch (Exception ex)
    {
        logger.LogError(ex, "Unexpected error during sync");
        Environment.ExitCode = 1;
    }
}
