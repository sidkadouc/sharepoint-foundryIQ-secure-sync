# ==============================================================================
# Deploy Single Knowledge Source from Custom Artifacts
# ==============================================================================
# This script deploys a single AI Search knowledge source using JSON files
# from a specified artifact directory.
# ==============================================================================

param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,
    
    [Parameter(Mandatory = $true)]
    [string]$SearchServiceName,
    
    [Parameter(Mandatory = $true)]
    [string]$StorageAccountName,
    
    [Parameter(Mandatory = $true)]
    [string]$ArtifactDir,
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIResourceUri,
    
    [Parameter(Mandatory = $false)]
    [string]$ChatCompletionDeploymentId = "gpt-4.1",
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIDeploymentId = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [string]$OpenAIModelName = "text-embedding-3-large",
    
    [Parameter(Mandatory = $false)]
    [int]$OpenAIEmbeddingDimensions = 3072,
    
    [Parameter(Mandatory = $false)]
    [string]$StorageContainerName,
    
    [Parameter(Mandatory = $false)]
    [string]$IndexName,
    
    [Parameter(Mandatory = $false)]
    [string]$DataSourceName,
    
    [Parameter(Mandatory = $false)]
    [string]$SkillsetName,
    
    [Parameter(Mandatory = $false)]
    [string]$IndexerName,
    
    [Parameter(Mandatory = $false)]
    [string]$ApiVersion = "2025-11-01-preview",
    
    [Parameter(Mandatory = $false)]
    [switch]$WhatIf
)

# ==============================================================================
# Functions
# ==============================================================================

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Error {
    param([string]$Message)
    Write-Host "✗ $Message" -ForegroundColor Red
}

function Update-JsonContent {
    param(
        [string]$JsonContent,
        [hashtable]$Replacements
    )
    
    $updatedContent = $JsonContent
    
    foreach ($key in $Replacements.Keys) {
        $value = $Replacements[$key]
        $updatedContent = $updatedContent -replace [regex]::Escape($key), $value
    }
    
    return $updatedContent
}

function Invoke-SearchApi {
    param(
        [string]$Method,
        [string]$Endpoint,
        [string]$Body = $null,
        [hashtable]$Headers
    )
    
    try {
        if ($Body) {
            $response = Invoke-RestMethod -Uri $Endpoint -Headers $Headers -Method $Method -Body $Body -ContentType "application/json"
        } else {
            $response = Invoke-RestMethod -Uri $Endpoint -Headers $Headers -Method $Method
        }
        return $response
    }
    catch {
        Write-Error "API call failed: $($_.Exception.Message)"
        if ($_.ErrorDetails) {
            Write-Host "Error details: $($_.ErrorDetails.Message)" -ForegroundColor Red
        }
        throw
    }
}

# ==============================================================================
# Main Script
# ==============================================================================

Write-Host "Deploying Knowledge Source from: $ArtifactDir" -ForegroundColor Yellow

# Get Azure context
$azAccount = az account show | ConvertFrom-Json
$SubscriptionId = $azAccount.id
Write-Host "Subscription: $($azAccount.name)" -ForegroundColor Gray

# Get Search admin key
Write-Step "Retrieving Search Service admin key"
$token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
$headers = @{
    'Authorization' = "Bearer $token"
    'Content-Type' = 'application/json'
}

$url = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Search/searchServices/$SearchServiceName/listAdminKeys?api-version=2022-09-01"
$response = Invoke-RestMethod -Uri $url -Headers $headers -Method POST
$primaryAdminKey = $response.primaryKey
Write-Success "Retrieved admin key"

# Build storage connection string
$storageResourceId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroupName/providers/Microsoft.Storage/storageAccounts/$StorageAccountName"
$storageConnectionString = "ResourceId=$storageResourceId;"

# Prepare Search API headers
$searchHeaders = @{
    'api-key' = $primaryAdminKey
    'Content-Type' = 'application/json'
    'Accept' = 'application/json'
}

$baseUrl = "https://$SearchServiceName.search.windows.net"

# Common replacements
$replacements = @{
    '${subscriptionId}' = $SubscriptionId
    '${resourceGroup}' = $ResourceGroupName
    '${storageAccount}' = $StorageAccountName
    '${openAIResourceUri}' = $OpenAIResourceUri
    '${chatCompletionDeploymentId}' = $ChatCompletionDeploymentId
    '${embeddingDeploymentId}' = $OpenAIDeploymentId
    '${embeddingModelName}' = $OpenAIModelName
    '${embeddingDimensions}' = "$OpenAIEmbeddingDimensions"
}

