using Microsoft.Extensions.Logging;
using Polly;
using Polly.Retry;

namespace SharePointSync.Core;

/// <summary>
/// Builds Polly retry pipelines for Graph API and storage calls.
/// Retries on 429, 5xx, and transient network errors with exponential backoff + jitter.
/// Honours the Retry-After header on 429 responses.
/// </summary>
public static class RetryPolicies
{
    private static readonly HashSet<int> RetryableStatusCodes = new() { 429, 500, 502, 503, 504 };

    /// <summary>Build an async retry pipeline for HTTP calls.</summary>
    public static ResiliencePipeline<HttpResponseMessage> ForHttp(SyncConfig config, ILogger logger)
    {
        return new ResiliencePipelineBuilder<HttpResponseMessage>()
            .AddRetry(new RetryStrategyOptions<HttpResponseMessage>
            {
                MaxRetryAttempts = config.RetryMaxAttempts,
                BackoffType = DelayBackoffType.Exponential,
                Delay = TimeSpan.FromSeconds(config.RetryBaseDelaySecs),
                MaxDelay = TimeSpan.FromSeconds(config.RetryMaxDelaySecs),
                UseJitter = true,
                ShouldHandle = new PredicateBuilder<HttpResponseMessage>()
                    .HandleResult(r => RetryableStatusCodes.Contains((int)r.StatusCode))
                    .Handle<HttpRequestException>()
                    .Handle<TaskCanceledException>(),
                OnRetry = args =>
                {
                    var status = args.Outcome.Result?.StatusCode;
                    logger.LogWarning("Retryable HTTP error — attempt {Attempt}/{Max}, status={Status}, delay={Delay:F1}s",
                        args.AttemptNumber + 1, config.RetryMaxAttempts,
                        status?.ToString() ?? args.Outcome.Exception?.GetType().Name ?? "unknown",
                        args.RetryDelay.TotalSeconds);
                    return ValueTask.CompletedTask;
                },
                DelayGenerator = args =>
                {
                    // Honour Retry-After header on 429.
                    var response = args.Outcome.Result;
                    if (response?.Headers.RetryAfter?.Delta is { } delta)
                        return ValueTask.FromResult<TimeSpan?>(delta);
                    if (response?.Headers.RetryAfter?.Date is { } date)
                    {
                        var wait = date - DateTimeOffset.UtcNow;
                        if (wait > TimeSpan.Zero)
                            return ValueTask.FromResult<TimeSpan?>(wait);
                    }
                    // Fallback to default exponential backoff
                    return ValueTask.FromResult<TimeSpan?>(null);
                },
            })
            .Build();
    }

    /// <summary>Build a generic async retry pipeline for SDK calls that throw exceptions.</summary>
    public static ResiliencePipeline ForSdk(SyncConfig config, ILogger logger, string operationName = "SDK")
    {
        return new ResiliencePipelineBuilder()
            .AddRetry(new RetryStrategyOptions
            {
                MaxRetryAttempts = config.RetryMaxAttempts,
                BackoffType = DelayBackoffType.Exponential,
                Delay = TimeSpan.FromSeconds(config.RetryBaseDelaySecs),
                MaxDelay = TimeSpan.FromSeconds(config.RetryMaxDelaySecs),
                UseJitter = true,
                ShouldHandle = new PredicateBuilder()
                    .Handle<HttpRequestException>()
                    .Handle<TaskCanceledException>()
                    .Handle<Microsoft.Graph.Models.ODataErrors.ODataError>(ex =>
                        ex.ResponseStatusCode is int code && RetryableStatusCodes.Contains(code)),
                OnRetry = args =>
                {
                    logger.LogWarning("{Op}: retryable error — attempt {Attempt}/{Max}, delay={Delay:F1}s, error={Error}",
                        operationName, args.AttemptNumber + 1, config.RetryMaxAttempts,
                        args.RetryDelay.TotalSeconds,
                        args.Outcome.Exception?.Message ?? "unknown");
                    return ValueTask.CompletedTask;
                },
            })
            .Build();
    }
}
