# ==============================================================================
# SharePoint to Blob Sync with AI Search - Complete Pipeline (PowerShell)
# ==============================================================================
# This script runs the full pipeline:
# 1. Sync files from SharePoint to Blob Storage
# 2. Create AI Search components (datasource, index, skillset, indexer)
# 3. Wait for indexing and run tests
#
# Usage: .\run-all.ps1
# ==============================================================================

$ErrorActionPreference = "Stop"

# Load environment variables from .env file
function Load-EnvFile {
    param([string]$Path = ".env")
    
    if (-not (Test-Path $Path)) {
        Write-Error "ERROR: .env file not found at $Path"
        exit 1
    }
    
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Write-Host "✓ Loaded environment variables from .env" -ForegroundColor Green
}

Load-EnvFile

# Validate required variables
$requiredVars = @(
    "SHAREPOINT_SITE_URL",
    "AZURE_STORAGE_ACCOUNT_NAME",
    "AZURE_BLOB_CONTAINER_NAME",
    "SEARCH_SERVICE_NAME",
    "SEARCH_API_KEY",
    "OPENAI_RESOURCE_URI",
    "EMBEDDING_DEPLOYMENT_ID",
    "SUBSCRIPTION_ID",
    "SEARCH_RESOURCE_GROUP"
)

foreach ($var in $requiredVars) {
    if (-not [Environment]::GetEnvironmentVariable($var)) {
        Write-Error "ERROR: Required variable $var is not set"
        exit 1
    }
}
Write-Host "✓ All required variables present" -ForegroundColor Green

# Get environment variables
$env:SEARCH_ENDPOINT = "https://$($env:SEARCH_SERVICE_NAME).search.windows.net"
$apiVersion = if ($env:API_VERSION) { $env:API_VERSION } else { "2025-11-01-preview" }

# ==============================================================================
# Step 1: Sync SharePoint to Blob
# ==============================================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Step 1: Syncing SharePoint to Blob Storage" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

Push-Location sync
pip install -q -r requirements.txt
python main.py 2>&1 | Select-Object -Last 5
Pop-Location

Write-Host "✓ SharePoint sync completed" -ForegroundColor Green

# ==============================================================================
# Step 2: Create AI Search Components
# ==============================================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Step 2: Creating AI Search Components" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$headers = @{
    "api-key" = $env:SEARCH_API_KEY
    "Content-Type" = "application/json"
}

function New-SearchComponent {
    param(
        [string]$Type,
        [string]$Name,
        [string]$FilePath
    )
    
    Write-Host "Creating ${Type}: $Name..."
    
    # Read and substitute variables
    $content = Get-Content $FilePath -Raw
    $content = $content -replace '\$\{dataSourceName\}', $env:DATASOURCE_NAME
    $content = $content -replace '\$\{subscriptionId\}', $env:SUBSCRIPTION_ID
    $content = $content -replace '\$\{resourceGroup\}', $env:SEARCH_RESOURCE_GROUP
    $content = $content -replace '\$\{storageAccount\}', $env:AZURE_STORAGE_ACCOUNT_NAME
    $content = $content -replace '\$\{containerName\}', $env:AZURE_BLOB_CONTAINER_NAME
    $content = $content -replace '\$\{indexName\}', $env:INDEX_NAME
    $content = $content -replace '\$\{indexerName\}', $env:INDEXER_NAME
    $content = $content -replace '\$\{skillsetName\}', $env:SKILLSET_NAME
    $content = $content -replace '\$\{embeddingDimensions\}', $env:EMBEDDING_DIMENSIONS
    $content = $content -replace '\$\{openAIResourceUri\}', $env:OPENAI_RESOURCE_URI
    $content = $content -replace '\$\{embeddingDeploymentId\}', $env:EMBEDDING_DEPLOYMENT_ID
    $content = $content -replace '\$\{embeddingModelName\}', $env:EMBEDDING_MODEL_NAME
    
    $uri = "$($env:SEARCH_ENDPOINT)/$Type/${Name}?api-version=$apiVersion"
    
    try {
        $response = Invoke-RestMethod -Uri $uri -Method Put -Headers $headers -Body $content
        Write-Host "  ✓ Created: $($response.name)" -ForegroundColor Green
    }
    catch {
        $errorMsg = $_.ErrorDetails.Message | ConvertFrom-Json
        Write-Host "  ✗ Error: $($errorMsg.error.message)" -ForegroundColor Red
        throw
    }
}

New-SearchComponent -Type "datasources" -Name $env:DATASOURCE_NAME -FilePath "ai-search/datasource.json"
New-SearchComponent -Type "indexes" -Name $env:INDEX_NAME -FilePath "ai-search/index.json"
New-SearchComponent -Type "skillsets" -Name $env:SKILLSET_NAME -FilePath "ai-search/skillset.json"
New-SearchComponent -Type "indexers" -Name $env:INDEXER_NAME -FilePath "ai-search/indexer.json"

Write-Host "✓ AI Search components created" -ForegroundColor Green

# ==============================================================================
# Step 3: Wait for Indexing
# ==============================================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Step 3: Waiting for Indexer" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$statusUri = "$($env:SEARCH_ENDPOINT)/indexers/$($env:INDEXER_NAME)/status?api-version=$apiVersion"

for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 5
    $status = Invoke-RestMethod -Uri $statusUri -Headers $headers
    $lastStatus = $status.lastResult.status
    $itemsProcessed = $status.lastResult.itemsProcessed
    Write-Host "  Indexer status: $lastStatus (items processed: $itemsProcessed)"
    
    if ($lastStatus -eq "success") {
        break
    }
}

# Get document count
$countUri = "$($env:SEARCH_ENDPOINT)/indexes/$($env:INDEX_NAME)/docs/`$count?api-version=$apiVersion"
$docCount = Invoke-RestMethod -Uri $countUri -Headers $headers
Write-Host "✓ Documents indexed: $docCount" -ForegroundColor Green

# ==============================================================================
# Step 4: Run Tests
# ==============================================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Step 4: Running Search Tests" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

Push-Location tests
python test_search.py -q "demo" 2>&1 | Select-String -Pattern "Document count|Vector Search|Total results"
Pop-Location

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  ✓ Pipeline Complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Summary:"
Write-Host "  - SharePoint files synced to: $($env:AZURE_STORAGE_ACCOUNT_NAME)/$($env:AZURE_BLOB_CONTAINER_NAME)"
Write-Host "  - AI Search index: $($env:INDEX_NAME)"
Write-Host "  - Documents indexed: $docCount"