# ==============================================================================
# Deploy Data Source
# ==============================================================================

Write-Step "Deploying Data Source"
$dataSourcePath = Join-Path $ArtifactDir "datasource.json"
$dataSourceJson = Get-Content -Path $dataSourcePath -Raw
$dataSourceJson = Update-JsonContent -JsonContent $dataSourceJson -Replacements $replacements

$dsObj = $dataSourceJson | ConvertFrom-Json
$dsObj.credentials.connectionString = $storageConnectionString
$dsName = $dsObj.name
$dataSourceBody = $dsObj | ConvertTo-Json -Depth 10

if (-not $WhatIf) {
    $dsUrl = "$baseUrl/datasources/$dsName`?api-version=$ApiVersion"
    Invoke-SearchApi -Method "PUT" -Endpoint $dsUrl -Body $dataSourceBody -Headers $searchHeaders
    Write-Success "Data source '$dsName' deployed"
} else {
    Write-Host "Would deploy data source: $dsName" -ForegroundColor Yellow
}

# ==============================================================================
# Deploy Index
# ==============================================================================

Write-Step "Deploying Index"
$indexPath = Join-Path $ArtifactDir "index.json"
$indexJson = Get-Content -Path $indexPath -Raw
$indexJson = Update-JsonContent -JsonContent $indexJson -Replacements $replacements

$idxObj = $indexJson | ConvertFrom-Json
$idxName = $idxObj.name
$indexBody = $idxObj | ConvertTo-Json -Depth 20

if (-not $WhatIf) {
    $idxUrl = "$baseUrl/indexes/$idxName`?api-version=$ApiVersion"
    Invoke-SearchApi -Method "PUT" -Endpoint $idxUrl -Body $indexBody -Headers $searchHeaders
    Write-Success "Index '$idxName' deployed"
} else {
    Write-Host "Would deploy index: $idxName" -ForegroundColor Yellow
}

# ==============================================================================
# Deploy Skillset
# ==============================================================================

Write-Step "Deploying Skillset"
$skillsetPath = Join-Path $ArtifactDir "skillset.json"
$skillsetJson = Get-Content -Path $skillsetPath -Raw
$skillsetJson = Update-JsonContent -JsonContent $skillsetJson -Replacements $replacements

$ssObj = $skillsetJson | ConvertFrom-Json
$ssName = $ssObj.name
$skillsetBody = $ssObj | ConvertTo-Json -Depth 20

if (-not $WhatIf) {
    $ssUrl = "$baseUrl/skillsets/$ssName`?api-version=$ApiVersion"
    Invoke-SearchApi -Method "PUT" -Endpoint $ssUrl -Body $skillsetBody -Headers $searchHeaders
    Write-Success "Skillset '$ssName' deployed"
} else {
    Write-Host "Would deploy skillset: $ssName" -ForegroundColor Yellow
}

# ==============================================================================
# Deploy Indexer
# ==============================================================================

Write-Step "Deploying Indexer"
$indexerPath = Join-Path $ArtifactDir "indexer.json"
$indexerJson = Get-Content -Path $indexerPath -Raw
$indexerJson = Update-JsonContent -JsonContent $indexerJson -Replacements $replacements

$ixrObj = $indexerJson | ConvertFrom-Json
$ixrName = $ixrObj.name
$indexerBody = $ixrObj | ConvertTo-Json -Depth 20

if (-not $WhatIf) {
    $ixrUrl = "$baseUrl/indexers/$ixrName`?api-version=$ApiVersion"
    Invoke-SearchApi -Method "PUT" -Endpoint $ixrUrl -Body $indexerBody -Headers $searchHeaders
    Write-Success "Indexer '$ixrName' deployed"
    
    # Run the indexer
    Write-Step "Running Indexer"
    $runUrl = "$baseUrl/indexers/$ixrName/run?api-version=$ApiVersion"
    Invoke-SearchApi -Method "POST" -Endpoint $runUrl -Headers $searchHeaders
    Write-Success "Indexer started"
} else {
    Write-Host "Would deploy indexer: $ixrName" -ForegroundColor Yellow
}

Write-Success "Knowledge source deployment complete!"
