# ── Build stage ──
FROM mcr.microsoft.com/dotnet/sdk:10.0-preview AS build
WORKDIR /src

# Copy csproj files first to cache restore
COPY src/SharePointSync.Core/SharePointSync.Core.csproj src/SharePointSync.Core/
COPY src/SharePointSync.Job/SharePointSync.Job.csproj  src/SharePointSync.Job/
RUN dotnet restore src/SharePointSync.Job/SharePointSync.Job.csproj

# Copy everything else and publish
COPY src/ src/
RUN dotnet publish src/SharePointSync.Job/SharePointSync.Job.csproj \
    -c Release -o /app/publish

# ── Runtime stage ──
FROM mcr.microsoft.com/dotnet/aspnet:10.0-preview AS runtime
WORKDIR /app
COPY --from=build /app/publish .

# Default: console/ACA-job mode (no FUNCTIONS_WORKER_RUNTIME set)
ENTRYPOINT ["dotnet", "SharePointSync.Job.dll"]
